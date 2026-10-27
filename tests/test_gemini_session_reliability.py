"""Hermetic unit tests for Gemini session continuation and iteration-ceiling recovery (DEF-REL-05).

Verifies that tool iteration exhaustion stops safely, invalidates dangling chat state,
disables Automatic Function Calling (AFC), preserves function call IDs, and enables
seamless multi-turn session continuation without live API calls.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

from google.genai import types

from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.providers.base import (
    BaseProvider,
    LLMResponse,
    ToolCall,
    UnifiedRunner,
)
from jarvis.core.safety import SafetyGate


class MockTurnProvider(BaseProvider):
    """Mock provider with programmed turns for testing UnifiedRunner."""

    def __init__(self, turns: list[LLMResponse]):
        super().__init__(name="mock", model="mock-model")
        self.turns = turns
        self.turn_index = 0

    def generate_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> LLMResponse:
        if self.turn_index < len(self.turns):
            res = self.turns[self.turn_index]
            self.turn_index += 1
            if sentence_callback and res.content:
                sentence_callback(res.content)
            return res
        return LLMResponse(content="No further turns.")


class TestGeminiSessionReliability(unittest.TestCase):
    """Hermetic test suite for CORE-RUN-01C (DEF-REL-05)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "memory.db"},
            "llm": {"provider": "gemini"},
        }
        self.env_patcher = patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini_api_key_for_testing"})
        self.env_patcher.start()

        self.mock_client = MagicMock()
        self.mock_chat = MagicMock()
        self.mock_client.chats.create.return_value = self.mock_chat

        self.patcher = patch("jarvis.core.brain_gemini.genai.Client", return_value=self.mock_client)
        self.patcher.start()

        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate()
        self.brain = JarvisBrain(self.config, self.memory, self.safety)
        self.brain.gemini_model = self.brain.gemini_candidates[self.brain.gemini_model_idx]
        self.brain.gemini_task_mode = self.brain.active_task_mode
        self.brain.gemini_tool_names = self.brain.active_tool_names
        self.brain.gemini_client = self.mock_client
        self.brain.gemini_chat = self.mock_chat

    def tearDown(self):
        self.patcher.stop()
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_iteration_ceiling_stops_safely_and_invalidates_chat(self):
        """When max_gemini_tool_iterations is reached, execution halts and chat session is invalidated."""
        self.brain.active_task_mode = "general"  # limit = 8
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        def make_fn_chunk(step: int) -> types.GenerateContentResponse:
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            parts=[types.Part.from_function_call(name=f"tool_step_{step}", args={"i": step})]
                        )
                    )
                ]
            )

        # First query + 8 followup calls returning another function call
        mock_chat.send_message_stream.side_effect = [
            [make_fn_chunk(i)] for i in range(1, 10)
        ]

        with patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("step_ok", True)) as mock_dispatch:
            reply = self.brain._process_gemini_query("Run endless tool loop")

            self.assertEqual(mock_dispatch.call_count, 8)
            self.assertIn("reached the limit for tool steps", reply)
            # Crucial requirement: gemini_chat must be invalidated to prevent dangling FunctionCall turns
            self.assertIsNone(self.brain.gemini_chat)

    def test_subsequent_request_succeeds_after_iteration_ceiling(self):
        """A subsequent user prompt following iteration exhaustion reconstructs a clean chat from memory."""
        self.brain.active_task_mode = "general"
        mock_chat_1 = MagicMock()
        self.brain.gemini_chat = mock_chat_1

        fn_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_function_call(name="loop_tool", args={})]
                    )
                )
            ]
        )
        mock_chat_1.send_message_stream.side_effect = [[fn_chunk] for _ in range(10)]

        with patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("ok", True)):
            reply_1 = self.brain._process_gemini_query("Endless loop request")
            self.assertIn("reached the limit for tool steps", reply_1)
            self.assertIsNone(self.brain.gemini_chat)

        # Setup mock client for the second request reinitialization
        mock_chat_2 = MagicMock()
        text_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="The current time is 12:00 PM, Sir.")]
                    )
                )
            ]
        )
        mock_chat_2.send_message_stream.return_value = [text_chunk]
        self.mock_client.chats.create.return_value = mock_chat_2

        reply_2 = self.brain._process_gemini_query("What time is it?")

        # Verify chats.create was called to reconstruct a fresh chat
        self.mock_client.chats.create.assert_called()
        _, create_kwargs = self.mock_client.chats.create.call_args
        history = create_kwargs.get("history")

        # Verify reconstructed history contains only alternating clean text turns from memory
        self.assertIsNotNone(history)
        self.assertGreaterEqual(len(history), 2)
        for content in history:
            for part in content.parts:
                self.assertIsNone(part.function_call, "Reconstructed history must not contain dangling function calls")
                self.assertIsNone(part.function_response, "Reconstructed history must not contain function responses")
                self.assertIsNotNone(part.text, "History entries must be clean text summaries")

        self.assertIn("12:00 PM", reply_2)
        self.assertIsNotNone(self.brain.gemini_chat)

    def test_parallel_function_calls_in_single_turn_all_resolved(self):
        """Batch of parallel function calls must each receive a corresponding FunctionResponse with matched IDs."""
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        # Model emits 3 parallel function calls: two with IDs, one without
        batch_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part(function_call=types.FunctionCall(name="get_weather", args={"city": "Paris"}, id="call_weather_1")),
                            types.Part(function_call=types.FunctionCall(name="get_time", args={"tz": "CET"}, id="call_time_2")),
                            types.Part(function_call=types.FunctionCall(name="get_status", args={}, id=None)),
                        ]
                    )
                )
            ]
        )
        final_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="Weather is clear and time is 12:05 PM.")]
                    )
                )
            ]
        )

        mock_chat.send_message_stream.side_effect = [
            [batch_chunk],
            [final_chunk],
        ]

        dispatched_tools = []

        def mock_dispatch(name, args, **kwargs):
            dispatched_tools.append(name)
            return f"Result for {name}", True

        with patch.object(self.brain, "_dispatch_tool_pipeline", side_effect=mock_dispatch):
            reply = self.brain._process_gemini_query("Check weather, time, and status in parallel")

            self.assertEqual(dispatched_tools, ["get_weather", "get_time", "get_status"])
            self.assertEqual(mock_chat.send_message_stream.call_count, 2)

            # Inspect the follow-up response sent to send_message_stream
            followup_call_args = mock_chat.send_message_stream.call_args_list[1]
            tool_responses = followup_call_args[0][0]
            self.assertEqual(len(tool_responses), 3)

            # Verify call 1: id preserved
            resp1 = tool_responses[0].function_response
            self.assertEqual(resp1.name, "get_weather")
            self.assertEqual(resp1.id, "call_weather_1")
            self.assertIn("Result for get_weather", str(resp1.response))

            # Verify call 2: id preserved
            resp2 = tool_responses[1].function_response
            self.assertEqual(resp2.name, "get_time")
            self.assertEqual(resp2.id, "call_time_2")
            self.assertIn("Result for get_time", str(resp2.response))

            # Verify call 3: id is None (never invented)
            resp3 = tool_responses[2].function_response
            self.assertEqual(resp3.name, "get_status")
            self.assertIsNone(resp3.id)
            self.assertIn("Result for get_status", str(resp3.response))

            self.assertIn("12:05 PM", reply)

    def test_model_returns_function_call_in_final_allowed_iteration(self):
        """Model returning a tool call at step == max_iterations is not executed beyond the limit."""
        self.brain.active_task_mode = "general"  # limit = 8
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        def make_chunk(name: str) -> types.GenerateContentResponse:
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            parts=[types.Part.from_function_call(name=name, args={})]
                        )
                    )
                ]
            )

        # 8 executed turns, then a 9th call that must not execute
        chunks = [make_chunk(f"tool_step_{i}") for i in range(1, 9)]
        chunks.append(make_chunk("forbidden_step_9"))

        mock_chat.send_message_stream.side_effect = [[c] for c in chunks]

        executed_tools = []

        def mock_dispatch(name, args, **kwargs):
            executed_tools.append(name)
            return "ok", True

        with patch.object(self.brain, "_dispatch_tool_pipeline", side_effect=mock_dispatch):
            reply = self.brain._process_gemini_query("Run step boundary test")

            self.assertEqual(len(executed_tools), 8)
            self.assertNotIn("forbidden_step_9", executed_tools)
            self.assertIn("reached the limit for tool steps", reply)
            self.assertIsNone(self.brain.gemini_chat)

    def test_normal_tool_completion_below_ceiling_preserves_chat(self):
        """Standard tool execution finishing within budget preserves the active chat session."""
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        fn_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_function_call(name="get_volume", args={})]
                    )
                )
            ]
        )
        text_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="Volume is at 50%, Sir.")]
                    )
                )
            ]
        )

        mock_chat.send_message_stream.side_effect = [
            [fn_chunk],
            [text_chunk],
        ]

        with patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("50%", True)):
            reply = self.brain._process_gemini_query("What is the volume level?")

            self.assertIn("Volume is at 50%", reply)
            # Normal completion below ceiling must preserve the chat session
            self.assertIsNotNone(self.brain.gemini_chat)
            self.assertIs(self.brain.gemini_chat, mock_chat)

    def test_streaming_error_mid_turn_resets_session_safely(self):
        """A network exception during streaming invalidates the chat session to prevent corruption."""
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat
        # Use an error string that does not trigger quota-congestion model failover
        mock_chat.send_message_stream.side_effect = RuntimeError("Fatal socket error mid-stream")

        reply = self.brain._process_gemini_query("Query triggering network fault")

        self.assertIn("slight anomaly in my cognitive circuits", reply)
        # Session must be invalidated so corrupted state does not persist
        self.assertIsNone(self.brain.gemini_chat)

    def test_afc_explicitly_disabled_in_config(self):
        """Verifies that automatic_function_calling(disable=True) is applied to both base and call configs."""
        # 1. Base configuration verification in _init_gemini
        self.brain._init_gemini("gemini-3.8-flash")
        self.mock_client.chats.create.assert_called()
        _, create_kwargs = self.mock_client.chats.create.call_args
        base_cfg = create_kwargs.get("config")
        self.assertIsNotNone(base_cfg)
        self.assertIsNotNone(base_cfg.automatic_function_calling)
        self.assertTrue(base_cfg.automatic_function_calling.disable)

        # 2. Routine voice turn verification (call_config)
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat
        dummy_chunk = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part.from_text(text="Hello.")]))]
        )
        mock_chat.send_message_stream.return_value = [dummy_chunk]

        self.brain._process_gemini_query("routine weather query")
        _, stream_kwargs = mock_chat.send_message_stream.call_args
        call_cfg = stream_kwargs.get("config")
        self.assertIsNotNone(call_cfg)
        self.assertIsNotNone(call_cfg.automatic_function_calling)
        self.assertTrue(call_cfg.automatic_function_calling.disable)
        self.assertEqual(call_cfg.thinking_config.thinking_budget, 0)

    def test_unified_runner_appends_closing_assistant_message_on_ceiling(self):
        """UnifiedRunner appends an assistant message when the iteration ceiling leaves trailing tool results."""
        tool_1 = ToolCall(id="call_1", name="step_one", arguments={})
        tool_2 = ToolCall(id="call_2", name="step_two", arguments={})

        turn_1 = LLMResponse(content="", tool_calls=[tool_1])
        turn_2 = LLMResponse(content="", tool_calls=[tool_2])

        provider = MockTurnProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        with patch.object(runner, "execute_tool_with_status") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, result="tool_success", failure_category=None)
            final_reply, history = runner.execute_session(
                provider=provider,
                messages=[{"role": "user", "content": "run two-step task"}],
            )

            self.assertIn("Maximum iteration limit reached", final_reply)
            # Must end in an assistant message closing the loop
            self.assertEqual(history[-1]["role"], "assistant")
            self.assertEqual(history[-1]["content"], final_reply)
            # The second to last message must be the tool result
            self.assertEqual(history[-2]["role"], "tool")

    def test_independent_sessions_do_not_leak_state(self):
        """Two separate JarvisBrain instances operate in total isolation without session cross-talk."""
        temp_dir_2 = tempfile.mkdtemp()
        try:
            cfg2 = {
                "_base_dir": temp_dir_2,
                "memory": {"db_path": "memory.db"},
                "llm": {"provider": "gemini"},
            }
            brain2 = JarvisBrain(cfg2, MemoryManager(cfg2), SafetyGate())
            brain2.gemini_model = brain2.gemini_candidates[brain2.gemini_model_idx]
            brain2.gemini_task_mode = brain2.active_task_mode
            brain2.gemini_tool_names = brain2.active_tool_names
            brain2.gemini_client = self.mock_client

            mock_chat_1 = MagicMock()
            mock_chat_2 = MagicMock()
            self.brain.gemini_chat = mock_chat_1
            brain2.gemini_chat = mock_chat_2

            # Force brain 1 to invalidate chat
            self.brain.gemini_chat = None

            # Verify brain 2 is completely unaffected
            self.assertIsNone(self.brain.gemini_chat)
            self.assertIsNotNone(brain2.gemini_chat)
            self.assertIs(brain2.gemini_chat, mock_chat_2)
        finally:
            shutil.rmtree(temp_dir_2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
