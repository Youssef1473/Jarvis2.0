"""Unit and integration regression tests for CORE-SEC-04H residual security boundaries.

Verifies:
1. SEC-04H-01: UnifiedRunner routes tools through ToolOrchestrator and DualKeyApprovalManager.
2. SEC-04H-02: Canary execution spawns suspended on Windows, assigns before execution, and fails closed.
3. SEC-04H-03: POSIX test runner fails closed on containment failure without bare subprocess.run fallback.
4. SEC-04H-04: CDP session cleanup is wired to orchestrator shutdown and atexit idempotently.
5. SEC-04H-05: py-spy and es.exe run under Job Object containment via run_sandboxed_batch.
"""
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from jarvis.core.evolution.sandbox import SkillSandbox, Win32JobObject
from jarvis.core.orchestrator import JarvisOrchestrator
from jarvis.core.providers.base import (
    BaseProvider,
    LLMResponse,
    ToolCall,
    UnifiedRunner,
)
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    TurnTaintTracker,
)
from jarvis.skills.browser_cdp import (
    _ACTIVE_TAB_SESSIONS,
    CDPTabSession,
    clear_all_tab_sessions,
    register_cdp_atexit_cleanup,
    register_tab_session,
)
from jarvis.skills.dev_shell import run_project_tests
from jarvis.skills.diagnostics import profile_runtime
from jarvis.skills.everything_search import search_everything


class MockProvider(BaseProvider):
    """Mock LLM provider returning programmed turns."""

    def __init__(self, turns: list[LLMResponse]):
        super().__init__(name="mock_provider", model="mock-v1")
        self.turns = turns
        self.turn_index = 0

    def generate_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> LLMResponse:
        if self.turn_index < len(self.turns):
            res = self.turns[self.turn_index]
            self.turn_index += 1
            if sentence_callback and res.content:
                sentence_callback(res.content)
            return res
        return LLMResponse(content="Done.")


class TestResidualSecurityBoundaries(unittest.TestCase):
    """Comprehensive regression tests for SEC-04H boundaries."""

    def setUp(self):
        self.taint_tracker = TurnTaintTracker.get_instance()
        self.taint_tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        clear_all_tab_sessions()

    def tearDown(self):
        self.taint_tracker.reset_turn()
        self.approval_mgr.reset()
        clear_all_tab_sessions()

    # =========================================================================
    # 1. SEC-04H-01: UnifiedRunner -> ToolOrchestrator Integration
    # =========================================================================

    def test_unified_runner_blocks_mutating_tool_on_tainted_turn_without_approval(self):
        """Mutating tools executed via UnifiedRunner MUST fail closed when turn is tainted."""
        # Mark turn tainted from an untrusted web ingestion source
        self.taint_tracker.mark_tainted("web_search", {"query": "malicious prompt injection"})
        self.assertTrue(self.taint_tracker.is_tainted)

        # Program mock provider to call write_file (high-privilege mutation tool)
        write_call = ToolCall(
            id="call_write_1",
            name="write_file",
            arguments={"file_path": "test_probe_unauthorized.txt", "content": "forbidden", "overwrite": True},
        )
        turn_1 = LLMResponse(content="Writing file", tool_calls=[write_call])
        turn_2 = LLMResponse(content="Finished", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        _reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "write something"}],
        )

        # Inspect tool result in history
        tool_entry = next((m for m in history if m.get("role") == "tool"), None)
        self.assertIsNotNone(tool_entry, "Expected tool entry in conversation history")
        self.assertIn("Dual-Key Safety Gate", tool_entry["content"])
        self.assertIn("blocked high-privilege tool 'write_file'", tool_entry["content"])

        # Confirm the file was NEVER written to disk
        self.assertFalse(Path("test_probe_unauthorized.txt").exists())

    def test_unified_runner_allows_mutating_tool_with_pre_granted_approval(self):
        """Mutating tools execute successfully when a valid out-of-band grant exists."""
        self.taint_tracker.mark_tainted("web_search", {"query": "prompt injection"})
        target_path = "test_probe_authorized.txt"
        clean_args = {"content": "approved payload", "file_path": target_path, "overwrite": True}

        # Issue genuine human approval grant bound to exact tool and arguments
        grant = self.approval_mgr.grant_approval("write_file", clean_args)
        self.assertNotEqual(grant.status, "consumed")

        write_call = ToolCall(
            id="call_write_auth",
            name="write_file",
            arguments=dict(clean_args),
        )
        turn_1 = LLMResponse(content="Writing authorized file", tool_calls=[write_call])
        turn_2 = LLMResponse(content="Finished writing", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        try:
            _reply, history = runner.execute_session(
                provider=provider,
                messages=[{"role": "user", "content": "write authorized file"}],
            )

            tool_entry = next((m for m in history if m.get("role") == "tool"), None)
            self.assertIsNotNone(tool_entry)
            self.assertIn("Successfully wrote", tool_entry["content"])
            # Target file was written under the active grant
            self.assertTrue(Path(target_path).exists(), "Target file must be written when approved")
            # Grant lifecycle must transition out of pending/active to terminal status
            self.assertIn(grant.status, ("consumed", "invalidated"))
        finally:
            if Path(target_path).exists():
                Path(target_path).unlink()
            bak = Path(f"{target_path}.bak")
            if bak.exists():
                bak.unlink()

    def test_unified_runner_untainted_safe_tools_execute_without_approval(self):
        """Read-only tools on untainted turns execute cleanly through ToolOrchestrator."""
        self.assertFalse(self.taint_tracker.is_tainted)

        time_call = ToolCall(id="call_time", name="get_current_time_and_date", arguments={})
        turn_1 = LLMResponse(content="Checking time", tool_calls=[time_call])
        turn_2 = LLMResponse(content="Reported time", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        _reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "what time is it"}],
        )

        tool_entry = next((m for m in history if m.get("role") == "tool"), None)
        self.assertIsNotNone(tool_entry)
        self.assertNotIn("Error", tool_entry["content"])

    def test_unified_runner_uses_provided_tool_orchestrator(self):
        """UnifiedRunner accepts an explicit ToolOrchestrator instance."""
        mock_orch = MagicMock()
        mock_orch.execute_step.return_value = "mocked orchestrator response"

        runner = UnifiedRunner(tool_orchestrator=mock_orch)
        res = runner.execute_tool_with_timeout("custom_tool", {"arg": 123})

        self.assertEqual(res, "mocked orchestrator response")
        mock_orch.execute_step.assert_called_once_with("custom_tool", {"arg": 123}, step_index=1, dual_key_confirmed=False)

    # =========================================================================
    # 2. SEC-04H-02: Canary Execution Suspended Spawn & Fail-Closed
    # =========================================================================

    def test_canary_spawns_suspended_on_windows(self):
        """On Windows, canary subprocess MUST be spawned with CREATE_SUSPENDED."""
        clean_name = "test_probe_canary_suspended"
        valid_code = (
            "from jarvis.skills.base import SkillRegistry\n\n"
            "@SkillRegistry.register(name='test_probe_canary_suspended', description='test')\n"
            "def test_probe_canary_suspended():\n"
            "    return 'ok'\n"
        )
        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen") as mock_popen, \
             patch("jarvis.core.evolution.sandbox.Win32JobObject.assign_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True):

            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("CANARY_VERIFICATION_SUCCESS\nSANDBOX_LOAD_SUCCESS", "")
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            from jarvis.skills.job_object_sandbox import CREATE_SUSPENDED
            _res = SkillSandbox.validate_and_promote(clean_name, valid_code)

            self.assertTrue(mock_popen.called)
            called_flags = mock_popen.call_args[1].get("creationflags", 0)
            self.assertTrue(
                bool(called_flags & CREATE_SUSPENDED),
                "FAIL: Canary process was NOT spawned with CREATE_SUSPENDED!",
            )

    def test_canary_assignment_failure_terminates_child_and_fails_closed(self):
        """If Job Object assignment fails, child must be killed immediately without execution."""
        clean_name = "test_probe_canary_assign_fail"
        valid_code = (
            "from jarvis.skills.base import SkillRegistry\n\n"
            "@SkillRegistry.register(name='test_probe_canary_assign_fail', description='test')\n"
            "def test_probe_canary_assign_fail():\n"
            "    return 'ok'\n"
        )
        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen") as mock_popen, \
             patch("jarvis.core.evolution.sandbox.Win32JobObject.assign_process", return_value=False):

            mock_proc = MagicMock()
            mock_popen.return_value = mock_proc

            res = SkillSandbox.validate_and_promote(clean_name, valid_code)

            self.assertFalse(res.success)
            self.assertIn("Security failure: Win32 Job Object assignment failed", res.message)
            self.assertTrue(mock_proc.kill.called, "Unassigned suspended child was not killed!")

    def test_canary_resumption_failure_terminates_child_and_fails_closed(self):
        """If process resumption fails, child must be killed immediately and fail closed."""
        clean_name = "test_probe_canary_resume_fail"
        valid_code = (
            "from jarvis.skills.base import SkillRegistry\n\n"
            "@SkillRegistry.register(name='test_probe_canary_resume_fail', description='test')\n"
            "def test_probe_canary_resume_fail():\n"
            "    return 'ok'\n"
        )
        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen") as mock_popen, \
             patch("jarvis.core.evolution.sandbox.Win32JobObject.assign_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=False):

            mock_proc = MagicMock()
            mock_popen.return_value = mock_proc

            res = SkillSandbox.validate_and_promote(clean_name, valid_code)

            self.assertFalse(res.success)
            self.assertIn("Failed to resume suspended canary process", res.message)
            self.assertTrue(mock_proc.kill.called)

    def test_canary_preserves_resource_limits(self):
        """Win32JobObject in sandbox preserves 4-process and 1 GiB limits."""
        with patch("sys.platform", "win32"):
            job = Win32JobObject(max_processes=4, max_memory_bytes=1024 * 1024 * 1024)
            self.assertEqual(job.max_processes, 4)
            self.assertEqual(job.max_memory_bytes, 1024 * 1024 * 1024)
            job.close()

    # =========================================================================
    # 3. SEC-04H-03: POSIX Test-Execution Fallback Fails Closed
    # =========================================================================

    @patch("sys.platform", "linux")
    def test_run_project_tests_contained_fallback_failure_fails_closed_on_posix(self):
        """When run_sandboxed_batch raises on POSIX, run_project_tests fails closed without bare subprocess.run."""
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value={"status": "error", "reason": "Non-windows host"}), \
             patch("jarvis.skills.dev_shell.run_sandboxed_batch", side_effect=RuntimeError("POSIX containment error")), \
             patch("subprocess.run") as mock_bare_run:
            out = run_project_tests("test_dummy.py")
            self.assertFalse(mock_bare_run.called, "Bare subprocess.run was invoked on POSIX fallback!")
            self.assertIn("Error: Contained fallback execution failed on POSIX", out)
            self.assertIn("Uncontained execution is strictly prohibited", out)

    # =========================================================================
    # 4. SEC-04H-04: CDP Session Teardown on Shutdown & Idempotence
    # =========================================================================

    def test_clear_all_tab_sessions_idempotent(self):
        """Calling clear_all_tab_sessions multiple times closes sessions safely without raising."""
        mock_ws = MagicMock()
        mock_session = CDPTabSession(mock_ws, "tab_test_idempotent")

        register_tab_session("tab_test_idempotent", mock_session)
        self.assertEqual(len(_ACTIVE_TAB_SESSIONS), 1)

        # First clear
        clear_all_tab_sessions()
        self.assertEqual(len(_ACTIVE_TAB_SESSIONS), 0)
        self.assertTrue(mock_session._closed)

        # Second clear (must be completely idempotent and error-free)
        clear_all_tab_sessions()
        self.assertEqual(len(_ACTIVE_TAB_SESSIONS), 0)

    def test_register_cdp_atexit_cleanup_idempotent(self):
        """register_cdp_atexit_cleanup registers with atexit only once."""
        with patch("atexit.register") as mock_atexit,              patch("jarvis.skills.browser_cdp._CDP_ATEXIT_REGISTERED", False):
            register_cdp_atexit_cleanup()
            register_cdp_atexit_cleanup()
            self.assertEqual(mock_atexit.call_count, 1)

    def test_orchestrator_stop_invokes_cdp_cleanup(self):
        """JarvisOrchestrator.stop() invokes clear_all_tab_sessions() cleanly."""
        orch = JarvisOrchestrator(config={})
        orch._running = True
        orch._stopped = False

        with patch("jarvis.skills.browser_cdp.clear_all_tab_sessions") as mock_cdp_clear, \
             patch.object(orch, "service_manager"):
            orch.stop()
            self.assertTrue(mock_cdp_clear.called, "Orchestrator.stop() did not invoke clear_all_tab_sessions!")

    # =========================================================================
    # 5. SEC-04H-05: CLI Containment (py-spy and es.exe)
    # =========================================================================

    def test_profile_runtime_dispatches_via_run_sandboxed_batch(self):
        """profile_runtime MUST execute py-spy via run_sandboxed_batch with timeout=8.0."""
        mock_res = subprocess.CompletedProcess(
            args=["py-spy", "dump", "--pid", "1234"],
            returncode=0,
            stdout="Thread 1 (0x123): idle\nFile 'main.py'\n",
            stderr="",
        )
        with patch("jarvis.skills.diagnostics._find_pyspy", return_value="py-spy.exe"), \
             patch("jarvis.skills.job_object_sandbox.run_sandboxed_batch", return_value=mock_res) as mock_batch:
            out = profile_runtime(mode="dump", target_pid=1234)

            self.assertTrue(mock_batch.called, "py-spy was NOT executed via run_sandboxed_batch!")
            call_kwargs = mock_batch.call_args[1]
            self.assertEqual(call_kwargs.get("timeout"), 8.0)
            self.assertIn("Runtime Thread Diagnostics", out)

    def test_search_everything_dispatches_via_run_sandboxed_batch(self):
        """search_everything MUST execute es.exe via run_sandboxed_batch with timeout=5.0."""
        mock_proc = subprocess.CompletedProcess(
            args=["es.exe", "query"],
            returncode=0,
            stdout="C:\\Users\\Test\\file1.txt\nC:\\Users\\Test\\file2.txt\n",
            stderr="",
        )
        with patch("jarvis.skills.everything_search.find_es_executable", return_value="es.exe"), \
             patch("jarvis.skills.job_object_sandbox.run_sandboxed_batch", return_value=mock_proc) as mock_batch:
            out = search_everything(query="test_file", max_results=5)

            self.assertTrue(mock_batch.called, "es.exe was NOT executed via run_sandboxed_batch!")
            call_kwargs = mock_batch.call_args[1]
            self.assertEqual(call_kwargs.get("timeout"), 5.0)
            self.assertIn("Found 2 instant result(s) via Voidtools Everything", out)


if __name__ == "__main__":
    unittest.main()
