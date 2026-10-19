"""Unit tests for the Graphify codebase knowledge graph skill integration."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jarvis.skills.base import SkillRegistry
import jarvis.skills.dev_shell


class TestGraphifySkillSuite(unittest.TestCase):
    """Verifies that query_codebase_graph correctly exposes the Graphify AST knowledge graph."""

    def test_tool_registration(self):
        tools = SkillRegistry.get_all_tools()
        self.assertIn("query_codebase_graph", tools)

    def test_query_codebase_graph_real_or_mock(self):
        graph_file = Path(__file__).resolve().parent.parent / "graphify-out" / "graph.json"
        if graph_file.exists():
            res = SkillRegistry.execute("query_codebase_graph", query="ManagedServiceManager")
            self.assertIn("ManagedServiceManager", res)
        else:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="Graph result for mock", stderr="")
                res = SkillRegistry.execute("query_codebase_graph", query="test")
                self.assertIn("Graph result", res)

    def test_missing_graph_file_handling(self):
        with patch.object(Path, "exists", return_value=False):
            res = SkillRegistry.execute("query_codebase_graph", query="test_symbol")
            self.assertIn("No codebase knowledge graph found", res)

    def test_explain_and_path_modes(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Path found: A -> B", stderr="")
            res_path = SkillRegistry.execute("query_codebase_graph", query="JarvisBrain SafetyGate", mode="path")
            self.assertIn("Path found", res_path)

            mock_run.return_value = MagicMock(returncode=0, stdout="Explain: SafetyGate guards high risk tools", stderr="")
            res_explain = SkillRegistry.execute("query_codebase_graph", query="SafetyGate", mode="explain")
            self.assertIn("SafetyGate", res_explain)


if __name__ == "__main__":
    unittest.main()
