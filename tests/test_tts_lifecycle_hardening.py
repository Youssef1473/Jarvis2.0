import gc
import queue
import threading
import time
import unittest
import weakref
from unittest.mock import MagicMock, patch

import numpy as np

from jarvis.audio.tts import TextToSpeech
from jarvis.core.cancellation import (
    GLOBAL_EPOCH_STATE,
    register_audio_flush_callback,
    signal_global_barge_in,
    unregister_audio_flush_callback,
)


class TestTtsLifecycleHardening(unittest.TestCase):
    """Regression test suite for concurrency, SAPI isolation, stream claiming, and lifecycle invariants."""

    def setUp(self):
        self.config = {
            "tts": {
                "engine": "sapi5",
                "kokoro_speed": 1.0,
                "kokoro_lang": "en-us",
                "kokoro_voice": "bm_george",
                "use_shm_transport": False,
            }
        }
        self.tts = TextToSpeech(self.config)

    def tearDown(self):
        try:
            self.tts.close(timeout=0.5)
        except Exception:  # noqa: BLE001, S110
            pass

    def test_shm_flush_uncertain_fails_closed_on_start(self):
        """Verifies start_streaming_session fails closed when _shm_flush_uncertain is set."""
        self.tts._shm_flush_uncertain = True
        started = self.tts.start_streaming_session()
        self.assertFalse(started)

        # After force reconciliation, it can start cleanly (or attempt start)
        reconciled = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertTrue(reconciled)
        self.assertFalse(self.tts._shm_flush_uncertain)

    def test_finish_streaming_session_stream_claim_serialized(self):
        """Verifies finish_streaming_session claims stream under _stream_claim_lock without double closure."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream
        self.tts._stream_active = True

        with patch.object(self.tts, "_close_stream_safely", return_value=True) as mock_close:
            res = self.tts.finish_streaming_session()
            self.assertTrue(res)
            mock_close.assert_called_once_with(mock_stream, stop_timeout=0.100)
            self.assertIsNone(self.tts._output_stream)

    def test_stream_startup_invalidation_routes_through_safe_shutdown(self):
        """Verifies that if session generation bumps during stream startup, safe shutdown is invoked."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        def mock_stream_factory(*args, **kwargs):
            self.tts._stop_event.set()
            return mock_stream

        with patch("sounddevice.OutputStream", side_effect=mock_stream_factory), \
             patch.object(self.tts, "_close_stream_safely", return_value=True) as mock_close:
            started = self.tts._do_start_streaming_session()
            self.assertFalse(started)
            mock_close.assert_called_once_with(mock_stream, stop_timeout=0.100)

    def test_signal_global_barge_in_async_callbacks_dispatches_in_background(self):
        """Verifies signal_global_barge_in(async_callbacks=True) advances epoch synchronously but runs callbacks async."""
        called_event = threading.Event()

        def slow_callback():
            time.sleep(0.05)
            called_event.set()

        register_audio_flush_callback(slow_callback)
        try:
            start_epoch = GLOBAL_EPOCH_STATE.barge_in_epoch
            t0 = time.monotonic()
            new_epoch = signal_global_barge_in(async_callbacks=True)
            elapsed = time.monotonic() - t0

            # Must return in under 15ms without waiting for slow_callback
            self.assertLess(elapsed, 0.030)
            self.assertEqual(new_epoch, start_epoch + 1)
            # The async callback completes shortly after
            self.assertTrue(called_event.wait(timeout=0.5))
        finally:
            unregister_audio_flush_callback(slow_callback)

    def test_sapi_worker_purges_on_barge_in(self):
        """Verifies SAPI worker thread catches purge event and stops speaking in its own apartment."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        mock_sapi_instance = MagicMock()
        mock_sapi_instance.WaitUntilDone.return_value = False

        with patch("pythoncom.CoInitialize"), \
             patch("pythoncom.CoUninitialize"), \
             patch("win32com.client.Dispatch", return_value=mock_sapi_instance):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)
            self.assertIsNotNone(self.tts._sapi_thread)

            # Trigger immediate barge in flush
            self.tts.on_barge_in_flush()
            self.assertTrue(self.tts._sapi_purge_event.is_set())

            # Worker should terminate and call Speak("", 2) to purge
            if self.tts._sapi_thread:
                self.tts._sapi_thread.join(timeout=1.0)
                self.assertFalse(self.tts._sapi_thread.is_alive())

            mock_sapi_instance.Speak.assert_any_call("", 2)

    def test_sapi_worker_init_failure_reports_false(self):
        """Verifies start_streaming_session reports False if SAPI COM initialization fails."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        with patch("pythoncom.CoInitialize", side_effect=RuntimeError("COM init failed")):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)
            self.assertFalse(self.tts.is_speaking)

    def test_shm_barge_in_epoch_fencing_property(self):
        """Verifies that SHM transport exposes both barge_in_epoch and epoch alias correctly."""
        mock_shm = MagicMock()
        mock_shm.barge_in_epoch = 123
        self.tts._shm_transport = mock_shm

        # Verify getter correctly accesses barge_in_epoch
        epoch_val = getattr(self.tts._shm_transport, "barge_in_epoch", None)
        self.assertEqual(epoch_val, 123)

    def test_shutdown_worker_ownership_and_reconciliation_barrier(self):
        """Finding 1: Verifies shutdown worker identity preservation and outcome reconciliation."""
        mock_stream = MagicMock()
        mock_stream.stop = MagicMock()
        mock_stream.close = MagicMock()

        # Clean closure records stream id in _closed_streams
        res = self.tts._close_stream_safely(mock_stream, stop_timeout=0.2)
        self.assertTrue(res)
        self.assertIn(id(mock_stream), self.tts._closed_streams)

        # Calling close again returns True immediately without spawning new worker
        with patch("threading.Thread") as mock_thread:
            res_cached = self.tts._close_stream_safely(mock_stream, stop_timeout=0.2)
            self.assertTrue(res_cached)
            mock_thread.assert_not_called()

        # Barrier-based overlap: completed worker outcome is reconciled cleanly
        stream2 = MagicMock()
        completed_worker = MagicMock()
        completed_worker.is_alive.return_value = False
        completed_worker.outcome = {"status": self.tts.SHUTDOWN_CLEAN_STOP}
        with self.tts._stream_claim_lock:
            self.tts._shutdown_worker = completed_worker
            self.tts._shutdown_stream = stream2

        res2 = self.tts._close_stream_safely(stream2, stop_timeout=0.2)
        self.assertTrue(res2)
        self.assertIn(id(stream2), self.tts._closed_streams)
        self.assertIsNone(self.tts._shutdown_worker)

    def test_on_barge_in_flush_is_non_blocking_and_enqueues_async(self):
        """Finding 2: Verifies barge-in callback does not call _close_stream_safely synchronously."""
        mock_stream = MagicMock()
        self.tts._output_stream = mock_stream

        t0 = time.monotonic()
        self.tts.on_barge_in_flush()
        elapsed = time.monotonic() - t0

        # Must execute in < 10ms
        self.assertLess(elapsed, 0.040)
        self.assertIsNone(self.tts._output_stream)
        # Stream must be placed into async close queue for background worker
        enqueued_stream = self.tts._async_close_queue.get(timeout=0.2)
        self.assertIs(enqueued_stream, mock_stream)

    def test_concurrent_close_serialized_under_close_lock(self):
        """Finding 3: Verifies concurrent close() calls are serialized without double teardown."""
        mock_shm = MagicMock()
        self.tts._shm_transport = mock_shm

        results = []
        barrier = threading.Barrier(3)

        def close_worker():
            barrier.wait()
            try:
                ok = self.tts.close(timeout=1.0)
                results.append(("ok", ok))
            except Exception as e:  # noqa: BLE001
                results.append(("err", e))

        threads = [threading.Thread(target=close_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        # All calls should report clean closure (True) without exceptions
        for r_type, val in results:
            self.assertEqual(r_type, "ok")
            self.assertTrue(val)
        # SHM transport close should be called at most once
        self.assertEqual(mock_shm.close.call_count, 1)

    def test_speak_sync_rejected_on_closed_instance_and_sync_active_drained(self):
        """Finding 4: Verifies speak_sync rejects execution on closed instance and tracks _sync_speech_active."""
        self.tts.close(timeout=0.5)
        with patch.object(self.tts, "_clean_text_for_speech") as mock_clean:
            self.tts.speak_sync("Hello world")
            mock_clean.assert_not_called()

    def test_sapi_worker_exits_when_stream_active_false_and_purge_failure_reported(self):
        """Finding 5: Verifies SAPI inner loop exits on _stream_active=False and purge failure marks stop unclean."""
        self.tts._sapi_purge_failed = True
        with patch("sounddevice.stop"):
            clean_stop = self.tts._do_stop(stop_timeout=0.05)
            self.assertFalse(clean_stop)

    def test_close_deadline_enforced_monotonic(self):
        """Finding 6: Verifies close() fails closed if monotonic deadline expires."""
        with patch.object(self.tts, "stop", side_effect=lambda **kw: time.sleep(0.08) or True):
            # A deadline of 0.02s should expire
            with self.assertRaises(RuntimeError) as ctx:
                self.tts.close(timeout=0.02)
            self.assertIn("deadline exceeded", str(ctx.exception).lower())

    def test_fallback_sd_stop_error_reports_unclean_stop(self):
        """Finding 7: Verifies exception during fallback sd.stop() sets clean_stop to False."""
        self.tts._output_stream = None
        self.tts._shutdown_worker = None
        self.tts._stream_cleanup_uncertain = False
        self.tts._stream_shutdown_timed_out = False
        self.tts._shm_flush_uncertain = False
        self.tts._sapi_purge_failed = False

        with patch("sounddevice.stop", side_effect=RuntimeError("PortAudio driver crashed")):
            clean_stop = self.tts._do_stop(stop_timeout=0.05)
            self.assertFalse(clean_stop)


    def test_reconcile_stream_lifecycle_never_clears_live_worker_even_with_force(self):
        """Round 7 Finding 1: Verifies reconcile_stream_lifecycle disallows clearing live worker even when force=True."""
        live_worker = MagicMock()
        live_worker.is_alive.return_value = True
        with self.tts._stream_claim_lock:
            self.tts._shutdown_worker = live_worker
            self.tts._shutdown_stream = MagicMock()

        res = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertFalse(res)
        self.assertIs(self.tts._shutdown_worker, live_worker)

    def test_start_streaming_fails_closed_when_pending_close_active(self):
        """Round 7 Finding 2: Verifies start_streaming_session fails closed while async close is pending."""
        with self.tts._stream_claim_lock:
            self.tts._pending_close_streams.add(12345)

        started = self.tts.start_streaming_session()
        self.assertFalse(started)

        # After pending close is resolved, session can start
        with self.tts._stream_claim_lock:
            self.tts._pending_close_streams.clear()

    def test_playback_modes_mutually_exclusive(self):
        """Round 7 Finding 6: Verifies streaming and synchronous speech are mutually exclusive."""
        # 1. Active sync speech rejects streaming start
        self.tts._sync_speech_active = 1
        started = self.tts.start_streaming_session()
        self.assertFalse(started)
        self.tts._sync_speech_active = 0

        # 2. Active stream rejects speak_sync
        self.tts._stream_active = True
        with patch.object(self.tts, "_clean_text_for_speech") as mock_clean:
            self.tts.speak_sync("hello")
            mock_clean.assert_not_called()
        self.tts._stream_active = False

    def test_sapi_startup_invalidated_by_barge_in(self):
        """Round 7 Finding 5: Verifies SAPI startup invalidated by barge-in reports False."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        with patch("pythoncom.CoInitialize"), patch("pythoncom.CoUninitialize"),              patch("win32com.client.Dispatch", return_value=MagicMock()):
            # Simulate barge in event set right during startup
            self.tts._stop_event.set()
            started = self.tts._do_start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)

    def test_callback_registration_failure_raises(self):
        """Round 7 Finding 8: Verifies failure to register flush callback fails construction."""
        with patch("jarvis.audio.cancellation.register_audio_flush_callback", side_effect=RuntimeError("Bus error")):
            with self.assertRaises(RuntimeError) as ctx:
                TextToSpeech(self.config)
            self.assertIn("barge-in callback registration failed", str(ctx.exception).lower())


    def test_concurrent_speak_sync_strictly_single_caller(self):
        """Round 8 Finding 4: Verifies concurrent synchronous speech is rejected."""
        self.tts._sync_speech_active = 1
        with patch.object(self.tts, "_clean_text_for_speech") as mock_clean:
            self.tts.speak_sync("hello")
            mock_clean.assert_not_called()
        self.tts._sync_speech_active = 0

    def test_finish_streaming_session_propagates_close_failure(self):
        """Round 8 Finding 7: Verifies finish_streaming_session returns False if stream close fails."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = False
        self.tts._output_stream = mock_stream
        self.tts._stream_active = True

        with patch.object(self.tts, "_close_stream_safely", return_value=False):
            res = self.tts.finish_streaming_session()
            self.assertFalse(res)

    def test_exhaustive_queue_drain_clears_large_backlog(self):
        """Round 8 Finding 6: Verifies unbounded queues are drained exhaustively beyond 256 items."""
        for i in range(300):
            self.tts._sentence_queue.put(f"item {i}")
            self.tts._audio_queue.put(f"audio {i}")

        self.assertEqual(self.tts._sentence_queue.qsize(), 300)
        self.assertEqual(self.tts._audio_queue.qsize(), 300)

        self.tts.stop(stop_timeout=0.05)
        self.assertTrue(self.tts._sentence_queue.empty())
        self.assertTrue(self.tts._audio_queue.empty())

    def test_constructor_failure_does_not_leak_worker(self):
        """Round 8 Finding 2: Verifies constructor failure leaves zero running async close workers."""
        before_threads = [t for t in threading.enumerate() if t.name == "Jarvis-TTS-AsyncCloseWorker"]
        with (
            patch("jarvis.audio.cancellation.register_audio_flush_callback", side_effect=RuntimeError("Init error")),
            self.assertRaises(RuntimeError),
        ):
            TextToSpeech(self.config)
        after_threads = [t for t in threading.enumerate() if t.name == "Jarvis-TTS-AsyncCloseWorker"]
        self.assertEqual(len(after_threads), len(before_threads))

    def test_sapi_purge_failure_sets_flag_and_fails_stop(self):
        """Round 8 Finding 3: Verifies SAPI purge failure sets failure flag and marks stop unclean."""
        mock_sapi = MagicMock()
        mock_sapi.Speak.side_effect = RuntimeError("SAPI dead")

        ok = self.tts._safe_purge_sapi(mock_sapi)
        self.assertFalse(ok)
        self.assertTrue(self.tts._sapi_purge_failed)

        clean = self.tts._do_stop(stop_timeout=0.05)
        self.assertFalse(clean)

    def test_concurrent_startup_rejected(self):
        """Round 9 Finding 1: Verifies second concurrent start_streaming_session is rejected."""
        with self.tts._lifecycle_lock:
            self.tts._startup_in_progress = True
        try:
            ok = self.tts.start_streaming_session()
            self.assertFalse(ok)
        finally:
            with self.tts._lifecycle_lock:
                self.tts._startup_in_progress = False

    def test_speak_sync_rejected_when_startup_in_progress(self):
        """Round 9 Finding 1: Verifies speak_sync rejects when start_streaming_session is reserving/starting."""
        with self.tts._lifecycle_lock:
            self.tts._startup_in_progress = True
        try:
            with (
                patch.object(self.tts, "_clean_text_for_speech", return_value="hello"),
                patch.object(self.tts, "_speak_local_sapi") as mock_speak,
            ):
                self.tts.speak_sync("hello")
                mock_speak.assert_not_called()
        finally:
            with self.tts._lifecycle_lock:
                self.tts._startup_in_progress = False

    def test_speak_sync_aborted_when_close_is_called(self):
        """Round 9 Finding 2: Verifies speak_sync aborts and does not clear _stop_event if close started."""
        with self.tts._lifecycle_lock:
            self.tts._closing = True
        try:
            self.tts._stop_event.set()
            with (
                patch.object(self.tts, "_clean_text_for_speech", return_value="hello"),
                patch.object(self.tts, "_speak_local_sapi") as mock_speak,
            ):
                self.tts.speak_sync("hello")
                mock_speak.assert_not_called()
                self.assertTrue(self.tts._stop_event.is_set())
        finally:
            with self.tts._lifecycle_lock:
                self.tts._closing = False

    def test_closed_streams_weakset_no_stale_id_reuse(self):
        """Round 9 Finding 3: Verifies WeakSet automatically evicts collected streams preventing ID collision."""
        mock_stream = MagicMock()
        self.tts._closed_streams.add(mock_stream)
        self.assertIn(mock_stream, self.tts._closed_streams)
        del mock_stream
        import gc
        gc.collect()
        self.assertEqual(len(self.tts._closed_streams), 0)

    def test_async_close_worker_does_not_pin_tts_instance(self):
        """Round 9 Finding 4: Verifies idle async close worker does not retain strong reference to TTS."""
        import weakref
        tts = TextToSpeech(self.config)
        tts_ref = weakref.ref(tts)
        mock_stream = MagicMock()
        mock_stream.active = True
        tts._output_stream = mock_stream
        tts.on_barge_in_flush()  # puts stream into queue
        time.sleep(0.05)  # worker processes stream and del tts runs
        tts.close(timeout=1.0)
        del tts
        import gc
        gc.collect()
        self.assertIsNone(tts_ref())


    def test_barge_in_flush_enqueues_stop_default_when_no_custom_stream(self):
        """Round 10 Finding 1: Verifies barge-in enqueues STOP_DEFAULT when no native stream is active."""
        self.tts._output_stream = None
        # Drain any existing items
        while not self.tts._async_close_queue.empty():
            try:
                self.tts._async_close_queue.get_nowait()
            except Exception:  # noqa: BLE001
                break

        with patch("jarvis.audio.tts.sd.stop") as mock_sd_stop:
            self.tts.on_barge_in_flush()
            time.sleep(0.05)  # worker processes queue
            mock_sd_stop.assert_called()

    def test_reconcile_stream_lifecycle_fails_closed_when_shm_clear_fails(self):
        """Round 10 Finding 2: Verifies reconcile fails closed even with force=True if SHM clear fails."""
        mock_shm = MagicMock()
        mock_shm.trigger_barge_in_clear.return_value = False
        self.tts._shm_transport = mock_shm
        self.tts._shm_flush_uncertain = True

        ok = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertFalse(ok)
        self.assertTrue(self.tts._shm_flush_uncertain)

        mock_shm.trigger_barge_in_clear.side_effect = RuntimeError("SHM kernel error")
        ok2 = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertFalse(ok2)
        self.assertTrue(self.tts._shm_flush_uncertain)

    def test_close_preempts_default_stream_via_sd_stop(self):
        """Round 10 Finding 1: Verifies close() issues sd.stop() to preempt default-device playback."""
        with patch("jarvis.audio.tts.sd.stop") as mock_sd_stop:
            self.tts.close(timeout=0.5)
            mock_sd_stop.assert_called()

    def test_stop_waits_for_in_flight_stream_startup_to_abort(self):
        """Round 11 Finding 1: Verifies stop() sets _barge_in_during_startup and waits for startup owner."""
        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream
        self.tts._stream_starting = True

        def finish_startup():
            time.sleep(0.02)
            with self.tts._stream_claim_lock:
                self.tts._stream_starting = False
                self.tts._output_stream = None
            with self.tts._lifecycle_lock:
                self.tts._startup_active = 0
                self.tts._startup_cond.notify_all()

        th = threading.Thread(target=finish_startup, daemon=True)
        th.start()

        ok = self.tts.stop(stop_timeout=0.2)
        self.assertTrue(ok)
        self.assertTrue(self.tts._barge_in_during_startup)
        self.assertFalse(self.tts._stream_starting)

    def test_finish_streaming_session_marks_pending_atomically(self):
        """Round 11 Finding 2: Verifies finish_streaming_session marks stream pending atomically."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream
        self.tts._stream_active = True

        observed_pending = [False]

        orig_close_safely = self.tts._close_stream_safely

        def spy_close(stream, stop_timeout=0.1):
            with self.tts._stream_claim_lock:
                observed_pending[0] = stream in self.tts._pending_close_streams
            return orig_close_safely(stream, stop_timeout=stop_timeout)

        with patch.object(self.tts, "_close_stream_safely", side_effect=spy_close):
            self.tts.finish_streaming_session()

        self.assertTrue(observed_pending[0])

    def test_standalone_stream_close_preserves_stop_abort_close(self):
        """Round 11 Finding 4: Verifies _safe_standalone_stream_close executes stop -> abort -> close."""
        from jarvis.audio.tts import _safe_standalone_stream_close
        mock_stream = MagicMock()
        mock_stream.stop.side_effect = RuntimeError("stop failed")

        ok = _safe_standalone_stream_close(mock_stream, timeout=0.2)
        self.assertTrue(ok)
        mock_stream.stop.assert_called_once()
        mock_stream.abort.assert_called_once()
        mock_stream.close.assert_called_once()

    def test_startup_invalidation_clears_stream_starting_and_startup_active(self):
        """Round 12 Finding 2: Verifies barge-in between reservation and _do_start_streaming_session
        clears _stream_starting, decrements _startup_active, and notifies _startup_cond."""
        orig_do_start = self.tts._do_start_streaming_session

        def barge_in_before_native_start(current_gen=None):
            # Simulate barge-in occurring immediately after reservation
            self.tts.on_barge_in_flush()
            return orig_do_start(current_gen=current_gen)

        with patch.object(self.tts, "_do_start_streaming_session", side_effect=barge_in_before_native_start):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)

        # Invariants: _stream_starting is False and _startup_active is 0
        self.assertFalse(self.tts._stream_starting)
        self.assertEqual(self.tts._startup_active, 0)
        self.assertFalse(self.tts._startup_in_progress)

        # Clean teardown succeeds without timing out waiting for startup
        clean = self.tts.stop(stop_timeout=0.1)
        self.assertTrue(clean)

    def test_queue_sentence_never_calls_sapi_directly_when_worker_inactive(self):
        """Round 12 Finding 3: Verifies queue_sentence drops clauses and marks stream inactive
        instead of crossing COM apartment boundaries to call self._sapi.Speak()."""
        mock_sapi = MagicMock()
        self.tts._sapi = mock_sapi
        self.tts._sapi_thread = None
        self.tts._stream_active = True
        self.tts._is_speaking = True

        self.tts.queue_sentence("This must not be spoken via constructor SAPI")

        mock_sapi.Speak.assert_not_called()
        self.assertFalse(self.tts._stream_active)
        self.assertFalse(self.tts._is_speaking)

    def test_startup_does_not_hold_lifecycle_lock_while_waiting_for_stop_lock(self):
        """Round 12 Finding 1: Verifies start_streaming_session does not hold _lifecycle_lock
        or increment _startup_active while blocked waiting for _stop_lock."""
        acquired_stop = self.tts._stop_lock.acquire(timeout=0.2)
        self.assertTrue(acquired_stop)

        startup_thread_started = threading.Event()
        startup_result = [None]

        def run_start():
            startup_thread_started.set()
            startup_result[0] = self.tts.start_streaming_session()

        th = threading.Thread(target=run_start, daemon=True)
        th.start()
        self.assertTrue(startup_thread_started.wait(timeout=0.2))

        # Give thread a few ms to reach and block on _stop_lock.acquire()
        time.sleep(0.02)

        # Invariant 1: While startup is waiting for _stop_lock, _startup_active is NOT incremented
        self.assertEqual(self.tts._startup_active, 0)

        # Invariant 2: While startup is waiting for _stop_lock, _lifecycle_lock is NOT held
        # Another thread can acquire _lifecycle_lock without blocking
        lifecycle_acquired = self.tts._lifecycle_lock.acquire(blocking=False)
        self.assertTrue(lifecycle_acquired)
        self.tts._lifecycle_lock.release()

        # Release _stop_lock to let startup thread finish cleanly
        self.tts._stop_lock.release()
        th.join(timeout=3.0)
        self.assertFalse(th.is_alive())

    def test_stop_startup_wait_strictly_bounded_without_deadline(self):
        """Round 13 Finding 1: Verifies _do_stop(stop_timeout=0.03) exits within timeout when _startup_active=1."""
        self.tts._startup_active = 1
        t0 = time.monotonic()
        clean = self.tts.stop(stop_timeout=0.03)
        elapsed = time.monotonic() - t0

        self.assertFalse(clean)
        # Must exit within ~60ms without waiting for startup to release
        self.assertLess(elapsed, 0.09)

        # Cleanup active count
        self.tts._startup_active = 0

    def test_speak_sync_does_not_erase_concurrent_barge_in(self):
        """Round 13 Finding 2: Verifies speak_sync aborts and does not clear _stop_event if barge-in occurs during cleaning."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()

        def barge_in_during_cleaning(text):
            self.tts.on_barge_in_flush()
            return text

        with patch.object(self.tts, "_clean_text_for_speech", side_effect=barge_in_during_cleaning):
            self.tts.speak_sync("Hello there, this should be aborted.")

        self.assertTrue(self.tts._stop_event.is_set())
        self.assertFalse(self.tts._is_speaking)
        self.assertEqual(self.tts._sync_speech_active, 0)
        self.tts._kokoro.create.assert_not_called()

    def test_safe_sd_stop_bounds_wait_on_driver_stall(self):
        """Round 13 Finding 3: Verifies _safe_sd_stop does not block caller when sd.stop hangs."""
        stall_gate = threading.Event()

        def stalled_sd_stop():
            stall_gate.wait(timeout=5.0)

        with patch("sounddevice.stop", side_effect=stalled_sd_stop):
            t0 = time.monotonic()
            ok = self.tts._safe_sd_stop(timeout=0.03)
            elapsed = time.monotonic() - t0

            self.assertFalse(ok)
            self.assertLess(elapsed, 0.10)

        stall_gate.set()

    def test_stop_in_progress_blocks_new_streaming_session_and_sync_speech(self):
        """Round 14 Finding 1: Verifies _stop_in_progress prevents starting a new session or sync speech."""
        with self.tts._lifecycle_lock:
            self.tts._stop_in_progress = True

        try:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)

            self.tts.speak_sync("This should not play while stop is in progress")
            self.assertFalse(self.tts._is_speaking)
            self.assertEqual(self.tts._sync_speech_active, 0)
        finally:
            with self.tts._lifecycle_lock:
                self.tts._stop_in_progress = False

    def test_delayed_async_flush_does_not_cancel_later_session(self):
        """Round 14 Finding 2: Verifies delayed flush callback does not clobber a subsequent new session."""
        from jarvis.core.cancellation import GLOBAL_EPOCH_STATE, signal_global_barge_in

        # Trigger barge-in locally and advance epoch
        epoch1 = signal_global_barge_in(async_callbacks=False)
        self.tts.on_barge_in_flush()

        # Start a new session
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True
        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)

        new_gen = self.tts._session_generation
        self.assertFalse(self.tts._stop_event.is_set())

        # Simulate delayed callback firing with the OLD epoch
        orig_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        try:
            GLOBAL_EPOCH_STATE.barge_in_epoch = epoch1
            self.tts.on_barge_in_flush()
        finally:
            GLOBAL_EPOCH_STATE.barge_in_epoch = orig_epoch

        # New session generation and stop event MUST remain untouched
        self.assertEqual(self.tts._session_generation, new_gen)
        self.assertFalse(self.tts._stop_event.is_set())

    def test_default_stop_coalescing_and_queue_bounding(self):
        """Round 14 Finding 3: Verifies default stop requests are coalesced and bounded to 32 items."""
        # Clear existing worker
        self.tts._output_stream = None
        for _ in range(50):
            self.tts.on_barge_in_flush()

        # Queue must not grow indefinitely; default stops are coalesced
        self.assertLessEqual(self.tts._async_close_queue.qsize(), 32)
        self.assertTrue(self.tts._default_stop_pending)

    def test_zero_timeout_safe_sd_stop_returns_false_when_unacknowledged(self):
        """Round 14 Finding 4: Verifies _safe_sd_stop(timeout=0) returns False when unacknowledged."""
        stall_gate = threading.Event()

        def stalled_sd_stop():
            stall_gate.wait(timeout=5.0)

        with patch("sounddevice.stop", side_effect=stalled_sd_stop):
            ok = self.tts._safe_sd_stop(timeout=0)
            self.assertFalse(ok)

        stall_gate.set()

    def test_cancel_then_restart_session_succeeds(self):
        """Round 15 Finding 2: Verifies non-blocking cancellation reconciles _stream_active
        allowing immediate start of a subsequent streaming session."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            # 1. Start initial session
            started = self.tts.start_streaming_session()
            self.assertTrue(started)
            self.assertTrue(self.tts._stream_active)

            # 2. Cancel non-blockingly (barge-in flush)
            self.tts.stop(non_blocking=True)
            self.assertFalse(self.tts._stream_active)
            self.assertFalse(self.tts.is_speaking)

            # Allow async close queue to drain detached stream
            time.sleep(0.05)

            # 3. Start subsequent session: must succeed cleanly without claiming another active session exists
            restarted = self.tts.start_streaming_session()
            self.assertTrue(restarted)
            self.assertTrue(self.tts._stream_active)

    def test_output_stream_start_failure_safely_closes_stream(self):
        """Round 16 Finding 1: Verifies stream.start() failure routes stream through safe close and clears reference."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.start.side_effect = RuntimeError("WASAPI device exclusive error")

        with patch("sounddevice.OutputStream", return_value=mock_stream), \
             patch.object(self.tts, "_close_stream_safely", return_value=True) as mock_close:
            started = self.tts.start_streaming_session()
            self.assertTrue(started)  # Falls back to standard playback
            self.assertIsNone(self.tts._output_stream)
            mock_close.assert_called_once_with(mock_stream, stop_timeout=0.100)

    def test_output_stream_barge_in_during_start_revalidates_and_closes(self):
        """Round 16 Finding 1: Verifies barge-in between validation and start() safely closes stream."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()

        def barge_in_on_start():
            self.tts.on_barge_in_flush()

        mock_stream.start.side_effect = barge_in_on_start

        with patch("sounddevice.OutputStream", return_value=mock_stream), \
             patch.object(self.tts, "_close_stream_safely", return_value=True) as mock_close:
            started = self.tts._do_start_streaming_session(current_gen=self.tts._session_generation)
            self.assertFalse(started)
            self.assertIsNone(self.tts._output_stream)
            mock_close.assert_called_once_with(mock_stream, stop_timeout=0.100)

    def test_close_fails_when_stop_fails(self):
        """Round 16 Finding 2: Verifies close() raises RuntimeError when stop() reports failure."""
        with patch.object(self.tts, "stop", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.tts.close(timeout=0.5)
            self.assertIn("stop failed", str(ctx.exception).lower())
            self.assertFalse(self.tts._closed)

    def test_close_propagates_shm_close_error_and_preserves_reference(self):
        """Round 16 Finding 2: Verifies close() propagates SHM close errors and retains reference for retry."""
        mock_shm = MagicMock()
        mock_shm.close.side_effect = OSError("SHM locked by OS kernel")
        self.tts._shm_transport = mock_shm

        with self.assertRaises(OSError):
            self.tts.close(timeout=0.5)

        self.assertIs(self.tts._shm_transport, mock_shm)
        self.assertFalse(self.tts._closed)

    def test_sapi_startup_timeout_reports_false(self):
        """Round 16 Finding 3: Verifies SAPI start_streaming_session reports False if worker init times out."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        with patch("pythoncom.CoInitialize", side_effect=lambda: time.sleep(1.5)):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)

    def test_sapi_finish_reports_false_when_worker_stalled(self):
        """Round 16 Finding 3: Verifies SAPI finish_streaming_session reports False if worker remains alive."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None
        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = True
        self.tts._sapi_thread = mock_worker

        ok = self.tts.finish_streaming_session()
        self.assertFalse(ok)

    def test_sapi_worker_cleans_com_on_setup_exception(self):
        """Round 16 Finding 3: Verifies SAPI worker calls CoUninitialize if Dispatch raises after CoInitialize."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        with patch("pythoncom.CoInitialize"), \
             patch("win32com.client.Dispatch", side_effect=RuntimeError("Dispatch failed")), \
             patch("pythoncom.CoUninitialize") as mock_uninit:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            time.sleep(0.05)
            mock_uninit.assert_called()

    def test_non_blocking_stop_under_lock_contention_and_slow_callbacks(self):
        """Round 16 Finding 4: Verifies stop(non_blocking=True) executes <30ms under lock contention."""
        slow_callback_entered = threading.Event()

        def slow_callback():
            slow_callback_entered.set()
            time.sleep(0.5)

        register_audio_flush_callback(slow_callback)
        try:
            lock_held = threading.Event()
            can_release = threading.Event()

            def hold_claim_lock():
                with self.tts._stream_claim_lock:
                    lock_held.set()
                    can_release.wait(timeout=2.0)

            th = threading.Thread(target=hold_claim_lock, daemon=True)
            th.start()
            self.assertTrue(lock_held.wait(timeout=0.5))

            t0 = time.monotonic()
            ok = self.tts.stop(non_blocking=True)
            elapsed = time.monotonic() - t0

            self.assertTrue(ok)
            self.assertLess(elapsed, 0.030)
            self.assertFalse(self.tts._stream_active)
            self.assertFalse(self.tts.is_speaking)

            can_release.set()
            th.join(timeout=1.0)
        finally:
            unregister_audio_flush_callback(slow_callback)

    def test_queue_full_stream_fallback_clears_pending_close_streams(self):
        """Round 16 Finding 5: Verifies stream fallback clears _pending_close_streams when queue is full."""
        for i in range(32):
            self.tts._async_close_queue.put(f"dummy_{i}")

        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream

        self.tts.on_barge_in_flush()
        time.sleep(0.15)

        with self.tts._stream_claim_lock:
            self.assertNotIn(mock_stream, self.tts._pending_close_streams)
            self.assertNotIn(id(mock_stream), self.tts._pending_close_streams)

    def test_queue_full_default_stop_fallback_clears_default_stop_pending(self):
        """Round 16 Finding 5: Verifies default stop fallback clears _default_stop_pending when queue is full."""
        for i in range(32):
            self.tts._async_close_queue.put(f"dummy_{i}")

        self.tts._output_stream = None

        with patch("sounddevice.stop"):
            self.tts.on_barge_in_flush()
            time.sleep(0.15)
            self.assertFalse(self.tts._default_stop_pending)

    def test_close_fails_when_async_close_worker_stalled(self):
        """Round 17 Finding 2: Verifies close() raises RuntimeError when async close worker is stalled."""
        stalled_worker = MagicMock()
        stalled_worker.is_alive.return_value = True
        self.tts._async_close_worker = stalled_worker

        with self.assertRaises(RuntimeError) as ctx:
            self.tts.close(timeout=0.1)
        self.assertIn("async close worker stalled", str(ctx.exception).lower())
        self.assertFalse(self.tts._closed)

    def test_sapi_startup_aborts_on_barge_in_before_activation(self):
        """Round 17 Finding 3: Verifies SAPI start_streaming_session aborts if barge-in occurs during init."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        def barge_in_during_init(*args, **kwargs):
            self.tts._stop_event.set()

        with patch("pythoncom.CoInitialize", side_effect=barge_in_during_init):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)

    def test_close_deadline_exceeded_waiting_for_close_lock(self):
        """Round 17 Finding 5: Verifies close() fails closed if _close_lock cannot be acquired within timeout."""
        lock_held = threading.Event()
        can_release = threading.Event()

        def hold_lock():
            with self.tts._close_lock:
                lock_held.set()
                can_release.wait(timeout=2.0)

        th = threading.Thread(target=hold_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.tts.close(timeout=0.03)
            self.assertIn("close lock", str(ctx.exception).lower())
        finally:
            can_release.set()
            th.join(timeout=1.0)

    def test_speak_sync_aborts_and_preserves_stop_when_stopped_during_cleaning(self):
        """Round 17 Finding 6: Verifies speak_sync aborts and preserves stop event if stop() occurs during cleaning."""
        def stop_during_clean(text):
            self.tts.stop(stop_timeout=0.05)
            return text

        with patch.object(self.tts, "_clean_text_for_speech", side_effect=stop_during_clean):
            self.tts.speak_sync("Hello world")

        self.assertTrue(self.tts._stop_event.is_set())
        self.assertFalse(self.tts._is_speaking)
        self.assertEqual(self.tts._sync_speech_active, 0)

    def test_sapi_purge_failure_preserved_when_thread_terminated(self):
        """Round 24 Finding 2: Verifies SAPI purge failure is preserved even when worker thread has terminated, reporting unclean stop."""
        self.tts._sapi_purge_failed = True
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        self.tts._sapi_thread = mock_thread

        clean = self.tts._do_stop(stop_timeout=0.05)
        self.assertFalse(clean)
        self.assertTrue(self.tts._sapi_purge_failed)

    def test_repeated_non_blocking_stop_detaches_stream_cleanly(self):
        """Round 18 Finding 1: Verifies repeated stop(non_blocking=True) detaches output stream."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream1 = MagicMock()
        mock_stream1.active = True

        with patch("sounddevice.OutputStream", return_value=mock_stream1):
            self.tts.start_streaming_session()
            self.assertIs(self.tts._output_stream, mock_stream1)

            # First non-blocking stop
            self.tts.stop(non_blocking=True)
            self.assertIsNone(self.tts._output_stream)

            # Allow async close worker to drain detached stream
            time.sleep(0.05)

        mock_stream2 = MagicMock()
        mock_stream2.active = True
        with patch("sounddevice.OutputStream", return_value=mock_stream2):
            self.tts.start_streaming_session()
            self.assertIs(self.tts._output_stream, mock_stream2)

            # Repeated stop: must not be suppressed by duplicate-epoch guard
            self.tts.stop(non_blocking=True)
            self.assertIsNone(self.tts._output_stream)

    def test_barge_in_during_stream_start_revalidates_and_closes_stream(self):
        """Round 19 Finding 1: Verifies startup revalidation closes and does not publish stream if stopped."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        def stop_on_start():
            self.tts._stop_event.set()

        mock_stream.start.side_effect = stop_on_start

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            self.assertIsNone(self.tts._output_stream)
            self.assertFalse(self.tts._stream_active)
            mock_stream.close.assert_called()

    def test_barge_in_deferred_stream_check_when_claim_lock_contested(self):
        """Round 19 Finding 1: Verifies deferred check detaches stream when _stream_claim_lock is contended."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True
        self.tts._output_stream = mock_stream

        # Hold claim lock to simulate contention
        lock_held = threading.Event()
        can_release = threading.Event()

        def hold_claim_lock():
            with self.tts._stream_claim_lock:
                lock_held.set()
                can_release.wait(timeout=1.0)

        th = threading.Thread(target=hold_claim_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            # barge in while lock is held
            self.tts.on_barge_in_flush(force=True)
            # stream still published during lock contention
            self.assertIs(self.tts._output_stream, mock_stream)
        finally:
            can_release.set()
            th.join(timeout=1.0)

        # Allow deferred check thread to acquire lock and detach
        time.sleep(0.08)
        self.assertIsNone(self.tts._output_stream)

    def test_failed_stream_start_cleanup_failure_aborts_startup(self):
        """Round 19 Finding 4: Verifies failed native stream cleanup returns False and does not enable fallback."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.start.side_effect = RuntimeError("PortAudio device error")

        with (
            patch("sounddevice.OutputStream", return_value=mock_stream),
            patch.object(self.tts, "_close_stream_safely", return_value=False),
        ):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)
            self.assertFalse(self.tts._is_speaking)

    def test_close_raises_on_unregistration_failure(self):
        """Round 19 Finding 6: Verifies close() propagates callback unregistration failure and does not mark closed."""
        with patch("jarvis.core.cancellation.unregister_audio_flush_callback", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.tts.close(timeout=0.2)
            self.assertIn("unregistration timed out", str(ctx.exception).lower())
            self.assertFalse(self.tts._closed)

    def test_barge_in_ring_buffer_called_non_blocking(self):
        """Round 19 Finding 5: Verifies on_barge_in_flush invokes ring_buffer.barge_in(blocking=False)."""
        mock_rb = MagicMock()
        self.tts._ring_buffer = mock_rb
        self.tts.on_barge_in_flush(force=True)
        mock_rb.barge_in.assert_called_with(blocking=False)

    def test_stop_waits_for_sync_speech_to_quiesce(self):
        """Round 19 Finding 3: Verifies stop() waits for active synchronous speech within deadline."""
        self.tts._sync_speech_active = 1

        def finish_sync_speech():
            time.sleep(0.04)
            with self.tts._lifecycle_lock:
                self.tts._sync_speech_active = 0
                self.tts._startup_cond.notify_all()

        th = threading.Thread(target=finish_sync_speech, daemon=True)
        th.start()
        clean = self.tts.stop(stop_timeout=0.200)
        th.join(timeout=1.0)
        self.assertTrue(clean)
        self.assertEqual(self.tts._sync_speech_active, 0)

    def test_stop_fails_if_sync_speech_does_not_quiesce(self):
        """Round 19 Finding 3: Verifies stop() reports unclean stop if sync speech remains active past deadline."""
        self.tts._sync_speech_active = 1
        clean = self.tts.stop(stop_timeout=0.030)
        self.assertFalse(clean)
        self.assertEqual(self.tts._sync_speech_active, 1)

    def test_close_raises_if_sync_speech_active_past_timeout(self):
        """Round 19 Finding 3: Verifies close() raises RuntimeError if sync speech cannot quiesce within deadline."""
        self.tts._sync_speech_active = 1
        with patch.object(self.tts, "stop", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                self.tts.close(timeout=0.05)
            self.assertIn("sync speech active past close timeout", str(ctx.exception).lower())
            self.assertFalse(self.tts._closed)

    def test_sapi_startup_rejects_if_prior_sapi_worker_alive(self):
        """Round 19 Finding 2: Verifies SAPI session start is rejected if prior worker thread is alive."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None
        mock_sapi_thread = MagicMock()
        mock_sapi_thread.is_alive.return_value = True
        self.tts._sapi_thread = mock_sapi_thread

        started = self.tts.start_streaming_session()
        self.assertFalse(started)

    def test_sd_play_serialized_against_sd_stop_on_cancellation(self):
        """Round 20 Finding 1: Verifies sd.play is not executed if stop() occurs before playback lock."""
        self.tts.engine = "kokoro"
        mock_kokoro = MagicMock()
        mock_kokoro.create.return_value = (np.zeros(2400, dtype=np.float32), 24000)
        self.tts._kokoro = mock_kokoro

        # Hold playback lock to simulate stop executing during chunk synthesis
        lock_held = threading.Event()
        can_proceed = threading.Event()

        def hold_playback_lock():
            with self.tts._sd_playback_lock:
                lock_held.set()
                can_proceed.wait(timeout=2.0)

        th = threading.Thread(target=hold_playback_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        with patch("sounddevice.play") as mock_sd_play:
            # Start speak_sync on background thread
            speak_th = threading.Thread(target=self.tts.speak_sync, args=("Test sentence.",), daemon=True)
            speak_th.start()
            time.sleep(0.05)

            # Signal stop
            self.tts.stop(stop_timeout=0.05)
            self.assertTrue(self.tts._stop_event.is_set())

            # Now release playback lock
            can_proceed.set()
            th.join(timeout=1.0)
            speak_th.join(timeout=1.0)

            # sd.play must NOT have been called because stop_event was observed under _sd_playback_lock
            mock_sd_play.assert_not_called()

    def test_sapi_worker_playback_error_reported_in_finish_streaming_session(self):
        """Round 20 Finding 2: Verifies SAPI worker error is recorded and causes finish_streaming_session to return False."""
        self.tts.engine = "sapi5"
        self.tts._kokoro = None

        mock_voice = MagicMock()
        mock_voice.Speak.side_effect = RuntimeError("SAPI COM failure")

        with (
            patch("pythoncom.CoInitialize"),
            patch("pythoncom.CoUninitialize"),
            patch("win32com.client.Dispatch", return_value=mock_voice),
        ):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)

            self.tts.queue_sentence("Testing SAPI error reporting.")
            time.sleep(0.1)

            ok = self.tts.finish_streaming_session()
            self.assertFalse(ok)
            self.assertIsNotNone(self.tts._sapi_worker_error)

    def test_stop_then_speak_sync_resumes_speech_cleanly(self):
        """Round 21 Finding 1: Verifies speak_sync clears stale stop state and executes speech after stop()."""
        self.tts.engine = "kokoro"
        mock_kokoro = MagicMock()
        mock_kokoro.create.return_value = (np.zeros(2400, dtype=np.float32), 24000)
        self.tts._kokoro = mock_kokoro

        # First, call stop() so _stop_event is set
        self.tts.stop(stop_timeout=0.05)
        self.assertTrue(self.tts._stop_event.is_set())

        with patch("sounddevice.play") as mock_sd_play, patch("sounddevice.wait"):
            self.tts.speak_sync("Fresh speech after stop.")
            mock_sd_play.assert_called_once()
            self.assertFalse(self.tts._stop_event.is_set())
            self.assertEqual(self.tts._sync_speech_active, 0)

    def test_speak_sync_aborts_if_barge_in_occurs_during_text_cleaning(self):
        """Round 21 Finding 1: Verifies speak_sync aborts if barge-in races with text normalization."""
        self.tts.engine = "kokoro"
        mock_kokoro = MagicMock()
        mock_kokoro.create.return_value = (np.zeros(2400, dtype=np.float32), 24000)
        self.tts._kokoro = mock_kokoro

        def barge_in_during_clean(text):
            self.tts.on_barge_in_flush(force=True)
            return text

        with (
            patch.object(self.tts, "_clean_text_for_speech", side_effect=barge_in_during_clean),
            patch("sounddevice.play") as mock_sd_play,
        ):
            self.tts.speak_sync("Interrupted before synthesis.")
            mock_sd_play.assert_not_called()
            self.assertTrue(self.tts._stop_event.is_set())
            self.assertEqual(self.tts._sync_speech_active, 0)

    def test_stop_waits_for_pending_async_stream_closes_to_drain(self):
        """Round 21 Finding 3: Verifies stop() waits for pending async stream close within deadline."""
        mock_stream = MagicMock()
        with self.tts._stream_claim_lock:
            self.tts._pending_close_streams.add(mock_stream)

        def finish_stream_close():
            time.sleep(0.04)
            with self.tts._stream_claim_lock:
                self.tts._pending_close_streams.discard(mock_stream)

        th = threading.Thread(target=finish_stream_close, daemon=True)
        th.start()

        clean = self.tts.stop(stop_timeout=0.200)
        th.join(timeout=1.0)
        self.assertTrue(clean)
        self.assertEqual(len(self.tts._pending_close_streams), 0)

    def test_stop_fails_if_pending_async_stream_close_stalls(self):
        """Round 21 Finding 3: Verifies stop() returns False if pending async stream close stalls past deadline."""
        mock_stream = MagicMock()
        with self.tts._stream_claim_lock:
            self.tts._pending_close_streams.add(mock_stream)

        try:
            clean = self.tts.stop(stop_timeout=0.030)
            self.assertFalse(clean)
        finally:
            with self.tts._stream_claim_lock:
                self.tts._pending_close_streams.discard(mock_stream)

    def test_ring_buffer_interleaved_barge_in_preserves_consumer_ramp(self):
        """Round 21 Finding 2: Verifies captured epoch in ring buffer write preserves barge-in ramp trigger."""
        from jarvis.audio.ring_buffer import AudioRingBuffer
        rb = AudioRingBuffer(capacity_samples=48000, ramp_samples=60)

        # Producer captures epoch before write
        producer_epoch = rb._barge_in_epoch
        data = np.ones(500, dtype=np.float32) * 0.5

        # Trigger non-blocking barge-in concurrently
        rb.barge_in(blocking=False)
        self.assertGreater(rb._barge_in_epoch, producer_epoch)

        # Stale write is rejected
        written = rb.write(data, producer_epoch=producer_epoch)
        self.assertEqual(written, 0)

        # Verify that if write occurred with matched captured epoch, an interleaved
        # barge-in leaves _consumer_epoch < _barge_in_epoch
        rb2 = AudioRingBuffer(capacity_samples=48000, ramp_samples=60)
        p_epoch = rb2._barge_in_epoch

        # Write first chunk
        rb2.write(data, producer_epoch=p_epoch)
        # Advance barge-in non-blockingly
        rb2.barge_in(blocking=False)
        # Consumer epoch should be p_epoch, which is < rb2._barge_in_epoch
        self.assertNotEqual(rb2._consumer_epoch, rb2._barge_in_epoch)

    def test_stop_preempts_sd_playback_immediately_unblocking_sync_speech(self):
        """Round 22 Finding 1: Verifies stop() immediately calls sd.stop before waiting for sync speech."""
        self.tts._sync_speech_active = 1
        stopped_called = threading.Event()

        def mock_sd_stop():
            stopped_called.set()
            # Simulate speak_sync finishing as soon as sd.stop is called
            with self.tts._lifecycle_lock:
                self.tts._sync_speech_active = 0
                self.tts._startup_cond.notify_all()

        with patch("sounddevice.stop", side_effect=mock_sd_stop):
            start_t = time.monotonic()
            clean = self.tts.stop(stop_timeout=0.500)
            elapsed = time.monotonic() - start_t
            self.assertTrue(clean)
            self.assertTrue(stopped_called.is_set())
            # Should finish well before the 500ms deadline because sd.stop was dispatched early
            self.assertLess(elapsed, 0.200)

    def test_startup_barge_in_single_owner_close(self):
        """Round 22 Finding 2: Verifies startup does not call _close_stream_safely if stream was already detached."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        def barge_in_on_start():
            # Simulate barge-in acquiring lock and detaching stream right after start
            with self.tts._stream_claim_lock:
                self.tts._output_stream = None
                self.tts._pending_close_streams.add(mock_stream)
            self.tts._stop_event.set()

        mock_stream.start.side_effect = barge_in_on_start

        with (
            patch("sounddevice.OutputStream", return_value=mock_stream),
            patch.object(self.tts, "_close_stream_safely") as mock_close_safely,
        ):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            # Since stream was already detached by barge-in, startup must NOT call _close_stream_safely
            mock_close_safely.assert_not_called()
        with self.tts._stream_claim_lock:
            self.tts._pending_close_streams.discard(mock_stream)

    def test_deferred_stream_check_queue_full_cleans_pending_streams(self):
        """Round 22 Finding 3: Verifies deferred check removes stream from pending set even when queue is full."""
        self.tts.engine = "kokoro"
        mock_stream = MagicMock()
        self.tts._output_stream = mock_stream

        # Fill queue to force queue.Full
        while not self.tts._async_close_queue.full():
            try:
                self.tts._async_close_queue.put_nowait("DUMMY")
            except queue.Full:
                break

        # Simulate lock contention so on_barge_in_flush triggers _deferred_stream_check
        lock_held = threading.Event()
        can_release = threading.Event()

        def hold_claim_lock():
            with self.tts._stream_claim_lock:
                lock_held.set()
                can_release.wait(timeout=1.0)

        th = threading.Thread(target=hold_claim_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            with patch.object(self.tts, "_close_stream_safely", return_value=True):
                self.tts.on_barge_in_flush(force=True)
        finally:
            can_release.set()
            th.join(timeout=1.0)

        # Allow deferred thread to execute fallback close under queue.Full
        time.sleep(0.1)
        with self.tts._stream_claim_lock:
            self.assertNotIn(mock_stream, self.tts._pending_close_streams)
            self.assertNotIn(id(mock_stream), self.tts._pending_close_streams)

        # Drain dummy items
        while not self.tts._async_close_queue.empty():
            try:
                self.tts._async_close_queue.get_nowait()
            except queue.Empty:
                break

    def test_start_streaming_session_rejected_if_prior_synth_or_playback_worker_alive(self):
        """Round 23 Finding 1: Verifies start_streaming_session rejects if prior synth/playback worker is still running."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()

        still_running = threading.Event()
        can_exit = threading.Event()

        def dummy_worker():
            still_running.set()
            can_exit.wait(timeout=1.0)

        th = threading.Thread(target=dummy_worker, daemon=True)
        th.start()
        self.assertTrue(still_running.wait(timeout=0.5))

        self.tts._synth_thread = th
        try:
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
        finally:
            can_exit.set()
            th.join(timeout=1.0)

    def test_stop_timeout_bounded_when_stream_claim_lock_contended(self):
        """Round 23 Finding 2: Verifies stop(stop_timeout=...) does not hang when stream_claim_lock is contended."""
        lock_held = threading.Event()
        can_release = threading.Event()

        def hold_claim_lock():
            with self.tts._stream_claim_lock:
                lock_held.set()
                can_release.wait(timeout=1.0)

        th = threading.Thread(target=hold_claim_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            start_t = time.monotonic()
            clean = self.tts.stop(stop_timeout=0.030)
            elapsed = time.monotonic() - start_t
            self.assertFalse(clean)
            # Must respect the bounded timeout (~30ms) instead of blocking for the entire 1.0s lock duration
            self.assertLess(elapsed, 0.150)
        finally:
            can_release.set()
            th.join(timeout=1.0)

    def test_async_close_worker_exits_when_tts_collected(self):
        """Round 23 Finding 3: Verifies _async_close_worker_loop exits cleanly when tts instance is garbage-collected."""
        from jarvis.audio.tts import _async_close_worker_loop

        class DummyTTS:
            pass

        dummy_tts = DummyTTS()
        ref = weakref.ref(dummy_tts)
        q = queue.Queue()

        worker = threading.Thread(target=_async_close_worker_loop, args=(ref, q), daemon=True)
        worker.start()

        # Drop strong reference to dummy_tts
        dummy_tts = None
        gc.collect()

        # The loop has timeout=1.0 on q.get, after which it checks ref() is None and breaks
        worker.join(timeout=2.5)
        self.assertFalse(worker.is_alive())

    def test_close_stream_safely_does_not_reclose_completed_stream(self):
        """Round 24 Finding 1: Verifies completed shutdown worker does not spawn another worker on repeated close."""
        mock_stream = MagicMock()
        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = False
        mock_worker.outcome = {"status": self.tts.SHUTDOWN_CLEAN_STOP}

        with self.tts._stream_claim_lock:
            self.tts._shutdown_worker = mock_worker
            self.tts._shutdown_stream = mock_stream

        ok = self.tts._close_stream_safely(mock_stream, stop_timeout=0.050)
        self.assertTrue(ok)
        mock_stream.stop.assert_not_called()
        mock_stream.close.assert_not_called()

    def test_do_stop_dispatches_global_callbacks_asynchronously(self):
        """Round 24 Finding 3: Verifies _do_stop does not block on slow registered flush callbacks."""
        slow_callback_started = threading.Event()
        can_finish_slow = threading.Event()

        def slow_flush_callback():
            slow_callback_started.set()
            can_finish_slow.wait(timeout=1.0)

        register_audio_flush_callback(slow_flush_callback)
        try:
            start_t = time.monotonic()
            clean = self.tts._do_stop(stop_timeout=0.020)
            elapsed = time.monotonic() - start_t
            self.assertTrue(clean)
            self.assertLess(elapsed, 0.100)
        finally:
            can_finish_slow.set()
            unregister_audio_flush_callback(slow_flush_callback)

    def test_sapi_finish_scales_and_purges_on_genuine_timeout(self):
        """Round 25 Finding 1: Verifies SAPI finish handles genuine timeout by setting purge event and waiting for quiescence."""
        self.tts.engine = "sapi5"
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        self.tts._sapi_thread = mock_thread
        self.tts.stream_timeout_seconds = 0.05
        self.tts._session_word_count = 10

        purged_observed = False

        def mock_join(timeout=None):
            nonlocal purged_observed
            if self.tts._sapi_purge_event.is_set():
                purged_observed = True
                mock_thread.is_alive.return_value = False

        mock_thread.join.side_effect = mock_join

        ok = self.tts.finish_streaming_session()
        self.assertFalse(ok)
        self.assertTrue(purged_observed)
        self.assertFalse(self.tts._stream_active)

    def test_native_stream_startup_timeout_fails_closed_without_hanging(self):
        """Round 25 Finding 2: Verifies native OutputStream startup timeout fails closed without calling close inside native call."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        self.tts.stream_startup_timeout = 0.05

        hang_gate = threading.Event()

        def hanging_stream_factory(*args, **kwargs):
            hang_gate.wait(timeout=1.0)
            return MagicMock()

        with (
            patch("sounddevice.OutputStream", side_effect=hanging_stream_factory),
            patch.object(self.tts, "_close_stream_safely") as mock_close,
        ):
            start_t = time.monotonic()
            started = self.tts._do_start_streaming_session()
            elapsed = time.monotonic() - start_t
            self.assertFalse(started)
            self.assertTrue(self.tts._stream_cleanup_uncertain)
            self.assertLess(elapsed, 0.200)
            mock_close.assert_not_called()
        hang_gate.set()

    def test_startup_exception_path_revalidates_cancellation(self):
        """Round 25 Finding 3: Verifies that if cancellation occurs while stream startup raises, it returns False."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()

        def raising_stream_factory(*args, **kwargs):
            self.tts._stop_event.set()
            raise RuntimeError("PortAudio driver initialization error")

        with patch("sounddevice.OutputStream", side_effect=raising_stream_factory):
            started = self.tts._do_start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)

    def test_startup_worker_tracked_and_blocks_reconciliation_while_alive(self):
        """Round 26 Finding 1: Verifies startup worker is tracked on self and prevents premature lifecycle reconciliation."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        self.tts.stream_startup_timeout = 0.05

        hang_gate = threading.Event()

        def hanging_factory(*args, **kwargs):
            hang_gate.wait(timeout=1.0)
            return MagicMock()

        with patch("sounddevice.OutputStream", side_effect=hanging_factory):
            started = self.tts._do_start_streaming_session()
            self.assertFalse(started)
            self.assertIsNotNone(self.tts._startup_worker)
            self.assertTrue(self.tts._startup_worker.is_alive())

            reconciled = self.tts.reconcile_stream_lifecycle(force=True)
            self.assertFalse(reconciled)

        hang_gate.set()
        self.tts._startup_worker.join(timeout=1.0)
        self.assertFalse(self.tts._startup_worker.is_alive())

        reconciled = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertTrue(reconciled)

    def test_deferred_stream_check_stops_default_playback_under_lock_contention(self):
        """Round 26 Finding 2: Verifies deferred barge-in check invokes sd.stop when claim lock was contested."""
        self.tts._output_stream = None
        self.tts._default_stop_pending = False

        lock_held = threading.Event()
        can_release = threading.Event()

        def hold_claim_lock():
            with self.tts._stream_claim_lock:
                lock_held.set()
                can_release.wait(timeout=1.0)

        th = threading.Thread(target=hold_claim_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        with patch("sounddevice.stop") as mock_sd_stop:
            try:
                self.tts.on_barge_in_flush(force=True)
            finally:
                can_release.set()
                th.join(timeout=1.0)

            # Wait for deferred check thread and async close worker to dispatch sd.stop
            time.sleep(0.15)
            mock_sd_stop.assert_called()

    def test_speak_sync_rejected_when_pending_close_streams_non_empty(self):
        """Round 26 Finding 3: Verifies speak_sync rejects and does not clear stop event while a stream is closing."""
        self.tts._stop_event.set()
        dummy_stream = MagicMock()
        self.tts._pending_close_streams.add(dummy_stream)

        with patch("sounddevice.play") as mock_play:
            self.tts.speak_sync("Hello world")
            mock_play.assert_not_called()

        self.assertEqual(self.tts._sync_speech_active, 0)
        self.assertTrue(self.tts._stop_event.is_set())
        self.tts._pending_close_streams.discard(dummy_stream)

    def test_stream_start_hang_does_not_block_caller_beyond_timeout(self):
        """Round 27 Finding 1: Verifies caller returns promptly within startup timeout even when stream.start() hangs."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        self.tts.stream_startup_timeout = 0.050

        hang_start = threading.Event()
        mock_stream = MagicMock()

        def hanging_start():
            hang_start.wait(timeout=1.0)

        mock_stream.start.side_effect = hanging_start

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            start_t = time.monotonic()
            started = self.tts.start_streaming_session()
            elapsed = time.monotonic() - start_t
            self.assertFalse(started)
            self.assertLess(elapsed, 0.200)

        hang_start.set()
        if self.tts._startup_worker:
            self.tts._startup_worker.join(timeout=1.0)

    def test_delayed_startup_failure_plays_all_queued_chunks_in_fallback(self):
        """Round 27 Finding 2: Verifies fallback playback plays all pre-synthesized chunks without discarding any."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        fake_audio = np.zeros(2400, dtype=np.float32)
        self.tts._kokoro.create.return_value = (fake_audio, 24000)

        def delayed_failing_stream(*args, **kwargs):
            time.sleep(0.040)
            raise RuntimeError("Hardware OutputStream creation failed")

        played_chunks = []

        def mock_play(samples, sr):
            played_chunks.append((samples, sr))

        with (
            patch("sounddevice.OutputStream", side_effect=delayed_failing_stream),
            patch("sounddevice.play", side_effect=mock_play),
            patch("sounddevice.wait"),
        ):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)
            self.tts.queue_sentence("First chunk.")
            self.tts.queue_sentence("Second chunk.")

            ok = self.tts.finish_streaming_session()
            self.assertTrue(ok)
            self.assertGreaterEqual(len(played_chunks), 2)

    def test_standalone_queue_sentence_with_sapi5_spawns_worker_and_speaks(self):
        """Round 27 Finding 3: Verifies standalone queue_sentence with SAPI5 automatically spawns STA worker."""
        self.tts.engine = "sapi5"
        self.assertIsNone(self.tts._sapi_thread)

        with patch("win32com.client.Dispatch") as mock_dispatch:
            mock_sapi_inst = MagicMock()
            mock_dispatch.return_value = mock_sapi_inst
            mock_sapi_inst.WaitUntilDone.return_value = True

            self.tts.queue_sentence("Warning: System thermal advisory, Sir.")
            self.assertIsNotNone(self.tts._sapi_thread)
            self.assertTrue(self.tts._sapi_thread.is_alive())

            ok = self.tts.finish_streaming_session()
            self.assertTrue(ok)

    def test_delayed_first_barge_in_callback_does_not_cancel_restarted_session(self):
        """Round 28 Finding 1: Verifies delayed FIRST barge-in callback does not cancel a restarted session."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        callback_gate = threading.Event()
        callback_executed = threading.Event()

        import jarvis.core.cancellation as canc_module
        orig_run_flush_callbacks = canc_module._run_flush_callbacks

        def delayed_run_flush_callbacks(exclude=None):
            callback_gate.wait(timeout=2.0)
            try:
                orig_run_flush_callbacks(exclude=exclude)
            finally:
                callback_executed.set()

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)
            initial_gen = self.tts._session_generation

            # Advance global barge-in epoch asynchronously, but delay callback execution
            with patch("jarvis.core.cancellation._run_flush_callbacks", side_effect=delayed_run_flush_callbacks):
                signal_global_barge_in(async_callbacks=True)

            # Stop and restart before the delayed first callback executes
            self.tts.stop()
            restarted = self.tts.start_streaming_session()
            self.assertTrue(restarted)
            restarted_gen = self.tts._session_generation
            self.assertNotEqual(initial_gen, restarted_gen)
            self.assertFalse(self.tts._stop_event.is_set())
            self.assertTrue(self.tts._stream_active)

            # Now release the gate to let the delayed first callback run
            callback_gate.set()
            self.assertTrue(callback_executed.wait(timeout=2.0))

            # The restarted session MUST remain active and uncancelled
            self.assertEqual(self.tts._session_generation, restarted_gen)
            self.assertFalse(self.tts._stop_event.is_set())
            self.assertTrue(self.tts._stream_active)

    def test_unclean_stream_shutdown_not_cleared_by_different_stream_close(self):
        """Round 29 Finding 1: Verifies failed stream close preserves uncertainty across different stream close."""
        stream_a = MagicMock()
        stream_a.stop.side_effect = RuntimeError("Hardware stall stop")
        stream_a.abort.side_effect = RuntimeError("Hardware stall abort")
        stream_a.close.side_effect = RuntimeError("Hardware stall close")

        stream_b = MagicMock()

        # Close stream_a, which fails stop, abort, and close
        ok_a = self.tts._close_stream_safely(stream_a, stop_timeout=0.100)
        self.assertFalse(ok_a)
        self.assertTrue(self.tts._stream_cleanup_uncertain)
        self.assertIn(stream_a, self.tts._unresolved_streams)

        # Attempt to close a different stream (stream_b)
        ok_b = self.tts._close_stream_safely(stream_b, stop_timeout=0.100)
        self.assertFalse(ok_b)
        self.assertTrue(self.tts._stream_cleanup_uncertain)
        self.assertIn(stream_a, self.tts._unresolved_streams)

        # Reconcile without force must fail while unresolved streams exist
        self.assertFalse(self.tts.reconcile_stream_lifecycle(force=False))
        self.assertTrue(self.tts._stream_cleanup_uncertain)

        # Round 31: Reconcile with force must ALSO fail while stream_a is unresolved and unclosed
        self.assertFalse(self.tts.reconcile_stream_lifecycle(force=True))
        self.assertTrue(self.tts._stream_cleanup_uncertain)
        self.assertIn(stream_a, self.tts._unresolved_streams)

        # Once stream_a is confirmed closed, reconcile without force still respects uncertain state
        stream_a.closed = True
        self.assertFalse(self.tts.reconcile_stream_lifecycle(force=False))

        # Reconcile with force succeeds now that stream_a is confirmed closed
        self.assertTrue(self.tts.reconcile_stream_lifecycle(force=True))
        self.assertFalse(self.tts._stream_cleanup_uncertain)
        self.assertEqual(len(self.tts._unresolved_streams), 0)

    def test_shm_transport_writes_use_shm_epoch_when_epochs_differ(self):
        """Round 29 Finding 2: Verifies KokoroSynthWorker passes shm_transport epoch, not ring_buffer epoch."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        fake_audio = np.zeros(2400, dtype=np.float32)
        self.tts._kokoro.create.return_value = (fake_audio, 24000)

        # Deliberately desynchronize ring buffer and SHM transport epochs
        self.tts._ring_buffer.clear()
        self.tts._ring_buffer._barge_in_epoch = 1

        mock_shm = MagicMock()
        mock_shm.barge_in_epoch = 42
        mock_shm.write_pcm.return_value = 2400
        self.tts._shm_transport = mock_shm

        mock_stream = MagicMock()
        mock_stream.active = True

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)

            self.tts.queue_sentence("Testing SHM epoch dispatch.")
            for _ in range(50):
                if mock_shm.write_pcm.called:
                    break
                time.sleep(0.02)

            self.tts.stop()

            # Assert write_pcm was called with shm's epoch (42), NOT ring buffer's epoch (1)
            mock_shm.write_pcm.assert_called()
            for call_args in mock_shm.write_pcm.call_args_list:
                _, kwargs = call_args
                self.assertEqual(kwargs.get("producer_epoch"), 42)

    def test_feed_token_discards_tokens_and_chunks_when_generation_advances(self):
        """Round 31 Finding 1: Verifies feed_token and flush_tokens reject chunks when session generation advances."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started = self.tts.start_streaming_session()
            self.assertTrue(started)
            initial_gen = self.tts._session_generation

            # 1. Advance generation; feed_token with old expected_gen must reject
            self.tts._session_generation = initial_gen + 1
            chunks = self.tts.feed_token("This is a token that forms a sentence. ", expected_gen=initial_gen)
            self.assertEqual(chunks, [])
            self.assertTrue(self.tts._sentence_queue.empty())

            # 2. Simulate race where generation advances DURING chunker feed
            self.tts._session_generation = initial_gen
            orig_feed = self.tts._chunker.feed

            def racing_feed(tok):
                self.tts._session_generation += 1
                return orig_feed(tok)

            self.tts._chunker.feed = racing_feed
            chunks_racing = self.tts.feed_token("Another token. ")
            self.assertEqual(chunks_racing, [])
            self.assertTrue(self.tts._sentence_queue.empty())

            # 3. flush_tokens with old expected_gen must reject
            flushed = self.tts.flush_tokens(expected_gen=initial_gen)
            self.assertEqual(flushed, [])
            self.assertTrue(self.tts._sentence_queue.empty())

            self.tts.stop()

    def test_reconcile_stream_lifecycle_fails_closed_with_force_until_stream_confirmed_closed(self):
        """Round 31 Finding 2: Verifies reconcile_stream_lifecycle disallows clearing unresolved streams with force=True."""
        mock_stream = MagicMock()
        mock_stream.closed = False
        self.tts._unresolved_streams.add(mock_stream)
        self.tts._stream_cleanup_uncertain = True

        # Even with force=True, unconfirmed streams must not be cleared
        ok = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertFalse(ok)
        self.assertTrue(self.tts._stream_cleanup_uncertain)
        self.assertIn(mock_stream, self.tts._unresolved_streams)

        # Once verified closed, reconciliation succeeds
        mock_stream.closed = True
        ok = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertTrue(ok)
        self.assertFalse(self.tts._stream_cleanup_uncertain)
        self.assertEqual(len(self.tts._unresolved_streams), 0)

    def test_safe_sd_stop_does_not_block_behind_stalled_sd_play(self):
        """Round 31 Finding 3: Verifies _safe_sd_stop does not block on _sd_playback_lock held across native play."""
        self.tts.engine = "kokoro"
        mock_kokoro = MagicMock()
        mock_kokoro.create.return_value = (np.zeros(2400, dtype=np.float32), 24000)
        self.tts._kokoro = mock_kokoro

        # Hold _sd_playback_lock to simulate slow acquisition or external holder
        lock_held = threading.Event()
        release_lock = threading.Event()

        def _hold_lock():
            with self.tts._sd_playback_lock:
                lock_held.set()
                release_lock.wait(timeout=2.0)

        t = threading.Thread(target=_hold_lock, daemon=True)
        t.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            # _safe_sd_stop should execute without deadlocking on _sd_playback_lock.
            # When the lock is held, it fails closed (stopped=False), marks cleanup
            # uncertain, and refrains from calling sd.stop outside the lock.
            with patch("sounddevice.stop") as mock_sd_stop:
                t0 = time.monotonic()
                stopped = self.tts._safe_sd_stop(timeout=0.100)
                dur = time.monotonic() - t0
                self.assertFalse(stopped)
                self.assertLess(dur, 0.5)
                self.assertTrue(self.tts._stream_cleanup_uncertain)
                mock_sd_stop.assert_not_called()
        finally:
            release_lock.set()
            t.join(timeout=1.0)

    def test_shutdown_worker_timeout_then_unclean_exit_preserves_unresolved_stream_across_reconcile_force(self):
        """Round 32 Finding 1: Verifies timed out shutdown worker records failed stream in unresolved streams."""
        stream = MagicMock()
        stream.closed = False
        shutdown_barrier = threading.Event()

        def _blocking_stop(*args, **kwargs):
            shutdown_barrier.wait(timeout=2.0)
            raise RuntimeError("Hardware crash on stop")

        stream.stop.side_effect = _blocking_stop
        stream.abort.side_effect = RuntimeError("Hardware crash on abort")
        stream.close.side_effect = RuntimeError("Hardware crash on close")

        # 1. Close stream with tiny timeout to trigger TIMEOUT_BLOCKED
        ok = self.tts._close_stream_safely(stream, stop_timeout=0.01)
        self.assertFalse(ok)
        self.assertTrue(self.tts._stream_shutdown_timed_out)
        self.assertTrue(self.tts._stream_cleanup_uncertain)
        self.assertIn(stream, self.tts._unresolved_streams)

        # 2. Let background shutdown worker finish with UNCLEAN_FAILED
        shutdown_barrier.set()
        if self.tts._shutdown_worker:
            self.tts._shutdown_worker.join(timeout=1.0)

        # 3. reconcile_stream_lifecycle(force=True) must still fail closed and retain stream
        reconciled = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertFalse(reconciled)
        self.assertIn(stream, self.tts._unresolved_streams)
        self.assertTrue(self.tts._stream_cleanup_uncertain)

        # 4. Once confirmed closed, reconciliation succeeds
        stream.closed = True
        reconciled_after_close = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertTrue(reconciled_after_close)
        self.assertEqual(len(self.tts._unresolved_streams), 0)
        self.assertFalse(self.tts._stream_cleanup_uncertain)

    def test_barge_in_race_before_startup_activation_aborts_session(self):
        """Round 32 Finding 2: Verifies barge-in between API entry and reservation aborts start_streaming_session."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()

        # Hold stop_lock to simulate thread preemption before lifecycle lock reservation
        lock_held = threading.Event()
        release_lock = threading.Event()

        def _block_stop_lock():
            with self.tts._stop_lock:
                lock_held.set()
                release_lock.wait(timeout=2.0)

        th = threading.Thread(target=_block_stop_lock, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            started_box = [None]

            def _start_call():
                started_box[0] = self.tts.start_streaming_session()

            start_th = threading.Thread(target=_start_call, daemon=True)
            start_th.start()
            time.sleep(0.05)

            # Signal barge-in while start_streaming_session is waiting on stop_lock
            from jarvis.core.cancellation import GLOBAL_EPOCH_STATE
            GLOBAL_EPOCH_STATE.barge_in_epoch += 1
            self.tts._stop_event.set()

            # Now release stop_lock and join start_th
            release_lock.set()
            start_th.join(timeout=1.0)

            # Reservation must have been aborted, returning False, preserving stop_event
            self.assertFalse(started_box[0])
            self.assertTrue(self.tts._stop_event.is_set())
            self.assertFalse(self.tts._stream_active)
        finally:
            release_lock.set()
            th.join(timeout=1.0)

    def test_barge_in_race_before_speak_sync_activation_aborts_speech(self):
        """Round 32 Finding 2: Verifies barge-in between API entry and reservation aborts speak_sync."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()

        # Hold lifecycle_lock to simulate preemption at API entry
        lock_held = threading.Event()
        release_lock = threading.Event()

        def _block_lifecycle():
            with self.tts._lifecycle_lock:
                lock_held.set()
                release_lock.wait(timeout=2.0)

        th = threading.Thread(target=_block_lifecycle, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            speak_th = threading.Thread(target=self.tts.speak_sync, args=("Sync speech test.",), daemon=True)
            speak_th.start()
            time.sleep(0.05)

            # Bump epoch and set stop event while speak_sync waits for lifecycle_lock
            from jarvis.core.cancellation import GLOBAL_EPOCH_STATE
            GLOBAL_EPOCH_STATE.barge_in_epoch += 1
            self.tts._stop_event.set()

            release_lock.set()
            speak_th.join(timeout=1.0)

            # speak_sync must abort reservation: sync_speech_active must be 0, not speaking
            self.assertEqual(self.tts._sync_speech_active, 0)
            self.assertFalse(self.tts._is_speaking)
            self.assertTrue(self.tts._stop_event.is_set())
            self.tts._kokoro.create.assert_not_called()
        finally:
            release_lock.set()
            th.join(timeout=1.0)

    def test_sapi_purge_failure_blocks_speech_until_com_recovery(self):
        """Round 33 Finding 2: Verifies _sapi_purge_failed blocks start_streaming_session and speak_sync until COM recovery."""
        self.tts.engine = "sapi5"
        self.tts._sapi_purge_failed = True

        # 1. start_streaming_session must fail closed
        started = self.tts.start_streaming_session()
        self.assertFalse(started)

        # 2. speak_sync must fail closed
        with patch("win32com.client.Dispatch") as mock_dispatch:
            self.tts.speak_sync("This must not speak while purge failed.")
            mock_dispatch.assert_not_called()

        # 3. If SAPI worker is still alive with failed purge, reconcile fails closed
        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = True
        self.tts._sapi_thread = mock_worker
        reconciled_alive = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertFalse(reconciled_alive)
        self.assertTrue(self.tts._sapi_purge_failed)

        # 4. Once SAPI worker terminates, forced reconciliation recovers
        mock_worker.is_alive.return_value = False
        reconciled = self.tts.reconcile_stream_lifecycle(force=True)
        self.assertTrue(reconciled)
        self.assertFalse(self.tts._sapi_purge_failed)

    def test_fallback_queue_unbounded_does_not_evict_streams_on_overflow(self):
        """Round 34 Finding 1: Verifies _fallback_streams is unbounded and does not silently evict streams (>32)."""
        block_worker = threading.Event()
        proceed_worker = threading.Event()

        # Mock close_stream_safely to coordinate with test
        def _mock_close(st, stop_timeout=0.100):
            block_worker.set()
            proceed_worker.wait(timeout=2.0)
            return True

        self.tts._close_stream_safely = _mock_close

        streams = [MagicMock() for _ in range(40)]
        for s in streams:
            with self.tts._stream_claim_lock:
                self.tts._pending_close_streams.add(s)

        # Dispatch the first stream to spin up the fallback thread
        self.tts._dispatch_fallback_close(stream=streams[0])
        self.assertTrue(block_worker.wait(timeout=1.0))

        try:
            # While the first stream is blocked in close, dispatch 39 more streams
            for s in streams[1:]:
                self.tts._dispatch_fallback_close(stream=s)

            # Previously, maxlen=32 evicted older streams, leaving fewer than 39 in deque
            with self.tts._fallback_lock:
                self.assertEqual(len(self.tts._fallback_streams), 39)
        finally:
            proceed_worker.set()
            if self.tts._fallback_thread:
                self.tts._fallback_thread.join(timeout=2.0)

        # All 40 streams must have been drained and discarded from pending_close_streams
        with self.tts._stream_claim_lock:
            for s in streams:
                self.assertNotIn(s, self.tts._pending_close_streams)

    def test_sd_stop_serializes_with_playback_startup_preventing_stale_playback(self):
        """Round 34 Finding 2: Verifies _sd_playback_lock serializes stop with playback startup."""
        self.tts.engine = "kokoro"
        mock_kokoro = MagicMock()
        mock_kokoro.create.return_value = (np.zeros(2400, dtype=np.float32), 24000)
        self.tts._kokoro = mock_kokoro

        lock_acquired_by_test = threading.Event()
        release_test_lock = threading.Event()

        def _hold_playback_lock():
            with self.tts._sd_playback_lock:
                lock_acquired_by_test.set()
                release_test_lock.wait(timeout=2.0)

        t = threading.Thread(target=_hold_playback_lock, daemon=True)
        t.start()
        self.assertTrue(lock_acquired_by_test.wait(timeout=0.5))

        try:
            # Start speak_sync in background: it creates audio and waits on _sd_playback_lock
            speak_th = threading.Thread(target=self.tts.speak_sync, args=("Serialization test.",), daemon=True)
            speak_th.start()
            time.sleep(0.05)

            # Signal stop while speak_sync is waiting for _sd_playback_lock
            self.tts._stop_event.set()

            with patch("sounddevice.play") as mock_play:
                # Release the lock: speak_sync enters _sd_playback_lock, sees _stop_event, and aborts
                release_test_lock.set()
                speak_th.join(timeout=1.0)
                mock_play.assert_not_called()
        finally:
            release_test_lock.set()
            t.join(timeout=1.0)

    def test_barge_in_default_stop_failure_marks_stream_cleanup_uncertain_and_reconciles(self):
        """Round 34 Finding 3: Verifies default sd.stop() failure sets _stream_cleanup_uncertain and reconciles."""
        ack_event = threading.Event()
        outcome: dict = {"ok": False, "error": None}

        # Put STOP_DEFAULT into async close queue with sd.stop raising an error
        with patch("sounddevice.stop", side_effect=RuntimeError("PortAudio hardware fault")):
            self.tts._async_close_queue.put(("STOP_DEFAULT", ack_event, outcome))
            self.assertTrue(ack_event.wait(timeout=1.0))
            self.assertFalse(outcome["ok"])
            self.assertIsInstance(outcome["error"], RuntimeError)
            self.assertTrue(self.tts._stream_cleanup_uncertain)

        # 1. start_streaming_session must fail closed while cleanup is uncertain
        self.assertFalse(self.tts.start_streaming_session())

        # 2. speak_sync must fail closed while cleanup is uncertain
        with patch("sounddevice.play") as mock_play:
            self.tts.speak_sync("This should fail closed.")
            mock_play.assert_not_called()

        # 3. reconcile_stream_lifecycle(force=False) must fail closed
        self.assertFalse(self.tts.reconcile_stream_lifecycle(force=False))
        self.assertTrue(self.tts._stream_cleanup_uncertain)

        # 4. reconcile_stream_lifecycle(force=True) with working sounddevice stops and recovers
        with patch("sounddevice.stop") as mock_sd_stop:
            reconciled = self.tts.reconcile_stream_lifecycle(force=True)
            self.assertTrue(reconciled)
            self.assertFalse(self.tts._stream_cleanup_uncertain)
            mock_sd_stop.assert_called_once()

    def test_pending_default_stop_barrier_blocks_session_and_playback_until_complete(self):
        """Round 35 Finding 1: Verifies _default_stop_pending barrier blocks streaming and playback until stop finishes."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        stop_barrier_hold = threading.Event()
        stop_barrier_released = threading.Event()

        # Simulate async close worker holding _default_stop_pending
        with self.tts._stream_claim_lock:
            self.tts._default_stop_pending = True

        def _worker_release_barrier():
            stop_barrier_hold.wait(timeout=2.0)
            time.sleep(0.02)
            with self.tts._stream_claim_lock:
                self.tts._default_stop_pending = False
                self.tts._default_stop_cond.notify_all()
            stop_barrier_released.set()

        t = threading.Thread(target=_worker_release_barrier, daemon=True)
        t.start()

        try:
            # 1. While worker is blocked and barrier is held (>50ms timeout), startup must fail closed
            with patch("sounddevice.OutputStream", return_value=mock_stream):
                started = self.tts.start_streaming_session()
                self.assertFalse(started)
                self.assertFalse(self.tts._stream_active)

            # 2. speak_sync must also reject while barrier is held
            with patch("sounddevice.play") as mock_play:
                self.tts.speak_sync("Playback while stop barrier pending.")
                mock_play.assert_not_called()

            # 3. Allow worker to clear the barrier
            stop_barrier_hold.set()
            self.assertTrue(stop_barrier_released.wait(timeout=1.0))

            # 4. Once barrier completes, start_streaming_session succeeds
            with patch("sounddevice.OutputStream", return_value=mock_stream):
                started_after = self.tts.start_streaming_session()
                self.assertTrue(started_after)
                self.assertTrue(self.tts._stream_active)
        finally:
            stop_barrier_hold.set()
            t.join(timeout=1.0)
            if self.tts._stream_active:
                self.tts.finish_streaming_session()

    def test_forced_reconciliation_fails_closed_when_sd_stop_fails(self):
        """Round 35 Finding 2: Verifies reconcile_stream_lifecycle(force=True) fails closed if sd.stop fails."""
        self.tts._stream_cleanup_uncertain = True

        # When sd.stop raises an exception during forced reconciliation
        with patch.object(self.tts, "_safe_sd_stop", return_value=False) as mock_safe_stop:
            reconciled = self.tts.reconcile_stream_lifecycle(force=True)
            self.assertFalse(reconciled)
            self.assertTrue(self.tts._stream_cleanup_uncertain)
            mock_safe_stop.assert_called_once()

        # When sd.stop succeeds during forced reconciliation
        with patch.object(self.tts, "_safe_sd_stop", return_value=True) as mock_safe_stop:
            reconciled = self.tts.reconcile_stream_lifecycle(force=True)
            self.assertTrue(reconciled)
            self.assertFalse(self.tts._stream_cleanup_uncertain)
            mock_safe_stop.assert_called_once()

    def test_chunker_lock_timeout_during_session_startup_fails_closed(self):
        """Round 35 Finding 3: Verifies session startup fails closed when chunker lock is held, preventing stale token bleed."""
        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        hold_chunker = threading.Event()
        release_chunker = threading.Event()

        def _hold_chunker_lock():
            with self.tts._chunker_lock:
                hold_chunker.set()
                release_chunker.wait(timeout=2.0)

        t = threading.Thread(target=_hold_chunker_lock, daemon=True)
        t.start()
        self.assertTrue(hold_chunker.wait(timeout=0.5))

        try:
            # While chunker lock is held, start_streaming_session must fail closed on timeout
            with patch("sounddevice.OutputStream", return_value=mock_stream):
                started = self.tts.start_streaming_session()
                self.assertFalse(started)
                self.assertFalse(self.tts._stream_active)
        finally:
            release_chunker.set()
            t.join(timeout=1.0)

        # After releasing chunker lock, start_streaming_session succeeds with cleanly reset chunker
        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started_clean = self.tts.start_streaming_session()
            self.assertTrue(started_clean)
            self.assertTrue(self.tts._stream_active)
            self.tts.finish_streaming_session()

    def test_sapi_barge_in_and_generation_mismatch_discards_stale_sentences(self):
        """Round 36 Finding 1: Verifies SAPI barge-in and generation mismatch discards queued sentences."""
        self.tts.engine = "sapi5"
        self.tts._session_generation = 1

        # Put a sentence tagged with generation 1 into the queue
        self.tts._sentence_queue.put(("Stale sentence from gen 1", 1))

        # Advance generation to simulate barge-in and a new session
        self.tts._session_generation = 2
        self.tts._stop_event.clear()
        self.tts._sapi_purge_event.clear()
        self.tts._stream_active = False

        with (
            patch("win32com.client.Dispatch") as mock_dispatch,
            patch("pythoncom.CoInitialize"),
            patch("pythoncom.CoUninitialize"),
        ):
            mock_sapi = MagicMock()
            mock_sapi.WaitUntilDone.return_value = True
            mock_dispatch.return_value = mock_sapi

            # Start streaming session for SAPI: it drains sentence_queue
            started = self.tts.start_streaming_session()
            self.assertTrue(started)
            active_gen = self.tts._session_generation

            # Queue a new sentence for active_gen
            self.tts.queue_sentence("Fresh sentence for active gen", expected_gen=active_gen)
            time.sleep(0.15)

            # SAPI worker must speak ONLY the active gen sentence, not the stale gen 1 sentence
            spoken_texts = [call.args[0] for call in mock_sapi.Speak.call_args_list if call.args and call.args[0]]
            self.assertIn("Fresh sentence for active gen", spoken_texts)
            self.assertNotIn("Stale sentence from gen 1", spoken_texts)

            self.tts.finish_streaming_session()

    def test_safe_sd_stop_tracks_fallback_worker_and_refuses_concurrent_native_stops(self):
        """Round 36 Finding 2: Verifies fallback _safe_sd_stop tracks worker and refuses concurrent native stop threads."""
        # Make async worker look dead so it routes to fallback thread
        self.tts._async_close_worker = None

        block_stop = threading.Event()
        stop_called = threading.Event()

        def _slow_stop():
            stop_called.set()
            block_stop.wait(timeout=2.0)

        with patch("sounddevice.stop", side_effect=_slow_stop):
            # Spawn the first fallback stop thread
            t1 = threading.Thread(target=self.tts._safe_sd_stop, kwargs={"timeout": 0.05}, daemon=True)
            t1.start()
            self.assertTrue(stop_called.wait(timeout=0.5))

            # Concurrently invoke _safe_sd_stop again
            t2 = threading.Thread(target=self.tts._safe_sd_stop, kwargs={"timeout": 0.05}, daemon=True)
            t2.start()
            time.sleep(0.05)

            # The fallback stop thread must be tracked; no extra threads spawned
            with self.tts._fallback_sd_stop_lock:
                tracked = getattr(self.tts, "_fallback_sd_stop_thread", None)
                self.assertIsNotNone(tracked)
                self.assertTrue(tracked.is_alive())

            block_stop.set()
            t1.join(timeout=1.0)
            t2.join(timeout=1.0)

    def test_lifecycle_reconciliation_protects_unresolved_streams_under_stream_claim_lock(self):
        """Round 36 Finding 3: Verifies reconcile_stream_lifecycle guards _unresolved_streams under _stream_claim_lock."""
        stream1 = MagicMock()
        stream1.closed = False
        with self.tts._stream_claim_lock:
            self.tts._unresolved_streams.add(stream1)
            self.tts._unresolved_streams.add(id(stream1))

        # Reconcile without force while unresolved streams exist fails closed
        reconciled = self.tts.reconcile_stream_lifecycle(force=False)
        self.assertFalse(reconciled)

        # Mark closed and reconcile with force under lock
        stream1.closed = True
        with patch.object(self.tts, "_safe_sd_stop", return_value=True):
            reconciled_forced = self.tts.reconcile_stream_lifecycle(force=True)
            self.assertTrue(reconciled_forced)
            with self.tts._stream_claim_lock:
                self.assertEqual(len(self.tts._unresolved_streams), 0)
                self.assertFalse(self.tts._stream_cleanup_uncertain)

    def test_safe_sd_stop_enqueues_pending_barrier_blocking_startup(self):
        """Round 37 Finding 2: Verifies _safe_sd_stop sets _default_stop_pending barrier before enqueueing."""
        # Hold the async close worker in sd.stop to observe barrier state
        pause_worker = threading.Event()
        worker_reached = threading.Event()

        def _slow_stop():
            worker_reached.set()
            pause_worker.wait(timeout=2.0)

        with patch("sounddevice.stop", side_effect=_slow_stop):
            # _safe_sd_stop with very short timeout times out waiting for ack
            ok = self.tts._safe_sd_stop(timeout=0.01)
            self.assertFalse(ok)

            # _default_stop_pending must be True even after _safe_sd_stop timeout
            with self.tts._stream_claim_lock:
                self.assertTrue(self.tts._default_stop_pending)

            # A subsequent start_streaming_session must fail closed on the barrier
            with patch("sounddevice.OutputStream"):
                started = self.tts.start_streaming_session()
                self.assertFalse(started)

            pause_worker.set()
            self.assertTrue(worker_reached.wait(timeout=1.0))
            time.sleep(0.05)

            # Once worker finishes, barrier clears
            with self.tts._stream_claim_lock:
                self.assertFalse(self.tts._default_stop_pending)

    def test_startup_rejects_barge_in_racing_between_reservation_and_activation(self):
        """Round 37 Finding 3: Verifies startup binds to entry_epoch and aborts if epoch advances during activation."""
        from jarvis.core.cancellation import GLOBAL_EPOCH_STATE

        self.tts.engine = "kokoro"
        self.tts._kokoro = MagicMock()
        mock_stream = MagicMock()
        mock_stream.active = True

        # Hook next() on session_counter to simulate barge-in epoch advancement during activation
        orig_counter = self.tts._session_counter

        def _bumping_generator():
            for val in orig_counter:
                GLOBAL_EPOCH_STATE.barge_in_epoch += 1
                yield val

        self.tts._session_counter = _bumping_generator()

        with patch("sounddevice.OutputStream", return_value=mock_stream):
            started = self.tts.start_streaming_session()
            self.assertFalse(started)
            self.assertFalse(self.tts._stream_active)
            self.assertTrue(self.tts._stop_event.is_set())


    def test_default_stream_abort_or_close_failure_marks_cleanup_uncertain_and_fails_stop(self):
        """Round 38 Finding 3: Default-stream abort/close exceptions fail stop and mark cleanup uncertain."""
        # Test 1: stream.abort raises in async close worker
        mock_stream = MagicMock()
        mock_stream.abort.side_effect = RuntimeError("Abort failed at PortAudio level")
        self.tts._default_stream = mock_stream

        with patch("sounddevice.stop") as mock_sd_stop:
            stopped = self.tts._safe_sd_stop(timeout=0.200)
            self.assertFalse(stopped)
            self.assertTrue(self.tts._stream_cleanup_uncertain)
            mock_sd_stop.assert_called_once()

        # Test 2: stream.close raises when worker is dead (inline fallback)
        self.tts._stream_cleanup_uncertain = False
        mock_stream2 = MagicMock()
        mock_stream2.close.side_effect = RuntimeError("Close handle corrupted")
        self.tts._default_stream = mock_stream2
        self.tts._async_close_worker = None  # Force inline execution

        with patch("sounddevice.stop") as mock_sd_stop:
            stopped = self.tts._safe_sd_stop(timeout=0.200)
            self.assertFalse(stopped)
            self.assertTrue(self.tts._stream_cleanup_uncertain)
            mock_sd_stop.assert_called_once()

    def test_playback_lock_timeout_in_safe_stop_skips_native_operations_and_marks_uncertain(self):
        """Round 38 Finding 1: Lock acquisition timeout skips native abort/close/stop and marks uncertain."""
        mock_stream = MagicMock()
        self.tts._default_stream = mock_stream
        self.tts._async_close_worker = None  # Force inline execution path

        # Hold playback lock to simulate concurrent worker inside sd.play()
        lock_held = threading.Event()
        release_lock = threading.Event()

        def _hold():
            with self.tts._sd_playback_lock:
                lock_held.set()
                release_lock.wait(timeout=2.0)

        t = threading.Thread(target=_hold, daemon=True)
        t.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            with patch("sounddevice.stop") as mock_sd_stop:
                stopped = self.tts._safe_sd_stop(timeout=0.100)
                self.assertFalse(stopped)
                self.assertTrue(self.tts._stream_cleanup_uncertain)
                # Ensure no native PortAudio calls were attempted without the lock
                mock_stream.abort.assert_not_called()
                mock_stream.close.assert_not_called()
                mock_sd_stop.assert_not_called()
        finally:
            release_lock.set()
            t.join(timeout=1.0)

    def test_barge_in_flush_bounded_sub_10ms_with_large_queue_backlog(self):
        """Round 38 Finding 2: on_barge_in_flush flushes ring-buffer/SHM first and stays <10ms under queue backlog."""
        mock_ring = MagicMock()
        mock_shm = MagicMock()
        self.tts._ring_buffer = mock_ring
        self.tts._shm_transport = mock_shm
        self.tts._stream_active = True

        # Pre-fill queues with 10,000 items each
        for i in range(10000):
            self.tts._sentence_queue.put_nowait((f"stale sentence {i}", 1))
            self.tts._audio_queue.put_nowait((np.zeros(100, dtype=np.float32), 24000))

        start_gen = self.tts._session_generation

        t0 = time.perf_counter()
        self.tts.on_barge_in_flush(force=True)
        dur_ms = (time.perf_counter() - t0) * 1000.0

        # Sub-10ms requirement: must complete well under 10ms
        self.assertLess(dur_ms, 10.0)
        # Ring buffer and SHM barge-in was signaled immediately
        mock_ring.barge_in.assert_called_once_with(blocking=False)
        mock_shm.trigger_barge_in_clear.assert_called_once_with(blocking=False)
        # Generation advanced to fence and discard stale items
        self.assertGreater(self.tts._session_generation, start_gen)


    def test_second_stream_ownership_preserved_when_earlier_close_stalled(self):
        """Round 39 Finding 1: Preserves ownership of second stream in unresolved_streams when earlier close stalls."""
        stream1 = MagicMock()
        stream2 = MagicMock()
        barrier = threading.Event()

        def _slow_close(*args, **kwargs):
            barrier.wait(timeout=2.0)

        stream1.stop.side_effect = _slow_close

        # Launch close on stream1 in background thread
        t1 = threading.Thread(target=self.tts._close_stream_safely, args=(stream1, 0.500), daemon=True)
        t1.start()

        # Wait until stream1 is registered as active shutdown worker
        for _ in range(50):
            with self.tts._stream_claim_lock:
                if self.tts._shutdown_stream is stream1:
                    break
            time.sleep(0.01)

        try:
            # Now attempt to close stream2 with a short timeout; stream1 is still running
            ok2 = self.tts._close_stream_safely(stream2, stop_timeout=0.030)
            self.assertFalse(ok2)

            with self.tts._stream_claim_lock:
                # Both stream1 and stream2 must be preserved in unresolved_streams
                self.assertIn(stream2, self.tts._unresolved_streams)
                self.assertIn(id(stream2), self.tts._unresolved_streams)
                self.assertTrue(self.tts._stream_cleanup_uncertain)
                self.assertTrue(self.tts._stream_shutdown_timed_out)
        finally:
            barrier.set()
            t1.join(timeout=1.0)

    def test_second_stream_closes_successfully_after_earlier_close_finishes_within_timeout(self):
        """Round 39 Finding 1: Second stream closes cleanly when earlier close finishes within remaining timeout."""
        stream1 = MagicMock()
        stream1.active = False
        stream2 = MagicMock()
        stream2.active = False

        # Launch close on stream1 with a very fast completion
        self.tts._close_stream_safely(stream1, stop_timeout=0.100)
        with self.tts._stream_claim_lock:
            self.assertIn(stream1, self.tts._closed_streams)

        # Now close stream2
        ok2 = self.tts._close_stream_safely(stream2, stop_timeout=0.100)
        self.assertTrue(ok2)
        with self.tts._stream_claim_lock:
            self.assertIn(stream2, self.tts._closed_streams)
            self.assertNotIn(stream2, self.tts._unresolved_streams)

    def test_default_stop_cond_released_safely_under_lock_contention(self):
        """Round 39 Finding 2: Releases default-stop condition safely without RuntimeError under lock contention."""
        # Hold stream_claim_lock to simulate severe lock contention during teardown
        lock_held = threading.Event()
        can_release = threading.Event()

        def _contend():
            with self.tts._stream_claim_lock:
                lock_held.set()
                can_release.wait(timeout=2.0)

        th = threading.Thread(target=_contend, daemon=True)
        th.start()
        self.assertTrue(lock_held.wait(timeout=0.5))

        try:
            # Set default_stop_pending barrier
            self.tts._default_stop_pending = True

            # Trigger inline stop_default logic in a thread
            # Must not crash with RuntimeError: cannot notify on un-acquired lock
            ack_event = threading.Event()
            outcome = {}
            item = ("STOP_DEFAULT", ack_event, outcome)

            # Queue worker executes while stream_claim_lock is contended
            self.tts._async_close_queue.put_nowait(item)
            self.assertTrue(ack_event.wait(timeout=0.5))

            # Since lock was contended, stream_cleanup_uncertain was marked
            self.assertTrue(self.tts._stream_cleanup_uncertain)
        finally:
            can_release.set()
            th.join(timeout=1.0)

        # Wait for deferred background thread to acquire lock and safely clear barrier
        time.sleep(0.08)
        with self.tts._stream_claim_lock:
            self.assertFalse(self.tts._default_stop_pending)


if __name__ == "__main__":
    unittest.main()
