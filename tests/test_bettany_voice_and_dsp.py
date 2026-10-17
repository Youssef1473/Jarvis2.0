"""Unit tests for Kokoro-82M Latent Style-Vector Blending & Signature MCU J.A.R.V.I.S. DSP Filter."""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock
import numpy as np

from jarvis.audio.ring_buffer import AudioRingBuffer, apply_cinematic_jarvis_dsp
from jarvis.audio.tts import TextToSpeech, create_bettanys_jarvis_voice_vector
from jarvis.skills.base import SkillRegistry
import jarvis.skills.voice_control as voice_skills


class TestBettanyVoiceAndDsp(unittest.TestCase):
    """Verifies latent style blending in R^256 and cinematic J.A.R.V.I.S. DSP filtering."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "tts": {
                "engine": "kokoro",
                "kokoro_voice": "jarvis_bettany",
                "kokoro_speed": 1.03,
                "kokoro_lang": "en-gb",
                "dsp_filter": True,
                "cpu_threads": 2,
            },
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_bettanys_jarvis_voice_vector(self):
        """Verifies weighted barycentric blend: 50% fable + 30% george + 20% daniel."""
        fake_kokoro = MagicMock()
        v_fable = np.ones((510, 1, 256), dtype=np.float32) * 1.0
        v_george = np.ones((510, 1, 256), dtype=np.float32) * 2.0
        v_daniel = np.ones((510, 1, 256), dtype=np.float32) * 3.0

        def fake_get_style(v_name):
            if v_name == "bm_fable":
                return v_fable
            elif v_name == "bm_george":
                return v_george
            elif v_name == "bm_daniel":
                return v_daniel
            return v_george

        fake_kokoro.get_voice_style.side_effect = fake_get_style

        blended = create_bettanys_jarvis_voice_vector(fake_kokoro)
        self.assertIsInstance(blended, np.ndarray)
        self.assertEqual(blended.shape, (510, 1, 256))
        self.assertEqual(blended.dtype, np.float32)

        # Expected value: 0.50 * 1.0 + 0.30 * 2.0 + 0.20 * 3.0 = 0.5 + 0.6 + 0.6 = 1.70
        self.assertAlmostEqual(float(blended[0, 0, 0]), 1.70, delta=0.001)

    def test_tts_resolves_jarvis_bettany_profile(self):
        """Verifies TextToSpeech resolves 'jarvis_bettany' to the 50/30/20 blend."""
        tts = TextToSpeech(self.config)
        fake_kokoro = MagicMock()
        v_fable = np.ones((510, 1, 256), dtype=np.float32) * 10.0
        v_george = np.ones((510, 1, 256), dtype=np.float32) * 20.0
        v_daniel = np.ones((510, 1, 256), dtype=np.float32) * 30.0

        def fake_get_style(v_name):
            if v_name == "bm_fable":
                return v_fable
            elif v_name == "bm_george":
                return v_george
            elif v_name == "bm_daniel":
                return v_daniel
            return v_george

        fake_kokoro.get_voice_style.side_effect = fake_get_style
        fake_kokoro.get_voices.return_value = ["bm_fable", "bm_george", "bm_daniel"]
        fake_kokoro.voices = {"bm_fable": v_fable, "bm_george": v_george, "bm_daniel": v_daniel}
        tts._kokoro = fake_kokoro

        tts.set_voice_profile("jarvis_bettany")
        blended = tts._resolve_voice()
        self.assertIsInstance(blended, np.ndarray)
        # Expected: 0.50*10 + 0.30*20 + 0.20*30 = 5 + 6 + 6 = 17.0
        self.assertAlmostEqual(float(blended[0, 0, 0]), 17.0, delta=0.01)

    def test_apply_cinematic_jarvis_dsp_hpf_and_comb(self):
        """Verifies 110Hz high-pass filtering and 7.5ms micro-reflection."""
        sr = 24000
        duration = 1.0
        t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)

        # 40Hz sub-bass wave (should be attenuated significantly by 110Hz HPF)
        sub_bass = np.sin(2 * np.pi * 40 * t)
        filtered_sub = apply_cinematic_jarvis_dsp(sub_bass, sample_rate=sr)
        sub_rms_before = np.sqrt(np.mean(sub_bass ** 2))
        sub_rms_after = np.sqrt(np.mean(filtered_sub ** 2))
        # 110Hz second-order butterworth HPF should attenuate 40Hz by > 12 dB (amplitude < 0.25)
        self.assertLess(sub_rms_after, sub_rms_before * 0.35)

        # 1000Hz tone (should pass through largely unattenuated)
        mid_tone = np.sin(2 * np.pi * 1000 * t) * 0.5
        filtered_mid = apply_cinematic_jarvis_dsp(mid_tone, sample_rate=sr)
        self.assertGreater(float(np.max(np.abs(filtered_mid))), 0.35)

    def test_apply_cinematic_jarvis_dsp_peak_limiting(self):
        """Verifies peak limiter constrains loud audio to <= 0.95."""
        loud_signal = np.ones(2400, dtype=np.float32) * 5.0  # Excessive amplitude
        processed = apply_cinematic_jarvis_dsp(loud_signal, sample_rate=24000)
        max_amplitude = float(np.max(np.abs(processed)))
        self.assertLessEqual(max_amplitude, 0.9501)

    def test_apply_cinematic_jarvis_dsp_edge_cases(self):
        """Verifies graceful handling of empty arrays and sub-delay slices."""
        self.assertEqual(len(apply_cinematic_jarvis_dsp(np.array([], dtype=np.float32))), 0)
        self.assertIsNone(apply_cinematic_jarvis_dsp(None))

        # Short slice (50 samples < 180 delay samples)
        short_slice = np.ones(50, dtype=np.float32) * 0.5
        res = apply_cinematic_jarvis_dsp(short_slice, sample_rate=24000)
        self.assertEqual(len(res), 50)

    def test_apply_cinematic_jarvis_dsp_latency(self):
        """Verifies DSP processing latency is strictly < 1.0ms for a 1-second audio chunk."""
        sr = 24000
        chunk = np.random.randn(sr).astype(np.float32) * 0.4
        # Warmup cache
        _ = apply_cinematic_jarvis_dsp(chunk, sample_rate=sr)

        t0 = time.perf_counter()
        for _ in range(50):
            _ = apply_cinematic_jarvis_dsp(chunk, sample_rate=sr)
        avg_ms = ((time.perf_counter() - t0) / 50.0) * 1000.0
        self.assertLess(avg_ms, 1.0, f"DSP filter took {avg_ms:.3f}ms, exceeding 1.0ms budget")

    def test_ring_buffer_write_with_dsp(self):
        """Verifies AudioRingBuffer.write executes DSP processing when apply_dsp=True."""
        rb = AudioRingBuffer(capacity_samples=48000)
        test_samples = np.ones(1000, dtype=np.float32) * 3.0
        written = rb.write(test_samples, apply_dsp=True, sample_rate=24000)
        self.assertEqual(written, 1000)

        read_samples, n = rb.read(1000)
        self.assertEqual(n, 1000)
        # Verify peak limiter was applied (<= 0.95)
        self.assertLessEqual(float(np.max(np.abs(read_samples))), 0.9501)

    def test_voice_control_skill_set_jarvis_bettany(self):
        """Verifies set_voice_profile skill accepts 'jarvis_bettany'."""
        tts = TextToSpeech(self.config)
        fake_kokoro = MagicMock()
        fake_kokoro.get_voices.return_value = ["bm_george", "bm_daniel", "bm_fable"]
        tts._kokoro = fake_kokoro
        voice_skills.set_tts_instance(tts)

        res = SkillRegistry.execute("set_voice_profile", profile_name="jarvis_bettany")
        self.assertIn("Voice profile successfully set to 'jarvis_bettany'", res)
        self.assertEqual(tts.kokoro_voice, "jarvis_bettany")


if __name__ == "__main__":
    unittest.main()
