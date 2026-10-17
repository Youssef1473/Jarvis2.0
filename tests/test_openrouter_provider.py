"""Unit tests for OpenRouterProvider adapter."""
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

from jarvis.core.providers.openrouter import (
    OpenRouterProvider,
    OPENROUTER_FREE_MODELS,
    BACKUP_FREE_MODELS,
)
from jarvis.core.providers.base import LLMResponse, ToolCall


class TestOpenRouterProviderInit(unittest.TestCase):
    """Tests initialization and model candidate pool construction."""

    def test_is_available_without_key(self):
        """Provider must report unavailable when no API key is set."""
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
            p = OpenRouterProvider(api_key="")
        self.assertFalse(p.is_available())

    def test_explicit_empty_key_does_not_use_environment_key(self):
        """An explicit empty key must not fall back to a configured environment key."""
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-v1-test"}):
            p = OpenRouterProvider(api_key="")
        self.assertFalse(p.is_available())

    def test_is_available_with_key(self):
        """Provider must report available when API key is provided."""
        with patch("jarvis.core.providers.openrouter.OpenAI"):
            p = OpenRouterProvider(api_key="sk-or-v1-test")
        self.assertTrue(p.is_available())

    def test_candidate_pool_contains_all_six_free_models(self):
        """Candidate pool must include all 6 primary free OpenRouter models."""
        with patch("jarvis.core.providers.openrouter.OpenAI"):
            p = OpenRouterProvider(api_key="sk-or-v1-test")
        for model in OPENROUTER_FREE_MODELS:
            self.assertIn(model, p.candidate_models)

    def test_configured_model_is_first_candidate(self):
        """Configured primary model must be at index 0 in the candidate pool."""
        primary = "cohere/north-mini-code:free"
        with patch("jarvis.core.providers.openrouter.OpenAI"):
            p = OpenRouterProvider(model=primary, api_key="sk-or-v1-test")
        self.assertEqual(p.candidate_models[0], primary)

    def test_switch_model(self):
        """switch_model must update self.model and promote the model to index 0."""
        with patch("jarvis.core.providers.openrouter.OpenAI"):
            p = OpenRouterProvider(api_key="sk-or-v1-test")
        p.switch_model("nvidia/nemotron-3-ultra-550b-a55b:free")
        self.assertEqual(p.model, "nvidia/nemotron-3-ultra-550b-a55b:free")
        self.assertEqual(p.candidate_models[0], "nvidia/nemotron-3-ultra-550b-a55b:free")


class TestOpenRouterProviderGenerate(unittest.TestCase):
    """Tests generate_turn output, tool call accumulation, and 429 failover."""

    def _make_stream_chunk(self, content: Optional[str] = None, tool_calls=None):
        """Helper to create a mock stream chunk."""
        chunk = MagicMock()
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = tool_calls or []
        choice = MagicMock()
        choice.delta = delta
        chunk.choices = [choice]
        return chunk

    def test_generate_turn_returns_error_without_client(self):
        """generate_turn must return a clear error string when no client is configured."""
        p = OpenRouterProvider(api_key="")
        result = p.generate_turn(messages=[{"role": "user", "content": "Hello"}])
        self.assertIn("OPENROUTER_API_KEY", result.content)
        self.assertEqual(result.tool_calls, [])

    def test_generate_turn_streams_content(self):
        """generate_turn must accumulate streamed text chunks into content."""
        chunks = [
            self._make_stream_chunk("Hello"),
            self._make_stream_chunk(", Sir."),
            self._make_stream_chunk(None),
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)

        with patch("jarvis.core.providers.openrouter.OpenAI", return_value=mock_client):
            p = OpenRouterProvider(api_key="sk-or-v1-test")
            p.client = mock_client

        result = p.generate_turn(messages=[{"role": "user", "content": "Hello"}])
        self.assertIn("Hello, Sir.", result.content)
        self.assertEqual(result.tool_calls, [])

    def test_generate_turn_accumulates_tool_calls(self):
        """generate_turn must correctly accumulate streamed tool call deltas."""
        tc_delta_1 = MagicMock()
        tc_delta_1.index = 0
        tc_delta_1.id = "call-abc"
        tc_delta_1.function = MagicMock()
        tc_delta_1.function.name = "adjust_volume"
        tc_delta_1.function.arguments = '{"percent":'

        tc_delta_2 = MagicMock()
        tc_delta_2.index = 0
        tc_delta_2.id = None
        tc_delta_2.function = MagicMock()
        tc_delta_2.function.name = None
        tc_delta_2.function.arguments = "50}"

        chunk1 = self._make_stream_chunk(tool_calls=[tc_delta_1])
        chunk2 = self._make_stream_chunk(tool_calls=[tc_delta_2])

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter([chunk1, chunk2])

        with patch("jarvis.core.providers.openrouter.OpenAI", return_value=mock_client):
            p = OpenRouterProvider(api_key="sk-or-v1-test")
            p.client = mock_client

        result = p.generate_turn(
            messages=[{"role": "user", "content": "Set volume to 50"}],
            tools=[{"type": "function", "function": {"name": "adjust_volume", "description": "Adjust volume", "parameters": {"type": "object", "properties": {"percent": {"type": "integer"}}, "required": ["percent"]}}}],
        )
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "adjust_volume")
        self.assertEqual(result.tool_calls[0].arguments, {"percent": 50})

    def test_generate_turn_429_failover(self):
        """generate_turn must cascade to the next candidate on 429 rate-limit error."""
        from openai import APIStatusError
        import httpx

        mock_client = MagicMock()
        # First call raises a 429-like error; second call returns a valid stream
        good_chunks = [self._make_stream_chunk("Fallback reply")]
        mock_client.chat.completions.create.side_effect = [
            Exception("429 rate limit exceeded"),
            iter(good_chunks),
        ]

        with patch("jarvis.core.providers.openrouter.OpenAI", return_value=mock_client):
            p = OpenRouterProvider(
                api_key="sk-or-v1-test",
                candidate_models=["model-a:free", "model-b:free"],
            )
            p.client = mock_client

        result = p.generate_turn(messages=[{"role": "user", "content": "Hello"}])
        self.assertIn("Fallback reply", result.content)
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    def test_generate_turn_all_candidates_exhausted(self):
        """generate_turn must return a clean error string when all candidates fail."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("429 rate limit exceeded")

        with patch("jarvis.core.providers.openrouter.OpenAI", return_value=mock_client):
            p = OpenRouterProvider(
                api_key="sk-or-v1-test",
                candidate_models=["model-a:free"],
            )
            p.client = mock_client

        result = p.generate_turn(messages=[{"role": "user", "content": "Hello"}])
        self.assertIn("could not complete", result.content)

    def test_sentence_callback_receives_streamed_text(self):
        """Sentence callback must be called with streamed text chunks during generation."""
        chunks = [
            self._make_stream_chunk("Hello, Sir. "),
            self._make_stream_chunk("System online."),
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)

        with patch("jarvis.core.providers.openrouter.OpenAI", return_value=mock_client):
            p = OpenRouterProvider(api_key="sk-or-v1-test")
            p.client = mock_client

        received: List[str] = []
        p.generate_turn(
            messages=[{"role": "user", "content": "Hello"}],
            sentence_callback=received.append,
        )
        combined = "".join(received)
        self.assertIn("Sir", combined)


class TestOpenRouterModelConstants(unittest.TestCase):
    """Validates the free model constant declarations."""

    def test_six_primary_free_models_defined(self):
        """OPENROUTER_FREE_MODELS must contain exactly 6 entries."""
        self.assertEqual(len(OPENROUTER_FREE_MODELS), 6)

    def test_all_primary_models_have_free_suffix(self):
        """Every configured primary model must be an explicitly free endpoint."""
        for model in OPENROUTER_FREE_MODELS:
            self.assertTrue(
                model.endswith(":free"),
                f"Expected ':free' suffix on model '{model}'"
            )

    def test_backup_free_models_not_empty(self):
        """BACKUP_FREE_MODELS must contain at least one fallback model."""
        self.assertGreater(len(BACKUP_FREE_MODELS), 0)


if __name__ == "__main__":
    unittest.main()
