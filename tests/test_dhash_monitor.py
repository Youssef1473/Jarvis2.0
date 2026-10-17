"""Unit tests for Passive 32x32 dHash Background Screen Monitor."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import numpy as np
from PIL import Image

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.vision.dhash_monitor import BackgroundDHashMonitor


class TestDHashMonitor(unittest.TestCase):
    """Verifies 32x32 horizontal gradient dHash, per-HWND baseline tracking, PrivacyGuard gating, and idle triggers."""

    def test_compute_dhash_shape_and_determinism(self):
        """Verifies compute_dhash returns 32x32 boolean array deterministically."""
        img = Image.new("RGB", (200, 200), color=(128, 128, 128))
        h1 = BackgroundDHashMonitor.compute_dhash(img)
        h2 = BackgroundDHashMonitor.compute_dhash(img)

        self.assertEqual(h1.shape, (32, 32))
        self.assertEqual(h1.dtype, bool)
        self.assertEqual(BackgroundDHashMonitor.hamming_distance(h1, h2), 0)

    def test_hamming_distance_contrasting_patterns(self):
        """Verifies contrasting gradient patterns produce significant Hamming distance."""
        # Gradient from left (black) to right (white)
        grad_arr = np.tile(np.linspace(0, 255, 100, dtype=np.uint8), (100, 1))
        img_left_to_right = Image.fromarray(grad_arr, mode="L")

        # Inverted gradient from left (white) to right (black)
        img_right_to_left = Image.fromarray(255 - grad_arr, mode="L")

        h_lr = BackgroundDHashMonitor.compute_dhash(img_left_to_right)
        h_rl = BackgroundDHashMonitor.compute_dhash(img_right_to_left)

        dist = BackgroundDHashMonitor.hamming_distance(h_lr, h_rl)
        # 32x32 = 1024 total bits; opposing horizontal gradients should have very high distance
        self.assertGreater(dist, 900)

    def test_per_hwnd_baseline_tracking_and_window_switch_reset(self):
        """Mandatory Constraint 4: Track previous 32x32 dHash per hwnd, reset baseline on window switch."""
        orchestrator = MagicMock()
        monitor = BackgroundDHashMonitor(orchestrator=orchestrator, hamming_threshold=140, idle_threshold=3.0)

        img_a = Image.new("RGB", (100, 100), color=(0, 0, 0))
        img_b = Image.new("RGB", (100, 100), color=(255, 255, 255))

        # 1. First poll on HWND 1001 (e.g. IDE)
        with patch.object(monitor, "_capture_window_or_screen", return_value=img_a), \
             patch("ctypes.windll.user32.GetForegroundWindow", return_value=1001), \
             patch("ctypes.windll.user32.IsWindow", return_value=1), \
             patch("jarvis.vision.dhash_monitor._get_window_title_and_proc", return_value=("VS Code", "code.exe")):
            res = monitor.poll_once()
            self.assertIsNone(res, "First poll on new HWND must establish baseline and not alert")
            self.assertEqual(monitor._current_fg_hwnd, 1001)
            self.assertIn(1001, monitor._last_hashes)

        # 2. Window switch to HWND 2002 (e.g. Terminal)
        with patch.object(monitor, "_capture_window_or_screen", return_value=img_b), \
             patch("ctypes.windll.user32.GetForegroundWindow", return_value=2002), \
             patch("ctypes.windll.user32.IsWindow", return_value=1), \
             patch("jarvis.vision.dhash_monitor._get_window_title_and_proc", return_value=("Windows Terminal", "windowsterminal.exe")):
            res = monitor.poll_once()
            self.assertIsNone(res, "Window switch must reset baseline and NOT alert as a background completion")
            self.assertEqual(monitor._current_fg_hwnd, 2002)
            self.assertIn(2002, monitor._last_hashes)
            # Verify HWND 1001 baseline was preserved in per-HWND dict
            self.assertIn(1001, monitor._last_hashes)

    def test_privacy_guard_gating_never_hashes_or_alerts_sensitive_apps(self):
        """Mandatory Constraint 4: Gate captures through PrivacyGuard so sensitive apps are never hashed or alerted."""
        orchestrator = MagicMock()
        monitor = BackgroundDHashMonitor(orchestrator=orchestrator)

        with patch("ctypes.windll.user32.GetForegroundWindow", return_value=5555), \
             patch("ctypes.windll.user32.IsWindow", return_value=1), \
             patch("jarvis.vision.dhash_monitor._get_window_title_and_proc", return_value=("1Password - Master Password", "1password.exe")), \
             patch.object(monitor, "_capture_window_or_screen") as mock_capture:
            res = monitor.poll_once()
            self.assertIsNone(res)
            # Screen capture must be completely gated and bypassed
            mock_capture.assert_not_called()
            self.assertNotIn(5555, monitor._last_hashes, "Sensitive HWND must never be hashed or tracked")

    def test_idle_transition_alert_trigger(self):
        """Verifies that when user is idle >= 3.0s and visual distance > 140 bits, an alert is dispatched."""
        orchestrator = MagicMock()
        transitions_fired = []

        monitor = BackgroundDHashMonitor(
            orchestrator=orchestrator,
            hamming_threshold=140,
            idle_threshold=3.0,
            get_idle_seconds_fn=lambda: 5.0,  # User idle for 5 seconds
        )
        monitor.add_transition_callback(lambda hwnd, title, dist: transitions_fired.append((hwnd, title, dist)))

        # Baseline image: black
        img_baseline = Image.new("RGB", (100, 100), color=(0, 0, 0))
        # Changed image: gradient with high difference
        grad_arr = np.tile(np.linspace(0, 255, 100, dtype=np.uint8), (100, 1))
        img_changed = Image.fromarray(grad_arr, mode="L")

        with patch("ctypes.windll.user32.GetForegroundWindow", return_value=7777), \
             patch("ctypes.windll.user32.IsWindow", return_value=1), \
             patch("jarvis.vision.dhash_monitor._get_window_title_and_proc", return_value=("Build Console", "cmd.exe")):
            
            # 1. Establish baseline
            with patch.object(monitor, "_capture_window_or_screen", return_value=img_baseline):
                monitor.poll_once()

            # 2. Polling again while idle with significant screen change
            with patch.object(monitor, "_capture_window_or_screen", return_value=img_changed):
                res = monitor.poll_once()
                self.assertIsNotNone(res, "Screen transition must be detected when idle with high bit difference")
                self.assertEqual(res[0], 7777)
                self.assertEqual(res[1], "Build Console")
                self.assertGreater(res[2], 140)
                self.assertEqual(len(transitions_fired), 1)
                orchestrator.broadcast_ambient_telemetry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
