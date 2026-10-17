"""Unit tests for Whisper Acoustic Prosody Extraction (Argon Dimension 1).

Verifies syllable counting heuristic, speech tempo categorization, ProsodyMetrics extraction,
and transcribe_with_prosody.
"""
import time
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from jarvis.audio.stt import (
    SpeechToText,
    ProsodyMetrics,
    count_syllables,
    classify_tempo,
)


class TestWhisperProsodySuite(unittest.TestCase):
    """Test suite validating acoustic prosody extraction and syllable heuristics."""

    def test_count_syllables_heuristic(self):
        """Verifies syllable estimation on various English words."""
        self.assertEqual(count_syllables("code"), 1)
        self.assertEqual(count_syllables("run"), 1)
        self.assertEqual(count_syllables("apple"), 2)
        self.assertEqual(count_syllables("computer"), 3)
        self.assertEqual(count_syllables("architecture"), 4)
        self.assertEqual(count_syllables("initialize"), 4)
        self.assertEqual(count_syllables(""), 0)

    def test_classify_tempo(self):
        """Verifies qualitative tempo categorization based on syllable rate."""
        self.assertEqual(classify_tempo(6.2), "urgent")
        self.assertEqual(classify_tempo(5.2), "fast")
        self.assertEqual(classify_tempo(4.2), "moderate")
        self.assertEqual(classify_tempo(2.1), "slow")

    def test_prosody_metrics_has_timestamp(self):
        """Verifies ProsodyMetrics carries a valid float timestamp."""
        now = time.time()
        p = ProsodyMetrics(speech_rate_syl_sec=4.5, timestamp=now)
        self.assertEqual(p.speech_rate_syl_sec, 4.5)
        self.assertEqual(p.timestamp, now)

    def test_extract_prosody_with_simulated_segments(self):
        """Verifies _extract_prosody calculates syllables/sec and RMS energy correctly."""
        # 2 segments covering 2.0 seconds of active speech
        seg1 = MagicMock()
        seg1.start = 0.0
        seg1.end = 1.0
        seg1.text = "open the"

        seg2 = MagicMock()
        seg2.start = 1.2
        seg2.end = 2.2
        seg2.text = "terminal window"

        text = "open the terminal window"
        # Dummy audio array with known RMS
        dummy_audio = np.full(32000, 0.1, dtype=np.float32)

        metrics = SpeechToText._extract_prosody(dummy_audio, [seg1, seg2], text)

        self.assertIsInstance(metrics, ProsodyMetrics)
        self.assertAlmostEqual(metrics.active_duration_sec, 2.0, places=1)
        self.assertEqual(metrics.word_count, 4)
        # "open" (2) + "the" (1) + "terminal" (3) + "window" (2) = 8 syllables
        self.assertGreaterEqual(metrics.total_syllables, 7)
        self.assertAlmostEqual(metrics.rms_energy, 0.1, places=2)
        self.assertGreater(metrics.speech_rate_syl_sec, 3.0)
        self.assertGreater(metrics.timestamp, 0.0)

    def test_transcribe_updates_last_prosody(self):
        """Verifies SpeechToText.transcribe updates self.last_prosody."""
        config = {
            "stt": {
                "device": "cpu",
                "model_size": "tiny",
                "compute_type": "int8",
            }
        }

        with patch.object(SpeechToText, "_init_model"):
            stt = SpeechToText(config)
            stt.model = MagicMock()

            # Mock segments returned by faster_whisper
            seg = MagicMock()
            seg.start = 0.0
            seg.end = 1.5
            seg.text = "system diagnostic report"
            stt.model.transcribe.return_value = ([seg], MagicMock())

            dummy_audio = np.zeros(24000, dtype=np.float32)
            result = stt.transcribe(dummy_audio)

            self.assertEqual(result, "system diagnostic report")
            self.assertIsNotNone(stt.last_prosody)
            self.assertEqual(stt.last_prosody.word_count, 3)
            self.assertAlmostEqual(stt.last_prosody.active_duration_sec, 1.5, places=1)
            self.assertGreater(stt.last_prosody.timestamp, 0.0)

    def test_transcribe_with_prosody_returns_tuple(self):
        """Verifies transcribe_with_prosody returns (text, ProsodyMetrics)."""
        config = {"stt": {"device": "cpu"}}
        with patch.object(SpeechToText, "_init_model"):
            stt = SpeechToText(config)
            stt.model = MagicMock()

            seg = MagicMock()
            seg.start = 0.0
            seg.end = 0.8
            seg.text = "clear terminal"
            stt.model.transcribe.return_value = ([seg], MagicMock())

            text, prosody = stt.transcribe_with_prosody(np.zeros(16000, dtype=np.float32))
            self.assertEqual(text, "clear terminal")
            self.assertIsInstance(prosody, ProsodyMetrics)
            self.assertEqual(prosody.word_count, 2)


if __name__ == "__main__":
    unittest.main()
