"""Regression test suite for final trusted-tool classification corrections.

Verifies:
1. verify_learned_habits persistent-content provenance boundary.
2. profile_runtime subprocess taint boundary across success, failure, and timeout.
3. Correct untrusted classification for list_active_timers, record_workspace_mutation,
   inspect_ram_hogs, and run_system_troubleshooting.
"""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jarvis.skills.dev_shell  # noqa: F401 - ensures list_directory is registered
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    IngestionTrustClassification,
    TurnTaintTracker,
)
from jarvis.skills.diagnostics import (
    profile_runtime,
    run_system_troubleshooting,
    verify_learned_habits,
)
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

        with patch("jarvis.skills.diagnostics._get_reflex_engine", return_value=mock_engine):
            report = verify_learned_habits()
            self.assertIn("open user reports", report)
            self.assertTrue(self.tracker.is_tainted, "verify_learned_habits must taint turn when habits are returned!")
            self.assertIn("persistent_memory:learned_habits", self.tracker.tainting_sources)

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
