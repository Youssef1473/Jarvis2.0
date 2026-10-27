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
        self.assertEqual(chunks, ["Very well, Sir.", "I shall begin immediately."])

    def test_single_word_terminal_clause(self):
        from jarvis.audio.tts import TextToSpeech
        text = "Understood. Proceeding with calibration now."
        chunks = TextToSpeech._split_into_audio_chunks(None, text)
        self.assertEqual(chunks, ["Understood.", "Proceeding with calibration now."])


class TestPlaybackReliabilityAndRingAllocation(unittest.TestCase):
    """Verifies DEF-REL-06 playback reliability, zero-allocation callback streaming,
    synchronized is_speaking lifecycle, clickless barge-in ramps, and starvation recovery."""

    def setUp(self):
        from unittest.mock import patch
        with patch("jarvis.audio.tts.TextToSpeech._init_kokoro"), \
             patch("jarvis.audio.tts.TextToSpeech._init_sapi"), \
             patch("sounddevice.query_devices"):
            from jarvis.audio.tts import TextToSpeech
            self.tts = TextToSpeech({"tts": {"engine": "kokoro"}})

    def test_callback_direct_read_into_avoids_allocating_read_path(self):
        """Verifies _stream_audio_callback invokes read_into directly without calling the allocating read() path,
        replicating mono samples across stereo channels and zero-filling the tail."""
        from unittest.mock import patch
        self.tts._stream_active = True
        self.tts._stop_event.clear()
        self.tts._prebuffer_filled = True

        test_pcm = np.linspace(0.05, 0.95, 512, dtype=np.float32)
        self.tts._ring_buffer.write(test_pcm)

        outdata = np.zeros((1024, 2), dtype=np.float32)
        with patch.object(self.tts._ring_buffer, "read_into", wraps=self.tts._ring_buffer.read_into) as mock_read_into, \
             patch.object(self.tts._ring_buffer, "read") as mock_read:
            self.tts._stream_audio_callback(outdata, 1024, None, None)
            mock_read_into.assert_called_once_with(outdata, frames=1024)
            mock_read.assert_not_called()

        # Both channels must match mono test PCM for 512 samples
        np.testing.assert_allclose(outdata[:512, 0], test_pcm, atol=1e-6)
        np.testing.assert_allclose(outdata[:512, 1], test_pcm, atol=1e-6)
        # Remaining tail must be strictly zeroed
        np.testing.assert_allclose(outdata[512:, :], 0.0)

    def test_is_speaking_lifecycle_synchronized_with_drain(self):
        """Verifies is_speaking stays True through synthesis completion and only flips
        to False when finish_streaming_session() verifies complete ring buffer drain."""
        from unittest.mock import MagicMock, patch
        self.tts._kokoro = MagicMock()
        self.tts._kokoro.create.return_value = (np.ones(2400, dtype=np.float32), 24000)

        with patch("sounddevice.OutputStream"):
            self.tts.start_streaming_session()
            self.assertTrue(self.tts.is_speaking)

            self.tts.queue_sentence("Awaiting hardware drain.")
            self.tts._sentence_queue.put(None)
            if self.tts._synth_thread:
                self.tts._synth_thread.join(timeout=1.0)
            if self.tts._playback_thread:
                self.tts._playback_thread.join(timeout=1.0)

            # Playback worker consumed None and terminated, but ring buffer has audio:
            # is_speaking must remain True!
            self.assertTrue(self.tts.is_speaking)
            self.assertGreater(self.tts._ring_buffer.available_samples, 0)

            # Drain ring buffer
            self.tts._ring_buffer.clear()
            self.tts.finish_streaming_session()
            self.assertFalse(self.tts.is_speaking)

    def test_barge_in_ramp_emitted_before_stream_shutdown(self):
        """Verifies that barge-in allows the 120-sample cos^2 fade-out ramp to be emitted
        by the callback before stream shutdown, with subsequent callback calls emitting silence."""
        self.tts._stream_active = True
        self.tts._stop_event.clear()
        self.tts._ring_buffer.write(np.ones(1000, dtype=np.float32))

        # Stop sets stop_event and triggers ring_buffer.barge_in()
        self.tts._stop_event.set()
        self.tts._stream_active = False
        self.tts._ring_buffer.barge_in()

        outdata1 = np.ones((512, 2), dtype=np.float32) * -1.0
        self.tts._stream_audio_callback(outdata1, 512, None, None)

        # Ramp must smoothly roll from ~1.0 down to ~0.0 over 120 samples
        ramp = outdata1[:120, 0]
        self.assertAlmostEqual(ramp[0], 1.0, places=3)
        self.assertAlmostEqual(ramp[-1], 0.0, places=3)
        # Tail beyond 120 samples must be zero
        np.testing.assert_allclose(outdata1[120:, :], 0.0)

        # Second invocation must output pure silence
        outdata2 = np.ones((512, 2), dtype=np.float32) * -1.0
        self.tts._stream_audio_callback(outdata2, 512, None, None)
        np.testing.assert_allclose(outdata2, 0.0)

    def test_producer_epoch_fencing_resets_polyphase_resampler(self):
        """Verifies that an invalidated producer epoch aborts ring buffer writes
        and resets stateful polyphase upsampler filter history."""
        from unittest.mock import MagicMock, patch
        self.tts._resampler = MagicMock()
        initial_epoch = self.tts._ring_buffer.barge_in_epoch

        # Write matching epoch succeeds
        written = self.tts._ring_buffer.write(np.ones(100, dtype=np.float32), producer_epoch=initial_epoch)
        self.assertEqual(written, 100)

        # Invalidate epoch via barge-in
        self.tts._ring_buffer.barge_in()

        # Stale write fails
        stale_written = self.tts._ring_buffer.write(np.ones(100, dtype=np.float32), producer_epoch=initial_epoch)
        self.assertEqual(stale_written, 0)

        # Synth worker resets resampler when epoch mismatch occurs mid-flight
        self.tts._kokoro = MagicMock()
        self.tts._kokoro.create.side_effect = lambda *a, **kw: (
            self.tts._ring_buffer.barge_in(),
            (np.ones(100, dtype=np.float32), 24000)
        )[1]

        with patch("sounddevice.OutputStream"):
            self.tts.start_streaming_session()
            self.tts.queue_sentence("Stale audio clause.")
            self.tts._sentence_queue.put(None)
            if self.tts._synth_thread:
                self.tts._synth_thread.join(timeout=1.0)

        self.tts._resampler.reset.assert_called()

    def test_starvation_recovery_and_dynamic_cushion(self):
        """Verifies prebuffer cushion calculation (frames * 2 = 2048 @ 48kHz = ~42.7ms),
        underrun tracking, and prebuffer re-arming upon buffer depletion during synthesis."""
        from unittest.mock import MagicMock
        self.tts._stream_active = True
        self.tts._stop_event.clear()
        self.tts._prebuffer_filled = False

        mock_synth = MagicMock()
        mock_synth.is_alive.return_value = True
        self.tts._synth_thread = mock_synth

        # Available samples (1500) < cushion (2048) -> outputs silence
        self.tts._ring_buffer.write(np.ones(1500, dtype=np.float32))
        outdata = np.ones((1024, 2), dtype=np.float32)
        self.tts._stream_audio_callback(outdata, 1024, None, None)
        self.assertFalse(self.tts._prebuffer_filled)
        np.testing.assert_allclose(outdata, 0.0)

        # Add samples to cross cushion (total 2100 >= 2048) -> unblocks streaming
        self.tts._ring_buffer.write(np.ones(600, dtype=np.float32))
        self.tts._stream_audio_callback(outdata, 1024, None, None)
        self.assertTrue(self.tts._prebuffer_filled)
        self.assertTrue(np.any(outdata != 0.0))

        # Clear buffer to simulate mid-speech starvation while synth active
        self.tts._ring_buffer.clear()
        initial_underruns = self.tts._underrun_count
        self.tts._stream_audio_callback(outdata, 1024, None, None)
        self.assertEqual(self.tts._underrun_count, initial_underruns + 1)
        self.assertFalse(self.tts._prebuffer_filled)

    def test_configured_ramp_duration_and_backwards_compatibility(self):
        """Verifies AudioRingBuffer in TextToSpeech is initialized with ramp_samples=120
        (exact 2.5ms at 48kHz) while AudioRingBuffer() retains default ramp_samples=60."""
        self.assertEqual(self.tts._ring_buffer.ramp_samples, 120)
        self.assertAlmostEqual(120 / 48000.0, 0.0025, places=5)

        default_buf = AudioRingBuffer()
        self.assertEqual(default_buf.ramp_samples, 60)
        self.assertAlmostEqual(60 / 24000.0, 0.0025, places=5)


    def test_stop_normal_shutdown_path(self):
        """Verifies that normal stop stops and closes the stream handle promptly and cleanly,
        passing ignore_errors=False to ensure native errors are observable."""
        from unittest.mock import MagicMock
        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream

        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.010)

        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.abort.assert_not_called()
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertIsNone(self.tts._output_stream)
        self.assertFalse(self.tts._stream_shutdown_timed_out)
        self.assertEqual(self.tts._last_shutdown_outcome, "CLEAN_STOP")
        self.assertFalse(self.tts._stream_cleanup_uncertain)

    def test_stop_exception_falls_back_to_abort(self):
        """Verifies that if stream.stop() raises an exception, the worker catches it
        and falls back sequentially to stream.abort() followed by stream.close()."""
        from unittest.mock import MagicMock
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_stream.stop.side_effect = RuntimeError("PortAudio device error")
        self.tts._output_stream = mock_stream

        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.010)

        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.abort.assert_called_once_with(ignore_errors=False)
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual([c[0] for c in mock_stream.method_calls], ["stop", "abort", "close"])
        self.assertIsNone(self.tts._output_stream)
        self.assertFalse(self.tts._stream_shutdown_timed_out)
        self.assertEqual(self.tts._last_shutdown_outcome, "RECOVERED_ABORT")
        self.assertFalse(self.tts._stream_cleanup_uncertain)

    def test_stop_portaudio_error_falls_back_to_abort(self):
        """Verifies that if stream.stop(ignore_errors=False) reports a sounddevice.PortAudioError,
        it is not silently suppressed and triggers the sequential abort fallback."""
        from unittest.mock import MagicMock

        import sounddevice as sd
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_stream.stop.side_effect = sd.PortAudioError("PaErrorCode -9999: Unanticipated host error")
        self.tts._output_stream = mock_stream

        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.010)

        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.abort.assert_called_once_with(ignore_errors=False)
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual([c[0] for c in mock_stream.method_calls], ["stop", "abort", "close"])
        self.assertEqual(self.tts._last_shutdown_outcome, "RECOVERED_ABORT")
        self.assertFalse(self.tts._stream_cleanup_uncertain)

    def test_stop_internal_type_error_does_not_retry_stop(self):
        """Regression test verifying that an internal TypeError raised during native stop()
        does NOT trigger an unintended second stop() call, but proceeds sequentially to abort and close."""
        from unittest.mock import MagicMock
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_stream.stop.side_effect = TypeError("Internal type mismatch inside PortAudio binding")
        self.tts._output_stream = mock_stream

        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.010)

        # Invariant: stop() was called strictly once with ignore_errors=False, NOT retried
        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.abort.assert_called_once_with(ignore_errors=False)
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual([c[0] for c in mock_stream.method_calls], ["stop", "abort", "close"])
        self.assertIsNone(self.tts._output_stream)

    def test_stop_ramp_acknowledgment_timeout(self):
        """Verifies that if the audio callback does not consume the anti-click fade-out ramp,
        the ramp-wait phase times out cleanly after ramp_timeout without stalling the caller."""
        import time
        from unittest.mock import MagicMock
        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream

        # Force a pending ramp without invoking the callback to consume it
        self.tts._ring_buffer._consumer_epoch = 0
        self.tts._ring_buffer._barge_in_epoch = 1

        t0 = time.monotonic()
        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.020)
        elapsed = time.monotonic() - t0

        # Ramp wait should have waited ~20ms, then proceeded to normal stop/close
        self.assertGreaterEqual(elapsed, 0.018)
        self.assertLess(elapsed, 0.200)
        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertIsNone(self.tts._output_stream)

    def test_stop_simulated_stalled_native_operation(self):
        """Verifies that if native stream.stop() deadlocks in the kernel audio driver,
        the caller's wait is strictly bounded by stop_timeout, the stream handle is retained
        without calling abort() or close() concurrently (preventing PortAudio corruption),
        and the timeout is explicitly recorded."""
        import time
        from unittest.mock import MagicMock

        mock_stream = MagicMock()
        mock_stream.active = True
        # Simulate native Pa_StopStream blocking indefinitely in kernel driver
        mock_stream.stop.side_effect = lambda *a, **kw: time.sleep(0.300)
        self.tts._output_stream = mock_stream

        t0 = time.monotonic()
        # Bound caller wait to 30ms
        self.tts.stop(stop_timeout=0.030, ramp_timeout=0.005)
        elapsed = time.monotonic() - t0

        # Caller wait must be bounded to ~30-60ms, NOT 300ms!
        self.assertLess(elapsed, 0.150)
        self.assertTrue(self.tts._stream_shutdown_timed_out)
        self.assertIsNone(self.tts._output_stream)
        self.assertIn(mock_stream, self.tts._retained_streams)

        # CRITICAL SAFETY INVARIANT: abort() must NOT be called concurrently on the stalled handle
        mock_stream.abort.assert_not_called()
        self.assertEqual(self.tts._last_shutdown_outcome, "TIMEOUT_BLOCKED")
        self.assertTrue(self.tts._stream_cleanup_uncertain)

    def test_shutdown_bounded_single_worker_and_fail_closed_on_stalled_shutdown(self):
        """Deterministic test using threading.Event to verify that a stalled native shutdown
        does NOT allow unbounded thread or stream accumulation, retains exclusive handle ownership,
        and causes subsequent start_streaming_session attempts to fail closed safely."""
        import threading
        import time
        from unittest.mock import MagicMock, patch

        stop_gate = threading.Event()
        mock_stream = MagicMock()
        mock_stream.active = True
        # Block inside native stop until event is set
        mock_stream.stop.side_effect = lambda *a, **kw: stop_gate.wait(timeout=5.0)
        self.tts._output_stream = mock_stream

        t0 = time.monotonic()
        # Initial stop attempt: caller bounds wait to 30ms
        self.tts.stop(stop_timeout=0.030, ramp_timeout=0.005)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 0.150)
        self.assertTrue(self.tts._stream_shutdown_timed_out)
        self.assertIsNone(self.tts._output_stream)
        self.assertIn(mock_stream, self.tts._retained_streams)

        # Active worker exists and is still waiting on stop_gate
        active_worker = self.tts._shutdown_worker
        self.assertIsNotNone(active_worker)
        self.assertTrue(active_worker.is_alive())

        # Invariant 1: Repeated stop calls do NOT spawn additional worker threads
        self.tts.stop(stop_timeout=0.020, ramp_timeout=0.005)
        self.assertIs(self.tts._shutdown_worker, active_worker)
        mock_stream.abort.assert_not_called()

        # Invariant 2: start_streaming_session fails closed while shutdown worker is active
        with patch("sounddevice.OutputStream") as mock_sd_output:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            mock_sd_output.assert_not_called()
            self.assertFalse(self.tts._stream_active)
            self.assertIsNone(self.tts._output_stream)
            self.assertIs(self.tts._shutdown_worker, active_worker)

        # Cleanup: Release the gate to unblock the worker and prevent thread leak
        stop_gate.set()
        active_worker.join(timeout=1.0)
        self.assertFalse(active_worker.is_alive())

        # Invariant 3: After worker exits, start_streaming_session can initialize safely
        with patch("sounddevice.OutputStream") as mock_sd_output:
            started_clean = self.tts.start_streaming_session()
            self.assertNotEqual(started_clean, False)
            self.assertTrue(self.tts._stream_active)
            self.assertIsNone(self.tts._shutdown_worker)


    def test_stop_failure_abort_failure_close_succeeds_is_uncertain(self):
        """Verifies that if both stop() and abort() fail, even if close() succeeds,
        the outcome is recorded as UNCLEAN_FAILED, cleanup is marked uncertain, and subsequent
        streaming sessions fail closed until reconciled."""
        from unittest.mock import MagicMock, patch

        mock_stream = MagicMock()
        mock_stream.active = True
        mock_stream.stop.side_effect = RuntimeError("Pa_StopStream fatal driver failure")
        mock_stream.abort.side_effect = RuntimeError("Pa_AbortStream fatal driver failure")
        self.tts._output_stream = mock_stream

        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.010)

        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.abort.assert_called_once_with(ignore_errors=False)
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual(self.tts._last_shutdown_outcome, "UNCLEAN_FAILED")
        self.assertTrue(self.tts._stream_cleanup_uncertain)
        self.assertFalse(self.tts._stream_shutdown_timed_out)

        # Invariant: start_streaming_session fails closed when cleanup is uncertain
        with patch("sounddevice.OutputStream") as mock_sd_output:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            mock_sd_output.assert_not_called()

        # Invariant: reconcile without force fails; reconcile with force clears uncertain state
        self.assertFalse(self.tts.reconcile_stream_lifecycle(force=False))
        self.assertTrue(self.tts.reconcile_stream_lifecycle(force=True))
        self.assertFalse(self.tts._stream_cleanup_uncertain)

        # Invariant: after reconciliation, streaming session can start
        with patch("sounddevice.OutputStream") as mock_sd_output:
            started_clean = self.tts.start_streaming_session()
            self.assertNotEqual(started_clean, False)
            self.assertTrue(self.tts._stream_active)

    def test_close_failure_reports_unclean_and_fails_closed(self):
        """Verifies that if close() fails (even if stop() succeeded), the outcome is recorded
        as UNCLEAN_FAILED, cleanup is marked uncertain, and subsequent sessions fail closed."""
        from unittest.mock import MagicMock, patch

        mock_stream = MagicMock()
        mock_stream.active = True
        mock_stream.stop.return_value = None
        mock_stream.close.side_effect = RuntimeError("Pa_CloseStream handle release failed")
        self.tts._output_stream = mock_stream

        self.tts.stop(stop_timeout=0.100, ramp_timeout=0.010)

        mock_stream.stop.assert_called_once_with(ignore_errors=False)
        mock_stream.abort.assert_not_called()
        mock_stream.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual(self.tts._last_shutdown_outcome, "UNCLEAN_FAILED")
        self.assertTrue(self.tts._stream_cleanup_uncertain)

        # Invariant: start_streaming_session fails closed
        with patch("sounddevice.OutputStream") as mock_sd_output:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            mock_sd_output.assert_not_called()

    def test_background_worker_unclean_outcome_detected_at_session_start(self):
        """Verifies that if a timed-out shutdown worker eventually finishes in the background
        with UNCLEAN_FAILED, a subsequent start_streaming_session call detects this outcome
        and fails closed."""
        import threading
        from unittest.mock import MagicMock, patch

        stop_gate = threading.Event()
        mock_stream = MagicMock()
        mock_stream.active = True

        def delayed_stop(*args, **kwargs):
            stop_gate.wait(timeout=5.0)
            raise RuntimeError("Deferred stop failure")

        mock_stream.stop.side_effect = delayed_stop
        mock_stream.abort.side_effect = RuntimeError("Deferred abort failure")
        self.tts._output_stream = mock_stream

        # Stop times out (30ms) while worker waits on gate
        self.tts.stop(stop_timeout=0.030, ramp_timeout=0.005)
        self.assertTrue(self.tts._stream_shutdown_timed_out)
        self.assertEqual(self.tts._last_shutdown_outcome, "TIMEOUT_BLOCKED")

        active_worker = self.tts._shutdown_worker
        self.assertIsNotNone(active_worker)
        self.assertTrue(active_worker.is_alive())

        # Unblock worker and let it complete with failure
        stop_gate.set()
        active_worker.join(timeout=1.0)
        self.assertFalse(active_worker.is_alive())

        # start_streaming_session discovers that background worker finished with UNCLEAN_FAILED
        with patch("sounddevice.OutputStream") as mock_sd_output:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            mock_sd_output.assert_not_called()
            self.assertEqual(self.tts._last_shutdown_outcome, "UNCLEAN_FAILED")
            self.assertTrue(self.tts._stream_cleanup_uncertain)


if __name__ == "__main__":
    unittest.main()
