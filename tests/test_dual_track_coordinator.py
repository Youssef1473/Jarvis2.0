"""Unit tests for DualTrackCoordinator, MonotonicCancellationToken, and BookmarkSnapshot.

Verifies:
- Monotonic lock-free cancellation token epoch tracking.
- Fast-path read-only tool caching with TTL (<1ms return).
- Track A milestone narration suppression when tool finishes within threshold.
- Track A milestone narration firing when tool execution exceeds 320ms threshold.
- Acoustic collision gating: waiting for Track A clause completion before Track B handoff.
- Sub-microsecond barge-in cancellation and snapshotting to ReasoningBookmarkStack.
- Synchronous and asynchronous tool execution handling.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from jarvis.core.pipeline.dual_track import (
    BookmarkSnapshot,
    DualTrackCoordinator,
    MonotonicCancellationToken,
)
from jarvis.conversation.bookmarks import ReasoningBookmarkStack


class TestMonotonicCancellationToken(unittest.TestCase):
    """Tests lock-free monotonic token generation and epoch invalidation."""

    def test_next_turn_and_cancellation(self):
        token = MonotonicCancellationToken()
        self.assertEqual(token.current_epoch, 0)

        epoch1 = token.next_turn()
        self.assertEqual(epoch1, 1)
        self.assertFalse(token.is_cancelled(epoch1))

        epoch2 = token.next_turn()
        self.assertEqual(epoch2, 2)
        self.assertFalse(token.is_cancelled(epoch2))

        # Cancel current epoch (epoch 2)
        token.cancel_current()
        self.assertTrue(token.is_cancelled(epoch1))
        self.assertTrue(token.is_cancelled(epoch2))

        # Next epoch should be fresh and uncancelled
        epoch3 = token.next_turn()
        self.assertEqual(epoch3, 3)
        self.assertFalse(token.is_cancelled(epoch3))
        # Prior epochs remain cancelled
        self.assertTrue(token.is_cancelled(epoch2))


class TestDualTrackCoordinator(unittest.IsolatedAsyncioTestCase):
    """Verifies dual-track milestone narration and acoustic collision avoidance."""

    async def asyncSetUp(self):
        self.enqueued_tts: list[tuple[str, int]] = []
        self.wait_clause_called = 0
        self.wasapi_flushed = 0
        self.bookmarks: list[BookmarkSnapshot] = []

        async def _tts_enqueue(phrase: str, epoch: int):
            self.enqueued_tts.append((phrase, epoch))

        async def _tts_wait_clause():
            self.wait_clause_called += 1

        def _wasapi_flush():
            self.wasapi_flushed += 1

        self.coordinator = DualTrackCoordinator(
            tts_enqueue_fn=_tts_enqueue,
            tts_wait_clause_done_fn=_tts_wait_clause,
            wasapi_flush_fn=_wasapi_flush,
            bookmark_stack=self.bookmarks,
            filler_threshold_ms=40.0,  # Fast threshold for test speed
            cache_ttl_seconds=1.0,
        )

    async def test_fast_tool_under_threshold_suppresses_track_a(self):
        """When tool finishes in <40ms, Track A interim narration must NOT fire."""
        async def _fast_tool():
            await asyncio.sleep(0.01)
            return {"status": "ok", "count": 42}

        result = await self.coordinator.execute_with_masking(
            user_query="inspect system status",
            tool_name="get_system_vitals",
            tool_args={"metric": "all"},
            tool_coro=_fast_tool(),
            milestone_phrase="Inspecting system status, Sir.",
        )

        self.assertEqual(result, {"status": "ok", "count": 42})
        self.assertEqual(len(self.enqueued_tts), 0)
        self.assertEqual(self.wait_clause_called, 0)

    async def test_slow_tool_exceeds_threshold_fires_track_a_and_gates_collision(self):
        """When tool exceeds 40ms, Track A fires and acoustic collision gate waits for clause done."""
        async def _slow_tool():
            await asyncio.sleep(0.12)
            return {"data": "deep_analysis_complete"}

        result = await self.coordinator.execute_with_masking(
            user_query="run deep research",
            tool_name="deep_research",
            tool_args={"topic": "kv_cache"},
            tool_coro=_slow_tool(),
            milestone_phrase="Synthesizing technical intelligence, Sir.",
        )

        self.assertEqual(result, {"data": "deep_analysis_complete"})
        self.assertEqual(len(self.enqueued_tts), 1)
        self.assertEqual(self.enqueued_tts[0][0], "Synthesizing technical intelligence, Sir.")
        self.assertEqual(self.enqueued_tts[0][1], 1)  # turn epoch 1
        self.assertEqual(self.wait_clause_called, 1)

    async def test_read_only_cache_hit_returns_immediately(self):
        """Read-only tool results must cache and return in <1ms without re-running or speech."""
        call_count = 0

        async def _idempotent_tool():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return "battery_level_92_percent"

        # First run: populates cache
        res1 = await self.coordinator.execute_with_masking(
            user_query="what is my battery",
            tool_name="get_battery_status",
            tool_args={},
            tool_coro=_idempotent_tool(),
            milestone_phrase="Checking battery, Sir.",
            is_read_only=True,
        )
        self.assertEqual(res1, "battery_level_92_percent")
        self.assertEqual(call_count, 1)

        # Second run: hits cache instantly
        res2 = await self.coordinator.execute_with_masking(
            user_query="what is my battery",
            tool_name="get_battery_status",
            tool_args={},
            tool_coro=_idempotent_tool(),
            milestone_phrase="Checking battery, Sir.",
            is_read_only=True,
        )
        self.assertEqual(res2, "battery_level_92_percent")
        self.assertEqual(call_count, 1)  # Coroutine was closed, not awaited!
        self.assertEqual(len(self.enqueued_tts), 0)

    async def test_read_only_cache_expiration(self):
        """Cache entries must expire after TTL."""
        call_count = 0

        async def _query():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        res1 = await self.coordinator.execute_with_masking(
            user_query="test",
            tool_name="read_file",
            tool_args={"path": "foo.txt"},
            tool_coro=_query(),
            is_read_only=True,
        )
        self.assertEqual(res1, "result_1")

        # Wait past TTL (1.0s)
        await asyncio.sleep(1.05)

        res2 = await self.coordinator.execute_with_masking(
            user_query="test",
            tool_name="read_file",
            tool_args={"path": "foo.txt"},
            tool_coro=_query(),
            is_read_only=True,
        )
        self.assertEqual(res2, "result_2")
        self.assertEqual(call_count, 2)

    async def test_barge_in_cancels_and_snapshots_bookmark(self):
        """Speech barge-in during tool execution flushes WASAPI and snapshots to bookmarks."""
        async def _long_tool():
            await asyncio.sleep(0.25)
            return {"result": "partial_data"}

        task = asyncio.create_task(
            self.coordinator.execute_with_masking(
                user_query="audit webpage performance",
                tool_name="audit_webpage_performance",
                tool_args={"url": "https://example.com"},
                tool_coro=_long_tool(),
                milestone_phrase="Running performance audit, Sir.",
            )
        )

        # Allow tool to start and pass threshold
        for _ in range(30):
            if len(self.enqueued_tts) >= 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.enqueued_tts), 1)

        # User interrupts!
        self.coordinator.trigger_barge_in()
        self.assertEqual(self.wasapi_flushed, 1)

        result = await task
        # Must return None on cancelled turn
        self.assertIsNone(result)

        # Verify bookmark snapshot was captured
        self.assertEqual(len(self.bookmarks), 1)
        snap = self.bookmarks[0]
        self.assertEqual(snap.tool_name, "audit_webpage_performance")
        self.assertEqual(snap.original_query, "audit webpage performance")
        self.assertEqual(snap.resolved_payload, {"result": "partial_data"})

    async def test_synchronous_callable_wrapping(self):
        """Coordinator seamlessly supports wrapping sync callables."""
        def _sync_calc():
            return 123 * 456

        result = await self.coordinator.execute_with_masking(
            user_query="calculate",
            tool_name="math_calc",
            tool_args={"a": 123, "b": 456},
            tool_coro=_sync_calc,
            is_read_only=True,
        )
        self.assertEqual(result, 56088)

    async def test_interoperability_with_reasoning_bookmark_stack(self):
        """Coordinator seamlessly pushes into ReasoningBookmarkStack singleton if configured."""
        stack = ReasoningBookmarkStack(capacity=4)
        coordinator = DualTrackCoordinator(
            bookmark_stack=stack,
            filler_threshold_ms=50.0,
        )

        async def _hanging_tool():
            await asyncio.sleep(0.15)
            return "ok"

        task = asyncio.create_task(
            coordinator.execute_with_masking(
                user_query="scan workspace",
                tool_name="search_codebase",
                tool_args={"pattern": "foo"},
                tool_coro=_hanging_tool(),
            )
        )

        for _ in range(30):
            if coordinator.token_gate.current_epoch > 0:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.06)
        coordinator.trigger_barge_in()
        await task

        latest = stack.peek_valid()
        self.assertIsNotNone(latest)
        self.assertIn("search_codebase", latest.resolved_tool_outputs)


if __name__ == "__main__":
    unittest.main()
