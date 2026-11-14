"""Empirical audit test suite verifying filesystem handle-bound validation and anti-TOCTOU security."""
import hashlib
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills import dev_shell

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestFilesystemToctouSecurityAudit(unittest.TestCase):
    """P0 Audit Suite: Verifies handle-bound validation prevents TOCTOU reparse point and junction bypasses."""

    def setUp(self):
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.test_dir = REPO_ROOT / "tests" / "temp_toctou_audit"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self._ctx_tokens = []

    def tearDown(self):
        for tok in self._ctx_tokens:
            self.approval_mgr.clear_active_authorized(tok)
        self.approval_mgr.reset()
        self.tracker.reset_turn()
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_write_file_handle_detects_leaf_reparse_point_attribute(self):
        """INVARIANT 1: Handle attribute inspection must fail closed if leaf is a reparse point."""
        target_file = self.test_dir / "reparse_leaf.txt"
        target_file.write_text("INITIAL", encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={"file_path": rel_path, "content": "NEW_DATA", "overwrite": True},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Mock os.stat on the open fd to simulate a reparse point attribute (0x0400)
        orig_stat = os.stat

        def mock_stat(fd_or_path, *args, **kwargs):
            res = orig_stat(fd_or_path, *args, **kwargs)
            if isinstance(fd_or_path, int):
                # Return stat with reparse point attribute flag set
                fake_attrs = getattr(res, "st_file_attributes", 0) | 0x0400
                res_dict = {attr: getattr(res, attr) for attr in dir(res) if attr.startswith("st_")}
                res_dict["st_file_attributes"] = fake_attrs
                mock_res = MagicMock(wraps=res)
                mock_res.st_file_attributes = fake_attrs
                return mock_res
            return res

        with patch("os.stat", side_effect=mock_stat):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                dev_shell.write_file(file_path=rel_path, content="NEW_DATA", overwrite=True)
            self.assertIn("reparse point", str(ctx.exception).lower())

        # Verify content was NOT overwritten
        self.assertEqual(target_file.read_text(encoding="utf-8"), "INITIAL")

    def test_02_write_file_handle_detects_parent_junction_escaping_workspace(self):
        """INVARIANT 2: GetFinalPathNameByHandleW resolving outside workspace must fail closed."""
        target_file = self.test_dir / "escaped_by_junction.txt"
        target_file.write_text("WORKSPACE_DATA", encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={"file_path": rel_path, "content": "ATTACK_PAYLOAD", "overwrite": True},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Simulate GetFinalPathNameByHandleW returning path pointing to System32 on the active workspace drive
        escaped_drive = REPO_ROOT.drive if REPO_ROOT.drive else "C:"
        escaped_path = f"{escaped_drive}\\Windows\\System32\\critical.dll"
        with patch("jarvis.skills.dev_shell._get_handle_physical_path", return_value=escaped_path):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                dev_shell.write_file(file_path=rel_path, content="ATTACK_PAYLOAD", overwrite=True)
            exc_lower = str(ctx.exception).lower()
            self.assertTrue(
                "escapes confined root" in exc_lower or "outside workspace" in exc_lower,
                f"Unexpected exception message: {ctx.exception}",
            )

        # Verify file content is intact
        self.assertEqual(target_file.read_text(encoding="utf-8"), "WORKSPACE_DATA")

    def test_03_write_file_does_not_truncate_file_before_handle_verification(self):
        """INVARIANT 3: Destination file must NEVER be truncated prior to handle-based verification."""
        target_file = self.test_dir / "secret_document.txt"
        original_secret = "TOP_SECRET_PROPRIETARY_INFORMATION_DO_NOT_DELETE"
        target_file.write_text(original_secret, encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={"file_path": rel_path, "content": "OVERWRITE_ATTEMPT", "overwrite": True},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Fail verification at handle check
        with (
            patch("jarvis.skills.dev_shell._verify_open_file_handle", side_effect=SecurityBoundaryViolation("Handle verification failed")),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            dev_shell.write_file(file_path=rel_path, content="OVERWRITE_ATTEMPT", overwrite=True)

        # Crucial check: original file was NOT truncated to 0 bytes
        self.assertEqual(target_file.read_text(encoding="utf-8"), original_secret)

    def test_04_patch_file_does_not_modify_file_if_handle_verification_fails(self):
        """INVARIANT 4: patch_file must not modify destination if handle verification fails."""
        target_file = self.test_dir / "patch_target.txt"
        initial_text = "def calculate_tax():\n    return 0.15\n"
        target_file.write_text(initial_text, encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="patch_file",
            arguments={
                "file_path": rel_path,
                "target_snippet": "return 0.15",
                "replacement_snippet": "return 0.00",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with (
            patch("jarvis.skills.dev_shell._get_handle_physical_path", return_value="C:\\Windows\\System32\\drivers.sys"),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            dev_shell.patch_file(
                file_path=rel_path,
                target_snippet="return 0.15",
                replacement_snippet="return 0.00",
            )

        self.assertEqual(target_file.read_text(encoding="utf-8"), initial_text)

    def test_05_non_overwrite_atomic_creation_fails_closed_if_file_exists(self):
        """INVARIANT 5: Atomic O_CREAT | O_EXCL prevents race condition where destination was created."""
        target_file = self.test_dir / "already_exists.txt"
        target_file.write_text("EXISTING", encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={"file_path": rel_path, "content": "NEW_DATA", "overwrite": False},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            dev_shell.write_file(file_path=rel_path, content="NEW_DATA", overwrite=False)
        self.assertIn("already exists", str(ctx.exception).lower())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "EXISTING")

    def test_06_absent_target_beneath_redirected_parent_fails_closed_without_creating_file(self):
        """INVARIANT 6: An absent target beneath a redirected parent junction fails closed without creating any file."""
        outside_dir = self.test_dir / "simulated_outside_junction"
        outside_dir.mkdir(parents=True, exist_ok=True)
        absent_target = outside_dir / "nonexistent_victim.txt"
        rel_path = str(absent_target.relative_to(REPO_ROOT)).replace("\\", "/")

        for overwrite_mode in (False, True):
            grant = self.approval_mgr.grant_approval(
                tool_name="write_file",
                arguments={"file_path": rel_path, "content": "MALICIOUS_PAYLOAD", "overwrite": overwrite_mode},
                ttl_seconds=30.0,
                turn_id=self.tracker.get_current_turn_id(),
            )
            ctx_token = self.approval_mgr.bind_active_grant(grant)
            self._ctx_tokens.append(ctx_token)

            with (
                patch("jarvis.skills.dev_shell._verify_directory_confinement", side_effect=SecurityBoundaryViolation("Parent directory resolved outside workspace")),
                self.assertRaises(SecurityBoundaryViolation),
            ):
                dev_shell.write_file(file_path=rel_path, content="MALICIOUS_PAYLOAD", overwrite=overwrite_mode)

            # Assert ZERO file was created on disk
            self.assertFalse(absent_target.exists(), f"Target file must not be created when overwrite={overwrite_mode}")

    def test_07_absent_target_handle_verification_failure_cleans_up_created_file(self):
        """INVARIANT 7: If handle verification fails on an absent target file, cleanup unlinks the file."""
        absent_target = self.test_dir / "unlinked_upon_failure.txt"
        self.assertFalse(absent_target.exists())
        rel_path = str(absent_target.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={"file_path": rel_path, "content": "UNAUTHORIZED_PAYLOAD", "overwrite": False},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with (
            patch("jarvis.skills.dev_shell._verify_open_file_handle", side_effect=SecurityBoundaryViolation("Handle verification failed")),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            dev_shell.write_file(file_path=rel_path, content="UNAUTHORIZED_PAYLOAD", overwrite=False)

        # Crucial check: the file created by O_CREAT was unlinked in exception handler
        self.assertFalse(absent_target.exists(), "Absent file must be unlinked if handle verification fails")

    def test_08_patch_file_rejects_parent_directory_junction_escaping_workspace(self):
        """INVARIANT 8: patch_file pre-verifies parent directory confinement before opening file."""
        target_file = self.test_dir / "patch_parent_test.txt"
        target_file.write_text("SOME_ORIGINAL_SNIPPET\n", encoding="utf-8")
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="patch_file",
            arguments={
                "file_path": rel_path,
                "target_snippet": "SOME_ORIGINAL_SNIPPET",
                "replacement_snippet": "PATCHED_SNIPPET",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with (
            patch("jarvis.skills.dev_shell._verify_directory_confinement", side_effect=SecurityBoundaryViolation("Parent directory escapes workspace")),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            dev_shell.patch_file(
                file_path=rel_path,
                target_snippet="SOME_ORIGINAL_SNIPPET",
                replacement_snippet="PATCHED_SNIPPET",
            )

        self.assertEqual(target_file.read_text(encoding="utf-8"), "SOME_ORIGINAL_SNIPPET\n")

    def test_09_handle_bound_deletion_does_not_unlink_substituted_path(self):
        """INVARIANT 9: Handle-bound deletion operates through handle, never unlinking path-based victims."""
        absent_target = self.test_dir / "victim_not_unlinked.txt"
        self.assertFalse(absent_target.exists())
        rel_path = str(absent_target.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={"file_path": rel_path, "content": "NEW_DATA", "overwrite": False},
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        with (
            patch("jarvis.skills.dev_shell._verify_open_file_handle", side_effect=SecurityBoundaryViolation("Handle verification failed")),
            patch("pathlib.Path.unlink") as mock_path_unlink,
            patch("os.unlink") as mock_os_unlink,
            self.assertRaises(SecurityBoundaryViolation),
        ):
            dev_shell.write_file(file_path=rel_path, content="NEW_DATA", overwrite=False)

        # Deletion must occur via SetFileInformationByHandle, NOT through path unlinking
        mock_path_unlink.assert_not_called()
        mock_os_unlink.assert_not_called()
        self.assertFalse(absent_target.exists())

    def test_10_write_file_overwrite_fails_closed_if_target_deleted_before_actuation(self):
        """INVARIANT 10: Overwrite with preimage fails closed if file was deleted before actuation."""
        target_file = self.test_dir / "deleted_before_overwrite.txt"
        target_file.write_text("ORIGINAL", encoding="utf-8")
        orig_digest = hashlib.sha256(b"ORIGINAL").hexdigest()
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="write_file",
            arguments={
                "file_path": rel_path,
                "content": "OVERWRITE_DATA",
                "overwrite": True,
                "preimage_digest": orig_digest,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Target file is deleted before actuation
        target_file.unlink()

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            dev_shell.write_file(
                file_path=rel_path,
                content="OVERWRITE_DATA",
                overwrite=True,
                preimage_digest=orig_digest,
            )
        self.assertIn("unexpectedly deleted", str(ctx.exception).lower())
        self.assertFalse(target_file.exists())

    def test_11_patch_file_with_preimage_digest_preserves_file_on_mismatch(self):
        """INVARIANT 11: patch_file with preimage digest fails closed and preserves file on content mismatch."""
        target_file = self.test_dir / "preimage_patch_target.txt"
        target_file.write_text("version = 1\nmode = 'debug'\n", encoding="utf-8")
        orig_digest = hashlib.sha256(b"version = 1\nmode = 'debug'\n").hexdigest()
        rel_path = str(target_file.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="patch_file",
            arguments={
                "file_path": rel_path,
                "target_snippet": "mode = 'debug'",
                "replacement_snippet": "mode = 'production'",
                "preimage_digest": orig_digest,
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        # Concurrently modify target file on disk before actuation
        tampered_content = "version = 1\nmode = 'debug'\n# Injected code\n"
        target_file.write_text(tampered_content, encoding="utf-8")

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            dev_shell.patch_file(
                file_path=rel_path,
                target_snippet="mode = 'debug'",
                replacement_snippet="mode = 'production'",
                preimage_digest=orig_digest,
            )
        self.assertIn("preimage integrity violation", str(ctx.exception).lower())
        # File must remain untouched and not truncated
        self.assertEqual(target_file.read_text(encoding="utf-8"), tampered_content)

    def test_12_patch_file_absent_destination_returns_error_string(self):
        """INVARIANT 12: patch_file on non-existent file returns clean error string without exception."""
        nonexistent = self.test_dir / "does_not_exist.txt"
        rel_path = str(nonexistent.relative_to(REPO_ROOT)).replace("\\", "/")

        grant = self.approval_mgr.grant_approval(
            tool_name="patch_file",
            arguments={
                "file_path": rel_path,
                "target_snippet": "old",
                "replacement_snippet": "new",
            },
            ttl_seconds=30.0,
            turn_id=self.tracker.get_current_turn_id(),
        )
        ctx_token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(ctx_token)

        res = dev_shell.patch_file(
            file_path=rel_path,
            target_snippet="old",
            replacement_snippet="new",
        )
        self.assertIn("File does not exist", res)
