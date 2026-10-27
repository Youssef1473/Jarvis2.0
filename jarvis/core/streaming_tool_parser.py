"""Streaming Tool Call & Nonce-Guarded Parser for Jarvis 2.0.

Provides single-pass deterministic O(1) parsing of tool call delimiters from streaming LLM output,
isolating spoken voice text for Kokoro neural TTS without regex backtracking, while buffering and
executing tool call JSON blocks cleanly under cryptographic nonce protection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import inspect
import json
import logging
import secrets
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Jarvis.StreamingToolParser")

# State Machine Constants
STATE_SPEAKING = 0
STATE_BUFFERING_POSSIBLE_TAG = 1
STATE_ACCUMULATING_TOOL_CALL = 2

# Canonical set of Level-0 read-only tools safe for speculative dispatch
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


@dataclass
class ParsedToolCall:
    """Represents a validated tool call extracted from a streaming response."""
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    raw_payload: str = ""


class StreamingToolCallParser:
    """Intercepts streaming tool-call JSON fragments and dispatches early read tools."""

    def __init__(
        self,
        idempotent_read_tools: Optional[Set[str]] = None,
        dispatch_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        coordinator: Optional[Any] = None,
    ) -> None:
        self.idempotent_read_tools = idempotent_read_tools or set(DEFAULT_IDEMPOTENT_READ_TOOLS)
        self.dispatch_callback = dispatch_callback
        self.coordinator = coordinator
        self._buffers: dict[str, str] = {}
        self._dispatched_call_ids: Set[str] = set()

    @property
    def _buffer(self) -> str:
        """Backward-compatibility property returning default buffer."""
        return self._buffers.get("default_0", "")

    @_buffer.setter
    def _buffer(self, value: str) -> None:
        if not value:
            self._buffers.pop("default_0", None)
        else:
            self._buffers["default_0"] = value

    @staticmethod
    def _log_unhandled_task_exception(task: asyncio.Task) -> None:
        """Done callback that retrieves and logs unhandled exceptions from fire-and-forgotten speculative tasks."""
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Speculative early tool execution failed: %s", exc)

    def feed_chunk(self, delta_json: str, call_id: str = "default_0") -> Optional[Any]:
        """Feeds partial JSON tokens and triggers early execution when valid tool JSON closes.

        Returns:
            - None if no tool JSON object has closed or if the call has already been dispatched.
            - The cached tool result (dict/Any) if a cache hit occurred in DualTrackCoordinator.
            - An asyncio.Task if DualTrackCoordinator scheduled acoustic masking on a running event loop.
            - The synchronous dispatch_callback result (or parsed payload) if no event loop/coordinator is active.
        """
        if call_id in self._dispatched_call_ids:
            return None

        self._buffers[call_id] = self._buffers.get(call_id, "") + delta_json
        stripped = self._buffers[call_id].strip()
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
            self._buffers.pop(call_id, None)
            logger.info("Speculative early tool dispatch for '%s' with args %s", tool_name, list(tool_args.keys()))

            # Acoustic masking via DualTrackCoordinator if configured
            if self.coordinator is not None:
                try:
                    args_repr = json.dumps(tool_args or {}, sort_keys=True, default=str)
                except Exception:
                    args_repr = str(sorted((tool_args or {}).items()))
                cache_key = f"{tool_name}:{args_repr}"
                cached = self.coordinator._get_cached_result(cache_key)
                if cached is not None:
                    logger.debug("Fast-path cache hit for '%s' in StreamingToolCallParser", tool_name)
                    return cached

                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        coro = self.coordinator.execute_with_masking(
                            user_query="",
                            tool_name=tool_name,
                            tool_args=tool_args,
                            tool_coro=(lambda: self.dispatch_callback(tool_name, tool_args)) if self.dispatch_callback else (lambda: parsed),
                            is_read_only=True,
                        )
                        task = loop.create_task(coro)
                        # Attach done callback to prevent unhandled exception warnings if task is fire-and-forgotten
                        task.add_done_callback(self._log_unhandled_task_exception)
                        return task
                except RuntimeError:
                    pass

            if self.dispatch_callback:
                return self.dispatch_callback(tool_name, tool_args)
            return parsed

        return None

    async def feed_chunk_async(
        self, delta_json: str, call_id: str = "default_0", user_query: str = ""
    ) -> Optional[Any]:
        """Asynchronously feeds partial JSON tokens and executes early read tools with acoustic masking."""
        if call_id in self._dispatched_call_ids:
            return None

        self._buffers[call_id] = self._buffers.get(call_id, "") + delta_json
        stripped = self._buffers[call_id].strip()
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
            self._buffers.pop(call_id, None)
            logger.info("Speculative early tool dispatch (async) for '%s' with args %s", tool_name, list(tool_args.keys()))

            if self.coordinator is not None:
                tool_target = (lambda: self.dispatch_callback(tool_name, tool_args)) if self.dispatch_callback else (lambda: parsed)
                return await self.coordinator.execute_with_masking(
                    user_query=user_query,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_coro=tool_target,
                    is_read_only=True,
                )

            if self.dispatch_callback:
                res = self.dispatch_callback(tool_name, tool_args)
                if inspect.isawaitable(res):
                    return await res
                return res
            return parsed

        return None

    def reset(self) -> None:
        """Resets the internal streaming buffer and dispatched call tracking."""
        self._buffers.clear()
        self._dispatched_call_ids.clear()


class NonceGuardedStreamParser:
    """Single-pass streaming parser with cryptographic nonce delimiter fencing.

    Fences: <<JARVIS_CALL_{turn_nonce}>> {JSON} <<END_CALL_{turn_nonce}>>
    - Yields clean text deltas immediately for neural TTS without regex backtracking.
    - Suppresses tool payload JSON from voice audio.
    - Emits parsed ParsedToolCall instances upon closing delimiters.
    - Automatically rejects and flushes invalid/injected nonce tags as inert text.
    """

    def __init__(self, turn_nonce: Optional[str] = None) -> None:
        self.turn_nonce: str = turn_nonce or secrets.token_hex(4)
        self.start_tag: str = f"<<JARVIS_CALL_{self.turn_nonce}>>"
        self.end_tag: str = f"<<END_CALL_{self.turn_nonce}>>"

        self._state: int = STATE_SPEAKING
        self._speech_buffer: str = ""
        self._tool_buffer: str = ""
        self._completed_tools: List[ParsedToolCall] = []

    def reset(self, new_nonce: Optional[str] = None) -> None:
        """Resets the parser state machine for a new turn."""
        if new_nonce:
            self.turn_nonce = new_nonce
            self.start_tag = f"<<JARVIS_CALL_{self.turn_nonce}>>"
            self.end_tag = f"<<END_CALL_{self.turn_nonce}>>"
        self._state = STATE_SPEAKING
        self._speech_buffer = ""
        self._tool_buffer = ""
        self._completed_tools.clear()

    def _parse_tool_payload(self, raw_json: str) -> Optional[ParsedToolCall]:
        """Safely parses raw tool JSON into a ParsedToolCall object."""
        if not raw_json.strip():
            return None
        try:
            data = json.loads(raw_json)
            name = (
                data.get("name")
                or data.get("tool")
                or (data.get("function", {}).get("name") if isinstance(data.get("function"), dict) else "")
                or ""
            )
            raw_args = data.get("args") or data.get("parameters") or data.get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            if name:
                return ParsedToolCall(name=name, args=args, raw_payload=raw_json)
        except Exception as e:
            logger.warning("Failed to parse nonce-guarded tool payload: %s (err: %s)", raw_json[:80], e)
        return None

    def feed(self, delta: str) -> Tuple[str, List[ParsedToolCall]]:
        """Feeds an incoming LLM token/delta through the state machine.

        Returns:
            Tuple[speech_delta, ready_tool_calls]
        """
        if not delta:
            return "", []

        speech_out: List[str] = []
        ready_tools: List[ParsedToolCall] = []

        if self._state == STATE_ACCUMULATING_TOOL_CALL:
            self._tool_buffer += delta
            if self.end_tag in self._tool_buffer:
                end_idx = self._tool_buffer.find(self.end_tag)
                raw_payload = self._tool_buffer[:end_idx].strip()
                remainder = self._tool_buffer[end_idx + len(self.end_tag):]

                tool = self._parse_tool_payload(raw_payload)
                if tool:
                    ready_tools.append(tool)
                    self._completed_tools.append(tool)

                self._tool_buffer = ""
                self._state = STATE_SPEAKING

                if remainder:
                    sub_speech, sub_tools = self.feed(remainder)
                    if sub_speech:
                        speech_out.append(sub_speech)
                    ready_tools.extend(sub_tools)
            return "".join(speech_out), ready_tools

        # STATE_SPEAKING
        self._speech_buffer += delta

        while True:
            # Check if complete start tag is present
            if self.start_tag in self._speech_buffer:
                start_idx = self._speech_buffer.find(self.start_tag)
                clean_prefix = self._speech_buffer[:start_idx]
                if clean_prefix:
                    speech_out.append(clean_prefix)

                remainder = self._speech_buffer[start_idx + len(self.start_tag):]
                self._speech_buffer = ""
                self._state = STATE_ACCUMULATING_TOOL_CALL

                if remainder:
                    sub_speech, sub_tools = self.feed(remainder)
                    if sub_speech:
                        speech_out.append(sub_speech)
                    ready_tools.extend(sub_tools)
                break

            # Fast deterministic check for partial tag prefix
            candidate_idx = self._speech_buffer.find("<<")
            if candidate_idx != -1:
                candidate = self._speech_buffer[candidate_idx:]
                if self.start_tag.startswith(candidate):
                    # Valid partial prefix: emit everything before candidate
                    prefix = self._speech_buffer[:candidate_idx]
                    if prefix:
                        speech_out.append(prefix)
                    self._speech_buffer = candidate
                    break
                else:
                    # Look for subsequent '<<'
                    next_idx = candidate[1:].find("<<")
                    if next_idx != -1:
                        advance = 1 + next_idx
                        speech_out.append(self._speech_buffer[:candidate_idx + advance])
                        self._speech_buffer = self._speech_buffer[candidate_idx + advance:]
                        continue
                    else:
                        speech_out.append(self._speech_buffer)
                        self._speech_buffer = ""
                        break
            else:
                speech_out.append(self._speech_buffer)
                self._speech_buffer = ""
                break

        return "".join(speech_out), ready_tools

    def flush(self) -> Tuple[str, List[ParsedToolCall]]:
        """Flushes any remaining text at EOF."""
        speech = self._speech_buffer
        self._speech_buffer = ""
        tools: List[ParsedToolCall] = []

        if self._state == STATE_ACCUMULATING_TOOL_CALL and self._tool_buffer.strip():
            tool = self._parse_tool_payload(self._tool_buffer.strip())
            if tool:
                tools.append(tool)
                self._completed_tools.append(tool)
            self._tool_buffer = ""

        self._state = STATE_SPEAKING
        return speech, tools


__all__ = [
    "DEFAULT_IDEMPOTENT_READ_TOOLS",
    "ParsedToolCall",
    "StreamingToolCallParser",
    "NonceGuardedStreamParser",
    "STATE_SPEAKING",
    "STATE_BUFFERING_POSSIBLE_TAG",
    "STATE_ACCUMULATING_TOOL_CALL",
]
