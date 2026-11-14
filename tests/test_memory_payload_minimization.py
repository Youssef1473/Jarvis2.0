"""Hermetic unit tests verifying strict memory payload minimization and Gemini envelope construction.

Asserts:
- ORCHID-47 synthetic test phrase is included in outgoing envelope.
- Unrelated HUD memory errors are excluded.
- Unrelated test-suite status records are excluded.
- Unrelated test-runner delegation lessons are excluded.
- Unrelated episodic lessons are excluded for informational/conversational queries.
- No personal user credentials or identity records are transmitted.
- Gemini request receives the exact filtered context envelope.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarvis.context.context_orchestrator import ContextOrchestrator
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.core.security_boundaries import TurnTaintTracker


class TestMemoryPayloadMinimization(unittest.TestCase):
    """Verifies strict scoping and privacy boundaries of the Gemini prompt envelope."""

    def setUp(self) -> None:
        TurnTaintTracker.get_instance().reset_turn()
        self.memory = MemoryManager({})
        self.safety = SafetyGate({})
        # Seed test fact in memory
        self.memory.remember_fact("memory_test_phrase", "ORCHID-47", category="general")
        self.brain = JarvisBrain({"llm": {"provider": "gemini"}}, self.memory, self.safety)

    def tearDown(self) -> None:
        TurnTaintTracker.get_instance().reset_turn()

    def test_synthetic_memory_phrase_recalled_strictly(self) -> None:
        """Verifies that only the target fact is recalled for 'What is my memory test phrase?'."""
        query = "What is my memory test phrase?"
        envelope = self.brain.build_ephemeral_user_envelope(query)

        # 1. Target synthetic fact MUST be included
        self.assertIn("Memory test phrase: ORCHID-47", envelope)
        self.assertIn("<jit_recalled_memory>", envelope)
        self.assertIn("What is my memory test phrase?", envelope)

        # 2. Unrelated HUD memory errors MUST be excluded
        self.assertNotIn("hud_memory_node_query_error", envelope)
        self.assertNotIn("Unexpected token N", envelope)
        self.assertNotIn("404 text response", envelope)

        # 3. Unrelated test-suite status MUST be excluded
        self.assertNotIn("test_suite_status", envelope)
        self.assertNotIn("417 unit tests", envelope)

        # 4. Unrelated test-runner instructions MUST be excluded
        self.assertNotIn("test_runner_delegation", envelope)
        self.assertNotIn("option to code tests personally", envelope)

        # 5. Unrelated episodic lessons MUST be excluded
        self.assertNotIn("<episodic_lessons>", envelope)
        self.assertNotIn("test_tool", envelope)
        self.assertNotIn("create_or_update_custom_skill", envelope)

        # 6. Unrelated personal identity facts MUST be excluded
        self.assertNotIn("Bardo", envelope)
        self.assertNotIn("Ras Tabia", envelope)
        self.assertNotIn("ESPRIT", envelope)
        self.assertNotIn("Chamroukhi", envelope)

        # 7. Total envelope size must be strictly compact (<500 bytes)
        self.assertLess(len(envelope.encode("utf-8")), 500)

    def test_gemini_request_receives_minimized_envelope(self) -> None:
        """Verifies that the Gemini send_message_stream request actually receives the minimized envelope."""
        query = "What is my memory test phrase?"

        # Mock gemini_chat.send_message_stream
        mock_chat = MagicMock()
        mock_stream = MagicMock()
        mock_part = MagicMock()
        mock_part.text = "Your memory test phrase is ORCHID-47, Sir."
        mock_part.thought = False
        mock_cand = MagicMock()
        mock_cand.content.parts = [mock_part]
        mock_chunk = MagicMock()
        mock_chunk.candidates = [mock_cand]
        mock_chunk.function_calls = None
        mock_stream.__iter__.return_value = [mock_chunk]
        mock_chat.send_message_stream.return_value = mock_stream

        self.brain.gemini_client = MagicMock()
        self.brain.gemini_chat = mock_chat
        self.brain.gemini_candidates = ["gemini-3.8-flash"]
        self.brain.gemini_model_idx = 0
        self.brain.gemini_model = "gemini-3.8-flash"

        reply = self.brain._process_gemini_query(query)

        self.assertEqual(reply, "Your memory test phrase is ORCHID-47, Sir.")
        self.assertTrue(mock_chat.send_message_stream.called)

        # Inspect the exact serialized contents passed to Gemini
        call_args, _ = mock_chat.send_message_stream.call_args
        sent_contents = call_args[0]
        self.assertIsInstance(sent_contents, list)
        self.assertTrue(len(sent_contents) >= 1)

        sent_payload = sent_contents[0]
        # Must contain target synthetic fact
        self.assertIn("Memory test phrase: ORCHID-47", sent_payload)
        # Must NOT contain unrelated noise
        self.assertNotIn("hud_memory_node_query_error", sent_payload)
        self.assertNotIn("test_suite_status", sent_payload)
        self.assertNotIn("<episodic_lessons>", sent_payload)
        self.assertNotIn("Bardo", sent_payload)


if __name__ == "__main__":
    unittest.main()
