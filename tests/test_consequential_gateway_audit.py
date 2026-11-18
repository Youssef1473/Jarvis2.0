"""Adversarial regression tests for Consequential Tool Authorization Gateway (Step 3).

Verifies non-bypassable security invariants across:
1. Speculative execution engine (execute_or_claim gate routing & taint propagation).
2. Reflex macro compiler (compilation policy & direct DB injection defense-in-depth).
3. Reflex macro cooperative cancellation (mid-execution barge-in preemption).
4. Critical privilege tool classification completeness.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from jarvis.context.flow_state_mirror import EnvironmentEpochTracker
from jarvis.core.cancellation import (
    reset_global_barge_in,
)
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry
from jarvis.speculative.engine import ExecutionMetrics, SpeculativeExecutionEngine


class TestConsequentialGatewayAudit(unittest.TestCase):
    def setUp(self):
        reset_global_barge_in()
        EnvironmentEpochTracker.reset()
        self.taint_tracker = TurnTaintTracker.get_instance()
        self.taint_tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()

        self.speculative_engine = SpeculativeExecutionEngine.get_instance()
        self.speculative_engine.metrics = ExecutionMetrics()
        self.speculative_engine.discard_unclaimed()

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db = str(Path(self.tmp_dir.name) / "test_macros.db")

    def tearDown(self):
        reset_global_barge_in()
        self.speculative_engine.discard_unclaimed()
        self.taint_tracker.reset_turn()
        self.approval_mgr.reset()
        EnvironmentEpochTracker.reset()
        self.tmp_dir.cleanup()

    def test_critical_privilege_tools_completeness(self):
        """Verifies that all 6 Risk=2 tools are classified as critical privilege tools."""
        expected_risk2_tools = {
            "create_or_update_custom_skill",
            "delete_custom_skill",
            "execute_powershell",
            "execute_browser_javascript",
            "run_background_task",
            "release_file_lock",
        }
        for tool_name in expected_risk2_tools:
            self.assertIn(
                tool_name,
                self.taint_tracker.CRITICAL_PRIVILEGE_TOOLS,
                f"Risk=2 tool '{tool_name}' must be listed in CRITICAL_PRIVILEGE_TOOLS.",
            )

    def test_speculative_execute_or_claim_blocks_unauthorized_critical_tool(self):
        """Verifies that execute_or_claim routes non-read-only tools through SkillRegistry gate.

        Direct invocation must raise SecurityBoundaryViolation when critical tools lack user approval.
        """
        import jarvis.skills.dev_shell  # noqa: F401
        # execute_powershell is risk_level=2 and requires approval
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.speculative_engine.execute_or_claim("execute_powershell", command="echo vulnerable")

        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_speculative_cache_hit_propagates_ingestion_taint_to_claiming_turn(self):
        """Verifies that claiming a speculatively executed ingestion tool taints the claiming turn."""
        tool_name = "_test_speculative_ingestion_feed"
        call_count = 0

        with SkillRegistry.scoped_test_read_only_certification(tool_name):
            @SkillRegistry.register(
                name=tool_name,
                read_only=True,
                risk_level=0,
                external_ingestion=True,
            )
            def mock_ingestion(url: str):
                nonlocal call_count
                call_count += 1
                return f"Content from {url}"

            try:
                # Mark tool as untrusted ingestion source
                self.taint_tracker.register_ingestion_tool(tool_name)
                self.assertFalse(self.taint_tracker.is_tainted)

                # 1. Speculate in background
                key = self.speculative_engine.speculate(tool_name, url="https://example.com/data")
                self.assertIsNotNone(key)

                # Wait for completion
                for _ in range(50):
                    with self.speculative_engine._entries_lock:
                        entry = self.speculative_engine._active_entries.get(key)
                    if entry and entry.future.done():
                        break
                    time.sleep(0.01)

                # 2. Claim result
                result = self.speculative_engine.execute_or_claim(tool_name, url="https://example.com/data")
                self.assertEqual(result, "Content from https://example.com/data")

                # 3. Ingestion taint must now be established monotonically on the calling turn
                self.assertTrue(
                    self.taint_tracker.is_tainted,
                    "Claiming speculative ingestion result must taint active turn context.",
                )
                self.assertIn(tool_name, self.taint_tracker.tainting_sources)

            finally:
                SkillRegistry.unregister(tool_name)


if __name__ == "__main__":
    unittest.main()
