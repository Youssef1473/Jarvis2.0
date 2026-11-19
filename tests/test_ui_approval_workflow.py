"""End-to-end integration test suite for the Human Approval Workflow in Jarvis 2.0.

Verifies:
1. End-to-end UI approval: ToolOrchestrator requests human authorization via JarvisUIService,
   the request is presented with readable summary, user approves via REST API, execution succeeds.
2. End-to-end UI rejection: User denies via REST API, execution fails closed with SecurityBoundaryViolation.
3. Replay prevention: An authorization grant is single-use; subsequent execution with identical arguments fails.
4. Expired grant rejection: Grants with expired TTL are rejected at the execution boundary.
5. Parameter tampering / payload modification defense: Modifying arguments produces a different action hash and is rejected.
6. Safe tool isolation: Non-critical tools execute without triggering approval before or after execution.
7. WebSocket approval response unblocks execution.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    TurnTaintTracker,
    compute_action_hash,
)
from jarvis.skills.base import SkillRegistry
from jarvis.ui.server import JarvisUIService, get_hud_session_token


class TestUIApprovalWorkflow(unittest.TestCase):
    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()

        # Register test privileged tool (risk_level=2) and safe read tool (risk_level=0)
        @SkillRegistry.register(
            name="test_privileged_mutation_tool",
            description="Test Privileged Mutation Tool",
            risk_level=2,
            origin="custom",
        )
        def _mock_priv(arg_x: str = "default", **kwargs):
            return f"Privileged execution success: {arg_x}"

        @SkillRegistry.register(
            name="test_safe_read_tool",
            description="Test Safe Read Tool",
            risk_level=0,
            origin="custom",
            read_only=True,
        )
        def _mock_safe(**kwargs):
            return "Safe tool output"

        SkillRegistry.certify_test_read_only_tool("test_safe_read_tool")

        self.service = JarvisUIService(orchestrator=None, host="127.0.0.1", port=20149)
        self.app = self.service._create_app()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:20149")

    def tearDown(self):
        self.approval_mgr.reset()
        self.tracker.reset_turn()
        SkillRegistry.unregister("test_privileged_mutation_tool")
        SkillRegistry.unregister("test_safe_read_tool")
        SkillRegistry.decertify_test_read_only_tool("test_safe_read_tool")

    def _http_get(self, path: str) -> dict:
        resp = self.client.get(
            path,
            headers={"X-Jarvis-Token": get_hud_session_token()},
        )
        return resp.json()

    def _http_post(self, path: str, body: dict) -> dict:
        resp = self.client.post(
            path,
            json=body,
            headers={"X-Jarvis-Token": get_hud_session_token()},
        )
        return resp.json()

    def test_end_to_end_ui_approval_grants_execution(self):
        """Proof: Interactive approval flow via UI presents summary and permits execution upon approval."""
        tool_args = {"arg_x": "run_authorized"}
        execution_result = {}

        def _run_tool():
            orch = ToolOrchestrator()
            res = orch.execute_step("test_privileged_mutation_tool", tool_args)
            execution_result["result"] = res

        t = threading.Thread(target=_run_tool, daemon=True)
        t.start()

        # Poll for pending approval request in UI
        pending_item = None
        for _ in range(30):
            time.sleep(0.05)
            pending_resp = self._http_get("/api/approval/pending")
            items = pending_resp.get("pending", [])
            if items:
                pending_item = items[0]
                break

        self.assertIsNotNone(pending_item, "Approval request should appear in pending list")
        self.assertEqual(pending_item["tool_name"], "test_privileged_mutation_tool")
        self.assertEqual(pending_item["arguments"]["arg_x"], "run_authorized")

        action_hash = pending_item["action_hash"]
        self.assertEqual(action_hash, compute_action_hash("test_privileged_mutation_tool", tool_args))

        # Human operator approves the request via UI
        resp = self._http_post("/api/approval/respond", {"action_hash": action_hash, "decision": "approve"})
        self.assertEqual(resp.get("status"), "recorded")
        self.assertTrue(resp.get("approved"))

        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "Tool thread should complete after user approval")
        self.assertIn("Privileged execution success: run_authorized", str(execution_result.get("result")))

    def test_end_to_end_ui_rejection_denies_execution(self):
        """Proof: Interactive rejection via UI denies execution and fails closed before tool runs."""
        tool_args = {"arg_x": "run_denied"}
        execution_result = {}

        def _run_tool():
            orch = ToolOrchestrator()
            res = orch.execute_step("test_privileged_mutation_tool", tool_args)
            execution_result["result"] = res

        t = threading.Thread(target=_run_tool, daemon=True)
        t.start()

        # Poll for pending approval request
        pending_item = None
        for _ in range(30):
            time.sleep(0.05)
            pending_resp = self._http_get("/api/approval/pending")
            items = pending_resp.get("pending", [])
            if items:
                pending_item = items[0]
                break

        self.assertIsNotNone(pending_item)
        action_hash = pending_item["action_hash"]

        # User denies the request via UI
        resp = self._http_post("/api/approval/respond", {"action_hash": action_hash, "decision": "deny"})
        self.assertEqual(resp.get("status"), "recorded")
        self.assertFalse(resp.get("approved"))

        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())
        self.assertIn("Critical Authorization Gate", str(execution_result.get("result")))

    def test_replay_prevention_rejects_second_execution(self):
        """Proof: A granted authorization is single-use; subsequent execution is blocked."""
        tool_args = {"arg_x": "replay_arg"}

        # Issue pre-grant via REST API
        grant_resp = self._http_post("/api/approval/grant", {
            "tool_name": "test_privileged_mutation_tool",
            "arguments": tool_args,
            "ttl_seconds": 60.0,
        })
        self.assertEqual(grant_resp.get("status"), "granted")

        orch = ToolOrchestrator()
        res1 = orch.execute_step("test_privileged_mutation_tool", tool_args)
        self.assertIn("Privileged execution success: replay_arg", str(res1))

        # Second execution with identical arguments must fail closed (grant consumed)
        self.approval_mgr.set_approval_handler(lambda req: False)
        res2 = orch.execute_step("test_privileged_mutation_tool", tool_args)
        self.assertIn("Critical Authorization Gate", str(res2))

    def test_expired_grant_rejection(self):
        """Proof: An expired grant cannot authorize execution."""
        tool_args = {"arg_x": "expired_arg"}

        # Issue grant with very short TTL (0.1s)
        self._http_post("/api/approval/grant", {
            "tool_name": "test_privileged_mutation_tool",
            "arguments": tool_args,
            "ttl_seconds": 0.1,
        })

        # Wait for grant to expire
        time.sleep(0.25)

        # Set approval handler to deny to verify expired grant fails
        self.approval_mgr.set_approval_handler(lambda req: False)
        orch = ToolOrchestrator()
        res = orch.execute_step("test_privileged_mutation_tool", tool_args)
        self.assertIn("Critical Authorization Gate", str(res))

    def test_modified_payload_tampering_prevention(self):
        """Proof: An authorization grant bound to payload A cannot authorize modified payload B."""
        approved_args = {"arg_x": "safe_arg"}
        tampered_args = {"arg_x": "malicious_arg"}

        # Grant approval for approved_args
        self._http_post("/api/approval/grant", {
            "tool_name": "test_privileged_mutation_tool",
            "arguments": approved_args,
            "ttl_seconds": 60.0,
        })

        # Attempt execution with tampered_args (handler denies if queried)
        self.approval_mgr.set_approval_handler(lambda req: False)
        orch = ToolOrchestrator()
        res = orch.execute_step("test_privileged_mutation_tool", tampered_args)
        self.assertIn("Critical Authorization Gate", str(res))

    def test_safe_tools_bypass_approval_without_post_execution_interference(self):
        """Proof: Non-critical tools execute directly without triggering approval before or after."""
        orch = ToolOrchestrator()
        mock_handler = mock.MagicMock(return_value=False)
        self.approval_mgr.set_approval_handler(mock_handler)
        res = orch.execute_step("test_safe_read_tool", {})
        self.assertIn("Safe tool output", str(res))
        mock_handler.assert_not_called()

        # Verify no pending approval was created
        pending = self._http_get("/api/approval/pending")
        self.assertEqual(len(pending.get("pending", [])), 0)

    def test_websocket_approval_response_unblocks_tool(self):
        """Proof: Submitting approval_response via submit_approval_decision (used by WebSocket) unblocks tool."""
        tool_args = {"arg_x": "ws_arg"}
        execution_result = {}

        def _run_tool():
            orch = ToolOrchestrator()
            res = orch.execute_step("test_privileged_mutation_tool", tool_args)
            execution_result["result"] = res

        t = threading.Thread(target=_run_tool, daemon=True)
        t.start()

        # Wait for pending request
        action_hash = None
        for _ in range(30):
            time.sleep(0.05)
            pending_resp = self._http_get("/api/approval/pending")
            items = pending_resp.get("pending", [])
            if items:
                action_hash = items[0]["action_hash"]
                break

        self.assertIsNotNone(action_hash)
        # Simulate websocket dispatch: submit approval decision directly
        success = self.service.submit_approval_decision(action_hash, approved=True)
        self.assertTrue(success)

        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())
        self.assertIn("Privileged execution success: ws_arg", str(execution_result.get("result")))


if __name__ == "__main__":
    unittest.main()
