"""Backward-compatible re-export shim for streaming tool parser.
Canonical implementation resides in jarvis.core.streaming_tool_parser.
"""
from jarvis.core.streaming_tool_parser import (
    DEFAULT_IDEMPOTENT_READ_TOOLS,
    STATE_ACCUMULATING_TOOL_CALL,
    STATE_BUFFERING_POSSIBLE_TAG,
    STATE_SPEAKING,
    NonceGuardedStreamParser,
    ParsedToolCall,
    StreamingToolCallParser,
)

__all__ = [
    "DEFAULT_IDEMPOTENT_READ_TOOLS",
    "STATE_ACCUMULATING_TOOL_CALL",
    "STATE_BUFFERING_POSSIBLE_TAG",
    "STATE_SPEAKING",
    "NonceGuardedStreamParser",
    "ParsedToolCall",
    "StreamingToolCallParser",
]
