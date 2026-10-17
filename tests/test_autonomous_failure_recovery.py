"""Tests for autonomous failure recovery and deterministic fallback cascades across core desktop tools."""
import unittest
from unittest.mock import patch, MagicMock
from jarvis.skills.base import SkillRegistry
import jarvis.skills.os_control as os_ctrl
import jarvis.skills.windows_uia as w_uia


class TestAutonomousFailureRecovery(unittest.TestCase):
    """Verifies deterministic fallback cascades across core OS control skills."""

    def test_focus_window_launches_app_on_miss(self):
        """When a window is not open, focus_window automatically launches the known application."""
        with patch("jarvis.skills.windows_uia.focus_window_uia", side_effect=[None, "Spotify Premium"]), \
             patch("win32gui.EnumWindows") as mock_enum, \
             patch("jarvis.skills.os_control.launch_application", return_value="Application 'spotify' launched, Sir.") as mock_launch:
            
            def no_match_enum(cb, _):
                return True
            mock_enum.side_effect = no_match_enum

            res = os_ctrl.focus_window("spotify")
            self.assertIn("was not open; launched", res)
            mock_launch.assert_called_once_with("spotify")

    def test_focus_window_unknown_app_returns_no_match(self):
        """When an unknown target is queried and not open, focus_window returns standard not found."""
        with patch("jarvis.skills.windows_uia.focus_window_uia", return_value=None), \
             patch("win32gui.EnumWindows") as mock_enum:
            
            def no_match_enum(cb, _):
                return True
            mock_enum.side_effect = no_match_enum

            res = os_ctrl.focus_window("nonexistent_phantom_app_8492")
            self.assertIn("no open window matching", res.lower())

    def test_close_application_wm_close_fallback(self):
        """When no process matches by name, close_application attempts graceful window close via WM_CLOSE."""
        fake_proc_iter = []
        with patch("psutil.process_iter", return_value=fake_proc_iter), \
             patch("win32gui.EnumWindows") as mock_enum, \
             patch("win32gui.IsWindowVisible", return_value=True), \
             patch("win32gui.GetWindowText", return_value="Custom App Window"), \
             patch("win32process.GetWindowThreadProcessId", return_value=(1, 9999)), \
             patch("psutil.Process") as mock_proc, \
             patch("win32gui.PostMessage") as mock_post:
            
            mock_proc.return_value.name.return_value = "custom_app.exe"

            def match_enum(cb, _):
                cb(1234, None)
                return True
            mock_enum.side_effect = match_enum

            res = os_ctrl.close_application("custom app")
            self.assertIn("graceful window termination", res)
            mock_post.assert_called_once()

    def test_close_browser_tab_cdp_unverified_fallback(self):
        """When CDP reports tab not found, close_browser_tab falls back to UIA focus and native Win32 Ctrl+W."""
        with patch("jarvis.skills.browser_cdp.close_cdp_tab", return_value="Could not find a browser tab matching 'dashboard' via DevTools channel, Sir."), \
             patch("jarvis.skills.windows_uia.focus_window_uia", return_value="Brave Browser"), \
             patch("win32api.keybd_event") as mock_keybd_event:

            res = os_ctrl.close_browser_tab(browser="brave", tab_title="dashboard")
            self.assertIn("Closed active tab in your Brave window", res)
            # Verify native Win32 VK sequence: VK_CONTROL down, VK_W down, VK_W up, VK_CONTROL up.
            self.assertGreaterEqual(mock_keybd_event.call_count, 2)


    def test_press_hotkey_win32_fallback_on_pyautogui_failure(self):
        """When pyautogui raises an exception, press_hotkey falls back to win32api.keybd_event."""
        with patch("pyautogui.hotkey", side_effect=Exception("Display hook unavailable")), \
             patch("win32api.keybd_event") as mock_keybd:
            
            res = os_ctrl.press_hotkey("ctrl+t")
            self.assertIn("Executed hotkey 'ctrl+t' via native Win32 fallback", res)
            self.assertTrue(mock_keybd.call_count >= 2)

    def test_type_text_write_fallback_on_clipboard_failure(self):
        """When clipboard paste fails, type_text falls back to direct pyautogui.write."""
        with patch("pyautogui.hotkey", side_effect=Exception("Display paste hook busy")), \
             patch("pyautogui.write") as mock_write:
            
            res = os_ctrl.type_text("Hello World")
            self.assertIn("direct keystroke injection", res)
            mock_write.assert_called_once_with("Hello World", interval=0.005)

    def test_focus_window_uia_win32_backend_fallback(self):
        """When UIA backend finds no matching window, focus_window_uia searches pywinauto win32 backend."""
        mock_win = MagicMock()
        mock_win.window_text.return_value = "Legacy Control Panel Window"
        mock_win.is_minimized.return_value = False

        with patch("jarvis.skills.windows_uia.is_uia_supported", return_value=True), \
             patch("pywinauto.Desktop") as mock_desktop:
            
            d_uia = MagicMock()
            d_uia.windows.return_value = []
            d_win32 = MagicMock()
            d_win32.windows.return_value = [mock_win]

            mock_desktop.side_effect = [d_uia, d_win32]

            res = w_uia.focus_window_uia("legacy control")
            self.assertEqual(res, "Legacy Control Panel Window")
            mock_win.set_focus.assert_called_once()


if __name__ == "__main__":
    unittest.main()
