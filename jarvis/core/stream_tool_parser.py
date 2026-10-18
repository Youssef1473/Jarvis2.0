"""Backward-compatible re-export shim for streaming tool parser.
Canonical implementation resides in jarvis.core.streaming_tool_parser.
"""
from jarvis.core.streaming_tool_parser import (
    DEFAULT_IDEMPOTENT_READ_TOOLS,
    ParsedToolCall,
    StreamingToolCallParser,
    NonceGuardedStreamParser,
    STATE_SPEAKING,
    STATE_BUFFERING_POSSIBLE_TAG,
    STATE_ACCUMULATING_TOOL_CALL,
)

__all__ = [
    "DEFAULT_IDEMPOTENT_READ_TOOLS",
    "ParsedToolCall",
    "StreamingToolCallParser",
    "NonceGuardedStreamParser",
    "STATE_SPEAKING",
    "STATE_BUFFERING_POSSIBLE_TAG",
    "STATE_ACCUMULATING_TOOL_CALL",
]
