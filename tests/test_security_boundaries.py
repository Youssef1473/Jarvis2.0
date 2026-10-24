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

    def test_validate_egress_url_blocks_disallowed_ports(self):
        """Verifies validate_egress_url blocks specified sensitive ports on localhost."""
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            validate_egress_url("http://localhost:20129", allow_localhost=True, disallowed_ports={20129, 9222})
        self.assertIn("prohibited", str(ctx.exception))

        with self.assertRaises(SecurityBoundaryViolation) as ctx2:
            validate_egress_url("http://127.0.0.1:9222", allow_localhost=True, disallowed_ports={20129, 9222})
        self.assertIn("prohibited", str(ctx2.exception))

        # Allowed port on localhost
        res = validate_egress_url("http://localhost:3000", allow_localhost=True, disallowed_ports={20129, 9222})
        self.assertEqual(res, "http://localhost:3000")

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



class TestAsyncApprovalPropagation(unittest.IsolatedAsyncioTestCase):
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
        if hasattr(self.approval_mgr, "clear_active_authorized"):
            self.approval_mgr.clear_active_authorized()
        else:
            self.approval_mgr.clear_thread_authorized()

    def test_ordinary_gate_success_cannot_manufacture_human_authorization(self):
        """Proof of SEC-M4-01-REV: An ordinary tool or untainted risk_level=1 tool passing the gate
        must NOT manufacture an active authorization token that satisfies verify_and_consume_active_context.
        """
        tool_name = "_test_manufactured_auth_tool"
        test_args = {"data": "unapproved_op"}
        gate_authorized = None

        @SkillRegistry.register(name=tool_name, description="Test tool", risk_level=1)
        def _tool_fn(data: str, user_approved: bool = False):
            nonlocal gate_authorized
            gate_authorized = self.approval_mgr.verify_and_consume_active_context(
                tool_name, {"data": data}
            )
            return "Executed"

        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)
        res = orchestrator.execute_step(tool_name, test_args)
        self.assertEqual(res, "Executed")
        self.assertFalse(
            gate_authorized,
            "VULNERABILITY SEC-M4-01-REV: Ordinary gate success manufactured human authorization!"
        )

    def test_set_active_authorized_rejects_without_genuine_grant(self):
        """Proof of SEC-M4-02-REV: Direct set_active_authorized cannot manufacture authority without a genuine grant."""
        tool_name = "_test_unapproved_direct_token"
        test_args = {"action": "forbidden"}

        with self.assertRaises(PermissionError):
            self.approval_mgr.set_active_authorized(tool_name, test_args)

    async def test_legitimate_async_tool_orchestrator_execution_succeeds(self):
        """Proof: Legitimate async execution through execute_step_async propagates authorization to worker thread."""
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        tool_name = "_test_async_mutation_authorized"
        test_args = {"path": "C:/safe/file.txt", "content": "hello"}

        @SkillRegistry.register(name=tool_name, description="Test async tool", risk_level=2)
        def _test_mutation_fn(path: str, content: str, user_approved: bool = False):
            authorized = self.approval_mgr.verify_and_consume_active_context(
                tool_name, {"path": path, "content": content}
            )
            if not authorized:
                raise PermissionError("Physical execution gate rejected: unauthorized execution context")
            return f"Mutated {path} successfully"

        self.approval_mgr.grant_approval(tool_name, test_args)

        coordinator = DualTrackCoordinator(filler_threshold_ms=500.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        res = await orchestrator.execute_step_async(tool_name, test_args)
        self.assertEqual(res, "Mutated C:/safe/file.txt successfully")

    async def test_unapproved_async_mutation_fails_closed(self):
        """Proof: An unapproved critical tool fails closed in execute_step_async and never runs."""
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        tool_name = "_test_async_mutation_unapproved"
        test_args = {"key": "val"}
        executed = False

        @SkillRegistry.register(name=tool_name, description="Test unapproved async tool", risk_level=2)
        def _unapproved_fn(key: str, user_approved: bool = False):
            nonlocal executed
            executed = True
            return "Should never run"

        coordinator = DualTrackCoordinator(filler_threshold_ms=500.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        res = await orchestrator.execute_step_async(tool_name, test_args)
        self.assertIn("Critical Authorization Gate", str(res))
        self.assertFalse(executed)

    async def test_action_or_argument_mismatch_fails_closed(self):
        """Proof: Active authorization bound to specific arguments cannot be consumed by mismatched arguments."""
        tool_name = "_test_mismatch_tool"
        auth_args = {"action": "read", "id": 1}
        mismatched_args = {"action": "write", "id": 2}

        self.approval_mgr.grant_approval(tool_name, auth_args)
        if hasattr(self.approval_mgr, "set_active_authorized"):
            self.approval_mgr.set_active_authorized(tool_name, auth_args)
        else:
            self.approval_mgr.set_thread_authorized(tool_name, auth_args)

        consumed = self.approval_mgr.verify_and_consume_active_context(tool_name, mismatched_args)
        self.assertFalse(consumed, "VULNERABILITY: verify_and_consume_active_context accepted mismatched arguments!")

        consumed_tool = self.approval_mgr.verify_and_consume_active_context("_other_tool", auth_args)
        self.assertFalse(consumed_tool, "VULNERABILITY: verify_and_consume_active_context accepted mismatched tool name!")

    async def test_concurrent_tasks_single_use_replay_prevention(self):
        """Proof: Concurrent asyncio tasks sharing the same context cannot replay or double-spend an authorization."""
        import asyncio
        tool_name = "_test_replay_tool"
        test_args = {"token": "xyz"}

        self.approval_mgr.grant_approval(tool_name, test_args)
        if hasattr(self.approval_mgr, "set_active_authorized"):
            self.approval_mgr.set_active_authorized(tool_name, test_args)
        else:
            self.approval_mgr.set_thread_authorized(tool_name, test_args)

        results = []

        async def worker():
            res = self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)
            results.append(res)
            return res

        await asyncio.gather(
            asyncio.create_task(worker()),
            asyncio.create_task(worker()),
            asyncio.create_task(worker()),
        )

        self.assertEqual(results.count(True), 1, f"Expected exactly 1 successful consumption, got: {results}")
        self.assertEqual(results.count(False), 2)

    def test_concurrent_threads_single_use_replay_prevention(self):
        """Proof: Concurrent threads in ThreadPoolExecutor with copied contexts cannot replay an authorization."""
        import concurrent.futures
        import contextvars

        tool_name = "_test_thread_replay_tool"
        test_args = {"token": "thread_xyz"}

        self.approval_mgr.grant_approval(tool_name, test_args)
        if hasattr(self.approval_mgr, "set_active_authorized"):
            self.approval_mgr.set_active_authorized(tool_name, test_args)
        else:
            self.approval_mgr.set_thread_authorized(tool_name, test_args)

        barrier = threading.Barrier(3)
        results = []

        def worker():
            barrier.wait(timeout=5.0)
            res = self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)
            results.append(res)
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = [ex.submit(contextvars.copy_context().run, worker) for _ in range(3)]
            concurrent.futures.wait(futures, timeout=5.0)

        self.assertEqual(results.count(True), 1, f"Expected exactly 1 thread success, got: {results}")
        self.assertEqual(results.count(False), 2)

    async def test_active_context_disarmed_after_orchestrator_execution(self):
        """Proof: Once orchestrator execution finishes, the active authorization token is invalidated."""
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        tool_name = "_test_disarm_tool"
        test_args = {"data": "test"}

        @SkillRegistry.register(name=tool_name, description="Test disarm tool", risk_level=0)
        def _disarm_fn(data: str):
            return "done"

        self.approval_mgr.grant_approval(tool_name, test_args)

        coordinator = DualTrackCoordinator(filler_threshold_ms=500.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        await orchestrator.execute_step_async(tool_name, test_args)

        consumed_after = self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)
        self.assertFalse(
            consumed_after,
            "VULNERABILITY: Active authorization was not disarmed after orchestrator execution!"
        )

    async def test_asyncio_to_thread_context_propagation(self):
        """Proof: asyncio.to_thread worker threads inherit the active authorization context."""
        import asyncio
        tool_name = "_test_to_thread_tool"
        test_args = {"cmd": "sync_worker"}

        self.approval_mgr.grant_approval(tool_name, test_args)
        if hasattr(self.approval_mgr, "set_active_authorized"):
            self.approval_mgr.set_active_authorized(tool_name, test_args)
        else:
            self.approval_mgr.set_thread_authorized(tool_name, test_args)

        def worker_thread_fn():
            return self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)

        result = await asyncio.to_thread(worker_thread_fn)
        self.assertTrue(result, "VULNERABILITY: asyncio.to_thread did not inherit active authorization context!")

        second_result = await asyncio.to_thread(worker_thread_fn)
        self.assertFalse(second_result, "VULNERABILITY: Second worker thread replayed the consumed authorization!")

    def test_sync_thread_authorized_backward_compatibility(self):
        """Proof: Legacy synchronous set_thread_authorized and clear_thread_authorized continue to function."""
        tool_name = "_test_legacy_sync_tool"
        test_args = {"legacy": True}

        self.approval_mgr.grant_approval(tool_name, test_args)
        self.approval_mgr.set_thread_authorized(tool_name, test_args)
        self.assertTrue(self.approval_mgr.verify_and_consume_active_context(tool_name, test_args))
        self.assertFalse(self.approval_mgr.verify_and_consume_active_context(tool_name, test_args))

        self.approval_mgr.grant_approval(tool_name, test_args)
        self.approval_mgr.set_thread_authorized(tool_name, test_args)
        self.approval_mgr.clear_thread_authorized()
        self.assertFalse(self.approval_mgr.verify_and_consume_active_context(tool_name, test_args))

    def test_nested_orchestrator_execution_preserves_outer_active_context(self):
        """Proof: Inner tool execution unwinds cleanly without wiping outer active authorization context."""
        tool_a = "_test_outer_authorized_tool"
        tool_b = "_test_inner_unprivileged_tool"
        args_a = {"op": "outer"}
        args_b = {"op": "inner"}

        @SkillRegistry.register(name=tool_b, description="Inner tool", risk_level=0)
        def _inner_fn(op: str):
            return "inner_ok"

        outer_authorized_post_inner = None

        @SkillRegistry.register(name=tool_a, description="Outer tool", risk_level=2)
        def _outer_fn(op: str, user_approved: bool = False):
            nonlocal outer_authorized_post_inner
            inner_res = orchestrator.execute_step(tool_b, args_b)
            outer_authorized_post_inner = self.approval_mgr.verify_and_consume_active_context(
                tool_a, args_a
            )
            return f"outer_ok:{inner_res}"

        self.approval_mgr.grant_approval(tool_a, args_a)
        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

        res = orchestrator.execute_step(tool_a, args_a)
        self.assertEqual(res, "outer_ok:inner_ok")
        self.assertTrue(
            outer_authorized_post_inner,
            "VULNERABILITY: Inner tool execution wiped outer active authorization context!"
        )

    def test_fabricated_grant_object_rejected(self):
        """Proof of SEC-M4-03-REV: Fabricated DualKeyAuthorization objects cannot be bound."""
        from jarvis.core.security_boundaries import DualKeyAuthorization, compute_action_hash
        import time, secrets

        tool_name = "_test_fabricated_tool"
        test_args = {"data": "attack"}
        action_hash = compute_action_hash(tool_name, test_args)

        fabricated_grant = DualKeyAuthorization(
            tool_name=tool_name,
            action_hash=action_hash,
            token=secrets.token_hex(16),
            expires_at=time.time() + 60.0,
            confirmed=True,
        )

        with self.assertRaises(PermissionError, msg="VULNERABILITY SEC-M4-03-REV: bind_active_grant accepted fabricated grant!"):
            self.approval_mgr.bind_active_grant(fabricated_grant)

        with self.assertRaises(PermissionError, msg="VULNERABILITY SEC-M4-03-REV: set_active_authorized accepted fabricated grant!"):
            self.approval_mgr.set_active_authorized(tool_name, test_args, grant=fabricated_grant)

    def test_rebinding_consumed_grant_fails(self):
        """Proof of SEC-M4-04-REV: A consumed grant cannot be rebound or reused."""
        tool_name = "_test_rebind_tool"
        test_args = {"data": "rebind"}

        grant = self.approval_mgr.grant_approval(tool_name, test_args)
        ctx_token = self.approval_mgr.bind_active_grant(grant)

        # First consumption succeeds
        consumed = self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)
        self.assertTrue(consumed)
        self.approval_mgr.clear_active_authorized(ctx_token)

        # Attempt to rebind the exact same consumed grant must fail closed
        with self.assertRaises(PermissionError, msg="VULNERABILITY SEC-M4-04-REV: Consumed grant was successfully rebound!"):
            self.approval_mgr.bind_active_grant(grant)

    def test_grant_copying_cannot_bypass_single_use(self):
        """Proof of SEC-M4-04-REV: Copying a grant object does not enable duplicate binding."""
        import copy
        tool_name = "_test_copy_tool"
        test_args = {"data": "copy"}

        grant = self.approval_mgr.grant_approval(tool_name, test_args)
        grant_copy = copy.copy(grant)

        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self.assertTrue(self.approval_mgr.verify_and_consume_active_context(tool_name, test_args))
        self.approval_mgr.clear_active_authorized(ctx_token)

        with self.assertRaises(PermissionError, msg="VULNERABILITY SEC-M4-04-REV: Copied grant was successfully rebound!"):
            self.approval_mgr.bind_active_grant(grant_copy)

    def test_concurrent_bind_attempts_allow_at_most_one_winner(self):
        """Proof of SEC-M4-04-REV: Concurrent bind attempts for the same grant allow exactly one winner."""
        import concurrent.futures

        tool_name = "_test_concurrent_bind_tool"
        test_args = {"data": "race"}

        grant = self.approval_mgr.grant_approval(tool_name, test_args)
        barrier = threading.Barrier(5)
        successes = []
        errors = []

        def worker():
            barrier.wait(timeout=5.0)
            try:
                tok = self.approval_mgr.bind_active_grant(grant)
                successes.append(tok)
            except PermissionError as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(worker) for _ in range(5)]
            concurrent.futures.wait(futures, timeout=5.0)

        self.assertEqual(len(successes), 1, f"Expected exactly 1 bind success, got {len(successes)}")
        self.assertEqual(len(errors), 4, f"Expected 4 PermissionErrors, got {len(errors)}")

    def test_verification_without_active_authorization_cannot_scavenge_grants(self):
        """Proof of SEC-M4-05-REV: Physical verification with no active authorization cannot scavenge from _grants."""
        tool_name = "_test_scavenge_victim_tool"
        test_args = {"data": "victim"}

        # Register pending grant in _grants
        self.approval_mgr.grant_approval(tool_name, test_args)

        # Clear any active authorization
        self.approval_mgr.clear_active_authorized()

        # Direct verification with no active token must fail closed and NOT scavenge
        scavenged = self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)
        self.assertFalse(scavenged, "VULNERABILITY SEC-M4-05-REV: verify_and_consume_active_context scavenged pending grant without active authorization!")

    def test_unprivileged_orchestrated_tool_cannot_scavenge_unrelated_pending_grant(self):
        """Proof of SEC-M4-05-REV: An unprivileged orchestrated tool cannot scavenge an unrelated pending grant."""
        tool_unprivileged = "_test_unprivileged_scavenger"
        tool_target = "_test_target_privileged"
        target_args = {"cmd": "rm -rf"}

        # Pre-grant approval for target tool
        self.approval_mgr.grant_approval(tool_target, target_args)

        scavenged_result = None

        @SkillRegistry.register(name=tool_unprivileged, description="Scavenger", risk_level=0)
        def _scavenger_fn():
            nonlocal scavenged_result
            scavenged_result = self.approval_mgr.verify_and_consume_active_context(tool_target, target_args)
            return "scavenger_ran"

        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)
        res = orchestrator.execute_step(tool_unprivileged, {})
        self.assertEqual(res, "scavenger_ran")
        self.assertFalse(scavenged_result, "VULNERABILITY SEC-M4-05-REV: Unprivileged tool scavenged unrelated pending grant!")

    def test_legacy_authorized_action_tuple_alone_cannot_authorize(self):
        """Proof of SEC-M4-05-REV: A legacy authorized_action tuple alone cannot authorize physical execution."""
        from jarvis.core.security_boundaries import compute_action_hash

        tool_name = "_test_tuple_fallback_tool"
        test_args = {"op": "priv"}
        action_hash = compute_action_hash(tool_name, test_args)

        # Directly set the thread-local tuple without a registered token
        self.approval_mgr._local.authorized_action = (tool_name, action_hash)
        self.approval_mgr._local.authorized_token = None

        authorized = self.approval_mgr.verify_and_consume_active_context(tool_name, test_args)
        self.assertFalse(authorized, "VULNERABILITY: Legacy authorized_action tuple alone authorized physical execution!")

    def test_inner_unapproved_tool_cannot_borrow_outer_active_token(self):
        """Proof of Nested Isolation: An inner unapproved tool cannot borrow the outer tool's active token."""
        tool_outer = "_test_outer_auth_tool"
        tool_inner = "_test_inner_unauth_tool"
        args_outer = {"target": "outer_target"}
        args_inner = {"target": "inner_target"}

        inner_borrowed_outer = None

        @SkillRegistry.register(name=tool_inner, description="Inner unapproved", risk_level=0)
        def _inner_tool_fn(target: str):
            nonlocal inner_borrowed_outer
            # Inner tool attempts to verify authorization for outer tool's action!
            inner_borrowed_outer = self.approval_mgr.verify_and_consume_active_context(tool_outer, args_outer)
            return "inner_done"

        @SkillRegistry.register(name=tool_outer, description="Outer approved", risk_level=2)
        def _outer_tool_fn(target: str, user_approved: bool = False):
            # Outer tool calls inner tool via orchestrator
            return orchestrator.execute_step(tool_inner, args_inner)

        self.approval_mgr.grant_approval(tool_outer, args_outer)
        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

        res = orchestrator.execute_step(tool_outer, args_outer)
        self.assertEqual(res, "inner_done")
        self.assertFalse(inner_borrowed_outer, "VULNERABILITY: Inner unapproved tool borrowed outer tool's active authorization!")

    def test_gate_decision_truthiness_and_fail_closed(self):
        """Proof of GateDecision fail-closed: GateDecision(allowed=False) is falsy and aborts execution."""
        from jarvis.core.security_boundaries import GateDecision

        denied_decision = GateDecision(allowed=False, grant=None)
        self.assertFalse(bool(denied_decision), "VULNERABILITY: GateDecision(allowed=False) evaluated truthy!")


class TestExternalIngestionProvenance(unittest.TestCase):
    """M1: External-Ingestion Provenance Tracking and Classification Test Suite."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.reset()

    def test_wikipedia_summary_path_correctly_classified_and_taints_turn(self):
        """M1 Req 1: Previously missed Wikipedia summary path taints turn and records provenance."""
        self.assertFalse(self.tracker.is_tainted)
        decision = self.tracker.evaluate_tool_execution(
            "get_wikipedia_summary", {"topic": "Alan Turing"}
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(
            self.tracker.is_tainted,
            "FAIL M1: get_wikipedia_summary did not taint the conversational turn!",
        )
        self.assertIn(
            "get_wikipedia_summary",
            self.tracker.tainting_sources,
            "FAIL M1: get_wikipedia_summary not present in tainting_sources!",
        )

    def test_all_canonical_web_and_external_tools_classified_and_recorded(self):
        """M1 Req 2: Canonical web, email, and external data ingestion tools are classified and recorded."""
        external_tools = [
            "web_search",
            "get_wikipedia_summary",
            "get_weather",
            "get_tech_news_headlines",
            "browse_and_read_webpage",
            "read_webpage_content",
            "check_unread_emails",
            "search_emails",
            "read_email_details",
            "deep_research",
            "execute_browser_javascript",
            "audit_webpage_accessibility",
            "audit_webpage_performance",
            "capture_devtools_snapshot",
            "get_daily_briefing",
            "search_modern_web_guidance",
            "retrieve_modern_web_guide",
            "search_gemini_api_documentation",
            "get_gemini_documentation_chunk",
        ]
        for tool_name in external_tools:
            self.tracker.reset_turn()
            self.assertFalse(self.tracker.is_tainted, f"Turn should start untainted for {tool_name}")
            decision = self.tracker.evaluate_tool_execution(tool_name, {"query": "test"})
            self.assertTrue(decision.allowed)
            self.assertTrue(
                self.tracker.is_tainted,
                f"FAIL M1: Tool '{tool_name}' was not recognized as untrusted ingestion!",
            )
            self.assertIn(
                tool_name,
                self.tracker.tainting_sources,
                f"FAIL M1: Tool '{tool_name}' missing from tainting_sources!",
            )

    def test_dynamic_mcp_tool_classified_before_output_influences_decisions(self):
        """M1 Req 3: Dynamically registered MCP tools are classified as untrusted before output influences decisions."""
        mcp_tool = "mcp_weather_get_forecast"
        self.assertFalse(self.tracker.is_tainted)

        decision = self.tracker.evaluate_tool_execution(mcp_tool, {"city": "Paris"})
        self.assertTrue(decision.allowed)
        self.assertTrue(self.tracker.is_tainted)
        self.assertIn(mcp_tool, self.tracker.tainting_sources)

        # Subsequent privileged mutation in the same turn must be blocked
        with self.assertRaises(SecurityBoundaryViolation):
            self.tracker.evaluate_tool_execution(
                "execute_powershell", {"command": "Get-Process"}, risk_level=2
            )

    def test_source_identity_distinct_stable_and_bounded(self):
        """M1 Req 4: Multiple external sources are distinctly recorded, bounded, and sanitized."""
        sources = ["web_search", "get_wikipedia_summary", "check_unread_emails"]
        for s in sources:
            self.tracker.mark_tainted(s)

        # Distinct sources preserved
        for s in sources:
            self.assertIn(s, self.tracker.tainting_sources)

        # Repeated ingestion from same source does not cause unbounded duplicate growth
        for _ in range(50):
            self.tracker.mark_tainted("web_search")

        self.assertEqual(
            len(self.tracker.tainting_sources),
            len(sources),
            "FAIL M1: Duplicate source markings caused unbounded growth in tainting_sources!",
        )

        # Unbounded payload strings or malicious names are sanitized/bounded
        dirty_source = "   " + "x" * 200 + "   "
        self.tracker.mark_tainted(dirty_source)
        # Verify bounded length
        recorded = [s for s in self.tracker.tainting_sources if "x" in s]
        self.assertEqual(len(recorded), 1)
        self.assertLessEqual(len(recorded[0]), 128)

    def test_subsequent_high_risk_mutation_denied_after_ingestion_without_grant(self):
        """M1 Req 5: After untrusted ingestion, high-privilege mutations are blocked without dual-key grant."""
        self.tracker.evaluate_tool_execution("get_wikipedia_summary", {"topic": "Security"})
        self.assertTrue(self.tracker.is_tainted)

        # High-privilege mutation (risk_level=1) must raise SecurityBoundaryViolation
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
            )
        self.assertIn("get_wikipedia_summary", str(ctx.exception))

        # Once granted by human, execution is allowed
        self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": "dir"})
        allowed_decision = self.tracker.evaluate_tool_execution(
            "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
        )
        self.assertTrue(allowed_decision.allowed)
        self.assertIsNotNone(allowed_decision.grant)

    def test_same_turn_async_and_worker_propagation(self):
        """M1 Req 6: Untrusted ingestion in worker thread updates current turn's shared provenance."""
        import concurrent.futures
        import contextvars

        self.assertFalse(self.tracker.is_tainted)
        ctx = contextvars.copy_context()

        def worker():
            self.tracker.mark_tainted("browse_and_read_webpage")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            fut = executor.submit(ctx.run, worker)
            fut.result()

        self.assertTrue(
            self.tracker.is_tainted,
            "FAIL M1: Ingestion in worker thread did not propagate to parent turn context!",
        )
        self.assertIn("browse_and_read_webpage", self.tracker.tainting_sources)

    def test_independent_turns_do_not_leak_provenance_or_taint(self):
        """M1 Req 7: Taint and provenance from one turn do not leak into another turn."""
        # Turn 1: Ingest
        self.tracker.mark_tainted("web_search")
        self.assertTrue(self.tracker.is_tainted)
        self.assertIn("web_search", self.tracker.tainting_sources)

        # Turn 2: Fresh turn
        self.tracker.reset_turn()
        self.assertFalse(
            self.tracker.is_tainted,
            "FAIL M1: Fresh turn inherited taint from previous turn!",
        )
        self.assertEqual(self.tracker.tainting_sources, ())

    def test_trusted_local_operations_do_not_taint(self):
        """M1 Req 8: Ordinary trusted local operations do not mark the turn as tainted."""
        local_tools = [
            ("recall_fact", {"key": "user_name"}),
            ("list_directory", {"path": "."}),
            ("get_current_time_and_date", {}),
            ("get_active_window", {}),
        ]
        for tool_name, args in local_tools:
            decision = self.tracker.evaluate_tool_execution(tool_name, args, risk_level=0)
            self.assertTrue(decision.allowed)
            self.assertFalse(
                self.tracker.is_tainted,
                f"FAIL M1: Trusted local tool '{tool_name}' erroneously tainted the turn!",
            )
            self.assertEqual(self.tracker.tainting_sources, ())

    def test_failure_handling_preserves_monotonicity_and_does_not_taint_on_trusted_errors(self):
        """M1 Req 9: Errors in local operations do not taint; errors during ingestion do not clear taint."""
        # Error in trusted local tool does not taint
        @SkillRegistry.register(name="_failing_local_tool", description="Test failure", risk_level=0)
        def _failing_fn():
            raise FileNotFoundError("Local file not found")

        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)
        res = orchestrator.execute_step("_failing_local_tool", {})
        self.assertIn("Local file not found", res)
        self.assertFalse(
            self.tracker.is_tainted,
            "FAIL M1: Failure of trusted local tool tainted the turn!",
        )

        # Ingestion established taint cannot be cleared or downgraded on tool error
        self.tracker.mark_tainted("web_search")
        self.assertTrue(self.tracker.is_tainted)

        # Attempt to set is_tainted = False or clear sources
        self.tracker.is_tainted = False
        self.tracker._tainting_sources = []
        self.assertTrue(
            self.tracker.is_tainted,
            "FAIL M1: Established taint was downgraded!",
        )
        self.assertIn("web_search", self.tracker.tainting_sources)

    def test_classification_completeness_audit(self):
        """M1 Req 10: Audit test checking registered ingestion tools are classified in TurnTaintTracker."""
        expected_ingestion = {
            "web_search",
            "get_wikipedia_summary",
            "get_weather",
            "get_tech_news_headlines",
            "browse_and_read_webpage",
            "read_webpage_content",
            "check_unread_emails",
            "search_emails",
            "read_email_details",
            "deep_research",
            "execute_browser_javascript",
            "audit_webpage_accessibility",
            "audit_webpage_performance",
            "capture_devtools_snapshot",
            "get_daily_briefing",
            "search_modern_web_guidance",
            "retrieve_modern_web_guide",
            "search_gemini_api_documentation",
            "get_gemini_documentation_chunk",
        }
        for tool_name in expected_ingestion:
            self.assertTrue(
                self.tracker.is_untrusted_ingestion(tool_name),
                f"FAIL M1: Ingestion tool '{tool_name}' is not recognized by is_untrusted_ingestion()!",
            )

    def test_provenance_metadata_scrubs_basic_auth_credentials(self):
        """M1 Hardening (Finding 1): Host extraction must not leak Basic Auth credentials into provenance metadata."""
        self.tracker.record_ingestion(
            "browse_and_read_webpage",
            {"url": "https://admin:supersecretpassword@api.example.com:8443/data"},
        )
        records = self.tracker.provenance_records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "browse_and_read_webpage")
        self.assertEqual(records[0].metadata.get("host"), "api.example.com")
        self.assertNotIn("supersecretpassword", str(records[0].metadata))
        self.assertNotIn("admin", str(records[0].metadata))

    def test_subsequent_ingestions_from_same_tool_preserve_provenance(self):
        """M1 Hardening (Finding 2): Multiple distinct ingestions from the same tool preserve individual provenance."""
        self.tracker.record_ingestion("browse_and_read_webpage", {"url": "https://docs.python.org"})
        self.tracker.record_ingestion("browse_and_read_webpage", {"url": "https://evil-prompt-injection.com"})
        records = self.tracker.provenance_records
        self.assertEqual(len(records), 2, "FAIL M1: Second ingestion from same tool was dropped from provenance_records!")
        hosts = [r.metadata.get("host") for r in records]
        self.assertEqual(hosts, ["docs.python.org", "evil-prompt-injection.com"])
        # Unique sources list preserves distinct tool names
        self.assertEqual(self.tracker.tainting_sources, ("browse_and_read_webpage",))

    def test_duplicate_back_to_back_identical_ingestion_deduplicated(self):
        """M1 Hardening (Finding 2 bound): Repeated identical ingestion calls are deduplicated."""
        for _ in range(10):
            self.tracker.record_ingestion("browse_and_read_webpage", {"url": "https://docs.python.org"})
        records = self.tracker.provenance_records
        self.assertEqual(len(records), 1, "FAIL M1: Repeated identical ingestion created duplicate records!")

    def test_sensitive_metadata_key_variants_and_nesting_redacted(self):
        """M1 Hardening (Finding 3): Sensitive key variants (api-key, apikey, bearer, etc.) and nested dicts are sanitized."""
        raw_meta = {
            "api-key": "secret1",
            "apikey": "secret2",
            "x-api-key": "secret3",
            "bearer_token": "secret4",
            "client_cert": "cert_val",
            "user_password": "pw_val",
            "keyboard_shortcut": "ctrl+c",
            "safe_count": 42,
            "headers": {
                "Authorization": "Bearer secret5",
                "Content-Type": "application/json",
                "apikey": "secret6",
            },
        }
        self.tracker.mark_tainted("custom_source", metadata=raw_meta)
        records = self.tracker.provenance_records
        self.assertEqual(len(records), 1)
        meta = records[0].metadata
        # Redacted keys
        self.assertNotIn("api-key", meta)
        self.assertNotIn("apikey", meta)
        self.assertNotIn("x-api-key", meta)
        self.assertNotIn("bearer_token", meta)
        self.assertNotIn("client_cert", meta)
        self.assertNotIn("user_password", meta)
        # Preserved safe keys
        self.assertEqual(meta.get("keyboard_shortcut"), "ctrl+c")
        self.assertEqual(meta.get("safe_count"), 42)
        # Nested dict sanitization
        self.assertIn("headers", meta)
        self.assertIsInstance(meta["headers"], dict)
        self.assertEqual(meta["headers"].get("content-type"), "application/json")
        self.assertNotIn("authorization", meta["headers"])
        self.assertNotIn("apikey", meta["headers"])

    def test_non_string_source_handling_and_resilience(self):
        """M1 Hardening (Finding 4): Non-string sources do not raise AttributeError."""
        try:
            self.tracker.mark_tainted(12345)
            self.tracker.mark_tainted(None)
            self.tracker.register_ingestion_tool(99999)
        except AttributeError as e:
            self.fail(f"FAIL M1: mark_tainted/register_ingestion_tool raised AttributeError: {e}")
        self.assertIn("12345", self.tracker.tainting_sources)
        self.assertIn("unknown_external_source", self.tracker.tainting_sources)

    def test_provenance_records_immutability_defensive_copy(self):
        """M1 Hardening (Finding 5): External mutation of returned provenance records does not corrupt internal state."""
        self.tracker.record_ingestion("web_search", {"query": "python"})
        records = self.tracker.get_provenance_records()
        self.assertTrue(len(records) >= 1)
        # Attempt to tamper with returned metadata dictionary
        records[0].metadata["tampered"] = True
        # Fresh retrieval must not reflect tampering
        fresh_records = self.tracker.get_provenance_records()
        self.assertNotIn("tampered", fresh_records[0].metadata)

    @mock.patch("jarvis.skills.web_search.web_search", return_value="Mocked search results")
    def test_skill_registry_direct_execution_records_provenance(self, mock_search):
        """M1 Hardening (Findings 5 & 6): Direct SkillRegistry.execute taints turn and records provenance."""
        from jarvis.skills import web_search  # Ensure skill registered
        self.assertFalse(self.tracker.is_tainted)
        # Execute tool via SkillRegistry
        res = SkillRegistry.execute("web_search", query="safe test query")
        self.assertTrue(self.tracker.is_tainted, "FAIL M1: SkillRegistry.execute did not taint turn!")
        self.assertIn("web_search", self.tracker.tainting_sources)
        records = self.tracker.provenance_records
        self.assertTrue(any(r.source == "web_search" for r in records))

    def test_provenance_records_timestamps_and_bounds(self):
        """M1 Hardening (Finding 5): Provenance records include valid timestamps and obey MAX_SOURCES_PER_TURN."""
        t_before = time.time()
        self.tracker.record_ingestion("web_search", {"query": "test"})
        t_after = time.time()
        records = self.tracker.provenance_records
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertIsInstance(r.timestamp, float)
        self.assertGreaterEqual(r.timestamp, t_before)
        self.assertLessEqual(r.timestamp, t_after)

        # Enforce maximum bounds
        for i in range(150):
            self.tracker.record_ingestion("web_search", {"query": f"query_{i}"})
        bounded_records = self.tracker.provenance_records
        self.assertLessEqual(len(bounded_records), 128)

    def test_provenance_malformed_url_and_missing_host(self):
        """M1 Hardening (Finding 1 & 5): Malformed or missing URLs do not crash and do not inject malformed hosts."""
        malformed_inputs = [
            {"url": ""},
            {"url": "not_a_url"},
            {"url": "http://"},
            {"url": 12345},
            {"url": None},
        ]
        for arg in malformed_inputs:
            self.tracker.reset_turn()
            self.tracker.record_ingestion("fetch_url", arg)
            records = self.tracker.provenance_records
            self.assertEqual(len(records), 1)
            # Malformed or missing host should not persist a malformed host
            if "host" in records[0].metadata:
                self.assertTrue(isinstance(records[0].metadata["host"], str))
                self.assertNotIn("@", records[0].metadata["host"])


if __name__ == "__main__":
    unittest.main()
