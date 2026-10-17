"""Automated tests for speculative streaming tool call parser."""
import unittest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    unittest.main()
