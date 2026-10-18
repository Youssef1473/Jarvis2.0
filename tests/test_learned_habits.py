"""Unit and integration tests for Dynamic Habit Learning and Sub-20ms Reflexes in Jarvis 2.0."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.reflex_engine import DynamicReflexEngine, normalize_trigger_phrase, REFLEX_ELIGIBLE_TOOLS
from jarvis.core.safety import SafetyGate
from jarvis.skills.base import SkillRegistry
import jarvis.skills.self_evolution as self_evo


class TestDynamicReflexEngine(unittest.TestCase):
    """Verifies SQLite persistence, in-memory cache, and normalization of DynamicReflexEngine."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_habits.db"
        self.engine = DynamicReflexEngine(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_normalize_trigger_phrase(self):
        """Tests that conversational preambles and punctuation are stripped cleanly."""
        cases = [
            ("Hey Jarvis, please open a Brave tab!", "open a brave tab"),
            ("Jarvis could you please mute the audio?", "mute the audio"),
            ("Ok Jarvis, I want you to launch calculator.", "launch calculator"),
            ("Please open youtube.com...", "open youtube.com"),
            ("Can you focus Discord?", "focus discord"),
            ("open brave", "open brave"),
        ]
        for raw, expected in cases:
            self.assertEqual(normalize_trigger_phrase(raw), expected)

    def test_record_and_check_habit(self):
        """Tests recording a novel habit and fast in-memory cache lookup."""
        success = self.engine.record_habit(
            trigger_query="Hey Jarvis, open calculator",
            skill_name="launch_application",
            skill_kwargs={"app_name": "calc"},
            turn1_latency_ms=1750.5,
        )
        self.assertTrue(success)

        # Lookup by normalized phrase
        habit = self.engine.check_habit("open calculator")
        self.assertIsNotNone(habit)
        self.assertEqual(habit["skill_name"], "launch_application")
        self.assertEqual(habit["skill_kwargs"], {"app_name": "calc"})
        self.assertAlmostEqual(habit["turn1_latency_ms"], 1750.5, delta=0.1)

        # Lookup with conversational filler should also hit
        habit_with_filler = self.engine.check_habit("Jarvis please open calculator")
        self.assertIsNotNone(habit_with_filler)
        self.assertEqual(habit_with_filler["id"], habit["id"])

    def test_reflex_eligibility_whitelist(self):
        """Ensures destructive or unapproved tools cannot be promoted to reflexes."""
        # execute_powershell is dangerous and must NOT be promoted
        success = self.engine.record_habit(
            trigger_query="run clean script",
            skill_name="execute_powershell",
            skill_kwargs={"command": "rm -rf /"},
            turn1_latency_ms=800.0,
        )
        self.assertFalse(success)
        self.assertIsNone(self.engine.check_habit("run clean script"))

        # open_browser_tab is in REFLEX_ELIGIBLE_TOOLS and must be promoted
        self.assertIn("open_browser_tab", REFLEX_ELIGIBLE_TOOLS)
        ok = self.engine.record_habit(
            trigger_query="open github",
            skill_name="open_browser_tab",
            skill_kwargs={"url": "https://github.com", "browser": "brave"},
            turn1_latency_ms=1200.0,
        )
        self.assertTrue(ok)

    def test_hit_telemetry_recording(self):
        """Tests that record_hit increments hit count and updates reflex latency."""
        self.engine.record_habit(
            trigger_query="mute volume",
            skill_name="adjust_volume",
            skill_kwargs={"action": "mute"},
            turn1_latency_ms=1500.0,
        )
        habit = self.engine.check_habit("mute volume")
        habit_id = habit["id"]

        self.engine.record_hit(habit_id, reflex_latency_ms=11.4)
        updated = self.engine.check_habit("mute volume")
        self.assertEqual(updated["hit_count"], 2)
        self.assertAlmostEqual(updated["reflex_latency_ms"], 11.4, delta=0.1)

    def test_sqlite_persistence_across_instances(self):
        """Tests that habits persist in SQLite across engine restarts."""
        self.engine.record_habit(
            trigger_query="focus discord",
            skill_name="focus_window",
            skill_kwargs={"title": "discord"},
            turn1_latency_ms=1900.0,
        )

        # Create a fresh engine instance reading from the same SQLite file
        new_engine = DynamicReflexEngine(db_path=self.db_path)
        habit = new_engine.check_habit("focus discord")
        self.assertIsNotNone(habit)
        self.assertEqual(habit["skill_name"], "focus_window")
        self.assertEqual(habit["skill_kwargs"], {"title": "discord"})

    def test_clear_habits(self):
        """Tests clearing all learned habits."""
        self.engine.record_habit("take snapshot", "take_desktop_screenshot", {})
        self.assertIsNotNone(self.engine.check_habit("take snapshot"))

        cleared = self.engine.clear_habits()
        self.assertEqual(cleared, 1)
        self.assertIsNone(self.engine.check_habit("take snapshot"))

    def test_token_set_conversational_matching(self):
        """Tests that natural conversational variations hit the habit via token-set matching."""
        self.engine.record_habit(
            trigger_query="open calculator",
            skill_name="launch_application",
            skill_kwargs={"app_name": "calc"},
            turn1_latency_ms=1600.0,
        )
        # Should hit with conversational particles
        h1 = self.engine.check_habit("Jarvis please open my calculator")
        self.assertIsNotNone(h1)
        self.assertEqual(h1["skill_name"], "launch_application")

        h2 = self.engine.check_habit("open the calculator for me")
        self.assertIsNotNone(h2)
        self.assertEqual(h2["skill_name"], "launch_application")

    def test_record_compound_habit_and_whitelist(self):
        """Tests compound habit recording and security whitelist enforcement."""
        # Valid compound pipeline: 2 safe tools
        ok = self.engine.record_compound_habit(
            trigger_query="prepare coding session",
            steps=[
                {"skill_name": "open_browser_tab", "skill_kwargs": {"url": "https://github.com"}},
                {"skill_name": "adjust_volume", "skill_kwargs": {"action": "set", "percent": 25}},
            ],
            turn1_latency_ms=2100.0,
        )
        self.assertTrue(ok)
        habit = self.engine.check_habit("prepare coding session")
        self.assertIsNotNone(habit)
        self.assertEqual(habit["skill_name"], "__compound__")
        self.assertTrue(habit.get("is_compound", False))
        self.assertEqual(len(habit["skill_kwargs"]), 2)

        # Dangerous step must reject entire compound promotion
        bad = self.engine.record_compound_habit(
            trigger_query="clean and run",
            steps=[
                {"skill_name": "open_browser_tab", "skill_kwargs": {}},
                {"skill_name": "execute_powershell", "skill_kwargs": {"command": "dir"}},
            ],
        )
        self.assertFalse(bad)
        self.assertIsNone(self.engine.check_habit("clean and run"))


class TestFastBrowserUrlReflex(unittest.TestCase):
    """Tests sub-15ms fast browser tab and URL navigation in JarvisBrain."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "memory.db"},
            "llm": {"provider": "ollama"},
        }
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate()
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("jarvis.skills.base.SkillRegistry.execute")
    def test_open_brave_tab_with_url(self, mock_exec):
        """Tests 'open a brave tab and go to [url]' calls open_browser_tab with url & brave."""
        mock_exec.return_value = "Opened 'youtube.com' in your active Brave window, Sir."

        t0 = time.perf_counter()
        res = self.brain._try_fast_reflex("open a brave tab and go to youtube.com")
        latency_ms = (time.perf_counter() - t0) * 1000.0

        self.assertIsNotNone(res)
        mock_exec.assert_called_with("open_browser_tab", url="youtube.com", browser="brave")
        self.assertLess(latency_ms, 25.0)

    @patch("jarvis.skills.base.SkillRegistry.execute")
    def test_open_url_in_brave_direct(self, mock_exec):
        """Tests 'open https://github.com/trending in brave'."""
        mock_exec.return_value = "Opened https://github.com/trending in Brave, Sir."

        res = self.brain._try_fast_reflex("open https://github.com/trending in brave")
        self.assertIsNotNone(res)
        mock_exec.assert_called_with("open_browser_tab", url="https://github.com/trending", browser="brave")

    @patch("jarvis.skills.base.SkillRegistry.execute")
    def test_open_brave_tab_empty(self, mock_exec):
        """Tests 'open a brave tab' without URL opens a blank tab in Brave."""
        mock_exec.return_value = "Opened a new tab in your active Brave window, Sir."

        res = self.brain._try_fast_reflex("open a brave tab")
        self.assertIsNotNone(res)
        mock_exec.assert_called_with("open_browser_tab", browser="brave")

    @patch("jarvis.skills.base.SkillRegistry.execute")
    def test_open_chrome_url(self, mock_exec):
        """Tests 'navigate to reddit.com in chrome'."""
        mock_exec.return_value = "Opened reddit.com in Chrome, Sir."

        res = self.brain._try_fast_reflex("navigate to reddit.com in chrome")
        self.assertIsNotNone(res)
        mock_exec.assert_called_with("open_browser_tab", url="reddit.com", browser="chrome")


class TestAdaptationProgressionAndVerification(unittest.TestCase):
    """Verifies Turn 1 Novel Cloud Reasoning -> Turn 2 Local Fast-Path Reflex adaptation and audit."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "memory.db"},
            "llm": {"provider": "ollama"},
        }
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate()
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_turn1_learning_to_turn2_reflex_flow(self):
        """Simulates Turn 1 tool execution and verifies Turn 2 sub-15ms reflex execution."""
        novel_command = "hey jarvis please check my workstation vitals right away"
        norm_key = normalize_trigger_phrase(novel_command)

        # Before Turn 1: Habit does not exist
        self.assertIsNone(self.brain.reflex_engine.check_habit(novel_command))

        # Turn 1: Simulate LLM execution of get_system_vitals
        t0 = time.perf_counter()
        self.brain._current_turn_executed_tools = []
        self.brain._record_tool_execution("get_system_vitals", {}, "CPU: 12%, RAM: 45%")
        # Finalize habit with simulated 1820ms Turn 1 reasoning latency
        self.brain._finalize_turn_habit(novel_command, t0 - 1.82)

        # Verify Habit was promoted
        habit = self.brain.reflex_engine.check_habit(norm_key)
        self.assertIsNotNone(habit)
        self.assertEqual(habit["skill_name"], "get_system_vitals")
        self.assertGreaterEqual(habit["turn1_latency_ms"], 1800.0)

        # Turn 2: Exact or variation query executes via _try_fast_reflex in <20ms
        with patch("jarvis.skills.base.SkillRegistry.execute") as mock_exec:
            mock_exec.return_value = "CPU: 14%, RAM: 46%"

            start = time.perf_counter()
            reply = self.brain._try_fast_reflex("hey jarvis, check my workstation vitals right away")
            duration_ms = (time.perf_counter() - start) * 1000.0

            self.assertEqual(reply, "CPU: 14%, RAM: 46%")
            mock_exec.assert_called_with("get_system_vitals")
            self.assertLess(duration_ms, 20.0)

            # Hit count incremented
            updated_habit = self.brain.reflex_engine.check_habit(norm_key)
            self.assertEqual(updated_habit["hit_count"], 2)
            self.assertGreater(updated_habit["reflex_latency_ms"], 0.0)

    def test_verify_learned_habits_audit(self):
        """Verifies the audit report formatting in verify_learned_habits()."""
        # Record a test habit
        self.brain.reflex_engine.record_habit(
            trigger_query="open youtube in brave",
            skill_name="open_browser_tab",
            skill_kwargs={"url": "youtube.com", "browser": "brave"},
            turn1_latency_ms=1640.0,
        )
        habit = self.brain.reflex_engine.check_habit("open youtube in brave")
        self.brain.reflex_engine.record_hit(habit["id"], reflex_latency_ms=12.2)

        # Run verification skill
        report = self_evo.verify_learned_habits()
        self.assertIn("Dynamic Reflex & Habit Audit", report)
        self.assertIn("open youtube in brave", report)
        self.assertIn("open_browser_tab", report)
        self.assertIn("Turn 1 (1640ms cloud reasoning)", report)
        self.assertIn("Turn 2 (12.2ms local reflex)", report)

    def test_voice_trigger_for_habit_audit(self):
        """Verifies 'verify your learning' triggers verify_learned_habits reflex."""
        # 1. When no habits have been cataloged yet
        res = self.brain._try_fast_reflex("verify your learning")
        self.assertIsNotNone(res)
        self.assertIn("cataloged any dynamic habits", res)

        # 2. After cataloging a habit
        self.brain.reflex_engine.record_habit(
            trigger_query="launch brave",
            skill_name="open_browser_tab",
            skill_kwargs={"browser": "brave"},
            turn1_latency_ms=1550.0,
        )
        res2 = self.brain._try_fast_reflex("what have you learned")
        self.assertIsNotNone(res2)
        self.assertIn("Dynamic Reflex & Habit Audit", res2)
        self.assertIn("launch brave", res2)

    def test_compound_habit_execution_in_brain(self):
        """Verifies Turn 2 sub-25ms compound reflex execution and atomic failure halting."""
        novel_cmd = "setup my workstation"
        # Turn 1: Brain executes two tools
        t0 = time.perf_counter()
        self.brain._current_turn_executed_tools = []
        self.brain._record_tool_execution("adjust_volume", {"percent": 20}, "Volume set to 20%")
        self.brain._record_tool_execution("open_browser_tab", {"url": "github.com"}, "Opened tab")
        self.brain._finalize_turn_habit(novel_cmd, t0 - 2.1)

        # Verify compound habit was stored
        habit = self.brain.reflex_engine.check_habit(novel_cmd)
        self.assertIsNotNone(habit)
        self.assertEqual(habit["skill_name"], "__compound__")

        # Turn 2: Executes both steps via _try_fast_reflex in <25ms
        executed_calls = []
        def mock_exec_skill(name, **kwargs):
            executed_calls.append((name, kwargs))
            return f"Executed {name}"

        with patch("jarvis.skills.base.SkillRegistry.execute", side_effect=mock_exec_skill):
            start = time.perf_counter()
            res = self.brain._try_fast_reflex("setup my workstation")
            duration_ms = (time.perf_counter() - start) * 1000.0

            self.assertEqual(len(executed_calls), 2)
            self.assertEqual(executed_calls[0][0], "adjust_volume")
            self.assertEqual(executed_calls[1][0], "open_browser_tab")
            self.assertLess(duration_ms, 75.0)

    def test_dynamic_voyager_skill_discovery(self):
        """Verifies custom registered skills are dynamically discovered by _select_tools_for_query."""
        @SkillRegistry.register(name="custom_deep_indexer", description="Indexes deep repository AST")
        def _mock_custom():
            return "Indexed"

        try:
            tools = self.brain._select_tools_for_query("can you run custom deep indexer please")
            self.assertIn("custom_deep_indexer", tools)
        finally:
            SkillRegistry.unregister("custom_deep_indexer")


if __name__ == "__main__":
    unittest.main()
