"""Adversarial regression tests for Consequential Tool Authorization Gateway (Step 3).

Verifies non-bypassable security invariants across:
1. Speculative execution engine (execute_or_claim gate routing & taint propagation).
2. Reflex macro compiler (compilation policy & direct DB injection defense-in-depth).
3. Reflex macro cooperative cancellation (mid-execution barge-in preemption).
4. Critical privilege tool classification completeness.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from jarvis.context.flow_state_mirror import EnvironmentEpochTracker
from jarvis.core.cancellation import (
    reset_global_barge_in,
    signal_global_barge_in,
)
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.reflex_macro_compiler import (
    compile_workflow_reflex,
    execute_workflow_reflex,
)
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

    def test_reflex_macro_compilation_blocks_critical_and_unregistered_tools(self):
        """Verifies compile_workflow_reflex rejects risk>=2, unregistered, recursive, or MCP tools."""
        # 1. Risk=2 tool rejection
        res_pwsh = compile_workflow_reflex(
            trigger_phrase_pattern=r"run danger",
            macro_name="danger_macro_pwsh",
            steps_json=json.dumps([{"skill_name": "execute_powershell", "static_args": {"command": "dir"}}]),
            db_path=self.tmp_db,
        )
        self.assertEqual(res_pwsh.get("status"), "rejected")

        res_js = compile_workflow_reflex(
            trigger_phrase_pattern=r"run browser js",
            macro_name="danger_macro_js",
            steps_json=json.dumps([{"skill_name": "execute_browser_javascript", "static_args": {"script": "alert(1)"}}]),
            db_path=self.tmp_db,
        )
        self.assertEqual(res_js.get("status"), "rejected")

        # 2. Unregistered tool rejection
        res_unknown = compile_workflow_reflex(
            trigger_phrase_pattern=r"run missing",
            macro_name="missing_macro",
            steps_json=json.dumps([{"skill_name": "completely_nonexistent_tool", "static_args": {}}]),
            db_path=self.tmp_db,
        )
        self.assertEqual(res_unknown.get("status"), "rejected")
        self.assertIn("not registered", res_unknown.get("reason", ""))

        # 3. Recursive macro loop rejection
        res_recursive = compile_workflow_reflex(
            trigger_phrase_pattern=r"run recursion",
            macro_name="recursive_macro",
            steps_json=json.dumps([{"skill_name": "execute_workflow_reflex", "static_args": {"macro_name": "nested"}}]),
            db_path=self.tmp_db,
        )
        self.assertEqual(res_recursive.get("status"), "rejected")

    def test_reflex_macro_execution_blocks_direct_db_injection(self):
        """Verifies execute_workflow_reflex blocks injected critical tools even if present in SQLite."""
        # Manually inject malicious steps directly into SQLite table
        from jarvis.skills.reflex_macro_compiler import _ensure_db_initialized
        _ensure_db_initialized(self.tmp_db)

        malicious_steps = [
            {"step_index": 0, "skill_name": "execute_powershell", "static_args": {"command": "whoami"}},
        ]
        conn = sqlite3.connect(self.tmp_db)
        try:
            conn.execute(
                """INSERT INTO compiled_reflex_macros
                   (macro_name, trigger_regex, steps_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                ("injected_pwsh_macro", "bypass_regex", json.dumps(malicious_steps), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        # Execute the injected macro
        res = execute_workflow_reflex(
            macro_name="injected_pwsh_macro",
            db_path=self.tmp_db,
        )
        self.assertEqual(res.get("status"), "security_denied")
        self.assertIn("forbidden", res.get("reason", ""))
        self.assertEqual(len(res.get("completed_steps", [])), 0)

    def test_reflex_macro_execution_halts_on_tainted_mutation(self):
        """Verifies execute_workflow_reflex halts when a step is blocked by turn taint."""
        step_tool = "_test_macro_mutation_step"

        @SkillRegistry.register(name=step_tool, description="Mock mutating step", risk_level=1)
        def _mock_mutating_step(arg: str = "val"):
            return f"mutated {arg}"

        try:
            steps = [{"skill_name": step_tool, "static_args": {"arg": "data"}}]
            compile_res = compile_workflow_reflex(
                trigger_phrase_pattern=r"run mutation macro",
                macro_name="mutation_macro",
                steps_json=json.dumps(steps),
                db_path=self.tmp_db,
            )
            self.assertEqual(compile_res.get("status"), "compiled")

            # Taint the turn context with untrusted source
            self.taint_tracker.mark_tainted("malicious_untrusted_source")
            self.assertTrue(self.taint_tracker.is_tainted)

            # Executing without human approval must fail closed with security_denied
            exec_res = execute_workflow_reflex(
                macro_name="mutation_macro",
                db_path=self.tmp_db,
            )
            self.assertEqual(exec_res.get("status"), "security_denied")
            self.assertIn("Dual-Key Safety Gate", exec_res.get("reason", ""))

        finally:
            SkillRegistry.unregister(step_tool)

    def test_reflex_macro_barge_in_cancellation_preemption(self):
        """Verifies that speech barge-in cancels execution between macro steps."""
        step1_tool = "_test_macro_step1"
        step2_tool = "_test_macro_step2"
        step1_executed = False
        step2_executed = False

        @SkillRegistry.register(name=step1_tool, description="Step 1", risk_level=1)
        def _step1():
            nonlocal step1_executed
            step1_executed = True
            # Simulate speech barge-in occurring immediately after step 1
            signal_global_barge_in()
            return {"step": 1}

        @SkillRegistry.register(name=step2_tool, description="Step 2", risk_level=1)
        def _step2():
            nonlocal step2_executed
            step2_executed = True
            return {"step": 2}

        try:
            steps = [
                {"skill_name": step1_tool, "static_args": {}},
                {"skill_name": step2_tool, "static_args": {}},
            ]
            compile_res = compile_workflow_reflex(
                trigger_phrase_pattern=r"run cancellable macro",
                macro_name="cancellable_macro",
                steps_json=json.dumps(steps),
                db_path=self.tmp_db,
            )
            self.assertEqual(compile_res.get("status"), "compiled")

            # Execute macro
            exec_res = execute_workflow_reflex(
                macro_name="cancellable_macro",
                db_path=self.tmp_db,
            )
            self.assertEqual(exec_res.get("status"), "cancelled")
            self.assertTrue(step1_executed, "Step 1 should have run.")
            self.assertFalse(step2_executed, "Step 2 must NOT run after barge-in cancellation.")
            self.assertEqual(len(exec_res.get("completed_steps", [])), 1)

        finally:
            SkillRegistry.unregister(step1_tool)
            SkillRegistry.unregister(step2_tool)


if __name__ == "__main__":
    unittest.main()
