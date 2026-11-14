"""Deterministic regression test suite for subprocess output provenance boundaries.

Validates:
1. execute_powershell cannot return arbitrary output as trusted.
2. execute_sandboxed_powershell taints the turn before its result reaches the LLM.
3. run_job_sandboxed_process preserves untrusted output provenance.
4. run_project_tests output cannot bypass turn tainting.
5. A subsequent Level 1 mutation encounters the expected approval gate.
6. Subprocess failures and timeouts do not bypass the result-provenance boundary.
7. Security exceptions are never swallowed by fallback handlers.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.dev_shell import (
    execute_powershell,
    execute_sandboxed_powershell,
    run_project_tests,
)
from jarvis.skills.job_object_sandbox import run_job_sandboxed_process


class TestSubprocessOutputProvenance(unittest.TestCase):
    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    def test_1_execute_powershell_cannot_return_output_as_trusted(self):
        """1. execute_powershell cannot return arbitrary output as trusted."""
        self.assertFalse(self.tracker.is_tainted)

        cmd = "Write-Output 'arbitrary_powershell_stdout_99'"
        # Verify unauthorized execution fails closed
        with self.assertRaises(SecurityBoundaryViolation):
            execute_powershell(cmd)

        # Execute with action-bound grant
        grant = self.approval_mgr.grant_approval("execute_powershell", {"command": cmd})
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        try:
            res = execute_powershell(cmd)
            self.assertIn("arbitrary_powershell_stdout_99", res)

            # Active turn MUST be marked tainted with subprocess provenance
            self.assertTrue(self.tracker.is_tainted)
            self.assertTrue(any("subprocess_output:execute_powershell" in s for s in self.tracker.tainting_sources))
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token)

    def test_2_execute_sandboxed_powershell_taints_turn_before_reaching_llm(self):
        """2. execute_sandboxed_powershell taints the turn before its result reaches the LLM."""
        self.assertFalse(self.tracker.is_tainted)

        cmd = "Write-Output 'sandboxed_output_val'"
        # Verify unauthorized execution fails closed
        with self.assertRaises(SecurityBoundaryViolation):
            execute_sandboxed_powershell(cmd)

        grant = self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": cmd})
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        try:
            res = execute_sandboxed_powershell(cmd)
            self.assertIn("sandboxed_output_val", res)

            self.assertTrue(self.tracker.is_tainted)
            self.assertTrue(any("subprocess_output:execute_sandboxed_powershell" in s for s in self.tracker.tainting_sources))
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token)

    def test_3_run_job_sandboxed_process_preserves_untrusted_output_provenance(self):
        """3. run_job_sandboxed_process preserves untrusted output provenance."""
        self.assertFalse(self.tracker.is_tainted)

        cmd = ["cmd.exe", "/c", "echo untrusted_job_stdout"]
        # Verify unauthorized execution fails closed
        with self.assertRaises(SecurityBoundaryViolation):
            run_job_sandboxed_process(cmd)

        grant = self.approval_mgr.grant_approval("run_job_sandboxed_process", {"command": cmd})
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        try:
            res = run_job_sandboxed_process(cmd)
            self.assertEqual(res.get("status"), "ok")
            self.assertIn("untrusted_job_stdout", res.get("stdout_tail", ""))

            self.assertTrue(self.tracker.is_tainted)
            self.assertTrue(any("subprocess_output:run_job_sandboxed_process" in s for s in self.tracker.tainting_sources))
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token)

    def test_4_run_project_tests_output_cannot_bypass_turn_tainting(self):
        """4. run_project_tests output cannot bypass turn tainting."""
        self.assertFalse(self.tracker.is_tainted)

        mock_res = {
            "status": "ok",
            "exit_code": 0,
            "stdout_tail": "tests/test_sample.py . [100%]\n1 passed in 0.05s",
            "stderr_tail": "",
        }
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_res):
            out = run_project_tests(test_file="test_sample.py")
            self.assertIn("PASSED", out)

        self.assertTrue(self.tracker.is_tainted)
        self.assertTrue(any("subprocess_output:run_project_tests" in s for s in self.tracker.tainting_sources))

    def test_5_subsequent_level_1_mutation_encounters_expected_approval_gate(self):
        """5. A subsequent Level 1 mutation encounters the expected approval gate."""
        self.assertFalse(self.tracker.is_tainted)

        # Step 1: Subprocess tool executes and returns output
        cmd = "Write-Output 'reconnaissance_output'"
        grant = self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": cmd})
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        try:
            execute_sandboxed_powershell(cmd)
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token)
        self.assertTrue(self.tracker.is_tainted)

        # Step 2: Attempting Level 1 mutation on tainted turn without grant raises SecurityBoundaryViolation
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                tool_name="close_application",
                arguments={"app_name": "notepad"},
                risk_level=1,
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

        # Step 3: Providing explicit human authorization grant permits execution
        self.approval_mgr.grant_approval("close_application", {"app_name": "notepad"})
        decision = self.tracker.evaluate_tool_execution(
            tool_name="close_application",
            arguments={"app_name": "notepad"},
            risk_level=1,
        )
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.grant)

    def test_6_subprocess_failures_and_timeouts_do_not_bypass_provenance_boundary(self):
        """6. Subprocess failures and timeouts do not bypass the result-provenance boundary."""
        # A. Non-zero exit code / failure
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        cmd_fail = "Write-Error 'fatal_diagnostic_error'"
        grant_fail = self.approval_mgr.grant_approval("execute_powershell", {"command": cmd_fail})
        ctx_token_fail = self.approval_mgr.bind_active_grant(grant_fail)
        try:
            res_fail = execute_powershell(cmd_fail)
            self.assertIn("fatal_diagnostic_error", res_fail)
            self.assertTrue(self.tracker.is_tainted)
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token_fail)

        # B. Timeout status in execute_powershell
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        cmd_sleep = "Start-Sleep -Seconds 100"
        grant_sleep = self.approval_mgr.grant_approval("execute_powershell", {"command": cmd_sleep})
        ctx_token_sleep = self.approval_mgr.bind_active_grant(grant_sleep)
        mock_timeout = {
            "status": "timeout",
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "Timeout exceeded",
        }
        try:
            with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_timeout):
                timeout_res = execute_powershell(cmd_sleep, timeout_seconds=1)
                self.assertIn("timed out", timeout_res.lower())
                self.assertTrue(self.tracker.is_tainted)
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token_sleep)

        # C. Test runner timeout
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_timeout):
            test_timeout = run_project_tests(test_file="test_long.py", timeout_seconds=1)
            self.assertIn("timed out", test_timeout.lower())
            self.assertTrue(self.tracker.is_tainted)

    def test_7_security_exceptions_never_swallowed_by_fallback_handlers(self):
        """7. Security exceptions are never swallowed by fallback handlers."""
        # When taint tracking raises SecurityBoundaryViolation, run_project_tests must propagate it
        mock_res = {"status": "ok", "exit_code": 0, "stdout_tail": "output", "stderr_tail": ""}
        with (
            patch.object(self.tracker, "mark_tainted", side_effect=SecurityBoundaryViolation("Taint boundary fault")),
            patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_res),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            run_project_tests(test_file="test_sample.py")

    def test_8_subprocess_cancellation_after_launch_taints_turn(self):
        """8. Subprocess cancelled after execution begins taints turn."""
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        cmd = "Get-LongRunningJob"
        grant = self.approval_mgr.grant_approval("execute_powershell", {"command": cmd})
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        mock_cancel = {
            "status": "cancelled",
            "exit_code": -1,
            "stdout_tail": "partial_computation_before_cancel",
            "stderr_tail": "Process tree terminated: cancelled by voice barge-in.",
        }
        try:
            with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_cancel):
                res = execute_powershell(cmd, timeout_seconds=5)
                self.assertIn("cancelled", res.lower())
                self.assertTrue(self.tracker.is_tainted)
                self.assertTrue(any("subprocess_output:execute_powershell" in s for s in self.tracker.tainting_sources))
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token)

    def test_9_subprocess_partial_output_on_error_taints_turn(self):
        """9. Partial stdout/stderr on unexpected status or failure taints turn."""
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        cmd = "heavy_memory_script.ps1"
        grant = self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": cmd})
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        mock_partial = {
            "status": "partial_failure",
            "exit_code": 137,
            "stdout_tail": "leaked_partial_data",
            "stderr_tail": "Killed by OOM watchdog",
        }
        try:
            with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_partial):
                out = execute_sandboxed_powershell(cmd)
                self.assertIn("Killed by OOM", out)
                self.assertTrue(self.tracker.is_tainted)
                self.assertTrue(any("subprocess_output:execute_sandboxed_powershell" in s for s in self.tracker.tainting_sources))
        finally:
            self.approval_mgr.clear_active_authorized(ctx_token)

    def test_10_subprocess_cancelled_tests_taint_turn(self):
        """10. Cancelled project tests taint turn before returning error."""
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        mock_cancel = {
            "status": "cancelled",
            "exit_code": -1,
            "stdout_tail": "test_partial_run",
            "stderr_tail": "",
        }
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_cancel):
            res = run_project_tests(test_file="test_massive.py")
            self.assertIn("cancelled", res.lower())
            self.assertTrue(self.tracker.is_tainted)
            self.assertTrue(any("subprocess_output:run_project_tests" in s for s in self.tracker.tainting_sources))


if __name__ == "__main__":
    unittest.main()
