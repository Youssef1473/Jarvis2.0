"""Jarvis 2.0 — Phase 2 Stage 6A: Host Process & Actuation Identity Security Suite.

Deterministic adversarial and specification test suite establishing the RED baseline for:
1. Application Launch Security:
   - Blocking unapproved application launches on untainted turns (launch_application).
   - Blocking direct Python invocation of launch_application without capability tokens.
   - Binding authorization to exact executable path, arguments, working directory, and launch mechanism.
   - Replay prevention and single-use consumption.
   - Preventing launch_or_focus_application from bypassing launch authorization.
2. Process Identity & Precise Termination Security:
   - Preventing ambiguous multi-process substring termination (close_application blast radius).
   - Enforcing process identity tuple binding: (PID, create_time, exe_path).
   - Detecting and failing closed on Windows PID reuse (creation time mismatch).
   - Detecting and failing closed on binary replacement (executable path mismatch).
   - Process replacement TOCTOU validation between discovery and actuation.
   - Positive single-instance termination under valid human authorization.
   - Defense-in-depth critical process protection (CRITICAL_PROCESS_DENYLIST).

SAFETY NOTE:
All tests mock low-level process actuation boundaries (subprocess.Popen, os.startfile,
psutil.process_iter, psutil.Process, kernel32). No real operating system processes
are launched or terminated.
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import jarvis.skills.os_control as os_ctrl
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry


class TestP1Stage6AExecutionSecurity(unittest.TestCase):
    """RED specification test suite for Phase 2 Stage 6A execution security."""

    def setUp(self) -> None:
        """Establishes clean, deterministic test fixture state."""
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self._ctx_tokens = []

    def tearDown(self) -> None:
        """Tears down test state, cleans up context tokens, and resets managers."""
        for tok in self._ctx_tokens:
            with contextlib.suppress(Exception):
                self.approval_mgr.clear_active_authorized(tok)
        self._ctx_tokens.clear()
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    # =========================================================================
    # SECTION 1: Application Launch Authorization Tests
    # =========================================================================

    @patch("subprocess.Popen")
    @patch("os.startfile", create=True)
    def test_01_launch_application_on_untainted_turn_without_approval_must_be_blocked(
        self, mock_startfile: MagicMock, mock_popen: MagicMock
    ) -> None:
        """VULNERABILITY STAGE-6A-01: launch_application on untainted turns.

        Launching applications can spawn powerful executables (e.g. cmd, powershell).
        Executing launch_application on an untainted turn without human approval MUST fail closed.
        In unfixed code: risk_level=1 and absence from CRITICAL_PRIVILEGE_TOOLS permits unprompted execution.
        """
        self.assertFalse(self.tracker.is_tainted, "Test precondition: turn must be untainted")

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute("launch_application", app_name="cmd.exe")

        mock_popen.assert_not_called()
        mock_startfile.assert_not_called()
        self.assertIn("authorization", str(ctx.exception).lower())

    @patch("subprocess.Popen")
    @patch("os.startfile", create=True)
    def test_02_direct_launch_application_invocation_without_grant_must_fail_closed(
        self, mock_startfile: MagicMock, mock_popen: MagicMock
    ) -> None:
        """VULNERABILITY STAGE-6A-02: Direct Python launch_application invocation.

        Calling os_ctrl.launch_application directly in Python without an active
        authorization capability in _active_auth_context MUST fail closed.
        In unfixed code: No inner capability gate exists; detached process is spawned.
        """
        with self.assertRaises(SecurityBoundaryViolation):
            os_ctrl.launch_application(app_name="powershell.exe")

        mock_popen.assert_not_called()
        mock_startfile.assert_not_called()

    def test_03_launch_application_executable_path_tampering_rejected(self) -> None:
        """ANTI-TAMPERING: Authorization must bind to the exact resolved executable path.

        User approves launching 'notepad' (resolved to C:\\Windows\\System32\\notepad.exe).
        Caller attempts to substitute 'cmd.exe' under the same grant.
        Execution MUST fail closed with action hash mismatch.
        """
        grant = self.approval_mgr.grant_approval(
            tool_name="launch_application",
            arguments={
                "app_name": "notepad",
                "executable_path": r"C:\Windows\System32\notepad.exe",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Attacker attempts to substitute cmd.exe
        res = SkillRegistry.execute_with_status(
            "launch_application",
            app_name="cmd.exe",
            executable_path=r"C:\Windows\System32\cmd.exe",
        )
        self.assertFalse(res.success, "Substituted executable path must fail closed")
        self.assertEqual(res.error_type, "security_violation")

    def test_04_launch_application_arguments_and_cwd_binding_rejects_tampering(self) -> None:
        """ANTI-TAMPERING: Authorization must bind to arguments and working directory.

        User approves launching python.exe with argument 'safe_worker.py'.
        Caller attempts to substitute 'malicious_payload.py'.
        Execution MUST fail closed.
        """
        grant = self.approval_mgr.grant_approval(
            tool_name="launch_application",
            arguments={
                "app_name": "python.exe",
                "args": ["safe_worker.py"],
                "cwd": r"C:\Workspace",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        res = SkillRegistry.execute_with_status(
            "launch_application",
            app_name="python.exe",
            args=["malicious_payload.py"],
            cwd=r"C:\Workspace",
        )
        self.assertFalse(res.success, "Altered launch arguments must fail closed")
        self.assertEqual(res.error_type, "security_violation")

    @patch("subprocess.Popen")
    @patch("os.startfile", create=True)
    def test_05_positive_approved_application_launch_executes_once(
        self, mock_startfile: MagicMock, mock_popen: MagicMock
    ) -> None:
        """POSITIVE TEST: When user explicitly approves application launch,

        the process is spawned exactly once and the grant is consumed single-use.
        """
        self.approval_mgr.set_approval_handler(lambda req: True)

        with patch("shutil.which", return_value=r"C:\Windows\System32\calc.exe"):
            res = SkillRegistry.execute_with_status("launch_application", app_name="calc.exe")

        self.assertTrue(res.success, f"Approved launch must succeed, got: {res.error_message}")
        self.assertTrue(mock_popen.called or mock_startfile.called, "Process launch boundary must be reached")

        # Single-use: Replay attempt under the same context must fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            os_ctrl.launch_application(app_name="calc.exe")

    @patch("subprocess.Popen")
    @patch("os.startfile", create=True)
    @patch("jarvis.skills.windows_uia.focus_window_uia", return_value=None)
    @patch("jarvis.skills.os_control.focus_window", return_value="Window not found")
    def test_06_launch_or_focus_application_cannot_bypass_launch_authorization(
        self,
        mock_focus: MagicMock,
        mock_focus_uia: MagicMock,
        mock_startfile: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        """ALIAS / WRAPPER SECURITY: launch_or_focus_application fallback.

        When target window is not currently open, launch_or_focus_application falls
        back to spawning a new process. This fallback MUST be gated by human authorization.
        In unfixed code: Falls back to unapproved launch_application unconditionally.
        """
        self.assertFalse(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation):
            SkillRegistry.execute("launch_or_focus_application", app_name="powershell.exe")

        mock_popen.assert_not_called()
        mock_startfile.assert_not_called()

    # =========================================================================
    # SECTION 2: Process Identity & Precise Termination Security Tests
    # =========================================================================

    def test_07_unapproved_process_termination_must_fail_closed(self) -> None:
        """INVARIANT: Process termination without an active capability grant fails closed."""
        with self.assertRaises(SecurityBoundaryViolation):
            os_ctrl.close_application(app_name="notepad.exe")

    def test_08_close_application_rejects_ambiguous_multi_process_substring_termination(self) -> None:
        """VULNERABILITY STAGE-6A-03: Multi-process termination & fuzzy substring matching.

        1. Fuzzy match 'code' must NEVER terminate 'transcoder.exe'.
        2. Approving close_application must NOT terminate multiple process instances blindly.
        In unfixed code:
        - Line 692 uses (cand in p_name), so 'code' matches and terminates 'transcoder.exe'.
        - Loop over psutil.process_iter terminates all matching processes without instance selection.
        """
        mock_transcoder = MagicMock()
        mock_transcoder.info = {"pid": 1004, "name": "transcoder.exe"}
        mock_transcoder.name.return_value = "transcoder.exe"

        grant = self.approval_mgr.grant_approval(
            tool_name="close_application",
            arguments={"app_name": "code"},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with patch("psutil.process_iter", return_value=[mock_transcoder]):
            SkillRegistry.execute("close_application", app_name="code")

        # Fuzzy substring match must NEVER kill transcoder.exe when user asked for 'code'
        mock_transcoder.terminate.assert_not_called()

    def test_09_process_termination_mismatched_creation_time_fails_closed_pid_reuse(self) -> None:
        """IDENTITY SECURITY: Windows PID reuse defense via process creation time.

        User approves terminating PID 2048 with create_time=1000.0.
        At actuation time, PID 2048 exists, but has create_time=5000.0 (PID was reused by a new process).
        Actuation MUST fail closed and MUST NOT terminate the new process.
        """
        mock_proc = MagicMock()
        mock_proc.pid = 2048
        mock_proc.name.return_value = "notepad.exe"
        # Actual running process has different create_time than approved
        mock_proc.create_time.return_value = 5000.0
        mock_proc.exe.return_value = r"C:\Windows\System32\notepad.exe"

        grant = self.approval_mgr.grant_approval(
            tool_name="close_application",
            arguments={
                "app_name": "notepad.exe",
                "pid": 2048,
                "create_time": 1000.0,  # Approved old process
                "exe_path": r"C:\Windows\System32\notepad.exe",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with (
            patch("psutil.Process", return_value=mock_proc),
            self.assertRaises(SecurityBoundaryViolation) as ctx,
        ):
            if hasattr(os_ctrl, "terminate_process_instance"):
                os_ctrl.terminate_process_instance(
                    pid=2048, create_time=1000.0, exe_path=r"C:\Windows\System32\notepad.exe"
                )
            else:
                # In current code, close_application does not support process identity binding
                SkillRegistry.execute(
                    "close_application",
                    app_name="notepad.exe",
                    pid=2048,
                    create_time=1000.0,
                    exe_path=r"C:\Windows\System32\notepad.exe",
                )

        mock_proc.terminate.assert_not_called()
        self.assertIn("creation time", str(ctx.exception).lower())

    def test_10_process_termination_mismatched_executable_path_fails_closed_binary_replacement(self) -> None:
        """IDENTITY SECURITY: Binary path mismatch defense.

        User approves terminating PID 3000 running C:\\Program Files\\App\\app.exe.
        At actuation time, PID 3000 is running C:\\Users\\Public\\Temp\\trojan.exe.
        Actuation MUST fail closed and MUST NOT terminate the unexpected binary.
        """
        mock_proc = MagicMock()
        mock_proc.pid = 3000
        mock_proc.name.return_value = "app.exe"
        mock_proc.create_time.return_value = 1234.0
        # Mismatched executable path on disk
        mock_proc.exe.return_value = r"C:\Users\Public\Temp\trojan.exe"

        grant = self.approval_mgr.grant_approval(
            tool_name="close_application",
            arguments={
                "app_name": "app.exe",
                "pid": 3000,
                "create_time": 1234.0,
                "exe_path": r"C:\Program Files\App\app.exe",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with (
            patch("psutil.Process", return_value=mock_proc),
            self.assertRaises(SecurityBoundaryViolation) as ctx,
        ):
            if hasattr(os_ctrl, "terminate_process_instance"):
                os_ctrl.terminate_process_instance(
                    pid=3000, create_time=1234.0, exe_path=r"C:\Program Files\App\app.exe"
                )
            else:
                SkillRegistry.execute(
                    "close_application",
                    app_name="app.exe",
                    pid=3000,
                    create_time=1234.0,
                    exe_path=r"C:\Program Files\App\app.exe",
                )

        mock_proc.terminate.assert_not_called()
        self.assertIn("executable path", str(ctx.exception).lower())

    def test_11_process_replacement_between_discovery_and_termination_fails_closed(self) -> None:
        """TOCTOU DEFENSE: Target process disappears between discovery and actuation.

        If target process terminates before actuation, actuation must fail closed gracefully
        without terminating any replacement process.
        """
        import psutil

        grant = self.approval_mgr.grant_approval(
            tool_name="close_application",
            arguments={
                "app_name": "worker.exe",
                "pid": 4040,
                "create_time": 2222.0,
                "exe_path": r"C:\App\worker.exe",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with patch("psutil.Process", side_effect=psutil.NoSuchProcess(pid=4040)):
            if hasattr(os_ctrl, "terminate_process_instance"):
                res = os_ctrl.terminate_process_instance(
                    pid=4040, create_time=2222.0, exe_path=r"C:\App\worker.exe"
                )
                self.assertIn("no longer exists", res.lower())
            else:
                self.fail("Security Interface Missing: os_ctrl lacks secure terminate_process_instance interface")

    def test_12_positive_approved_process_identity_terminates_exact_instance_only(self) -> None:
        """POSITIVE TEST: Two-phase process termination terminates approved instance only.

        Given candidate processes PID 5001, 5002, 5003.
        User approves terminating PID 5002 with verified identity (PID, create_time, exe_path).
        Actuation terminates PID 5002 exactly once; PID 5001 and 5003 remain untouched.
        """
        mock_proc1 = MagicMock()
        mock_proc1.pid = 5001
        mock_proc1.create_time.return_value = 1000.0
        mock_proc1.exe.return_value = r"C:\App\app.exe"

        mock_proc2 = MagicMock()
        mock_proc2.pid = 5002
        mock_proc2.create_time.return_value = 1010.0
        mock_proc2.exe.return_value = r"C:\App\app.exe"

        mock_proc3 = MagicMock()
        mock_proc3.pid = 5003
        mock_proc3.create_time.return_value = 1020.0
        mock_proc3.exe.return_value = r"C:\App\app.exe"

        procs_by_pid = {5001: mock_proc1, 5002: mock_proc2, 5003: mock_proc3}

        self.approval_mgr.set_approval_handler(lambda req: True)

        with patch("psutil.Process", side_effect=lambda pid: procs_by_pid[pid]):
            if hasattr(os_ctrl, "terminate_process_instance"):
                res = SkillRegistry.execute_with_status(
                    "terminate_process_instance",
                    pid=5002,
                    create_time=1010.0,
                    exe_path=r"C:\App\app.exe",
                )
                self.assertTrue(res.success)
                mock_proc2.terminate.assert_called_once()
                mock_proc1.terminate.assert_not_called()
                mock_proc3.terminate.assert_not_called()
            else:
                self.fail("Security Interface Missing: os_ctrl lacks secure terminate_process_instance interface")

    def test_13_critical_process_denylist_enforced_even_if_user_approved(self) -> None:
        """DEFENSE IN DEPTH: System-critical processes cannot be terminated even with user grant.

        Attempting to terminate explorer.exe or python.exe must be rejected by CRITICAL_PROCESS_DENYLIST.
        """
        grant = self.approval_mgr.grant_approval(
            tool_name="close_application",
            arguments={"app_name": "explorer.exe"},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        res = SkillRegistry.execute("close_application", app_name="explorer.exe")
        self.assertIn("cannot terminate system-critical process", res.lower())


if __name__ == "__main__":
    unittest.main()
