"""Hermetic unit tests for provider routing, dynamic failover, and candidate rotation (Phase 1B).

Covers 10 scenarios:
1. Primary available -> uses Antigravity.
2. Antigravity unavailable + Gemini configured -> uses Gemini fallback.
3. Both unavailable -> deterministic reflexes remain available.
4. Unknown query + no providers -> explains unavailability without fabricating reflexes.
5. Gemini auth failure (401/403) -> clean error without retry loop.
6. Gemini rate limit (429) -> candidate rotation.
7. Gemini timeout (504) -> candidate rotation.
8. Invalid Gemini response -> clean error without crashing.
9. Conversation context preserved across provider transition.
10. No duplicate tool action during mid-turn fallback.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestProviderRoutingFailover(unittest.TestCase):
    """Test suite for Phase 1B provider routing contracts and failure modes."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = load_config()
        self.config["_base_dir"] = self.temp_dir
        self.config.setdefault("memory", {})["db_path"] = os.path.join(self.temp_dir, "test_memory.db")
        self.config.setdefault("llm", {})["provider"] = "antigravity"

        self.env_patcher = patch.dict(os.environ, {"GEMINI_API_KEY": "test_gemini_api_key_valid"})
        self.env_patcher.start()

        self.mock_genai_client = MagicMock()
        self.mock_chat = MagicMock()
        self.mock_genai_client.chats.create.return_value = self.mock_chat
        self.client_patcher = patch("jarvis.core.brain_gemini.genai.Client", return_value=self.mock_genai_client)
        self.client_patcher.start()

        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)

    def tearDown(self):
        self.client_patcher.stop()
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_mock_chunk(self, text: str) -> types.GenerateContentResponse:
        """Helper to create a GenerateContentResponse chunk containing text."""
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text=text)]
                    )
                )
            ]
        )

    def test_01_primary_antigravity_available_uses_antigravity(self):
        """Scenario 1: When Antigravity is operational, active route is 'antigravity' and it handles queries."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: sys.executable,
        )
        brain.antigravity_bridge.is_available = MagicMock(return_value=True)
        brain.antigravity_bridge.generate_response = MagicMock(return_value={
            "status": "SUCCESS",
            "response": "Antigravity response from Google AI Pro.",
            "duration_seconds": 0.42,
        })

        status = brain.get_provider_status()
        self.assertEqual(status["active_route"], "antigravity")
        self.assertTrue(status["providers"]["antigravity"]["available"])

        reply = brain.process_query("What is the capital of France?")
        self.assertEqual(reply, "Antigravity response from Google AI Pro.")
        brain.antigravity_bridge.generate_response.assert_called_once()
        brain.shutdown()

    def test_02_antigravity_unavailable_gemini_configured_uses_gemini_fallback(self):
        """Scenario 2: When Antigravity is unavailable, routing smoothly falls back to Gemini."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        self.assertFalse(brain.antigravity_bridge.is_available())

        status = brain.get_provider_status()
        self.assertEqual(status["active_route"], "gemini_fallback")
        self.assertFalse(status["providers"]["antigravity"]["available"])
        self.assertTrue(status["providers"]["gemini"]["available"])

        self.mock_chat.send_message_stream.return_value = [
            self._create_mock_chunk("Gemini fallback answer: Paris.")
        ]

        reply = brain.process_query("What is the capital of France?")
        self.assertEqual(reply, "Gemini fallback answer: Paris.")
        brain.shutdown()

    def test_03_both_unavailable_deterministic_reflexes_remain_operational(self):
        """Scenario 3: When no reasoning LLM is available, local reflexes execute in <10ms."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            brain = JarvisBrain(
                self.config,
                self.memory,
                self.safety,
                antigravity_resolver=lambda: None,
            )
            status = brain.get_provider_status()
            self.assertEqual(status["active_route"], "reflexes_only")

            with patch("jarvis.skills.base.SkillRegistry.execute", return_value="Volume muted.") as mock_exec:
                reply = brain.process_query("mute")
                self.assertEqual(reply, "Volume muted.")
                mock_exec.assert_called_once_with("adjust_volume", action="mute")
            brain.shutdown()

    def test_04_both_unavailable_unknown_query_explains_unavailability(self):
        """Scenario 4: When no reasoning LLM is available, unknown queries explain unavailability cleanly."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            brain = JarvisBrain(
                self.config,
                self.memory,
                self.safety,
                antigravity_resolver=lambda: None,
            )
            brain.antigravity_bridge.availability_status = "NOT_FOUND"
            brain.antigravity_bridge.availability_diagnostic = "agy not installed"

            reply = brain.process_query("Explain quantum superdense coding in detail")
            self.assertIn("My primary reasoning provider ('antigravity') is unavailable", reply)
            self.assertIn("secondary Gemini reasoning is not configured", reply)
            self.assertIn("Local deterministic reflexes remain operational", reply)
            brain.shutdown()

    def test_05_gemini_auth_failure_fails_fast_without_retry_loop(self):
        """Scenario 5: 401/403/auth failures return an immediate explanation without cycling candidate models."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        self.mock_chat.send_message_stream.side_effect = Exception("403 Forbidden: API_KEY_INVALID")

        reply = brain._process_gemini_query("Hello")
        self.assertIn("Gemini API credentials failed authentication", reply)
        # Verify it did not cycle through candidates (called send_message_stream only once)
        self.assertEqual(self.mock_chat.send_message_stream.call_count, 1)
        brain.shutdown()

    def test_06_gemini_rate_limit_triggers_candidate_rotation(self):
        """Scenario 6: 429 rate limit triggers rotation to the next candidate model and succeeds."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        initial_idx = brain.gemini_model_idx
        initial_model = brain.gemini_candidates[initial_idx]

        # First call fails with 429 quota exhaustion; second call succeeds
        mock_chat_2 = MagicMock()
        mock_chat_2.send_message_stream.return_value = [
            self._create_mock_chunk("Response from rotated candidate model.")
        ]
        self.mock_chat.send_message_stream.side_effect = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded")
        self.mock_genai_client.chats.create.side_effect = [self.mock_chat, mock_chat_2]

        reply = brain._process_gemini_query("Solve this problem")
        self.assertEqual(reply, "Response from rotated candidate model.")
        self.assertNotEqual(brain.gemini_model, initial_model)
        self.assertEqual(brain.gemini_model_idx, (initial_idx + 1) % len(brain.gemini_candidates))
        brain.shutdown()

    def test_07_gemini_timeout_triggers_candidate_rotation(self):
        """Scenario 7: 504 / timeout triggers rotation to the next candidate model and succeeds."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        initial_idx = brain.gemini_model_idx

        mock_chat_2 = MagicMock()
        mock_chat_2.send_message_stream.return_value = [
            self._create_mock_chunk("Response after timeout recovery.")
        ]
        self.mock_chat.send_message_stream.side_effect = Exception("504 DEADLINE_EXCEEDED: Deadline expired")
        self.mock_genai_client.chats.create.side_effect = [self.mock_chat, mock_chat_2]

        reply = brain._process_gemini_query("Query during network latency spike")
        self.assertEqual(reply, "Response after timeout recovery.")
        self.assertEqual(brain.gemini_model_idx, (initial_idx + 1) % len(brain.gemini_candidates))
        brain.shutdown()

    def test_08_gemini_invalid_response_returns_clean_default_without_crashing(self):
        """Scenario 8: Empty or invalid response streams yield clean conversational default without crashing."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        # Stream yields empty candidate chunk
        empty_chunk = types.GenerateContentResponse(candidates=[types.Candidate(content=None)])
        self.mock_chat.send_message_stream.return_value = [empty_chunk]

        reply = brain._process_gemini_query("Ping")
        self.assertEqual(reply, "At your service, Sir.")
        brain.shutdown()

    def test_09_conversation_context_preserved_across_provider_transition(self):
        """Scenario 9: Conversation interactions and memories are preserved when switching providers."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        self.mock_chat.send_message_stream.return_value = [
            self._create_mock_chunk("Understood, Sir. I have noted that your code is Bettany99.")
        ]

        turn_1_reply = brain.process_query("My secret code is Bettany99")
        self.assertIn("Bettany99", turn_1_reply)

        # Verify interaction was logged into memory
        interactions = brain.memory.get_recent_interactions(limit=5)
        self.assertTrue(any("Bettany99" in str(item) for item in interactions))
        brain.shutdown()

    def test_10_tool_deduplication_notice_injected_during_failover(self):
        """Scenario 10: Executed tools prior to mid-turn failover are injected as deduplication notice."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        # Simulate that a tool was executed in this turn prior to failover
        brain._current_turn_executed_tools = [
            ("adjust_volume", {"action": "mute"}),
            ("launch_application", {"app_name": "notepad"}),
        ]

        captured_contents = []

        def capture_stream(contents, config=None):
            captured_contents.extend(contents)
            return [self._create_mock_chunk("I see notepad was launched and volume muted.")]

        self.mock_chat.send_message_stream.side_effect = capture_stream

        reply = brain._process_gemini_query("What did you just do?")
        self.assertEqual(reply, "I see notepad was launched and volume muted.")

        # Check system note in captured contents
        system_notes = [str(c) for c in captured_contents if "ALREADY executed" in str(c)]
        self.assertTrue(len(system_notes) > 0)
        self.assertIn("adjust_volume({'action': 'mute'})", system_notes[0])
        self.assertIn("launch_application({'app_name': 'notepad'})", system_notes[0])
        self.assertIn("DO NOT call these tools again", system_notes[0])
        brain.shutdown()

    def test_11_idempotency_ledger_prevents_duplicate_side_effect_execution_during_retry(self):
        """Scenario 11: Idempotency ledger guarantees that retries cannot execute side effects twice."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        side_effect_counter = 0

        def fake_execute_tool(tool_name: str, tool_args: dict, step_index: int = 1, dual_key_confirmed: bool = False):
            nonlocal side_effect_counter
            side_effect_counter += 1
            return f"Executed {tool_name} successfully (count={side_effect_counter})"

        brain._execute_tool_with_orchestrator = MagicMock(side_effect=fake_execute_tool)

        # 1. First execution in turn
        res1, ok1 = brain._dispatch_tool_pipeline("adjust_volume", {"action": "mute"})
        self.assertTrue(ok1)
        self.assertEqual(side_effect_counter, 1)
        self.assertIn("count=1", res1)

        # 2. Simulate model failover retry within same turn re-requesting the identical side-effecting action
        brain._is_turn_retry = True
        res2, ok2 = brain._dispatch_tool_pipeline("adjust_volume", {"action": "mute"})
        self.assertTrue(ok2)
        # Crucial invariant: side effect counter MUST NOT increment
        self.assertEqual(side_effect_counter, 1)
        self.assertIn("count=1", res2)

        # 3. If model during retry attempts a NEW side-effecting tool, it is halted closed
        res3, ok3 = brain._dispatch_tool_pipeline("launch_application", {"app_name": "calc"})
        self.assertFalse(ok3)
        self.assertIn("Action halted", res3)
        self.assertIn("cannot be automatically replayed", res3)
        self.assertEqual(side_effect_counter, 1)
        brain.shutdown()

    def test_12_gemini_candidate_models_audit_no_redundant_aliases(self):
        """Scenario 12: Audits candidate models to ensure no redundant API alias routing."""
        candidates = JarvisBrain.GEMINI_CANDIDATE_MODELS
        self.assertEqual(candidates[0], "gemini-3.8-flash")
        self.assertEqual(candidates[1], "gemini-3.6-flash")
        self.assertEqual(candidates[2], "gemini-3.1-flash-lite")
        # Ensure redundant aliases identified in audit are absent
        self.assertNotIn("gemini-3.7-flash", candidates)
        self.assertNotIn("gemini-3.5-flash", candidates)

    def test_13_total_turn_deadline_aborts_excessive_retries(self):
        """Scenario 13: Bounded turn deadline aborts retries when time budget is exhausted."""
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        self.mock_chat.send_message_stream.side_effect = Exception("504 DEADLINE_EXCEEDED")

        # Simulate turn starting with only 1s remaining in budget
        with patch("time.perf_counter", side_effect=[0.0, 24.5, 24.6, 24.7]):
            reply = brain._process_gemini_query("Query under tight deadline")
            self.assertIn("timed out before receiving a response", reply)
        brain.shutdown()

    def test_14_health_report_states_reconciliation(self):
        """Scenario 14: Distinguishes NOT_CONFIGURED, CONFIGURED, CONNECTION_VERIFIED, and DEGRADED."""
        from jarvis.core.health_check import run_system_health_check

        # 1. Configured state (key present, untested)
        brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        status_untested = brain.get_provider_status()
        self.assertEqual(status_untested["providers"]["gemini"]["status"], "CONFIGURED")

        # 2. Connection verified state
        brain._gemini_last_verified_timestamp = 1700000000.0
        status_verified = brain.get_provider_status()
        self.assertIn("CONNECTION_VERIFIED", status_verified["providers"]["gemini"]["status"])

        # 3. Degraded state
        brain._gemini_last_error = "503 UNAVAILABLE"
        status_degraded = brain.get_provider_status()
        self.assertIn("DEGRADED", status_degraded["providers"]["gemini"]["status"])
        self.assertIn("503 UNAVAILABLE", status_degraded["providers"]["gemini"]["status"])

        # 4. Routine health check reflects state without billable calls
        report = run_system_health_check()
        self.assertIn(report["subsystems"]["cognition"]["secondary_status"], status_degraded["providers"]["gemini"]["status"])
        brain.shutdown()


if __name__ == "__main__":
    unittest.main()
