"""Base tool decorator and skill registry for Jarvis 2.0."""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import inspect
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar

from jarvis.context.flow_state_mirror import ResourceScope


class SpeculationForbiddenError(PermissionError):
    """Raised when an agent attempts to execute a non-read-only skill speculatively."""


# Verified Level-0 read-only query and inspection tools safe unconditionally
KNOWN_READ_ONLY_TOOLS: set[str] = {
    "get_battery_status",
    "get_current_time_and_date",
    "get_system_vitals",
    "get_weather",
    "web_search",
    "get_wikipedia_summary",
    "get_tech_news_headlines",
    "get_daily_briefing",
    "get_voice_settings",
    "get_user_preferences",
    "get_pipeline_latency_stats",
    "get_background_task_output",
    "get_recent_workspace_mutations",
    "list_active_timers",
    "list_calendar_events",
    "list_open_browser_tabs",
    "list_pending_tasks",
    "list_running_applications",
    "list_background_tasks",
    "list_staged_documents",
    "list_photos",
    "list_compiled_reflex_macros",
    "list_custom_skills",
    "list_audio_devices",
    "list_directory",
    "read_clipboard",
    "read_file",
    "read_multiple_files",
    "read_webpage_content",
    "read_webpage_content_cdp",
    "browse_and_read_webpage",
    "read_recent_notes",
    "read_email_details",
    "read_archived_document",
    "recall_fact",
    "recall_episodic_lessons",
    "recall_past_conversation",
    "semantic_search_memory",
    "search_codebase",
    "search_desktop_files_fast",
    "search_everything",
    "search_notes",
    "search_emails",
    "search_archived_documents",
    "git_status",
    "git_diff",
    "query_codebase_graph",
    "ast_search_codebase",
    "diagnose_test_failure_ast",
    "profile_runtime",
    "audit_memory_heap",
    "audit_vram_and_thermal_budget",
    "audit_webpage_accessibility",
    "audit_webpage_performance",
    "capture_devtools_snapshot",
    "capture_webpage_screenshot",
    "describe_photo",
    "inspect_file_locks",
    "inspect_port_holders",
    "inspect_screen",
    "see_screen",
    "inspect_screen_with_marks",
    "inspect_ram_hogs",
    "inspect_window_controls",
    "check_unread_emails",
    "check_hardware_health",
    "check_code_diagnostics",
    "search_gemini_api_documentation",
    "get_gemini_documentation_chunk",
    "get_gemini_live_architecture_overview",
    "search_modern_web_guidance",
    "retrieve_modern_web_guide",
    "jarvis_doctor",
    "run_system_troubleshooting",
    "verify_learned_habits",
}


@dataclass
class SkillMetadata:
    """Metadata describing execution constraints and resource scopes of a registered skill."""
    name: str
    func: Callable
    read_only: bool = False
    risk_level: int = 1
    external_ingestion: bool = False
    scopes: ResourceScope = ResourceScope.NONE
    path_arg_names: tuple[str, ...] = ()
    default_timeout_ms: int = 1500
    origin: str = "builtin"


class ExecutionCapability:
    """Single-use, action-bound capability token granting execution for an authorized tool call.

    In-Process Threat Model & Security Notice:
    In CPython, in-process plugins and tools share the same memory space, thread pool,
    and ContextVar scopes. ExecutionCapability provides strict defense-in-depth against
    accidental re-entrancy, cross-turn propagation, and argument tampering within the
    orchestrator pipeline. It does NOT provide kernel-enforced process isolation against
    adversarial native code or direct CPython object inspection running in-process. Untrusted
    skills must execute in isolated external worker processes (e.g., Win32 Job Object sandbox).

    Action hashing uses canonical JSON serialization with SHA-256 and constant-time digest
    comparison (hmac.compare_digest) for TOCTOU prevention. It does NOT use a hardcoded
    secret key or claim HMAC-keyed secrecy against in-process memory inspection.
    """

    def __init__(
        self,
        tool_name: str,
        action_hash: str,
        turn_id: str | None,
        expires_at: float,
    ) -> None:
        object.__setattr__(self, "_tool_name", str(tool_name))
        object.__setattr__(self, "_action_hash", str(action_hash))
        object.__setattr__(self, "_turn_id", str(turn_id) if turn_id is not None else None)
        object.__setattr__(self, "_expires_at", float(expires_at))
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_lock", threading.Lock())

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def action_hash(self) -> str:
        return self._action_hash

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def expires_at(self) -> float:
        return self._expires_at

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"ExecutionCapability attribute '{name}' is immutable and cannot be reassigned.")

    def __repr__(self) -> str:
        return (
            f"ExecutionCapability(tool_name={self._tool_name!r}, "
            f"action_hash={self._action_hash[:8]}..., "
            f"turn_id={self._turn_id!r}, "
            f"expires_at={self._expires_at}, "
            f"consumed={self.is_consumed})"
        )

    @classmethod
    def compute_action_hash(cls, tool_name: str, arguments: dict[str, Any]) -> str:
        """Computes deterministic canonical SHA-256 action hash across tool name and arguments.

        Requires canonical JSON serialization. Non-serializable inputs fail closed with ValueError.
        """
        clean_args = dict(arguments or {})
        try:
            canonical_json = json.dumps(clean_args, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as err:
            raise ValueError(f"Arguments for capability hashing must be canonical JSON serializable: {err}") from err
        data = f"{tool_name}:{canonical_json}".encode()
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        turn_id: str | None = None,
        ttl_seconds: float = 10.0,
    ) -> ExecutionCapability:
        h = cls.compute_action_hash(tool_name, arguments)
        return cls(
            tool_name=tool_name,
            action_hash=h,
            turn_id=turn_id,
            expires_at=time.monotonic() + ttl_seconds,
        )

    def consume(self, target_tool: str, target_args: dict[str, Any], current_turn_id: str | None = None) -> bool:
        """Atomically validates and consumes the capability. Single-use and TOCTOU protected."""
        with self._lock:
            if self._consumed:
                return False

            if time.monotonic() > self._expires_at:
                return False

            if target_tool != self._tool_name:
                return False

            if self._turn_id is not None and (not current_turn_id or current_turn_id != self._turn_id):
                return False

            try:
                expected_hash = self.compute_action_hash(target_tool, target_args)
            except ValueError:
                return False

            if not hmac.compare_digest(self._action_hash, expected_hash):
                return False

            object.__setattr__(self, "_consumed", True)
            return True


# ContextVar tracking active single-use ToolOrchestrator execution capability
_active_orchestrator_scope: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "_active_orchestrator_scope", default=None
)


@dataclass
class SkillExecutionResult:
    """Structured execution result from SkillRegistry."""
    result: Any
    success: bool
    error_type: str | None = None  # "not_found", "exception"
    error_message: str | None = None


class SkillRegistry:
    """Central registry holding all callable tools for Jarvis."""

    _tools: ClassVar[dict[str, Callable]] = {}
    _descriptions: ClassVar[dict[str, str]] = {}
    _risk_levels: ClassVar[dict[str, int]] = {}
    _metadata: ClassVar[dict[str, SkillMetadata]] = {}
    _openai_tools_cache: ClassVar[dict[tuple[str, ...], list[dict[str, Any]]]] = {}
    _test_certified_read_only_tools: ClassVar[set[str]] = set()
    _aliases: ClassVar[dict[str, str]] = {
        "store_fact": "remember_fact",
        "save_fact": "remember_fact",
        "get_fact": "recall_fact",
        "find_fact": "recall_fact",
        "remember": "remember_fact",
        "recall": "recall_fact",
    }

    @classmethod
    def resolve_canonical_name(cls, name: str) -> str:
        """Resolves tool aliases to their canonical registered name."""
        return cls._aliases.get(name, name)

    @classmethod
    def certify_test_read_only_tool(cls, name: str) -> None:
        """Explicitly certifies a test tool as read-only within a test harness."""
        cls._test_certified_read_only_tools.add(name)

    @classmethod
    def decertify_test_read_only_tool(cls, name: str) -> None:
        """Removes test certification for a tool."""
        cls._test_certified_read_only_tools.discard(name)

    @classmethod
    @contextlib.contextmanager
    def scoped_test_read_only_certification(cls, *names: str):
        """Context manager temporarily certifying tools as read-only for testing."""
        for n in names:
            cls._test_certified_read_only_tools.add(n)
        try:
            yield
        finally:
            for n in names:
                cls._test_certified_read_only_tools.discard(n)

    @classmethod
    def register(
        cls,
        name: str | Callable | None = None,
        description: str | None = None,
        risk_level: int | None = None,
        read_only: bool = False,
        external_ingestion: bool = False,
        scopes: ResourceScope = ResourceScope.NONE,
        path_arg_names: tuple[str, ...] = (),
        default_timeout_ms: int = 1500,
        origin: str = "builtin",
    ):
        """Decorator to register a Python function as a Jarvis skill tool.
        
        Supports:
        - Bare: @SkillRegistry.register
        - Call without args: @SkillRegistry.register()
        - Parameterized: @SkillRegistry.register(name="foo", read_only=True, external_ingestion=True)
        """
        # 1. Enforce exact permitted origin policy
        valid_origins = {"builtin", "custom", "mcp"}
        if origin not in valid_origins:
            raise ValueError(f"Invalid skill origin '{origin}'. Must be one of {valid_origins}.")

        # 2. Enforce exact permitted risk-level policy bounds if declared
        if risk_level is not None and (
            isinstance(risk_level, bool) or not isinstance(risk_level, int) or risk_level < 0 or risk_level > 2
        ):
            raise ValueError(
                f"Invalid risk_level '{risk_level}'. Risk level must be an integer in the range [0, 2]."
            )

        def _verify_origin_trust(t_name: str, fn: Callable):
            if origin == "builtin":
                fn_mod = getattr(fn, "__module__", "") or ""
                co = getattr(fn, "__code__", None)
                fn_file = getattr(co, "co_filename", "").replace("\\", "/") if co else ""
                if (
                    fn_mod.startswith("jarvis.skills.custom.")
                    or "jarvis/skills/custom" in fn_file
                    or "data/skills" in fn_file
                    or (fn_file.startswith("<") and "test" not in fn_mod)
                ):
                    raise PermissionError(
                        f"Security Violation: Custom or dynamically executed skill '{t_name}' cannot claim origin='builtin'."
                    )

        def _check_collision(t_name: str, incoming_origin: str):
            existing_meta = cls._metadata.get(t_name)
            if existing_meta:
                if getattr(existing_meta, "origin", "builtin") == "builtin" and incoming_origin == "custom":
                    raise PermissionError(
                        f"Security Violation: Cannot overwrite built-in tool '{t_name}' with custom skill."
                    )
                elif getattr(existing_meta, "origin", "builtin") == "custom" and incoming_origin == "builtin":
                    import logging
                    logging.getLogger("Jarvis.SkillRegistry").warning(
                        "Built-in tool '%s' is overriding previously registered custom skill.",
                        t_name,
                    )

        def _resolve_read_only(t_name: str) -> bool:
            # 1. Custom skills and untrusted extensions are strictly prohibited from being read-only
            if origin == "custom":
                return False
            # 2. Elevated risk takes precedence: if risk_level > 0 was declared, it cannot be read-only
            if risk_level is not None and risk_level > 0:
                return False
            # 3. Must be in canonical KNOWN_READ_ONLY_TOOLS or test-certified set
            return t_name in KNOWN_READ_ONLY_TOOLS or t_name in cls._test_certified_read_only_tools

        def _resolve_risk(t_name: str, is_ro: bool) -> int:
            if is_ro:
                return 0
            if origin != "builtin":
                return max(1, risk_level if risk_level is not None else 1)
            if risk_level is not None:
                if risk_level == 0:
                    import logging
                    logging.getLogger("Jarvis.SkillRegistry").warning(
                        "Inconsistent tool registration: tool '%s' declared risk_level=0 but is not certified read_only. Failing closed to risk_level=1.",
                        t_name,
                    )
                    return 1
                return risk_level
            return 1

        def _log_ro_rejection_if_needed(t_name: str, is_ro: bool):
            if read_only and not is_ro:
                import logging
                logging.getLogger("Jarvis.SkillRegistry").warning(
                    "Tool '%s' requested read_only=True but is not certified (origin='%s', in_known=%s, risk_level=%s). Failing closed to read_only=False.",
                    t_name,
                    origin,
                    (t_name in KNOWN_READ_ONLY_TOOLS or t_name in cls._test_certified_read_only_tools),
                    risk_level,
                )

        # Case 1: Bare decorator @SkillRegistry.register
        if callable(name):
            func = name
            tool_name = func.__name__
            _verify_origin_trust(tool_name, func)
            _check_collision(tool_name, origin)
            effective_ro = _resolve_read_only(tool_name)
            resolved_risk = _resolve_risk(tool_name, effective_ro)
            _log_ro_rejection_if_needed(tool_name, effective_ro)
            doc = (func.__doc__ or "").strip()
            cls._tools[tool_name] = func
            cls._descriptions[tool_name] = doc
            cls._risk_levels[tool_name] = resolved_risk
            cls._metadata[tool_name] = SkillMetadata(
                name=tool_name,
                func=func,
                read_only=effective_ro,
                risk_level=resolved_risk,
                external_ingestion=external_ingestion,
                scopes=scopes,
                path_arg_names=tuple(path_arg_names),
                default_timeout_ms=default_timeout_ms,
                origin=origin,
            )
            cls._openai_tools_cache.clear()
            return func

        # Case 2: Parameterized decorator @SkillRegistry.register(...)
        def decorator(func: Callable):
            tool_name = name or func.__name__
            _verify_origin_trust(tool_name, func)
            _check_collision(tool_name, origin)
            effective_ro = _resolve_read_only(tool_name)
            resolved_risk = _resolve_risk(tool_name, effective_ro)
            _log_ro_rejection_if_needed(tool_name, effective_ro)
            doc = description or (func.__doc__ or "").strip()
            cls._tools[tool_name] = func
            cls._descriptions[tool_name] = doc
            cls._risk_levels[tool_name] = resolved_risk
            cls._metadata[tool_name] = SkillMetadata(
                name=tool_name,
                func=func,
                read_only=effective_ro,
                risk_level=resolved_risk,
                external_ingestion=external_ingestion,
                scopes=scopes,
                path_arg_names=tuple(path_arg_names),
                default_timeout_ms=default_timeout_ms,
                origin=origin,
            )
            cls._openai_tools_cache.clear()
            return func

        return decorator

    @classmethod
    def is_ingestion_tool(cls, name: str) -> bool:
        """Returns True if the tool is explicitly classified as an external ingestion source."""
        canonical = cls.resolve_canonical_name(name)
        meta = cls.get_metadata(canonical)
        return bool(meta and getattr(meta, "external_ingestion", False))

    @classmethod
    def get_metadata(cls, name: str) -> SkillMetadata | None:
        """Returns the SkillMetadata for a registered tool, or a synthesized fallback."""
        canonical = cls.resolve_canonical_name(name)
        meta = cls._metadata.get(canonical)
        if meta is not None:
            return meta
        func = cls.get_tool(canonical)
        if func is not None:
            return SkillMetadata(name=canonical, func=func, read_only=False, external_ingestion=False)
        return None

    @classmethod
    def get_risk_level(cls, name: str) -> int:
        return cls._risk_levels.get(cls.resolve_canonical_name(name), 1)

    @classmethod
    def get_tool(cls, name: str) -> Callable | None:
        return cls._tools.get(cls.resolve_canonical_name(name))

    @classmethod
    def get_all_tools(cls) -> MappingProxyType[str, Callable]:
        from types import MappingProxyType
        return MappingProxyType(cls._tools)

    @classmethod
    def unregister(cls, name: str) -> bool:
        """Unregisters a tool from the registry."""
        resolved = cls.resolve_canonical_name(name)
        if resolved in cls._tools:
            del cls._tools[resolved]
            cls._descriptions.pop(resolved, None)
            cls._risk_levels.pop(resolved, None)
            cls._metadata.pop(resolved, None)
            cls._test_certified_read_only_tools.discard(resolved)
            cls._openai_tools_cache.clear()
            return True
        return False

    @classmethod
    def get_openai_tools(cls, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Return cached OpenAI-compatible schemas for every tool or a selected subset."""
        selected = set(names) if names is not None else None
        ordered_names = tuple(name for name in cls._tools if selected is None or name in selected)
        cached = cls._openai_tools_cache.get(ordered_names)
        if cached is not None:
            return cached

        openai_tools = []
        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }

        for name in ordered_names:
            func = cls._tools[name]
            sig = inspect.signature(func)
            props = {}
            required = []

            for p_name, param in sig.parameters.items():
                if (
                    p_name in ("self", "cls")
                    or p_name.startswith("_")
                    or p_name in (
                        "user_approved",
                        "automated",
                        "allow_unmanaged",
                        "context",
                        "approval_token",
                    )
                    or param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
                ):
                    continue
                p_type = "string"
                if param.annotation != inspect.Parameter.empty:
                    p_type = type_map.get(param.annotation, "string")

                props[p_name] = {"type": p_type}
                if param.default == inspect.Parameter.empty:
                    required.append(p_name)

            desc = cls._descriptions.get(name) or (func.__doc__ or name).strip()
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required
                    }
                }
            })
        cls._openai_tools_cache[ordered_names] = openai_tools
        return openai_tools

    @classmethod
    def execute_with_status(cls, name: str, **kwargs) -> SkillExecutionResult:
        """Executes a registered skill and returns a structured SkillExecutionResult."""
        resolved = cls.resolve_canonical_name(name)
        tool = cls.get_tool(resolved)
        if not tool:
            err_msg = f"Error: Tool '{name}' not found."
            return SkillExecutionResult(
                result=err_msg,
                success=False,
                error_type="not_found",
                error_message=f"Tool '{name}' not found.",
            )

        # CRITICAL SECURITY GATE: Direct execution boundary
        # Single-use authorization capability bound to exact tool authorized by orchestrator
        cap = _active_orchestrator_scope.get()
        is_authorized_orchestrator_call = False
        if isinstance(cap, ExecutionCapability):
            current_turn_id = None
            try:
                from jarvis.core.security_boundaries import TurnTaintTracker
                current_turn_id = TurnTaintTracker.get_instance().get_current_turn_id()
            except (ImportError, AttributeError):
                current_turn_id = None
            if cap.consume(resolved, kwargs, current_turn_id=current_turn_id):
                is_authorized_orchestrator_call = True

        if not is_authorized_orchestrator_call:
            try:
                from jarvis.core.security_boundaries import (
                    SecurityBoundaryViolation,
                    TurnTaintTracker,
                )
                tracker = TurnTaintTracker.get_instance()
                effective_risk = cls.get_risk_level(resolved)

                gate_decision = tracker.evaluate_tool_execution(
                    resolved, kwargs, risk_level=effective_risk
                )
                if not gate_decision.allowed:
                    return SkillExecutionResult(
                        result=f"Security Violation: Security gate denied execution of tool '{name}'.",
                        success=False,
                        error_type="security_violation",
                        error_message=f"Security gate denied execution of tool '{name}'.",
                    )
            except SecurityBoundaryViolation as sec_err:
                return SkillExecutionResult(
                    result=f"Security Violation: {sec_err}",
                    success=False,
                    error_type="security_violation",
                    error_message=str(sec_err),
                )

        try:
            output = tool(**kwargs)
            return SkillExecutionResult(
                result=output,
                success=True,
            )
        except Exception as e:  # noqa: BLE001 - catch arbitrary skill execution failures to return structured status
            err_msg = f"Error executing tool '{name}': {e}"
            return SkillExecutionResult(
                result=err_msg,
                success=False,
                error_type="exception",
                error_message=str(e),
            )

    @classmethod
    def execute(cls, name: str, **kwargs) -> Any:
        res = cls.execute_with_status(name, **kwargs)
        if not res.success and res.error_type == "security_violation":
            from jarvis.core.security_boundaries import SecurityBoundaryViolation
            raise SecurityBoundaryViolation(res.error_message or str(res.result))
        return res.result
