"""Automated tests for speculative streaming tool call parser and dual-track acoustic masking."""
import asyncio
import sys
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.pipeline.dual_track import DualTrackCoordinator
from jarvis.core.streaming_tool_parser import StreamingToolCallParser


class TestStreamingToolParserSuite(unittest.TestCase):
    """Verifies partial JSON stream decoding and speculative early dispatch."""

    def test_speculative_early_tool_dispatch(self):
        """Verifies closing a read-only tool JSON object immediately invokes dispatch callback."""
        dispatched = []

        def callback(name, args):
            dispatched.append((name, args))
            return "speculative_result_123"

        parser = StreamingToolCallParser(
            idempotent_read_tools={"get_battery_status", "get_weather"},
            dispatch_callback=callback,
        )

        # Feed incremental stream tokens
        parser.feed_chunk('{"name": ', call_id="c1")
        parser.feed_chunk('"get_battery_status", ', call_id="c1")
        parser.feed_chunk('"arguments": {}', call_id="c1")
        res = parser.feed_chunk('}', call_id="c1")

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][0], "get_battery_status")
        self.assertEqual(res, "speculative_result_123")

        # Duplicate feed with same call_id should not re-trigger
        res2 = parser.feed_chunk('}', call_id="c1")
        self.assertIsNone(res2)
        self.assertEqual(len(dispatched), 1)

    def test_non_idempotent_tool_not_dispatched_early(self):
        """Verifies destructive/stateful tools (not in read allowlist) are not dispatched early."""
        dispatched = []
        parser = StreamingToolCallParser(
            idempotent_read_tools={"get_weather"},
            dispatch_callback=lambda n, a: dispatched.append(n),
        )

        res = parser.feed_chunk('{"name": "execute_powershell", "arguments": {"command": "dir"}}', call_id="c2")
        self.assertIsNone(res)
        self.assertEqual(len(dispatched), 0)

    def test_streaming_tool_with_dual_track_fast_cache(self):
        """Verifies StreamingToolCallParser honors DualTrackCoordinator read-only cache hit."""
        spoken_fillers = []

        async def fake_tts_enqueue(phrase: str, epoch: int):
            spoken_fillers.append(phrase)

        coordinator = DualTrackCoordinator(
            tts_enqueue_fn=fake_tts_enqueue,
            filler_threshold_ms=50.0,
            cache_ttl_seconds=60.0,
        )
        # Pre-seed cache
        coordinator._set_cached_result('get_battery_status:{}', {"percent": 95, "plugged": True})

        parser = StreamingToolCallParser(
            idempotent_read_tools={"get_battery_status"},
            coordinator=coordinator,
        )

        res = parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="cached_1")
        self.assertEqual(res, {"percent": 95, "plugged": True})
        self.assertEqual(len(spoken_fillers), 0, "Cached read-only hit must produce zero filler audio")

    def test_streaming_tool_async_feed_chunk_with_masking(self):
        """Verifies feed_chunk_async executes tool and gates acoustic narration."""
        spoken_fillers = []

        async def fake_tts_enqueue(phrase: str, epoch: int):
            spoken_fillers.append(phrase)

        coordinator = DualTrackCoordinator(
            tts_enqueue_fn=fake_tts_enqueue,
            filler_threshold_ms=50.0,
        )

        async def run_test():
            def slow_tool(name, args):
                import time
                time.sleep(0.08)
                return "slow_tool_done"

            parser = StreamingToolCallParser(
                idempotent_read_tools={"get_system_vitals"},
                dispatch_callback=slow_tool,
                coordinator=coordinator,
            )

            res = await parser.feed_chunk_async(
                '{"name": "get_system_vitals", "arguments": {}}',
                call_id="async_1",
            )
            self.assertEqual(res, "slow_tool_done")
            self.assertEqual(len(spoken_fillers), 1)

        asyncio.run(run_test())

    def test_interleaved_streaming_tool_calls_buffer_isolation(self):
        """Verifies that interleaved streaming chunks for distinct call_ids do not corrupt buffers."""
        dispatched = []

        def callback(name, args):
            dispatched.append((name, args))
            return f"result_{name}"

        parser = StreamingToolCallParser(
            idempotent_read_tools={"get_battery_status", "get_weather"},
            dispatch_callback=callback,
        )

        # Interleave feed chunks from c1 (weather) and c2 (battery)
        parser.feed_chunk('{"name": ', call_id="c1")
        parser.feed_chunk('{"name": "get_battery_status"', call_id="c2")
        parser.feed_chunk('"get_weather", "arguments": {"city": ', call_id="c1")
        parser.feed_chunk(', "arguments": {}}', call_id="c2")  # c2 completes
        parser.feed_chunk('"London"}}', call_id="c1")           # c1 completes

        self.assertEqual(len(dispatched), 2)
        # Verify c2 completed first based on chunk completion order
        self.assertEqual(dispatched[0][0], "get_battery_status")
        self.assertEqual(dispatched[0][1], {})
        self.assertEqual(dispatched[1][0], "get_weather")
        self.assertEqual(dispatched[1][1], {"city": "London"})

    def test_exactly_once_callback_dispatch_with_coordinator(self):
        """Verifies that feed_chunk does not dispatch duplicate callback when coordinator is attached."""
        dispatched_count = 0

        async def run_test():
            nonlocal dispatched_count

            def counting_callback(name, args):
                nonlocal dispatched_count
                dispatched_count += 1
                return "ok"

            coordinator = DualTrackCoordinator(
                filler_threshold_ms=50.0,
            )

            parser = StreamingToolCallParser(
                idempotent_read_tools={"get_battery_status"},
                dispatch_callback=counting_callback,
                coordinator=coordinator,
            )

            res = parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="coord_call")
            # In an active event loop with coordinator, feed_chunk returns the asyncio.Task
            self.assertIsNotNone(res)
            self.assertTrue(asyncio.iscoroutine(res) or isinstance(res, asyncio.Task))
            await res
            # Must be called exactly once
            self.assertEqual(dispatched_count, 1)

        asyncio.run(run_test())

    def test_feed_chunk_task_exception_handled_safely(self):
        """Verifies that exceptions in speculative tasks do not crash the parser or unretrieved loop state."""
        async def run_test():
            def failing_callback(name, args):
                raise ValueError("Simulated tool crash in speculative execution")

            coordinator = DualTrackCoordinator(filler_threshold_ms=50.0)
            parser = StreamingToolCallParser(
                idempotent_read_tools={"get_battery_status"},
                dispatch_callback=failing_callback,
                coordinator=coordinator,
            )

            task = parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="fail_call")
            self.assertIsInstance(task, asyncio.Task)

            # Awaiting the task raises the exception, verifying it propagates to caller properly
            with self.assertRaises(ValueError) as ctx:
                await task
            self.assertIn("Simulated tool crash", str(ctx.exception))

        asyncio.run(run_test())

    def test_fire_and_forget_task_exception_logged_without_warning(self):
        """Verifies that an unawaited speculative task exception is handled and logged via the done callback."""
        async def run_test():
            def failing_callback(name, args):
                raise RuntimeError("Fire-and-forget tool failure")

            coordinator = DualTrackCoordinator(filler_threshold_ms=10.0)
            parser = StreamingToolCallParser(
                idempotent_read_tools={"get_battery_status"},
                dispatch_callback=failing_callback,
                coordinator=coordinator,
            )

            with self.assertLogs("Jarvis.StreamingToolParser", level="ERROR") as log_capture:
                # Intentionally do NOT await the returned task directly
                task = parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="unawaited_fail")
                self.assertIsInstance(task, asyncio.Task)
                # Wait for task completion using asyncio.wait without retrieving its result/exception
                await asyncio.wait([task], timeout=2.0)

            # Verify the exception was intercepted and logged by the named done callback
            self.assertTrue(any("Fire-and-forget tool failure" in msg for msg in log_capture.output))

        asyncio.run(run_test())

    def test_empty_allowlist_disables_speculative_dispatch(self):
        """Verifies that explicitly providing an empty set for idempotent_read_tools does not fall back to defaults."""
        dispatched = []
        parser = StreamingToolCallParser(
            idempotent_read_tools=set(),
            dispatch_callback=lambda n, a: dispatched.append(n),
        )
        self.assertEqual(len(parser.idempotent_read_tools), 0)
        # Even a canonical Level-0 tool should NOT be dispatched
        res = parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="empty_set_test")
        self.assertIsNone(res)
        self.assertEqual(len(dispatched), 0)

    def test_streaming_buffer_size_ceiling(self):
        """Verifies that chunk accumulation exceeding MAX_STREAMING_BUFFER_BYTES is discarded safely."""
        parser = StreamingToolCallParser()
        # Feed 65500 bytes (under 64KB limit)
        chunk_1 = "a" * 65500
        res = parser.feed_chunk(chunk_1, call_id="c_overflow")
        self.assertIsNone(res)
        self.assertEqual(len(parser._buffers["c_overflow"]), 65500)

        # Feed another 100 bytes (total 65600 > 65536)
        with self.assertLogs("Jarvis.StreamingToolParser", level="WARNING") as log_capture:
            res2 = parser.feed_chunk("b" * 100, call_id="c_overflow")
            self.assertIsNone(res2)
            self.assertTrue(any("Streaming buffer ceiling exceeded" in msg for msg in log_capture.output))
        # The buffer must not have grown beyond the previous size
        self.assertEqual(len(parser._buffers["c_overflow"]), 65500)

    def test_streaming_buffer_capacity_eviction(self):
        """Verifies that exceeding MAX_ACTIVE_CALL_BUFFERS evicts the oldest incomplete buffer."""
        parser = StreamingToolCallParser()
        # Fill up to 128 distinct call buffers
        for i in range(128):
            parser.feed_chunk('{"name": ', call_id=f"call_{i}")
        self.assertEqual(len(parser._buffers), 128)
        self.assertIn("call_0", parser._buffers)

        # Adding a 129th buffer must evict the oldest key (call_0)
        with self.assertLogs("Jarvis.StreamingToolParser", level="WARNING") as log_capture:
            parser.feed_chunk('{"name": ', call_id="call_128")
            self.assertTrue(any("capacity 128 reached" in msg for msg in log_capture.output))

        self.assertEqual(len(parser._buffers), 128)
        self.assertNotIn("call_0", parser._buffers)
        self.assertIn("call_128", parser._buffers)

    def test_reset_cancels_pending_tasks(self):
        """Verifies that parser.reset() cancels any in-flight speculative tasks."""
        async def run_test():
            task_started = asyncio.Event()

            async def mock_execute(*args, **kwargs):
                task_started.set()
                await asyncio.sleep(5.0)
                return "completed"

            coordinator = DualTrackCoordinator(filler_threshold_ms=10.0)
            coordinator.execute_with_masking = mock_execute

            parser = StreamingToolCallParser(
                idempotent_read_tools={"get_battery_status"},
                coordinator=coordinator,
            )

            task = parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="c_cancel")
            self.assertIsInstance(task, asyncio.Task)
            self.assertIn(task, parser._pending_tasks)

            # Wait until task starts
            await task_started.wait()

            # Call reset()
            parser.reset()
            self.assertEqual(len(parser._pending_tasks), 0)
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.cancelled())

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
