import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.audio.ring_buffer import AudioRingBuffer
from jarvis.core.providers.ollama import OllamaProvider


class TestAudioRingBufferCapacity(unittest.TestCase):
    def test_rejects_nonpositive_capacity(self):
        for capacity in (0, -1):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    AudioRingBuffer(capacity_samples=capacity)

    def test_rejects_noninteger_capacity(self):
        with self.assertRaises(ValueError):
            AudioRingBuffer(capacity_samples=1.5)


class TestOllamaProviderHistory(unittest.TestCase):
    def test_preserves_assistant_tool_calls_and_tool_result(self):
        provider = OllamaProvider()
        provider.client.chat.completions.create = Mock(return_value=[])
        history = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "12:00"},
        ]

        provider.generate_turn(history)

        sent_messages = provider.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(sent_messages[0]["tool_calls"], history[0]["tool_calls"])
        self.assertEqual(sent_messages[1]["tool_call_id"], "call-1")
        self.assertEqual(sent_messages[1]["content"], "12:00")

    def test_streamed_text_uses_shared_sentence_chunker(self):
        provider = OllamaProvider()
        deltas = ["The value is 3.14. ", "Dr. Smith agrees."]
        stream = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=part, tool_calls=None))]
            )
            for part in deltas
        ]
        provider.client.chat.completions.create = Mock(return_value=stream)

        actual = []
        provider.generate_turn(
            [{"role": "user", "content": "Summarize."}],
            sentence_callback=actual.append,
        )

        expected_chunker = StreamingSentenceChunker()
        expected = []
        for part in deltas:
            expected.extend(expected_chunker.feed(part))
        expected.extend(expected_chunker.flush())
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
