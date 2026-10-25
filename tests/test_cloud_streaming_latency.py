"""Unit tests for ultra-low latency cloud reasoning and streaming speech pipelines."""
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.skills.base import SkillExecutionResult, SkillRegistry


class TestCloudStreamingLatency(unittest.TestCase):
    """Verifies real-time token streaming, dynamic thinking budgets, and early speech pipelining."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "memory.db"},
            "llm": {"provider": "gemini"},
        }
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate()
        self.brain = JarvisBrain(self.config, self.memory, self.safety)
        # Ensure gemini chat session state matches active brain attributes so mock_chat is not overwritten
        self.brain.gemini_model = self.brain.gemini_candidates[self.brain.gemini_model_idx]
        self.brain.gemini_task_mode = self.brain.active_task_mode
        self.brain.gemini_tool_names = self.brain.active_tool_names
        self.brain.gemini_client = MagicMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_gemini_streaming_incremental_clause_dispatch(self):
        """Verifies that chunks from send_message_stream are dispatched to sentence_callback incrementally."""
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        # Simulate streaming chunks arriving sequentially
        chunk1 = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part.from_text(text="Right away, Sir, ")]))]
        )
        chunk2 = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part.from_text(text="I am monitoring your systems now.")]))]
        )
        mock_chat.send_message_stream.return_value = [chunk1, chunk2]

        spoken_chunks = []

        def callback(text: str):
            spoken_chunks.append(text)

        reply = self.brain._process_gemini_query("check my systems", sentence_callback=callback)

        # Verify chat received stream call
        mock_chat.send_message_stream.assert_called_once()
        # Verify first clause was dispatched immediately to speech callback
        self.assertGreaterEqual(len(spoken_chunks), 1)
        self.assertTrue(any("Right away" in c for c in spoken_chunks))
        self.assertIn("monitoring your systems", reply)

    def test_dynamic_thinking_budget_clamping(self):
        """Verifies voice turns get thinking_budget=0, while deep reasoning turns get unconstrained thinking."""
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        dummy_chunk = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part.from_text(text="All clear, Sir.")]))]
        )
        mock_chat.send_message_stream.return_value = [dummy_chunk]

        # 1. Voice / Routine Turn -> thinking_budget clamped to 0
        self.brain._process_gemini_query("what is the weather today")
        _args, kwargs = mock_chat.send_message_stream.call_args
        call_cfg = kwargs.get("config")
        self.assertIsNotNone(call_cfg)
        self.assertEqual(call_cfg.thinking_config.thinking_budget, 0)

        # 2. Deep Analytical / Code Turn -> thinking_budget unconstrained (None)
        mock_chat.send_message_stream.reset_mock()
        mock_chat.send_message_stream.return_value = [dummy_chunk]
        self.brain._process_gemini_query("explain in detail why quantum entanglement does not violate relativity")
        _args_deep, kwargs_deep = mock_chat.send_message_stream.call_args
        call_cfg_deep = kwargs_deep.get("config")
        self.assertIsNone(call_cfg_deep)

    def test_gemini_streaming_with_tool_call_atomicity(self):
        """Verifies that function calls in stream are executed atomically before emitting follow-up text."""
        mock_chat = MagicMock()
        self.brain.gemini_chat = mock_chat

        # Stream 1 yields a tool call
        fn_chunk = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[
                types.Part.from_function_call(name="get_battery_status", args={})
            ]))]
        )
        # Stream 2 yields text acknowledgment
        text_chunk = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[
                types.Part.from_text(text="Your battery is at 92%, Sir.")
            ]))]
        )

        mock_chat.send_message_stream.side_effect = [
            [fn_chunk],
            [text_chunk],
        ]

        spoken_clauses = []
        with (
            patch.object(self.brain.safety, "check_tool_call", return_value=(True, "")) as mock_safety,
            patch.object(
                SkillRegistry,
                "execute_with_status",
                return_value=SkillExecutionResult(result="Battery: 92%", success=True),
            ) as mock_exec_status,
        ):
            reply = self.brain._process_gemini_query("check battery status", sentence_callback=lambda x: spoken_clauses.append(x))

            mock_safety.assert_called_with("get_battery_status", {})
            mock_exec_status.assert_called_with("get_battery_status")
            self.assertIn("92%", reply)

    def test_antigravity_bridge_early_clause_extraction(self):
        """Tests that natural clause breaks emit early speech chunks."""
        buffer = "Certainly, Sir, I will bring up that window immediately."
        chunk, remaining = AntigravityBridge._extract_speech_chunk(buffer)

        self.assertIsNotNone(chunk)
        self.assertEqual(chunk, "Certainly,")
        self.assertIn("Sir, I will bring up", remaining)


if __name__ == "__main__":
    unittest.main()
