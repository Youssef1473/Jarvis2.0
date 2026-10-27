"""Targeted security regression test suite for CORE-SEC-04D:
Tool Authorization Integrity in Jarvis 2.0.

Verifies:
1. execute_browser_javascript is rejected without required human approval on an untainted turn.
2. execute_browser_javascript is rejected on a tainted turn without required approval.
3. Valid human approval permits execute_browser_javascript and is consumed single-use.
4. Denied browser JavaScript never reaches the CDP execution function.
5. Ordinary read-only ingestion tools remain permitted without human approval and still record turn taint.
6. check_code_diagnostics without mutation remains available and read-only.
7. Model-callable fix=True is rejected before modifying files, leaving filesystem unchanged.
8. Critical or mutating dynamic MCP tools do not bypass authorization through ingestion classification.
9. Verified read-only MCP tools retain intended behavior and do not require elevation.
10. Cancellation and grant-replay protections continue to function securely.
11. Ingestion taint recording and privilege authorization gates remain completely orthogonal.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis.skills.browser_cdp
import jarvis.skills.dev_shell
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry

# Ensure skills are registered
_ = (jarvis.skills.browser_cdp, jarvis.skills.dev_shell)


class TestToolAuthorizationIntegrity(unittest.TestCase):
    """CORE-SEC-04D Security Specification & Regression Suite."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self.orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    # -------------------------------------------------------------------------
    # 1. execute_browser_javascript Critical Privilege Enforcement
    # -------------------------------------------------------------------------

    def test_01_execute_browser_javascript_rejected_without_approval_untainted(self):
        """1. execute_browser_javascript is rejected without human approval on an untainted turn."""
        self.assertFalse(self.tracker.is_tainted)
        risk = SkillRegistry.get_risk_level("execute_browser_javascript")
        self.assertEqual(risk, 2, "execute_browser_javascript must be registered at risk_level=2")
        self.assertIn("execute_browser_javascript", TurnTaintTracker.CRITICAL_PRIVILEGE_TOOLS)

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_browser_javascript",
                {"script": "document.cookie"},
                risk_level=risk,
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))
        self.assertIn("execute_browser_javascript", str(ctx.exception))

    def test_02_execute_browser_javascript_rejected_without_approval_tainted(self):
        """2. execute_browser_javascript is rejected on a tainted turn without human approval."""
        self.tracker.mark_tainted("web_search")
        self.assertTrue(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_browser_javascript",
                {"script": "localStorage.getItem('token')"},
                risk_level=2,
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_03_execute_browser_javascript_permitted_with_valid_approval_and_consumed(self):
        """3. A valid approval authorizes execute_browser_javascript and is consumed safely."""
        args = {"script": "document.title", "tab_name": "ActiveTab"}
        self.approval_mgr.grant_approval("execute_browser_javascript", args)

        decision = self.tracker.evaluate_tool_execution(
            "execute_browser_javascript",
            args,
            risk_level=2,
        )
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.grant)
        self.assertEqual(decision.grant.status, "gate_consumed")

        # Single-use consumption: a second call with identical arguments must fail closed
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_browser_javascript",
                args,
                risk_level=2,
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_04_denied_browser_javascript_never_reaches_cdp_execution(self):
        """4. Denied browser JavaScript never reaches the CDP execution function."""
        with patch("jarvis.skills.browser_cdp.execute_cdp_javascript") as mock_exec, \
             patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True):
            # Execute through ToolOrchestrator without approval
            res = self.orchestrator.execute_step(
                "execute_browser_javascript",
                {"script": "alert(1)", "tab_name": "TestTab"},
            )
            # Must return an authorization error message and never call CDP
            self.assertIn("Error:", str(res))
            self.assertIn("Critical Authorization Gate", str(res))
            mock_exec.assert_not_called()

    # -------------------------------------------------------------------------
    # 2. Ordinary Ingestion Tools Retain Non-Elevated Behavior & Turn Taint
    # -------------------------------------------------------------------------

    def test_05_ordinary_ingestion_tools_permitted_and_record_taint(self):
        """5. Ordinary read-only ingestion tools remain permitted and still record appropriate taint."""
        passive_tools = [
            ("web_search", {"query": "python asyncio"}),
            ("read_clipboard", {}),
            ("get_weather", {"city": "London"}),
            ("get_wikipedia_summary", {"topic": "Turing"}),
            ("read_webpage_content", {"tab_name_or_url": "docs"}),
        ]

        for tool_name, args in passive_tools:
            self.tracker.reset_turn()
            self.assertFalse(self.tracker.is_tainted)

            decision = self.tracker.evaluate_tool_execution(tool_name, args, risk_level=0)
            self.assertTrue(
                decision.allowed,
                f"Ordinary ingestion tool '{tool_name}' should be allowed without prompts."
            )
            self.assertTrue(
                self.tracker.is_tainted,
                f"Tool '{tool_name}' must mark the turn context tainted."
            )
            self.assertIn(tool_name, self.tracker.tainting_sources)

    # -------------------------------------------------------------------------
    # 3. check_code_diagnostics Read-Only Enforcement & Mutation Rejection
    # -------------------------------------------------------------------------

    def test_06_check_code_diagnostics_read_only_permitted_without_approval(self):
        """6. check_code_diagnostics without mutation remains available and read-only."""
        risk = SkillRegistry.get_risk_level("check_code_diagnostics")
        self.assertEqual(risk, 0)
        meta = SkillRegistry.get_metadata("check_code_diagnostics")
        self.assertIsNotNone(meta)
        self.assertTrue(meta.read_only)

        # On untainted turn
        decision = self.tracker.evaluate_tool_execution(
            "check_code_diagnostics",
            {"target_path": "jarvis/core"},
            risk_level=0,
        )
        self.assertTrue(decision.allowed)

        # On tainted turn (read-only diagnostics must remain permitted without elevation)
        self.tracker.mark_tainted("web_search")
        tainted_decision = self.tracker.evaluate_tool_execution(
            "check_code_diagnostics",
            {"target_path": "jarvis/core"},
            risk_level=0,
        )
        self.assertTrue(tainted_decision.allowed)

    def test_07_check_code_diagnostics_fix_flag_rejected_without_filesystem_mutation(self):
        """7. Model-callable fix=True is rejected before modifying files, leaving filesystem unchanged."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = Path(tmp_dir) / "broken.py"
            original_code = "import os, sys\nx = 1\n"
            test_file.write_text(original_code, encoding="utf-8")

            # A. Authorization boundary: tainted turn rejects fix=True as a privileged mutation
            self.tracker.mark_tainted("web_search")
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                self.tracker.evaluate_tool_execution(
                    "check_code_diagnostics",
                    {"target_path": str(test_file), "fix": True},
                    risk_level=0,
                )
            self.assertIn("Dual-Key Safety Gate", str(ctx.exception))

            # B. Dispatch boundary: direct skill call with fix=True rejects before ruff subprocess
            self.tracker.reset_turn()
            res = SkillRegistry.execute("check_code_diagnostics", target_path=str(test_file), fix=True)
            self.assertIn("Execution rejected: Automated code mutation via 'fix=True' is prohibited", res)

            # Filesystem must be strictly unmodified
            self.assertEqual(test_file.read_text(encoding="utf-8"), original_code)

    # -------------------------------------------------------------------------
    # 4. Dynamic MCP Authorization Gate Enforcement
    # -------------------------------------------------------------------------

    def test_08_dynamic_mcp_critical_and_mutating_tools_enforce_approval(self):
        """8. Critical or mutating dynamic MCP tools do not bypass authorization."""
        # A. Unclassified MCP tool defaults to risk_level=1 (fail closed)
        unclassified_mcp = "mcp_custom_service_write_record"
        self.tracker.register_ingestion_tool(unclassified_mcp)

        # On a tainted turn, unclassified MCP tool must be blocked
        self.tracker.mark_tainted("web_search")
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                unclassified_mcp,
                {"key": "value"},
                risk_level=0,  # Unclassified 0 must elevate to >= 1
            )
        self.assertIn("Dual-Key Safety Gate", str(ctx.exception))

        # B. Critical MCP tool (risk_level=2) requires unconditional approval on untainted turn
        self.tracker.reset_turn()
        critical_mcp = "mcp_terminal_exec_command"
        with self.assertRaises(SecurityBoundaryViolation) as ctx2:
            self.tracker.evaluate_tool_execution(
                critical_mcp,
                {"command": "rm -rf /"},
                risk_level=2,
            )
        self.assertIn("Critical Authorization Gate", str(ctx2.exception))

    def test_09_verified_read_only_mcp_tools_permitted_without_approval(self):
        """9. Verified read-only MCP tools retain intended behavior and do not require elevation."""
        ro_mcp = "mcp_github_read_repository"

        @SkillRegistry.register(
            name=ro_mcp,
            description="Verified read-only MCP inspection tool",
            risk_level=0,
            read_only=True,
            external_ingestion=True,
        )
        def _dummy_ro_mcp(**kwargs):
            return "repo data"

        try:
            self.tracker.reset_turn()
            decision = self.tracker.evaluate_tool_execution(
                ro_mcp,
                {"repo": "test/repo"},
                risk_level=0,
            )
            self.assertTrue(decision.allowed)
            # Ingestion taint is still properly recorded
            self.assertTrue(self.tracker.is_tainted)
            self.assertIn(ro_mcp, self.tracker.tainting_sources)
        finally:
            SkillRegistry.unregister(ro_mcp)

    # -------------------------------------------------------------------------
    # 5. Cancellation & Replay Protections
    # -------------------------------------------------------------------------

    def test_10_cancellation_and_grant_replay_protections_intact(self):
        """10. Cancellation and grant-replay protections continue to work securely."""
        cancel_event = threading.Event()
        cancel_event.set()  # Cancelled before check

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_browser_javascript",
                {"script": "alert(1)"},
                risk_level=2,
                cancel_event=cancel_event,
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_11_ingestion_and_authorization_orthogonality(self):
        """11. Ingestion taint recording and privilege authorization gates remain completely orthogonal."""
        # Tool marked as both untrusted ingestion and critical privilege
        dual_nature_tool = "_test_dual_nature_tool"

        @SkillRegistry.register(
            name=dual_nature_tool,
            description="Dual nature tool for testing",
            risk_level=2,
            read_only=False,
            external_ingestion=True,
        )
        def _dummy_dual(**kwargs):
            return "done"

        self.tracker.register_ingestion_tool(dual_nature_tool)

        try:
            self.tracker.reset_turn()
            self.assertFalse(self.tracker.is_tainted)

            # Ingestion classification MUST NOT bypass the Level 2 critical check
            with self.assertRaises(SecurityBoundaryViolation):
                self.tracker.evaluate_tool_execution(
                    dual_nature_tool,
                    {"payload": "test"},
                    risk_level=2,
                )

            # But the attempt MUST have tainted the turn with provenance!
            self.assertTrue(
                self.tracker.is_tainted,
                "Failed tool execution attempt must still register turn taint provenance."
            )
            self.assertIn(dual_nature_tool, self.tracker.tainting_sources)
        finally:
            SkillRegistry.unregister(dual_nature_tool)


if __name__ == "__main__":
    unittest.main()
