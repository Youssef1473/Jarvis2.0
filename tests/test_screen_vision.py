"""Unit tests for multimodal screen vision and perception skill."""
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jarvis.skills.base import SkillRegistry
import jarvis.skills.screen_vision as screen_vision


class TestScreenVisionSkill(unittest.TestCase):
    """Verifies screen vision skill registration, Visual Privacy Guard, Gemini analysis, and fallbacks."""

    def test_skills_registered(self):
        """Verifies inspect_screen and see_screen are registered in SkillRegistry."""
        tools = SkillRegistry.get_all_tools()
        self.assertIn("inspect_screen", tools)
        self.assertIn("see_screen", tools)

    @patch("jarvis.skills.screen_vision._get_active_window_details")
    @patch("jarvis.skills.screen_vision.ScreenPerception.capture_screen_bytes")
    def test_capture_failure_graceful(self, mock_capture, mock_window):
        """When screen capture fails, an informative message must be returned."""
        mock_window.return_value = ("Terminal", "pwsh.exe")
        mock_capture.return_value = None
        result = screen_vision.inspect_screen()
        self.assertIn("unable to capture your display", result)

    @patch("jarvis.skills.screen_vision._get_active_window_details")
    @patch("jarvis.skills.screen_vision._analyze_screen_with_gemini")
    @patch("jarvis.skills.screen_vision.ScreenPerception.capture_screen_bytes")
    def test_gemini_vision_success(self, mock_capture, mock_gemini, mock_window):
        """When Gemini analysis succeeds, its visual description must be returned."""
        mock_window.return_value = ("Visual Studio Code", "Code.exe")
        mock_capture.return_value = b"\xff\xd8\xff\xe0fakejpegdata"
        mock_gemini.return_value = "VS Code is open displaying the test suite."

        result = screen_vision.inspect_screen(query="What is open?")
        self.assertEqual(result, "VS Code is open displaying the test suite.")

    @patch("jarvis.skills.screen_vision._get_active_window_details")
    @patch("jarvis.skills.os_control.get_active_window")
    @patch("jarvis.skills.screen_vision._analyze_screen_with_gemini")
    @patch("jarvis.skills.screen_vision.ScreenPerception.capture_screen_bytes")
    def test_gemini_fallback_to_window_telemetry(self, mock_capture, mock_gemini, mock_window, mock_details):
        """When cloud vision analysis returns None, fallback to OS window telemetry."""
        mock_details.return_value = ("Terminal", "pwsh.exe")
        mock_capture.return_value = b"\xff\xd8\xff\xe0fakejpegdata"
        mock_gemini.return_value = None
        mock_window.return_value = "Active Application: Terminal (Process: pwsh.exe)"

        result = screen_vision.inspect_screen()
        self.assertIn("Visual cloud inference was momentarily unavailable", result)
        self.assertIn("Active Application: Terminal", result)

    @patch("jarvis.skills.screen_vision.inspect_screen")
    def test_see_screen_alias(self, mock_inspect):
        """Verifies see_screen delegates to inspect_screen."""
        mock_inspect.return_value = "Test response"
        res = screen_vision.see_screen(focus="Check tabs")
        mock_inspect.assert_called_once_with(query="Check tabs")
        self.assertEqual(res, "Test response")

    @patch("jarvis.skills.screen_vision.ScreenPerception.capture_screen_bytes")
    @patch("jarvis.skills.screen_vision._get_active_window_details")
    def test_visual_privacy_guard_blocks_sensitive_vault(self, mock_details, mock_capture):
        """Visual Privacy Guard must halt capture when a password vault is in foreground."""
        mock_details.return_value = ("Bitwarden - My Vault", "Bitwarden.exe")
        result = screen_vision.inspect_screen()
        self.assertIn("Visual capture paused for your security", result)
        self.assertIn("Bitwarden - My Vault", result)
        mock_capture.assert_not_called()

    @patch("jarvis.skills.screen_vision.ScreenPerception.capture_screen_bytes")
    @patch("jarvis.skills.screen_vision._get_active_window_details")
    def test_visual_privacy_guard_blocks_online_banking(self, mock_details, mock_capture):
        """Visual Privacy Guard must halt capture when an online banking portal is active."""
        mock_details.return_value = ("Chase Online - Log in to your account - Brave", "brave.exe")
        result = screen_vision.inspect_screen()
        self.assertIn("Visual capture paused for your security", result)
        mock_capture.assert_not_called()

    @patch("jarvis.skills.screen_vision._analyze_screen_with_gemini")
    @patch("jarvis.skills.screen_vision.ScreenPerception.capture_screen_bytes")
    @patch("jarvis.skills.screen_vision._get_active_window_details")
    def test_visual_privacy_guard_allows_dev_code_editing(self, mock_details, mock_capture, mock_gemini):
        """Visual Privacy Guard must not false-positive on developer code files (e.g. login.py in VS Code)."""
        mock_details.return_value = ("login.py - Jarvis_2.0 - Visual Studio Code", "Code.exe")
        mock_capture.return_value = b"\xff\xd8\xff\xe0fakejpegdata"
        mock_gemini.return_value = "Viewing login script in editor."

        result = screen_vision.inspect_screen()
        self.assertEqual(result, "Viewing login script in editor.")
        mock_capture.assert_called_once()

    def test_check_visual_privacy_matrix(self):
        """Direct unit verification of privacy classification rules."""
        # Sensitive password managers and crypto wallets
        self.assertIsNotNone(screen_vision.check_visual_privacy("1Password", "1password.exe"))
        self.assertIsNotNone(screen_vision.check_visual_privacy("My Vault", "bitwarden.exe"))
        self.assertIsNotNone(screen_vision.check_visual_privacy("MetaMask Notification", "chrome.exe"))
        self.assertIsNotNone(screen_vision.check_visual_privacy("Bank of America - Sign In", "firefox.exe"))
        self.assertIsNotNone(screen_vision.check_visual_privacy("Personal KeePass Database (kdbx)", "keepass.exe"))

        # Safe windows
        self.assertIsNone(screen_vision.check_visual_privacy("Administrator: Windows PowerShell", "pwsh.exe"))
        self.assertIsNone(screen_vision.check_visual_privacy("Google Search - Python tutorials - Brave", "brave.exe"))
        self.assertIsNone(screen_vision.check_visual_privacy("auth_login.py - Jarvis - Visual Studio Code", "code.exe"))

    def test_save_latest_screenshot(self):
        """Verifies _save_latest_screenshot writes bytes to data/screenshots/latest_screen.jpg."""
        test_bytes = b"\xff\xd8\xff\xe0testjpegcontent"
        saved_path = screen_vision._save_latest_screenshot(test_bytes)
        self.assertIsNotNone(saved_path)
        self.assertTrue(Path(saved_path).exists())
        self.assertEqual(Path(saved_path).read_bytes(), test_bytes)


if __name__ == "__main__":
    unittest.main()
