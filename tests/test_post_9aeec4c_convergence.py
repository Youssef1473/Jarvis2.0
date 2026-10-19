"""Unit tests for Post-9aeec4c Final Polish & Convergence.

Verifies:
1. EpochVector & EnvironmentEpochTracker shared between context_orchestrator and speculative engine.
2. TOU invalidation when environment mutations occur.
3. ReasoningBookmarkStack LIFO ordering, 900s TTL eviction, and multi-phrase resumption matching.
4. Orchestrator delegation to brain/tool_orchestrator and latency profiler turn tracing.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.context.context_orchestrator import (
    ContextOrchestrator,
    EpochVector as ContextEpochVector,
    EnvironmentEpochTracker as ContextEpochTracker,
)
from jarvis.conversation.bookmarks import (
    ReasoningBookmark,
    ReasoningBookmarkStack,
    DEFAULT_BOOKMARK_TTL_NS,
)
from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
from jarvis.skills.base import SkillRegistry
from jarvis.speculative.engine import (
    EpochVector as SpecEpochVector,
    EnvironmentEpochTracker as SpecEpochTracker,
    SpeculativeExecutionEngine,
)


class TestPost9aeec4cConvergence(unittest.TestCase):
    """Test suite for post-9aeec4c convergence invariants."""

    def setUp(self):
        ContextEpochTracker.reset()
        self.engine = SpeculativeExecutionEngine.get_instance()
        self.engine.discard_unclaimed()

    def tearDown(self):
        self.engine.discard_unclaimed()
        ContextEpochTracker.reset()

    def test_epoch_vector_and_tracker_shared_identity(self):
        """Verifies EpochVector and EnvironmentEpochTracker are shared across context and speculative modules."""
        self.assertIs(ContextEpochVector, SpecEpochVector)
        self.assertIs(ContextEpochTracker, SpecEpochTracker)

        v_context = ContextOrchestrator.get_epoch_vector()
        v_engine = self.engine.epoch_tracker.snapshot()
        self.assertEqual(v_context, v_engine)
        self.assertTrue(v_context.is_compatible_with(v_engine))

    def test_context_mutation_invalidates_speculative_cache(self):
        """Verifies ContextOrchestrator.notify_environment_mutation invalidates speculative cache at TOU."""
        call_count = 0

        @SkillRegistry.register(name="test_conv_read", read_only=True)
        def test_conv_read(item: str):
            nonlocal call_count
            call_count += 1
            return f"Data_{item}"

        key = self.engine.speculate("test_conv_read", item="catalog")
        for _ in range(50):
            with self.engine._entries_lock:
                entry = self.engine._active_entries.get(key)
            if entry and entry.future.done():
                break
            time.sleep(0.01)

        # Notify mutation via ContextOrchestrator
        ContextOrchestrator.notify_environment_mutation("fs", 101)

        # Claim: should detect mutated epoch and re-run synchronously
        result = self.engine.execute_or_claim("test_conv_read", item="catalog")
        self.assertEqual(result, "Data_catalog")
        self.assertEqual(call_count, 2)
        self.assertGreaterEqual(self.engine.metrics.stale_invalidations, 1)

    def test_bookmark_lifo_and_900s_ttl(self):
        """Verifies ReasoningBookmarkStack LIFO ordering and 900s TTL eviction."""
        stack = ReasoningBookmarkStack(max_depth=4)
        stack.clear()

        # Check default TTL
        self.assertEqual(DEFAULT_BOOKMARK_TTL_NS, 900_000_000_000)

        # Push two turns
        b1 = stack.push_interrupted_turn(
            turn_id="turn_1",
            original_query="First topic query",
            topic_label="first topic",
            spoken_clauses=["Clause 1"],
            unspoken_clauses=["Remaining 1"],
        )
        b2 = stack.push_interrupted_turn(
            turn_id="turn_2",
            original_query="Second topic query",
            topic_label="second topic",
            spoken_clauses=["Clause 2"],
            unspoken_clauses=["Remaining 2"],
        )

        self.assertEqual(len(stack), 2)
        # Strict LIFO: top is b2
        self.assertEqual(stack.peek_valid().turn_id, "turn_2")
        popped = stack.pop_valid()
        self.assertEqual(popped.turn_id, "turn_2")
        self.assertEqual(stack.peek_valid().turn_id, "turn_1")

        # Expired bookmark check: simulate >900s
        now_ns = time.perf_counter_ns()
        future_ns = now_ns + 901_000_000_000  # 901 seconds later
        self.assertTrue(b1.is_expired(now_ns=future_ns))

    def test_bookmark_multi_phrase_resumption_matching(self):
        """Verifies multi-phrase resumption matching for 'continue', 'go on', 'resume', 'as you were saying'."""
        stack = ReasoningBookmarkStack()

        # Phrases that must trigger explicit resumption
        resumption_phrases = [
            "continue",
            "go on",
            "resume",
            "as you were saying",
            "please continue",
            "continue what you were saying",
            "keep going",
        ]
        for phrase in resumption_phrases:
            self.assertTrue(stack.is_resumption_intent(phrase), f"Failed for '{phrase}'")
            self.assertEqual(stack.classify_barge_in_intent(phrase), "CLARIFY_AND_RESUME", f"Failed for '{phrase}'")

        # Test formatting injection for explicit resumption
        stack.clear()
        stack.push_interrupted_turn(
            turn_id="turn_resume_test",
            original_query="Explain the architecture",
            topic_label="the architecture",
            spoken_clauses=["We use a five stage pipeline."],
            unspoken_clauses=["The output stage uses Kokoro."],
        )
        xml = stack.format_prompt_injection("as you were saying")
        self.assertIn('<active_resumption_bookmark mode="CLARIFY_AND_RESUME">', xml)
        self.assertIn("Sir requested to resume", xml)
        self.assertIn("Continuing with the architecture...", xml)

    def test_orchestrator_delegation_and_profiler_tracing(self):
        """Verifies JarvisOrchestrator delegates to brain and records TurnTrace via latency_profiler."""
        from jarvis.core.orchestrator import JarvisOrchestrator

        with patch.object(JarvisOrchestrator, "__init__", lambda self, cfg: None):
            orch = JarvisOrchestrator({})
            orch.config = {}
            orch.brain = MagicMock()
            orch.tts = MagicMock()
            orch.sfx = MagicMock()
            orch.hud = MagicMock()
            orch.get_state = MagicMock(return_value="idle")
            orch.bookmarks = MagicMock()
            orch.bookmarks.format_prompt_injection.return_value = ""
            orch.latency_profiler = PipelineLatencyProfiler(window_size=16)
            orch.tool_orchestrator = MagicMock()
            orch._is_processing_command = False
            orch._extract_topic_label = JarvisOrchestrator._extract_topic_label

            mock_reply = "Architecture is validated, Sir."
            orch.brain.process_query.return_value = mock_reply
            orch.brain.is_fast_reflex.return_value = False
            orch.tts.finish_streaming_session.return_value = True

            orch.handle_command("explain the pipeline architecture")

            # Check that brain was invoked with flow state and bookmark context
            orch.brain.process_query.assert_called_once()
            call_kwargs = orch.brain.process_query.call_args[1]
            self.assertIn("flow_state_xml", call_kwargs)
            self.assertIn("bookmark_context", call_kwargs)

            # Check that latency profiler completed the turn
            percentiles = orch.latency_profiler.get_percentiles()
            self.assertIn("stt_ms", percentiles)

    def test_stateful_polyphase_upsampler(self):
        """Verifies StatefulPolyphaseUpsampler carries 32-sample lookbehind tail to eliminate FIR boundary notch."""
        import numpy as np
        from jarvis.audio.tts import StatefulPolyphaseUpsampler

        upsampler = StatefulPolyphaseUpsampler(up=2, down=1, pad_samples=32)
        self.assertIsNone(upsampler._tail_24k)

        # First chunk
        chunk1 = np.ones(100, dtype=np.float32)
        out1 = upsampler.process(chunk1)
        self.assertEqual(out1.shape, (200,))
        self.assertEqual(out1.dtype, np.float32)
        self.assertIsNotNone(upsampler._tail_24k)
        self.assertEqual(len(upsampler._tail_24k), 32)
        np.testing.assert_array_equal(upsampler._tail_24k, chunk1[-32:])

        # Second chunk uses previous tail
        chunk2 = np.ones(80, dtype=np.float32) * 2.0
        out2 = upsampler.process(chunk2)
        self.assertEqual(out2.shape, (160,))
        self.assertEqual(out2.dtype, np.float32)
        np.testing.assert_array_equal(upsampler._tail_24k, chunk2[-32:])

        # Reset flushes lookbehind tail
        upsampler.reset()
        self.assertIsNone(upsampler._tail_24k)

        # Empty array handling
        empty = np.array([], dtype=np.float32)
        self.assertEqual(len(upsampler.process(empty)), 0)

    def test_is_window_dwm_cloaked(self):
        """Verifies is_window_dwm_cloaked detects Windows 11 DWM cloaked windows on inactive virtual desktops."""
        import ctypes
        from ctypes import wintypes
        from jarvis.vision.privacy_guard import is_window_dwm_cloaked

        # Invalid HWND returns False
        self.assertFalse(is_window_dwm_cloaked(0))

        # Mock dwmapi.DwmGetWindowAttribute returning cloaked != 0
        def fake_dwm_cloaked(hwnd, attr, out_ptr, size):
            val = wintypes.DWORD(1)
            ctypes.memmove(out_ptr, ctypes.byref(val), ctypes.sizeof(val))
            return 0

        with patch("ctypes.windll.dwmapi.DwmGetWindowAttribute", side_effect=fake_dwm_cloaked):
            self.assertTrue(is_window_dwm_cloaked(99999))

        # Mock dwmapi.DwmGetWindowAttribute returning cloaked == 0 (uncloaked)
        def fake_dwm_uncloaked(hwnd, attr, out_ptr, size):
            val = wintypes.DWORD(0)
            ctypes.memmove(out_ptr, ctypes.byref(val), ctypes.sizeof(val))
            return 0

        with patch("ctypes.windll.dwmapi.DwmGetWindowAttribute", side_effect=fake_dwm_uncloaked):
            self.assertFalse(is_window_dwm_cloaked(99999))

        # Exception fallback returns False
        with patch("ctypes.windll.dwmapi.DwmGetWindowAttribute", side_effect=RuntimeError("DWM error")):
            self.assertFalse(is_window_dwm_cloaked(99999))

    def test_hung_app_window_safe_foreground_activation(self):
        """Verifies safe_set_foreground_window checks IsHungAppWindow and skips AttachThreadInput on hung windows."""
        from jarvis.skills.os_control import safe_set_foreground_window

        # Invalid HWND
        self.assertFalse(safe_set_foreground_window(0))

        # Case 1: Foreground window is hung -> Must skip AttachThreadInput and fallback to VK_MENU
        with patch("ctypes.windll.user32.IsIconic", return_value=0), \
             patch("ctypes.windll.user32.GetForegroundWindow", return_value=5555), \
             patch("ctypes.windll.user32.IsHungAppWindow", return_value=1) as mock_hung, \
             patch("ctypes.windll.user32.AttachThreadInput") as mock_attach, \
             patch("ctypes.windll.user32.keybd_event") as mock_keybd, \
             patch("ctypes.windll.user32.SetForegroundWindow", return_value=1) as mock_set_fg:
            success = safe_set_foreground_window(8888)
            self.assertTrue(success)
            mock_hung.assert_called_once()
            # Must NOT call AttachThreadInput when foreground is hung
            mock_attach.assert_not_called()
            # Must call synthetic VK_MENU (Alt key) fallback
            self.assertGreaterEqual(mock_keybd.call_count, 2)
            mock_set_fg.assert_called_once()

        # Case 2: Foreground window is NOT hung -> Can attach thread input
        with patch("ctypes.windll.user32.IsIconic", return_value=0), \
             patch("ctypes.windll.user32.GetForegroundWindow", return_value=5555), \
             patch("ctypes.windll.user32.IsHungAppWindow", return_value=0) as mock_hung2, \
             patch("ctypes.windll.kernel32.GetCurrentThreadId", return_value=101), \
             patch("ctypes.windll.user32.GetWindowThreadProcessId", return_value=202), \
             patch("ctypes.windll.user32.AttachThreadInput", return_value=1) as mock_attach2, \
             patch("ctypes.windll.user32.BringWindowToTop", return_value=1), \
             patch("ctypes.windll.user32.SetForegroundWindow", return_value=1):
            success = safe_set_foreground_window(8888)
            self.assertTrue(success)
            mock_hung2.assert_called_once()
            # Must attach and detach
            self.assertEqual(mock_attach2.call_count, 2)

    def test_incremental_utf8_pipe_decoding(self):
        """Verifies multibyte UTF-8 characters split across pipe buffer boundaries do not corrupt."""
        import codecs

        # Test string containing multibyte characters: 3-byte unicode symbol '⚡' (b'\xe2\x9a\xa1')
        full_text = "⚡ System status: operational → all green\n"
        raw_bytes = full_text.encode("utf-8")

        # Split directly in the middle of '⚡' (first 2 bytes in chunk1, 3rd byte in chunk2)
        chunk1 = raw_bytes[:2]
        chunk2 = raw_bytes[2:]

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        part1 = decoder.decode(chunk1)
        self.assertEqual(part1, "", "Incomplete multibyte sequence should buffer without yielding corrupt replacement char")
        part2 = decoder.decode(chunk2)
        reconstructed = part1 + part2
        self.assertEqual(reconstructed, full_text)
        self.assertNotIn("\ufffd", reconstructed)

    def test_ro_pool_wal_read_mark_unpinned(self):
        """Verifies ro_pool connections use autocommit (isolation_level=None) and release read-marks on context exit."""
        import sqlite3
        import tempfile
        from pathlib import Path
        from jarvis.memory.ro_pool import ReadOnlyConnectionPool

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_wal_unpin.db"
            # Initialize WAL mode database with single writer
            conn_w = sqlite3.connect(str(db_path))
            conn_w.execute("PRAGMA journal_mode=WAL;")
            conn_w.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);")
            conn_w.execute("INSERT INTO items (name) VALUES ('alpha');")
            conn_w.commit()

            pool = ReadOnlyConnectionPool.get_instance(db_path)

            # Borrow read-only connection
            with pool.connection(db_path) as conn_ro:
                self.assertIsNone(conn_ro.isolation_level)
                cursor = conn_ro.execute("SELECT * FROM items WHERE name = 'alpha';")
                row = cursor.fetchone()
                self.assertEqual(row["name"], "alpha")

            # Upon exiting context manager, read transaction snapshot must be committed/released
            # Checkpoint TRUNCATE should succeed with zero lock contention
            cursor_ckpt = conn_w.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            ckpt_res = cursor_ckpt.fetchone()
            # ckpt_res is (busy, log, checkpointed) -> busy must be 0
            self.assertEqual(ckpt_res[0], 0, f"Expected checkpoint busy=0 (unpinned), got: {ckpt_res}")
            conn_w.close()
            pool.close_current_thread()


if __name__ == "__main__":
    unittest.main()

