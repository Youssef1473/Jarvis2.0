"""Hermetic unit and integration tests for Plan 045: HUD Patch Approval & Live Diff Viewer."""
from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
)
from jarvis.skills.ast_code_surgeon import (
    EvolutionVerificationReport,
    PatchProposalRegistry,
    get_proposal_registry,
)
from jarvis.skills.self_evolution import propose_and_verify_code_patch
from jarvis.ui.server import JarvisUIService, get_hud_session_token


class TestHudPatchApproval(unittest.TestCase):
    """Verifies proposal lifecycle, TTL expiry, optimistic concurrency, REST API, and HUD Zero-CLS compliance."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_path = Path(self.temp_dir.name).resolve()

        # Create isolated workspace structure
        self.skills_dir = self.ws_path / "jarvis" / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        self.sample_file = self.skills_dir / "math_calc.py"
        self.sample_content = (
            "def calculate_tax(amount: float) -> float:\n"
            "    # Faulty rate\n"
            "    return amount * 0.05\n"
        )
        self.sample_file.write_text(self.sample_content, encoding="utf-8")
        self.sha256_before = hashlib.sha256(self.sample_file.read_bytes()).hexdigest()

        # Reset registries and approvals
        self.registry = get_proposal_registry()
        self.registry.reset()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()

        self.service = JarvisUIService(enforce_auth=True)
        self.app = self.service._create_app()
        self.client = TestClient(self.app)
        self.token = get_hud_session_token()

    def tearDown(self) -> None:
        self.registry.reset()
        self.approval_mgr.reset()
        try:
            self.temp_dir.cleanup()
        except (OSError, PermissionError):
            pass

    def _create_dummy_report(self, passed: bool = True) -> EvolutionVerificationReport:
        return EvolutionVerificationReport(
            passed=passed,
            file_path="jarvis/skills/math_calc.py",
            unified_diff="--- a/jarvis/skills/math_calc.py\n+++ b/jarvis/skills/math_calc.py\n@@ -1,3 +1,3 @@\n",
            lint_exit_code=0 if passed else 1,
            diagnostics_exit_code=0,
            test_exit_code=0 if passed else 1,
            stdout="OK" if passed else "FAILED",
            stderr="",
            elapsed_ms=42.0,
        )

    def test_01_proposal_registration_and_ttl_expiration(self) -> None:
        """Verifies proposal registration, retrieval, short TTL expiration, and capacity pruning."""
        rep = self._create_dummy_report(passed=True)
        prop = self.registry.register_proposal(
            file_path="jarvis/skills/math_calc.py",
            node_start_line=1,
            node_end_line=3,
            replacement_code="def calculate_tax(amount: float) -> float:\n    return amount * 0.15\n",
            target_test_module="tests/test_math.py",
            sha256_before=self.sha256_before,
            sha256_shadow="dummy_shadow_hash",
            unified_diff="--- a\n+++ b\n",
            report=rep,
            proposal_id="prop_ttl_test_01",
            ttl_seconds=0.05,
            workspace_root=self.ws_path,
        )

        self.assertEqual(prop.proposal_id, "prop_ttl_test_01")
        self.assertEqual(prop.status, "pending")
        self.assertFalse(prop.is_expired)
        self.assertGreater(prop.expires_in, 0.0)

        fetched = self.registry.get_proposal("prop_ttl_test_01")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.proposal_id, "prop_ttl_test_01")

        # Wait for TTL to elapse
        time.sleep(0.06)

        expired_fetch = self.registry.get_proposal("prop_ttl_test_01")
        self.assertIsNotNone(expired_fetch)
        self.assertTrue(expired_fetch.is_expired)
        self.assertEqual(expired_fetch.status, "expired")
        self.assertEqual(self.registry.get_pending_count(), 0)

        # Verify capacity bounding pruning
        reg = PatchProposalRegistry(max_proposals=5, ttl_seconds=60.0)
        for i in range(7):
            reg.register_proposal(
                file_path="jarvis/skills/math_calc.py",
                node_start_line=1,
                node_end_line=3,
                replacement_code=f"# patch {i}\n",
                target_test_module="tests/test_math.py",
                sha256_before=self.sha256_before,
                sha256_shadow="shadow",
                unified_diff="diff",
                report=rep,
                proposal_id=f"prop_bound_{i}",
                workspace_root=self.ws_path,
            )
        self.assertLessEqual(len(reg._proposals), 5)

    def test_02_get_proposals_lists_pending(self) -> None:
        """Verifies GET /api/evolution/proposals returns pending proposals with accurate counts."""
        rep = self._create_dummy_report(passed=True)
        self.registry.register_proposal(
            file_path="jarvis/skills/math_calc.py",
            node_start_line=1,
            node_end_line=3,
            replacement_code="def calculate_tax(amount: float) -> float:\n    return amount * 0.15\n",
            target_test_module="tests/test_math.py",
            sha256_before=self.sha256_before,
            sha256_shadow="shadow_hash_1",
            unified_diff="--- a\n+++ b\n",
            report=rep,
            proposal_id="prop_list_01",
            workspace_root=self.ws_path,
        )
        self.registry.register_proposal(
            file_path="jarvis/skills/math_calc.py",
            node_start_line=1,
            node_end_line=3,
            replacement_code="def calculate_tax(amount: float) -> float:\n    return amount * 0.20\n",
            target_test_module="tests/test_math.py",
            sha256_before=self.sha256_before,
            sha256_shadow="shadow_hash_2",
            unified_diff="--- a\n+++ b\n",
            report=rep,
            proposal_id="prop_list_02",
            workspace_root=self.ws_path,
        )

        res = self.client.get(
            "/api/evolution/proposals",
            headers={"X-Jarvis-Token": self.token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["pending_count"], 2)
        self.assertEqual(len(data["proposals"]), 2)

        prop_dict = data["proposals"][0]
        for key in (
            "proposal_id",
            "file_path",
            "sha256_before",
            "sha256_shadow",
            "unified_diff",
            "report",
            "expires_in",
            "status",
        ):
            self.assertIn(key, prop_dict)

    def test_03_get_proposals_unauthenticated_rejected_401(self) -> None:
        """Verifies unauthenticated access to /api/evolution/proposals fails closed with 401."""
        # Missing token
        res_none = self.client.get("/api/evolution/proposals")
        self.assertEqual(res_none.status_code, 401)
        self.assertIn("Unauthorized", res_none.json().get("error", ""))

        # Invalid token
        res_bad = self.client.get(
            "/api/evolution/proposals",
            headers={"X-Jarvis-Token": "invalid_credentials"},
        )
        self.assertEqual(res_bad.status_code, 401)

    def test_04_approve_proposal_promotes_live_file_atomically(self) -> None:
        """Verifies approving a proposal promotes code atomically to the live file."""
        rep = self._create_dummy_report(passed=True)
        replacement = "def calculate_tax(amount: float) -> float:\n    return amount * 0.15\n"
        self.registry.register_proposal(
            file_path=str(self.sample_file),
            node_start_line=1,
            node_end_line=3,
            replacement_code=replacement,
            target_test_module="tests/test_math.py",
            sha256_before=self.sha256_before,
            sha256_shadow="shadow_hash",
            unified_diff="--- a\n+++ b\n",
            report=rep,
            proposal_id="prop_approve_04",
            workspace_root=self.ws_path,
        )

        # Approve proposal
        with patch.object(self.registry, "approve_proposal", wraps=self.registry.approve_proposal) as mock_approve:
            res = self.client.post(
                "/api/evolution/proposals/prop_approve_04/approve",
                headers={"X-Jarvis-Token": self.token},
            )
            mock_approve.assert_called_once()

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "approved")
        self.assertTrue(data["promoted"])

        # Check live file on disk was replaced
        updated_content = self.sample_file.read_text(encoding="utf-8")
        self.assertEqual(updated_content, replacement)

        # Check proposal status
        p = self.registry.get_proposal("prop_approve_04")
        self.assertIsNotNone(p)
        self.assertEqual(p.status, "approved")

    def test_05_approve_proposal_optimistic_concurrency_conflict_409(self) -> None:
        """Verifies altering the live file before approval triggers 409 Conflict with zero overwrites."""
        rep = self._create_dummy_report(passed=True)
        replacement = "def calculate_tax(amount: float) -> float:\n    return amount * 0.15\n"
        self.registry.register_proposal(
            file_path=str(self.sample_file),
            node_start_line=1,
            node_end_line=3,
            replacement_code=replacement,
            target_test_module="tests/test_math.py",
            sha256_before=self.sha256_before,
            sha256_shadow="shadow_hash",
            unified_diff="--- a\n+++ b\n",
            report=rep,
            proposal_id="prop_conflict_05",
            workspace_root=self.ws_path,
        )

        # Simulate drift / external edit on disk
        drifted_content = "# External edit by another process\ndef calculate_tax(amount):\n    return amount\n"
        self.sample_file.write_text(drifted_content, encoding="utf-8")
        drifted_hash = hashlib.sha256(self.sample_file.read_bytes()).hexdigest()

        res = self.client.post(
            "/api/evolution/proposals/prop_conflict_05/approve",
            headers={"X-Jarvis-Token": self.token},
        )
        self.assertEqual(res.status_code, 409)
        data = res.json()
        self.assertEqual(data["status"], "conflict")
        self.assertEqual(data["sha256_expected"], self.sha256_before)
        self.assertEqual(data["sha256_actual"], drifted_hash)

        # Verify disk content remained intact (zero writes)
        self.assertEqual(self.sample_file.read_text(encoding="utf-8"), drifted_content)

        # Verify proposal marked conflict
        p = self.registry.get_proposal("prop_conflict_05")
        self.assertIsNotNone(p)
        self.assertEqual(p.status, "conflict")

    def test_06_approve_proposal_without_token_rejected_401(self) -> None:
        """Verifies unauthenticated or cross-origin requests to approve return 401 or 403."""
        # 1. Missing token -> 401
        res_unauth = self.client.post("/api/evolution/proposals/dummy_id/approve")
        self.assertEqual(res_unauth.status_code, 401)

        # 2. Bad token -> 401
        res_bad = self.client.post(
            "/api/evolution/proposals/dummy_id/approve",
            headers={"X-Jarvis-Token": "bogus_token"},
        )
        self.assertEqual(res_bad.status_code, 401)

        # 3. Malicious Origin header -> 403
        res_origin = self.client.post(
            "/api/evolution/proposals/dummy_id/approve",
            headers={
                "X-Jarvis-Token": self.token,
                "Origin": "http://evil-attacker.example.com",
            },
        )
        self.assertEqual(res_origin.status_code, 403)

    def test_07_reject_proposal_marks_status_rejected(self) -> None:
        """Verifies POST to reject transitions proposal to rejected state with reason."""
        rep = self._create_dummy_report(passed=True)
        self.registry.register_proposal(
            file_path="jarvis/skills/math_calc.py",
            node_start_line=1,
            node_end_line=3,
            replacement_code="# reject me\n",
            target_test_module="tests/test_math.py",
            sha256_before=self.sha256_before,
            sha256_shadow="shadow_hash",
            unified_diff="--- a\n+++ b\n",
            report=rep,
            proposal_id="prop_reject_07",
            workspace_root=self.ws_path,
        )

        res = self.client.post(
            "/api/evolution/proposals/prop_reject_07/reject",
            headers={"X-Jarvis-Token": self.token},
            json={"reason": "Operator determined replacement is suboptimal"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["reason"], "Operator determined replacement is suboptimal")

        # Verify registry record
        p = self.registry.get_proposal("prop_reject_07")
        self.assertIsNotNone(p)
        self.assertEqual(p.status, "rejected")
        self.assertEqual(p.rejection_reason, "Operator determined replacement is suboptimal")
        self.assertEqual(self.registry.get_pending_count(), 0)

    def test_08_forbidden_module_proposal_rejected_at_registration(self) -> None:
        """Targeting security-critical infrastructure modules must fail closed with SecurityBoundaryViolation."""
        forbidden_targets = [
            "jarvis/core/safety.py",
            "jarvis/core/security_boundaries.py",
            "jarvis/core/evolution/appcontainer.py",
            "jarvis/core/evolution/sandbox.py",
        ]
        rep = self._create_dummy_report(passed=True)
        for f_target in forbidden_targets:
            dummy_target = self.ws_path / f_target
            dummy_target.parent.mkdir(parents=True, exist_ok=True)
            dummy_target.write_text("# dummy", encoding="utf-8")

            with self.assertRaises(SecurityBoundaryViolation):
                self.registry.register_proposal(
                    file_path=f_target,
                    node_start_line=1,
                    node_end_line=2,
                    replacement_code="# evil replacement\n",
                    target_test_module="tests/test_safety.py",
                    sha256_before="dummy",
                    sha256_shadow="dummy",
                    unified_diff="--- a\n+++ b\n",
                    report=rep,
                    workspace_root=self.ws_path,
                )

    def test_09_hud_html_zero_cls_patches_elements(self) -> None:
        """Verifies hud.html defines fixed-geometry #evolutionPendingBadge, #tabPatchesBtn, and diff viewer."""
        hud_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        self.assertTrue(hud_path.exists())
        hud_html = hud_path.read_text(encoding="utf-8")

        # 1. Badge element in top-bar
        self.assertIn('id="evolutionPendingBadge"', hud_html)
        self.assertIn('data-action="open-patches-drawer"', hud_html)
        self.assertIn("tabular-nums inline-block", hud_html)

        # 2. Drawer tab button
        self.assertIn('id="tabPatchesBtn"', hud_html)
        self.assertIn('data-action="switch-drawer-tab"', hud_html)
        self.assertIn('data-tab="patches"', hud_html)
        self.assertIn('id="drawerPatchesCount"', hud_html)

        # 3. JavaScript render and action handlers
        self.assertIn("function renderPatchesTab()", hud_html)
        self.assertIn("function formatUnifiedDiff(", hud_html)
        self.assertIn("function executePatchApprove(", hud_html)
        self.assertIn("function executePatchReject(", hud_html)
        self.assertIn("refreshPatchesBadge()", hud_html)

        # 4. Invariant: No inline onclick in patches drawer markup
        self.assertNotIn("onclick=\"executePatchApprove", hud_html)
        self.assertNotIn("onclick=\"executePatchReject", hud_html)

    def test_10_websocket_broadcast_on_proposal_ready_and_resolved(self) -> None:
        """Verifies JarvisUIService.broadcast is invoked on patch_proposal_ready and patch_proposal_resolved."""
        mock_service = MagicMock()
        mock_service.broadcast = MagicMock()
        mock_service.broadcast_message = MagicMock()

        # 1. Test proposal_ready broadcast from propose_and_verify_code_patch
        mock_seq = [
            (0, "All checks passed!", ""),
            (0, "ALL DIAGNOSTICS PASSED", ""),
            (0, "1 passed in 0.05s", ""),
        ]
        with (
            patch("jarvis.ui.server.JarvisUIService.get_instance", return_value=mock_service),
            patch("jarvis.skills.ast_code_surgeon._run_sandboxed_command", side_effect=mock_seq),
        ):
            res = propose_and_verify_code_patch(
                file_path=str(self.sample_file),
                node_start_line=1,
                node_end_line=3,
                replacement_code="def calculate_tax(amount: float) -> float:\n    return amount * 0.15\n",
                target_test_module="tests/test_math.py",
                workspace_root=str(self.ws_path),
            )
            self.assertEqual(res.get("status"), "verified")
            mock_service.broadcast.assert_called()
            ready_call_arg = mock_service.broadcast.call_args[0][0]
            self.assertEqual(ready_call_arg.get("type"), "patch_proposal_ready")

        # 2. Test proposal_resolved broadcast on approve
        prop_id = res.get("proposal_id")
        self.assertIsNotNone(prop_id)

        # Call approve route on service with broadcast mock
        with patch.object(self.service, "broadcast") as mock_ws_broadcast:
            approve_res = self.client.post(
                f"/api/evolution/proposals/{prop_id}/approve",
                headers={"X-Jarvis-Token": self.token},
            )
            self.assertEqual(approve_res.status_code, 200)
            resolved_calls = [
                c[0][0] for c in mock_ws_broadcast.call_args_list
                if isinstance(c[0][0], dict) and c[0][0].get("type") == "patch_proposal_resolved"
            ]
            self.assertEqual(len(resolved_calls), 1)
            self.assertEqual(resolved_calls[0]["data"]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
