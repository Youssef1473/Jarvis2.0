"""Dual-Track Milestone Narration and Acoustic Collision Coordinator for Jarvis 2.0.

Provides race-condition-free coordination between Track A (Speculative Milestone Narration)
and Track B (Concurrent Tool Execution).

Key Systems Capabilities:
1. Lock-free Monotonic Cancellation Tokens for sub-millisecond stream abort.
2. Fast-Path Read-Only Result Caching (<1ms return for idempotent/read-only tools).
3. Speculative Threshold Gating (default 320ms): eliminates dead air for long tools
   while keeping fast-path tools completely silent.
4. Acoustic Collision Gate: ensures Track A spoken clauses finish cleanly before Track B
   synthesis begins, preventing speech overlapping and acoustic whiplash.
5. Barge-in Snapshotting: preserves interrupted tool outputs into ReasoningBookmarkStack.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Coroutine, Dict, Optional, Tuple, Union

from jarvis.core.pipeline.interim_bridge import _INTERIM_PHRASE_MAP, DEFAULT_INTERIM_PHRASE
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Pipeline.DualTrack")


@dataclass(frozen=True)
class BookmarkSnapshot:
    """Immutable state snapshot pushed to ReasoningBookmarkStack on barge-in."""
    turn_id: int
    original_query: str
    tool_name: str
    tool_args: Dict[str, Any]
    resolved_payload: Optional[Any]
    timestamp: float = field(default_factory=time.monotonic)


class MonotonicCancellationToken:
    """Lock-free monotonic cancellation token for sub-millisecond stream abort."""

    __slots__ = ("_epoch", "_cancelled_epoch")

    def __init__(self) -> None:
        self._epoch: int = 0
        self._cancelled_epoch: int = -1

    def next_turn(self) -> int:
        """Advances to the next monotonic turn epoch."""
        self._epoch += 1
        return self._epoch

    def cancel_current(self) -> None:
        """Atomically marks the current active epoch as cancelled."""
        self._cancelled_epoch = self._epoch

    def is_cancelled(self, turn_epoch: int) -> bool:
        """Returns True if the specified turn epoch has been cancelled."""
        return turn_epoch <= self._cancelled_epoch

    @property
    def current_epoch(self) -> int:
        """Returns current monotonic epoch."""
        return self._epoch

    def raise_if_cancelled(self, turn_epoch: int) -> None:
        """Raises RuntimeError if the specified turn epoch is cancelled."""
        if self.is_cancelled(turn_epoch):
            raise RuntimeError(f"Turn epoch {turn_epoch} cancelled by voice barge-in.")


class DualTrackCoordinator:
    """Coordinates Track A (Milestone Narration) and Track B (Async Tool Execution)

    with acoustic collision avoidance, read-only result caching, and barge-in bookmarking.
    """

    # Heuristic prefix patterns for automatic read-only tool classification
    _READ_ONLY_PREFIXES = (
        "get_",
        "read_",
        "search_",
        "audit_",
        "list_",
        "inspect_",
        "check_",
        "poll_",
        "query_",
    )

    def __init__(
        self,
        tts_enqueue_fn: Optional[Callable[[str, int], Awaitable[None]]] = None,
        tts_wait_clause_done_fn: Optional[Callable[[], Awaitable[None]]] = None,
        wasapi_flush_fn: Optional[Callable[[], None]] = None,
        bookmark_stack: Optional[Any] = None,
        filler_threshold_ms: float = 320.0,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._tts_enqueue = tts_enqueue_fn or self._default_tts_enqueue
        self._tts_wait_clause_done = tts_wait_clause_done_fn or self._default_wait_clause
        self._wasapi_flush = wasapi_flush_fn or (lambda: None)
        self._bookmarks = bookmark_stack if bookmark_stack is not None else []
        self._filler_threshold_s = max(0.01, filler_threshold_ms / 1000.0)
        self._cache_ttl_s = max(0.0, cache_ttl_seconds)

        self.token_gate = MonotonicCancellationToken()
        # Read-only tool cache: {cache_key: (expiry_monotonic, result)}
        self._ro_cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    async def _default_tts_enqueue(phrase: str, epoch: int) -> None:
        """Default async no-op for TTS enqueue."""
        pass

    @staticmethod
    async def _default_wait_clause() -> None:
        """Default async no-op for clause completion."""
        pass

    def trigger_barge_in(self) -> None:
        """Invoked by VAD / Hotkey interrupt thread.

        Flushes hardware playback buffer and invalidates active generation epoch.
        """
        self.token_gate.cancel_current()
        try:
            self._wasapi_flush()
        except Exception as e:
            logger.debug("WASAPI flush callback notice: %s", e)

    def _get_cached_result(self, key: str) -> Optional[Any]:
        """Thread-safe retrieval of cached read-only tool results."""
        with self._cache_lock:
            entry = self._ro_cache.get(key)
            if entry is not None:
                expiry, val = entry
                if time.monotonic() < expiry:
                    return val
                del self._ro_cache[key]
            return None

    def _set_cached_result(self, key: str, val: Any) -> None:
        """Thread-safe caching of read-only tool results with TTL."""
        with self._cache_lock:
            self._ro_cache[key] = (time.monotonic() + self._cache_ttl_s, val)

    def clear_cache(self) -> None:
        """Clears the read-only tool result cache."""
        with self._cache_lock:
            self._ro_cache.clear()

    @classmethod
    def is_read_only_tool(cls, tool_name: str) -> bool:
        """Determines whether a tool is read-only via SkillRegistry metadata or safe prefixes."""
        try:
            meta = SkillRegistry.get_metadata(tool_name)
            if meta is not None and getattr(meta, "read_only", False):
                return True
        except Exception:
            pass
        return tool_name.startswith(cls._READ_ONLY_PREFIXES)

    @staticmethod
    def get_milestone_phrase(tool_name: str) -> str:
        """Selects a context-appropriate, calm status phrase for the given tool."""
        return _INTERIM_PHRASE_MAP.get(tool_name, DEFAULT_INTERIM_PHRASE)

    async def execute_with_masking(
        self,
        user_query: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_coro: Union[Coroutine[Any, Any, Any], Callable[..., Any]],
        milestone_phrase: Optional[str] = None,
        is_read_only: Optional[bool] = None,
    ) -> Optional[Any]:
        """Executes a tool on Track B while conditionally firing Track A acoustic filler

        only if tool execution exceeds `filler_threshold_ms`.
        """
        turn_epoch = self.token_gate.next_turn()
        effective_ro = is_read_only if is_read_only is not None else self.is_read_only_tool(tool_name)
        phrase = milestone_phrase or self.get_milestone_phrase(tool_name)

        # Deterministic cache key serialization
        try:
            args_repr = json.dumps(tool_args or {}, sort_keys=True, default=str)
        except Exception:
            args_repr = str(sorted((tool_args or {}).items()))
        cache_key = f"{tool_name}:{args_repr}"

        # 1. Fast-Path Read-Only Cache Check (0ms dead air)
        if effective_ro:
            cached = self._get_cached_result(cache_key)
            if cached is not None:
                logger.debug("RO Cache Hit for %s (<1ms)", tool_name)
                # Close unawaited coroutine if passed to prevent ResourceWarning
                if inspect.iscoroutine(tool_coro):
                    tool_coro.close()
                return cached

        # Prepare coroutine for Track B
        if inspect.iscoroutine(tool_coro):
            coro_to_run = tool_coro
        elif callable(tool_coro):
            coro_to_run = asyncio.to_thread(tool_coro)
        else:
            raise TypeError(f"tool_coro must be a coroutine or callable, got {type(tool_coro)}")

        # 2. Launch Track B (Speculative/Concurrent Tool Execution)
        tool_task = asyncio.create_task(coro_to_run, name=f"TrackB_{tool_name}_{turn_epoch}")
        filler_spoken = False

        try:
            # Wait up to threshold (e.g. 320ms) before committing to Track A speech
            done, _ = await asyncio.wait(
                [tool_task],
                timeout=self._filler_threshold_s,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if self.token_gate.is_cancelled(turn_epoch):
                tool_task.cancel()
                return None

            # 3. Tool exceeded threshold -> Fire Track A Milestone Narration
            if not done:
                filler_spoken = True
                logger.debug(
                    "Tool >%.0fms; firing Track A filler: '%s'",
                    self._filler_threshold_s * 1000,
                    phrase,
                )
                await self._tts_enqueue(phrase, turn_epoch)

            # 4. Await Track B completion
            payload = await tool_task

            if effective_ro and payload is not None:
                self._set_cached_result(cache_key, payload)

            # 5. Check if user barged in while tool was resolving
            if self.token_gate.is_cancelled(turn_epoch):
                logger.info("Barge-in detected post-tool resolution. Snapshotting to BookmarkStack.")
                snapshot = BookmarkSnapshot(
                    turn_id=turn_epoch,
                    original_query=user_query,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    resolved_payload=payload,
                )
                if hasattr(self._bookmarks, "append"):
                    self._bookmarks.append(snapshot)
                elif hasattr(self._bookmarks, "push_interrupted_turn"):
                    try:
                        self._bookmarks.push_interrupted_turn(
                            turn_id=str(turn_epoch),
                            original_query=user_query,
                            topic_label=tool_name,
                            resolved_tool_outputs={tool_name: payload},
                        )
                    except Exception as b_err:
                        logger.debug("Bookmark stack push notice: %s", b_err)
                return None

            # 6. Acoustic Collision Gate: Ensure Track A clause finishes cleanly before Track B synthesis starts
            if filler_spoken:
                await self._tts_wait_clause_done()

            return payload

        except asyncio.CancelledError:
            tool_task.cancel()
            raise
        except Exception as exc:
            logger.error("Track B tool execution failed (%s): %s", tool_name, exc, exc_info=True)
            raise


__all__ = [
    "BookmarkSnapshot",
    "MonotonicCancellationToken",
    "DualTrackCoordinator",
]
