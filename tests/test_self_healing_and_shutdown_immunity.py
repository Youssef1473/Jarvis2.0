"""Unit tests for self-healing desktop/browser cascades and autonomous shutdown crash immunity."""
import unittest
from unittest.mock import patch, MagicMock
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.antigravity_bridge import AntigravityBridge, AntigravityDaemon
from jarvis.core.orchestrator import JarvisOrchestrator
from jarvis.skills.os_control import focus_window, close_browser_tab, open_browser_tab


class TestSelfHealingAndShutdownImmunity(unittest.TestCase):
    """Verifies process group isolation, stdin disconnect immunity, and desktop fallback cascades."""

    def test_antigravity_daemon_process_group_flags(self):
        """Verifies AntigravityDaemon spawns workers with CREATE_NEW_PROCESS_GROUP on Windows."""
        daemon = AntigravityDaemon(agy_path="dummy_agy.exe", default_model="gemini-3.8-flash-medium")
        with patch("subprocess.Popen") as mock_popen, \
             patch("os.path.exists", return_value=True), \
             patch.object(daemon, "stop"):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0
            mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "test_cid"}\n'
            mock_popen.return_value = mock_proc

            daemon.start(model="gemini-3.8-flash-medium")
            self.assertTrue(mock_popen.called)
            _, kwargs = mock_popen.call_args
            flags = kwargs.get("creationflags", 0)
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                self.assertTrue(
                    flags & subprocess.CREATE_NEW_PROCESS_GROUP,
                    "Daemon worker must have CREATE_NEW_PROCESS_GROUP flag set"
                )
            self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)

    def test_antigravity_streaming_subprocess_flags(self):
        """Verifies AntigravityBridge._generate_streaming uses CREATE_NEW_PROCESS_GROUP and DEVNULL stderr."""
        bridge = AntigravityBridge()
        bridge.agy_path = "dummy_agy.exe"

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout.readline.side_effect = [
                '{"event": "step_update", "step_update": {"text_delta": "Hello"}}\n',
                '{"event": "result", "result": {"response": "Hello, Sir."}}\n',
                ""
            ]
            mock_proc.stderr = None
            mock_popen.return_value = mock_proc

            res = bridge._generate_streaming(
                prompt="Test prompt",
                model="gemini-3.8-flash-medium",
                sentence_callback=lambda s: None,
                timeout=10.0,
            )
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertEqual(res.get("response"), "Hello, Sir.")
            _, kwargs = mock_popen.call_args
            flags = kwargs.get("creationflags", 0)
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                self.assertTrue(
                    flags & subprocess.CREATE_NEW_PROCESS_GROUP,
                    "Streaming subprocess must have CREATE_NEW_PROCESS_GROUP flag set"
                )
            self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)

    def test_orchestrator_stdin_eof_immunity(self):
        """Verifies interactive loop handles EOFError on stdin by transitioning to daemon mode without crash."""
        config = {"audio": {"sample_rate": 16000}, "llm": {"provider": "ollama"}}
        with patch("jarvis.core.orchestrator.JarvisHUD"), \
             patch("jarvis.core.orchestrator.JarvisHeartbeat"), \
             patch("jarvis.core.orchestrator.WakeWordDetector"), \
             patch("jarvis.core.orchestrator.HotkeyListener"), \
             patch("jarvis.core.orchestrator.TextToSpeech"), \
             patch("jarvis.core.orchestrator.SpeechToText"), \
             patch("jarvis.core.orchestrator.SoundEffects"), \
             patch("jarvis.core.orchestrator.MemoryManager"), \
             patch("jarvis.core.orchestrator.JarvisBrain"):

            orch = JarvisOrchestrator(config)
            orch._running = True

            # Mock stdin isatty to True
            mock_stdin = MagicMock()
            mock_stdin.isatty.return_value = True

            # First input call raises EOFError, simulating disconnected stdin
            with patch.object(orch, "start"), \
                 patch("sys.stdin", mock_stdin), \
                 patch("builtins.input", side_effect=EOFError), \
                 patch("time.sleep", side_effect=lambda s: setattr(orch, "_running", False)):
                orch.run_interactive_loop()

            self.assertFalse(orch._running)
            self.assertTrue(orch._stopped)

    def test_focus_window_self_healing_launch_cascade(self):
        """Verifies focus_window automatically cascades to launch_application if window is closed."""
        with patch("jarvis.skills.windows_uia.focus_window_uia", side_effect=[None, "Spotify Premium"]), \
             patch("win32gui.EnumWindows") as mock_enum, \
             patch("jarvis.skills.os_control.launch_application", return_value="Application 'spotify' launched, Sir.") as mock_launch:
            res = focus_window("spotify")
            mock_launch.assert_called_once_with("spotify")
            self.assertIn("launched and focused", res.lower())

    def test_close_browser_tab_cross_browser_fallback(self):
        """Verifies close_browser_tab cascades across candidate browsers if default Brave window is absent."""
        with patch("jarvis.skills.browser_cdp.close_cdp_tab", return_value=None), \
             patch("jarvis.skills.windows_uia.focus_window_uia", return_value=None), \
             patch("win32gui.EnumWindows") as mock_enum, \
             patch("win32api.keybd_event") as mock_keybd_event:

            # Simulate enum windows: no brave, but chrome window is found
            def fake_enum(callback, _):
                callback(101, None)
                return True

            with patch("win32gui.IsWindowVisible", return_value=True), \
                 patch("win32gui.GetWindowText", side_effect=lambda h: "GitHub - Google Chrome" if h == 101 else ""), \
                 patch("win32gui.ShowWindow"), \
                 patch("win32gui.SetForegroundWindow"):
                mock_enum.side_effect = fake_enum
                res = close_browser_tab(browser="brave")
                self.assertIn("closed active tab", res.lower())
                # Verify native Win32 VK_CONTROL + VK_W sequence fired.
                self.assertGreaterEqual(mock_keybd_event.call_count, 2)



if __name__ == "__main__":
    unittest.main()
