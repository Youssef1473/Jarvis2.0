"""Unit tests for the Graphify codebase knowledge graph skill integration."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.skills.base import SkillRegistry


class TestGraphifySkillSuite(unittest.TestCase):
    """Verifies that query_codebase_graph correctly exposes the Graphify AST knowledge graph."""

    def test_tool_registration(self):
        tools = SkillRegistry.get_all_tools()
        self.assertIn("query_codebase_graph", tools)

    def test_query_codebase_graph_real_or_mock(self):
        # 1. Negative test: Unauthorized execution fails closed
        with patch.object(Path, "exists", return_value=True):
            unauth_res = SkillRegistry.execute("query_codebase_graph", query="ManagedServiceManager")
            self.assertIn("Authorization Required", unauth_res)

        # 2. Contained batch execution mock
        mock_proc = subprocess.CompletedProcess(
            args=["graphify"],
            returncode=0,
            stdout="Graph result for ManagedServiceManager",
            stderr="",
        )
        with patch.object(Path, "exists", return_value=True), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc):
            res = SkillRegistry.execute("query_codebase_graph", query="ManagedServiceManager")
            self.assertIn("ManagedServiceManager", res)

    def test_missing_graph_file_handling(self):
        with patch.object(Path, "exists", return_value=False):
            res = SkillRegistry.execute("query_codebase_graph", query="test_symbol")
            self.assertIn("No codebase knowledge graph found", res)

    def test_explain_and_path_modes(self):
        mock_path_proc = subprocess.CompletedProcess(
            args=["graphify", "path"],
            returncode=0,
            stdout="Path found: A -> B",
            stderr="",
        )
        mock_explain_proc = subprocess.CompletedProcess(
            args=["graphify", "explain"],
            returncode=0,
            stdout="Explain: SafetyGate guards high risk tools",
            stderr="",
        )
        with patch.object(Path, "exists", return_value=True), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=[mock_path_proc, mock_explain_proc]):
            res_path = SkillRegistry.execute("query_codebase_graph", query="JarvisBrain SafetyGate", mode="path")
            self.assertIn("Path found", res_path)

            res_explain = SkillRegistry.execute("query_codebase_graph", query="SafetyGate", mode="explain")
            self.assertIn("SafetyGate", res_explain)


if __name__ == "__main__":
    unittest.main()
