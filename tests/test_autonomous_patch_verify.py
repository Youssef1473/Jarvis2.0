"""Hermetic unit and security-boundary tests for Plan 042 Autonomous AppContainer Patch & Verify Loop."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
)
from jarvis.skills.ast_code_surgeon import (
    EvolutionVerificationReport,
    create_shadow_patch,
    promote_verified_patch,
    validate_target_patch_path,
    verify_patch_in_appcontainer,
)
from jarvis.skills.self_evolution import propose_and_verify_code_patch


class TestAutonomousPatchVerify(unittest.TestCase):
    """Test suite verifying shadow staging, path confinement, AppContainer verification, and human gates."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_path = Path(self.temp_dir.name)

        # Create simulated workspace layout
        self.jarvis_dir = self.ws_path / "jarvis" / "skills"
        self.jarvis_dir.mkdir(parents=True, exist_ok=True)

        self.target_file = self.jarvis_dir / "calc_service.py"
        self.initial_content = (
            "def add_values(a: int, b: int) -> int:\n"
            "    # Faulty implementation\n"
            "    return a - b\n"
        )
        self.target_file.write_text(self.initial_content, encoding="utf-8")
        self.initial_hash = hashlib.sha256(self.initial_content.encode("utf-8")).hexdigest()

        # Reset approval manager
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()

    def tearDown(self) -> None:
        self.approval_mgr.reset()
        try:
            self.temp_dir.cleanup()
        except (OSError, PermissionError):
            pass

    def test_01_path_traversal_outside_workspace_rejected(self) -> None:
        """Paths attempting to traverse outside the workspace root must be rejected immediately."""
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            validate_target_patch_path("../../outside.py", workspace_root=self.ws_path)
        self.assertIn("traversal", str(ctx.exception).lower())

    def test_02_forbidden_security_modules_rejected(self) -> None:
        """Targeting security-critical infrastructure modules must fail closed with SecurityBoundaryViolation."""
        forbidden_targets = [
            "jarvis/core/safety.py",
            "jarvis/core/security_boundaries.py",
            "jarvis/core/evolution/appcontainer.py",
            "jarvis/core/evolution/sandbox.py",
            "jarvis/skills/job_object_sandbox.py",
        ]
        for f_target in forbidden_targets:
            dummy_target = self.ws_path / f_target
            dummy_target.parent.mkdir(parents=True, exist_ok=True)
            dummy_target.write_text("# dummy", encoding="utf-8")

            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                validate_target_patch_path(f_target, workspace_root=self.ws_path)
            self.assertIn("immutable", str(ctx.exception).lower())

    def test_03_syntax_error_rejected_in_memory(self) -> None:
        """Candidate patches with syntax errors must fail fast before staging files in shadow."""
        shadow_dir = self.ws_path / "shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)

        invalid_code = "def add_values(a: int, b: int) -> int:\n    return a +++ \n"
        with self.assertRaises(SyntaxError):
            create_shadow_patch(
                file_path="jarvis/skills/calc_service.py",
                node_start_line=1,
                node_end_line=3,
                replacement_code=invalid_code,
                shadow_root=shadow_dir,
                workspace_root=self.ws_path,
            )

        # Confirm no staged file was written to shadow
        staged_target = shadow_dir / "jarvis" / "skills" / "calc_service.py"
        self.assertFalse(staged_target.exists())

    def test_04_blocked_ast_constructs_rejected(self) -> None:
        """Candidate patches containing dangerous AST nodes (reflection, dunder escapes) fail closed."""
        shadow_dir = self.ws_path / "shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)

        dangerous_code = (
            "def add_values(a: int, b: int) -> int:\n"
            "    fn = getattr(int, '__doc__')\n"
            "    return a + b\n"
        )
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            create_shadow_patch(
                file_path="jarvis/skills/calc_service.py",
                node_start_line=1,
                node_end_line=3,
                replacement_code=dangerous_code,
                shadow_root=shadow_dir,
                workspace_root=self.ws_path,
            )
        self.assertIn("AST security audit", str(ctx.exception))

    def test_05_shadow_staging_produces_unified_diff_without_mutating_live_file(self) -> None:
        """Valid patch creates shadow candidate and computes unified diff while live file remains untouched."""
        shadow_dir = self.ws_path / "shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)

        valid_fix = (
            "def add_values(a: int, b: int) -> int:\n"
            "    # Corrected implementation\n"
            "    return a + b\n"
        )
        shadow_target, diff, new_content = create_shadow_patch(
            file_path="jarvis/skills/calc_service.py",
            node_start_line=1,
            node_end_line=3,
            replacement_code=valid_fix,
            shadow_root=shadow_dir,
            workspace_root=self.ws_path,
        )

        # 1. Shadow target exists and matches new content
        self.assertTrue(shadow_target.exists())
        self.assertEqual(shadow_target.read_text(encoding="utf-8"), new_content)

        # 2. Unified diff is accurate
        self.assertIn("--- a/jarvis/skills/calc_service.py", diff)
        self.assertIn("+++ b/jarvis/skills/calc_service.py", diff)
        self.assertIn("+    return a + b", diff)

        # 3. Live disk file is byte-for-byte identical to initial state
        current_disk_content = self.target_file.read_text(encoding="utf-8")
        current_disk_hash = hashlib.sha256(current_disk_content.encode("utf-8")).hexdigest()
        self.assertEqual(current_disk_content, self.initial_content)
        self.assertEqual(current_disk_hash, self.initial_hash)

    def test_06_candidate_patch_violating_ruff_lint_fails_verification(self) -> None:
        """Stage 1 Ruff linter failure rejects verification and reports failure details."""
        shadow_dir = self.ws_path / "shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)
        shadow_target = shadow_dir / "jarvis" / "skills" / "calc_service.py"
        shadow_target.parent.mkdir(parents=True, exist_ok=True)
        shadow_target.write_text(self.initial_content, encoding="utf-8")

        mock_out = (1, "I001 Import block unformatted\nF401 unused import", "")
        with patch("jarvis.skills.ast_code_surgeon._run_sandboxed_command", return_value=mock_out):
            report = verify_patch_in_appcontainer(
                shadow_target=shadow_target,
                target_test_module="tests/test_calc.py",
                shadow_root=shadow_dir,
                workspace_root=self.ws_path,
            )

        self.assertIsInstance(report, EvolutionVerificationReport)
        self.assertFalse(report.passed)
        self.assertEqual(report.lint_exit_code, 1)
        self.assertIn("Ruff linter violation", str(report.error_reason))

    def test_07_candidate_patch_failing_targeted_test_fails_verification(self) -> None:
        """Stage 3 targeted test failure rejects candidate patch without touching live file."""
        shadow_dir = self.ws_path / "shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)
        shadow_target = shadow_dir / "jarvis" / "skills" / "calc_service.py"
        shadow_target.parent.mkdir(parents=True, exist_ok=True)
        shadow_target.write_text(self.initial_content, encoding="utf-8")

        # Mock sequence: Lint pass (0), Diagnostics pass (0), Pytest fail (1)
        mock_seq = [
            (0, "All checks passed!", ""),
            (0, "ALL DIAGNOSTICS PASSED", ""),
            (1, "FAILED test_calc.py::test_add - AssertionError: 2 != 3", ""),
        ]
        with patch("jarvis.skills.ast_code_surgeon._run_sandboxed_command", side_effect=mock_seq):
            report = verify_patch_in_appcontainer(
                shadow_target=shadow_target,
                target_test_module="tests/test_calc.py",
                shadow_root=shadow_dir,
                workspace_root=self.ws_path,
            )

        self.assertFalse(report.passed)
        self.assertEqual(report.lint_exit_code, 0)
        self.assertEqual(report.diagnostics_exit_code, 0)
        self.assertEqual(report.test_exit_code, 1)
        self.assertIn("Targeted test failure", str(report.error_reason))

    def test_08_valid_candidate_patch_passes_all_verification_stages(self) -> None:
        """Candidate patch satisfying lint, diagnostics, and test stages passes with 0 exit codes."""
        shadow_dir = self.ws_path / "shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)
        shadow_target = shadow_dir / "jarvis" / "skills" / "calc_service.py"
        shadow_target.parent.mkdir(parents=True, exist_ok=True)
        shadow_target.write_text(self.initial_content, encoding="utf-8")

        mock_seq = [
            (0, "All checks passed!", ""),
            (0, "ALL DIAGNOSTICS PASSED", ""),
            (0, "1 passed in 0.05s", ""),
        ]
        with patch("jarvis.skills.ast_code_surgeon._run_sandboxed_command", side_effect=mock_seq):
            report = verify_patch_in_appcontainer(
                shadow_target=shadow_target,
                target_test_module="tests/test_calc.py",
                shadow_root=shadow_dir,
                workspace_root=self.ws_path,
                unified_diff="--- a\n+++ b",
                file_path="jarvis/skills/calc_service.py",
            )

        self.assertTrue(report.passed)
        self.assertEqual(report.lint_exit_code, 0)
        self.assertEqual(report.diagnostics_exit_code, 0)
        self.assertEqual(report.test_exit_code, 0)
        self.assertIsNone(report.error_reason)
        self.assertIn("calc_service.py", report.file_path)

    def test_09_live_promotion_requires_human_approval_token(self) -> None:
        """Promoting a candidate patch to the live file strictly requires human authorization."""
        valid_fix = (
            "def add_values(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        # 1. Unauthorized attempt must be rejected
        res_unauth = promote_verified_patch(
            file_path="jarvis/skills/calc_service.py",
            replacement_code=valid_fix,
            node_start_line=1,
            node_end_line=3,
            user_approved=False,
            workspace_root=self.ws_path,
        )
        self.assertEqual(res_unauth.get("status"), "rejected_unauthorized")
        self.assertFalse(res_unauth.get("promoted"))
        self.assertEqual(self.target_file.read_text(encoding="utf-8"), self.initial_content)

        # 2. Authorized attempt via DualKeyApprovalManager succeeds
        self.approval_mgr.grant_approval("promote_verified_patch", {
            "file_path": "jarvis/skills/calc_service.py",
            "node_start_line": 1,
            "node_end_line": 3,
        })
        res_auth = promote_verified_patch(
            file_path="jarvis/skills/calc_service.py",
            replacement_code=valid_fix,
            node_start_line=1,
            node_end_line=3,
            workspace_root=self.ws_path,
        )
        self.assertEqual(res_auth.get("status"), "promoted")
        self.assertTrue(res_auth.get("promoted"))
        self.assertEqual(self.target_file.read_text(encoding="utf-8"), valid_fix)

    def test_10_shadow_directory_cleaned_up_on_failure_and_success(self) -> None:
        """propose_and_verify_code_patch must clean up the ephemeral shadow workspace in finally:."""
        valid_fix = (
            "def add_values(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        mock_seq = [
            (0, "All checks passed!", ""),
            (0, "ALL DIAGNOSTICS PASSED", ""),
            (0, "1 passed in 0.05s", ""),
        ]
        created_shadows: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def tracked_mkdtemp(*args, **kwargs):
            p = Path(real_mkdtemp(*args, **kwargs))
            created_shadows.append(p)
            return str(p)

        with (
            patch("tempfile.mkdtemp", side_effect=tracked_mkdtemp),
            patch("jarvis.skills.ast_code_surgeon._run_sandboxed_command", side_effect=mock_seq),
        ):
            res = propose_and_verify_code_patch(
                file_path="jarvis/skills/calc_service.py",
                node_start_line=1,
                node_end_line=3,
                replacement_code=valid_fix,
                target_test_module="tests/test_calc.py",
                workspace_root=str(self.ws_path),
            )

        self.assertEqual(res.get("status"), "verified")
        self.assertIsNotNone(res.get("report"))
        self.assertTrue(res["report"]["passed"])

        # Confirm all tracked shadow directories are completely unlinked
        self.assertGreater(len(created_shadows), 0)
        for s_dir in created_shadows:
            self.assertFalse(s_dir.exists())


if __name__ == "__main__":
    unittest.main()
