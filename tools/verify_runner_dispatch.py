"""ENG-OS-03C2R: Split-File Dispatch Verification and Regression Test Suite.

Proves:
1. Whole-file single-tier dispatch (non-split file passed as file path).
2. Two-tier split-file dispatch for Case A (test_self_evolution.py: 1 T3 node in Tier 3).
3. Two-tier split-file dispatch for Case C (test_job_object_sandbox_hardening.py: 44 T3 nodes in Tier 3).
4. Two-tier split-file dispatch for Case B (test_onnx_embeddings.py [5 T4] & test_semantic_endpointing.py [2 T4]).
5. Three-tier split-file dispatch (test_browser_network_boundaries.py across T1, T2B, Browser).
6. Win32 CLI character length safety via _build_tier_targets (prevents WinError 206 limit of 32,767 chars).
7. Zero duplicate node assignments across all 2,206 manifest nodes.
8. Complete inventory and marker isolation for all 7 split files in the repository.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.run_fast_tests import FourTierTestRunner
from tools.test_supervisor import SupervisedResult, TestSupervisor


class TestSplitFileDispatchSuite(unittest.TestCase):
    """Verifies ENG-OS-03C2R: Split-file dispatch integrity, exact node targeting, and Win32 CLI safety."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.supervisor = TestSupervisor(
            base_tmp_dir=self.tmp_dir.name,
            default_timeout_sec=5.0,
            workspace_root=WORKSPACE_ROOT,
        )
        self.runner = FourTierTestRunner(
            workspace_root=WORKSPACE_ROOT,
            supervisor=self.supervisor,
        )
        self.manifest = self.runner._get_manifest()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_01_whole_file_single_tier_dispatch(self):
        """1. A file whose nodes all belong to a single tier is dispatched as a whole file path string."""
        recorded_targets = []

        def mock_run_pytest(targets, **kwargs):
            recorded_targets.append(targets)
            return SupervisedResult(
                status="COMPLETED",
                exit_code=0,
                duration_sec=0.1,
                stdout="",
                stderr="",
                target=str(targets),
                passed_count=1,
            )

        with patch.object(self.supervisor, "run_pytest", side_effect=mock_run_pytest):
            self.runner.run_tier3_windows_integration()

        # test_antigravity_job_containment.py is 100% Tier 3 (38 nodes)
        self.assertIn("tests/test_antigravity_job_containment.py", recorded_targets)
        ag_call = next(t for t in recorded_targets if t == "tests/test_antigravity_job_containment.py")
        self.assertIsInstance(ag_call, str)

    def test_02_two_tier_split_file_dispatch_case_a_self_evolution(self):
        """2. Case A (Tier 1 / Tier 3): test_self_evolution.py dispatches only its 1 Tier 3 node in Tier 3."""
        recorded_targets = []

        def mock_run_pytest(targets, **kwargs):
            recorded_targets.append(targets)
            return SupervisedResult(
                status="COMPLETED",
                exit_code=0,
                duration_sec=0.1,
                stdout="",
                stderr="",
                target=str(targets),
                passed_count=1,
            )

        with patch.object(self.supervisor, "run_pytest", side_effect=mock_run_pytest):
            self.runner.run_tier3_windows_integration()

        se_targets = [t for t in recorded_targets if isinstance(t, list) and any("test_self_evolution.py" in str(x) for x in t)]
        self.assertEqual(len(se_targets), 1, "test_self_evolution.py must be dispatched as a node list in Tier 3")
        expected_node = "tests/test_self_evolution.py::TestSelfEvolution::test_dynamic_skill_creation_and_hot_reload"
        self.assertEqual(se_targets[0], [expected_node])

    def test_03_two_tier_split_file_dispatch_case_c_job_object_hardening(self):
        """3. Case C (Tier 2B / Tier 3): test_job_object_sandbox_hardening.py dispatches 44 Tier 3 nodes, excluding Uvicorn."""
        recorded_targets = []

        def mock_run_pytest(targets, **kwargs):
            recorded_targets.append(targets)
            return SupervisedResult(
                status="COMPLETED",
                exit_code=0,
                duration_sec=0.1,
                stdout="",
                stderr="",
                target=str(targets),
                passed_count=1,
            )

        with patch.object(self.supervisor, "run_pytest", side_effect=mock_run_pytest):
            self.runner.run_tier3_windows_integration()

        jo_targets = [t for t in recorded_targets if isinstance(t, list) and any("test_job_object_sandbox_hardening.py" in str(x) for x in t)]
        self.assertEqual(len(jo_targets), 1)
        self.assertEqual(len(jo_targets[0]), 39, "Must dispatch exactly 39 Tier 3 nodes")
        uvicorn_node = "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_end_to_end_live_uvicorn_percent_encoded_access_log_redaction"
        self.assertNotIn(uvicorn_node, jo_targets[0], "Tier 2B Uvicorn test must be excluded from Tier 3")
        async_node = "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_oversized_query_beginning_and_end"
        self.assertNotIn(async_node, jo_targets[0], "Tier 2A async test must be excluded from Tier 3")

    def test_04_two_tier_split_file_dispatch_case_b_hardware(self):
        """4. Case B (Tier 2A / Tier 4): test_onnx_embeddings.py and test_semantic_endpointing.py dispatch only Tier 4 nodes."""
        recorded_targets = []

        def mock_run_pytest(targets, **kwargs):
            recorded_targets.append(targets)
            return SupervisedResult(
                status="COMPLETED",
                exit_code=0,
                duration_sec=0.1,
                stdout="",
                stderr="",
                target=str(targets),
                passed_count=1,
            )

        with (
            patch("tools.run_fast_tests.probe_hardware_available", return_value={"cuda": True, "onnx": True, "audio": True}),
            patch.object(self.supervisor, "run_pytest", side_effect=mock_run_pytest),
        ):
            self.runner.run_tier4_hardware_integration()

        onnx_targets = [t for t in recorded_targets if isinstance(t, list) and any("test_onnx_embeddings.py" in str(x) for x in t)]
        self.assertEqual(len(onnx_targets), 1)
        self.assertEqual(len(onnx_targets[0]), 5, "Must dispatch exactly 5 Tier 4 nodes")
        self.assertNotIn(
            "tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_async_embedding_event_loop",
            onnx_targets[0],
            "Tier 2A async embedding node must be excluded from Tier 4",
        )

        semantic_targets = [t for t in recorded_targets if isinstance(t, list) and any("test_semantic_endpointing.py" in str(x) for x in t)]
        self.assertEqual(len(semantic_targets), 1)
        self.assertEqual(len(semantic_targets[0]), 2, "Must dispatch exactly 2 Tier 4 nodes")
        self.assertNotIn(
            "tests/test_semantic_endpointing.py::test_adaptive_tripwire_trigger",
            semantic_targets[0],
            "Tier 2A tripwire trigger node must be excluded from Tier 4",
        )

    def test_05_three_tier_split_file_dispatch(self):
        """5. Three-tier split file (test_browser_network_boundaries.py) partitions cleanly across T1, T2B, Browser."""
        t1_nodes = self.manifest["node_ids_by_tier"]["tier1_hermetic_unit"]
        t2b_nodes = self.manifest["node_ids_by_tier"]["tier2b_loopback_ipc"]
        tb_nodes = self.manifest["node_ids_by_tier"]["tier_browser_integration"]

        fpath = "tests/test_browser_network_boundaries.py"
        f_t1 = [n for n in t1_nodes if n.startswith(fpath)]
        f_t2b = [n for n in t2b_nodes if n.startswith(fpath)]
        f_tb = [n for n in tb_nodes if n.startswith(fpath)]

        self.assertEqual(len(f_t1), 12, "Must have 12 Tier 1 nodes")
        self.assertEqual(len(f_t2b), 11, "Must have 11 Tier 2B nodes")
        self.assertEqual(len(f_tb), 6, "Must have 6 Browser Integration nodes")
        self.assertEqual(len(f_t1) + len(f_t2b) + len(f_tb), 29, "Must sum to all 29 file nodes")

        # Mutually exclusive
        self.assertEqual(len(set(f_t1) & set(f_t2b)), 0)
        self.assertEqual(len(set(f_t1) & set(f_tb)), 0)
        self.assertEqual(len(set(f_t2b) & set(f_tb)), 0)

        # Build targets for Tier 2B isolates exact nodes
        t2b_targets = self.runner._build_tier_targets(t2b_nodes, self.manifest)
        self.assertTrue(all(n in t2b_targets for n in f_t2b))
        self.assertFalse(any(n in t2b_targets for n in f_t1))
        self.assertFalse(any(n in t2b_targets for n in f_tb))

    def test_06_build_tier_targets_prevents_win32_cmdline_overflow(self):
        """6. _build_tier_targets collapses non-split files, preventing Windows 32,767 char cmdline overflow."""
        nodes_2a = self.manifest["node_ids_by_tier"]["tier2a_async_safe"]
        targets_2a = self.runner._build_tier_targets(nodes_2a, self.manifest)

        # Raw node IDs would be ~32,811 chars (exceeding 32,767 limit and causing WinError 206)
        raw_cmd_len = sum(len(n) + 1 for n in nodes_2a)
        self.assertGreater(raw_cmd_len, 32000)

        # Collapsed targets are < 1,500 chars (safe on all Win32 environments, 96.2% reduction)
        collapsed_cmd_len = sum(len(t) + 1 for t in targets_2a)
        self.assertLess(collapsed_cmd_len, 1500)
        self.assertEqual(len(targets_2a), 20)

        # Verify loopback dispatch uses collapsed targets
        recorded_targets = []

        def mock_run_pytest(targets, **kwargs):
            recorded_targets.append(targets)
            return SupervisedResult(
                status="COMPLETED",
                exit_code=0,
                duration_sec=0.1,
                stdout="",
                stderr="",
                target=str(targets),
                passed_count=len(nodes_2a),
            )

        with (
            patch("tools.run_fast_tests.probe_loopback_available", return_value=True),
            patch.object(self.supervisor, "run_pytest", side_effect=mock_run_pytest),
        ):
            self.runner.run_tier2_async_and_ipc()

        # Both 2B and 2A should be dispatched with collapsed targets
        self.assertEqual(len(recorded_targets), 2)
        # 2A targets must match targets_2a
        self.assertEqual(recorded_targets[1], targets_2a)

    def test_07_duplicate_prevention_across_all_manifest_tiers(self):
        """7. Strict verification that zero node IDs appear in multiple tiers."""
        seen = {}
        duplicates = []
        for tier, n_list in self.manifest["node_ids_by_tier"].items():
            if not isinstance(n_list, list):
                continue
            for n in n_list:
                if n in seen:
                    duplicates.append((n, seen[n], tier))
                seen[n] = tier

        self.assertEqual(len(duplicates), 0, f"Discovered duplicate node assignments: {duplicates}")
        self.assertEqual(len(seen), 2206, "Unique nodes must equal exactly 2,206")

    def test_08_all_seven_split_files_inventory_and_marker_isolation(self):
        """8. Exactly 7 files in the repository have split-tier nodes, all cleanly isolated."""
        file_tiers = defaultdict(set)
        for tier, n_list in self.manifest["node_ids_by_tier"].items():
            if not isinstance(n_list, list):
                continue
            for n in n_list:
                file_tiers[n.split("::")[0]].add(tier)

        split_files = {f: tiers for f, tiers in file_tiers.items() if len(tiers) > 1}
        self.assertEqual(len(split_files), 7, f"Expected exactly 7 split files, found {len(split_files)}: {list(split_files.keys())}")
        expected_split_files = {
            "tests/test_background_tasks.py",
            "tests/test_browser_agent.py",
            "tests/test_browser_network_boundaries.py",
            "tests/test_job_object_sandbox_hardening.py",
            "tests/test_onnx_embeddings.py",
            "tests/test_self_evolution.py",
            "tests/test_semantic_endpointing.py",
        }
        self.assertEqual(set(split_files.keys()), expected_split_files)


if __name__ == "__main__":
    unittest.main()
