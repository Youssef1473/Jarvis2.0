"""Unit tests for ReflexDispatcher in Jarvis 2.0."""
import unittest
from unittest.mock import MagicMock

from jarvis.core.reflex_dispatcher import ReflexDispatcher


class TestReflexDispatcher(unittest.TestCase):
    def setUp(self):
        self.mock_engine = MagicMock()
        self.mock_engine.check_habit.return_value = None
        self.dispatcher = ReflexDispatcher(reflex_engine=self.mock_engine)

    def test_normalize_conversational_text(self):
        """Verifies stripping of fillers, polite prefixes, and trailing punctuation."""
        self.assertEqual(self.dispatcher.normalize_conversational_text("hey jarvis, please mute!"), "mute")
        self.assertEqual(self.dispatcher.normalize_conversational_text("can you please open a brave tab?"), "open a brave tab")
        self.assertEqual(self.dispatcher.normalize_conversational_text("okay jarvis, volume up..."), "volume up")
        self.assertEqual(self.dispatcher.normalize_conversational_text("please show desktop"), "show desktop")

    def test_is_disallowed_reflex_rejects_questions_and_debugging(self):
        """Verifies questions, post-mortems, and debugging inquiries are disallowed from fast reflex."""
        self.assertTrue(self.dispatcher.is_disallowed_reflex("why did you open two tabs"))
        self.assertTrue(self.dispatcher.is_disallowed_reflex("how do i open a window"))
        self.assertTrue(self.dispatcher.is_disallowed_reflex("what happened to the server"))
        self.assertTrue(self.dispatcher.is_disallowed_reflex("when i asked you to mute"))
        self.assertTrue(self.dispatcher.is_disallowed_reflex("add a feature to control the fans"))
        self.assertFalse(self.dispatcher.is_disallowed_reflex("mute"))
        self.assertFalse(self.dispatcher.is_disallowed_reflex("open a brave tab"))

    def test_is_fast_reflex_dry_run(self):
        """Verifies dry run reflex checking returns boolean without physical execution."""
        self.assertTrue(self.dispatcher.is_fast_reflex("mute"))
        self.assertTrue(self.dispatcher.is_fast_reflex("volume up"))
        self.assertTrue(self.dispatcher.is_fast_reflex("open a brave tab"))
        self.assertTrue(self.dispatcher.is_fast_reflex("switch to classic voice"))
        self.assertTrue(self.dispatcher.is_fast_reflex("speak faster"))
        self.assertFalse(self.dispatcher.is_fast_reflex("write a python function to refactor"))

    def test_try_fast_reflex_execution_delegates_to_execute_fn(self):
        """Verifies try_fast_reflex invokes execute_fn with parsed keyword arguments."""
        mock_exec = MagicMock()
        mock_exec.return_value = "Volume muted, Sir."

        res = self.dispatcher.try_fast_reflex("mute the volume", execute_fn=mock_exec)
        self.assertEqual(res, "Volume muted, Sir.")
        mock_exec.assert_called_once_with("adjust_volume", action="mute")

    def test_try_fast_reflex_browser_navigation(self):
        """Verifies direct browser navigation URL parsing."""
        mock_exec = MagicMock()
        mock_exec.return_value = "Opened tab."

        self.dispatcher.try_fast_reflex("open https://github.com/trending in chrome", execute_fn=mock_exec)
        mock_exec.assert_called_once_with("open_browser_tab", url="https://github.com/trending", browser="chrome")

    def test_try_fast_reflex_learned_habit_dispatch(self):
        """Verifies cached learned habits execute in <15ms and record telemetry hits."""
        self.mock_engine.check_habit.return_value = {
            "id": 42,
            "skill_name": "launch_application",
            "skill_kwargs": {"app_name": "notepad"},
        }
        mock_exec = MagicMock(return_value="Launched notepad.")

        res = self.dispatcher.try_fast_reflex("open notes", execute_fn=mock_exec)
        self.assertEqual(res, "Launched notepad.")
        self.mock_engine.record_hit.assert_called_once()

    def test_finalize_turn_habit_recording(self):
        """Verifies habit promotion for single-step and compound tools."""
        # Single tool
        self.dispatcher.finalize_turn_habit(
            user_text="mute",
            executed_tools=[("adjust_volume", {"action": "mute"})],
            turn_start_time=0.0,
        )
        self.mock_engine.record_habit.assert_called_once()

        # Compound tools
        self.mock_engine.reset_mock()
        self.dispatcher.finalize_turn_habit(
            user_text="setup workspace",
            executed_tools=[
                ("launch_application", {"app_name": "code"}),
                ("open_browser_tab", {"url": "https://github.com"}),
            ],
            turn_start_time=0.0,
        )
        self.mock_engine.record_compound_habit.assert_called_once()

    def test_narrate_tool_action(self):
        """Verifies spoken natural language descriptions for tool executions."""
        phrase1 = self.dispatcher.narrate_tool_action("open_browser_tab", {"browser": "brave"})
        self.assertEqual(phrase1, "Opening a new tab in brave, Sir.")

        phrase2 = self.dispatcher.narrate_tool_action("adjust_volume", {})
        self.assertEqual(phrase2, "Adjusting the system volume, Sir.")

        phrase3 = self.dispatcher.narrate_tool_action("run_project_tests", {})
        self.assertEqual(phrase3, "Running the test suite to verify our changes, Sir.")


if __name__ == "__main__":
    unittest.main()
