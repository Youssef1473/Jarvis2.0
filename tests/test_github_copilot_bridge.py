"""Tests for the restricted GitHub Copilot CLI fallback."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.github_bridge import GitHubCopilotBridge
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestGitHubCopilotBridge(unittest.TestCase):
    def setUp(self):
        self.bridge = GitHubCopilotBridge(
            {"llm": {"github_copilot_path": sys.executable, "github_copilot_timeout_seconds": 4}}
        )

    def test_success_uses_no_tools_agent_and_safe_subprocess_options(self):
        process_result = subprocess.CompletedProcess(
            args=["copilot"], returncode=0, stdout="A considered reply, Sir.", stderr=""
        )
        with patch("jarvis.core.github_bridge.subprocess.run", return_value=process_result) as run:
            result = self.bridge.generate_response(
                "Explain the current task.",
                system_instruction="Answer in the Jarvis voice.",
            )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["response"], "A considered reply, Sir.")
        command = run.call_args.args[0]
        self.assertIn("--agent", command)
        self.assertIn(GitHubCopilotBridge.AGENT_NAME, command)
        self.assertIn("--deny-tool=write", command)
        self.assertIn("--deny-tool=shell", command)
        self.assertNotIn("--allow-all-tools", command)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["close_fds"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["timeout"], 4)

        agent_text = self.bridge.agent_path.read_text(encoding="utf-8")
        self.assertIn("tools: []", agent_text)

    def test_nonzero_cli_exit_is_an_error_not_a_success_shaped_fallback(self):
        process_result = subprocess.CompletedProcess(
            args=["copilot"], returncode=1, stdout="", stderr="Authentication required."
        )
        with patch("jarvis.core.github_bridge.subprocess.run", return_value=process_result):
            result = self.bridge.generate_response("Answer this.")

        self.assertEqual(result["status"], "ERROR")
        self.assertIn("Authentication failed", result["response"])

    def test_timeout_is_reported_as_timeout(self):
        with patch(
            "jarvis.core.github_bridge.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["copilot"], timeout=1),
        ):
            result = self.bridge.generate_response("Answer this.", timeout=1)

        self.assertEqual(result["status"], "TIMEOUT")
        self.assertIn("timed out", result["response"])

    def test_missing_cli_is_unavailable(self):
        bridge = GitHubCopilotBridge(
            {"llm": {"github_copilot_path": str(Path.cwd() / "missing-copilot.exe")}}
        )
        self.assertFalse(bridge.is_available())
        self.assertEqual(bridge.generate_response("Hello.")["status"], "ERROR")


class TestAntigravityCopilotQuotaFallback(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.memory = MemoryManager(self.config)
        self.brain = JarvisBrain(self.config, self.memory, SafetyGate(self.config))
        self.brain.active_tool_names = frozenset()
        self.brain.antigravity_auto_tiering = False
        self.brain.antigravity_model = "gemini-3.8-flash-low"
        self.brain.antigravity_bridge = MagicMock()
        self.brain.antigravity_bridge.is_claude_exhausted.return_value = False
        self.brain.antigravity_bridge._is_token_exhaustion_error.side_effect = (
            lambda error: "quota" in error.lower() or "429" in error
        )
        self.brain.github_bridge = MagicMock()
        self.brain.github_bridge.is_available.return_value = True

    def tearDown(self):
        self.brain.shutdown()

    def _run_antigravity_turn(self, response):
        self.brain.antigravity_bridge.generate_response.return_value = response
        with (
            patch.object(self.brain, "_get_system_instruction", return_value="System"),
            patch.object(self.memory, "log_interaction"),
            patch.object(self.memory, "auto_learn_from_interaction"),
        ):
            return self.brain._process_antigravity_query("Please explain the architecture.")

    def test_quota_error_hands_off_to_copilot_without_executing_its_text_as_a_tool(self):
        self.brain.antigravity_bridge.generate_response.return_value = {
            "status": "ERROR",
            "response": "RESOURCE_EXHAUSTED: quota reached",
            "duration_seconds": 0.1,
        }
        tool_shaped_text = '{"tool":"lock_workstation","arguments":{}}'
        self.brain.github_bridge.generate_response.return_value = {
            "status": "SUCCESS",
            "response": tool_shaped_text,
            "duration_seconds": 0.2,
        }
        with (
            patch.object(self.brain, "_get_system_instruction", return_value="System"),
            patch.object(self.memory, "log_interaction"),
            patch.object(self.memory, "auto_learn_from_interaction"),
            patch("jarvis.core.brain.SkillRegistry.execute") as execute,
        ):
            reply = self.brain._process_antigravity_query("Please explain the architecture.")

        self.assertEqual(reply, tool_shaped_text)
        self.brain.github_bridge.generate_response.assert_called_once()
        execute.assert_not_called()
        self.assertGreater(self.brain.last_latencies["github"], 0)

    def test_non_quota_antigravity_error_does_not_switch_provider(self):
        reply = self._run_antigravity_turn(
            {"status": "ERROR", "response": "Connection refused.", "duration_seconds": 0.1}
        )

        self.assertIn("Connection refused", reply)
        self.brain.github_bridge.generate_response.assert_not_called()

    def test_unknown_antigravity_quota_error_is_not_reported_as_success(self):
        self.brain.github_bridge.is_available.return_value = False
        reply = self._run_antigravity_turn(
            {"status": "ERROR", "response": "RESOURCE_EXHAUSTED: quota reached", "duration_seconds": 0.1}
        )

        self.assertIn("quota reached", reply)
        self.assertNotEqual(reply, "Request acknowledged, Sir.")

    def test_quota_signal_survives_later_candidate_error(self):
        bridge = AntigravityBridge({"llm": {"antigravity_use_daemon": False}})
        bridge.use_daemon = False
        results = [
            {"status": "ERROR", "response": "429 RESOURCE_EXHAUSTED: quota reached"},
            {"status": "ERROR", "response": "CLI exited with code 15"},
        ]
        with (
            patch.object(bridge, "is_available", return_value=True),
            patch.object(
                bridge,
                "get_candidate_cascade",
                return_value=["gemini-3.8-flash-low", "gemini-3.8-flash-medium"],
            ),
            patch.object(bridge, "_generate_batch", side_effect=results),
        ):
            result = bridge.generate_response(
                "Respond with exactly two words: System Online.",
                model="gemini-3.8-flash-low",
            )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["response"], "System Online.")


if __name__ == "__main__":
    unittest.main()
