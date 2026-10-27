"""Adversarial and functional test suite for fail-closed tool authorization.

Verifies:
1. Unclassified/undeclared tools default to risk_level=1 and fail closed on tainted turns.
2. Direct SkillRegistry.execute calls on critical tools require human approval grants.
3. Direct SkillRegistry.execute calls on mutating tools are blocked on tainted turns.
4. Valid human approval grants permit direct execution.
5. Forged or invalid grants fail closed.
6. Verified read-only tools execute safely on tainted turns.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import jarvis.skills.dev_shell  # Registers execute_powershell, write_file, list_directory, etc.
import jarvis.skills.productivity  # Registers get_current_time_and_date, etc. # noqa: F401
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    DualKeyAuthorization,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry


class TestToolAuthorizationHardening(unittest.TestCase):
    """Hermetic adversarial tests for fail-closed tool authorization."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.orchestrator = ToolOrchestrator()

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.set_approval_handler(None)
        self.approval_mgr.reset()

    def test_unclassified_tool_defaults_to_restricted_and_denied_on_tainted_turn(self):
        """Undeclared tools without read_only=True default to risk_level=1 and are blocked on tainted turns."""
        tool_name = "test_custom_unclassified_mutator"

        @SkillRegistry.register(name=tool_name, description="An unclassified custom tool")
        def unclassified_tool(arg: str = "val"):
            return f"mutated_{arg}"

        try:
            # 1. Verify registry metadata defaults risk_level to 1
            meta = SkillRegistry.get_metadata(tool_name)
            self.assertIsNotNone(meta)
            self.assertFalse(meta.read_only)
            self.assertEqual(meta.risk_level, 1)
            self.assertEqual(SkillRegistry.get_risk_level(tool_name), 1)

            # 2. On a clean turn, it can execute directly
            res = SkillRegistry.execute(tool_name, arg="clean")
            self.assertEqual(res, "mutated_clean")

            # 3. Once turn is tainted, unclassified tool MUST fail closed
            self.tracker.mark_tainted("web_search")

            # A. Orchestrator dispatch path
            orch_res = self.orchestrator.execute_step_with_status(tool_name, {"arg": "tainted"})
            self.assertFalse(orch_res.success)
            self.assertEqual(orch_res.failure_category, "orchestrator_denied")
            self.assertIn("Dual-Key Safety Gate", orch_res.result)

            # B. Direct SkillRegistry.execute path
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(tool_name, arg="tainted")
            self.assertIn("Dual-Key Safety Gate: blocked high-privilege tool", str(ctx.exception))

            # C. Direct SkillRegistry.execute_with_status path
            status_res = SkillRegistry.execute_with_status(tool_name, arg="tainted")
            self.assertFalse(status_res.success)
            self.assertEqual(status_res.error_type, "security_violation")
            self.assertIn("Security Violation: Dual-Key Safety Gate", status_res.result)

        finally:
            SkillRegistry.unregister(tool_name)

    def test_direct_execution_of_critical_tool_blocked_without_grant(self):
        """Direct execution of critical privilege tools (risk_level >= 2) fails closed without approval."""
        # Clean turn without taint
        self.tracker.reset_turn()

        # Direct execution of critical tool without grant must raise SecurityBoundaryViolation
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute("execute_powershell", command="whoami")
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

        # Direct execution_with_status must fail with security_violation error_type
        status_res = SkillRegistry.execute_with_status("execute_powershell", command="whoami")
        self.assertFalse(status_res.success)
        self.assertEqual(status_res.error_type, "security_violation")
        self.assertIn("Critical Authorization Gate", status_res.result)

    def test_direct_execution_of_mutating_tool_blocked_on_tainted_turn(self):
        """Direct execution of Level 1 mutating tools is blocked when turn is tainted."""
        self.tracker.mark_tainted("browse_and_read_webpage")

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute("write_file", file_path="test_attack.txt", content="payload")
        self.assertIn("Dual-Key Safety Gate: blocked high-privilege tool 'write_file'", str(ctx.exception))

        status_res = SkillRegistry.execute_with_status("write_file", file_path="test_attack.txt", content="payload")
        self.assertFalse(status_res.success)
        self.assertEqual(status_res.error_type, "security_violation")

    def test_direct_execution_succeeds_when_authorized_with_grant(self):
        """Direct execution of critical tools succeeds when a valid human grant is present."""
        cmd = "echo authorized"
        self.approval_mgr.set_approval_handler(lambda req: True)

        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value={"status": "ok", "stdout_tail": "authorized", "stderr_tail": ""}):
            res = SkillRegistry.execute("execute_powershell", command=cmd)
            self.assertIn("authorized", res)

    def test_read_only_tool_allowed_on_tainted_turn(self):
        """Verified read-only tools (read_only=True or KNOWN_READ_ONLY_TOOLS) execute safely on tainted turns."""
        self.tracker.mark_tainted("read_clipboard")

        # get_current_time_and_date is a verified read-only tool
        res = SkillRegistry.execute("get_current_time_and_date")
        self.assertIsInstance(res, str)
        self.assertNotIn("Security Violation", res)
        self.assertNotIn("Dual-Key Safety Gate", res)

    def test_forged_or_invalid_grant_fails_closed(self):
        """Forged grant token cannot authorize tool execution."""
        fake_grant = DualKeyAuthorization(
            grant_id="fake_001",
            tool_name="execute_powershell",
            action_hash="00" * 32,
            token="invalid_secret_token",
            expires_at=time.time() + 60,
            status="pending",
        )

        with self.assertRaises(PermissionError):
            self.approval_mgr.bind_active_grant(fake_grant)

    def test_nested_tool_call_cannot_bypass_authorization_synchronously(self):
        """A permitted Level-1 tool cannot bypass authorization when invoking a critical tool nested."""
        nested_tool_name = "test_parent_tool_nested_attack"
        intercepted_child_error = []

        @SkillRegistry.register(name=nested_tool_name, description="Parent tool attempting nested attack", risk_level=1)
        def parent_tool():
            try:
                # Attempt to execute critical tool nested inside parent tool execution
                return SkillRegistry.execute("execute_powershell", command="whoami")
            except Exception as e:
                intercepted_child_error.append(e)
                raise

        try:
            # Dispatch parent tool via orchestrator
            orch_res = self.orchestrator.execute_step_with_status(nested_tool_name, {})
            # The nested critical tool call must fail closed and be caught
            self.assertFalse(orch_res.success)
            self.assertEqual(len(intercepted_child_error), 1)
            self.assertIsInstance(intercepted_child_error[0], SecurityBoundaryViolation)
            self.assertIn("Critical Authorization Gate", str(intercepted_child_error[0]))
        finally:
            SkillRegistry.unregister(nested_tool_name)

    def test_nested_tool_call_cannot_bypass_authorization_in_child_task(self):
        """A permitted Level-1 tool cannot pass orchestrator authorization to child asynchronous tasks."""
        import asyncio

        child_task_tool = "test_parent_spawning_child_task"
        child_task_results = []

        @SkillRegistry.register(name=child_task_tool, description="Spawns async child task", risk_level=1)
        def parent_spawner():
            async def _run_async_child():
                try:
                    res = SkillRegistry.execute("execute_powershell", command="whoami")
                    child_task_results.append(("success", res))
                except (SecurityBoundaryViolation, PermissionError, RuntimeError) as e:
                    child_task_results.append(("error", e))

            # Run child task synchronously via asyncio loop
            asyncio.run(_run_async_child())
            return "parent_done"

        try:
            orch_res = self.orchestrator.execute_step_with_status(child_task_tool, {})
            self.assertTrue(orch_res.success)
            # Child task execution of execute_powershell must have failed closed
            self.assertEqual(len(child_task_results), 1)
            status, err = child_task_results[0]
            self.assertEqual(status, "error")
            self.assertIsInstance(err, SecurityBoundaryViolation)
            self.assertIn("Critical Authorization Gate", str(err))
        finally:
            SkillRegistry.unregister(child_task_tool)

    def test_explicit_risk_level_zero_without_read_only_fails_closed(self):
        """A mutating tool declaring risk_level=0 without read_only=True fails closed to risk_level=1."""
        mutator_name = "test_fake_risk_zero_mutator"

        @SkillRegistry.register(name=mutator_name, description="Claims risk 0 but is a mutator", risk_level=0)
        def fake_zero_mutator(data: str = "x"):
            return f"mutated_{data}"

        try:
            meta = SkillRegistry.get_metadata(mutator_name)
            self.assertIsNotNone(meta)
            # Must NOT be certified read-only
            self.assertFalse(meta.read_only)
            # Must fail closed to risk 1
            self.assertEqual(meta.risk_level, 1)
            self.assertEqual(SkillRegistry.get_risk_level(mutator_name), 1)

            # Taint turn; uncertified tool must be blocked
            self.tracker.mark_tainted("web_search")
            with self.assertRaises(SecurityBoundaryViolation):
                SkillRegistry.execute(mutator_name, data="attack")
        finally:
            SkillRegistry.unregister(mutator_name)
