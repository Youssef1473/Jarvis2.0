"""Test suite for StreamingSentenceChunker and AudioRingBuffer."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
import numpy as np
from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.audio.ring_buffer import AudioRingBuffer


class TestStreamingSentenceChunker(unittest.TestCase):
    """Verifies speculative clause splitting, abbreviation guards, and flush semantics."""

    def setUp(self):
        self.chunker = StreamingSentenceChunker(min_clause_words=1, min_clause_chars=3)

    def test_clause_and_terminal_splitting(self):
        tokens = ["Good ", "evening, ", "Sir. ", "Systems ", "are ", "fully ", "operational."]
        emitted = []
        for t in tokens:
            emitted.extend(self.chunker.feed(t))
        emitted.extend(self.chunker.flush())

        self.assertIn("Good evening,", emitted)
        self.assertIn("Sir.", emitted)
        self.assertIn("Systems are fully operational.", emitted)

    def test_decimal_and_abbreviation_guards(self):
        tokens = ["The ", "ratio ", "is ", "3.14", " approximately."]
        emitted = []
        for t in tokens:
            emitted.extend(self.chunker.feed(t))
        emitted.extend(self.chunker.flush())

        full_text = " ".join(emitted)
        self.assertIn("3.14", full_text)

    def test_flush_emits_unpunctuated_tail(self):
        self.chunker.feed("Standing by for your instructions")
        remaining = self.chunker.flush()
        self.assertEqual(remaining, ["Standing by for your instructions"])


class TestAudioRingBuffer(unittest.TestCase):
    """Verifies circular PCM buffer allocations, reads, writes, and atomic clear."""

    def setUp(self):
        self.buf = AudioRingBuffer(capacity_samples=1000)

    def test_write_and_read(self):
        samples = np.linspace(-0.5, 0.5, 300, dtype=np.float32)
        written = self.buf.write(samples)
        self.assertEqual(written, 300)
        self.assertEqual(self.buf.available_samples, 300)

        out, read_count = self.buf.read(300)
        self.assertEqual(read_count, 300)
        np.testing.assert_allclose(out, samples, atol=1e-6)
        self.assertEqual(self.buf.available_samples, 0)

    def test_circular_wrap_around(self):
        dummy = np.zeros(800, dtype=np.float32)
        self.buf.write(dummy)
        self.buf.read(800)

        data = np.ones(400, dtype=np.float32)
        self.buf.write(data)
        out, read_count = self.buf.read(400)
        self.assertEqual(read_count, 400)
        np.testing.assert_allclose(out, data, atol=1e-6)

    def test_atomic_clear(self):
        data = np.ones(500, dtype=np.float32)
        self.buf.write(data)
        self.assertGreater(self.buf.available_samples, 0)

        self.buf.clear()
        self.assertEqual(self.buf.available_samples, 0)

    def test_barge_in_anti_click_ramp(self):
        """Verifies that barge_in() triggers a smooth 2.5ms cos^2 fade-out ramp in the consumer read period."""
        data = np.ones(500, dtype=np.float32)
        self.buf.write(data)
        self.assertEqual(self.buf.available_samples, 500)

        # Signal barge-in
        self.buf.barge_in()
        # available_samples must immediately report 0 so drain loops terminate
        self.assertEqual(self.buf.available_samples, 0)

        # Consumer callback reads period (e.g. 100 samples)
        out, read_count = self.buf.read(100)
        self.assertEqual(read_count, 60)  # ramp_samples default = 60

        # Boundary checks: must smoothly roll from ~1.0 down to ~0.0
        self.assertAlmostEqual(out[0], 1.0, places=4)
        self.assertAlmostEqual(out[59], 0.0, places=4)
        # Tail beyond ramp must be zero
        np.testing.assert_allclose(out[60:], 0.0)

        # Next read must return zero samples
        out2, read_count2 = self.buf.read(100)
        self.assertEqual(read_count2, 0)
        np.testing.assert_allclose(out2, 0.0)

    def test_producer_epoch_abort(self):
        """Verifies that synthesis producers abort stale writes when barge-in occurs mid-flight."""
        epoch = self.buf.barge_in_epoch
        # Write with matching epoch succeeds
        written = self.buf.write(np.ones(100, dtype=np.float32), producer_epoch=epoch)
        self.assertEqual(written, 100)

        # Barge-in advances epoch
        self.buf.barge_in()

        # Stale write with old epoch returns 0
        aborted = self.buf.write(np.ones(100, dtype=np.float32), producer_epoch=epoch)
        self.assertEqual(aborted, 0)


class TestAudioChunkSegmentation(unittest.TestCase):
    """Verifies that short polite clauses emit immediately for sub-300ms speech turnaround."""

    def test_short_clause_splitting(self):
        from jarvis.audio.tts import TextToSpeech
        text = "Very well, Sir. I shall begin immediately."
        chunks = TextToSpeech._split_into_audio_chunks(None, text)
        self.assertEqual(chunks, ["Very well,", "Sir.", "I shall begin immediately."])

    def test_single_word_terminal_clause(self):
        from jarvis.audio.tts import TextToSpeech
        text = "Understood. Proceeding with calibration now."
        chunks = TextToSpeech._split_into_audio_chunks(None, text)
        self.assertEqual(chunks, ["Understood.", "Proceeding with calibration now."])


if __name__ == "__main__":
    unittest.main()

