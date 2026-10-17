"""Automated tests for Tier 4 Proactive Desktop Presence (Wake-Decide-Sleep Architecture) in Jarvis 2.0.
Verifies Win32 idle tracking, active window observation, context staging, ambient HUD broadcast,
and non-intrusive meeting reminders on user return.
"""
import datetime
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.heartbeat import JarvisHeartbeat
from jarvis.core.memory import MemoryManager


class TestProactiveDesktopPresenceSuite(unittest.TestCase):
    """Verifies user idle tracking, context staging, and ambient presence evaluation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "test_memory.db", "background_learning": False},
            "heartbeat": {
                "enabled": True,
                "idle_threshold_seconds": 300,
                "desktop_presence": True,
            },
        }
        self.memory = MemoryManager(self.config)

        # Mock orchestrator
        self.orchestrator = MagicMock()
        self.orchestrator.memory = self.memory
        self.orchestrator._is_recording = False
        self.orchestrator.tts.is_speaking = False

        self.heartbeat = JarvisHeartbeat(self.orchestrator, self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_win32_idle_and_active_window_query(self):
        """Verifies get_idle_seconds and get_active_window_title execute safely without exceptions."""
        idle_s = self.heartbeat.get_idle_seconds()
        self.assertIsInstance(idle_s, float)
        self.assertGreaterEqual(idle_s, 0.0)

        title = self.heartbeat.get_active_window_title()
        self.assertIsInstance(title, str)

    def test_presence_idle_transition_and_hud_broadcast(self):
        """Verifies heartbeat detects user absence and dispatches ambient telemetry to HUD."""
        with patch.object(self.heartbeat, "get_idle_seconds", return_value=320.0), \
             patch.object(self.heartbeat, "get_active_window_title", return_value="Desktop"):
            self.heartbeat._check_desktop_presence()

            self.assertTrue(self.heartbeat._is_user_idle)
            self.orchestrator.broadcast_ambient_telemetry.assert_any_call({
                "event": "user_idle",
                "idle_duration_s": 320.0,
            })

    def test_presence_user_return_and_context_staging(self):
        """Verifies user return from idle resets state, stages context in memory, and notifies HUD."""
        self.heartbeat._is_user_idle = True
        self.heartbeat._last_active_window = "Previous App"

        with patch.object(self.heartbeat, "get_idle_seconds", return_value=2.5), \
             patch.object(self.heartbeat, "get_active_window_title", return_value="Visual Studio Code - Jarvis_2.0"):
            self.heartbeat._check_desktop_presence()

            self.assertFalse(self.heartbeat._is_user_idle)

            # Staged in memory
            staged = self.memory.get_fact("active_desktop_context")
            self.assertEqual(staged, "Visual Studio Code - Jarvis_2.0")

            # Telemetry dispatched
            calls = self.orchestrator.broadcast_ambient_telemetry.call_args_list
            events = [c[0][0]["event"] for c in calls if "event" in c[0][0]]
            self.assertIn("user_returned", events)

    def test_urgent_calendar_alert_on_return(self):
        """Verifies imminent calendar events (<=10m away) trigger polite greeting upon user return."""
        self.heartbeat._is_user_idle = True

        # Add event starting 5 minutes from now
        now_dt = datetime.datetime.now()
        start_time_str = (now_dt + datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        self.memory.add_calendar_event(
            title="Systems Architecture Review",
            start_time=start_time_str,
            location="Room 402",
        )

        with patch.object(self.heartbeat, "get_idle_seconds", return_value=1.0), \
             patch.object(self.heartbeat, "get_active_window_title", return_value="Chrome"), \
             patch.object(self.heartbeat, "speak_proactive", return_value=True) as mock_speak:
            self.heartbeat._check_desktop_presence()

            mock_speak.assert_called_once()
            spoken_text = mock_speak.call_args[0][0]
            self.assertIn("Welcome back, Sir", spoken_text)
            self.assertIn("Systems Architecture Review", spoken_text)

    def test_window_focus_change_detection(self):
        """Verifies active window focus changes are staged into memory and dispatched to HUD."""
        self.heartbeat._last_active_window = "Old Window"

        with patch.object(self.heartbeat, "get_idle_seconds", return_value=0.5), \
             patch.object(self.heartbeat, "get_active_window_title", return_value="GitHub Desktop"):
            self.heartbeat._check_desktop_presence()

            self.assertEqual(self.heartbeat._last_active_window, "GitHub Desktop")
            self.assertEqual(self.memory.get_fact("active_desktop_context"), "GitHub Desktop")


if __name__ == "__main__":
    unittest.main()
