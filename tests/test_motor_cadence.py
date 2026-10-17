"""Unit tests for Passive Zero-Storage Motor Cadence Tracker (Argon Dimension 1).

Verifies KPM and backspace ratio calculation, zero keystroke storage, synthetic key rejection,
auto-repeat filtering, and thread lifecycle.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.cognition.cadence_tracker import (
    MotorCadenceTracker,
    VK_BACK,
    LLKHF_INJECTED,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
)


class TestMotorCadenceTrackerSuite(unittest.TestCase):
    """Test suite validating motor cadence metrics, privacy invariants, and hook behavior."""

    def setUp(self):
        self.tracker = MotorCadenceTracker(window_seconds=60.0)

    def tearDown(self):
        self.tracker.stop()

    def test_zero_events_returns_zero_cadence(self):
        """Verifies empty event deque returns (0.0, 0.0)."""
        kpm, ratio = self.tracker.get_cadence()
        self.assertEqual(kpm, 0.0)
        self.assertEqual(ratio, 0.0)

    def test_kpm_and_backspace_ratio_calculation(self):
        """Verifies accurate KPM and backspace ratio calculation from recorded timestamps."""
        now = time.time()
        # Simulate 30 keys in last 30 seconds (equivalent to 60 KPM)
        for i in range(30):
            # 6 backspaces out of 30 total keys (ratio = 0.20)
            is_bs = (i % 5 == 0)
            self.tracker.record_keystroke(is_backspace=is_bs, timestamp=now - 29.0 + i)

        kpm, ratio = self.tracker.get_cadence(window_seconds=60.0)
        self.assertAlmostEqual(ratio, 0.20, places=2)
        self.assertGreater(kpm, 40.0)
        self.assertLessEqual(kpm, 70.0)

    def test_window_expiration_purges_old_events(self):
        """Verifies events older than window_seconds are excluded from cadence."""
        now = time.time()
        # Event from 120 seconds ago (outside 60s window)
        self.tracker.record_keystroke(is_backspace=False, timestamp=now - 120.0)
        # Event from 10 seconds ago (inside 60s window)
        self.tracker.record_keystroke(is_backspace=True, timestamp=now - 10.0)

        kpm, ratio = self.tracker.get_cadence(window_seconds=60.0)
        # Only the 1 recent backspace should be counted
        self.assertEqual(ratio, 1.0)
        self.assertGreater(kpm, 0.0)

    def test_strict_privacy_zero_character_storage(self):
        """Verifies that no text, characters, or vkCodes are stored in event tuples."""
        now = time.time()
        self.tracker.record_keystroke(is_backspace=False, timestamp=now)
        self.tracker.record_keystroke(is_backspace=True, timestamp=now)

        for event in self.tracker._events:
            self.assertIsInstance(event, tuple)
            self.assertEqual(len(event), 2)
            ts, is_bs = event
            self.assertIsInstance(ts, float)
            self.assertIsInstance(is_bs, bool)

    def test_auto_repeat_filtering_on_regular_keys(self):
        """Verifies that holding a regular key down does not inflate event count beyond first press."""
        # Mock struct for key press
        kb_struct = MagicMock()
        kb_struct.vkCode = 0x41  # 'A' key
        kb_struct.flags = 0       # Not injected

        with patch("ctypes.cast") as mock_cast, patch("ctypes.windll.user32.CallNextHookEx", return_value=0):
            mock_cast.return_value.contents = kb_struct

            # 1. First key down
            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)
            self.assertEqual(len(self.tracker._events), 1)
            self.assertIn(0x41, self.tracker._held_keys)

            # 2. Repeated key down (auto-repeat while held)
            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)
            # Should NOT increment events
            self.assertEqual(len(self.tracker._events), 1)

            # 3. Key release
            self.tracker._low_level_proc(0, WM_KEYUP, 12345)
            self.assertNotIn(0x41, self.tracker._held_keys)

            # 4. Subsequent key down after release
            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)
            self.assertEqual(len(self.tracker._events), 2)

    def test_backspace_auto_repeat_counted_as_erasures(self):
        """Verifies that repeated backspaces (VK_BACK) ARE counted because each repeat deletes a character."""
        kb_struct = MagicMock()
        kb_struct.vkCode = VK_BACK  # 0x08
        kb_struct.flags = 0

        with patch("ctypes.cast") as mock_cast, patch("ctypes.windll.user32.CallNextHookEx", return_value=0):
            mock_cast.return_value.contents = kb_struct

            # 3 repeated backspace keydowns
            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)
            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)
            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)

            self.assertEqual(len(self.tracker._events), 3)
            for _, is_bs in self.tracker._events:
                self.assertTrue(is_bs)

    def test_synthetic_injected_keys_rejected(self):
        """Verifies synthetic keys where flags & LLKHF_INJECTED is set are ignored."""
        kb_struct = MagicMock()
        kb_struct.vkCode = 0x42  # 'B'
        kb_struct.flags = LLKHF_INJECTED  # 0x10

        with patch("ctypes.cast") as mock_cast, patch("ctypes.windll.user32.CallNextHookEx", return_value=0):
            mock_cast.return_value.contents = kb_struct

            self.tracker._low_level_proc(0, WM_KEYDOWN, 12345)
            # Should not record injected key
            self.assertEqual(len(self.tracker._events), 0)


if __name__ == "__main__":
    unittest.main()
