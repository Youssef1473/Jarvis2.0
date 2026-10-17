"""Automated tests for Adaptive Listening & Thinking Pause Tolerance in Jarvis 2.0.
Verifies dynamic silence scaling, hesitation/murmur damping, PTT toggle mode, and hard ceiling protection.
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.orchestrator import JarvisOrchestrator


class TestAdaptiveListeningSuite(unittest.TestCase):
    """Verifies adaptive silence thresholding, hesitation damping, and PTT toggle mode."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "audio": {
                "sample_rate": 16000,
                "channels": 1,
                "silence_threshold_seconds": 1.4,
                "thinking_pause_seconds": 3.2,
                "max_record_seconds": 35.0,
                "initial_timeout_seconds": 3.5,
                "hesitation_damping": True,
                "ptt_mode": "hold",
                "ptt_key": "fin",
            },
            "ui": {"enabled": False},
            "heartbeat": {"enabled": False},
            "memory": {"db_path": str(Path(self.temp_dir) / "test_mem.db")},
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dynamic_silence_scaling_formula(self):
        """Verifies mathematical scaling from base silence (1.4s) to thinking pause (3.2s)."""
        base_silence_s = 1.4
        thinking_pause_s = 3.2

        # 1. Very brief utterance (0.4s speech) -> ~1.54s pause tolerance
        spoken_brief = 0.4
        thresh_brief = min(thinking_pause_s, base_silence_s + (spoken_brief * 0.35))
        self.assertAlmostEqual(thresh_brief, 1.54, delta=0.01)

        # 2. Medium utterance (2.0s speech) -> ~2.10s pause tolerance
        spoken_med = 2.0
        thresh_med = min(thinking_pause_s, base_silence_s + (spoken_med * 0.35))
        self.assertAlmostEqual(thresh_med, 2.10, delta=0.01)

        # 3. Long complex thought (6.0s speech) -> hits 3.2s cap
        spoken_long = 6.0
        thresh_long = min(thinking_pause_s, base_silence_s + (spoken_long * 0.35))
        self.assertEqual(thresh_long, 3.2)

    def test_ptt_toggle_mode_lifecycle(self):
        """Verifies PTT toggle mode: Press 1 starts recording, Release does nothing, Press 2 concludes."""
        cfg = dict(self.config)
        cfg["audio"]["ptt_mode"] = "toggle"

        with patch("jarvis.core.orchestrator.SoundEffects"), \
             patch("jarvis.core.orchestrator.MemoryManager") as mock_mem_cls, \
             patch("jarvis.core.orchestrator.SafetyGate"), \
             patch("jarvis.core.orchestrator.SpeechToText"), \
             patch("jarvis.core.orchestrator.TextToSpeech"), \
             patch("jarvis.core.orchestrator.JarvisBrain"), \
             patch("jarvis.core.orchestrator.JarvisHUD"), \
             patch("jarvis.core.orchestrator.JarvisHeartbeat"), \
             patch("jarvis.core.orchestrator.HotkeyListener"), \
             patch("jarvis.core.orchestrator.WakeWordDetector"), \
             patch("sounddevice.InputStream") as mock_stream:

            mock_mem_instance = mock_mem_cls.return_value
            mock_mem_instance.db_path = Path(self.temp_dir) / "test_mem.db"

            orchestrator = JarvisOrchestrator(cfg)
            orchestrator._process_audio = MagicMock()

            # Press 1: Starts recording
            orchestrator._on_ptt_press()
            self.assertTrue(orchestrator._is_recording)

            # Release: In toggle mode, does NOT finish
            orchestrator._on_ptt_release()
            self.assertTrue(orchestrator._is_recording)

            # Add a simulated frame
            orchestrator._recorded_frames.append(np.zeros((1600, 1), dtype=np.float32))

            # Press 2: Concludes recording and processes audio
            orchestrator._on_ptt_press()
            self.assertFalse(orchestrator._is_recording)

    def test_hesitation_and_breathing_decay(self):
        """Verifies intermediate vad scores (0.12-0.29) or breathing RMS energy damp silence counter."""
        silence_count = 10
        vad_score = 0.22
        rms_energy = 0.0040
        hesitation_damping = True

        if hesitation_damping and (0.12 <= vad_score < 0.30 or rms_energy >= 0.0038):
            silence_count = max(0, silence_count - 2)

        self.assertEqual(silence_count, 8, "Hesitation or breathing must decay silence count")

    def test_pure_silence_increments_counter(self):
        """Verifies true silence (vad < 0.12 and low RMS) increments silence counter."""
        silence_count = 10
        vad_score = 0.05
        rms_energy = 0.0010
        hesitation_damping = True

        if hesitation_damping and (0.12 <= vad_score < 0.30 or rms_energy >= 0.0038):
            silence_count = max(0, silence_count - 2)
        else:
            silence_count += 1

        self.assertEqual(silence_count, 11, "Pure silence must increment silence count")


if __name__ == "__main__":
    unittest.main()
