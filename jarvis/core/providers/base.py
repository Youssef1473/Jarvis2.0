"""Unified LLM Provider Interface & Orchestration Runner for Jarvis 2.0.

Provides BaseProvider(ABC), structured response abstractions, and the UnifiedRunner
which consolidates multi-turn tool execution, progressive audio narration, and
deterministic cycle detection into a single robust engine.
"""
from abc import ABC, abstractmethod
import asyncio
import concurrent.futures
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from jarvis.skills.base import SkillRegistry
from jarvis.core.safety import SafetyGate
from jarvis.core.win32_affinity import configure_ecore_worker_thread

logger = logging.getLogger("Jarvis.UnifiedRunner")


UIA_SKILL_NAMES: Set[str] = {
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
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Structured response from an individual provider turn."""
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    latency_ms: float = 0.0
    finish_reason: Optional[str] = None
    model: str = ""
    raw_response: Any = None


class BaseProvider(ABC):
    """Abstract Base Class for all LLM providers in Jarvis 2.0.
    
    Providers are strictly single-turn generators: they consume dialogue messages,
    stream tokens to the client callback, parse tool calls, and return an LLMResponse.
    They do NOT manage recursive tool loops—that responsibility is centralized in UnifiedRunner.
    """

    def __init__(self, name: str, model: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.model = model
        self.config = config or {}

    @abstractmethod
    def generate_turn(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_instruction: Optional[str] = None,
        sentence_callback: Optional[Callable[[str], None]] = None,
        timeout: float = 30.0,
    ) -> LLMResponse:
        """Executes a single generation turn with the underlying model."""
        pass


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
        safety_gate: Optional[SafetyGate] = None,
        max_iterations: int = 4,
        per_turn_timeout: float = 35.0,
        tool_timeout: float = 5.0,
    ):
        self.safety = safety_gate or SafetyGate({})
        self.max_iterations = max_iterations
        self.per_turn_timeout = per_turn_timeout
        self.tool_timeout = tool_timeout
        # Dedicated 2-thread STA executor for Windows UIA / COM automation (COINIT_APARTMENTTHREADED)
        self._uia_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="Jarvis-UIAWorker",
            initializer=lambda: configure_ecore_worker_thread(coinit_sta=True, tag="Jarvis-UIAWorker"),
        )
        # 6-thread general executor for I/O, web search, dev shell, and document tools
        self._general_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=6,
            thread_name_prefix="Jarvis-ToolWorker",
            initializer=lambda: configure_ecore_worker_thread(coinit_sta=False, tag="Jarvis-ToolWorker"),
        )
        # Backward compatibility alias
        self._tool_executor = self._general_executor

    def _get_executor_for_tool(self, fn_name: str) -> concurrent.futures.ThreadPoolExecutor:
        """Routes UIA / desktop skills to the STA executor to prevent COM apartment mode collision."""
        if fn_name in UIA_SKILL_NAMES:
            return self._uia_executor
        return self._general_executor

    def shutdown(self, wait: bool = False):
        """Halts the tool executor thread pools immediately."""
        try:
            self._uia_executor.shutdown(wait=wait, cancel_futures=True)
            self._general_executor.shutdown(wait=wait, cancel_futures=True)
            logger.info("UnifiedRunner tool executor pools (STA UIA + General) shut down.")
        except Exception as e:
            logger.debug(f"Tool executor shutdown notice: {e}")

    def execute_tool_with_timeout(
        self,
        fn_name: str,
        fn_args: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> str:
        """Executes a tool offloaded asynchronously to a dedicated worker thread with strict timeout guard."""
        tool_timeout = timeout if timeout is not None else self.tool_timeout
        executor = self._get_executor_for_tool(fn_name)
        try:
            future = executor.submit(SkillRegistry.execute, fn_name, **fn_args)
            result = future.result(timeout=tool_timeout)
            return str(result)
        except concurrent.futures.TimeoutError:
            logger.error("Tool '%s' exceeded mandatory execution timeout of %.1fs.", fn_name, tool_timeout)
            return (
                f"Error: Tool '{fn_name}' exceeded maximum execution timeout of {tool_timeout:.1f}s "
                "and was terminated to protect real-time system responsiveness."
            )
        except Exception as err:
            logger.error("Error executing tool '%s': %s", fn_name, err)
            return f"Execution error: {err}"

    async def execute_tool_async(
        self,
        fn_name: str,
        fn_args: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> str:
        """Asynchronous coroutine executing a tool off-loop via asyncio.wait_for and the worker pool."""
        tool_timeout = timeout if timeout is not None else self.tool_timeout
        executor = self._get_executor_for_tool(fn_name)
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(executor, lambda: SkillRegistry.execute(fn_name, **fn_args))
            result = await asyncio.wait_for(future, timeout=tool_timeout)
            return str(result)
        except asyncio.TimeoutError:
            logger.error("Tool '%s' exceeded mandatory async execution timeout of %.1fs.", fn_name, tool_timeout)
            return (
                f"Error: Tool '{fn_name}' exceeded maximum execution timeout of {tool_timeout:.1f}s "
                "and was terminated to protect real-time system responsiveness."
            )
        except Exception as err:
            logger.error("Error executing tool '%s' asynchronously: %s", fn_name, err)
            return f"Execution error: {err}"



    @staticmethod
    def narrate_tool_action(fn_name: str, fn_args: Dict[str, Any]) -> str:
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
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_instruction: Optional[str] = None,
        sentence_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Runs the multi-turn interaction loop until completion or loop halt.
        
        Returns:
            Tuple of (final_reply_text, updated_messages_list)
        """
        active_messages = list(messages)
        executed_signatures: Set[Tuple[str, str]] = set()
        iterations = 0
        last_text_reply = ""
        had_tool_call = False

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
                break

            had_tool_call = True

            # Loop Cycle Detector: Check for repeated tool calls with identical arguments
            has_cycle = False
            for tc in turn_res.tool_calls:
                args_key = json.dumps(tc.arguments, sort_keys=True)
                sig = (tc.name, args_key)
                if sig in executed_signatures:
                    logger.warning("Loop cycle detected on tool '%s' with identical arguments. Halting recursion.", tc.name)
                    has_cycle = True
                    break
                executed_signatures.add(sig)

            if has_cycle:
                if sentence_callback:
                    sentence_callback("Halting repetitive action cycle, Sir.")
                break

            # Append assistant message with tool calls to message history
            assistant_entry: Dict[str, Any] = {
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
                    if sentence_callback:
                        sentence_callback(f"Action was halted: {warning}")
                else:
                    tool_result = self.execute_tool_with_timeout(fn_name, fn_args, timeout=self.tool_timeout)

                # Append tool result to history
                active_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(tool_result),
                })

        final_reply = last_text_reply
        if not final_reply and had_tool_call:
            final_reply = "Action completed, Sir."

        return final_reply or "Standing by, Sir.", active_messages

