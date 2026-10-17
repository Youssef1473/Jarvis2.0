"""Automated tests for Closed-Loop Verification & MemGPT Core User Preferences in Jarvis 2.0.
Verifies self-editing core user profile blocks in SQLite, prompt assembly injection,
and closed-loop post-action verification gates on desktop OS tools.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory import MemoryManager
from jarvis.skills.base import SkillRegistry
import jarvis.skills.productivity as prod_skills
import jarvis.skills.os_control as os_skills
from jarvis.core.brain import JarvisBrain


class TestClosedLoopAndCorePreferencesSuite(unittest.TestCase):
    """Verifies core preference self-editing memory and closed-loop verification gates."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_prefs.db"
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": str(self.db_path)},
            "llm": {
                "provider": "antigravity",
                "antigravity_model": "gemini-3.8-flash-low",
            },
            "safety": {"confirm_destructive": True},
        }
        self.memory = MemoryManager(self.config)
        prod_skills.set_memory_manager(self.memory)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_update_and_get_user_preferences_memory(self):
        """Verifies storing and retrieving core user preferences in SQLite WAL."""
        res = self.memory.update_user_preference("preferred_browser", "brave")
        self.assertIn("Core preference updated", res)
        self.assertIn("preferred_browser", res)

        self.memory.update_user_preference("call_me", "Boss")
        self.memory.update_user_preference("voice_speed", "1.10")

        prefs = self.memory.get_user_preferences()
        self.assertEqual(prefs.get("preferred_browser"), "brave")
        self.assertEqual(prefs.get("call_me"), "Boss")
        self.assertEqual(prefs.get("voice_speed"), "1.10")

    def test_format_core_preferences_block(self):
        """Verifies structured formatting of core user preferences for working memory."""
        self.memory.update_user_preference("preferred_browser", "brave")
        self.memory.update_user_preference("work_mode", "coding")

        block = self.memory.format_core_preferences_block()
        self.assertIn("Core User Profile & Preferences", block)
        self.assertIn("- preferred_browser: brave", block)
        self.assertIn("- work_mode: coding", block)

    def test_brain_system_instruction_injects_core_preferences(self):
        """Verifies that JarvisBrain._build_system_instruction injects Core User Preferences into prompt."""
        self.memory.update_user_preference("preferred_browser", "brave")
        safety = MagicMock()
        brain = JarvisBrain(self.config, self.memory, safety)

        prompt = brain._get_system_instruction(user_text="help me search something")
        self.assertIn("Core User Profile & Preferences", prompt)
        self.assertIn("preferred_browser: brave", prompt)

    def test_productivity_tools_user_preferences(self):
        """Verifies update_user_preference and get_user_preferences skills execute cleanly via SkillRegistry."""
        set_res = SkillRegistry.execute("update_user_preference", key="primary_project", value="Jarvis 2.0")
        self.assertIn("Core preference updated", set_res)

        get_res = SkillRegistry.execute("get_user_preferences")
        self.assertIn("Sir's Active Core Preferences", get_res)
        self.assertIn("primary_project: Jarvis 2.0", get_res)

    def test_adjust_volume_closed_loop_verification(self):
        """Verifies adjust_volume returns verified hardware readout."""
        fake_spk = MagicMock()
        fake_spk.volume_percent = 45.0
        fake_spk.EndpointVolume.GetMute.return_value = 0

        with patch("pycaw.pycaw.AudioUtilities.GetSpeakers", return_value=fake_spk):
            res = SkillRegistry.execute("adjust_volume", percent=45)
            self.assertIn("verified at 45%", res)

            # Test mute toggle verification
            fake_spk.EndpointVolume.GetMute.side_effect = [0, 1]  # Before mute: 0, after mute: 1
            mute_res = SkillRegistry.execute("adjust_volume", action="mute")
            self.assertIn("Volume muted, Sir.", mute_res)

    def test_focus_window_closed_loop_verification(self):
        """Verifies focus_window executes post-action verification."""
        with patch("jarvis.skills.windows_uia.focus_window_uia", return_value=None), \
             patch("win32gui.EnumWindows") as mock_enum, \
             patch("win32gui.ShowWindow"), \
             patch("win32gui.SetForegroundWindow"), \
             patch("win32gui.GetForegroundWindow", return_value=12345), \
             patch("win32gui.GetWindowText", return_value="Brave Browser"):

            def fake_enum(cb, _):
                # Simulate matching hwnd
                cb(12345, None)
                return True

            mock_enum.side_effect = fake_enum
            with patch("win32gui.IsWindowVisible", return_value=True):
                res = SkillRegistry.execute("focus_window", title="brave")
                self.assertIn("Switched focus to", res)

    def test_launch_or_focus_application_closed_loop(self):
        """Verifies launch_or_focus_application invokes launch and verifies focus."""
        with patch("jarvis.skills.windows_uia.focus_window_uia", side_effect=[None, "Spotify Premium"]), \
             patch("jarvis.skills.os_control.focus_window", return_value="No open Spotify window was found"), \
             patch("jarvis.skills.os_control.launch_application", return_value="Application 'spotify' launched, Sir.") as mock_launch:
            res = SkillRegistry.execute("launch_or_focus_application", app_name="spotify")
            self.assertIn("Verified: Launched and focused 'Spotify Premium', Sir.", res)
            mock_launch.assert_called_once_with("spotify")


if __name__ == "__main__":
    unittest.main()
