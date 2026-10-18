"""Unit tests for the 7 Operational Dimensions & Calibration Architecture in Jarvis 2.0.

Verifies:
1. Tier 1 XML serialization (<600 bytes, strictly under 1KB per-turn token budget).
2. SQLite WAL user_preferences table operations and schema constraints.
3. LIFO Thought Resumption Bookmark Stack (CLARIFY_AND_RESUME vs PIVOT_WITH_CACHED_TOOLS).
4. Hybrid Anomaly Dispatch (5-word ear-normalized spoken chime and HUD telemetry).
5. Post-midnight circadian cadence detection with understated Bettany irony.
"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.context.flow_state_mirror import FlowStateSnapshot
from jarvis.conversation.bookmarks import ReasoningBookmark, ReasoningBookmarkStack
from jarvis.core.heartbeat import JarvisHeartbeat
from jarvis.core.memory import MemoryManager
from jarvis.core.orchestrator import JarvisOrchestrator


class TestSevenDimensionOperationalProfile(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_memory.db"
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": str(self.db_path)},
            "heartbeat": {"enabled": True},
        }
        self.memory = MemoryManager(self.config)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tier1_xml_serializer_budget(self):
        """Verifies that format_core_preferences_xml serializes in <600 bytes (<1KB budget)."""
        xml_block = self.memory.format_core_preferences_xml()
        self.assertIn("<core_user_preferences>", xml_block)
        self.assertIn('persona style="bettany_mcu_wit_litotes"', xml_block)
        self.assertIn('vocative="Sir:max_1_pivot_no_repeat"', xml_block)
        self.assertIn('anomaly="hybrid_hud_diff_plus_5word_chime"', xml_block)
        self.assertIn('circadian="ironic_quip_after_0000_120m_flow"', xml_block)
        self.assertIn('design="socratic_constraint_plus_ab_fork"', xml_block)
        self.assertIn('execution="autonomous_pytest_loop_until_green"', xml_block)
        self.assertIn('vision="instant_with_privacy_guard"', xml_block)
        self.assertIn('browser="silent_cdp_9222"', xml_block)
        self.assertIn('facts="ask_confirmation_before_sql_commit"', xml_block)
        self.assertIn('habits="auto_compile_3gram_at_3x_0.85p"', xml_block)
        self.assertIn('barge_in mode="1_sentence_answer_then_lifo_resume"', xml_block)
        self.assertIn('safety="read_test_instant_gate_destructive"', xml_block)
        self.assertIn("</core_user_preferences>", xml_block)

        # Budget verification: strictly under 1KB (1024 bytes)
        byte_len = len(xml_block.encode("utf-8"))
        self.assertLess(byte_len, 1024, f"Tier 1 XML serialization exceeded compact budget: {byte_len} bytes")

    def test_user_preferences_table_crud(self):
        """Verifies storing and retrieving locked preferences from user_preferences table."""
        self.memory.update_user_preference(
            key="persona.archetype",
            value="paul_bettany_mcu_jarvis",
            dimension_id="D1.1",
            locked=1,
        )
        prefs = self.memory.get_user_preferences()
        self.assertEqual(prefs.get("persona.archetype"), "paul_bettany_mcu_jarvis")

    def test_barge_in_intent_classification(self):
        """Verifies intent classification between ABORT, CLARIFY_AND_RESUME, and PIVOT."""
        stack = ReasoningBookmarkStack()

        # ABORT triggers
        self.assertEqual(stack.classify_barge_in_intent("stop right there"), "ABORT")
        self.assertEqual(stack.classify_barge_in_intent("never mind"), "ABORT")
        self.assertEqual(stack.classify_barge_in_intent("cancel that"), "ABORT")

        # CLARIFY_AND_RESUME triggers (<=12 words with question or interjection)
        self.assertEqual(stack.classify_barge_in_intent("wait, what was the port?"), "CLARIFY_AND_RESUME")
        self.assertEqual(stack.classify_barge_in_intent("quick question, is this in WAL mode?"), "CLARIFY_AND_RESUME")
        self.assertEqual(stack.classify_barge_in_intent("hold on, how many tables?"), "CLARIFY_AND_RESUME")

        # PIVOT triggers (new instructions or longer queries)
        self.assertEqual(
            stack.classify_barge_in_intent("No, switch to using synchronous SQLite WAL and add connection pool"),
            "PIVOT",
        )

    def test_format_prompt_injection_clarify_and_resume(self):
        """Verifies prompt injection XML generation for clarifying questions."""
        stack = ReasoningBookmarkStack()
        stack.clear()

        stack.push_interrupted_turn(
            turn_id="turn_42",
            original_query="Explain the Kokoro audio synthesis pipeline",
            topic_label="the audio pipeline",
            spoken_clauses=["First, text enters the phonemizer."],
            unspoken_clauses=["Then, latent vectors are synthesized.", "Finally, WASAPI streams the buffer."],
            resolved_tool_outputs={"audio_status": "ok"},
            flow_state_xml="<telemetry />",
        )
        self.assertEqual(len(stack), 1)

        prompt_xml = stack.format_prompt_injection("wait, what sample rate?")
        self.assertIn('<active_resumption_bookmark mode="CLARIFY_AND_RESUME">', prompt_xml)
        self.assertIn("<suspended_topic>the audio pipeline</suspended_topic>", prompt_xml)
        self.assertIn("<already_spoken>First, text enters the phonemizer.</already_spoken>", prompt_xml)
        self.assertIn("<remaining_to_speak>Then, latent vectors are synthesized. Finally, WASAPI streams the buffer.</remaining_to_speak>", prompt_xml)
        self.assertIn("Continuing with the audio pipeline...", prompt_xml)
        # Consumed upon resumption
        self.assertEqual(len(stack), 0)

    def test_five_word_chime_cadence(self):
        """Verifies strict 5-word ear-normalized cadence on terminal anomalies."""
        snap_assert = FlowStateSnapshot(
            captured_monotonic_ns=1000,
            active_file_path="tests/test_router.py",
            cursor_line=42,
            enclosing_ast_symbol="test_routing",
            last_terminal_cmd="pytest tests/test_router.py",
            last_exit_code=1,
            stderr_tail="AssertionError: expected 200 got 500",
        )
        chime = JarvisHeartbeat._format_five_word_chime(snap_assert)
        self.assertEqual(chime, "Assertion failed in router test, Sir.")

        snap_syntax = FlowStateSnapshot(
            captured_monotonic_ns=2000,
            active_file_path="jarvis/core/brain.py",
            cursor_line=12,
            enclosing_ast_symbol="process",
            last_terminal_cmd="python main.py",
            last_exit_code=1,
            stderr_tail="SyntaxError: invalid syntax",
        )
        chime_syntax = JarvisHeartbeat._format_five_word_chime(snap_syntax)
        self.assertEqual(chime_syntax, "Syntax failed in brain test, Sir.")

    def test_terminal_anomaly_dispatch(self):
        """Verifies full AST diff WebSocket dispatch and 5-word voice chime on non-zero exit."""
        mock_flow = MagicMock()
        mock_tts = MagicMock()
        snap = FlowStateSnapshot(
            captured_monotonic_ns=5000,
            active_file_path="tests/test_engine.py",
            cursor_line=88,
            enclosing_ast_symbol="test_run",
            last_terminal_cmd="pytest",
            last_exit_code=1,
            stderr_tail="TypeError: unsupported operand",
        )
        mock_flow.get_latest_snapshot.return_value = snap

        hb = JarvisHeartbeat(flow_mirror=mock_flow, tts_engine=mock_tts)

        with patch("jarvis.ui.server.broadcast_ambient_telemetry", new_callable=AsyncMock) as mock_ws:
            asyncio.run(hb._evaluate_terminal_anomaly(snap))
            mock_ws.assert_called_once()
            call_kwargs = mock_ws.call_args.kwargs
            self.assertEqual(call_kwargs["type"], "terminal_failure")
            self.assertEqual(call_kwargs["data"]["cmd"], "pytest")
            self.assertEqual(call_kwargs["data"]["exit_code"], 1)

            # Verifies 5-word spoken chime queued to TTS
            mock_tts.queue_sentence.assert_called_once()
            chime_spoken = mock_tts.queue_sentence.call_args[0][0]
            self.assertEqual(chime_spoken, "Type failed in engine test, Sir.")

    def test_circadian_quip_after_midnight(self):
        """Verifies Bettany circadian irony when coding >= 120 continuous minutes after midnight."""
        mock_flow = MagicMock()
        mock_tts = MagicMock()
        hb = JarvisHeartbeat(flow_mirror=mock_flow, tts_engine=mock_tts)

        # Simulate 125 continuous minutes elapsed
        hb._continuous_flow_start_mono = time.monotonic() - (125.0 * 60.0)

        with patch("jarvis.core.heartbeat._get_idle_duration_sec", return_value=10.0), \
             patch("datetime.datetime") as mock_dt, \
             patch("jarvis.ui.server.broadcast_ambient_telemetry", new_callable=AsyncMock) as mock_ws:
            # Simulate 02:30 AM local time
            mock_dt.now.return_value.hour = 2
            asyncio.run(hb._evaluate_circadian_cadence())

            mock_ws.assert_called_once()
            mock_tts.queue_sentence.assert_called_once()
            quip = mock_tts.queue_sentence.call_args[0][0]
            self.assertIn("Operating well past optimal circadian parameters, Sir.", quip)

    def test_format_prompt_injection_pivot_with_cached_tools(self):
        """Verifies prompt injection XML generation for pivot directives preserving tool cache."""
        stack = ReasoningBookmarkStack()
        stack.clear()
        stack.push_interrupted_turn(
            turn_id="turn_pivot_42",
            original_query="Explain Option A implementation details",
            topic_label="Option A details",
            spoken_clauses=["Here are the details for Option A."],
            unspoken_clauses=["First we allocate the memory buffer."],
            resolved_tool_outputs={"profile_memory": "4GB available"},
        )
        self.assertEqual(len(stack), 1)

        prompt_xml = stack.format_prompt_injection("No, proceed with Option B instead")
        self.assertIn('<active_resumption_bookmark mode="PIVOT_WITH_CACHED_TOOLS">', prompt_xml)
        self.assertIn("<prior_query>Explain Option A implementation details</prior_query>", prompt_xml)
        self.assertIn("4GB available", prompt_xml)
        self.assertIn("Sir pivoted direction. Reuse the cached tool results", prompt_xml)
        self.assertEqual(len(stack), 0)

    def test_orchestrator_abort_pushes_bookmark_with_brain_tools(self):
        """Verifies abort_active_command snapshots spoken/unspoken clauses and pulls brain tool outputs."""
        with patch.object(JarvisOrchestrator, "__init__", lambda self, cfg: None):
            orch = JarvisOrchestrator({})
            orch.tts = MagicMock()
            orch.tts.is_speaking = True
            orch.tts.abort_and_partition_clauses.return_value = (
                ["First step completed."],
                ["Second step remaining.", "Third step remaining."],
            )
            orch.brain = MagicMock()
            orch.brain.get_current_turn_tool_outputs.return_value = {
                "read_file": {"status": "ok", "content": "class Foo: pass"}
            }
            orch.bookmarks = ReasoningBookmarkStack()
            orch.bookmarks.clear()
            orch._active_turn_id = "turn_test_101"
            orch._active_query = "Inspect the architecture of Foo"
            orch._active_topic_label = "class Foo architecture"
            orch._active_tool_outputs = {}
            orch._is_processing_command = True
            orch._current_state = "speaking"
            orch.set_state = MagicMock()
            orch.ui_service = None

            was_busy = orch.abort_active_command()
            self.assertTrue(was_busy)
            self.assertEqual(len(orch.bookmarks), 1)

            top = orch.bookmarks.peek_valid()
            self.assertIsNotNone(top)
            self.assertEqual(top.turn_id, "turn_test_101")
            self.assertEqual(top.topic_label, "class Foo architecture")
            self.assertEqual(list(top.spoken_clauses_completed), ["First step completed."])
            self.assertEqual(list(top.unspoken_clauses_remaining), ["Second step remaining.", "Third step remaining."])
            self.assertEqual(top.resolved_tool_outputs["read_file"]["status"], "ok")

    def test_on_atomic_barge_in_invokes_abort(self):
        """Verifies sub-1us barge-in callback triggers abort_active_command immediately."""
        with patch.object(JarvisOrchestrator, "__init__", lambda self, cfg: None):
            orch = JarvisOrchestrator({})
            orch.abort_active_command = MagicMock(return_value=True)
            orch.on_atomic_barge_in()
            orch.abort_active_command.assert_called_once()

    def test_dynamic_topic_label_extraction(self):
        """Verifies dynamic extraction of concise topic labels from user queries for resumption."""
        self.assertEqual(
            JarvisOrchestrator._extract_topic_label("Explain how Kokoro audio synthesis works"),
            "how kokoro audio synthesis",
        )
        self.assertEqual(
            JarvisOrchestrator._extract_topic_label("Can you tell me about the SQLite WAL memory pool?"),
            "the sqlite wal memory",
        )
        self.assertEqual(
            JarvisOrchestrator._extract_topic_label(""),
            "the prior topic",
        )

    def test_brain_generate_turn_async_contract(self):
        """Verifies JarvisBrain.generate_turn contract returns (reply, topic, tool_outputs)."""
        from jarvis.core.brain import JarvisBrain
        mock_mem = MagicMock()
        mock_mem.format_core_preferences_xml.return_value = "<prefs />"
        mock_mem.get_recent_interactions.return_value = []
        mock_mem.get_context_summary.return_value = ""
        mock_mem.recall_episodic_lessons.return_value = []
        mock_safety = MagicMock()

        brain = JarvisBrain({"_base_dir": self.temp_dir, "llm": {"provider": "ollama"}}, mock_mem, mock_safety)
        with patch.object(brain, "process_query", return_value="System operational."):
            res = asyncio.run(brain.generate_turn("Explain the Kokoro audio synthesis"))
            self.assertEqual(len(res), 3)
            reply, topic, tool_outputs = res
            self.assertEqual(reply, "System operational.")
            self.assertIn("kokoro", topic)
            self.assertIsInstance(tool_outputs, dict)


if __name__ == "__main__":
    unittest.main()
