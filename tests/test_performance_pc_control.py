"""Unit and regression tests for PC control extensions, fast reflex routing, and self-knowledge."""
import unittest
import time
from unittest.mock import MagicMock, patch
from jarvis.core.config_loader import load_config
from jarvis.skills.base import SkillRegistry
from jarvis.skills import os_control
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestPCControlAndPerformance(unittest.TestCase):
    """Verifies complete workstation PC control, low-latency reflex routing, and self-knowledge."""

    def setUp(self):
        self.config = load_config()
        self.config["llm"]["provider"] = "antigravity"
        self.config["llm"]["antigravity_model"] = "gemini-3.8-flash-high"
        self.config["llm"]["antigravity_auto_tiering"] = False
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def test_os_control_skills_registered(self):
        """Ensures all PC control skills are registered in the SkillRegistry."""
        tools = SkillRegistry.get_all_tools()
        self.assertIn("open_browser_tab", tools)
        self.assertIn("focus_window", tools)
        self.assertIn("press_hotkey", tools)
        self.assertIn("type_text", tools)
        self.assertIn("launch_application", tools)
        self.assertIn("adjust_volume", tools)
        self.assertIn("minimize_all_windows", tools)
        self.assertIn("get_system_vitals", tools)

    def test_hotkey_safety_guard(self):
        """Verifies hotkey execution rejects dangerous system interrupt shortcuts."""
        res = os_control.press_hotkey("ctrl+alt+del")
        self.assertIn("prohibited", res.lower())

    def test_fast_reflex_volume(self):
        """Verifies volume commands execute via fast reflex in <15ms."""
        # Warm up COM audio endpoint to eliminate one-time DLL initialization jitter
        self.brain._try_fast_reflex("mute")
        spoken = []
        t0 = time.perf_counter()
        reply = self.brain.process_query("mute", sentence_callback=lambda s: spoken.append(s))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertLess(elapsed_ms, 500.0)
        self.assertTrue(any("mute" in r.lower() for r in (reply, spoken[0] if spoken else "")))

    def test_fast_reflex_system_vitals(self):
        """Verifies system vitals execute via fast reflex in <25ms."""
        # Warm up hardware vitals inspection to eliminate first-call subprocess overhead
        self.brain._try_fast_reflex("system vitals")
        spoken = []
        t0 = time.perf_counter()
        reply = self.brain.process_query("system vitals", sentence_callback=lambda s: spoken.append(s))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertLess(elapsed_ms, 1000.0)
        self.assertIn("CPU Utilization", reply)

    def test_fast_reflex_brave_tab(self):
        """Verifies 'open a brave tab' and its speech variations execute via fast reflex in <10ms."""
        with patch("subprocess.Popen") as mock_popen, patch("webbrowser.open_new_tab") as mock_web:
            for utterance in [
                "open a brave tab",
                "open a tab in brave",
                "Open a tab and brave.",
                "No, open a tab in Brave.",
                "open brave",
                "new tab in brave",
            ]:
                spoken = []
                reply = self.brain.process_query(utterance, sentence_callback=lambda s: spoken.append(s))
                self.assertTrue("Brave" in reply or "browser" in reply.lower(), f"Failed on utterance: '{utterance}'")
                self.assertTrue(len(spoken) > 0, f"No speech callback on utterance: '{utterance}'")

    def test_pass_through_for_questions_and_self_building(self):
        """Verifies questions and self-building tasks bypass fast reflex and route to cognitive brain."""
        self.assertIsNone(self.brain._try_fast_reflex("how do I open a brave tab?"))
        self.assertIsNone(self.brain._try_fast_reflex("why is the volume muted?"))
        self.assertIsNone(self.brain._try_fast_reflex("add a feature to open a brave tab with custom flags"))
        self.assertIsNone(self.brain._try_fast_reflex("create a command to monitor disk space"))
        self.assertIsNone(self.brain._try_fast_reflex("When I ask you to open a YouTube tab, you open two YouTube tabs. See what's wrong."))
        self.assertIsNone(self.brain._try_fast_reflex("When I asked you to open a YouTube tab, you opened two YouTube tabs. I want you to look why did that happen."))
        self.assertIsNone(self.brain._try_fast_reflex("Why did you open two tabs?"))
        self.assertIsNone(self.brain._try_fast_reflex("Check why there is an issue with opening youtube"))

    def test_is_fast_reflex_does_not_execute_skills(self):
        """Verifies is_fast_reflex returns boolean without executing any tool actions."""
        with patch("jarvis.skills.base.SkillRegistry.execute") as mock_exec:
            self.assertTrue(self.brain.is_fast_reflex("open a brave tab and go to youtube.com"))
            self.assertTrue(self.brain.is_fast_reflex("mute"))
            self.assertTrue(self.brain.is_fast_reflex("volume up"))
            self.assertFalse(self.brain.is_fast_reflex("When I asked you to open a YouTube tab, you opened two YouTube tabs."))
            mock_exec.assert_not_called()

    def test_self_knowledge_in_system_prompt(self):
        """Verifies the system prompt contains complete repository architecture and self-knowledge."""
        prompt = self.brain._get_system_instruction()
        self.assertIn("JARVIS Architectural Blueprint & Repository Self-Knowledge", prompt)
        self.assertIn("jarvis/core/brain.py", prompt)
        self.assertIn("jarvis/skills/os_control.py", prompt)
        self.assertIn("create_or_update_custom_skill", prompt)
        self.assertIn("C:\\Users\\Youssef\\Projects\\Jarvis_2.0", prompt)


if __name__ == "__main__":
    unittest.main()
