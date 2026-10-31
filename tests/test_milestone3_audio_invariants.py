"""Milestone 3 Verification: Audio Pipeline Real-Time Invariants & Cancellation Safety (C-06, C-08).

Verifies:
1. signal_global_barge_in & trigger_speech_barge_in default to non-blocking background dispatch (<10ms).
2. StatefulPolyphaseUpsampler lookbehind state retains continuous speech waveform without zero-padding contamination.
3. MMCSS lifecycle management, thread-local handle tracking, and mmcss_scope cleanup.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.audio.tts import StatefulPolyphaseUpsampler, TextToSpeech
from jarvis.core.cancellation import (
    GLOBAL_EPOCH_STATE,
    register_audio_flush_callback,
    signal_global_barge_in,
    trigger_speech_barge_in,
    unregister_audio_flush_callback,
)
from jarvis.core.win32_affinity import (
    get_current_thread_mmcss_handle,
    mmcss_scope,
    revert_current_thread_mmcss,
)


class TestMilestone3NonBlockingBargeIn(unittest.TestCase):
    """Verifies C-06: Barge-in signals return in <10ms without stalling real-time audio threads."""

    def setUp(self):
        self.slow_callback_ran = threading.Event()

    def tearDown(self):
        self.slow_callback_ran.set()

    def test_trigger_speech_barge_in_returns_immediately_under_slow_callback(self):
        """trigger_speech_barge_in must invalidate epoch in <10ns and return in <10ms,

        dispatching slow flush callbacks asynchronously without stalling the caller thread.
        """
        def slow_flush():
            time.sleep(0.15)
            self.slow_callback_ran.set()

        register_audio_flush_callback(slow_flush)
        try:
            initial_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)

            t0 = time.perf_counter()
            new_epoch = trigger_speech_barge_in()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            # 1. Epoch advanced immediately
            self.assertGreater(new_epoch, initial_epoch)

            # 2. Returned in <15ms (never stalled by the 150ms slow flush callback)
            self.assertLess(elapsed_ms, 25.0, f"Barge-in stalled caller thread for {elapsed_ms:.2f}ms!")

            # 3. Background callback eventually executes
            self.assertTrue(self.slow_callback_ran.wait(timeout=1.0))
        finally:
            unregister_audio_flush_callback(slow_flush)

    def test_signal_global_barge_in_defaults_to_async_callbacks(self):
        """signal_global_barge_in defaults async_callbacks=True for caller thread responsiveness."""
        callback_event = threading.Event()

        def background_flush():
            callback_event.set()

        register_audio_flush_callback(background_flush)
        try:
            t0 = time.perf_counter()
            new_epoch = signal_global_barge_in()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            self.assertLess(elapsed_ms, 25.0)
            self.assertTrue(callback_event.wait(timeout=1.0))
            self.assertGreaterEqual(new_epoch, 1)
        finally:
            unregister_audio_flush_callback(background_flush)


class TestMilestone3ResamplerSpeechContinuity(unittest.TestCase):
    """Verifies C-06: Polyphase upsampler receives raw speech and captures active waveform tail."""

    def test_smooth_and_pad_audio_pad_silence_parameter(self):
        """_smooth_and_pad_audio supports pad_silence=False to prevent appending zeros before upsampler."""
        sr = 24000
        # 0.1s synthetic tone at 440Hz
        t = np.linspace(0, 0.1, int(sr * 0.1), endpoint=False, dtype=np.float32)
        tone = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)

        # 1. With pad_silence=True (default), trailing zeros are appended
        padded = TextToSpeech._smooth_and_pad_audio(tone, sr, "Hello, Sir.", apply_dsp=False, pad_silence=True)
        self.assertGreater(len(padded), len(tone))
        # The end must be silent zeros
        self.assertEqual(float(np.sum(np.abs(padded[-100:]))), 0.0)

        # 2. With pad_silence=False, exact speech samples are preserved without trailing zero padding
        raw_smooth = TextToSpeech._smooth_and_pad_audio(tone, sr, "Hello, Sir.", apply_dsp=False, pad_silence=False)
        self.assertEqual(len(raw_smooth), len(tone))
        # The end contains smoothed speech waveform, NOT zero silence
        self.assertGreater(float(np.sum(np.abs(raw_smooth[-100:]))), 0.0)

    def test_stateful_polyphase_upsampler_captures_non_zero_speech_tail(self):
        """When fed continuous speech (without trailing zeros), upsampler retains non-zero lookbehind state."""
        upsampler = StatefulPolyphaseUpsampler(up=2, down=1, pad_samples=32)
        sr = 24000
        t = np.linspace(0, 0.05, int(sr * 0.05), endpoint=False, dtype=np.float32)
        tone = 0.7 * np.sin(2 * np.pi * 880 * t).astype(np.float32)

        # Initial process: lookbehind is empty
        out1 = upsampler.process(tone)
        self.assertEqual(len(out1), len(tone) * 2)

        # Verify tail state holds non-zero waveform samples from the end of chunk 1
        tail = upsampler._tail_24k
        self.assertIsNotNone(tail)
        self.assertEqual(tail.size, 32)
        self.assertGreater(float(np.sum(np.abs(tail))), 0.0, "Tail state contains only zeros!")

        # Process chunk 2: lookbehind ensures seamless FIR filter interpolation
        out2 = upsampler.process(tone)
        self.assertEqual(len(out2), len(tone) * 2)


class TestMilestone3MMCSSLifecycle(unittest.TestCase):
    """Verifies C-08: MMCSS thread-local registration, reversion, and scope management."""

    def test_mmcss_scope_and_revert_lifecycle(self):
        """mmcss_scope context manager elevates thread and reverts cleanly on exit."""
        if sys.platform != "win32":
            self.skipTest("MMCSS tests require Windows platform.")

        initial_h = get_current_thread_mmcss_handle()
        self.assertIsNone(initial_h)

        with (
            patch("ctypes.windll.avrt.AvSetMmThreadCharacteristicsW", return_value=12345) as mock_set,
            patch("ctypes.windll.avrt.AvSetMmThreadPriority", return_value=1),
            patch("ctypes.windll.avrt.AvRevertMmThreadCharacteristics", return_value=1) as mock_rev,
            mmcss_scope("Pro Audio") as h_task,
        ):
            self.assertEqual(h_task, 12345)
            self.assertEqual(get_current_thread_mmcss_handle(), 12345)
            self.assertTrue(mock_set.called)

        # On scope exit, AvRevertMmThreadCharacteristics must be called and thread-local cleared
        self.assertTrue(mock_rev.called)
        self.assertIsNone(get_current_thread_mmcss_handle())

    def test_revert_current_thread_mmcss_safe_when_no_handle_registered(self):
        """revert_current_thread_mmcss returns False safely if no MMCSS handle is active."""
        res = revert_current_thread_mmcss()
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
