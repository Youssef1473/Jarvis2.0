"""Jarvis 2.0 — Phase 2 Stage 6B Security Addendum: Identity, Context & Preimage Integrity.

Deterministic adversarial RED specification test suite probing four critical security gaps:
1. File-Lock Process Identity & Retained Handle (release_file_lock):
   - Rejection of reused PID with mismatched process creation timestamp (TOCTOU).
   - Rejection of substituted executable image on identical PID (Binary replacement).
   - Retained process handle binding via ProcessHandleRegistry to prevent kernel PID recycling.
2. Browser Execution Context & Navigation Binding (execute_browser_javascript):
   - Rejection of execution when target tab navigates or changes URL between approval and dispatch.
   - Rejection of execution when CDP execution context is replaced or frame navigates.
   - Rejection of execution when browser target session ID mismatches approved session.
3. File Mutation Preimage Integrity (write_file & patch_file):
   - Rejection of patch_file when destination file preimage digest was modified after approval.
   - Rejection of write_file overwrite when destination file preimage digest was modified after approval.
   - Rejection of write_file non-overwrite when file was unexpectedly created before actuation.
   - Rejection of file mutation when destination is substituted with a symlink.
4. Positive Workflows:
   - Legitimate execution succeeds when complete identity, context, and preimage match.

RESTRICTIONS & SAFETY INVARIANTS:
- All external processes, CDP WebSockets, and kernel terminations are mocked.
- Filesystem mutations are strictly confined to isolated scratch sandbox directories.
- No real user files, system processes, or live browser sessions are modified.
"""
from __future__ import annotations

import contextlib
import hashlib
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills import dev_shell
from jarvis.skills.base import SkillRegistry
from jarvis.skills.os_control import ProcessHandleRegistry


class TestP1Stage6BAddendumSecurity(unittest.TestCase):
    """RED specification and adversarial suite for Stage 6B Security Addendum."""

    def setUp(self) -> None:
        """Establishes clean, deterministic test fixture state."""
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self._ctx_tokens = []
        ProcessHandleRegistry.clear()

        # Isolated scratch directory for safe file mutation tests
        self.test_dir = REPO_ROOT / "data" / "test_stage6b_addendum_sandbox"
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """Tears down test state, cleans up context tokens, and deletes scratch files."""
        for tok in self._ctx_tokens:
            with contextlib.suppress(Exception):
                self.approval_mgr.clear_active_authorized(tok)
        self._ctx_tokens.clear()
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        ProcessHandleRegistry.clear()

        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # SECTION 1: File-Lock Process Identity & Retained Handle (release_file_lock)
    # =========================================================================

    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.TerminateProcess")
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.OpenProcess", return_value=1234)
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.CloseHandle")
    def test_01_release_file_lock_pid_reuse_different_creation_time_must_fail_closed(
        self, mock_close: MagicMock, mock_open: MagicMock, mock_term: MagicMock
    ) -> None:
        """GAP ADDENDUM-01: File-lock release must bind to process creation timestamp.

        User approves terminating holder PID 7001 (recorded create_time = 1000.0).
        Between approval and actuation, the original process terminates and Windows
        recycles PID 7001 to a brand-new process (create_time = 1050.0).
        At actuation time, the caller executes release_file_lock.
        The runtime MUST verify that the locking process's creation time matches the
        approved creation time. If mismatched, actuation MUST fail closed and MUST NOT terminate.
        In unfixed code: release_file_lock only matches integer PID, so it terminates the recycled PID.
        """
        grant = self.approval_mgr.grant_approval(
            tool_name="release_file_lock",
            arguments={
                "file_path": "busy.dat",
                "allow_kill": True,
                "target_pid": 7001,
                "create_time": 1000.0,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Inspection returns PID 7001, but the live process has create_time 1050.0 (recycled)
        recycled_holder = {
            "pid": 7001,
            "app_name": "worker.exe",
            "create_time": 1050.0,
            "image_path": "C:/apps/worker.exe",
        }
        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [recycled_holder]}):
            res = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="busy.dat",
                allow_kill=True,
                target_pid=7001,
                create_time=1000.0,
            )

        self.assertFalse(res.success, "Reused PID with different creation time must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_term.assert_not_called()

    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.TerminateProcess")
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.OpenProcess", return_value=1234)
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.CloseHandle")
    def test_02_release_file_lock_executable_image_changed_must_fail_closed(
        self, mock_close: MagicMock, mock_open: MagicMock, mock_term: MagicMock
    ) -> None:
        """GAP ADDENDUM-02: File-lock release must bind to executable image path.

        User approves terminating holder PID 7001 with exe_path 'C:/tools/notepad.exe'.
        At actuation time, PID 7001 is running a different binary 'C:/critical/database.exe'.
        Actuation MUST verify the live executable image matches the approved image path.
        If mismatched, actuation MUST fail closed with SecurityBoundaryViolation and NOT terminate.
        In unfixed code: release_file_lock does not compare executable image paths and terminates.
        """
        grant = self.approval_mgr.grant_approval(
            tool_name="release_file_lock",
            arguments={
                "file_path": "data.bin",
                "allow_kill": True,
                "target_pid": 7001,
                "exe_path": "C:/tools/notepad.exe",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Inspection returns PID 7001, but binary is database.exe
        substituted_binary_holder = {
            "pid": 7001,
            "app_name": "database.exe",
            "image_path": "C:/critical/database.exe",
        }
        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [substituted_binary_holder]}):
            res = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="data.bin",
                allow_kill=True,
                target_pid=7001,
                exe_path="C:/tools/notepad.exe",
            )

        self.assertFalse(res.success, "Substituted executable image must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_term.assert_not_called()

    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.TerminateProcess")
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.OpenProcess", return_value=1234)
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.CloseHandle")
    def test_03_release_file_lock_retained_handle_binding_and_invalidation(
        self, mock_close: MagicMock, mock_open: MagicMock, mock_term: MagicMock
    ) -> None:
        """GAP ADDENDUM-03: File-lock release should integrate with ProcessHandleRegistry.

        Stage 6A uses ProcessHandleRegistry to hold a process handle across the approval
        window so the Windows kernel guarantees the PID cannot be recycled.
        If release_file_lock is invoked with an expired or mismatched handle_id,
        it MUST fail closed with SecurityBoundaryViolation.
        In unfixed code: release_file_lock does not accept or verify handle_id.
        """
        # Register a valid handle in ProcessHandleRegistry and then release it (expired)
        handle_id = ProcessHandleRegistry.register(
            pid=7003,
            handle=9999,
            create_time=1000.0,
            exe_path="C:/tools/helper.exe",
            ttl=0.01,
        )
        ProcessHandleRegistry.release(handle_id)

        grant = self.approval_mgr.grant_approval(
            tool_name="release_file_lock",
            arguments={
                "file_path": "locked_file.dat",
                "allow_kill": True,
                "target_pid": 7003,
                "handle_id": handle_id,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        holder = {"pid": 7003, "app_name": "helper.exe", "image_path": "C:/tools/helper.exe"}
        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [holder]}):
            res = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="locked_file.dat",
                allow_kill=True,
                target_pid=7003,
                handle_id=handle_id,
            )

        self.assertFalse(res.success, "Expired or invalid retained handle must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_term.assert_not_called()

    # =========================================================================
    # SECTION 2: Browser Execution Context & Navigation Binding
    # =========================================================================

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_04_execute_browser_javascript_navigation_before_dispatch_must_fail_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """GAP ADDENDUM-04: Browser JavaScript execution must bind to URL/origin.

        User approves executing JS on 'Banking Portal' at 'https://bank.example.com/account'.
        Between approval and dispatch, the tab navigates to 'https://attacker.evil.com/login'.
        Immediately prior to dispatch, the runtime must verify that the active tab's URL
        matches the approved expected_url.
        If navigated away, execution MUST fail closed with SecurityBoundaryViolation and NOT execute.
        In unfixed code: execute_browser_javascript executes JS on whatever URL the tab navigated to.
        """
        mock_exec.return_value = "stolen_data"
        script = "document.cookie"
        script_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()

        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script,
                "tab_name": "Banking Portal",
                "expected_url": "https://bank.example.com/account",
                "unique_context_id": "ctx_v8_bank_1",
                "script_digest": script_digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Tab in browser has navigated to attacker URL
        navigated_tabs = [
            {"id": "tab_1", "title": "Banking Portal", "url": "https://attacker.evil.com/login", "unique_context_id": "ctx_v8_bank_1"}
        ]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=navigated_tabs):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="Banking Portal",
                expected_url="https://bank.example.com/account",
                unique_context_id="ctx_v8_bank_1",
                script_digest=script_digest,
                allow_unmanaged=True,
            )

        self.assertFalse(res.success, "JavaScript execution on navigated tab must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_05_execute_browser_javascript_execution_context_replaced_must_fail_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """GAP ADDENDUM-05: Browser JavaScript execution must bind to execution context ID.

        User approves script execution in CDP execution context context_id=1.
        The page reloads or frame navigates, destroying context_id=1 and creating context_id=2.
        Actuation against the replaced execution context MUST fail closed with SecurityBoundaryViolation.
        In unfixed code: execute_browser_javascript does not verify execution context IDs.
        """
        mock_exec.return_value = "evaluated_result"
        script = "location.href"
        script_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()

        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script,
                "tab_name": "App Console",
                "execution_context_id": 1,
                "unique_context_id": "ctx_v8_app_1",
                "script_digest": script_digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Tab's active execution context is now 2 (reloaded/replaced)
        active_tab = [{"id": "tab_console", "title": "App Console", "url": "https://app.local", "execution_context_id": 2, "unique_context_id": "ctx_v8_app_1"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="App Console",
                execution_context_id=1,
                unique_context_id="ctx_v8_app_1",
                script_digest=script_digest,
                allow_unmanaged=True,
            )

        self.assertFalse(res.success, "Replaced execution context must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_06_execute_browser_javascript_browser_session_mismatch_must_fail_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """GAP ADDENDUM-06: Browser JavaScript execution must bind to browser target session ID.

        User approves execution on specific CDP target session target_id='PAGE_AAA'.
        The tab was closed and replaced by target_id='PAGE_BBB' sharing the same title.
        Actuation MUST verify the target_id matches and fail closed with SecurityBoundaryViolation.
        In unfixed code: execute_browser_javascript matches by substring title and executes on PAGE_BBB.
        """
        mock_exec.return_value = "evaluated_title"
        script = "document.title"
        script_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()

        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script,
                "tab_name": "Dashboard",
                "target_id": "PAGE_AAA",
                "unique_context_id": "ctx_v8_dash_1",
                "script_digest": script_digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Current browser has tab with target_id PAGE_BBB
        active_tabs = [{"id": "PAGE_BBB", "title": "Dashboard", "url": "https://corp.local/dash", "unique_context_id": "ctx_v8_dash_1"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tabs):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="Dashboard",
                target_id="PAGE_AAA",
                unique_context_id="ctx_v8_dash_1",
                script_digest=script_digest,
                allow_unmanaged=True,
            )

        self.assertFalse(res.success, "Substituted browser target session must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    # =========================================================================
    # SECTION 3: File Mutation Preimage Integrity (write_file & patch_file)
    # =========================================================================

    def test_07_patch_file_original_target_modified_before_execution_must_fail_closed(self) -> None:
        """GAP ADDENDUM-07: patch_file must verify original file preimage digest.

        User approves patching target.txt which initially has digest D1.
        Between approval and actuation, target.txt is modified (digest D2), though
        target_snippet is still present in the file.
        Actuation MUST check the preimage digest and fail closed with SecurityBoundaryViolation
        because the destination file changed after approval.
        In unfixed code: patch_file only checks snippet presence and modifies the tampered file.
        """
        target_file = self.test_dir / "preimage_patch.txt"
        initial_content = "CONFIG_FLAG = True\nLOG_LEVEL = 'INFO'\n"
        target_file.write_text(initial_content, encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")
        initial_digest = hashlib.sha256(initial_content.encode("utf-8")).hexdigest()

        grant = self.approval_mgr.grant_approval(
            tool_name="patch_file",
            arguments={
                "file_path": rel_path,
                "target_snippet": "CONFIG_FLAG = True",
                "replacement_snippet": "CONFIG_FLAG = False",
                "preimage_digest": initial_digest,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Attacker/external process modifies file before execution
        tampered_content = "CONFIG_FLAG = True\nLOG_LEVEL = 'INFO'\n# INJECTED_MALICIOUS_LINE\n"
        target_file.write_text(tampered_content, encoding="utf-8")

        res = SkillRegistry.execute_with_status(
            "patch_file",
            file_path=rel_path,
            target_snippet="CONFIG_FLAG = True",
            replacement_snippet="CONFIG_FLAG = False",
            preimage_digest=initial_digest,
        )

        self.assertFalse(res.success, "Modified file preimage before patch must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        # Ensure file was not patched on top of tampered baseline
        self.assertIn("CONFIG_FLAG = True", target_file.read_text(encoding="utf-8"))

    def test_08_write_file_overwrite_destination_modified_before_execution_must_fail_closed(self) -> None:
        """GAP ADDENDUM-08: write_file overwrite must verify destination preimage digest.

        User approves overwriting destination.txt which has digest D_orig.
        Between approval and actuation, destination.txt is altered to D_altered.
        Actuation MUST verify the destination preimage digest and fail closed with
        SecurityBoundaryViolation to prevent silently clobbering unseen file changes.
        In unfixed code: write_file overwrites destination.txt unconditionally without checking preimage.
        """
        dest_file = self.test_dir / "overwrite_preimage.txt"
        original_content = "SAFE_BASELINE_DATA\n"
        dest_file.write_text(original_content, encoding="utf-8")
        rel_path = str(dest_file.relative_to(REPO_ROOT)).replace("\\", "/")
        orig_digest = hashlib.sha256(original_content.encode("utf-8")).hexdigest()

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={
                "file_path": rel_path,
                "content": "NEW_APPROVED_DATA\n",
                "overwrite": True,
                "preimage_digest": orig_digest,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # File modified behind the scenes
        altered_content = "CRITICAL_STATE_CHANGED\n"
        dest_file.write_text(altered_content, encoding="utf-8")

        res = SkillRegistry.execute_with_status(
            "write_file",
            file_path=rel_path,
            content="NEW_APPROVED_DATA\n",
            overwrite=True,
            preimage_digest=orig_digest,
        )

        self.assertFalse(res.success, "Altered overwrite destination preimage must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        self.assertEqual(dest_file.read_text(encoding="utf-8"), altered_content)

    def test_09_write_file_unexpected_file_creation_before_non_overwrite_execution_must_fail_closed(self) -> None:
        """GAP ADDENDUM-09: write_file non-overwrite must fail with SecurityBoundaryViolation if file appeared.

        User approves creating a brand new file (expected preimage: nonexistent).
        A file appears at destination before write_file actuation.
        Actuation MUST fail closed with a SecurityBoundaryViolation rather than a soft error string.
        In unfixed code: write_file returns a normal error string without raising SecurityBoundaryViolation.
        """
        dest_file = self.test_dir / "nonexistent.txt"
        rel_path = str(dest_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={
                "file_path": rel_path,
                "content": "FRESH_CONTENT\n",
                "overwrite": False,
                "preimage_digest": None,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # File is created before tool actuation
        dest_file.write_text("SUDDENLY_EXISTS\n", encoding="utf-8")

        res = SkillRegistry.execute_with_status(
            "write_file",
            file_path=rel_path,
            content="FRESH_CONTENT\n",
            overwrite=False,
            preimage_digest=None,
        )

        self.assertFalse(res.success, "Unexpected file creation prior to non-overwrite write must fail closed")
        self.assertEqual(res.error_type, "security_violation")

    def test_10_file_mutation_destination_path_substitution_via_symlink_must_fail_closed(self) -> None:
        """GAP ADDENDUM-10: File mutation must reject symlink destination substitution.

        User approves modifying target.txt.
        Before actuation, target.txt is replaced with a filesystem symlink or junction.
        Actuation MUST fail closed with SecurityBoundaryViolation.
        In unfixed code: dev_shell.write_file does not check is_symlink prior to open().
        """
        dest_file = self.test_dir / "symlink_target.txt"
        outside_file = self.test_dir / "outside_payload.txt"
        outside_file.write_text("OUTSIDE_DATA\n", encoding="utf-8")
        rel_path = str(dest_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={
                "file_path": rel_path,
                "content": "ATTEMPTED_OVERWRITE\n",
                "overwrite": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Mock is_symlink to simulate symlink attack safely on any filesystem/OS
        with (
            patch.object(Path, "is_symlink", return_value=True),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            dev_shell.write_file(file_path=rel_path, content="ATTEMPTED_OVERWRITE\n", overwrite=True)

    # =========================================================================
    # SECTION 4: Positive Approved Workflows with Full Identity & Context
    # =========================================================================

    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.TerminateProcess", return_value=1)
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.OpenProcess")
    @patch("jarvis.skills.os_control._close_process_handle", return_value=1)
    def test_11_positive_approved_release_file_lock_with_complete_process_identity(
        self, mock_close: MagicMock, mock_open: MagicMock, mock_term: MagicMock
    ) -> None:
        """POSITIVE WORKFLOW: Approved release_file_lock succeeds when PID, creation time and image match."""
        retained_handle = 1234
        hid = ProcessHandleRegistry.register(
            pid=9001,
            handle=retained_handle,
            create_time=2000.0,
            exe_path="C:/apps/safe_editor.exe",
            ttl=30.0,
        )
        mock_holder = {
            "pid": 9001,
            "app_name": "safe_editor.exe",
            "image_path": "C:/apps/safe_editor.exe",
            "create_time": 2000.0,
        }
        self.approval_mgr.set_approval_handler(lambda req: True)

        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [mock_holder]}):
            res = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="document.docx",
                allow_kill=True,
                target_pid=9001,
                handle_id=hid,
            )

        self.assertTrue(res.success, f"Approved lock release must succeed: {res.error_message}")
        mock_open.assert_not_called()
        mock_term.assert_called_once_with(retained_handle, 1)
        mock_close.assert_called_once_with(retained_handle)

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_12_positive_approved_browser_javascript_with_matching_context(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """POSITIVE WORKFLOW: Approved browser JavaScript executes when context matches."""
        mock_exec.return_value = "Verified Header"
        script = "document.title"
        script_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        self.approval_mgr.set_approval_handler(lambda req: True)

        active_tab = [{"id": "tab_1", "title": "Verified Tab", "url": "https://verified.local", "unique_context_id": "ctx_v8_verified_1"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="Verified Tab",
                unique_context_id="ctx_v8_verified_1",
                script_digest=script_digest,
                allow_unmanaged=True,
            )

        self.assertTrue(res.success, f"Approved browser JS execution must succeed: {res.error_message}")
        mock_exec.assert_called_once()

    def test_13_positive_approved_file_mutation_with_matching_preimage(self) -> None:
        """POSITIVE WORKFLOW: Approved file write succeeds when destination matches expected state."""
        target_file = self.test_dir / "positive_addendum_write.txt"
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        self.approval_mgr.set_approval_handler(lambda req: True)

        res = SkillRegistry.execute_with_status(
            "write_file",
            file_path=rel_path,
            content="VERIFIED_ADDENDUM_DATA",
            overwrite=True,
        )

        self.assertTrue(res.success, f"Approved file write must succeed: {res.error_message}")
        self.assertEqual(target_file.read_text(encoding="utf-8"), "VERIFIED_ADDENDUM_DATA")

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_14_execute_browser_javascript_script_digest_mismatch_must_fail_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """Browser JavaScript execution must reject script digest tampering."""
        import hashlib
        approved_script = "console.log('approved')"
        expected_digest = hashlib.sha256(approved_script.encode("utf-8")).hexdigest()

        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": approved_script,
                "tab_name": "Portal",
                "unique_context_id": "ctx_v8_portal_1",
                "script_digest": expected_digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Attacker tampers with script
        tampered_script = "console.log('tampered')"
        active_tab = [{"id": "tab_1", "title": "Portal", "url": "https://portal.local", "unique_context_id": "ctx_v8_portal_1"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=tampered_script,
                tab_name="Portal",
                unique_context_id="ctx_v8_portal_1",
                script_digest=expected_digest,
                allow_unmanaged=True,
            )

        self.assertFalse(res.success, "Script digest mismatch must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_15_execute_browser_javascript_unique_context_id_mismatch_must_fail_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """Browser JavaScript execution must reject unique_context_id mismatch."""
        script = "document.body"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script,
                "tab_name": "Portal",
                "unique_context_id": "CTX_UUID_111",
                "script_digest": digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Tab in browser has different uniqueContextId
        active_tab = [{"id": "tab_1", "title": "Portal", "url": "https://portal.local", "unique_context_id": "CTX_UUID_222"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="Portal",
                unique_context_id="CTX_UUID_111",
                script_digest=digest,
                allow_unmanaged=True,
            )

        self.assertFalse(res.success, "Unique context ID mismatch must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_16_execute_browser_javascript_omitting_unique_context_id_after_approval_fails_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """Browser JavaScript execution must fail closed if unique_context_id was approved but omitted during execution."""
        script = "document.body"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script,
                "tab_name": "Portal",
                "unique_context_id": "CTX_UUID_SECURE_999",
                "script_digest": digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Execution attempts to omit unique_context_id
        res = SkillRegistry.execute_with_status(
            "execute_browser_javascript",
            script=script,
            tab_name="Portal",
            script_digest=digest,
            allow_unmanaged=True,
        )

        self.assertFalse(res.success, "Omitting approved unique_context_id must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    # =========================================================================
    # SECTION 5: Retained Process Handle Lifecycle & Pruning Invariants
    # =========================================================================

    @patch("jarvis.skills.os_control._close_process_handle")
    def test_17_process_handle_registry_prunes_expired_handles_and_closes_handles_once(
        self, mock_close: MagicMock
    ) -> None:
        """ProcessHandleRegistry must close handle and prune entry upon monotonic expiration."""
        handle_val = 8888
        hid = ProcessHandleRegistry.register(
            pid=4444,
            handle=handle_val,
            create_time=500.0,
            exe_path="C:/tools/test.exe",
            ttl=0.03,
        )
        self.assertIsNotNone(ProcessHandleRegistry.get(hid))
        for _ in range(20):
            time.sleep(0.01)
            if ProcessHandleRegistry.get(hid) is None:
                break

        # Accessing get or register invokes _prune_expired
        res = ProcessHandleRegistry.get(hid)
        self.assertIsNone(res, "Expired handle must be pruned from registry")
        mock_close.assert_called_once_with(handle_val)

    @patch("jarvis.skills.os_control._close_process_handle")
    def test_18_process_handle_registry_release_for_pid_closes_handle_exactly_once(
        self, mock_close: MagicMock
    ) -> None:
        """ProcessHandleRegistry.release_for_pid must close handle once and tolerate double release."""
        handle_val = 7777
        hid = ProcessHandleRegistry.register(
            pid=5555,
            handle=handle_val,
            create_time=600.0,
            exe_path="C:/tools/test2.exe",
            ttl=60.0,
        )
        self.assertIsNotNone(ProcessHandleRegistry.get(hid))

        # First release
        ProcessHandleRegistry.release_for_pid(5555)
        self.assertIsNone(ProcessHandleRegistry.get(hid))
        mock_close.assert_called_once_with(handle_val)

        # Second release on same PID must be a safe no-op (no double close)
        ProcessHandleRegistry.release_for_pid(5555)
        mock_close.assert_called_once_with(handle_val)

    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.TerminateProcess", return_value=1)
    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.OpenProcess")
    @patch("jarvis.skills.os_control._close_process_handle")
    def test_19_release_file_lock_with_retained_handle_terminates_without_open_process(
        self, mock_close: MagicMock, mock_open: MagicMock, mock_term: MagicMock
    ) -> None:
        """release_file_lock with valid retained handle terminates directly without OpenProcess TOCTOU."""
        retained_handle = 3333
        hid = ProcessHandleRegistry.register(
            pid=9901,
            handle=retained_handle,
            create_time=2500.0,
            exe_path="C:/apps/safe_editor.exe",
            ttl=30.0,
        )

        grant = self.approval_mgr.grant_approval(
            tool_name="release_file_lock",
            arguments={
                "file_path": "project.db",
                "allow_kill": True,
                "target_pid": 9901,
                "handle_id": hid,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        mock_holder = {
            "pid": 9901,
            "app_name": "safe_editor.exe",
            "image_path": "C:/apps/safe_editor.exe",
            "create_time": 2500.0,
        }
        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [mock_holder]}):
            res = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="project.db",
                allow_kill=True,
                target_pid=9901,
                handle_id=hid,
            )

        self.assertTrue(res.success, f"Retained handle termination must succeed: {res.error_message}")
        mock_open.assert_not_called()
        mock_term.assert_called_once_with(retained_handle, 1)
        mock_close.assert_called_once_with(retained_handle)
        self.assertIsNone(ProcessHandleRegistry.get(hid), "Retained handle must be released post-termination")

    @patch("jarvis.skills.rstrtmgr_lock_surgeon.kernel32.TerminateProcess", side_effect=RuntimeError("Kernel error"))
    @patch("jarvis.skills.os_control._close_process_handle")
    def test_20_release_file_lock_exception_during_actuation_releases_handle_in_finally(
        self, mock_close: MagicMock, mock_term: MagicMock
    ) -> None:
        """release_file_lock must release retained handle even if termination encounters an exception."""
        retained_handle = 2222
        hid = ProcessHandleRegistry.register(
            pid=9902,
            handle=retained_handle,
            create_time=2600.0,
            exe_path="C:/apps/safe_editor.exe",
            ttl=30.0,
        )

        grant = self.approval_mgr.grant_approval(
            tool_name="release_file_lock",
            arguments={
                "file_path": "project.db",
                "allow_kill": True,
                "target_pid": 9902,
                "handle_id": hid,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        mock_holder = {
            "pid": 9902,
            "app_name": "safe_editor.exe",
            "image_path": "C:/apps/safe_editor.exe",
            "create_time": 2600.0,
        }
        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [mock_holder]}):
            _ = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="project.db",
                allow_kill=True,
                target_pid=9902,
                handle_id=hid,
            )

        # Status is processed with failed action or exception
        mock_close.assert_called_once_with(retained_handle)
        self.assertIsNone(ProcessHandleRegistry.get(hid), "Handle must be released on failure")

    # =========================================================================
    # SECTION 5: Browser Context Identity & Active Process Handle Lifecycle
    # =========================================================================

    def test_21_browser_approval_without_unique_context_id_fails_closed(self) -> None:
        """1. Approval attempt for execute_browser_javascript without unique_context_id fails closed."""
        script = "console.log('test')"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.approval_mgr.grant_approval(
                tool_name="execute_browser_javascript",
                arguments={"script": script, "script_digest": digest},
                ttl_seconds=30.0,
            )
        self.assertIn("unique_context_id", str(ctx.exception))

    def test_22_browser_approval_without_script_digest_fails_closed(self) -> None:
        """2. Approval attempt for execute_browser_javascript without script_digest fails closed."""
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.approval_mgr.grant_approval(
                tool_name="execute_browser_javascript",
                arguments={"script": "console.log('test')", "unique_context_id": "v8_ctx_test_1"},
                ttl_seconds=30.0,
            )
        self.assertIn("script_digest", str(ctx.exception))

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_23_browser_context_id_changed_after_approval_fails_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """3. Execution with a different unique_context_id than approved fails closed."""
        script = "document.cookie"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script,
                "tab_name": "TestTab",
                "unique_context_id": "ctx_approved_1",
                "script_digest": digest,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        active_tab = [{"id": "tab_1", "title": "TestTab", "url": "https://test.local", "unique_context_id": "ctx_substituted_2"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="TestTab",
                unique_context_id="ctx_substituted_2",
                script_digest=digest,
                allow_unmanaged=True,
            )
        self.assertFalse(res.success, "Substituted unique_context_id must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_24_browser_script_digest_changed_after_approval_fails_closed(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """4. Execution with a different script_digest than approved fails closed."""
        script1 = "document.title"
        digest1 = hashlib.sha256(script1.encode("utf-8")).hexdigest()
        grant = self.approval_mgr.grant_approval(
            tool_name="execute_browser_javascript",
            arguments={
                "script": script1,
                "tab_name": "TestTab",
                "unique_context_id": "ctx_v8_1",
                "script_digest": digest1,
                "allow_unmanaged": True,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        script2 = "document.body.innerHTML"
        digest2 = hashlib.sha256(script2.encode("utf-8")).hexdigest()
        active_tab = [{"id": "tab_1", "title": "TestTab", "url": "https://test.local", "unique_context_id": "ctx_v8_1"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script2,
                tab_name="TestTab",
                unique_context_id="ctx_v8_1",
                script_digest=digest2,
                allow_unmanaged=True,
            )
        self.assertFalse(res.success, "Substituted script_digest must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        mock_exec.assert_not_called()

    def test_25_browser_missing_context_id_at_execution_fails_closed(self) -> None:
        """5. Calling execute_browser_javascript without unique_context_id fails closed."""
        script = "1 + 1"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        from jarvis.skills.browser_cdp import execute_browser_javascript
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            execute_browser_javascript(script=script, script_digest=digest)
        self.assertIn("unique_context_id", str(ctx.exception))

    def test_26_browser_missing_script_digest_at_execution_fails_closed(self) -> None:
        """6. Calling execute_browser_javascript without script_digest fails closed."""
        from jarvis.skills.browser_cdp import execute_browser_javascript
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            execute_browser_javascript(script="1 + 1", unique_context_id="v8_ctx_1")
        self.assertIn("script_digest", str(ctx.exception))

    @patch("jarvis.skills.browser_cdp.execute_cdp_javascript", return_value="success_val")
    @patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True)
    @patch("jarvis.skills.browser_cdp.get_managed_cdp_port", return_value=9222)
    def test_27_browser_successful_execution_with_exact_approved_values(
        self, mock_port: MagicMock, mock_avail: MagicMock, mock_exec: MagicMock
    ) -> None:
        """7. Successful execution when unique_context_id, script_digest, tab and script match approved values."""
        script = "window.navigator.userAgent"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        self.approval_mgr.set_approval_handler(lambda req: True)

        active_tab = [{"id": "tab_agent", "title": "Agent Tab", "url": "https://agent.local", "unique_context_id": "v8_agent_ctx_99"}]
        with patch("jarvis.skills.browser_cdp.list_cdp_tabs", return_value=active_tab):
            res = SkillRegistry.execute_with_status(
                "execute_browser_javascript",
                script=script,
                tab_name="Agent Tab",
                unique_context_id="v8_agent_ctx_99",
                script_digest=digest,
                allow_unmanaged=True,
            )
        self.assertTrue(res.success, f"Execution with exact approved values must succeed: {res.error_message}")
        mock_exec.assert_called_once()

    @patch("jarvis.skills.os_control._close_process_handle")
    def test_28_approval_cancellation_releases_retained_process_handle(
        self, mock_close: MagicMock
    ) -> None:
        """Approval cancellation via cancel_approval_request releases retained process handle."""
        handle_val = 5555
        hid = ProcessHandleRegistry.register(
            pid=7777,
            handle=handle_val,
            create_time=3000.0,
            exe_path="C:/apps/victim.exe",
            ttl=60.0,
        )
        self.assertIsNotNone(ProcessHandleRegistry.get(hid))

        cancel_event = threading.Event()
        # Request approval with cancel_event
        self.approval_mgr.set_approval_handler(lambda req: True)
        grant = self.approval_mgr.request_human_approval(
            tool_name="release_file_lock",
            arguments={"file_path": "test.txt", "allow_kill": True, "target_pid": 7777, "handle_id": hid},
            risk_level=2,
            cancel_event=cancel_event,
        )
        self.assertIsNotNone(grant)

        # Cancel the request
        self.approval_mgr.cancel_approval_request(cancel_event)
        mock_close.assert_called_once_with(handle_val)
        self.assertIsNone(ProcessHandleRegistry.get(hid), "Cancelled approval must release process handle")

    @patch("jarvis.skills.os_control._close_process_handle")
    def test_29_process_handle_registry_active_timer_expiration_closes_handle(
        self, mock_close: MagicMock
    ) -> None:
        """ProcessHandleRegistry active daemon timer closes expired handle without user access."""
        handle_val = 8888
        hid = ProcessHandleRegistry.register(
            pid=8888,
            handle=handle_val,
            create_time=3100.0,
            exe_path="C:/apps/timer_test.exe",
            ttl=0.03,
        )
        self.assertIsNotNone(ProcessHandleRegistry._handles.get(hid))

        # Wait for the daemon timer to fire actively without calling any registry methods
        for _ in range(50):
            time.sleep(0.02)
            if mock_close.called:
                break
        mock_close.assert_called_once_with(handle_val)
        self.assertNotIn(hid, ProcessHandleRegistry._handles)

    def test_30_release_file_lock_requires_retained_handle_and_prohibits_openprocess(self) -> None:
        """release_file_lock with allow_kill=True fails closed if no retained handle is provided."""
        self.approval_mgr.set_approval_handler(lambda req: True)
        mock_holder = {
            "pid": 9999,
            "app_name": "orphan.exe",
            "image_path": "C:/apps/orphan.exe",
            "create_time": 3200.0,
        }
        with patch("jarvis.skills.rstrtmgr_lock_surgeon.inspect_file_locks", return_value={"status": "locked", "holders": [mock_holder]}):
            res = SkillRegistry.execute_with_status(
                "release_file_lock",
                file_path="orphan.txt",
                allow_kill=True,
                target_pid=9999,
            )
        self.assertFalse(res.success, "Termination without retained handle must fail closed")
        self.assertEqual(res.error_type, "security_violation")
        self.assertIn("retained process handle", res.error_message)

    @patch("jarvis.skills.os_control._close_process_handle")
    def test_31_process_handle_lease_concurrency_barrier_race_protection(
        self, mock_close: MagicMock
    ) -> None:
        """ProcessHandleRegistry.lease guarantees in-flight handle retention during concurrent cancellation/expiry.

        Uses deterministic threading.Barrier:
        1. Thread A acquires lease and enters in-flight execution.
        2. Barrier 1: Thread B (cancellation/expiry) triggers release while Thread A is actively running.
        3. Registry evicts entry so no NEW acquisitions can succeed, but defers CloseHandle while leased.
        4. Thread A verifies handle remains open and valid throughout in-flight execution.
        5. Barrier 2: Thread A completes actuation and exits lease.
        6. Registry immediately closes handle exactly once upon active lease exit.
        """
        handle_val = 99999
        hid = ProcessHandleRegistry.register(
            pid=7788,
            handle=handle_val,
            create_time=5000.0,
            exe_path="C:/apps/concurrency.exe",
            ttl=60.0,
        )

        b1 = threading.Barrier(2)
        b2 = threading.Barrier(2)
        events = []

        def worker_actuation():
            with ProcessHandleRegistry.lease(handle_id=hid) as lease_entry:
                events.append("leased")
                b1.wait()  # Signal to Thread B that lease is active
                b2.wait()  # Wait for Thread B to execute cancellation/release
                # Assert handle was NOT closed while we are inside the active lease!
                self.assertEqual(mock_close.call_count, 0, "Handle must NOT be closed while lease is active")
                self.assertIsNotNone(lease_entry)
                self.assertEqual(lease_entry["handle"], handle_val)
                events.append("actuation_complete")

        t = threading.Thread(target=worker_actuation)
        t.start()

        b1.wait()  # Wait until worker_actuation has entered lease
        # Now worker is inside the lease. Concurrently invoke release/cancellation:
        ProcessHandleRegistry.release(hid)
        events.append("released_externally")

        # Verify entry is immediately evicted from registry so no NEW acquisition can succeed
        self.assertIsNone(ProcessHandleRegistry.get(hid), "Evicted entry cannot be retrieved by new callers")
        with ProcessHandleRegistry.lease(handle_id=hid) as new_lease:
            self.assertIsNone(new_lease, "Evicted entry cannot be leased by new callers")

        # But physical handle MUST NOT be closed yet because worker_actuation is still running!
        self.assertEqual(mock_close.call_count, 0, "Handle closure must be deferred while in-flight lease is active")

        b2.wait()  # Allow worker_actuation to exit its lease
        t.join(timeout=2.0)

        # Upon lease exit, handle MUST be closed exactly once
        self.assertEqual(events, ["leased", "released_externally", "actuation_complete"])
        mock_close.assert_called_once_with(handle_val)

    @patch("jarvis.skills.os_control._close_process_handle")
    def test_32_cancellation_preempts_actuation_before_lease(
        self, mock_close: MagicMock
    ) -> None:
        """Cancellation winning before actuation prevents termination from starting."""
        handle_val = 66666
        hid = ProcessHandleRegistry.register(
            pid=8899,
            handle=handle_val,
            create_time=6000.0,
            exe_path="C:/apps/preempt.exe",
            ttl=60.0,
        )
        # Cancellation occurs BEFORE actuation
        ProcessHandleRegistry.release(hid)
        mock_close.assert_called_once_with(handle_val)

        # Subsequent actuation attempt fails closed
        with ProcessHandleRegistry.lease(handle_id=hid) as lease_entry:
            self.assertIsNone(lease_entry, "Cancelled handle cannot be leased")
        # No double close
        mock_close.assert_called_once_with(handle_val)



if __name__ == "__main__":
    unittest.main()
