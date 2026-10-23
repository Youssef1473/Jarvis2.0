import threading
"""Unit tests for Closed-Loop Security Boundaries, SSRF Protection, Taint Tracking, and Shell Approval."""
import socket
import time
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from jarvis.core.security_boundaries import (
    SecurityBoundaryViolation,
    TurnTaintTracker,
    DualKeyApprovalManager,
    validate_confined_path,
    validate_egress_url,
)
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.skills.base import SkillRegistry
from jarvis.skills import dev_shell  # Ensure skills registered


class TestSecurityBoundaries(unittest.TestCase):
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

    def test_blocks_illegal_schemes(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("file:///C:/Windows/win.ini")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("javascript:alert(1)")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("data:text/html,<h1>test</h1>")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("ftp://ftp.example.com/file.txt")

    def test_blocks_localhost_and_loopback(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://127.0.0.1:9222")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://localhost:8080")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://sub.localhost")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://[::1]:80")

    def test_blocks_private_and_link_local_ips(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://169.254.169.254/latest/meta-data/")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://192.168.1.1/")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://10.0.0.1/admin")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://172.16.0.1/dashboard")

    def test_blocks_ipv4_mapped_ipv6_loopback(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://[::ffff:127.0.0.1]/test")

    def test_blocks_dns_resolving_to_private_ip(self):
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.50", 80))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                validate_egress_url("http://attacker.example.com/api")
            self.assertIn("non-global address", str(ctx.exception))

    def test_allows_valid_public_domain(self):
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            res = validate_egress_url("https://example.com/news")
            self.assertEqual(res, "https://example.com/news")

    def test_path_confinement_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            with self.assertRaises(SecurityBoundaryViolation):
                validate_confined_path(r"..\..\Windows\System32\cmd.exe", tmp_root)

    def test_path_confinement_blocks_reserved_devices(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            for dev in ("NUL.txt", "CON", "prn.png", "AUX.log", "com1.bin"):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_confined_path(f"{tmp_root}/{dev}", tmp_root)

    def test_path_confinement_allows_safe_subpath(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            safe_target = f"{tmp_root}/sub/screenshot.png"
            res = validate_confined_path(safe_target, tmp_root)
            self.assertIn("screenshot.png", res)

    # -------------------------------------------------------------------------
    # Phase 2 — PowerShell Approval (SEC-02) Regression Tests
    # -------------------------------------------------------------------------

    def test_execute_powershell_registered_at_risk_level_2(self):
        """Proof: execute_powershell is registered at risk_level=2 in SkillRegistry."""
        risk = SkillRegistry.get_risk_level("execute_powershell")
        self.assertEqual(risk, 2, "execute_powershell must be registered at risk_level=2")
        self.assertIn("execute_powershell", TurnTaintTracker.CRITICAL_PRIVILEGE_TOOLS)

    def test_execute_sandboxed_powershell_preserved_at_risk_level_1(self):
        """Proof: execute_sandboxed_powershell is preserved at risk_level=1 and permitted on untainted turns."""
        risk = SkillRegistry.get_risk_level("execute_sandboxed_powershell")
        self.assertEqual(risk, 1, "execute_sandboxed_powershell must remain at risk_level=1")
        self.assertNotIn("execute_sandboxed_powershell", TurnTaintTracker.CRITICAL_PRIVILEGE_TOOLS)

        # Permitted on untainted turn without approval
        self.assertFalse(self.tracker.is_tainted)
        allowed = self.tracker.check_tool_execution(
            "execute_sandboxed_powershell", {"command": "Get-Date"}, risk_level=risk
        )
        self.assertTrue(allowed)

        # Blocked on tainted turn without approval
        self.tracker.mark_tainted("web_search")
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_sandboxed_powershell", {"command": "Get-Date"}, risk_level=risk
            )
        self.assertIn("Dual-Key Safety Gate", str(ctx.exception))

        # Permitted on tainted turn with approval grant
        self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": "Get-Date"})
        allowed_tainted = self.tracker.check_tool_execution(
            "execute_sandboxed_powershell", {"command": "Get-Date"}, risk_level=risk
        )
        self.assertTrue(allowed_tainted)

    def test_execute_powershell_unapproved_rejected_on_untainted_turn(self):
        """Proof: Unapproved execute_powershell execution is rejected on an untainted turn."""
        self.assertFalse(self.tracker.is_tainted)
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_powershell", {"command": "Get-Process"}, risk_level=2
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_execute_powershell_unapproved_rejected_on_tainted_turn(self):
        """Proof: Unapproved execute_powershell execution is rejected on a tainted turn."""
        self.tracker.mark_tainted("browser_get_dom")
        self.assertTrue(self.tracker.is_tainted)
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_powershell", {"command": "Get-Process"}, risk_level=2
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_execute_powershell_valid_human_approval_permits_exact_command(self):
        """Proof: A valid human approval permits the exact approved command."""
        cmd_args = {"command": "Get-Process"}
        self.approval_mgr.grant_approval("execute_powershell", cmd_args)
        # Succeeded without raising
        result = self.tracker.check_tool_execution(
            "execute_powershell", cmd_args, risk_level=2
        )
        self.assertTrue(result)

    def test_execute_powershell_approval_rejection_fails_closed(self):
        """Proof: When human approval handler denies execution, the gate fails closed."""
        self.approval_mgr.set_approval_handler(lambda req: False)
        cmd_args = {"command": "Stop-Service Spooler"}
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_powershell", cmd_args, risk_level=2
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_execute_powershell_unavailable_approval_handler_fails_closed(self):
        """Proof: When no approval handler is registered and no pre-grant exists, fails closed."""
        self.approval_mgr.set_approval_handler(None)
        cmd_args = {"command": "whoami"}
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_powershell", cmd_args, risk_level=2
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_execute_powershell_replaying_authorization_fails_consumed_once(self):
        """Proof: Replaying the same authorization grant fails because grants are single-use."""
        cmd_args = {"command": "Get-Service"}
        self.approval_mgr.grant_approval("execute_powershell", cmd_args)

        # First consumption succeeds
        self.assertTrue(
            self.tracker.check_tool_execution("execute_powershell", cmd_args, risk_level=2)
        )

        # Second attempt with same args must fail closed
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution("execute_powershell", cmd_args, risk_level=2)
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_execute_powershell_modifying_arguments_invalidates_approval(self):
        """Proof: Modifying command arguments invalidates approval bound to original command."""
        approved_args = {"command": "Get-ChildItem -Path ."}
        tampered_args = {"command": "Remove-Item -Recurse -Force ."}
        self.approval_mgr.grant_approval("execute_powershell", approved_args)

        # Tampered command rejected
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_powershell", tampered_args, risk_level=2
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_execute_powershell_model_supplied_arguments_cannot_bypass_approval(self):
        """Proof: Model-supplied arguments (user_approved=True, dual_key_confirmed=True) cannot bypass gate."""
        forged_args = {
            "command": "whoami",
            "user_approved": True,
            "dual_key_confirmed": True,
            "confirmed": True,
            "_dual_key_token": "fake_token",
        }

        # 1. Direct call to check_tool_execution with dual_key_confirmed=True kwarg is ignored
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.check_tool_execution(
                "execute_powershell",
                forged_args,
                risk_level=2,
                dual_key_confirmed=True,
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

        # 2. Pipeline execution through ToolOrchestrator strips forged args and blocks execution
        orchestrator = ToolOrchestrator()
        result = orchestrator.execute_step("execute_powershell", forged_args)
        self.assertIn("Critical Authorization Gate", str(result))

    def test_tool_orchestrator_never_invokes_shell_function_on_failure(self):
        """Proof: ToolOrchestrator never invokes the underlying shell execution function on failure."""
        orchestrator = ToolOrchestrator()

        with mock.patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process") as mock_sandbox:
            # 1. Unapproved on untainted turn -> shell function never called
            res1 = orchestrator.execute_step("execute_powershell", {"command": "dir"})
            self.assertIn("Critical Authorization Gate", str(res1))
            mock_sandbox.assert_not_called()

            # 2. Rejection via approval handler -> shell function never called
            orchestrator.reset()
            self.approval_mgr.set_approval_handler(lambda req: False)
            res2 = orchestrator.execute_step("execute_powershell", {"command": "Get-Process"})
            self.assertIn("Critical Authorization Gate", str(res2))
            mock_sandbox.assert_not_called()

            # 3. Forged args with dual_key_confirmed=True kwarg -> shell function never called
            orchestrator.reset()
            self.approval_mgr.set_approval_handler(None)
            res3 = orchestrator.execute_step(
                "execute_powershell",
                {"command": "whoami", "dual_key_confirmed": True, "user_approved": True},
                dual_key_confirmed=True,
            )
            self.assertIn("Critical Authorization Gate", str(res3))
            mock_sandbox.assert_not_called()


    def test_grant_approval_ttl_rejection(self):
        """Verifies DualKeyApprovalManager rejects non-finite, negative, and invalid TTLs."""
        bad_ttls = [float("nan"), float("inf"), float("-inf"), -1.0, 0, "invalid_str", None]
        for bad_ttl in bad_ttls:
            with self.assertRaises(ValueError, msg=f"Expected ValueError for bad TTL: {bad_ttl}"):
                self.approval_mgr.grant_approval("execute_powershell", {"command": "dir"}, ttl_seconds=bad_ttl)

        # Valid TTL is clamped between 0.05 and 300.0
        auth_min = self.approval_mgr.grant_approval("execute_powershell", {"command": "dir"}, ttl_seconds=0.01)
        self.assertLessEqual(auth_min.expires_at - time.time(), 0.06)

        auth_max = self.approval_mgr.grant_approval("execute_powershell", {"command": "dir"}, ttl_seconds=99999.0)
        self.assertLessEqual(auth_max.expires_at - time.time(), 300.5)

    def test_grant_expiration_and_replay_rejection(self):
        """Verifies grants expire cleanly and cannot be replayed after single-use consumption."""
        self.approval_mgr.reset()
        args = {"command": "Get-Date"}

        # 1. Single-use consumption
        self.approval_mgr.grant_approval("execute_powershell", args, ttl_seconds=10.0)
        self.assertTrue(self.approval_mgr.has_valid_grant("execute_powershell", args))
        self.assertTrue(self.approval_mgr.consume_grant("execute_powershell", args))
        # Replay attempt fails
        self.assertFalse(self.approval_mgr.consume_grant("execute_powershell", args))
        self.assertFalse(self.approval_mgr.has_valid_grant("execute_powershell", args))

        # 2. Grant expiration
        self.approval_mgr.grant_approval("execute_powershell", args, ttl_seconds=0.05)
        time.sleep(0.08)
        self.assertFalse(self.approval_mgr.has_valid_grant("execute_powershell", args))
        self.assertFalse(self.approval_mgr.consume_grant("execute_powershell", args))

    def test_action_binding_prevents_cross_tool_and_cross_arg_reuse(self):
        """Verifies authorization grants are bound to the exact tool and arguments, preventing cross-tool reuse."""
        self.approval_mgr.reset()
        ps_args = {"command": "Get-Process"}

        # Issue grant strictly for execute_powershell
        self.approval_mgr.grant_approval("execute_powershell", ps_args, ttl_seconds=60.0)

        # 1. Attempt to consume for a different tool with same arguments fails
        self.assertFalse(self.approval_mgr.consume_grant("execute_sandboxed_powershell", ps_args))
        self.assertFalse(self.approval_mgr.consume_grant("delete_file", {"command": "Get-Process"}))

        # 2. Attempt to consume for same tool with different arguments fails
        self.assertFalse(self.approval_mgr.consume_grant("execute_powershell", {"command": "Get-Service"}))
        self.assertFalse(self.approval_mgr.consume_grant("execute_powershell", {"command": "Get-Process", "extra": "1"}))

        # 3. Original grant is still valid and consumable only for the exact tool and arguments
        self.assertTrue(self.approval_mgr.has_valid_grant("execute_powershell", ps_args))
        self.assertTrue(self.approval_mgr.consume_grant("execute_powershell", ps_args))


class TestTurnTaintConcurrencyIsolation(unittest.TestCase):
    """H2 Regression Tests: Per-Turn Taint State Isolation across concurrent execution threads and contexts."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()

    def test_concurrent_turn_reset_does_not_clear_active_turn_taint(self):
        """Turn A's taint state must not be reset or wiped by concurrent Turn B calling reset_turn()."""
        turn1_started = threading.Event()
        turn2_reset_done = threading.Event()
        turn1_tainted_after_turn2_reset = None

        def turn1_worker():
            nonlocal turn1_tainted_after_turn2_reset
            self.tracker.mark_tainted("web_search")
            turn1_started.set()
            # Wait until Turn 2 has invoked reset_turn()
            self.assertTrue(turn2_reset_done.wait(timeout=2.0))
            turn1_tainted_after_turn2_reset = self.tracker.is_tainted

        def turn2_worker():
            self.assertTrue(turn1_started.wait(timeout=2.0))
            self.tracker.reset_turn()
            turn2_reset_done.set()

        t1 = threading.Thread(target=turn1_worker)
        t2 = threading.Thread(target=turn2_worker)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertFalse(t1.is_alive(), "Turn 1 thread timed out")
        self.assertFalse(t2.is_alive(), "Turn 2 thread timed out")
        self.assertTrue(
            turn1_tainted_after_turn2_reset,
            "VULNERABILITY: Turn A taint was wiped out by concurrent Turn B calling reset_turn()!"
        )

    def test_turn_does_not_inherit_taint_from_another_turn(self):
        """A new turn running in a separate thread must not inherit taint from an earlier or concurrent tainted turn."""
        turn1_tainted_done = threading.Event()
        turn2_inherited_taint = None

        def turn1_worker():
            self.tracker.mark_tainted("browser_get_dom")
            turn1_tainted_done.set()

        def turn2_worker():
            nonlocal turn2_inherited_taint
            self.assertTrue(turn1_tainted_done.wait(timeout=2.0))
            # Turn 2 has not executed any untrusted tool and has not called reset_turn
            turn2_inherited_taint = self.tracker.is_tainted

        t1 = threading.Thread(target=turn1_worker)
        t2 = threading.Thread(target=turn2_worker)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertFalse(
            turn2_inherited_taint,
            "VULNERABILITY: Turn B inherited Turn A's taint state across thread boundaries!"
        )

    def test_concurrent_tool_execution_gate_isolation(self):
        """Turn A (tainted) must be blocked by Dual-Key gate, while concurrent Turn B (untainted) is permitted."""
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()

        turn1_tainted_ready = threading.Event()
        turn2_finished = threading.Event()
        turn1_result = None
        turn2_result = None

        tool_args = {"command": "Get-Date"}

        def turn1_worker():
            nonlocal turn1_result
            self.tracker.mark_tainted("web_search")
            turn1_tainted_ready.set()
            self.assertTrue(turn2_finished.wait(timeout=2.0))
            try:
                # Level 1 mutation must be blocked on tainted turn without approval
                self.tracker.check_tool_execution("execute_sandboxed_powershell", tool_args, risk_level=1)
                turn1_result = "allowed"
            except SecurityBoundaryViolation:
                turn1_result = "blocked"

        def turn2_worker():
            nonlocal turn2_result
            self.assertTrue(turn1_tainted_ready.wait(timeout=2.0))
            # Turn 2 calls reset_turn() and executes level 1 mutation
            self.tracker.reset_turn()
            try:
                allowed = self.tracker.check_tool_execution("execute_sandboxed_powershell", tool_args, risk_level=1)
                turn2_result = "allowed" if allowed else "disallowed"
            except SecurityBoundaryViolation:
                turn2_result = "blocked"
            finally:
                turn2_finished.set()

        t1 = threading.Thread(target=turn1_worker)
        t2 = threading.Thread(target=turn2_worker)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertEqual(turn2_result, "allowed", "Turn B was untainted and should have been allowed")
        self.assertEqual(turn1_result, "blocked", "Turn A was tainted and should have been blocked by Dual-Key gate")
    def test_child_asyncio_task_cannot_clear_parent_taint(self):
        """SEC-H2-01: A child asyncio task cannot clear its parent's established taint state."""
        import asyncio

        async def run():
            self.tracker.reset_turn()
            self.tracker.mark_tainted("web_search")
            self.assertTrue(self.tracker.is_tainted)

            async def child():
                self.tracker.is_tainted = False

            await asyncio.create_task(child())
            self.assertTrue(
                self.tracker.is_tainted,
                "VULNERABILITY: Child task untainted parent via shared mutable state!"
            )

        asyncio.run(run())

    def test_child_task_cannot_clear_parent_taint_sources(self):
        """SEC-H2-01: A child task cannot mutate or wipe parent's exposed tainting sources."""
        import asyncio

        async def run():
            self.tracker.reset_turn()
            self.tracker.mark_tainted("web_search")

            async def child():
                try:
                    self.tracker._tainting_sources.clear()
                except Exception:
                    pass
                try:
                    self.tracker._tainting_sources = []
                except Exception:
                    pass

            await asyncio.create_task(child())
            self.assertIn(
                "web_search",
                self.tracker._tainting_sources,
                "VULNERABILITY: Child task cleared parent tainting sources!"
            )

        asyncio.run(run())

    def test_unseeded_worker_thread_fails_closed_at_security_gate(self):
        """SEC-H2-02: An unseeded worker thread without turn context cannot execute privileged mutations."""
        err = None

        def worker():
            nonlocal err
            try:
                self.tracker.check_tool_execution(
                    "execute_sandboxed_powershell", {"command": "Get-Process"}, risk_level=1
                )
            except SecurityBoundaryViolation as e:
                err = e

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3.0)

        self.assertIsNotNone(
            err,
            "VULNERABILITY: Unseeded worker thread executed privileged tool without context or authorization!"
        )

    def test_unseeded_context_allows_safe_read_only_tool(self):
        """SEC-H2-02: Safe read-only inspections (risk_level=0) are permitted even in unseeded contexts."""
        err = None
        allowed = None

        def worker():
            nonlocal err, allowed
            try:
                allowed = self.tracker.check_tool_execution(
                    "list_directory", {"folder_path": "."}, risk_level=0
                )
            except Exception as e:
                err = e

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3.0)

        self.assertIsNone(err)
        self.assertTrue(allowed)

    def test_child_task_taint_propagates_to_parent_turn(self):
        """SEC-H2-03: Untrusted ingestion in a child task taints the logical turn for the parent."""
        import asyncio

        async def run():
            self.tracker.reset_turn()
            self.assertFalse(self.tracker.is_tainted)

            async def child():
                self.tracker.mark_tainted("web_search")

            await asyncio.create_task(child())
            self.assertTrue(
                self.tracker.is_tainted,
                "VULNERABILITY: Child task ingestion taint was not propagated to parent turn!"
            )

        asyncio.run(run())

    def test_concurrent_sibling_ingestion_protects_parallel_sibling(self):
        """SEC-H2-03: Concurrent sibling tasks within the same turn share taint protection."""
        import asyncio
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        DualKeyApprovalManager.get_instance().reset()

        async def run():
            self.tracker.reset_turn()
            step1 = asyncio.Event()

            async def sibling_a():
                self.tracker.mark_tainted("browser_get_dom")
                step1.set()

            async def sibling_b():
                await step1.wait()
                self.tracker.check_tool_execution(
                    "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
                )

            await asyncio.gather(sibling_a(), sibling_b())

        with self.assertRaises(SecurityBoundaryViolation):
            asyncio.run(run())

    def test_dynamic_mcp_tool_recognized_as_untrusted_ingestion_in_gate(self):
        """SEC-H2-04: Dynamic MCP tools (mcp_*) are recognized as untrusted ingestion at check_tool_execution."""
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)
        allowed = self.tracker.check_tool_execution("mcp_github_create_issue", {"title": "Test"}, risk_level=1)
        self.assertTrue(allowed)
        self.assertTrue(
            self.tracker.is_tainted,
            "VULNERABILITY: Dynamic MCP tool call was not recognized as untrusted ingestion at the gate!"
        )
        self.assertIn("mcp_github_create_issue", self.tracker._tainting_sources)

    def test_delegated_threadpool_execution_propagates_mcp_taint_to_parent(self):
        """SEC-H2-04: Delegated threadpool execution of MCP ingestion propagates taint to parent turn."""
        import concurrent.futures
        import contextvars
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        ctx = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            f = ex.submit(ctx.run, self.tracker.mark_tainted, "mcp_custom_api")
            f.result()

        self.assertTrue(
            self.tracker.is_tainted,
            "VULNERABILITY: Worker thread taint in copied context did not update logical turn state!"
        )
        self.assertIn("mcp_custom_api", self.tracker._tainting_sources)



if __name__ == "__main__":
    unittest.main()
