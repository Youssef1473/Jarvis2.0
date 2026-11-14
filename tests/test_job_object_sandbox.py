"""Unit tests for jarvis/skills/job_object_sandbox.py."""
from __future__ import annotations

import contextlib
import contextvars
import sys
import unittest
from typing import Any

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.job_object_sandbox import (
    _resolve_safe_affinity_mask,
    run_job_sandboxed_process,
)


class TestJobObjectSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self._ctx_tokens: list[contextvars.Token] = []

    def tearDown(self) -> None:
        for token in reversed(self._ctx_tokens):
            with contextlib.suppress(Exception):
                self.approval_mgr.clear_active_authorized(token)
        self._ctx_tokens.clear()
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    def _authorize(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments=arguments,
            ttl_seconds=60.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)
        return grant

    def test_run_job_sandboxed_process_success(self) -> None:
        cmd = [sys.executable, "-c", "print('hello_sandbox_kernel')"]

        # 1. Unauthenticated direct invocation MUST fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            run_job_sandboxed_process(command=cmd, timeout_sec=5.0)

        # 2. Legitimate authorization permits successful contained execution
        self._authorize("run_job_sandboxed_process", {"command": cmd})
        res = run_job_sandboxed_process(
            command=cmd,
            timeout_sec=5.0,
        )
        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("exit_code"), 0)
        self.assertIn("hello_sandbox_kernel", res.get("stdout_tail", ""))
        self.assertGreaterEqual(res.get("peak_job_memory_mb", 0), 0)
        self.assertTrue(res.get("job_drained"))
        self.assertIn("0x", str(res.get("affinity_mask", "")))

    def test_run_job_sandboxed_process_timeout(self) -> None:
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]

        # 1. Unauthenticated direct invocation MUST fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            run_job_sandboxed_process(command=cmd, timeout_sec=0.2)

        # 2. Legitimate authorization permits execution and verifies timeout termination
        self._authorize("run_job_sandboxed_process", {"command": cmd})
        res = run_job_sandboxed_process(
            command=cmd,
            timeout_sec=0.2,
        )
        self.assertEqual(res.get("status"), "timeout")
        self.assertIn("Process tree terminated", res.get("stderr_tail", ""))

    def test_dynamic_affinity_mask(self) -> None:
        mask = _resolve_safe_affinity_mask(0xFF00)
        self.assertIsNotNone(mask)
        self.assertIsInstance(mask, int)
        self.assertGreater(mask, 0)

    def test_empty_command_rejection(self) -> None:
        res = run_job_sandboxed_process(command=[])
        self.assertEqual(res.get("status"), "error")

    def test_registry_registration(self) -> None:
        tool = SkillRegistry.get_tool("run_job_sandboxed_process")
        self.assertIsNotNone(tool)
        self.assertEqual(SkillRegistry.get_risk_level("run_job_sandboxed_process"), 1)

    def test_execute_sandboxed_powershell(self) -> None:
        from jarvis.skills.dev_shell import execute_sandboxed_powershell

        cmd = "Write-Output 'sandboxed_powershell_ok'"

        # 1. Unauthenticated invocation MUST fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            execute_sandboxed_powershell(cmd)

        # 2. Legitimate authorization permits sandboxed PowerShell execution
        self._authorize("execute_sandboxed_powershell", {"command": cmd})
        out = execute_sandboxed_powershell(cmd)
        self.assertIn("sandboxed_powershell_ok", out)
        self.assertIn("Job Object", out)
        self.assertIn("E-Core mask", out)


if __name__ == "__main__":
    unittest.main()
