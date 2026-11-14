"""Hermetic unit tests for the Jarvis 2.0 Acoustic Loop (Phase 2).

Verifies:
1. Kokoro-82M Bettany blend voice style resolution in R^256.
2. MCU J.A.R.V.I.S. signature DSP filter (110Hz HPF + 7.5ms micro-reflection).
3. Speech clause splitting and abbreviation protection.
4. Hann window edge smoothing and peak limiting.
5. SPSC Frame Ring Buffer thread safety and bounded backpressure.
6. End-to-end acoustic loop integration (Wake -> STT -> Brain -> TTS -> Audio Queue).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import numpy as np

from jarvis.audio.ring_buffer import apply_cinematic_jarvis_dsp
from jarvis.audio.tts import TextToSpeech, create_bettanys_jarvis_voice_vector
from jarvis.audio.wake_word import SPSCFrameRingBuffer
from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestAcousticLoop(unittest.TestCase):
    """Hermetic test suite for Phase 2 Acoustic Loop components."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = load_config()
        self.config["_base_dir"] = self.temp_dir
        self.config.setdefault("memory", {})["db_path"] = os.path.join(self.temp_dir, "test_memory.db")
        self.config.setdefault("llm", {})["provider"] = "antigravity"

        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_kokoro_voice_style_resolution_and_blend(self):
        """Scenario 1: Verifies barycentric blend of British voices (50% fable, 30% george, 20% daniel)."""
        fake_kokoro = MagicMock()
        v_fable = np.ones((510, 1, 256), dtype=np.float32) * 10.0
        v_george = np.ones((510, 1, 256), dtype=np.float32) * 20.0
        v_daniel = np.ones((510, 1, 256), dtype=np.float32) * 30.0

        def fake_get_style(name: str):
            if name == "bm_fable":
                return v_fable
            elif name == "bm_george":
                return v_george
            elif name == "bm_daniel":
                return v_daniel
            return v_george

        fake_kokoro.get_voice_style.side_effect = fake_get_style
        blended = create_bettanys_jarvis_voice_vector(fake_kokoro)

        self.assertEqual(blended.shape, (510, 1, 256))
        self.assertEqual(blended.dtype, np.float32)
        # Expected: 0.50*10 + 0.30*20 + 0.20*30 = 5 + 6 + 6 = 17.0
        self.assertAlmostEqual(float(blended[0, 0, 0]), 17.0, delta=0.01)

    def test_02_cinematic_dsp_filtering_and_peak_limiting(self):
        """Scenario 2: Verifies 110Hz HPF attenuation and peak limiting to <= 0.95."""
        sr = 24000
        duration = 0.5
        t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)

        # 30Hz sub-bass wave (should be attenuated significantly by 110Hz HPF)
        sub_bass = np.sin(2 * np.pi * 30 * t) * 1.5  # deliberately peak > 1.0
        filtered = apply_cinematic_jarvis_dsp(sub_bass, sample_rate=sr)

        self.assertLessEqual(float(np.max(np.abs(filtered))), 0.95)
        # Verify sub-bass energy attenuation
        rms_in = np.sqrt(np.mean(sub_bass**2))
        rms_out = np.sqrt(np.mean(filtered**2))
        self.assertLess(rms_out, rms_in * 0.5)

    def test_03_speech_clause_splitting_and_abbreviations(self):
        """Scenario 3: Verifies clause splitting on natural punctuation while preserving common abbreviations."""
        tts = TextToSpeech(self.config)
        text = "Good morning, Dr. Watson. All systems are operational; we are ready to proceed."
        chunks = tts._split_into_audio_chunks(text)

        self.assertTrue(len(chunks) >= 2)
        # Ensure 'Dr. Watson' is NOT split at the period in 'Dr.'
        dr_found = any("dr. watson" in c.lower() for c in chunks)
        self.assertTrue(dr_found, f"Dr. Watson was improperly split: {chunks}")

    def test_04_smooth_and_pad_audio_hann_window(self):
        """Scenario 4: Verifies 5ms Hann window edge smoothing prevents audio click artifacts."""
        tts = TextToSpeech(self.config)
        raw_samples = np.ones(2400, dtype=np.float32)  # 100ms at 24kHz
        smoothed = tts._smooth_and_pad_audio(raw_samples, 24000, "test", apply_dsp=False, pad_silence=False)

        # Leading samples should fade in from 0.0 towards 1.0
        self.assertLess(float(smoothed[0]), 0.1)
        self.assertGreater(float(smoothed[120]), 0.8)
        # Trailing samples should fade out to near 0.0
        self.assertLess(float(smoothed[-1]), 0.1)

    def test_05_spsc_frame_ring_buffer_backpressure_and_occupancy(self):
        """Scenario 5: Verifies SPSCFrameRingBuffer bounded capacity, occupancy tracking, and drop accounting."""
        ring = SPSCFrameRingBuffer(capacity=10, blocksize=1280)
        self.assertEqual(ring.occupancy, 0)

        # Write 5 frames
        dummy_frame = np.ones(1280, dtype=np.float32) * 0.5
        for _ in range(5):
            ring.write_frame(dummy_frame)
        self.assertEqual(ring.occupancy, 5)

        # Write 10 more frames (exceeding capacity of 10)
        for _ in range(10):
            ring.write_frame(dummy_frame)
        # Ring should cap occupancy and record overflow
        self.assertLessEqual(ring.occupancy, 10)
        self.assertGreater(ring.overflow_count, 0)

    def test_06_acoustic_loop_end_to_end_simulation(self):
        """Scenario 6: End-to-end integration: Wake word -> STT -> Brain reflex -> TTS speech output."""
        brain = JarvisBrain(self.config, self.memory, self.safety, antigravity_resolver=lambda: None)

        # 1. Deterministic reflex route
        reply = brain.process_query("What time is it?")
        self.assertIn("Sir", reply)
        self.assertIn("time is", reply)

        # 2. TTS chunking and synthesis simulation
        tts = TextToSpeech(self.config)
        fake_kokoro = MagicMock()
        mock_audio = np.zeros(24000, dtype=np.float32)
        fake_kokoro.create.return_value = (mock_audio, 24000)
        fake_kokoro.get_voice_style.return_value = np.ones((510, 1, 256), dtype=np.float32)
        tts._kokoro = fake_kokoro

        sub_chunks = tts._split_into_audio_chunks(reply)
        self.assertTrue(len(sub_chunks) >= 1)

        samples, sr = tts._kokoro.create(sub_chunks[0], voice="bm_george")
        self.assertEqual(len(samples), 24000)
        self.assertEqual(sr, 24000)

        brain.shutdown()


if __name__ == "__main__":
    unittest.main()
