"""Unit tests for the ast-grep structural AST search and Ruff diagnostics skills."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import jarvis.skills.dev_shell
from jarvis.core.security_boundaries import DualKeyApprovalManager
from jarvis.skills.base import SkillRegistry


class TestAstGrepAndRuffSkills(unittest.TestCase):
    """Verifies that ast_search_codebase and check_code_diagnostics operate reliably."""

    def test_tool_registrations(self):
        tools = SkillRegistry.get_all_tools()
        self.assertIn("ast_search_codebase", tools)
        self.assertIn("check_code_diagnostics", tools)

    def test_ast_search_live_execution(self):
        """Runs a real ast-grep pattern search on safety.py with single-use action-bound permit."""
        ast_grep_bin = jarvis.skills.dev_shell._find_ast_grep()
        if not ast_grep_bin:
            self.skipTest("ast-grep binary is not installed in the environment")
        pattern = "def check_tool_call($$$ARGS): $$$BODY"
        target_path = "jarvis/core/safety.py"
        resolved = jarvis.skills.dev_shell._resolve_workspace_path(target_path)
        rel_target = str(resolved.relative_to(jarvis.skills.dev_shell.WORKSPACE_DIR))
        cmd = [str(ast_grep_bin), "run", "--color=never", "--heading=never", "-p", pattern, rel_target]

        # 1. Negative test: Unauthorized execution fails closed
        unauth_res = SkillRegistry.execute("ast_search_codebase", pattern=pattern, path=target_path)
        self.assertIn("Authorization Required", unauth_res)

        # 2. Positive test: Authorized single-use permit succeeds
        mgr = DualKeyApprovalManager.get_instance()
        grant = mgr.grant_approval("run_sandboxed_batch", {"command": cmd})
        ctx_token = mgr.bind_active_grant(grant)
        try:
            res = SkillRegistry.execute(
                "ast_search_codebase",
                pattern=pattern,
                path=target_path,
            )
            self.assertIn("jarvis", res.lower())
            self.assertIn("check_tool_call", res)
        finally:
            mgr.clear_active_authorized(ctx_token)

        # 3. Post-revocation test: unauthorized again
        revoked_res = SkillRegistry.execute("ast_search_codebase", pattern=pattern, path=target_path)
        self.assertIn("Authorization Required", revoked_res)

    def test_ast_search_no_matches(self):
        """Runs real ast-grep pattern search with zero matches with single-use action-bound permit."""
        ast_grep_bin = jarvis.skills.dev_shell._find_ast_grep()
        if not ast_grep_bin:
            self.skipTest("ast-grep binary is not installed in the environment")
        pattern = "def non_existent_function_xyz_123($$$ARGS): $$$BODY"
        target_path = "jarvis/core/safety.py"
        resolved = jarvis.skills.dev_shell._resolve_workspace_path(target_path)
        rel_target = str(resolved.relative_to(jarvis.skills.dev_shell.WORKSPACE_DIR))
        cmd = [str(ast_grep_bin), "run", "--color=never", "--heading=never", "-p", pattern, rel_target]

        mgr = DualKeyApprovalManager.get_instance()
        grant = mgr.grant_approval("run_sandboxed_batch", {"command": cmd})
        ctx_token = mgr.bind_active_grant(grant)
        try:
            res = SkillRegistry.execute(
                "ast_search_codebase",
                pattern=pattern,
                path=target_path,
            )
            self.assertIn("No AST nodes matched pattern", res)
        finally:
            mgr.clear_active_authorized(ctx_token)

    def test_ast_search_invalid_path(self):
        res = SkillRegistry.execute(
            "ast_search_codebase",
            pattern="def $FUNC(): $$$BODY",
            path="../../outside_workspace",
        )
        self.assertIn("Invalid path", res)

    def test_ast_search_missing_binary(self):
        with patch.object(jarvis.skills.dev_shell, "_find_ast_grep", return_value=None):
            res = SkillRegistry.execute(
                "ast_search_codebase",
                pattern="def $FUNC(): $$$BODY",
                path="jarvis/core/safety.py",
            )
            self.assertIn("ast-grep binary is not installed", res)

    def test_ast_search_truncation(self):
        """Verifies output truncation when exceeding max_lines."""
        long_output = "\n".join([f"line_{i}: code" for i in range(100)])
        mock_proc = subprocess.CompletedProcess(
            args=["ast-grep"],
            returncode=0,
            stdout=long_output,
            stderr="",
        )
        with patch.object(jarvis.skills.dev_shell, "_find_ast_grep", return_value=Path("mock/ast-grep.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc):
            res = SkillRegistry.execute(
                "ast_search_codebase",
                pattern="def $FUNC(): $$$BODY",
                path=".",
                max_lines=10,
            )
            self.assertIn("Output truncated: showing first 10 of 100 matched lines", res)

    def test_ruff_diagnostics_live_execution(self):
        """Runs a real ruff check with select E9,F821 on safety.py with single-use action-bound permit."""
        ruff_bin = jarvis.skills.dev_shell._find_ruff()
        if not ruff_bin:
            self.skipTest("Ruff binary is not installed in the environment")
        target_path = "jarvis/core/safety.py"
        resolved = jarvis.skills.dev_shell._resolve_workspace_path(target_path)
        rel_target = str(resolved.relative_to(jarvis.skills.dev_shell.WORKSPACE_DIR))
        cmd = [str(ruff_bin), "check", "--color", "never", "--output-format", "concise", "--select", "E9,F821", rel_target]

        # 1. Negative test: Unauthorized execution fails closed
        unauth_res = SkillRegistry.execute("check_code_diagnostics", target_path=target_path, select="E9,F821")
        self.assertIn("Authorization Required", unauth_res)

        # 2. Positive test: Authorized single-use permit succeeds
        mgr = DualKeyApprovalManager.get_instance()
        grant = mgr.grant_approval("run_sandboxed_batch", {"command": cmd})
        ctx_token = mgr.bind_active_grant(grant)
        try:
            res = SkillRegistry.execute(
                "check_code_diagnostics",
                target_path=target_path,
                select="E9,F821",
            )
            self.assertIn("All diagnostics clean", res)
        finally:
            mgr.clear_active_authorized(ctx_token)

        # 3. Post-revocation test: unauthorized again
        revoked_res = SkillRegistry.execute("check_code_diagnostics", target_path=target_path, select="E9,F821")
        self.assertIn("Authorization Required", revoked_res)

    def test_ruff_diagnostics_with_findings(self):
        mock_proc = subprocess.CompletedProcess(
            args=["ruff"],
            returncode=1,
            stdout="jarvis\\core\\test.py:1:1: F401 `os` imported but unused",
            stderr="",
        )
        with patch.object(jarvis.skills.dev_shell, "_find_ruff", return_value=Path("mock/ruff.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc):
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
        with patch.object(jarvis.skills.dev_shell, "_find_ruff", return_value=None):
            res = SkillRegistry.execute(
                "check_code_diagnostics",
                target_path="jarvis/core/safety.py",
            )
            self.assertIn("Ruff binary is not installed", res)

    def test_ruff_diagnostics_truncation(self):
        long_output = "\n".join([f"error_{i}: violation" for i in range(80)])
        mock_proc = subprocess.CompletedProcess(
            args=["ruff"],
            returncode=1,
            stdout=long_output,
            stderr="",
        )
        with patch.object(jarvis.skills.dev_shell, "_find_ruff", return_value=Path("mock/ruff.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc):
            res = SkillRegistry.execute(
                "check_code_diagnostics",
                target_path=".",
                max_lines=15,
            )
            self.assertIn("Output truncated: showing first 15 of 80 diagnostic messages", res)


if __name__ == "__main__":
    unittest.main()
