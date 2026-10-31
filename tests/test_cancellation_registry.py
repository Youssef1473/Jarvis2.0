"""tests/test_cancellation_registry.py — Preemptive Barge-In Registry & Non-Blocking TTS Tests."""
from __future__ import annotations

import gc
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.cancellation import (
    _FLUSH_CALLBACKS,
    _run_flush_callbacks,
    register_audio_flush_callback,
    unregister_audio_flush_callback,
)


class DummySpeaker:
    def __init__(self) -> None:
        self.flushed = False

    def on_flush(self) -> None:
        self.flushed = True


class TestCancellationRegistry(unittest.TestCase):
    """Verifies weak-reference callback registration, thread safety, and memory leak prevention."""

    def setUp(self) -> None:
        # Clear any callbacks before each test
        _FLUSH_CALLBACKS.clear()

    def tearDown(self) -> None:
        _FLUSH_CALLBACKS.clear()

    def test_bound_method_weakref_prevents_memory_leak(self) -> None:
        """Verifies that registering a bound method does not hold a strong reference to the instance."""
        speaker = DummySpeaker()
        register_audio_flush_callback(speaker.on_flush)
        self.assertEqual(len(_FLUSH_CALLBACKS), 1)

        # Trigger flush while object is alive
        _run_flush_callbacks()
        self.assertTrue(speaker.flushed)

        # Delete speaker and force garbage collection
        del speaker
        gc.collect()

        # Next flush must prune dead weakref
        _run_flush_callbacks()
        self.assertEqual(len(_FLUSH_CALLBACKS), 0)

    def test_explicit_unregistration(self) -> None:
        """Verifies that unregister_audio_flush_callback removes bound methods accurately."""
        speaker = DummySpeaker()
        register_audio_flush_callback(speaker.on_flush)
        self.assertEqual(len(_FLUSH_CALLBACKS), 1)

        unregister_audio_flush_callback(speaker.on_flush)
        self.assertEqual(len(_FLUSH_CALLBACKS), 0)

        # Flushing should do nothing
        _run_flush_callbacks()
        self.assertFalse(speaker.flushed)

    def test_duplicate_registration_idempotence(self) -> None:
        """Verifies that registering the same callback multiple times does not create duplicates."""
        speaker = DummySpeaker()
        register_audio_flush_callback(speaker.on_flush)
        register_audio_flush_callback(speaker.on_flush)
        self.assertEqual(len(_FLUSH_CALLBACKS), 1)

        unregister_audio_flush_callback(speaker.on_flush)
        self.assertEqual(len(_FLUSH_CALLBACKS), 0)

    def test_reentrant_unregistration_during_flush(self) -> None:
        """Verifies that a callback unregistering itself executes immediately without 2s timeout wait."""
        speaker = DummySpeaker()
        unreg_duration = 0.0

        def self_removing_callback() -> None:
            nonlocal unreg_duration
            speaker.flushed = True
            t0 = time.perf_counter()
            unreg_res = unregister_audio_flush_callback(self_removing_callback, timeout=2.0)
            unreg_duration = time.perf_counter() - t0
            self.assertTrue(unreg_res)

        register_audio_flush_callback(self_removing_callback)
        _run_flush_callbacks()

        self.assertTrue(speaker.flushed)
        self.assertEqual(len(_FLUSH_CALLBACKS), 0)
        # Self-unregistration must return instantly without blocking on itself
        self.assertLess(unreg_duration, 0.05)

    def test_builtin_bound_method_dedup_and_unregister(self) -> None:
        """Verifies that built-in bound methods (e.g. threading.Event.set) are deduplicated and unregistered."""
        ev = threading.Event()
        # Separate attribute accesses create distinct builtin_function_or_method objects
        set1 = ev.set
        set2 = ev.set
        self.assertIsNot(set1, set2)

        register_audio_flush_callback(set1)
        register_audio_flush_callback(set2)
        self.assertEqual(len(_FLUSH_CALLBACKS), 1)

        _run_flush_callbacks()
        self.assertTrue(ev.is_set())

        # Unregister via fresh attribute access
        res = unregister_audio_flush_callback(ev.set)
        self.assertTrue(res)
        self.assertEqual(len(_FLUSH_CALLBACKS), 0)

    def test_transient_lambda_is_retained_and_invoked(self) -> None:
        """Verifies that ordinary lambdas and functions are strongly retained and not lost to GC."""
        invoked = []

        def register_temp() -> None:
            # Create a transient closure lambda
            register_audio_flush_callback(lambda: invoked.append(True))

        register_temp()
        gc.collect()

        _run_flush_callbacks()
        self.assertEqual(len(invoked), 1)

    def test_in_flight_unregistration_suppresses_pending_callback(self) -> None:
        """Verifies that unregistering a callback while earlier callbacks are running suppresses it."""
        second_ran = []

        def first_cb() -> None:
            # Unregister second callback while flush loop is already in-flight
            unregister_audio_flush_callback(second_cb)

        def second_cb() -> None:
            second_ran.append(True)

        register_audio_flush_callback(first_cb)
        register_audio_flush_callback(second_cb)

        _run_flush_callbacks()
        self.assertEqual(second_ran, [])

    def test_registration_does_not_execute_callback(self) -> None:
        """Verifies that registering a callback or checking matches never accidentally invokes it."""
        calls = []

        def tracked_cb() -> None:
            calls.append(True)

        register_audio_flush_callback(tracked_cb)
        self.assertEqual(calls, [])

    def test_unhashable_callback_supported(self) -> None:
        """Verifies that callables with __hash__ = None register and execute cleanly."""
        class UnhashableCallable:
            __hash__ = None

            def __init__(self) -> None:
                self.invoked = False

            def __call__(self) -> None:
                self.invoked = True

        cb = UnhashableCallable()
        register_audio_flush_callback(cb)
        _run_flush_callbacks()
        self.assertTrue(cb.invoked)
        unregister_audio_flush_callback(cb)

    def test_slotted_class_without_weakref_supported(self) -> None:
        """Verifies that bound methods on slotted classes without __weakref__ register and execute cleanly."""
        class SlottedSpeaker:
            __slots__ = ("flushed",)

            def __init__(self) -> None:
                self.flushed = False

            def on_flush(self) -> None:
                self.flushed = True

        s = SlottedSpeaker()
        register_audio_flush_callback(s.on_flush)
        _run_flush_callbacks()
        self.assertTrue(s.flushed)
        unregister_audio_flush_callback(s.on_flush)

    def test_user_defined_eq_never_called_under_lock(self) -> None:
        """Verifies that identity matching avoids invoking user-defined __eq__ (preventing reentrant deadlock)."""
        class ReentrantEqualSpeaker:
            def __init__(self) -> None:
                self.flushed = False

            def __eq__(self, other: object) -> bool:
                raise RuntimeError("Custom __eq__ should never be called during registry operations")

            def on_flush(self) -> None:
                self.flushed = True

        s = ReentrantEqualSpeaker()
        # Should not raise RuntimeError from __eq__
        register_audio_flush_callback(s.on_flush)
        register_audio_flush_callback(s.on_flush)
        _run_flush_callbacks()
        self.assertTrue(s.flushed)
        unregister_audio_flush_callback(s.on_flush)

    def test_idle_unregister_does_not_retain_instance(self) -> None:
        """Verifies that unregistering while idle does not keep bound method receiver alive."""
        import weakref
        speaker = DummySpeaker()
        wr = weakref.ref(speaker)
        register_audio_flush_callback(speaker.on_flush)
        unregister_audio_flush_callback(speaker.on_flush)
        del speaker
        gc.collect()
        self.assertIsNone(wr())

    def test_multiple_methods_on_same_instance_independent(self) -> None:
        """Verifies that unregistering method A on an instance does NOT suppress method B on the same instance."""
        class DualMethodSpeaker:
            def __init__(self) -> None:
                self.method_a_ran = False
                self.method_b_ran = False

            def on_flush_a(self) -> None:
                self.method_a_ran = True

            def on_flush_b(self) -> None:
                self.method_b_ran = True

        speaker = DualMethodSpeaker()

        def unregister_a_during_dispatch() -> None:
            # Unregister method A while dispatch is in-flight
            unregister_audio_flush_callback(speaker.on_flush_a)

        # Register: trigger unregister of A, then method A, then method B
        register_audio_flush_callback(unregister_a_during_dispatch)
        register_audio_flush_callback(speaker.on_flush_a)
        register_audio_flush_callback(speaker.on_flush_b)

        _run_flush_callbacks()

        # Method A should be suppressed, but method B MUST run cleanly
        self.assertFalse(speaker.method_a_ran)
        self.assertTrue(speaker.method_b_ran)

    def test_unregister_linearization_waits_for_in_flight_callback(self) -> None:
        """Verifies that unregistering an in-flight executing callback blocks until the callback exits."""
        callback_entered = threading.Event()
        can_callback_exit = threading.Event()
        callback_completed = False

        def slow_callback() -> None:
            nonlocal callback_completed
            callback_entered.set()
            can_callback_exit.wait(timeout=2.0)
            callback_completed = True

        register_audio_flush_callback(slow_callback)

        dispatch_thread = threading.Thread(target=_run_flush_callbacks, daemon=True)
        dispatch_thread.start()

        # Wait until slow_callback is actively executing
        self.assertTrue(callback_entered.wait(timeout=2.0))

        # Start unregistering in a separate thread: must wait until callback finishes
        unregister_finished = threading.Event()

        def do_unregister() -> None:
            unregister_audio_flush_callback(slow_callback, timeout=2.0)
            unregister_finished.set()

        unreg_thread = threading.Thread(target=do_unregister, daemon=True)
        unreg_thread.start()

        # At this point, unregister must NOT have finished yet because slow_callback is still in execution
        time.sleep(0.05)
        self.assertFalse(unregister_finished.is_set())

        # Release slow_callback
        can_callback_exit.set()
        dispatch_thread.join(timeout=2.0)
        unreg_thread.join(timeout=2.0)

        # Now both must be finished
        self.assertTrue(unregister_finished.is_set())
        self.assertTrue(callback_completed)

    def test_concurrent_registration_and_flush_thread_safety(self) -> None:
        """Verifies thread-safe registration, unregistration, and execution across worker threads."""
        threads = []
        errors = []

        def worker(idx: int) -> None:
            try:
                s = DummySpeaker()
                for _ in range(50):
                    register_audio_flush_callback(s.on_flush)
                    if idx % 2 == 0:
                        _run_flush_callbacks()
                    unregister_audio_flush_callback(s.on_flush)
            except (RuntimeError, ValueError, TypeError) as e:
                errors.append(e)

        for i in range(4):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [])


class TestTTSBargeInDecoupling(unittest.TestCase):
    """Verifies that TextToSpeech on_barge_in_flush executes non-blockingly within microsecond budgets."""

    @patch("jarvis.audio.tts.sd")
    @patch("jarvis.audio.tts.KokoroPipeline", create=True)
    def test_tts_on_barge_in_flush_is_non_blocking(self, mock_pipeline: MagicMock, mock_sd: MagicMock) -> None:
        from jarvis.audio.tts import TextToSpeech

        with patch.object(TextToSpeech, "_init_kokoro"):
            tts = TextToSpeech(config={"tts": {"voice": "bm_fable", "speed": 1.0}})
            mock_stream = MagicMock()
            mock_stream.active = True
            tts._output_stream = mock_stream
            tts._is_speaking = True

            start = time.perf_counter()
            tts.on_barge_in_flush()
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            # Must complete well under 25ms (normally <2ms without sleeping for anti-click ramp)
            self.assertLess(elapsed_ms, 25.0)
            self.assertFalse(tts._is_speaking)
            self.assertIsNone(tts._output_stream)

            # Cleanup
            tts.close()

    @patch("jarvis.audio.tts.sd")
    @patch("jarvis.audio.tts.KokoroPipeline", create=True)
    def test_tts_close_unregisters_barge_in_callback(self, mock_pipeline: MagicMock, mock_sd: MagicMock) -> None:
        from jarvis.audio.tts import TextToSpeech

        with patch.object(TextToSpeech, "_init_kokoro"):
            tts = TextToSpeech(config={"tts": {"voice": "bm_fable", "speed": 1.0}})
            # Verify callback is registered
            matches = [w for w in _FLUSH_CALLBACKS if w.matches(tts.on_barge_in_flush)]
            self.assertEqual(len(matches), 1)

            # Close TTS and verify callback is removed
            tts.close()
            matches_after = [w for w in _FLUSH_CALLBACKS if w.matches(tts.on_barge_in_flush)]
            self.assertEqual(len(matches_after), 0)

    @patch("jarvis.audio.tts.sd")
    @patch("jarvis.audio.tts.KokoroPipeline", create=True)
    def test_tts_close_raises_when_callback_in_flight_past_timeout(self, mock_pipeline: MagicMock, mock_sd: MagicMock) -> None:
        """Verifies that tts.close() raises RuntimeError if barge-in callback is held past timeout."""
        from jarvis.audio.tts import TextToSpeech

        with patch.object(TextToSpeech, "_init_kokoro"):
            tts = TextToSpeech(config={"tts": {"voice": "bm_fable", "speed": 1.0}})

            callback_entered = threading.Event()
            can_exit = threading.Event()

            def slow_barge_in() -> None:
                callback_entered.set()
                can_exit.wait(timeout=1.0)

            # Replace callback with slow callback
            tts.on_barge_in_flush = slow_barge_in
            register_audio_flush_callback(slow_barge_in)

            dispatch_thread = threading.Thread(target=_run_flush_callbacks, daemon=True)
            dispatch_thread.start()

            self.assertTrue(callback_entered.wait(timeout=1.0))

            # Attempting to close with 0.05s timeout must raise RuntimeError and NOT proceed to stop()
            with self.assertRaises(RuntimeError):
                tts.close(timeout=0.05)

            # Cleanup
            can_exit.set()
            dispatch_thread.join(timeout=1.0)
            unregister_audio_flush_callback(slow_barge_in)
            tts.close(timeout=1.0)

    @patch("jarvis.audio.tts.sd")
    @patch("jarvis.audio.tts.KokoroPipeline", create=True)
    def test_tts_close_raises_when_worker_thread_stalled_and_preserves_shm(self, mock_pipeline: MagicMock, mock_sd: MagicMock) -> None:
        """Verifies that tts.close() raises RuntimeError if a worker stalls and keeps SHM open."""
        from jarvis.audio.tts import TextToSpeech

        with patch.object(TextToSpeech, "_init_kokoro"):
            tts = TextToSpeech(config={"tts": {"voice": "bm_fable", "speed": 1.0}})

            stalled_worker = MagicMock()
            stalled_worker.is_alive.return_value = True

            tts._synth_thread = stalled_worker
            mock_shm = MagicMock()
            tts._shm_transport = mock_shm

            with self.assertRaises(RuntimeError):
                tts.close(timeout=0.1)

            # SHM must NOT have been closed (prevents use-after-free for active workers)
            mock_shm.close.assert_not_called()
            self.assertIsNotNone(tts._shm_transport)

            # Cleanup
            stalled_worker.is_alive.return_value = False
            tts.close(timeout=1.0)
            mock_shm.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
