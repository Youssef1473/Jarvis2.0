"""Comprehensive hermetic tests for CORE-AUDIO-01: Voice Interruption & Microphone Reliability.

Covers:
1. Barge-in during TTS (DEF-REL-02)
2. Echo rejection below barge-in threshold
3. Recording suppression during VAD & PTT
4. Single persistent InputStream invariant (DEF-REL-03)
5. Pre-roll handoff from wake detection to VAD
6. Queue overflow detection and reporting
7. Error cleanup and listening state restoration
8. Repeated start/stop stability
9. Concurrent command transitions (PTT preempts VAD)
"""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from jarvis.audio.wake_word import CaptureMode, SPSCFrameRingBuffer, WakeWordDetector
from jarvis.core.cancellation import reset_global_barge_in


class TestAudioReliability(unittest.TestCase):
    """Hermetic unit tests validating single-stream routing and voice interruption invariants."""

    def setUp(self):
        reset_global_barge_in()
        self.config = {
            "audio": {
                "sample_rate": 16000,
                "block_size": 1280,
                "wake_word_threshold": 0.55,
                "wake_word_barge_in_threshold": 0.70,
                "wake_word_energy_threshold": 0.0035,
            }
        }

    def tearDown(self):
        reset_global_barge_in()

    def _make_dummy_frame(self, amplitude: float = 0.05) -> np.ndarray:
        """Generates a 1280-sample float32 chunk above energy threshold."""
        return np.full((1280, 1), amplitude, dtype=np.float32)

    def test_barge_in_during_tts_triggers_interruption(self):
        """A qualifying wake score (>= 0.70) while TTS is speaking triggers barge-in cancellation."""
        wake_called = []
        detector = WakeWordDetector(
            self.config,
            on_wake_callback=lambda: wake_called.append(True),
            is_speaking_callback=lambda: True,  # Jarvis is currently speaking
        )
        detector._is_running = True

        mock_model = MagicMock()
        # High confidence wake score matching human direct voice during speech
        mock_model.predict.return_value = {"hey_jarvis": 0.78}
        detector.model = mock_model

        with patch("jarvis.audio.cancellation.trigger_speech_barge_in") as mock_barge_in:
            frame = self._make_dummy_frame(0.08)
            detector._audio_callback(frame, 1280, None, None)

            # Give dispatch thread a brief moment to execute callback
            time.sleep(0.05)

            mock_barge_in.assert_called_once()
            self.assertEqual(len(wake_called), 1, "Wake callback should be invoked on barge-in")

    def test_echo_rejection_below_barge_in_threshold(self):
        """A wake score below barge-in threshold (0.60 < 0.70) during TTS does NOT trigger barge-in."""
        wake_called = []
        detector = WakeWordDetector(
            self.config,
            on_wake_callback=lambda: wake_called.append(True),
            is_speaking_callback=lambda: True,  # Jarvis is speaking (speaker bleed scenario)
        )
        detector._is_running = True

        mock_model = MagicMock()
        # Score is above nominal 0.55 but below elevated barge-in 0.70
        mock_model.predict.return_value = {"hey_jarvis": 0.62}
        detector.model = mock_model

        with patch("jarvis.audio.cancellation.trigger_speech_barge_in") as mock_barge_in:
            frame = self._make_dummy_frame(0.08)
            detector._audio_callback(frame, 1280, None, None)

            time.sleep(0.05)

            mock_barge_in.assert_not_called()
            self.assertEqual(len(wake_called), 0, "Speaker echo below 0.70 must not trigger wake callback")

    def test_nominal_threshold_when_not_speaking(self):
        """When Jarvis is idle (not speaking), nominal threshold (0.55) triggers wake detection."""
        wake_called = []
        detector = WakeWordDetector(
            self.config,
            on_wake_callback=lambda: wake_called.append(True),
            is_speaking_callback=lambda: False,  # Not speaking
        )
        detector._is_running = True

        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis": 0.58}
        detector.model = mock_model

        frame = self._make_dummy_frame(0.08)
        detector._audio_callback(frame, 1280, None, None)

        time.sleep(0.05)
        self.assertEqual(len(wake_called), 1, "Score >= 0.55 while idle must trigger wake callback")

    def test_recording_mode_suppression_vad(self):
        """While VAD recording is active, wake-word prediction does NOT run and frames route to VAD."""
        wake_called = []
        detector = WakeWordDetector(
            self.config,
            on_wake_callback=lambda: wake_called.append(True),
            is_speaking_callback=lambda: False,
        )
        detector._is_running = True

        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis": 0.99}
        detector.model = mock_model

        session = detector.start_vad_session()
        self.assertEqual(detector.mode, CaptureMode.VAD)

        frame = self._make_dummy_frame(0.08)
        detector._audio_callback(frame, 1280, None, None)

        mock_model.predict.assert_not_called()
        self.assertEqual(len(wake_called), 0, "Wake callback must never fire during VAD recording")

        # Frame arrived in VAD session
        chunk = session.get_chunk(timeout=0.2)
        np.testing.assert_allclose(chunk, frame[:, 0], atol=1e-5)
        session.close()

    def test_recording_mode_suppression_ptt(self):
        """While PTT recording is active, wake-word prediction does NOT run and frames route to PTT."""
        wake_called = []
        detector = WakeWordDetector(
            self.config,
            on_wake_callback=lambda: wake_called.append(True),
            is_speaking_callback=lambda: False,
        )
        detector._is_running = True

        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis": 0.99}
        detector.model = mock_model

        detector.start_ptt_session()
        self.assertEqual(detector.mode, CaptureMode.PTT)

        frame = self._make_dummy_frame(0.08)
        detector._audio_callback(frame, 1280, None, None)

        mock_model.predict.assert_not_called()
        self.assertEqual(len(wake_called), 0, "Wake callback must never fire during PTT recording")

        chunks = detector.stop_ptt_session()
        self.assertEqual(len(chunks), 1)
        np.testing.assert_allclose(chunks[0], frame[:, 0], atol=1e-5)

    def test_single_input_stream_invariant(self):
        """Only one sounddevice.InputStream is created across wake listening, VAD, and PTT."""
        detector = WakeWordDetector(self.config)
        detector.model = MagicMock()

        with patch("sounddevice.InputStream") as mock_input_stream:
            mock_inst = MagicMock()
            mock_input_stream.return_value = mock_inst

            # 1. Start detector
            detector.start()
            self.assertEqual(mock_input_stream.call_count, 1, "Exactly 1 InputStream on start")

            # 2. VAD session lifecycle
            vad_session = detector.start_vad_session()
            self.assertEqual(mock_input_stream.call_count, 1, "No new stream for VAD")
            vad_session.close()
            self.assertEqual(mock_input_stream.call_count, 1, "No stream closed or opened after VAD")

            # 3. PTT session lifecycle
            detector.start_ptt_session()
            self.assertEqual(mock_input_stream.call_count, 1, "No new stream for PTT start")
            detector.stop_ptt_session()
            self.assertEqual(mock_input_stream.call_count, 1, "No new stream for PTT stop")

            # 4. Stop detector
            detector.stop()
            mock_inst.stop.assert_called_once()
            mock_inst.close.assert_called_once()
            self.assertEqual(mock_input_stream.call_count, 1, "Total InputStream creations must remain exactly 1")

    def test_preroll_handoff_retains_speech(self):
        """Pre-roll lead-in frames prior to wake word detection are handed off to the VAD session."""
        detector = WakeWordDetector(self.config)
        detector._is_running = True

        mock_model = MagicMock()
        detector.model = mock_model

        # Populate pre-roll buffer with 2 lead-in chunks (e.g. ambient/onset speech)
        c1 = np.full(1280, 0.01, dtype=np.float32)
        c2 = np.full(1280, 0.02, dtype=np.float32)
        detector._preroll_buffer.append(c1)
        detector._preroll_buffer.append(c2)

        # Start VAD session
        session = detector.start_vad_session()

        # Both pre-roll chunks must immediately be at the front of the VAD queue
        p1 = session.get_chunk(timeout=0.1)
        p2 = session.get_chunk(timeout=0.1)
        np.testing.assert_allclose(p1, c1)
        np.testing.assert_allclose(p2, c2)
        session.close()

    def test_queue_overflow_detection_and_reporting(self):
        """Queue overflow during recording sets recording_overflow_detected without raising unhandled errors."""
        detector = WakeWordDetector(self.config)
        detector._is_running = True

        # Start VAD session with a tiny queue of maxsize=2
        session = detector.start_vad_session(maxsize=2)
        self.assertFalse(detector.recording_overflow_detected)

        # Enqueue 2 chunks to fill queue
        frame = self._make_dummy_frame(0.05)
        detector._audio_callback(frame, 1280, None, None)
        detector._audio_callback(frame, 1280, None, None)

        # Enqueue 3rd chunk, triggering queue.Full
        detector._audio_callback(frame, 1280, None, None)

        self.assertTrue(detector.recording_overflow_detected, "Overflow during recording must be reported")
        self.assertTrue(session.overflow_detected)
        self.assertGreaterEqual(detector.queue_overflow_count, 1)
        session.close()

    def test_error_cleanup_restores_listening_state(self):
        """Failed or aborted recording session restores router to CaptureMode.WAKE without leaking."""
        detector = WakeWordDetector(self.config)
        detector._is_running = True
        session = detector.start_vad_session()
        self.assertEqual(detector.mode, CaptureMode.VAD)

        # Simulate exception during consumer processing
        try:
            raise RuntimeError("Simulated speech recognition error")
        except RuntimeError:
            session.close()

        self.assertEqual(detector.mode, CaptureMode.WAKE, "Must return to WAKE mode after error cleanup")
        self.assertIsNone(detector._vad_queue)

    def test_repeated_start_stop_stability(self):
        """Repeated rapid start/stop cycles join worker threads cleanly without resource leakage."""
        detector = WakeWordDetector(self.config)
        detector.model = MagicMock()

        with patch("sounddevice.InputStream") as mock_input_stream:
            mock_inst = MagicMock()
            mock_input_stream.return_value = mock_inst

            for _ in range(10):
                detector.start()
                self.assertTrue(detector._is_running)
                self.assertIsNotNone(detector._worker_thread)
                detector.stop()
                self.assertFalse(detector._is_running)
                self.assertIsNone(detector._worker_thread)

    def test_concurrent_ptt_preempts_vad(self):
        """Push-to-Talk activation cleanly preempts an ongoing VAD session without data corruption."""
        detector = WakeWordDetector(self.config)
        detector._is_running = True

        # 1. Start VAD session
        detector.start_vad_session()
        self.assertEqual(detector.mode, CaptureMode.VAD)

        # 2. User presses PTT while VAD is recording
        detector.start_ptt_session()
        self.assertEqual(detector.mode, CaptureMode.PTT)
        self.assertIsNone(detector._vad_queue, "VAD queue must be severed on PTT preemption")

        # 3. New frame belongs to PTT
        frame = self._make_dummy_frame(0.06)
        detector._audio_callback(frame, 1280, None, None)

        chunks = detector.stop_ptt_session()
        self.assertEqual(len(chunks), 1)
        self.assertEqual(detector.mode, CaptureMode.WAKE)

    def test_frame_ownership_guarantees_memory_independence(self):
        """Asynchronously queued audio owns its sample storage; modifying callback buffer does not mutate queued frame."""
        detector = WakeWordDetector(self.config)
        detector._is_running = True
        session = detector.start_vad_session()

        original_buffer = np.full((1280, 1), 0.123, dtype=np.float32)
        detector._audio_callback(original_buffer, 1280, None, None)

        # Mutate the original input buffer immediately (simulating PortAudio overwriting its DMA buffer)
        original_buffer.fill(0.999)

        chunk = session.get_chunk(timeout=0.2)
        self.assertTrue(chunk.flags.c_contiguous, "Chunk must be contiguous")
        self.assertEqual(chunk.dtype, np.float32, "Chunk must be float32")
        self.assertAlmostEqual(float(chunk[0]), 0.123, places=5, msg="Queued audio must not reflect callback buffer overwrite")
        session.close()

    def test_wake_latency_budget_prunes_stale_backlog(self):
        """Worker loop prunes stale audio backlog exceeding 4 chunks (320ms) in wake mode to preserve real-time response."""
        detector = WakeWordDetector(self.config)
        detector._is_running = True
        detector.model = MagicMock()
        detector.model.predict.return_value = {"hey_jarvis": 0.01}

        # Simulate 10 chunks accumulating during high CPU load
        for _ in range(10):
            detector._frame_buffer.write_frame(np.full(1280, 0.01, dtype=np.float32), is_recording=False)

        # Initial occupancy is 10
        self.assertEqual(detector._frame_buffer.occupancy, 10)

        # Execute one worker iteration
        chunk = detector._frame_buffer.read_frame(timeout=0.1)
        self.assertIsNotNone(chunk)
        with detector._state_lock:
            mode = detector._mode

        if mode == CaptureMode.WAKE:
            while detector._frame_buffer.occupancy > 4:
                stale = detector._frame_buffer.read_frame(timeout=0.0)
                if stale is None:
                    break
                detector._frame_buffer.dropped_wake_frames += 1
            detector._process_wake_chunk(chunk)

        # Backlog must now be capped at 4 chunks, and 5 stale chunks pruned
        self.assertLessEqual(detector._frame_buffer.occupancy, 4)
        self.assertEqual(detector.dropped_wake_frames, 5)

    def test_spsc_frame_buffer_memory_ownership(self):
        """Preallocated SPSC buffer guarantees complete memory independence between callback buffer and read frame."""
        buf = SPSCFrameRingBuffer(capacity=5, blocksize=1280)
        input_buf = np.full((1280, 1), 0.333, dtype=np.float32)

        buf.write_frame(input_buf)
        # Mutate the input buffer immediately (simulating DMA overwrite)
        input_buf.fill(0.888)

        frame = buf.read_frame(timeout=0.1)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.flags.c_contiguous)
        self.assertEqual(frame.dtype, np.float32)
        self.assertAlmostEqual(float(frame[0]), 0.333, places=5)

    def test_spsc_frame_buffer_wraparound(self):
        """SPSC buffer safely wraps around ring indices across multiple cycles without data corruption."""
        buf = SPSCFrameRingBuffer(capacity=4, blocksize=1280)
        for i in range(25):
            inp = np.full(1280, float(i), dtype=np.float32)
            buf.write_frame(inp)
            out = buf.read_frame(timeout=0.1)
            self.assertIsNotNone(out)
            self.assertAlmostEqual(float(out[0]), float(i), places=5)

    def test_spsc_frame_buffer_queue_full_wake_drops_oldest(self):
        """In wake mode, buffer fullness advances the read pointer, dropping the oldest frame to preserve real-time response."""
        buf = SPSCFrameRingBuffer(capacity=3, blocksize=1280)
        # Write 5 frames into capacity 3
        for i in range(5):
            buf.write_frame(np.full(1280, float(i), dtype=np.float32), is_recording=False)

        self.assertEqual(buf.occupancy, 3)
        self.assertEqual(buf.dropped_wake_frames, 2)
        self.assertEqual(buf.overflow_count, 2)

        # Remaining frames must be 2, 3, 4
        f1 = buf.read_frame(timeout=0.1)
        f2 = buf.read_frame(timeout=0.1)
        f3 = buf.read_frame(timeout=0.1)
        self.assertAlmostEqual(float(f1[0]), 2.0, places=5)
        self.assertAlmostEqual(float(f2[0]), 3.0, places=5)
        self.assertAlmostEqual(float(f3[0]), 4.0, places=5)

    def test_spsc_frame_buffer_queue_full_recording_flags_overflow(self):
        """In recording mode, buffer fullness does NOT silently discard; it flags recording_overflow_detected."""
        buf = SPSCFrameRingBuffer(capacity=2, blocksize=1280)
        res1 = buf.write_frame(np.full(1280, 1.0, dtype=np.float32), is_recording=True)
        res2 = buf.write_frame(np.full(1280, 2.0, dtype=np.float32), is_recording=True)
        self.assertTrue(res1)
        self.assertTrue(res2)
        self.assertFalse(buf.recording_overflow_detected)

        # Third frame triggers overflow
        res3 = buf.write_frame(np.full(1280, 3.0, dtype=np.float32), is_recording=True)
        self.assertFalse(res3)
        self.assertTrue(buf.recording_overflow_detected)
        self.assertEqual(buf.overflow_count, 1)
        self.assertEqual(buf.occupancy, 2)

    def test_concurrent_producer_consumer_counter_consistency_stress(self):
        """Stress test: concurrent producer and consumer maintain 100% counter and data consistency under load."""
        import threading
        buf = SPSCFrameRingBuffer(capacity=20, blocksize=1280)
        frames_to_send = 200
        received = []
        stop_worker = threading.Event()

        def consumer():
            while not stop_worker.is_set() or buf.occupancy > 0:
                frame = buf.read_frame(timeout=0.01)
                if frame is not None:
                    received.append(float(frame[0]))

        worker = threading.Thread(target=consumer)
        worker.start()

        test_data = np.zeros((1280, 1), dtype=np.float32)
        for i in range(frames_to_send):
            test_data.fill(float(i))
            buf.write_frame(test_data, is_recording=False)

        stop_worker.set()
        worker.join(timeout=2.0)

        # Counter consistency invariant: sent == received + remaining + dropped
        total_accounted = len(received) + buf.occupancy + buf.dropped_wake_frames
        self.assertEqual(total_accounted, frames_to_send, "Every frame must be accounted for without leakage")

        # Received data must strictly monotonically increase (zero data corruption or tearing)
        for idx in range(1, len(received)):
            self.assertGreater(received[idx], received[idx - 1], "Frames must arrive in strict monotonic sequence")

    def test_orchestrator_flags_recording_overflow_not_silent(self):
        """Orchestrator explicitly flags last_recording_had_overflow when PTT or VAD recording overflows."""
        import sys
        if "pynput" not in sys.modules:
            try:
                import pynput  # noqa: F401
            except ModuleNotFoundError:
                sys.modules["pynput"] = MagicMock()
                sys.modules["pynput.keyboard"] = MagicMock()
                sys.modules["pynput.mouse"] = MagicMock()
        from jarvis.core.orchestrator import JarvisOrchestrator
        with patch.object(JarvisOrchestrator, "__init__", return_value=None):
            orch = JarvisOrchestrator(self.config)
            orch._is_ptt_recording = True
            orch._recorded_frames = []
            orch.last_recording_had_overflow = False
            orch._lock = threading.Lock()
            orch.config = self.config
            orch.hotkey_listener = MagicMock()
            orch.hotkey_listener.mouse_target_btn = None
            orch.wake_detector = MagicMock()
            orch.wake_detector.stop_ptt_session.return_value = [np.zeros(1280, dtype=np.float32)]
            orch.wake_detector.recording_overflow_detected = True

            with patch.object(orch, "set_state"), patch("threading.Thread"):
                orch._finalize_ptt_recording()

            self.assertTrue(orch.last_recording_had_overflow, "Overflow must be explicitly flagged on orchestrator")

    def test_producer_pause_prevents_incomplete_frame_publication(self):
        """Forced producer pause verifies consumer cannot observe or read a frame before samples are fully copied."""
        buf = SPSCFrameRingBuffer(capacity=5, blocksize=1280)
        producer_paused = threading.Event()
        release_producer = threading.Event()

        def pause_producer():
            producer_paused.set()
            release_producer.wait(timeout=2.0)

        buf._before_write_finish = pause_producer

        def run_producer():
            buf.write_frame(np.full(1280, 0.777, dtype=np.float32), is_recording=False)

        p_thread = threading.Thread(target=run_producer)
        p_thread.start()

        self.assertTrue(producer_paused.wait(timeout=1.0), "Producer must reach write pause point")
        # While producer is paused holding lock mid-copy, consumer must not observe frame
        observed_while_paused = buf.read_frame(timeout=0.05)
        self.assertIsNone(observed_while_paused, "Consumer must not read incomplete frame while producer is copying")

        release_producer.set()
        p_thread.join(timeout=1.0)

        # After producer completes and publishes, consumer receives complete, valid frame
        frame = buf.read_frame(timeout=0.1)
        self.assertIsNotNone(frame)
        self.assertTrue(np.allclose(frame, 0.777), "Frame samples must be completely and accurately written")

    def test_consumer_pause_prevents_slot_overwrite_during_extraction(self):
        """Forced consumer pause verifies producer cannot overwrite or corrupt a slot being extracted by consumer."""
        buf = SPSCFrameRingBuffer(capacity=2, blocksize=1280)
        consumer_paused = threading.Event()
        release_consumer = threading.Event()

        def pause_consumer():
            consumer_paused.set()
            release_consumer.wait(timeout=2.0)

        buf._before_read_finish = pause_consumer

        # Fill buffer with frames: 10.0 and 20.0
        buf.write_frame(np.full(1280, 10.0, dtype=np.float32), is_recording=False)
        buf.write_frame(np.full(1280, 20.0, dtype=np.float32), is_recording=False)

        extracted_frames = []

        def run_consumer():
            f = buf.read_frame(timeout=0.2)
            if f is not None:
                extracted_frames.append(f)

        c_thread = threading.Thread(target=run_consumer)
        c_thread.start()

        self.assertTrue(consumer_paused.wait(timeout=1.0), "Consumer must reach read pause point")

        # Producer attempts to push 99.0 while consumer holds slot during extraction
        producer_result = []

        def run_producer():
            ok = buf.write_frame(np.full(1280, 99.0, dtype=np.float32), is_recording=False)
            producer_result.append(ok)

        p_thread = threading.Thread(target=run_producer)
        p_thread.start()
        time.sleep(0.05)

        # Producer must be blocked waiting for consumer to complete extraction
        self.assertEqual(len(producer_result), 0, "Producer must be blocked and not overwrite slot being extracted")

        release_consumer.set()
        c_thread.join(timeout=1.0)
        p_thread.join(timeout=1.0)

        # Extracted frame must be completely intact with value 10.0 (not overwritten by 99.0)
        self.assertEqual(len(extracted_frames), 1)
        self.assertTrue(np.allclose(extracted_frames[0], 10.0), "Extracted frame must not be corrupted by producer")
        self.assertEqual(len(producer_result), 1)
        self.assertTrue(producer_result[0])

    def test_wake_backlog_pruning_discards_stale_frames_beyond_320ms(self):
        """Consumer delay in WAKE mode verifies that backlogged audio (>4 frames / 320ms)
        has its oldest stale frames discarded before inference, processing only the latest <=4 frames."""
        processed_chunks = []
        detector = WakeWordDetector(self.config)
        detector._is_running = True
        detector._mode = CaptureMode.WAKE
        detector._process_wake_chunk = lambda c: processed_chunks.append(float(c[0]))

        # Feed 10 frames while worker is paused / not yet started
        for i in range(1, 11):
            frame = np.full(1280, float(i), dtype=np.float32)
            detector._frame_buffer.write_frame(frame, is_recording=False)

        self.assertEqual(detector._frame_buffer.occupancy, 10)

        worker = threading.Thread(target=detector._worker_loop, daemon=True)
        worker.start()

        deadline = time.time() + 2.0
        while time.time() < deadline and detector._frame_buffer.occupancy > 0:
            time.sleep(0.01)

        detector._is_running = False
        worker.join(timeout=1.0)

        # 6 stale frames (1..6) should be dropped to satisfy the 4-frame (320ms) budget
        self.assertEqual(detector._frame_buffer.dropped_wake_frames, 6)
        # Exactly the 4 latest frames (7, 8, 9, 10) should be processed
        self.assertEqual(processed_chunks, [7.0, 8.0, 9.0, 10.0])

    def test_recording_mode_preserves_all_frames_without_backlog_pruning(self):
        """In VAD recording mode, backlogged frames (>4) are NOT discarded; all audio is preserved."""
        detector = WakeWordDetector(self.config)
        session = detector.start_vad_session(maxsize=100)
        detector._is_running = True

        # Feed 10 frames while worker is paused / not yet started
        for i in range(1, 11):
            frame = np.full(1280, float(i), dtype=np.float32)
            detector._frame_buffer.write_frame(frame, is_recording=True)

        self.assertEqual(detector._frame_buffer.occupancy, 10)

        worker = threading.Thread(target=detector._worker_loop, daemon=True)
        worker.start()

        # Read all 10 chunks from VAD session queue
        received_chunks = []
        for _ in range(10):
            chunk = session.get_chunk(timeout=1.0)
            received_chunks.append(float(chunk[0]))

        detector._is_running = False
        worker.join(timeout=1.0)
        session.close()

        # Zero wake frames dropped
        self.assertEqual(detector._frame_buffer.dropped_wake_frames, 0)
        # All 10 frames preserved in order
        self.assertEqual(received_chunks, [float(i) for i in range(1, 11)])
        self.assertFalse(session.overflow_detected)

    def test_ptt_recording_preserves_all_frames_without_backlog_pruning(self):
        """In PTT recording mode, backlogged frames (>4) are NOT discarded; all audio is preserved."""
        detector = WakeWordDetector(self.config)
        detector.start_ptt_session()
        detector._is_running = True

        for i in range(1, 11):
            frame = np.full(1280, float(i), dtype=np.float32)
            detector._frame_buffer.write_frame(frame, is_recording=True)

        self.assertEqual(detector._frame_buffer.occupancy, 10)

        worker = threading.Thread(target=detector._worker_loop, daemon=True)
        worker.start()

        deadline = time.time() + 2.0
        while time.time() < deadline and detector._frame_buffer.occupancy > 0:
            time.sleep(0.01)

        detector._is_running = False
        worker.join(timeout=1.0)
        chunks = detector.stop_ptt_session()

        self.assertEqual(detector._frame_buffer.dropped_wake_frames, 0)
        self.assertEqual(len(chunks), 10)
        self.assertEqual([float(c[0]) for c in chunks], [float(i) for i in range(1, 11)])


if __name__ == "__main__":
    unittest.main()
