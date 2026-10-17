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


@dataclass
class SkillMetadata:
    """Metadata describing execution constraints and resource scopes of a registered skill."""
    name: str
    func: Callable
    read_only: bool = False
    scopes: ResourceScope = ResourceScope.NONE
    path_arg_names: Tuple[str, ...] = ()
    default_timeout_ms: int = 1500


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
        risk_level: int = 0,
        read_only: bool = False,
        scopes: ResourceScope = ResourceScope.NONE,
        path_arg_names: Tuple[str, ...] = (),
        default_timeout_ms: int = 1500,
    ):
        """Decorator to register a Python function as a Jarvis skill tool.
        
        Supports:
        - Bare: @SkillRegistry.register
        - Call without args: @SkillRegistry.register()
        - Parameterized: @SkillRegistry.register(name="foo", read_only=True, scopes=ResourceScope.FILESYSTEM)
        """
        # Case 1: Bare decorator @SkillRegistry.register
        if callable(name):
            func = name
            tool_name = func.__name__
            doc = (func.__doc__ or "").strip()
            cls._tools[tool_name] = func
            cls._descriptions[tool_name] = doc
            cls._risk_levels[tool_name] = risk_level
            cls._metadata[tool_name] = SkillMetadata(
                name=tool_name,
                func=func,
                read_only=read_only,
                scopes=scopes,
                path_arg_names=tuple(path_arg_names),
                default_timeout_ms=default_timeout_ms,
            )
            cls._openai_tools_cache.clear()
            return func

        # Case 2: Parameterized decorator @SkillRegistry.register(...)
        def decorator(func: Callable):
            tool_name = name or func.__name__
            doc = description or (func.__doc__ or "").strip()
            cls._tools[tool_name] = func
            cls._descriptions[tool_name] = doc
            cls._risk_levels[tool_name] = risk_level
            cls._metadata[tool_name] = SkillMetadata(
                name=tool_name,
                func=func,
                read_only=read_only,
                scopes=scopes,
                path_arg_names=tuple(path_arg_names),
                default_timeout_ms=default_timeout_ms,
            )
            cls._openai_tools_cache.clear()
            return func

        return decorator

    @classmethod
    def get_metadata(cls, name: str) -> Optional[SkillMetadata]:
        """Returns the SkillMetadata for a registered tool, or a synthesized fallback."""
        meta = cls._metadata.get(name)
        if meta is not None:
            return meta
        func = cls.get_tool(name)
        if func is not None:
            return SkillMetadata(name=name, func=func, read_only=False)
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
                if p_name in ("self", "cls"):
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
    def execute(cls, name: str, **kwargs) -> Any:
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
            return f"Error: Tool '{name}' not found."
        try:
            return tool(**kwargs)
        except Exception as e:
            return f"Error executing tool '{name}': {e}"
