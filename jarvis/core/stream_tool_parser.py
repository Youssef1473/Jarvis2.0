"""Nonce-Fenced Streaming Tool Parser with Injection Guard for Jarvis 2.0.

Provides single-pass parsing of tool call delimiters from streaming LLM output,
isolating spoken voice text for Kokoro TTS while protecting against delimiter
smuggling and prompt injection via cryptographically random turn nonces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.StreamToolParser")

STATE_SPEAKING = 0
STATE_BUFFERING_POSSIBLE_TAG = 1
STATE_ACCUMULATING_TOOL_CALL = 2


@dataclass
class ParsedToolCall:
    """Represents a validated tool call extracted from a nonce-fenced stream."""
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    raw_payload: str = ""


class NonceGuardedStreamParser:
    """Single-pass streaming parser with cryptographic nonce delimiter fencing.

    Fences: <<JARVIS_CALL_{turn_nonce}>> {JSON} <<END_CALL_{turn_nonce}>>
    - Yields clean text deltas for neural TTS.
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
                remainder = self._tool_buffer[end_idx + len(self.end_tag) :]

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
            # Check if start tag is present
            if self.start_tag in self._speech_buffer:
                start_idx = self._speech_buffer.find(self.start_tag)
                clean_prefix = self._speech_buffer[:start_idx]
                if clean_prefix:
                    speech_out.append(clean_prefix)

                remainder = self._speech_buffer[start_idx + len(self.start_tag) :]
                self._speech_buffer = ""
                self._state = STATE_ACCUMULATING_TOOL_CALL

                if remainder:
                    sub_speech, sub_tools = self.feed(remainder)
                    if sub_speech:
                        speech_out.append(sub_speech)
                    ready_tools.extend(sub_tools)
                break

            # Check if speech buffer ends with a partial prefix of start_tag
            candidate_idx = self._speech_buffer.find("<<")
            if candidate_idx != -1:
                candidate = self._speech_buffer[candidate_idx:]
                if self.start_tag.startswith(candidate):
                    # Valid partial prefix, buffer it and emit everything before candidate_idx
                    prefix = self._speech_buffer[:candidate_idx]
                    if prefix:
                        speech_out.append(prefix)
                    self._speech_buffer = candidate
                    break
                else:
                    # Look if there is another "<<" further inside candidate
                    next_idx = candidate[1:].find("<<")
                    if next_idx != -1:
                        # Advance up to next candidate
                        advance = 1 + next_idx
                        speech_out.append(self._speech_buffer[: candidate_idx + advance])
                        self._speech_buffer = self._speech_buffer[candidate_idx + advance :]
                        continue
                    else:
                        # Candidate is not our tag prefix: flush everything
                        speech_out.append(self._speech_buffer)
                        self._speech_buffer = ""
                        break
            else:
                # No "<<" found anywhere: flush entire buffer
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
            # If stream closed without end tag, attempt to parse partial JSON
            tool = self._parse_tool_payload(self._tool_buffer.strip())
            if tool:
                tools.append(tool)
                self._completed_tools.append(tool)
            self._tool_buffer = ""

        self._state = STATE_SPEAKING
        return speech, tools


__all__ = [
    "ParsedToolCall",
    "NonceGuardedStreamParser",
    "STATE_SPEAKING",
    "STATE_BUFFERING_POSSIBLE_TAG",
    "STATE_ACCUMULATING_TOOL_CALL",
]
