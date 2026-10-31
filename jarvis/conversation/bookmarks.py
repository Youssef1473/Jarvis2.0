"""Conversational Resumption Bookmarks & Interruption Stack for Jarvis 2.0.

Maintains a bounded LIFO ReasoningBookmarkStack (capacity 8) capturing
interrupted cognitive frames across sub-1us Win32 atomic barge-ins.
(Dimension 6.1: Smart Resumption).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from jarvis.context.flow_state_mirror import FlowStateSnapshot

logger = logging.getLogger("Jarvis.Conversation.Bookmarks")

MAX_BOOKMARK_CAPACITY = 8
DEFAULT_BOOKMARK_TTL_SEC: float = 900.0
DEFAULT_BOOKMARK_TTL_NS: int = 900_000_000_000  # 900s (15 minutes) validity


@dataclass(frozen=True)
class ReasoningBookmark:
    """Immutable snapshot of an interrupted reasoning and speech synthesis frame."""
    turn_id: str
    original_query: str = ""
    topic_label: str = "the architecture"
    spoken_clauses_completed: tuple[str, ...] = ()
    unspoken_clauses_remaining: tuple[str, ...] = ()
    resolved_tool_outputs: dict[str, Any] = field(default_factory=dict)
    flow_state_xml: str = ""
    created_monotonic_ns: int = field(default_factory=time.perf_counter_ns)
    ttl_ns: int = DEFAULT_BOOKMARK_TTL_NS  # 900s (15 minutes) validity
    # Backwards compatibility fields:
    user_query: str = ""
    partial_llm_plan: str = ""
    flow_state_snapshot: FlowStateSnapshot | None = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.original_query and self.user_query:
            object.__setattr__(self, "original_query", self.user_query)
        elif not self.user_query and self.original_query:
            object.__setattr__(self, "user_query", self.original_query)

    def is_expired(self, now_ns: int | None = None) -> bool:
        current = now_ns if now_ns is not None else time.perf_counter_ns()
        return (current - self.created_monotonic_ns) > self.ttl_ns

    @property
    def remaining_speech_text(self) -> str:
        return " ".join(self.unspoken_clauses_remaining).strip()


class ReasoningBookmarkStack:
    """
    Thread-safe bounded LIFO stack preserving interrupted cognitive frames across
    sub-1us Win32 atomic barge-in events (Dimension 6.1: Smart Resumption).
    """
    _INTERJECTION_PATTERNS = re.compile(
        r"^(wait|hold on|quick question|which|what was|who|where|when|how many|was that|did you say)\b",
        re.IGNORECASE,
    )
    _CANCEL_PATTERNS = re.compile(
        r"^(stop|cancel|never mind|forget it|drop that|abort|scratch that)\b",
        re.IGNORECASE,
    )
    _RESUME_PATTERNS = re.compile(
        r"^(continue|go on|resume|as you were saying|please continue|keep going|carry on)\b",
        re.IGNORECASE,
    )

    _instance: ReasoningBookmarkStack | None = None
    _singleton_lock = threading.Lock()

    def __init__(self, max_depth: int = MAX_BOOKMARK_CAPACITY, capacity: int | None = None) -> None:
        eff_capacity = capacity if capacity is not None else max_depth
        self.max_depth = eff_capacity
        self.capacity = eff_capacity
        self._stack: deque[ReasoningBookmark] = deque(maxlen=eff_capacity)
        self._lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> ReasoningBookmarkStack:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def push_interrupted_turn(
        self,
        turn_id: str,
        original_query: str = "",
        topic_label: str = "the architecture",
        spoken_clauses: list[str] | None = None,
        unspoken_clauses: list[str] | None = None,
        resolved_tool_outputs: dict[str, Any] | None = None,
        flow_state_xml: str = "",
        user_query: str | None = None,
        partial_llm_plan: str = "",
        flow_state_snapshot: FlowStateSnapshot | None = None,
    ) -> ReasoningBookmark | None:
        """Pushes an interrupted turn onto the LIFO stack if unspoken clauses remain."""
        eff_query = original_query or user_query or ""
        raw_remaining = unspoken_clauses if unspoken_clauses is not None else ([partial_llm_plan] if partial_llm_plan else ["resumed turn"])
        cleaned_remaining = [c.strip() for c in raw_remaining if c and c.strip()]
        if not cleaned_remaining:
            return None

        eff_spoken = spoken_clauses or []
        eff_resolved = dict(resolved_tool_outputs or {})

        bookmark = ReasoningBookmark(
            turn_id=turn_id,
            original_query=eff_query,
            topic_label=topic_label,
            spoken_clauses_completed=tuple(eff_spoken),
            unspoken_clauses_remaining=tuple(cleaned_remaining),
            resolved_tool_outputs=eff_resolved,
            flow_state_xml=flow_state_xml,
            created_monotonic_ns=time.perf_counter_ns(),
            user_query=eff_query,
            partial_llm_plan=partial_llm_plan,
            flow_state_snapshot=flow_state_snapshot,
            timestamp=time.time(),
        )
        with self._lock:
            self._stack.append(bookmark)
            logger.info("Reasoning bookmark pushed: turn %s (depth=%d)", turn_id, len(self._stack))
        return bookmark

    def peek_valid(self) -> ReasoningBookmark | None:
        with self._lock:
            while self._stack:
                top = self._stack[-1]
                if top.is_expired():
                    self._stack.pop()
                else:
                    return top
            return None

    def pop_valid(self) -> ReasoningBookmark | None:
        with self._lock:
            while self._stack:
                top = self._stack.pop()
                if not top.is_expired():
                    logger.info("Restored reasoning bookmark: turn %s (remaining=%d)", top.turn_id, len(self._stack))
                    return top
            return None

    def pop_latest_bookmark(self) -> ReasoningBookmark | None:
        return self.pop_valid()

    def peek_latest_bookmark(self) -> ReasoningBookmark | None:
        return self.peek_valid()

    def inject_resumed_context(self, pivot_prompt: str) -> tuple[str, dict[str, Any]]:
        """Restores latest bookmark outputs and formats them into the pivot prompt context."""
        bookmark = self.pop_valid()
        if not bookmark:
            return pivot_prompt, {}

        injected_outputs = bookmark.resolved_tool_outputs
        context_block = (
            f"[SYSTEM NOTE: User interrupted prior turn '{bookmark.turn_id}' ('{bookmark.original_query}'). "
            f"Prior resolved tool outputs are preserved: {list(injected_outputs.keys())}. "
            f"Pivot to user's updated directive immediately without re-executing resolved tools.]\n"
        )
        enriched_prompt = f"{context_block}\n{pivot_prompt}"
        return enriched_prompt, injected_outputs

    def clear(self) -> None:
        with self._lock:
            self._stack.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._stack)

    @classmethod
    def classify_barge_in_intent(cls, utterance: str) -> str:
        """
        Classifies whether a barge-in utterance is:
        - 'ABORT': Sir wants to stop the explanation completely.
        - 'CLARIFY_AND_RESUME': Brief clarifying tangent or explicit resumption intent.
        - 'PIVOT': A brand-new directive that supersedes the prior topic.
        """
        text = (utterance or "").strip()
        if cls._CANCEL_PATTERNS.search(text):
            return "ABORT"
        if cls._RESUME_PATTERNS.search(text):
            return "CLARIFY_AND_RESUME"
        word_count = len(text.split())
        if word_count <= 12 and (text.endswith("?") or cls._INTERJECTION_PATTERNS.search(text)):
            return "CLARIFY_AND_RESUME"
        return "PIVOT"

    @classmethod
    def is_resumption_intent(cls, utterance: str) -> bool:
        """Returns True if utterance matches explicit multi-phrase resumption ('continue', 'go on', 'resume', 'as you were saying')."""
        text = (utterance or "").strip()
        return bool(cls._RESUME_PATTERNS.search(text))

    def format_prompt_injection(self, utterance: str) -> str:
        """
        Generates the XML context block injected into JarvisBrain when a valid bookmark exists.
        """
        intent = self.classify_barge_in_intent(utterance)
        if intent == "ABORT":
            self.clear()
            return ""

        top = self.peek_valid()
        if top is None:
            return ""

        import html

        if intent == "CLARIFY_AND_RESUME":
            # Pop so we consume the bookmark on this resumption turn
            self.pop_valid()
            escaped_topic = html.escape(str(top.topic_label))
            escaped_spoken = html.escape(' '.join(top.spoken_clauses_completed))
            escaped_remaining = html.escape(str(top.remaining_speech_text))
            escaped_tools = html.escape(str(top.resolved_tool_outputs))
            if self.is_resumption_intent(utterance):
                directive = (
                    f"Sir requested to resume. Immediately bridge with: 'Continuing with {escaped_topic}...' "
                    "and deliver the remaining explanation."
                )
            else:
                directive = (
                    "Answer Sir's clarifying question in 1 crisp sentence, then immediately "
                    f"bridge with: 'Continuing with {escaped_topic}...' and deliver the remaining explanation."
                )
            return (
                '<active_resumption_bookmark mode="CLARIFY_AND_RESUME">\n'
                f"  <suspended_topic>{escaped_topic}</suspended_topic>\n"
                f"  <already_spoken>{escaped_spoken}</already_spoken>\n"
                f"  <remaining_to_speak>{escaped_remaining}</remaining_to_speak>\n"
                f"  <cached_tool_results>{escaped_tools}</cached_tool_results>\n"
                f"  <directive>{directive}</directive>\n"
                "</active_resumption_bookmark>"
            )

        # For a pivot ("No, use Option B"), keep resolved tool outputs hot without repeating speech
        self.pop_valid()
        escaped_query = html.escape(str(top.original_query))
        escaped_tools = html.escape(str(top.resolved_tool_outputs))
        return (
            '<active_resumption_bookmark mode="PIVOT_WITH_CACHED_TOOLS">\n'
            f"  <prior_query>{escaped_query}</prior_query>\n"
            f"  <cached_tool_results>{escaped_tools}</cached_tool_results>\n"
            "  <directive>Sir pivoted direction. Reuse the cached tool results above without "
            "re-executing read-only tools.</directive>\n"
            "</active_resumption_bookmark>"
        )
