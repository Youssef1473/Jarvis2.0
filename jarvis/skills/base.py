"""Base tool decorator and skill registry for Jarvis 2.0."""
from __future__ import annotations

from dataclasses import dataclass, field
import enum
import inspect
from typing import Callable, Dict, Any, Iterable, List, Optional, Tuple

from jarvis.context.flow_state_mirror import ResourceScope


class SpeculationForbiddenError(PermissionError):
    """Raised when an agent attempts to execute a non-read-only skill speculatively."""
    pass


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
    path_arg_names: Tuple[str, ...] = ()
    default_timeout_ms: int = 1500
    origin: str = "builtin"


import contextvars

# ContextVar tracking active single-use ToolOrchestrator execution capability
# When active, holds dict: {"tool_name": str, "consumed": bool}
_active_orchestrator_scope: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "_active_orchestrator_scope", default=None
)


@dataclass
class SkillExecutionResult:
    """Structured execution result from SkillRegistry."""
    result: Any
    success: bool
    error_type: Optional[str] = None  # "not_found", "exception"
    error_message: Optional[str] = None


class SkillRegistry:
    """Central registry holding all callable tools for Jarvis."""

    _tools: Dict[str, Callable] = {}
    _descriptions: Dict[str, str] = {}
    _risk_levels: Dict[str, int] = {}
    _metadata: Dict[str, SkillMetadata] = {}
    _openai_tools_cache: Dict[tuple[str, ...], List[Dict[str, Any]]] = {}

    @classmethod
    def register(
        cls,
        name: str | Callable | None = None,
        description: str | None = None,
        risk_level: Optional[int] = None,
        read_only: bool = False,
        external_ingestion: bool = False,
        scopes: ResourceScope = ResourceScope.NONE,
        path_arg_names: Tuple[str, ...] = (),
        default_timeout_ms: int = 1500,
        origin: str = "builtin",
    ):
        """Decorator to register a Python function as a Jarvis skill tool.
        
        Supports:
        - Bare: @SkillRegistry.register
        - Call without args: @SkillRegistry.register()
        - Parameterized: @SkillRegistry.register(name="foo", read_only=True, external_ingestion=True)
        """
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
            return bool(read_only) or (t_name in KNOWN_READ_ONLY_TOOLS)

        def _resolve_risk(t_name: str, is_ro: bool) -> int:
            if is_ro:
                return 0
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

        # Case 1: Bare decorator @SkillRegistry.register
        if callable(name):
            func = name
            tool_name = func.__name__
            _check_collision(tool_name, origin)
            effective_ro = _resolve_read_only(tool_name)
            resolved_risk = _resolve_risk(tool_name, effective_ro)
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
            _check_collision(tool_name, origin)
            effective_ro = _resolve_read_only(tool_name)
            resolved_risk = _resolve_risk(tool_name, effective_ro)
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
        meta = cls.get_metadata(name)
        return bool(meta and getattr(meta, "external_ingestion", False))

    @classmethod
    def get_metadata(cls, name: str) -> Optional[SkillMetadata]:
        """Returns the SkillMetadata for a registered tool, or a synthesized fallback."""
        meta = cls._metadata.get(name)
        if meta is not None:
            return meta
        func = cls.get_tool(name)
        if func is not None:
            return SkillMetadata(name=name, func=func, read_only=False, external_ingestion=False)
        return None

    @classmethod
    def get_risk_level(cls, name: str) -> int:
        return cls._risk_levels.get(name, 0)

    @classmethod
    def get_tool(cls, name: str) -> Callable | None:
        return cls._tools.get(name)

    @classmethod
    def get_all_tools(cls) -> Dict[str, Callable]:
        return cls._tools

    @classmethod
    def unregister(cls, name: str) -> bool:
        """Unregisters a tool from the registry."""
        if name in cls._tools:
            del cls._tools[name]
            cls._descriptions.pop(name, None)
            cls._risk_levels.pop(name, None)
            cls._metadata.pop(name, None)
            cls._openai_tools_cache.clear()
            return True
        return False

    @classmethod
    def get_openai_tools(cls, names: Iterable[str] | None = None) -> List[Dict[str, Any]]:
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
        aliases = {
            "store_fact": "remember_fact",
            "save_fact": "remember_fact",
            "get_fact": "recall_fact",
            "find_fact": "recall_fact",
            "remember": "remember_fact",
            "recall": "recall_fact",
        }
        resolved = aliases.get(name, name)
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
        if isinstance(cap, dict) and not cap.get("consumed", False) and cap.get("tool_name") == resolved:
            # Consume capability so nested or concurrent child calls cannot inherit it
            cap["consumed"] = True
            is_authorized_orchestrator_call = True

        if not is_authorized_orchestrator_call:
            try:
                from jarvis.core.security_boundaries import TurnTaintTracker, SecurityBoundaryViolation
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
        except Exception as e:
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
