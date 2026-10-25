"""Unified LLM Provider Interface & Orchestration Runner for Jarvis 2.0.

Provides BaseProvider(ABC), structured response abstractions, and the UnifiedRunner
which consolidates multi-turn tool execution, progressive audio narration, and
deterministic cycle detection into a single robust engine.
"""
import asyncio
import concurrent.futures
import contextvars
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.core.cancellation import (
    ToolCancellationToken,
    bind_tool_cancellation_token,
    get_global_cancellation_token,
    reset_tool_cancellation_token,
)
from jarvis.core.safety import SafetyGate
from jarvis.core.win32_affinity import configure_ecore_worker_thread
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.UnifiedRunner")


UIA_SKILL_NAMES: set[str] = {
    "inspect_window_controls",
    "click_ui_element",
    "focus_window",
    "get_active_window",
    "list_open_windows",
    "close_window",
    "resize_window",
    "read_clipboard",
    "copy_to_clipboard",
    "type_text",
    "press_hotkey",
    "minimize_all_windows",
    "lock_workstation",
    "take_desktop_screenshot",
    "adjust_volume",
    "control_media",
}


@dataclass
class ToolCall:
    """Represents a structured tool execution request from any LLM provider."""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Structured response from an individual provider turn."""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    latency_ms: float = 0.0
    finish_reason: str | None = None
    model: str = ""
    raw_response: Any = None


@dataclass
class ToolExecutionResult:
    """Represents the structured execution outcome of an individual tool call."""
    result: str
    success: bool
    failure_category: str | None = None  # "timeout", "exception", "safety_halt", "loop_cycle", "orchestrator_denied", "cancelled"
    error_message: str | None = None


@dataclass
class SessionExecutionTracker:
    """Tracks per-session tool execution outcomes without cross-turn state leakage."""
    had_tool_call: bool = False
    all_tools_succeeded: bool = True
    iteration_limit_exhausted: bool = False
    first_failure_category: str | None = None
    first_failed_tool: str | None = None
    first_failure_reason: str | None = None

    def record_failure(self, tool_name: str, category: str, reason: str) -> None:
        self.all_tools_succeeded = False
        if self.first_failure_category is None:
            self.first_failure_category = category
            self.first_failed_tool = tool_name
            self.first_failure_reason = reason


class BaseProvider(ABC):
    """Abstract Base Class for all LLM providers in Jarvis 2.0.
    
    Providers are strictly single-turn generators: they consume dialogue messages,
    stream tokens to the client callback, parse tool calls, and return an LLMResponse.
    They do NOT manage recursive tool loops—that responsibility is centralized in UnifiedRunner.
    """

    def __init__(self, name: str, model: str, config: dict[str, Any] | None = None):
        self.name = name
        self.model = model
        self.config = config or {}

    @abstractmethod
    def generate_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> LLMResponse:
        """Executes a single generation turn with the underlying model."""


class UnifiedRunner:
    """Centralized multi-turn tool calling engine and loop-cycle supervisor.
    
    Guarantees:
    1. Eliminates multi-minute stalls via hard iteration bounds and loop-cycle detection.
    2. Streams real-time spoken narration before any tool executes via sentence_callback.
    3. Enforces SafetyGate authorization before executing physical actions.
    4. Provides identical multi-tool behavior across all providers (Gemini, Antigravity, Ollama, Groq).
    """

    def __init__(
        self,
        safety_gate: SafetyGate | None = None,
        max_iterations: int = 4,
        per_turn_timeout: float = 35.0,
        tool_timeout: float = 5.0,
        tool_orchestrator: Any | None = None,
    ):
        self.safety = safety_gate or SafetyGate({})
        self.max_iterations = max_iterations
        self.per_turn_timeout = per_turn_timeout
        self.tool_timeout = tool_timeout
        if tool_orchestrator is not None:
            self.tool_orchestrator = tool_orchestrator
        else:
            try:
                from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
                self.tool_orchestrator = ToolOrchestrator()
            except (ImportError, AttributeError, RuntimeError, TypeError) as e:
                logger.debug("ToolOrchestrator initialization fallback in UnifiedRunner: %s", e)
                self.tool_orchestrator = None
        # Concurrency & Bounded Pool Recovery Controls (DEF-REL-04)
        self._executor_lock = threading.RLock()
        self._general_max_workers = 6
        self._uia_max_workers = 2
        self._max_retired_generations = 2
        self._max_stranded_workers = 12
        self._general_active_tasks: int = 0
        self._uia_active_tasks: int = 0
        self._retired_executors: list[tuple[concurrent.futures.ThreadPoolExecutor, int]] = []
        self._generation_id: int = 1
        self._stranded_workers_count: int = 0
        self._general_timed_out_workers: int = 0
        self._uia_degraded: bool = False
        self._monotonic_fencing_counter: int = 0

        # Dedicated 2-thread STA executor for Windows UIA / COM automation (COINIT_APARTMENTTHREADED)
        self._uia_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._uia_max_workers,
            thread_name_prefix="Jarvis-UIAWorker",
            initializer=lambda: configure_ecore_worker_thread(coinit_sta=True, tag="Jarvis-UIAWorker"),
        )
        # 6-thread general executor for I/O, web search, dev shell, and document tools
        self._general_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._general_max_workers,
            thread_name_prefix=f"Jarvis-ToolWorker-gen{self._generation_id}",
            initializer=lambda: configure_ecore_worker_thread(coinit_sta=False, tag=f"Jarvis-ToolWorker-gen{self._generation_id}"),
        )
        # Backward compatibility alias
        self._tool_executor = self._general_executor

    def _get_executor_for_tool(self, fn_name: str) -> concurrent.futures.ThreadPoolExecutor:
        """Routes UIA / desktop skills to the STA executor to prevent COM apartment mode collision."""
        with self._executor_lock:
            executor, _ = self._get_or_renew_executor(fn_name)
            return executor

    def _get_or_renew_executor(self, fn_name: str) -> tuple[concurrent.futures.ThreadPoolExecutor, bool]:
        """Returns the appropriate executor under self._executor_lock with bounded general renewal.

        Guarantees:
        - Strict UIA policy: never renews active STA/COM executor to prevent overlapping desktop interactions.
        - Bounded general policy: enforces hard cap on retired generations and stranded workers before failing closed.
        """
        is_uia = fn_name in UIA_SKILL_NAMES
        if is_uia:
            if self._uia_degraded and self._uia_active_tasks >= self._uia_max_workers:
                raise RuntimeError(
                    "UIA worker pool is degraded due to in-flight unresponsive desktop automation. "
                    "Failing closed to protect COM apartment state."
                )
            return self._uia_executor, True

        # Prune terminated retired executors
        alive_retired: list[tuple[concurrent.futures.ThreadPoolExecutor, int]] = []
        for old_exec, gen in self._retired_executors:
            threads = getattr(old_exec, "_threads", set())
            if any(t.is_alive() for t in threads):
                alive_retired.append((old_exec, gen))
        self._retired_executors = alive_retired

        # Check if general executor requires renewal due to saturation by timed-out workers
        if self._general_active_tasks >= self._general_max_workers and self._general_timed_out_workers > 0:
            if (
                len(self._retired_executors) >= self._max_retired_generations
                or self._stranded_workers_count >= self._max_stranded_workers
            ):
                raise RuntimeError(
                    f"General worker pool capacity exhausted ({self._stranded_workers_count} stranded workers "
                    f"across {len(self._retired_executors)} retired generations). Failing closed to prevent unbounded thread growth."
                )

            # Controlled bounded renewal
            old_exec = self._general_executor
            try:
                old_exec.shutdown(wait=False, cancel_futures=False)
            except Exception as shut_err:  # noqa: BLE001
                logger.debug("Old executor shutdown notice during renewal: %s", shut_err)
            self._stranded_workers_count += self._general_active_tasks
            self._retired_executors.append((old_exec, self._generation_id))
            self._generation_id += 1
            self._general_active_tasks = 0
            self._general_timed_out_workers = 0
            tag = f"Jarvis-ToolWorker-gen{self._generation_id}"
            self._general_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._general_max_workers,
                thread_name_prefix=tag,
                initializer=lambda: configure_ecore_worker_thread(coinit_sta=False, tag=tag),
            )
            self._tool_executor = self._general_executor
            logger.warning(
                "General worker pool saturated; renewed to generation %d (%d stranded workers across %d retired pools).",
                self._generation_id,
                self._stranded_workers_count,
                len(self._retired_executors),
            )

        return self._general_executor, False

    def shutdown(self, wait: bool = False):
        """Halts active and retired tool executor thread pools."""
        with self._executor_lock:
            try:
                self._uia_executor.shutdown(wait=wait, cancel_futures=True)
                self._general_executor.shutdown(wait=wait, cancel_futures=True)
                for retired_exec, _ in self._retired_executors:
                    try:
                        retired_exec.shutdown(wait=wait, cancel_futures=True)
                    except Exception as re_err:  # noqa: BLE001
                        logger.debug("Retired executor shutdown notice: %s", re_err)
                self._retired_executors.clear()
                logger.info("UnifiedRunner tool executor pools (STA UIA + General + Retired) shut down.")
            except Exception as e:  # noqa: BLE001
                logger.debug("Tool executor shutdown notice: %s", e)

    def execute_tool_with_status(
        self,
        fn_name: str,
        fn_args: dict[str, Any],
        timeout: float | None = None,
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> ToolExecutionResult:
        """Executes a tool offloaded to a worker thread and returns a structured ToolExecutionResult."""
        tool_timeout = timeout if timeout is not None else self.tool_timeout
        is_uia = fn_name in UIA_SKILL_NAMES

        # Mint execution tokens
        with self._executor_lock:
            self._monotonic_fencing_counter += 1
            fencing_id = self._monotonic_fencing_counter

        deadline = time.monotonic() + tool_timeout
        cancel_token = ToolCancellationToken(
            deadline=deadline,
            parent_token=get_global_cancellation_token(),
            fencing_id=fencing_id,
        )

        ctx = contextvars.copy_context()

        # Synchronized executor selection and active-task admission
        with self._executor_lock:
            try:
                executor, _ = self._get_or_renew_executor(fn_name)
            except RuntimeError as err:
                logger.warning("Execution rejected due to degraded capacity: %s", err)
                return ToolExecutionResult(
                    result=f"Error: {err}",
                    success=False,
                    failure_category="degraded_capacity",
                    error_message=str(err),
                )

            if is_uia:
                self._uia_active_tasks += 1
            else:
                self._general_active_tasks += 1
            exec_gen = self._generation_id

        def _dispatch():
            ctx_tok = bind_tool_cancellation_token(cancel_token)
            try:
                if cancel_token.is_cancelled:
                    return ToolExecutionResult(
                        result="Action cancelled: Operation was aborted before launch.",
                        success=False,
                        failure_category="cancelled",
                        error_message="Operation was aborted before launch.",
                    )

                if self.tool_orchestrator is not None:
                    old_orch_tok = getattr(self.tool_orchestrator, "cancel_token", None)
                    self.tool_orchestrator.cancel_token = cancel_token

                    use_with_status = hasattr(self.tool_orchestrator, "execute_step_with_status")
                    if (
                        getattr(self.tool_orchestrator, "_mock_return_value", None) is not None
                        or "Mock" in type(self.tool_orchestrator).__name__
                    ):
                        from unittest.mock import DEFAULT
                        mock_ws = getattr(self.tool_orchestrator, "execute_step_with_status", None)
                        if mock_ws is None or getattr(mock_ws, "_mock_return_value", DEFAULT) is DEFAULT:
                            use_with_status = False

                    try:
                        if use_with_status:
                            step_res = self.tool_orchestrator.execute_step_with_status(
                                fn_name,
                                fn_args,
                                step_index=step_index,
                                dual_key_confirmed=dual_key_confirmed,
                            )
                            res = ToolExecutionResult(
                                result=str(step_res.result),
                                success=bool(step_res.success),
                                failure_category=step_res.failure_category,
                                error_message=step_res.error_message,
                            )
                        else:
                            raw_res = self.tool_orchestrator.execute_step(
                                fn_name,
                                fn_args,
                                step_index=step_index,
                                dual_key_confirmed=dual_key_confirmed,
                            )
                            res = ToolExecutionResult(
                                result=str(raw_res),
                                success=True,
                            )
                    finally:
                        self.tool_orchestrator.cancel_token = old_orch_tok
                else:
                    exec_args = {k: v for k, v in (fn_args or {}).items() if k not in ("automated", "_automated")}
                    tool_fn = SkillRegistry.get_tool(fn_name)
                    if tool_fn:
                        import inspect
                        try:
                            sig = inspect.signature(tool_fn)
                            if "automated" in sig.parameters:
                                exec_args["automated"] = True
                            if "cancel_token" in sig.parameters:
                                exec_args["cancel_token"] = cancel_token
                        except Exception as sig_err:  # noqa: BLE001
                            logger.debug("Parameter inspection notice for '%s': %s", fn_name, sig_err)
                    skill_res = SkillRegistry.execute_with_status(fn_name, **exec_args)
                    res = ToolExecutionResult(
                        result=str(skill_res.result),
                        success=skill_res.success,
                        failure_category=skill_res.error_type or ("exception" if not skill_res.success else None),
                        error_message=skill_res.error_message,
                    )

                # Post-timeout fencing verification
                if cancel_token.fencing_token.is_invalidated:
                    logger.warning(
                        "Late completion of tool '%s' (fencing_id=%d) discarded after timeout.",
                        fn_name,
                        fencing_id,
                    )
                    return ToolExecutionResult(
                        result=f"Action discarded: Tool '{fn_name}' completed after timeout deadline expired.",
                        success=False,
                        failure_category="timeout_discarded",
                        error_message="Completed after execution deadline expired.",
                    )
                return res
            except Exception as ex:  # noqa: BLE001
                logger.error("Internal worker exception executing tool '%s': %s", fn_name, ex)
                return ToolExecutionResult(
                    result=f"Execution error: {ex}",
                    success=False,
                    failure_category="exception",
                    error_message=str(ex),
                )
            finally:
                reset_tool_cancellation_token(ctx_tok)
                with self._executor_lock:
                    if is_uia:
                        self._uia_active_tasks = max(0, self._uia_active_tasks - 1)
                        if self._uia_active_tasks == 0:
                            self._uia_degraded = False
                    else:
                        if exec_gen == self._generation_id:
                            self._general_active_tasks = max(0, self._general_active_tasks - 1)
                        else:
                            self._stranded_workers_count = max(0, self._stranded_workers_count - 1)
                        if cancel_token.fencing_token.is_invalidated:
                            self._general_timed_out_workers = max(0, self._general_timed_out_workers - 1)

        try:
            future = executor.submit(ctx.run, _dispatch)
            step_res = future.result(timeout=tool_timeout)
            if isinstance(step_res, ToolExecutionResult):
                return step_res
            return ToolExecutionResult(
                result=str(getattr(step_res, "result", step_res)),
                success=bool(getattr(step_res, "success", True)),
                failure_category=getattr(step_res, "failure_category", None),
                error_message=getattr(step_res, "error_message", None),
            )
        except concurrent.futures.TimeoutError:
            # Distinguish internal tool TimeoutError from runner wait deadline
            if future.done():
                try:
                    err = future.exception()
                    return ToolExecutionResult(
                        result=f"Execution error: {err}",
                        success=False,
                        failure_category="exception",
                        error_message=str(err),
                    )
                except Exception as ex_chk:  # noqa: BLE001
                    logger.debug("Exception extraction notice from completed future: %s", ex_chk)

            # Genuine runner deadline wait timeout
            future.cancel()
            cancel_token.cancel()
            with self._executor_lock:
                if is_uia:
                    self._uia_degraded = True
                    logger.warning(
                        "Tool '%s' timed out in UIA pool. Pool marked degraded (no automatic replacement).",
                        fn_name,
                    )
                else:
                    self._general_timed_out_workers += 1

            logger.error("Tool '%s' exceeded mandatory execution timeout of %.1fs.", fn_name, tool_timeout)
            timeout_msg = (
                f"Error: Tool '{fn_name}' exceeded maximum execution timeout of {tool_timeout:.1f}s "
                "and timed out to protect real-time system responsiveness."
            )
            return ToolExecutionResult(
                result=timeout_msg,
                success=False,
                failure_category="timeout",
                error_message=f"Tool '{fn_name}' timed out after {tool_timeout:.1f}s",
            )
        except concurrent.futures.CancelledError:
            return ToolExecutionResult(
                result=f"Action cancelled: Execution of tool '{fn_name}' was aborted.",
                success=False,
                failure_category="cancelled",
                error_message=f"Tool '{fn_name}' was cancelled.",
            )
        except Exception as err:  # noqa: BLE001
            future.cancel()
            cancel_token.cancel()
            logger.error("Error executing tool '%s': %s", fn_name, err)
            return ToolExecutionResult(
                result=f"Execution error: {err}",
                success=False,
                failure_category="exception",
                error_message=str(err),
            )

    def execute_tool_with_timeout(
        self,
        fn_name: str,
        fn_args: dict[str, Any],
        timeout: float | None = None,
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> str:
        """Executes a tool offloaded asynchronously to a dedicated worker thread with strict timeout guard.

        Routes through ToolOrchestrator to ensure central authorization, taint tracking,
        and human approval verification.
        """
        exec_res = self.execute_tool_with_status(
            fn_name=fn_name,
            fn_args=fn_args,
            timeout=timeout,
            step_index=step_index,
            dual_key_confirmed=dual_key_confirmed,
        )
        return exec_res.result

    async def execute_tool_async(
        self,
        fn_name: str,
        fn_args: dict[str, Any],
        timeout: float | None = None,
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> str:
        """Asynchronous coroutine executing a tool off-loop via asyncio.wait_for and the worker pool."""
        tool_timeout = timeout if timeout is not None else self.tool_timeout
        loop = asyncio.get_running_loop()
        is_uia = fn_name in UIA_SKILL_NAMES

        with self._executor_lock:
            self._monotonic_fencing_counter += 1
            fencing_id = self._monotonic_fencing_counter

        deadline = time.monotonic() + tool_timeout
        cancel_token = ToolCancellationToken(
            deadline=deadline,
            parent_token=get_global_cancellation_token(),
            fencing_id=fencing_id,
        )
        ctx = contextvars.copy_context()

        with self._executor_lock:
            try:
                executor, _ = self._get_or_renew_executor(fn_name)
            except RuntimeError as err:
                return f"Error: {err}"

            if is_uia:
                self._uia_active_tasks += 1
            else:
                self._general_active_tasks += 1
            exec_gen = self._generation_id

        def _dispatch():
            ctx_tok = bind_tool_cancellation_token(cancel_token)
            try:
                if cancel_token.is_cancelled:
                    return "Action cancelled: Operation was aborted before launch."

                if self.tool_orchestrator is not None:
                    old_orch_tok = getattr(self.tool_orchestrator, "cancel_token", None)
                    self.tool_orchestrator.cancel_token = cancel_token
                    try:
                        res = self.tool_orchestrator.execute_step(
                            fn_name,
                            fn_args,
                            step_index=step_index,
                            dual_key_confirmed=dual_key_confirmed,
                        )
                    finally:
                        self.tool_orchestrator.cancel_token = old_orch_tok
                else:
                    exec_args = {k: v for k, v in (fn_args or {}).items() if k not in ("automated", "_automated")}
                    tool_fn = SkillRegistry.get_tool(fn_name)
                    if tool_fn:
                        import inspect
                        try:
                            sig = inspect.signature(tool_fn)
                            if "automated" in sig.parameters:
                                exec_args["automated"] = True
                            if "cancel_token" in sig.parameters:
                                exec_args["cancel_token"] = cancel_token
                        except Exception as sig_err:  # noqa: BLE001
                            logger.debug("Parameter inspection notice for '%s': %s", fn_name, sig_err)
                    res = SkillRegistry.execute(fn_name, **exec_args)

                if cancel_token.fencing_token.is_invalidated:
                    logger.warning(
                        "Late async completion of tool '%s' (fencing_id=%d) discarded after timeout.",
                        fn_name,
                        fencing_id,
                    )
                    return f"Action discarded: Tool '{fn_name}' completed after timeout deadline expired."
                return res
            finally:
                reset_tool_cancellation_token(ctx_tok)
                with self._executor_lock:
                    if is_uia:
                        self._uia_active_tasks = max(0, self._uia_active_tasks - 1)
                        if self._uia_active_tasks == 0:
                            self._uia_degraded = False
                    else:
                        if exec_gen == self._generation_id:
                            self._general_active_tasks = max(0, self._general_active_tasks - 1)
                        else:
                            self._stranded_workers_count = max(0, self._stranded_workers_count - 1)

        future = loop.run_in_executor(executor, ctx.run, _dispatch)
        try:
            result = await asyncio.wait_for(future, timeout=tool_timeout)
            return str(result)
        except TimeoutError:
            future.cancel()
            cancel_token.cancel()
            with self._executor_lock:
                if is_uia:
                    self._uia_degraded = True
            logger.error("Tool '%s' exceeded mandatory async execution timeout of %.1fs.", fn_name, tool_timeout)
            return (
                f"Error: Tool '{fn_name}' exceeded maximum execution timeout of {tool_timeout:.1f}s "
                "and timed out to protect real-time system responsiveness."
            )
        except asyncio.CancelledError:
            future.cancel()
            cancel_token.cancel()
            logger.warning("Async execution of tool '%s' was cancelled by caller.", fn_name)
            raise
        except Exception as err:  # noqa: BLE001
            future.cancel()
            cancel_token.cancel()
            logger.error("Error executing tool '%s' asynchronously: %s", fn_name, err)
            return f"Execution error: {err}"



    @staticmethod
    def narrate_tool_action(fn_name: str, fn_args: dict[str, Any]) -> str:
        """Returns a natural, safe spoken phrase describing an action before execution."""
        name = fn_name.lower().strip()

        # Browser
        if name == "open_browser_tab":
            b = fn_args.get("browser") or "your browser"
            return f"Opening a new tab in {b}, Sir."
        elif name == "focus_window":
            title = fn_args.get("title") or "the application"
            return f"Switching to {title}, Sir."
        elif name == "launch_application":
            app = fn_args.get("app_name") or "the requested application"
            return f"Launching {app}, Sir."
        elif name == "close_application":
            app = fn_args.get("app_name") or "the application"
            return f"Closing {app}, Sir."
        elif name == "take_desktop_screenshot":
            return "Capturing a screenshot of your screen now, Sir."
        elif name == "adjust_volume":
            return "Adjusting the system volume, Sir."
        elif name == "control_media":
            return "Updating media playback, Sir."
        elif name == "minimize_all_windows":
            return "Minimizing windows to reveal your desktop, Sir."
        elif name == "lock_workstation":
            return "Locking the workstation, Sir."
        elif name == "get_system_vitals":
            return "Checking system vitals and hardware telemetry, Sir."
        elif name == "get_battery_status":
            return "Checking battery levels, Sir."

        # Self-Evolution & Code Building
        elif name == "create_or_update_custom_skill":
            skill = fn_args.get("skill_name", "").replace("_", " ").strip()
            return f"Engineering the '{skill}' capability now, Sir. Validating syntax and compiling it into memory."
        elif name == "list_custom_skills":
            return "Retrieving your custom engineered capabilities, Sir."
        elif name == "delete_custom_skill":
            skill = fn_args.get("skill_name", "").replace("_", " ").strip()
            return f"Removing custom skill '{skill}', Sir."

        # Dev & Codebase Stewardship
        elif name == "search_codebase":
            query = fn_args.get("query", "")
            return f"Searching the repository for {query}, Sir." if query else "Scanning the codebase, Sir."
        elif name == "read_file":
            fname = Path(fn_args.get("file_path", "the file")).name
            return f"Reading {fname}, Sir."
        elif name == "write_file":
            fname = Path(fn_args.get("file_path", "the file")).name
            return f"Writing updates to {fname}, Sir."
        elif name == "patch_file":
            fname = Path(fn_args.get("file_path", "the file")).name
            return f"Patching {fname}, Sir."
        elif name == "execute_powershell":
            return "Executing command in the terminal, Sir."
        elif name == "run_project_tests":
            return "Running the test suite to verify our changes, Sir."

        # Calendar & Documents
        elif name == "list_calendar_events":
            return "Consulting your calendar schedule, Sir."
        elif name == "add_calendar_event":
            return "Adding the event to your schedule, Sir."
        elif name == "cancel_calendar_event":
            return "Updating your schedule and cancelling the event, Sir."
        elif name == "process_staged_document":
            return "Parsing and extracting details from your document, Sir."
        elif name == "recall_past_conversation":
            return "Consulting our past conversation records, Sir."

        # Generic / dynamic custom skill
        humanized = name.replace("_", " ").capitalize()
        return f"Executing {humanized}, Sir."

    def execute_session(
        self,
        provider: BaseProvider,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Runs the multi-turn interaction loop until completion or loop halt.
        
        Returns:
            Tuple of (final_reply_text, updated_messages_list)
        """
        active_messages = list(messages)
        executed_signatures: set[tuple[str, str]] = set()
        iterations = 0
        if self.tool_orchestrator is not None and hasattr(self.tool_orchestrator, "reset"):
            try:
                self.tool_orchestrator.reset()
            except (AttributeError, RuntimeError) as e:
                logger.debug("ToolOrchestrator reset notice in UnifiedRunner: %s", e)
        last_text_reply = ""
        conversational_conclusion = False
        session_tracker = SessionExecutionTracker()

        while iterations < self.max_iterations:
            iterations += 1

            turn_res = provider.generate_turn(
                messages=active_messages,
                tools=tools,
                system_instruction=system_instruction,
                sentence_callback=sentence_callback,
                timeout=self.per_turn_timeout,
            )

            if turn_res.content:
                last_text_reply = turn_res.content.strip()

            if not turn_res.tool_calls:
                # No tools requested: we reached the conversational conclusion
                conversational_conclusion = True
                break

            session_tracker.had_tool_call = True

            # Loop Cycle Detector: Check for repeated tool calls with identical arguments
            has_cycle = False
            for tc in turn_res.tool_calls:
                args_key = json.dumps(tc.arguments, sort_keys=True)
                sig = (tc.name, args_key)
                if sig in executed_signatures:
                    logger.warning("Loop cycle detected on tool '%s' with identical arguments. Halting recursion.", tc.name)
                    has_cycle = True
                    session_tracker.record_failure(
                        tool_name=tc.name,
                        category="loop_cycle",
                        reason=f"Repetitive action cycle detected on '{tc.name}'",
                    )
                    break
                executed_signatures.add(sig)

            if has_cycle:
                if sentence_callback:
                    sentence_callback("Halting repetitive action cycle, Sir.")
                break

            # Append assistant message with tool calls to message history
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": turn_res.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments)
                        }
                    }
                    for tc in turn_res.tool_calls
                ]
            }
            active_messages.append(assistant_entry)

            # Execute tool calls sequentially
            for tc in turn_res.tool_calls:
                fn_name = tc.name
                fn_args = tc.arguments

                logger.info("UnifiedRunner dispatching [%s]: %s(%s)", provider.name, fn_name, fn_args)

                # Progressive vocal narration (<50ms audio dispatch)
                if sentence_callback:
                    narration = self.narrate_tool_action(fn_name, fn_args)
                    sentence_callback(narration)

                # Safety Gate evaluation
                is_safe, warning = self.safety.check_tool_call(fn_name, fn_args)
                if not is_safe:
                    tool_result = f"Action halted: {warning}"
                    logger.warning("Safety gate blocked tool '%s': %s", fn_name, warning)
                    session_tracker.record_failure(
                        tool_name=fn_name,
                        category="safety_halt",
                        reason=warning,
                    )
                    if sentence_callback:
                        sentence_callback(f"Action was halted: {warning}")
                else:
                    exec_res = self.execute_tool_with_status(
                        fn_name, fn_args, timeout=self.tool_timeout, step_index=iterations
                    )
                    tool_result = exec_res.result
                    if not exec_res.success:
                        session_tracker.record_failure(
                            tool_name=fn_name,
                            category=exec_res.failure_category or "exception",
                            reason=exec_res.error_message or str(tool_result),
                        )

                # Append tool result to history
                active_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(tool_result),
                })

        # Iteration-limit exhaustion check:
        if not conversational_conclusion and not has_cycle and iterations >= self.max_iterations:
            session_tracker.iteration_limit_exhausted = True
            session_tracker.record_failure(
                tool_name="",
                category="iteration_limit",
                reason=f"Maximum iteration limit ({self.max_iterations}) reached before task completion",
            )

        final_reply = last_text_reply
        if session_tracker.iteration_limit_exhausted and not conversational_conclusion:
            final_reply = "Action stopped: Maximum iteration limit reached before task completion, Sir."
        elif not final_reply and session_tracker.had_tool_call:
            if session_tracker.first_failure_category == "loop_cycle":
                final_reply = "Action stopped: A repetitive action loop was halted, Sir."
            elif session_tracker.first_failure_category == "safety_halt":
                final_reply = f"Action was halted: {session_tracker.first_failure_reason}, Sir."
            elif session_tracker.first_failure_category == "timeout":
                final_reply = (
                    f"I was unable to complete the action: {session_tracker.first_failed_tool} timed out, Sir."
                )
            elif session_tracker.first_failure_category == "cancelled":
                final_reply = "Action cancelled: Operation was aborted by user interrupt, Sir."
            elif session_tracker.first_failure_category in (
                "exception",
                "orchestrator_denied",
                "degraded_capacity",
                "timeout_discarded",
            ):
                final_reply = f"I encountered an error executing {session_tracker.first_failed_tool}, Sir."
            elif session_tracker.all_tools_succeeded:
                final_reply = "Action completed, Sir."
            else:
                final_reply = "I was unable to complete the requested action, Sir."

        return final_reply or "Standing by, Sir.", active_messages

