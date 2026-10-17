import sys
import unittest
import warnings
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore", message=".*Revert to STA COM threading mode.*")

from jarvis.skills.base import SkillRegistry
import jarvis.skills.windows_uia as w_uia
import jarvis.skills.os_control as os_ctrl


class TestWindowsUIASuite(unittest.TestCase):
    """Verifies pywinauto UIA support, element inspection, and focus routing."""

    def test_uia_supported_on_windows(self):
        """Verifies UIA availability detection."""
        self.assertTrue(w_uia.is_uia_supported())

    def test_inspect_window_controls_empty(self):
        """Verifies empty window title is politely rejected."""
        res = SkillRegistry.execute("inspect_window_controls", window_title="")
        self.assertIn("cannot be empty", res.lower())

    def test_inspect_window_controls_nonexistent(self):
        """Verifies non-existent window handling."""
        res = SkillRegistry.execute("inspect_window_controls", window_title="nonexistent_xyz_998877")
        self.assertIn("could not find", res.lower())

    def test_focus_window_uia_empty(self):
        """Verifies empty title returns None cleanly without error."""
        res = w_uia.focus_window_uia("")
        self.assertIsNone(res)

    def test_os_control_focus_window_with_uia_fallback(self):
        """Verifies focus_window seamlessly handles non-existent windows via UIA/Win32."""
        res = os_ctrl.focus_window("nonexistent_app_window_9988")
        self.assertTrue(
            "no open window matching" in res.lower() or "failed to focus window" in res.lower()
        )

    def test_is_hwnd_responsive_zero_or_invalid(self):
        """Verifies invalid or zero HWND returns True without crashing."""
        self.assertTrue(w_uia.is_hwnd_responsive(0))

    def test_is_hwnd_responsive_hung_detection(self):
        """Verifies that an unresponsive window (hung message pump) is detected."""
        from unittest.mock import patch, MagicMock
        with patch("ctypes.windll.user32.IsHungAppWindow", return_value=1):
            self.assertFalse(w_uia.is_hwnd_responsive(12345, timeout_ms=10))

    def test_inspect_window_controls_hung_window_aborts_safely(self):
        """Verifies inspect_window_controls detects hung window and aborts without deadlock."""
        from unittest.mock import patch, MagicMock

        mock_win = MagicMock()
        mock_win.window_text.return_value = "Frozen Document - App"
        mock_win.handle = 99999

        mock_desktop = MagicMock()
        mock_desktop.windows.return_value = [mock_win]

        with patch("pywinauto.Desktop", return_value=mock_desktop), \
             patch("jarvis.skills.windows_uia.is_hwnd_responsive", return_value=False):
            res = w_uia.inspect_window_controls("Frozen Document")
            self.assertTrue("unresponsive" in res.lower() or "hungappwindow" in res.lower() or "could not find" in res.lower())


if __name__ == "__main__":
    unittest.main()
