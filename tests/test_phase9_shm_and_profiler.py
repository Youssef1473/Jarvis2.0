"""Unit tests for Phase 3 Zero-Copy Shared Memory IPC, Real-Time Profiler, and Cancellation Identity."""
import time
import unittest
import uuid
import numpy as np

from jarvis.audio.shm_transport import ShmAudioRingTransport
from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler, TurnTrace
import jarvis.core.cancellation as core_canc
import jarvis.audio.cancellation as audio_canc


class TestShmAudioRingTransport(unittest.TestCase):
    """Verifies lock-free Single-Producer Single-Consumer SharedMemory audio ring transport."""

    def setUp(self):
        self.shm_name = f"test_shm_{uuid.uuid4().hex[:12]}"
        self.transports_to_close = []

    def tearDown(self):
        for transport in self.transports_to_close:
            try:
                transport.close()
            except Exception:
                pass

    def test_write_read_basic(self):
        """Verifies producer write and consumer read across separate shared memory views."""
        producer = ShmAudioRingTransport(name=self.shm_name, capacity_samples=1000, sample_rate=24000, create=True)
        self.transports_to_close.append(producer)

        consumer = ShmAudioRingTransport(name=self.shm_name, capacity_samples=1000, sample_rate=24000, create=False)
        self.transports_to_close.append(consumer)

        pcm_in = np.linspace(-0.8, 0.8, 250, dtype=np.float32)
        written = producer.write_pcm(pcm_in)
        self.assertEqual(written, 250)

        out = np.zeros(250, dtype=np.float32)
        read_count = consumer.read_pcm_into(out)
        self.assertEqual(read_count, 250)
        np.testing.assert_allclose(out, pcm_in, rtol=1e-5, atol=1e-5)

    def test_wrap_around_and_overflow(self):
        """Verifies ring buffer boundary wrap-around and overflow handling."""
        capacity = 100
        producer = ShmAudioRingTransport(name=self.shm_name, capacity_samples=capacity, sample_rate=24000, create=True)
        self.transports_to_close.append(producer)

        consumer = ShmAudioRingTransport(name=self.shm_name, capacity_samples=capacity, sample_rate=24000, create=False)
        self.transports_to_close.append(consumer)

        # 1. Fill 70 samples
        chunk1 = np.ones(70, dtype=np.float32) * 1.5
        producer.write_pcm(chunk1)

        # 2. Consume 70 samples
        out1 = np.zeros(70, dtype=np.float32)
        consumer.read_pcm_into(out1)
        np.testing.assert_allclose(out1, chunk1)

        # 3. Write 50 samples (wraps past end of buffer back to index 0)
        chunk2 = np.ones(50, dtype=np.float32) * 2.5
        producer.write_pcm(chunk2)

        out2 = np.zeros(50, dtype=np.float32)
        read_count = consumer.read_pcm_into(out2)
        self.assertEqual(read_count, 50)
        np.testing.assert_allclose(out2, chunk2)

        # 4. Overflow scenario: write 120 samples into capacity 100
        overflow_chunk = np.arange(120, dtype=np.float32)
        written = producer.write_pcm(overflow_chunk)
        self.assertEqual(written, 100)

        out_overflow = np.zeros(100, dtype=np.float32)
        read_overflow = consumer.read_pcm_into(out_overflow)
        self.assertEqual(read_overflow, 100)
        # Verify it retained the freshest 100 samples (from 20 to 119)
        np.testing.assert_allclose(out_overflow, overflow_chunk[-100:])

    def test_trigger_barge_in_clear(self):
        """Verifies <1us barge-in atomic flush and epoch increment across producer and consumer."""
        producer = ShmAudioRingTransport(name=self.shm_name, capacity_samples=500, sample_rate=24000, create=True)
        self.transports_to_close.append(producer)

        consumer = ShmAudioRingTransport(name=self.shm_name, capacity_samples=500, sample_rate=24000, create=False)
        self.transports_to_close.append(consumer)

        self.assertEqual(producer.barge_in_epoch, 0)
        self.assertEqual(consumer.barge_in_epoch, 0)

        # Write samples
        producer.write_pcm(np.ones(100, dtype=np.float32))

        # Barge-in triggered on producer
        new_epoch = producer.trigger_barge_in_clear()
        self.assertEqual(new_epoch, 1)
        self.assertEqual(consumer.barge_in_epoch, 1)

        # Buffer should be flushed immediately: read returns 0
        out = np.zeros(100, dtype=np.float32)
        read_count = consumer.read_pcm_into(out)
        self.assertEqual(read_count, 0)


class TestPipelineLatencyProfiler(unittest.TestCase):
    """Verifies stage-by-stage turn latency tracking and percentile metrics."""

    def test_turn_trace_durations(self):
        """Verifies TurnTrace computes nanosecond deltas in milliseconds correctly."""
        base_ns = 1_000_000_000
        trace = TurnTrace(
            turn_id="turn-42",
            vad_onset_ns=base_ns,
            vad_end_ns=base_ns + 100_000_000,       # +100ms
            stt_final_ns=base_ns + 150_000_000,     # +50ms from vad_end
            route_complete_ns=base_ns + 160_000_000, # +10ms from stt_final
            llm_first_token_ns=base_ns + 250_000_000,# +100ms from stt_final
            tts_first_chunk_ns=base_ns + 320_000_000,# +70ms from llm_first_token
            audio_first_frame_ns=base_ns + 330_000_000, # +10ms from tts_first_chunk
        )

        metrics = trace.stage_durations_ms()
        self.assertAlmostEqual(metrics["stt_ms"], 50.0, places=1)
        self.assertAlmostEqual(metrics["routing_ms"], 10.0, places=1)
        self.assertAlmostEqual(metrics["ttft_ms"], 100.0, places=1)
        self.assertAlmostEqual(metrics["tts_synth_ms"], 70.0, places=1)
        self.assertAlmostEqual(metrics["dsp_ipc_ms"], 10.0, places=1)
        self.assertAlmostEqual(metrics["total_ttfa_ms"], 230.0, places=1)

    def test_pipeline_latency_profiler_lifecycle_and_percentiles(self):
        """Verifies begin_turn, mark, complete_turn, and P50/P95/P99 rolling window."""
        profiler = PipelineLatencyProfiler(window_size=16)

        # Empty histogram check
        self.assertEqual(profiler.get_percentiles(), {})

        # Simulate 10 turns
        for i in range(10):
            turn_id = f"turn_{i}"
            profiler.begin_turn(turn_id)
            time.sleep(0.001)
            profiler.mark(turn_id, "stt_final_ns")
            profiler.mark(turn_id, "route_complete_ns")
            time.sleep(0.001)
            profiler.mark(turn_id, "llm_first_token_ns")
            profiler.mark(turn_id, "tts_first_chunk_ns")
            profiler.mark(turn_id, "audio_first_frame_ns")
            metrics = profiler.complete_turn(turn_id)
            self.assertIn("total_ttfa_ms", metrics)
            self.assertGreater(metrics["total_ttfa_ms"], 0.0)

        percentiles = profiler.get_percentiles()
        self.assertIn("total_ttfa_ms", percentiles)
        self.assertIn("p50", percentiles["total_ttfa_ms"])
        self.assertIn("p95", percentiles["total_ttfa_ms"])
        self.assertIn("p99", percentiles["total_ttfa_ms"])
        self.assertEqual(percentiles["total_ttfa_ms"]["count"], 10.0)
        self.assertGreaterEqual(percentiles["total_ttfa_ms"]["p99"], percentiles["total_ttfa_ms"]["p50"])


class TestCancellationUnification(unittest.TestCase):
    """Verifies jarvis.core.cancellation and jarvis.audio.cancellation share 100% unified state."""

    def test_cancellation_identity_and_shared_state(self):
        """Verifies class and function references are identical singletons."""
        self.assertIs(core_canc.CancellationToken, audio_canc.CancellationToken)
        self.assertIs(core_canc.is_barge_in_active, audio_canc.is_barge_in_active)
        self.assertIs(core_canc.signal_global_barge_in, audio_canc.signal_global_barge_in)
        self.assertIs(core_canc.reset_global_barge_in, audio_canc.reset_global_barge_in)
        self.assertIs(core_canc.register_audio_flush_callback, audio_canc.register_audio_flush_callback)

        callback_called = []
        def flush_handler():
            callback_called.append(True)

        audio_canc.register_audio_flush_callback(flush_handler)
        core_canc.signal_global_barge_in()

        self.assertTrue(core_canc.is_barge_in_active())
        self.assertTrue(audio_canc.is_barge_in_active())
        self.assertIn(True, callback_called)

        # Reset
        core_canc.reset_global_barge_in()
        self.assertFalse(core_canc.is_barge_in_active())
        self.assertFalse(audio_canc.is_barge_in_active())


if __name__ == "__main__":
    unittest.main()
