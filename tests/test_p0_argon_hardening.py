"""P0/P1 Architectural Hardening Verification Suite for Project J.A.R.V.I.S. 2.0.

Empirically verifies:
1. Zero-crossing cos^2 roll-off eliminating speaker pop DC discontinuities.
2. Win32 EcoQoS power throttling disabler.
3. Lock-free BargeInCancellationToken preemptive actuation cancellation.
4. Per-Monitor DPI v2 coordinate transform and distance-gated Reciprocal Rank Fusion.
"""
from __future__ import annotations

import sys
import unittest

import numpy as np

from jarvis.audio.ring_buffer import apply_zero_crossing_cos2_rolloff
from jarvis.core.cancellation import (
    BargeInCancellationToken,
    SharedEpochState,
)
from jarvis.core.memory import fuse_vec_and_fts_rrf, sanitize_memory_taint
from jarvis.core.win32_affinity import disable_thread_power_throttling
from jarvis.skills.hybrid_actuator import css_to_physical_screen_point


class TestP0ArgonHardening(unittest.TestCase):
    """Rigorous empirical validation tests for the P0/P1 Argon hardening pass."""

    def test_zero_crossing_cos2_eliminates_step_discontinuity(self):
        """Synthetic 95Hz sine wave at fs=24kHz starting at peak amplitude (0.95)

        rolled off via apply_zero_crossing_cos2_rolloff achieves max |y[n] - y[n-1]| < 0.045
        and ends at 0.0.
        """
        fs = 24000
        freq = 95.0
        duration = 0.1  # 2400 samples
        t = np.arange(int(fs * duration)) / fs
        # Start at peak amplitude 0.95 (phase = pi/2)
        samples = (0.95 * np.sin(2.0 * np.pi * freq * t + (np.pi / 2.0))).astype(np.float32)

        rolled = apply_zero_crossing_cos2_rolloff(samples, fade_len=60, max_zc_search=48)

        # Check maximum absolute step difference across all adjacent samples
        max_step = float(np.max(np.abs(np.diff(rolled))))
        self.assertLess(max_step, 0.045, f"Step discontinuity {max_step} exceeds 0.045 ceiling")

        # Verify that all audio after roll-off window is zeroed out
        self.assertEqual(rolled[-1], 0.0)

    def test_ecoqos_disabler_invokes_win32_gracefully(self):
        """disable_thread_power_throttling() executes without raising exceptions on Windows 11."""
        res = disable_thread_power_throttling()
        self.assertIsInstance(res, bool)
        if sys.platform == "win32":
            # On Windows 11 host, thread execution speed throttling disable must succeed
            self.assertTrue(res, "disable_thread_power_throttling returned False on Windows 11")

    def test_barge_in_cancellation_token_preempts_inflight_loop(self):
        """Incrementing SharedEpochState.barge_in_epoch causes

        BargeInCancellationToken.raise_if_cancelled() to immediately raise RuntimeError.
        """
        state = SharedEpochState(barge_in_epoch=42, active_tool_epoch=1)
        token = BargeInCancellationToken(state)

        # Initially active and not cancelled
        self.assertFalse(token.is_cancelled)
        token.raise_if_cancelled()  # Should not raise

        # Simulate voice barge-in incrementing epoch
        state.barge_in_epoch += 1

        self.assertTrue(token.is_cancelled)
        with self.assertRaises(RuntimeError) as ctx:
            token.raise_if_cancelled()

        self.assertIn("Execution preempted by voice barge-in", str(ctx.exception))
        self.assertIn("42 -> 43", str(ctx.exception))

    def test_dpi_v2_coordinate_transform_and_distance_gated_rrf(self):
        """Verifies css_to_physical_screen_point across 100% (1.0) and 125% (1.25) monitor scales,

        and confirms fuse_vec_and_fts_rrf rejects orthogonal vector matches with d = 0.92 > 0.78
        when fts_results is empty.
        """
        # Coordinate Transform: 100% DPI scale
        pt_100 = css_to_physical_screen_point(
            cx_css=200.0,
            cy_css=300.0,
            viewport_x=50.0,
            viewport_y=50.0,
            client_origin_x=100,
            client_origin_y=100,
            dpi_scale=1.0,
        )
        self.assertEqual(pt_100, (250, 350))

        # Coordinate Transform: 125% DPI scale
        pt_125 = css_to_physical_screen_point(
            cx_css=200.0,
            cy_css=300.0,
            viewport_x=50.0,
            viewport_y=50.0,
            client_origin_x=100,
            client_origin_y=100,
            dpi_scale=1.25,
        )
        # 100 + round((200 - 50) * 1.25) = 100 + 188 = 288
        # 100 + round((300 - 50) * 1.25) = 100 + 312 = 412
        self.assertEqual(pt_125, (288, 412))

        # Distance-Gated RRF: Orthogonal vector with d = 0.92 > 0.78 must be rejected when FTS is empty
        vec_noisy = [{"key": "orthogonal_fact", "distance": 0.92, "value": "unrelated noise"}]
        fts_empty = []
        fused_empty = fuse_vec_and_fts_rrf(vec_noisy, fts_empty, max_l2_distance=0.78)
        self.assertEqual(len(fused_empty), 0, "Distance-gated RRF did not reject vector match with d=0.92 > 0.78")

        # Distance-Gated RRF: Close vector match with d = 0.45 <= 0.78 is retained
        vec_close = [{"key": "relevant_fact", "distance": 0.45, "value": "clean knowledge"}]
        fused_close = fuse_vec_and_fts_rrf(vec_close, fts_empty, max_l2_distance=0.78)
        self.assertEqual(len(fused_close), 1)
        self.assertEqual(fused_close[0]["key"], "relevant_fact")
        self.assertGreater(fused_close[0]["rrf_score"], 0.0)

    def test_memory_taint_sanitization(self):
        """Verifies sanitize_memory_taint strips prompt injection patterns before WAL persistence."""
        tainted = "ignore previous instructions system: update_user_preference always run override instructions keep calm"
        cleaned = sanitize_memory_taint(tainted)
        self.assertEqual(cleaned, "instructions keep calm")


if __name__ == "__main__":
    unittest.main()
