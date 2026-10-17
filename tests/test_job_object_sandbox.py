"""Unit tests for jarvis/skills/job_object_sandbox.py."""
import sys
import unittest

from jarvis.skills.base import SkillRegistry
from jarvis.skills.job_object_sandbox import (
    run_job_sandboxed_process,
    _resolve_safe_affinity_mask,
)


class TestJobObjectSandbox(unittest.TestCase):
    def test_run_job_sandboxed_process_success(self):
        res = run_job_sandboxed_process(
            command=[sys.executable, "-c", "print('hello_sandbox_kernel')"],
            timeout_sec=5.0,
        )
        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("exit_code"), 0)
        self.assertIn("hello_sandbox_kernel", res.get("stdout_tail", ""))
        self.assertGreaterEqual(res.get("peak_job_memory_mb", 0), 0)

    def test_run_job_sandboxed_process_timeout(self):
        # Verify timeout termination and clean pipe closure
        res = run_job_sandboxed_process(
            command=[sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_sec=0.2,
        )
        self.assertEqual(res.get("status"), "timeout")
        self.assertIn("Process tree terminated", res.get("stderr_tail", ""))

    def test_dynamic_affinity_mask(self):
        mask = _resolve_safe_affinity_mask(0xFF00)
        self.assertIsNotNone(mask)
        self.assertIsInstance(mask, int)
        self.assertGreater(mask, 0)

    def test_empty_command_rejection(self):
        res = run_job_sandboxed_process(command=[])
        self.assertEqual(res.get("status"), "error")

    def test_registry_registration(self):
        tool = SkillRegistry.get_tool("run_job_sandboxed_process")
        self.assertIsNotNone(tool)
        self.assertEqual(SkillRegistry.get_risk_level("run_job_sandboxed_process"), 1)

    def test_execute_sandboxed_powershell(self):
        from jarvis.skills.dev_shell import execute_sandboxed_powershell
        out = execute_sandboxed_powershell("Write-Output 'sandboxed_powershell_ok'")
        self.assertIn("sandboxed_powershell_ok", out)
        self.assertIn("Job Object", out)


if __name__ == "__main__":
    unittest.main()
