"""Unit tests for the ast-grep structural AST search and Ruff diagnostics skills."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jarvis.skills.base import SkillRegistry
import jarvis.skills.dev_shell


class TestAstGrepAndRuffSkills(unittest.TestCase):
    """Verifies that ast_search_codebase and check_code_diagnostics operate reliably."""

    def test_tool_registrations(self):
        tools = SkillRegistry.get_all_tools()
        self.assertIn("ast_search_codebase", tools)
        self.assertIn("check_code_diagnostics", tools)

    def test_ast_search_live_execution(self):
        """Runs a real ast-grep pattern search on safety.py."""
        res = SkillRegistry.execute(
            "ast_search_codebase",
            pattern="def check_tool_call($$$ARGS): $$$BODY",
            path="jarvis/core/safety.py",
        )
        self.assertIn("jarvis", res.lower())
        self.assertIn("check_tool_call", res)

    def test_ast_search_no_matches(self):
        res = SkillRegistry.execute(
            "ast_search_codebase",
            pattern="def non_existent_function_xyz_123($$$ARGS): $$$BODY",
            path="jarvis/core/safety.py",
        )
        self.assertIn("No AST nodes matched pattern", res)

    def test_ast_search_invalid_path(self):
        res = SkillRegistry.execute(
            "ast_search_codebase",
            pattern="def $FUNC(): $$$BODY",
            path="../../outside_workspace",
        )
        self.assertIn("Invalid path", res)

    def test_ast_search_missing_binary(self):
        with patch.object(Path, "exists", return_value=False), \
             patch("shutil.which", return_value=None):
            res = SkillRegistry.execute(
                "ast_search_codebase",
                pattern="def $FUNC(): $$$BODY",
                path="jarvis/core/safety.py",
            )
            self.assertIn("ast-grep binary is not installed", res)

    def test_ast_search_truncation(self):
        """Verifies output truncation when exceeding max_lines."""
        long_output = "\n".join([f"line_{i}: code" for i in range(100)])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=long_output, stderr="")
            res = SkillRegistry.execute(
                "ast_search_codebase",
                pattern="def $FUNC(): $$$BODY",
                path=".",
                max_lines=10,
            )
            self.assertIn("Output truncated: showing first 10 of 100 matched lines", res)

    def test_ruff_diagnostics_live_execution(self):
        """Runs a real ruff check with select E9,F821 on safety.py."""
        res = SkillRegistry.execute(
            "check_code_diagnostics",
            target_path="jarvis/core/safety.py",
            select="E9,F821",
        )
        self.assertIn("All diagnostics clean", res)

    def test_ruff_diagnostics_with_findings(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="jarvis\\core\\test.py:1:1: F401 `os` imported but unused",
                stderr="",
            )
            res = SkillRegistry.execute(
                "check_code_diagnostics",
                target_path="jarvis/core/test.py",
            )
            self.assertIn("F401", res)

    def test_ruff_diagnostics_invalid_path(self):
        res = SkillRegistry.execute(
            "check_code_diagnostics",
            target_path="../../outside_workspace",
        )
        self.assertIn("Invalid path", res)

    def test_ruff_diagnostics_missing_binary(self):
        with patch.object(Path, "exists", return_value=False), \
             patch("shutil.which", return_value=None):
            res = SkillRegistry.execute(
                "check_code_diagnostics",
                target_path="jarvis/core/safety.py",
            )
            self.assertIn("Ruff binary is not installed", res)

    def test_ruff_diagnostics_truncation(self):
        long_output = "\n".join([f"error_{i}: violation" for i in range(80)])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=long_output, stderr="")
            res = SkillRegistry.execute(
                "check_code_diagnostics",
                target_path=".",
                max_lines=15,
            )
            self.assertIn("Output truncated: showing first 15 of 80 diagnostic messages", res)


if __name__ == "__main__":
    unittest.main()
