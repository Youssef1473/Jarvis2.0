"""Unit test suite for Stage 2: Unified Tool Dispatch Gateway & Shell SafetyGate.

Verifies:
1. SafetyGate blocks destructive commands on execute_sandboxed_powershell and run_job_sandboxed_process.
2. JarvisBrain._dispatch_tool_pipeline enforces SafetyGate, cancellation tokens,
   ToolOrchestrator execution, and narrative callbacks uniformly.
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.core.brain import JarvisBrain
from jarvis.core.safety import SafetyGate


class TestStage2DispatchGateway(unittest.TestCase):
    def setUp(self):
        self.config = {
            "safety": {"confirm_destructive": True},
            "system": {"workspace_dir": str(Path(__file__).parent.parent)},
        }
        self.safety = SafetyGate(self.config)

    def test_safety_gate_blocks_destructive_execute_sandboxed_powershell(self):
        """Proof: execute_sandboxed_powershell is guarded by SafetyGate dangerous-command rules."""
        safe, msg = self.safety.check_tool_call(
            "execute_sandboxed_powershell",
            {"command": "Remove-Item -Recurse -Force C:\\Windows"}
        )
        self.assertFalse(safe)
        self.assertIn("destructive-command rules", msg)

    def test_safety_gate_blocks_destructive_run_job_sandboxed_process(self):
        """Proof: run_job_sandboxed_process is guarded by SafetyGate even when command is a list."""
        safe, msg = self.safety.check_tool_call(
            "run_job_sandboxed_process",
            {"command": ["powershell", "-Command", "del -r -force *"]}
        )
        self.assertFalse(safe)
        self.assertIn("destructive-command rules", msg)

    def test_safety_gate_allows_safe_command(self):
        """Proof: Safe status query commands are allowed through SafetyGate."""
        safe, msg = self.safety.check_tool_call(
            "execute_sandboxed_powershell",
            {"command": "Get-Process"}
        )
        self.assertTrue(safe)
        self.assertEqual(msg, "")

    @patch.object(JarvisBrain, "__init__", lambda self, *args, **kwargs: None)
    def test_dispatch_tool_pipeline_halts_on_safety_violation(self):
        """Proof: _dispatch_tool_pipeline halts execution and notifies callback if SafetyGate trips."""
        brain = JarvisBrain()
        brain.safety = self.safety
        brain._active_cancel_token = None
        brain.logger = MagicMock()
        brain._narrate_tool_action = MagicMock(return_value="Narrating action...")

        callbacks = []
        def _cb(text):
            callbacks.append(text)

        result, ok = brain._dispatch_tool_pipeline(
            "execute_sandboxed_powershell",
            {"command": "format C:"},
            step_index=1,
            sentence_callback=_cb,
        )
        self.assertFalse(ok)
        self.assertIn("Action halted", result)
        self.assertTrue(any("Action halted" in c for c in callbacks))

    @patch.object(JarvisBrain, "__init__", lambda self, *args, **kwargs: None)
    def test_dispatch_tool_pipeline_executes_and_records(self):
        """Proof: _dispatch_tool_pipeline executes tool via orchestrator and records in memory."""
        brain = JarvisBrain()
        brain.safety = self.safety
        brain._active_cancel_token = None
        brain._narrate_tool_action = MagicMock(return_value="Executing test tool")
        brain._execute_tool_with_orchestrator = MagicMock(return_value="Execution successful")
        brain._record_tool_execution = MagicMock()

        narrations = []
        result, ok = brain._dispatch_tool_pipeline(
            "read_file",
            {"path": "test.txt"},
            step_index=2,
            sentence_callback=lambda s: narrations.append(s),
        )
        self.assertTrue(ok)
        self.assertEqual(result, "Execution successful")
        self.assertIn("Executing test tool", narrations)
        brain._execute_tool_with_orchestrator.assert_called_once_with("read_file", {"path": "test.txt"}, step_index=2)
        brain._record_tool_execution.assert_called_once_with("read_file", {"path": "test.txt"}, "Execution successful")

    @patch.object(JarvisBrain, "__init__", lambda self, *args, **kwargs: None)
    def test_dispatch_tool_pipeline_enforces_cancellation(self):
        """Proof: _dispatch_tool_pipeline raises when cancellation token is triggered."""
        brain = JarvisBrain()
        brain.safety = self.safety
        mock_token = MagicMock()
        mock_token.raise_if_cancelled.side_effect = RuntimeError("Execution preempted by voice barge-in")
        brain._active_cancel_token = mock_token
        brain._narrate_tool_action = MagicMock(return_value="Action")

        with self.assertRaises(RuntimeError) as ctx:
            brain._dispatch_tool_pipeline(
                "read_file",
                {"path": "test.txt"},
                step_index=1,
            )
        self.assertIn("preempted by voice barge-in", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
