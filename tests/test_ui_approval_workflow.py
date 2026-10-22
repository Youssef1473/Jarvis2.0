"""End-to-end integration test suite for the Human Approval Workflow in Jarvis 2.0.

Verifies:
1. End-to-end UI approval: ToolOrchestrator requests human authorization via JarvisUIService,
   the request is presented with readable summary, user approves via REST API, execution succeeds.
2. End-to-end UI rejection: User denies via REST API, execution fails closed with SecurityBoundaryViolation.
3. Replay prevention: An authorization grant is single-use; subsequent execution with identical arguments fails.
4. Expired grant rejection: Grants with expired TTL are rejected at the execution boundary.
5. Parameter tampering / payload modification defense: Modifying arguments produces a different action hash and is rejected.
6. Safe tool isolation: Non-critical tools execute without triggering approval before or after execution.
"""
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    TurnTaintTracker,
    compute_action_hash,
    format_action_summary,
)
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.skills.base import SkillRegistry
from jarvis.skills.self_evolution import CUSTOM_SKILLS_DIR
from jarvis.ui.server import JarvisUIService


class TestUIApprovalWorkflow(unittest.TestCase):
    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.port = 20149
        self.service = JarvisUIService(orchestrator=None, host="127.0.0.1", port=self.port)
        self.service.start()
        time.sleep(0.4)

    def tearDown(self):
        self.service.stop()
        self.approval_mgr.reset()
        self.tracker.reset_turn()
        time.sleep(0.2)

    def _http_get(self, path: str) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_end_to_end_ui_approval_grants_execution(self):
        """Proof: Interactive approval flow via UI presents summary and permits execution upon approval."""
        clean_name = "test_e2e_approved_skill"
        valid_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{clean_name}", description="E2E test")
def {clean_name}():
    return "executed_ok"
"""
        tool_args = {
            "skill_name": clean_name,
            "python_code": valid_code,
            "description": "A dynamic capability",
        }

        execution_result = {}
        fake_canary = mock.MagicMock(
            success=True,
            skill_name=clean_name,
            message="Successfully engineered and hot-reloaded capability 'test_e2e_approved_skill', Sir!",
            target_path=CUSTOM_SKILLS_DIR / f"{clean_name}.py",
        )

        def _run_tool():
            with mock.patch("jarvis.core.evolution.sandbox.SkillSandbox.validate_and_promote", return_value=fake_canary) as mock_promote:
                orch = ToolOrchestrator()
                res = orch.execute_step("create_or_update_custom_skill", tool_args)
                execution_result["result"] = res
                execution_result["called_user_approved"] = mock_promote.call_args[1].get("user_approved")

        t = threading.Thread(target=_run_tool, daemon=True)
        t.start()

        # Poll for pending approval request in UI
        pending_item = None
        for _ in range(30):
            time.sleep(0.1)
            pending_resp = self._http_get("/api/approval/pending")
            items = pending_resp.get("pending", [])
            if items:
                pending_item = items[0]
                break

        self.assertIsNotNone(pending_item, "Approval request should appear in pending list")
        self.assertEqual(pending_item["tool_name"], "create_or_update_custom_skill")
        self.assertIn("Autonomous Self-Building", pending_item["summary"])
        self.assertEqual(pending_item["arguments"]["skill_name"], clean_name)

        action_hash = pending_item["action_hash"]
        self.assertEqual(action_hash, compute_action_hash("create_or_update_custom_skill", tool_args))

        # Human operator approves the request via UI
        resp = self._http_post("/api/approval/respond", {"action_hash": action_hash, "decision": "approve"})
        self.assertEqual(resp.get("status"), "recorded")
        self.assertTrue(resp.get("approved"))

        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "Tool thread should complete after user approval")
        self.assertIn("Successfully engineered and hot-reloaded", str(execution_result.get("result")))
        self.assertTrue(execution_result.get("called_user_approved"), "Sandbox promotion must receive user_approved=True")

    def test_end_to_end_ui_rejection_denies_execution(self):
        """Proof: Interactive rejection via UI denies execution and fails closed before tool runs."""
        clean_name = "test_e2e_denied_skill"
        valid_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{clean_name}", description="Denied test")
def {clean_name}():
    return "should_not_run"
"""
        tool_args = {
            "skill_name": clean_name,
            "python_code": valid_code,
            "description": "Denied capability",
        }

        execution_result = {}

        def _run_tool():
            orch = ToolOrchestrator()
            res = orch.execute_step("create_or_update_custom_skill", tool_args)
            execution_result["result"] = res

        t = threading.Thread(target=_run_tool, daemon=True)
        t.start()

        # Poll for pending approval request
        pending_item = None
        for _ in range(30):
            time.sleep(0.1)
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
        target_path = CUSTOM_SKILLS_DIR / f"{clean_name}.py"
        self.assertFalse(target_path.exists(), "Denied skill must never be written to custom directory")

    def test_replay_prevention_rejects_second_execution(self):
        """Proof: A granted authorization is single-use; subsequent execution is blocked."""
        clean_name = "test_replay_prevention_skill"
        valid_code = "def dummy(): pass"
        tool_args = {"skill_name": clean_name, "python_code": valid_code}

        # Issue pre-grant via REST API
        grant_resp = self._http_post("/api/approval/grant", {
            "tool_name": "create_or_update_custom_skill",
            "arguments": tool_args,
            "ttl_seconds": 60.0,
        })
        self.assertEqual(grant_resp.get("status"), "granted")

        fake_canary = mock.MagicMock(success=True, skill_name=clean_name, message="First execution success")
        with mock.patch("jarvis.core.evolution.sandbox.SkillSandbox.validate_and_promote", return_value=fake_canary):
            orch = ToolOrchestrator()
            res1 = orch.execute_step("create_or_update_custom_skill", tool_args)
            self.assertIn("First execution success", str(res1))

            # Second execution with identical arguments must fail closed (grant consumed)
            # Set approval handler to deny immediately to simulate human rejection
            self.approval_mgr.set_approval_handler(lambda req: False)
            res2 = orch.execute_step("create_or_update_custom_skill", tool_args)
            self.assertIn("Critical Authorization Gate", str(res2))

    def test_expired_grant_rejection(self):
        """Proof: An expired grant cannot authorize execution."""
        clean_name = "test_expired_grant_skill"
        tool_args = {"skill_name": clean_name, "python_code": "def dummy(): pass"}

        # Issue grant with very short TTL (0.1s)
        self._http_post("/api/approval/grant", {
            "tool_name": "create_or_update_custom_skill",
            "arguments": tool_args,
            "ttl_seconds": 0.1,
        })

        # Wait for grant to expire
        time.sleep(0.25)

        # Set approval handler to deny to verify expired grant fails
        self.approval_mgr.set_approval_handler(lambda req: False)
        orch = ToolOrchestrator()
        res = orch.execute_step("create_or_update_custom_skill", tool_args)
        self.assertIn("Critical Authorization Gate", str(res))

    def test_modified_payload_tampering_prevention(self):
        """Proof: An authorization grant bound to payload A cannot authorize modified payload B."""
        approved_args = {"skill_name": "target_skill", "python_code": "def safe_code(): return 1"}
        tampered_args = {"skill_name": "target_skill", "python_code": "def malicious_code(): return 2"}

        # Grant approval for approved_args
        self._http_post("/api/approval/grant", {
            "tool_name": "create_or_update_custom_skill",
            "arguments": approved_args,
            "ttl_seconds": 60.0,
        })

        # Attempt execution with tampered_args (handler denies if queried)
        self.approval_mgr.set_approval_handler(lambda req: False)
        orch = ToolOrchestrator()
        res = orch.execute_step("create_or_update_custom_skill", tampered_args)
        self.assertIn("Critical Authorization Gate", str(res))

    def test_safe_tools_bypass_approval_without_post_execution_interference(self):
        """Proof: Non-critical tools execute directly without triggering approval before or after."""
        orch = ToolOrchestrator()
        mock_handler = mock.MagicMock(return_value=False)
        self.approval_mgr.set_approval_handler(mock_handler)
        res = orch.execute_step("list_custom_skills", {})
        self.assertIsInstance(res, str)
        mock_handler.assert_not_called()

        # Verify no pending approval was created
        pending = self._http_get("/api/approval/pending")
        self.assertEqual(len(pending.get("pending", [])), 0)

    def test_websocket_approval_response_unblocks_tool(self):
        """Proof: Submitting approval_response via submit_approval_decision (used by WebSocket) unblocks tool."""
        clean_name = "test_ws_approved_skill"
        valid_code = "def ws_code(): return 42"
        tool_args = {"skill_name": clean_name, "python_code": valid_code}

        fake_canary = mock.MagicMock(success=True, skill_name=clean_name, message="WS approval success")
        execution_result = {}

        def _run_tool():
            with mock.patch("jarvis.core.evolution.sandbox.SkillSandbox.validate_and_promote", return_value=fake_canary):
                orch = ToolOrchestrator()
                res = orch.execute_step("create_or_update_custom_skill", tool_args)
                execution_result["result"] = res

        t = threading.Thread(target=_run_tool, daemon=True)
        t.start()

        # Wait for pending request
        action_hash = None
        for _ in range(30):
            time.sleep(0.1)
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
        self.assertIn("WS approval success", str(execution_result.get("result")))


if __name__ == "__main__":
    unittest.main()
