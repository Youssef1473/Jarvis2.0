"""Automated tests for speculative streaming tool call parser and dual-track acoustic masking."""
import asyncio
import json
import unittest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.streaming_tool_parser import StreamingToolCallParser
from jarvis.core.pipeline.dual_track import DualTrackCoordinator


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


if __name__ == "__main__":
    unittest.main()
