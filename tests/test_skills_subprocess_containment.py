"""Unit and integration tests for subprocess containment across modern_web and dev_shell skills.

Verifies that all converted subprocess calls in jarvis/skills/modern_web.py and
jarvis/skills/dev_shell.py:
1. Use Job Object process-tree containment (run_sandboxed_batch).
2. Cleanly reap child and grandchild processes on timeouts.
3. Preserve backward-compatible mock transparency when unit tests patch subprocess.run.
4. Strictly enforce fail-closed security gates in run_project_tests and check_code_diagnostics.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.skills import dev_shell, modern_web
from jarvis.skills.job_object_sandbox import SandboxedProcess

WIN32 = sys.platform == "win32"


def _is_pid_alive(pid: int) -> bool:
    """Helper to check if a process ID is currently alive on the host system."""
    if pid <= 0:
        return False
    if WIN32:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            # STILL_ACTIVE is 259
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


class TestSkillsSubprocessContainment(unittest.TestCase):
    """Test suite for subprocess containment in modern_web.py and dev_shell.py."""

    def setUp(self):
        # Clear module-level caches before each test
        modern_web._SEARCH_CACHE.clear()
        modern_web._GUIDE_CACHE.clear()

    # -------------------------------------------------------------------------
    # 1. modern_web.py Tests
    # -------------------------------------------------------------------------

    def test_modern_web_search_success_contained(self):
        """search_modern_web_guidance parses JSON results from run_sandboxed_batch."""
        mock_results = [
            {
                "id": "css-container-queries",
                "description": "Adaptive styling based on parent size.",
                "category": "layout",
                "featuresUsed": ["@container"],
            }
        ]
        mock_proc = subprocess.CompletedProcess(
            args=["npx", "search"],
            returncode=0,
            stdout=json.dumps(mock_results),
            stderr="",
        )
        with patch("jarvis.skills.modern_web.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = modern_web.search_modern_web_guidance("container queries", limit=5)
            self.assertTrue(mock_batch.called)
            self.assertIn("css-container-queries", out)
            self.assertIn("Adaptive styling based on parent size.", out)

    def test_modern_web_search_timeout_contained(self):
        """search_modern_web_guidance catches TimeoutExpired and returns clean timeout error."""
        with patch(
            "jarvis.skills.modern_web.run_sandboxed_batch",
            side_effect=subprocess.TimeoutExpired(cmd=["npx"], timeout=15),
        ):
            out = modern_web.search_modern_web_guidance("timeout query")
            self.assertIn("timed out for query 'timeout query'", out)

    def test_modern_web_retrieve_success_contained(self):
        """retrieve_modern_web_guide parses markdown content from run_sandboxed_batch."""
        guide_body = "# Dialog Guide\nUse the <dialog> element with showModal()."
        mock_proc = subprocess.CompletedProcess(
            args=["npx", "retrieve"],
            returncode=0,
            stdout=guide_body,
            stderr="",
        )
        with patch("jarvis.skills.modern_web.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = modern_web.retrieve_modern_web_guide("dialog-guide")
            self.assertTrue(mock_batch.called)
            self.assertIn("# Modern Web Guidance: dialog-guide", out)
            self.assertIn("Use the <dialog> element", out)

    def test_modern_web_retrieve_timeout_contained(self):
        """retrieve_modern_web_guide handles TimeoutExpired gracefully."""
        with patch(
            "jarvis.skills.modern_web.run_sandboxed_batch",
            side_effect=subprocess.TimeoutExpired(cmd=["npx"], timeout=20),
        ):
            out = modern_web.retrieve_modern_web_guide("slow-guide")
            self.assertIn("Timeout retrieving guide 'slow-guide'", out)

    def test_modern_web_mock_transparency_preserved(self):
        """Existing tests that patch('subprocess.run') still work via mock transparency."""
        mock_proc = MagicMock(
            returncode=0,
            stdout=json.dumps([{"id": "popover-api", "description": "Native popovers."}]),
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_proc) as mock_subproc:
            out = modern_web.search_modern_web_guidance("popover")
            self.assertTrue(mock_subproc.called)
            self.assertIn("popover-api", out)

    # -------------------------------------------------------------------------
    # 2. dev_shell.py: git_diff and git_status Tests
    # -------------------------------------------------------------------------

    def test_git_diff_success(self):
        """git_diff executes under run_sandboxed_batch and returns diff output."""
        mock_proc = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="diff --git a/file.py b/file.py\n+new line",
            stderr="",
        )
        with patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = dev_shell.git_diff()
            self.assertTrue(mock_batch.called)
            self.assertIn("+new line", out)

    def test_git_diff_timeout(self):
        """git_diff handles TimeoutExpired gracefully."""
        with patch(
            "jarvis.skills.dev_shell.run_sandboxed_batch",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=15),
        ):
            out = dev_shell.git_diff()
            self.assertIn("Git diff error: command timed out after 15 seconds.", out)

    def test_git_status_success(self):
        """git_status executes porcelain status and branch check under run_sandboxed_batch."""
        status_proc = subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout=" M test.py", stderr="")
        branch_proc = subprocess.CompletedProcess(args=["git", "branch"], returncode=0, stdout="main\n", stderr="")

        with patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=[status_proc, branch_proc]) as mock_batch:
            out = dev_shell.git_status()
            self.assertEqual(mock_batch.call_count, 2)
            self.assertIn("Branch: main", out)
            self.assertIn("M test.py", out)

    def test_git_status_timeout(self):
        """git_status catches TimeoutExpired and returns clean error message."""
        with patch(
            "jarvis.skills.dev_shell.run_sandboxed_batch",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=10),
        ):
            out = dev_shell.git_status()
            self.assertIn("Git error: git command timed out.", out)

    # -------------------------------------------------------------------------
    # 3. dev_shell.py: query_codebase_graph & ast_search_codebase Tests
    # -------------------------------------------------------------------------

    def test_query_codebase_graph_contained(self):
        """query_codebase_graph runs graphify inside run_sandboxed_batch."""
        mock_proc = subprocess.CompletedProcess(
            args=["graphify"],
            returncode=0,
            stdout="Node: CognitiveRouter -> ToolOrchestrator",
            stderr="",
        )
        with patch.object(Path, "exists", return_value=True), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = dev_shell.query_codebase_graph("CognitiveRouter")
            self.assertTrue(mock_batch.called)
            self.assertIn("CognitiveRouter -> ToolOrchestrator", out)

    def test_query_codebase_graph_timeout(self):
        """query_codebase_graph handles timeout properly."""
        with patch.object(Path, "exists", return_value=True), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=subprocess.TimeoutExpired(cmd=["graphify"], timeout=10)):
            out = dev_shell.query_codebase_graph("test_query")
            self.assertIn("Graph query timed out after 10 seconds, Sir.", out)

    def test_ast_search_codebase_contained(self):
        """ast_search_codebase executes ast-grep via run_sandboxed_batch."""
        mock_proc = subprocess.CompletedProcess(
            args=["ast-grep"],
            returncode=0,
            stdout="jarvis/core/safety.py:10: def check_tool_call(): pass",
            stderr="",
        )
        with patch.object(dev_shell, "_find_ast_grep", return_value=Path("mock/ast-grep.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = dev_shell.ast_search_codebase("def check_tool_call($$$ARGS): $$$BODY", path="jarvis/core/safety.py")
            self.assertTrue(mock_batch.called)
            self.assertIn("check_tool_call", out)

    def test_ast_search_codebase_timeout(self):
        """ast_search_codebase catches TimeoutExpired properly."""
        with patch.object(dev_shell, "_find_ast_grep", return_value=Path("mock/ast-grep.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=subprocess.TimeoutExpired(cmd=["ast-grep"], timeout=15)):
            out = dev_shell.ast_search_codebase("pattern")
            self.assertIn("ast-grep search timed out after 15 seconds, Sir.", out)

    # -------------------------------------------------------------------------
    # 4. dev_shell.py: check_code_diagnostics Tests
    # -------------------------------------------------------------------------

    def test_check_code_diagnostics_rejects_mutation(self):
        """check_code_diagnostics rejects fix=True upfront without invoking subprocess."""
        with patch("jarvis.skills.dev_shell.run_sandboxed_batch") as mock_batch:
            out = dev_shell.check_code_diagnostics(fix=True)
            self.assertFalse(mock_batch.called)
            self.assertIn("Execution rejected: Automated code mutation via 'fix=True' is prohibited", out)

    def test_check_code_diagnostics_contained_success(self):
        """check_code_diagnostics runs ruff check via run_sandboxed_batch."""
        mock_proc = subprocess.CompletedProcess(
            args=["ruff", "check"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch.object(dev_shell, "_find_ruff", return_value=Path("mock/ruff.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = dev_shell.check_code_diagnostics(target_path="jarvis/core/safety.py")
            self.assertTrue(mock_batch.called)
            self.assertIn("All diagnostics clean", out)

    def test_check_code_diagnostics_timeout(self):
        """check_code_diagnostics handles timeout cleanly."""
        with patch.object(dev_shell, "_find_ruff", return_value=Path("mock/ruff.exe")), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=subprocess.TimeoutExpired(cmd=["ruff"], timeout=15)):
            out = dev_shell.check_code_diagnostics(target_path="jarvis/core/safety.py")
            self.assertIn("Ruff diagnostics check timed out after 15 seconds, Sir.", out)

    # -------------------------------------------------------------------------
    # 5. dev_shell.py: run_project_tests Security Gate Tests
    # -------------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_run_project_tests_fails_closed_by_default(self):
        """run_project_tests refuses uncontained execution on Windows if sandbox errors."""
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value={"status": "error", "reason": "Job Object access denied"}), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch") as mock_batch, \
             patch("subprocess.run") as mock_subproc:
            out = dev_shell.run_project_tests("test_dummy.py")
            self.assertFalse(mock_batch.called)
            self.assertFalse(mock_subproc.called)
            self.assertIn("Error: Job Object containment failed on Windows", out)
            self.assertIn("Fallback to uncontained execution is disabled", out)

    @patch("sys.platform", "win32")
    def test_run_project_tests_contained_fallback_when_opted_in(self):
        """run_project_tests uses run_sandboxed_batch when _allow_uncontained=True."""
        mock_proc = subprocess.CompletedProcess(args=["pytest"], returncode=0, stdout="5 passed", stderr="")
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value={"status": "error", "reason": "No privileges"}), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = dev_shell.run_project_tests("test_dummy.py", _allow_uncontained=True)
            self.assertTrue(mock_batch.called)
            self.assertIn("[WARNING: UNCONTAINED EXECUTION", out)
            self.assertIn("Test Run [PASSED]:\n5 passed", out)

    @patch("sys.platform", "win32")
    def test_run_project_tests_contained_fallback_failure_fails_closed_on_windows(self):
        """When fallback is authorized but run_sandboxed_batch fails on Windows, fails closed without bare subprocess.run."""
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value={"status": "error", "reason": "Primary sandbox failed"}), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=RuntimeError("Job Object handle denied")), \
             patch("subprocess.run") as mock_subproc:
            out = dev_shell.run_project_tests("test_dummy.py", _allow_uncontained=True)
            self.assertFalse(mock_subproc.called)
            self.assertIn("Error: Contained fallback execution failed on Windows", out)
            self.assertIn("Uncontained execution is strictly prohibited", out)

    @patch("sys.platform", "linux")
    def test_run_project_tests_contained_fallback_on_non_windows(self):
        """On non-Windows hosts, if run_sandboxed_batch fails, fails closed without bare subprocess.run."""
        mock_proc = subprocess.CompletedProcess(args=["pytest"], returncode=0, stdout="linux 5 passed", stderr="")
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value={"status": "error", "reason": "Non-windows host"}), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=RuntimeError("Non-windows sandbox error")), \
             patch("subprocess.run", return_value=mock_proc) as mock_subproc:
            out = dev_shell.run_project_tests("test_dummy.py")
            self.assertFalse(mock_subproc.called)
            self.assertIn("Error: Contained fallback execution failed on POSIX", out)
            self.assertIn("Uncontained execution is strictly prohibited", out)

    # -------------------------------------------------------------------------
    # 6. Harmless Real Windows Parent/Grandchild Process Tree Termination
    # -------------------------------------------------------------------------

    @unittest.skipUnless(WIN32, "Live Windows Job Object tests require Windows host")
    def test_live_windows_parent_grandchild_containment(self):
        """Live integration test: Verifies that run_sandboxed_batch terminates both parent and grandchild on timeout."""
        # This script creates a grandchild process sleeping 60 seconds, outputs its PID, then sleeps 60 seconds
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(f'GRANDCHILD_PID={p.pid}', flush=True)\n"
            "time.sleep(60)\n"
        )

        with SandboxedProcess([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True) as proc:
            grandchild_pid = None
            start_t = time.perf_counter()
            while time.perf_counter() - start_t < 5.0:
                line = proc.stdout.readline() if proc.stdout else ""
                if "GRANDCHILD_PID=" in line:
                    grandchild_pid = int(line.strip().split("=")[1])
                    break

            self.assertIsNotNone(grandchild_pid, "Failed to capture grandchild PID from test process")
            parent_pid = proc.pid

            # Both parent and grandchild should be alive right now
            self.assertTrue(_is_pid_alive(parent_pid), f"Parent {parent_pid} should be alive")
            self.assertTrue(_is_pid_alive(grandchild_pid), f"Grandchild {grandchild_pid} should be alive")

            # Terminate the Job Object tree
            proc.terminate_tree()

        # Both parent and grandchild must be terminated cleanly
        time.sleep(0.5)
        self.assertFalse(_is_pid_alive(parent_pid), f"Parent PID {parent_pid} must be terminated")
        self.assertFalse(_is_pid_alive(grandchild_pid), f"Grandchild PID {grandchild_pid} must be terminated by Job Object")


if __name__ == "__main__":
    unittest.main()
