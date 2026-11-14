"""Regression test suite for final trusted-tool classification corrections.

Verifies:
1. execute_workflow_reflex dynamic provenance and taint propagation across nested operations.
2. verify_learned_habits and list_compiled_reflex_macros persistent-content provenance boundary.
3. profile_runtime subprocess taint boundary across success, failure, and timeout.
4. Correct untrusted classification for list_active_timers, record_workspace_mutation,
   inspect_ram_hogs, and run_system_troubleshooting.
"""
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jarvis.skills.dev_shell  # noqa: F401 - ensures list_directory is registered
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    IngestionTrustClassification,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.diagnostics import profile_runtime, run_system_troubleshooting
from jarvis.skills.reflex_macro_compiler import (
    compile_workflow_reflex,
    execute_workflow_reflex,
    list_compiled_reflex_macros,
)
from jarvis.skills.self_evolution import verify_learned_habits
from jarvis.skills.system_maintenance import inspect_ram_hogs
from jarvis.skills.timers import cancel_timer, list_active_timers, start_timer


class TestFinalToolClassificationRemediation(unittest.TestCase):
    """Rigorous verification of the final trusted-tool classification remediations."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.tmp_fd, self.tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(self.tmp_fd)

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        if os.path.exists(self.tmp_db):
            try:
                os.remove(self.tmp_db)
            except OSError:
                pass

    def test_questionable_tools_not_in_trusted_system_data(self):
        """Verifies evicted tools are absent from TRUSTED_SYSTEM_DATA_TOOLS and classified as untrusted/dynamic."""
        evicted_tools = [
            "verify_learned_habits",
            "list_compiled_reflex_macros",
            "profile_runtime",
            "list_active_timers",
            "record_workspace_mutation",
            "inspect_ram_hogs",
            "run_system_troubleshooting",
            "jarvis_doctor",
            "execute_protocol",
        ]
        for tool in evicted_tools:
            self.assertNotIn(
                tool,
                self.tracker.TRUSTED_SYSTEM_DATA_TOOLS,
                f"Tool '{tool}' must not be in TRUSTED_SYSTEM_DATA_TOOLS allowlist!",
            )
            classification = self.tracker.classify_tool_ingestion(tool)
            self.assertEqual(
                classification,
                IngestionTrustClassification.KNOWN_UNTRUSTED,
                f"Tool '{tool}' must be classified as KNOWN_UNTRUSTED, got {classification}",
            )

        # execute_workflow_reflex must be in DYNAMIC_DOCUMENT_PROVENANCE_TOOLS
        self.assertNotIn("execute_workflow_reflex", self.tracker.TRUSTED_SYSTEM_DATA_TOOLS)
        self.assertIn("execute_workflow_reflex", self.tracker.DYNAMIC_DOCUMENT_PROVENANCE_TOOLS)

    def test_verify_learned_habits_establishes_provenance(self):
        """Verifies verify_learned_habits establishes persistent memory taint before returning."""
        self.assertFalse(self.tracker.is_tainted)

        mock_engine = MagicMock()
        mock_engine.list_learned_habits.return_value = [
            {
                "trigger_phrase": "open user reports",
                "skill_name": "launch_application",
                "skill_kwargs": {"app_name": "reports.exe"},
                "hit_count": 3,
                "turn1_latency_ms": 250.0,
                "reflex_latency_ms": 12.0,
            }
        ]

        with patch("jarvis.skills.self_evolution._get_reflex_engine", return_value=mock_engine):
            report = verify_learned_habits()
            self.assertIn("open user reports", report)
            self.assertTrue(self.tracker.is_tainted, "verify_learned_habits must taint turn when habits are returned!")
            self.assertIn("persistent_memory:learned_habits", self.tracker.tainting_sources)

    def test_list_compiled_reflex_macros_establishes_provenance(self):
        """Verifies list_compiled_reflex_macros establishes persistent memory taint."""
        self.assertFalse(self.tracker.is_tainted)

        res = list_compiled_reflex_macros(db_path=self.tmp_db)
        self.assertEqual(res.get("status"), "ok")
        self.assertTrue(self.tracker.is_tainted, "list_compiled_reflex_macros must taint active turn!")
        self.assertIn("persistent_memory:compiled_reflex_macros", self.tracker.tainting_sources)

    def test_profile_runtime_subprocess_taint_lifecycle(self):
        """Verifies profile_runtime taints turn on normal output, error output, and timeout."""
        # 1. Success execution path
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Thread 1:\nFile \"foo.py\", line 10\n"
        mock_proc.stderr = ""

        with patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe"), \
             patch("jarvis.skills.job_object_sandbox.run_sandboxed_batch", return_value=mock_proc):
            out = profile_runtime(mode="dump")
            self.assertIn("Runtime Thread Diagnostics", out)
            self.assertTrue(self.tracker.is_tainted, "profile_runtime must taint turn on successful run!")
            self.assertIn("subprocess_output:profile_runtime", self.tracker.tainting_sources)

        # 2. Non-zero exit code execution path
        self.tracker.reset_turn()
        mock_proc.returncode = 1
        mock_proc.stderr = "Access denied sampling process"

        with patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe"), \
             patch("jarvis.skills.job_object_sandbox.run_sandboxed_batch", return_value=mock_proc):
            out = profile_runtime(mode="dump")
            self.assertIn("Diagnostic profiling encountered an access notice", out)
            self.assertTrue(self.tracker.is_tainted, "profile_runtime must taint turn on access notice with stderr!")
            self.assertIn("subprocess_output:profile_runtime", self.tracker.tainting_sources)

        # 3. Timeout execution path
        self.tracker.reset_turn()
        with patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe"), \
             patch("jarvis.skills.job_object_sandbox.run_sandboxed_batch", side_effect=subprocess.TimeoutExpired(cmd=["py-spy"], timeout=8.0)):
            out = profile_runtime(mode="dump")
            self.assertIn("timed out", out)
            self.assertTrue(self.tracker.is_tainted, "profile_runtime must taint turn on timeout!")
            self.assertIn("subprocess_output:profile_runtime", self.tracker.tainting_sources)

    def test_nested_workflow_execution_taint_propagation_and_safety_gate(self):
        """Integration test: Macro executing an untrusted child skill taints turn and blocks subsequent privileged operation."""
        child_privileged = "_test_child_privileged_step"

        @SkillRegistry.register(name=child_privileged, description="Privileged mutation", risk_level=1, origin="custom")
        def _mock_privileged():
            return "mutated host"

        try:
            # Step 1: list_directory (untrusted ingestion tool, read_only=True)
            steps = [{"skill_name": "list_directory", "static_args": {"folder_path": "."}}]
            compile_res = compile_workflow_reflex(
                trigger_phrase_pattern=r"run untrusted workflow",
                macro_name="untrusted_macro",
                steps_json=json.dumps(steps),
                db_path=self.tmp_db,
            )
            self.assertEqual(compile_res.get("status"), "compiled")

            self.assertFalse(self.tracker.is_tainted)
            exec_res = execute_workflow_reflex("untrusted_macro", db_path=self.tmp_db)
            self.assertEqual(exec_res.get("status"), "success")
            self.assertTrue(self.tracker.is_tainted, "Turn must be tainted after workflow executed untrusted child!")
            self.assertIn("workflow_reflex:untrusted_macro", self.tracker.tainting_sources)

            # 2. Subsequent privileged operation in same turn MUST be blocked by Dual-Key gate
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(child_privileged)
            self.assertIn("Dual-Key Safety Gate", str(ctx.exception))
            self.assertIn("blocked high-privilege tool", str(ctx.exception))

        finally:
            SkillRegistry.unregister(child_privileged)

    def test_nested_workflow_intra_macro_tainted_mutation_blocked(self):
        """Integration test: Step 0 taints turn within macro, Step 1 is privileged mutation -> blocked at Step 1."""
        step2_mutate = "_test_step2_mutate"

        @SkillRegistry.register(name=step2_mutate, description="Mutating action step", risk_level=1, origin="custom")
        def _step2():
            return "performed mutation"

        try:
            steps = [
                {"skill_name": "list_directory", "static_args": {"folder_path": "."}},
                {"skill_name": step2_mutate, "static_args": {}},
            ]
            compile_res = compile_workflow_reflex(
                trigger_phrase_pattern=r"run compound workflow",
                macro_name="compound_macro",
                steps_json=json.dumps(steps),
                db_path=self.tmp_db,
            )
            self.assertEqual(compile_res.get("status"), "compiled")

            self.assertFalse(self.tracker.is_tainted)
            exec_res = execute_workflow_reflex("compound_macro", db_path=self.tmp_db)
            self.assertEqual(exec_res.get("status"), "security_denied")
            self.assertEqual(exec_res.get("failed_step"), 1)
            self.assertIn("Dual-Key Safety Gate", exec_res.get("reason", ""))
            self.assertTrue(self.tracker.is_tainted, "Parent turn must remain tainted when child step tainted!")

        finally:
            SkillRegistry.unregister(step2_mutate)

    def test_list_active_timers_taints_turn_when_timers_present(self):
        """Verifies list_active_timers taints turn when countdown timers with user labels exist."""
        self.assertFalse(self.tracker.is_tainted)

        # Start a timer with a user label
        start_timer("60s", label="User Custom Task")
        try:
            timers_out = list_active_timers()
            self.assertIn("User Custom Task", timers_out)
            self.assertTrue(self.tracker.is_tainted, "list_active_timers must taint turn when active timers exist!")
            self.assertIn("in_memory_telemetry:list_active_timers", self.tracker.tainting_sources)
        finally:
            cancel_timer("User Custom Task")

    def test_inspect_ram_hogs_taints_turn(self):
        """Verifies inspect_ram_hogs taints turn on process inspection."""
        self.assertFalse(self.tracker.is_tainted)

        out = inspect_ram_hogs()
        self.assertIn("Top Memory Consumers", out)
        self.assertTrue(self.tracker.is_tainted, "inspect_ram_hogs must taint turn!")
        self.assertIn("os_telemetry:inspect_ram_hogs", self.tracker.tainting_sources)

    def test_run_system_troubleshooting_taints_turn(self):
        """Verifies jarvis_doctor and run_system_troubleshooting taint turn."""
        self.assertFalse(self.tracker.is_tainted)

        with patch("jarvis.core.health_check.run_system_health_check") as mock_health:
            mock_health.return_value = {
                "timestamp": 123456.0,
                "status": "HEALTHY",
                "subsystems": {"audio": {"status": "OK"}},
            }
            report = run_system_troubleshooting(verbose=False)
            self.assertTrue(len(report) > 0)
            self.assertTrue(self.tracker.is_tainted, "run_system_troubleshooting must taint turn!")
            self.assertIn("diagnostics:jarvis_doctor", self.tracker.tainting_sources)


if __name__ == "__main__":
    unittest.main()
