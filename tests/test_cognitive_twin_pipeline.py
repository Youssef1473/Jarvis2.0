"""Unit tests for Argon Dimension 1 Cognitive Twin End-to-End Pipeline.

Verifies:
1. Window Switch vs. Title Change filtering (0x0003 vs 0x800C).
2. Dwell-time IDE residency calculation over rolling 60s window.
3. Decaying active debug signals (120s TTL).
4. Whisper acoustic prosody TTL expiration (180s fallback to 4.2).
5. sample_telemetry_slice metrics fusion and cognitive state inference.
6. JarvisBrain prompt tail envelope injection (<live_telemetry_envelope>).
"""
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.audio.stt import ProsodyMetrics
from jarvis.core.brain import JarvisBrain
from jarvis.core.cognition.cadence_tracker import MotorCadenceTracker
from jarvis.core.cognition.proactive_evi import CognitiveState, CognitiveTwinEngine, TelemetrySlice
from jarvis.core.heartbeat import JarvisHeartbeat
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate



class TestCognitiveTwinPipelineSuite(unittest.TestCase):
    """End-to-end integration tests for the Cognitive Twin telemetry and prompt tail pipeline."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "test_memory.db", "background_learning": False},
            "heartbeat": {
                "enabled": True,
                "idle_threshold_seconds": 300,
                "desktop_presence": True,
            },
        }
        self.memory = MemoryManager(self.config)

        self.orchestrator = MagicMock()
        self.orchestrator.memory = self.memory
        self.orchestrator._is_recording = False
        self.orchestrator.tts.is_speaking = False
        self.orchestrator.stt = MagicMock()
        self.orchestrator.stt.last_prosody = ProsodyMetrics(speech_rate_syl_sec=4.2, timestamp=time.time())

        self.heartbeat = JarvisHeartbeat(self.orchestrator, self.config)

    def tearDown(self):
        self.heartbeat.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_window_switch_vs_title_change_filtering(self):
        """Verifies only event_id 0x0003 with changed HWND records a switch; 0x800C is ignored."""
        # 1. First foreground window event (HWND 1001)
        self.heartbeat._on_window_event(hwnd=1001, event_id=0x0003, title="editor.py - Visual Studio Code")
        self.assertEqual(len(self.heartbeat._window_switches), 1)

        # 2. Title change event within the SAME window (HWND 1001, event 0x800C)
        self.heartbeat._on_window_event(hwnd=1001, event_id=0x800C, title="editor.py* - Visual Studio Code")
        # Must NOT count as a window switch!
        self.assertEqual(len(self.heartbeat._window_switches), 1)

        # 3. Another title change (0x800C)
        self.heartbeat._on_window_event(hwnd=1001, event_id=0x800C, title="terminal - Visual Studio Code")
        self.assertEqual(len(self.heartbeat._window_switches), 1)

        # 4. Actual window switch to a new HWND (HWND 2002, event 0x0003)
        self.heartbeat._on_window_event(hwnd=2002, event_id=0x0003, title="GitHub - Brave")
        self.assertEqual(len(self.heartbeat._window_switches), 2)

    def test_ide_patterns_include_required_tokens(self):
        """Verifies qtcreator, antigravity, .cpp, .h, and .tsx match IDE patterns."""
        self.assertTrue(self.heartbeat.is_ide_window("QtCreator - project.pro"))
        self.assertTrue(self.heartbeat.is_ide_window("Antigravity - AI Pair Programmer"))
        self.assertTrue(self.heartbeat.is_ide_window("main.cpp - Visual Studio"))
        self.assertTrue(self.heartbeat.is_ide_window("server.h - Sublime Text"))
        self.assertTrue(self.heartbeat.is_ide_window("App.tsx - Visual Studio Code"))
        self.assertFalse(self.heartbeat.is_ide_window("YouTube - Brave"))

    def test_dwell_time_ide_focus_ratio(self):
        """Verifies ide_focus_ratio is computed from elapsed dwell time rather than event count."""
        now = time.time()
        # Simulate 50 seconds in an IDE window
        self.heartbeat._focus_intervals.append((now - 60.0, now - 10.0, True))
        # Followed by 10 seconds in a browser
        self.heartbeat._current_window_start = now - 10.0
        self.heartbeat._current_window_is_ide = False

        ratio = self.heartbeat.compute_ide_focus_ratio(window_seconds=60.0)
        # 50s IDE out of 60s total dwell = ~0.83
        self.assertAlmostEqual(ratio, 50.0 / 60.0, places=1)

    def test_decaying_debug_signals_expire_after_120s(self):
        """Verifies debug signals older than 120 seconds decay and do not keep state stuck in debugging."""
        now = time.time()
        # Signal from 150 seconds ago (expired)
        self.heartbeat.record_debug_signal(timestamp=now - 150.0)
        # Signal from 30 seconds ago (active)
        self.heartbeat.record_debug_signal(timestamp=now - 30.0)

        active = self.heartbeat.get_active_debug_signals(window_seconds=120.0)
        self.assertEqual(active, 1)

    def test_prosody_ttl_expiry_falls_back_to_neutral_baseline(self):
        """Verifies utterance older than 180 seconds falls back to 4.2 syl/sec baseline."""
        now = time.time()
        # Stale voice command from 250s ago with rapid 6.5 syl/sec tempo
        stale_prosody = ProsodyMetrics(speech_rate_syl_sec=6.5, timestamp=now - 250.0)
        self.orchestrator.stt.last_prosody = stale_prosody

        slice_obj = self.heartbeat.sample_telemetry_slice()
        # Must fall back to 4.2 neutral baseline!
        self.assertEqual(slice_obj.speech_rate_syl_sec, 4.2)

        # Fresh voice command from 30s ago with 5.6 syl/sec tempo
        fresh_prosody = ProsodyMetrics(speech_rate_syl_sec=5.6, timestamp=now - 30.0)
        self.orchestrator.stt.last_prosody = fresh_prosody

        slice_obj2 = self.heartbeat.sample_telemetry_slice()
        # Must reflect the active voice tempo!
        self.assertEqual(slice_obj2.speech_rate_syl_sec, 5.6)

    def test_sample_telemetry_slice_infers_deep_flow(self):
        """Verifies high KPM, low backspace, and high IDE focus correctly triggers DEEP_FLOW."""
        now = time.time()
        # Simulate active typing: 150 keystrokes with 4 backspaces
        self.heartbeat.cadence_tracker = MotorCadenceTracker(window_seconds=60.0)
        for i in range(150):
            self.heartbeat.cadence_tracker.record_keystroke(is_backspace=(i < 4), timestamp=now - 50.0 + (i * 0.3))

        # 100% IDE dwell
        self.heartbeat._focus_intervals.append((now - 60.0, now, True))
        self.heartbeat._current_window_start = now
        self.heartbeat._current_window_is_ide = True

        slice_obj = self.heartbeat.sample_telemetry_slice()
        self.assertGreaterEqual(slice_obj.kpm, 140.0)
        self.assertLess(slice_obj.backspace_ratio, 0.09)
        self.assertGreaterEqual(slice_obj.ide_focus_ratio, 0.82)
        self.assertEqual(self.heartbeat.cognitive_twin.state, CognitiveState.DEEP_FLOW)

    def test_brain_envelope_injection_contains_live_telemetry(self):
        """Verifies JarvisBrain.build_ephemeral_user_envelope injects live telemetry at prompt tail."""
        safety = SafetyGate(self.config)
        brain = JarvisBrain(self.config, self.memory, safety)
        brain.cognitive_twin = self.heartbeat.cognitive_twin

        self.heartbeat.cognitive_twin.state = CognitiveState.DEEP_FLOW
        self.heartbeat.cognitive_twin.last_telemetry = TelemetrySlice(
            kpm=175.0,
            continuous_session_mins=50.0,
            speech_rate_syl_sec=4.8,
            ide_focus_ratio=0.95,
        )

        user_prompt = "Refactor the WASAPI buffer."
        envelope = brain.build_ephemeral_user_envelope(user_prompt)

        self.assertIn('<live_telemetry_envelope state="DEEP_FLOW"', envelope)
        self.assertIn('kpm="175"', envelope)
        self.assertIn('speech_rate="4.8"', envelope)
        self.assertIn("Directive: Sir is in deep engineering flow. Zero preamble. Max 1-2 crisp sentences.", envelope)
        self.assertIn(user_prompt, envelope)


if __name__ == "__main__":
    unittest.main()
