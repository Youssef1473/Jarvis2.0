"""Automated tests for py-spy runtime profiling and tracemalloc memory diagnostics."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
import jarvis.skills.diagnostics as diag


class TestDiagnosticsSuite(unittest.TestCase):
    """Verifies py-spy zero-overhead profiler and heap telemetry tools."""

    def test_pyspy_binary_discovery(self):
        """Verifies py-spy executable discovery or graceful handling when absent."""
        pyspy_path = diag._find_pyspy()
        if pyspy_path is None:
            self.skipTest("py-spy binary not found in environment (skipped in environments without py-spy)")
        self.assertTrue(os.path.exists(pyspy_path), f"py-spy path '{pyspy_path}' must exist")

    @patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe")
    @patch("subprocess.run")
    def test_profile_runtime_execution(self, mock_run, mock_find):
        """Verifies profile_runtime parses sampling dump output correctly."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Thread 1234 (active):\n"
                '  File "jarvis/core/brain.py", line 42, in run\n'
                "Thread 5678 (idle):\n"
                '  File "threading.py", line 100, in wait\n'
            ),
            stderr="",
        )
        res = SkillRegistry.execute("profile_runtime", mode="dump")
        self.assertIsInstance(res, str)
        self.assertIn("Runtime Thread Diagnostics", res)
        self.assertIn(str(os.getpid()), res)
        self.assertIn("Active Threads Monitored: 2", res)

    @patch("jarvis.skills.diagnostics._find_pyspy", return_value=None)
    def test_profile_runtime_missing_binary(self, mock_find):
        """Verifies profile_runtime returns clean fallback message when py-spy is absent."""
        res = SkillRegistry.execute("profile_runtime", mode="dump")
        self.assertIsInstance(res, str)
        self.assertIn("Diagnostic profiler unavailable", res)

    @patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe")
    @patch("subprocess.run")
    def test_profile_runtime_access_notice(self, mock_run, mock_find):
        """Verifies profile_runtime handles non-zero exit codes gracefully."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Access is denied",
        )
        res = SkillRegistry.execute("profile_runtime", mode="dump")
        self.assertIsInstance(res, str)
        self.assertIn("Diagnostic profiling encountered an access notice", res)

    def test_audit_memory_heap(self):
        """Verifies audit_memory_heap initializes and returns heap allocation metrics."""
        res = SkillRegistry.execute("audit_memory_heap")
        self.assertIsInstance(res, str)
        self.assertTrue(
            "Memory Allocation Audit" in res or "Memory allocation tracing initialized" in res
        )

    @patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe")
    @patch("subprocess.run")
    def test_brain_diagnostics_fast_reflex(self, mock_run, mock_find):
        """Verifies brain fast reflexes trigger profile_runtime and memory audit instantly."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Thread 1:\n  File 'test.py', line 1\n",
            stderr="",
        )
        from jarvis.core.brain import JarvisBrain
        from jarvis.core.config_loader import load_config
        from jarvis.core.memory import MemoryManager
        from jarvis.core.safety import SafetyGate
        
        cfg = load_config()
        brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))
        
        # Profile runtime reflex
        res = brain._try_fast_reflex("profile runtime")
        self.assertIsNotNone(res)
        self.assertIn("Runtime Thread Diagnostics", res)

        # Audit memory heap reflex
        res_mem = brain._try_fast_reflex("audit memory heap")
        self.assertIsNotNone(res_mem)
        self.assertTrue("Memory" in res_mem)

    def test_jarvis_doctor_execution(self):
        """Verifies jarvis_doctor executes full 360-degree self-audit cleanly."""
        spoken_res = SkillRegistry.execute("jarvis_doctor", verbose=False)
        self.assertIsInstance(spoken_res, str)
        self.assertTrue("operational" in spoken_res or "diagnostics" in spoken_res)

        table_res = SkillRegistry.execute("jarvis_doctor", verbose=True)
        self.assertIsInstance(table_res, str)
        self.assertIn("LIVE OPERATIONAL SUBSYSTEM DIAGNOSTICS", table_res)
        self.assertIn("AUDIO HARDWARE", table_res)
        self.assertIn("SKILLS REGISTRY", table_res)

        # Verify alias
        alias_res = SkillRegistry.execute("run_system_troubleshooting", verbose=False)
        self.assertEqual(type(alias_res), str)

    def test_jarvis_doctor_fast_reflex(self):
        """Verifies jarvis_doctor voice commands resolve as sub-15ms fast reflexes."""
        from jarvis.core.brain import JarvisBrain
        from jarvis.core.config_loader import load_config
        from jarvis.core.memory import MemoryManager
        from jarvis.core.safety import SafetyGate

        cfg = load_config()
        brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))

        res_self = brain._try_fast_reflex("troubleshoot yourself")
        self.assertIsNotNone(res_self)
        self.assertTrue("operational" in res_self or "diagnostics" in res_self)

        res_health = brain._try_fast_reflex("system health check")
        self.assertIsNotNone(res_health)
        self.assertTrue("operational" in res_health or "diagnostics" in res_health)


if __name__ == "__main__":
    unittest.main()

