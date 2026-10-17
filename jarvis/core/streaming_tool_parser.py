"""Speculative Streaming Tool Call Parser for Jarvis 2.0.

Intercepts streaming tool-call JSON fragments from the LLM token stream and
speculatively dispatches Level-0 idempotent read tools before the turn finishes.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger("Jarvis.StreamingToolParser")

# Default canonical set of Level-0 read-only tools safe for speculative dispatch
DEFAULT_IDEMPOTENT_READ_TOOLS: Set[str] = {
    "get_battery_status",
    "get_current_time_and_date",
    "get_system_vitals",
    "get_weather",
    "list_active_timers",
    "list_calendar_events",
    "list_open_browser_tabs",
    "list_pending_tasks",
    "list_running_applications",
    "read_clipboard",
    "search_codebase",
    "list_directory",
    "read_file",
    "read_multiple_files",
    "recall_fact",
    "recall_episodic_lessons",
    "get_active_window",
    "inspect_screen",
    "see_screen",
    "check_unread_emails",
    "search_emails",
}


class StreamingToolCallParser:
    """Intercepts streaming tool-call JSON fragments and dispatches early read tools."""

    def __init__(
        self,
        idempotent_read_tools: Optional[Set[str]] = None,
        dispatch_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> None:
        self.idempotent_read_tools = idempotent_read_tools or set(DEFAULT_IDEMPOTENT_READ_TOOLS)
        self.dispatch_callback = dispatch_callback
        self._buffer = ""
        self._dispatched_call_ids: Set[str] = set()

    def feed_chunk(self, delta_json: str, call_id: str = "default_0") -> Optional[Any]:
        """Feeds partial JSON tokens and triggers early execution when valid tool JSON closes."""
        if call_id in self._dispatched_call_ids:
            return None

        self._buffer += delta_json
        # Fast structural check before attempting JSON parse
        stripped = self._buffer.strip()
        if not stripped.endswith("}"):
            return None

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        tool_name = parsed.get("name")
        tool_args = parsed.get("arguments", {})
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except json.JSONDecodeError:
                return None

        if tool_name and tool_name in self.idempotent_read_tools and isinstance(tool_args, dict):
            self._dispatched_call_ids.add(call_id)
            self._buffer = ""
            logger.info("Speculative early tool dispatch for '%s' with args %s", tool_name, list(tool_args.keys()))
            if self.dispatch_callback:
                return self.dispatch_callback(tool_name, tool_args)
            return parsed

        return None

    def reset(self) -> None:
        """Resets the internal streaming buffer and dispatched call tracking."""
        self._buffer = ""
        self._dispatched_call_ids.clear()
