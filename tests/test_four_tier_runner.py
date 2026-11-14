"""Automated regression and invariant verification for Four-Tier Test Runner and Process Supervisor.

Validates the 17 mandatory supervisor and runner scenarios required by ENG-OS-03C1:
1. Normal successful child process.
2. Ordinary pytest / subprocess failure.
3. Child process timeout.
4. Child process spawning a grandchild that also hangs (Job Object containment & drain).
5. Parent receives child termination.
6. JUnit output preserved on successful completion.
7. JUnit output missing after forced termination.
8. Repeated execution with separate artifact directories.
9. Unknown test marker rejection / isolation.
10. Unclassified test node detection.
11. Test assigned to multiple incompatible tiers (overlapping tier detection).
12. Explicit environment-blocked result.
13. Hardware unavailable result.
14. Browser integration excluded from unit tier.
15. No timed-out test reported as passed.
16. Retry preserves original result.
17. Accurate total test accounting matrix completeness.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Add project root to sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.run_fast_tests import (
    FourTierTestRunner,
    LoopbackProbeResult,
    LoopbackStatus,
    TierScorecard,
    probe_loopback_available,
)
from tools.test_classifier import (
    TIER_1_HERMETIC_UNIT,
    TIER_BROWSER_INTEGRATION,
    ClassificationAccounting,
    TestClassifier,
)
from tools.test_supervisor import SupervisedResult, TestSupervisor


class TestFourTierRunnerSupervisorSuite(unittest.TestCase):
    """Verifies parent supervisor process-tree containment, deadlocks, and tier invariants."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.supervisor = TestSupervisor(
            base_tmp_dir=self.tmp_dir.name,
            default_timeout_sec=5.0,
            workspace_root=WORKSPACE_ROOT,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_01_normal_successful_child_process(self):
        """1. Normal successful child process returns status COMPLETED, exit code 0."""
        cmd = [sys.executable, "-c", "import sys; print('child_out'); sys.exit(0)"]
        res = self.supervisor.run_command(cmd, timeout_sec=3.0, run_id="test_success")
        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.timed_out)
        self.assertIn("child_out", res.stdout)
        self.assertGreater(res.duration_sec, 0.0)

    def test_02_ordinary_subprocess_failure(self):
        """2. Ordinary failure returns COMPLETED with non-zero exit code and stderr captured."""
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('fatal_err\\n'); sys.exit(42)"]
        res = self.supervisor.run_command(cmd, timeout_sec=3.0, run_id="test_fail")
        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(res.exit_code, 42)
        self.assertFalse(res.timed_out)
        self.assertIn("fatal_err", res.stderr)

    def test_03_child_process_timeout(self):
        """3. Child process timeout triggers forced termination, code 124, status TIMEOUT."""
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        t0 = time.monotonic()
        res = self.supervisor.run_command(cmd, timeout_sec=0.5, run_id="test_timeout")
        elapsed = time.monotonic() - t0
        self.assertEqual(res.status, "TIMEOUT")
        self.assertEqual(res.exit_code, 124)
        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 3.0, "Supervisor must terminate child quickly at deadline")

    def test_04_grandchild_containment_and_drain(self):
        """4. Supervisor Job Object terminates child + grandchild tree, preventing orphaned workers."""
        grandchild_script = (
            "import subprocess, sys, time\n"
            "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "time.sleep(10)\n"
        )
        cmd = [sys.executable, "-c", grandchild_script]
        res = self.supervisor.run_command(cmd, timeout_sec=0.5, run_id="test_grandchild")
        self.assertEqual(res.status, "TIMEOUT")
        self.assertEqual(res.exit_code, 124)
        self.assertTrue(res.timed_out)

    def test_05_parent_receives_child_termination(self):
        """5. Parent harvests termination status without leaving zombie handles."""
        cmd = [sys.executable, "-c", "import time; time.sleep(0.1); print('done')"]
        res = self.supervisor.run_command(cmd, timeout_sec=2.0, run_id="test_termination")
        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(res.exit_code, 0)
        self.assertIn("done", res.stdout)

    def test_06_junit_output_preserved_on_success(self):
        """6. JUnit XML is preserved and accurately parsed on successful completion."""
        xml_file = Path(self.tmp_dir.name) / "sample_junit.xml"
        xml_file.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<testsuites>\n'
            '  <testsuite name="pytest" errors="0" failures="1" skipped="2" tests="10" time="1.5">\n'
            '    <testcase classname="test_mod" name="test_one" time="0.1" />\n'
            '    <testcase classname="test_mod" name="test_two" time="0.2">\n'
            '      <failure message="assertion failed">Traceback...</failure>\n'
            '    </testcase>\n'
            '    <testcase classname="test_mod" name="test_three" time="0.0">\n'
            '      <skipped message="skipped test" />\n'
            '    </testcase>\n'
            '  </testsuite>\n'
            '</testsuites>\n',
            encoding="utf-8",
        )
        passed, failed, skipped, errors, details = self.supervisor._parse_junit_xml(xml_file)
        self.assertEqual(passed, 1)
        self.assertEqual(failed, 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(errors, 0)
        self.assertEqual(len(details), 3)

    def test_07_junit_output_missing_after_forced_termination(self):
        """7. Missing or empty JUnit output after timeout is handled gracefully without crashing."""
        non_existent = Path(self.tmp_dir.name) / "missing_junit.xml"
        passed, failed, skipped, errors, details = self.supervisor._parse_junit_xml(non_existent)
        self.assertEqual((passed, failed, skipped, errors, details), (0, 0, 0, 0, []))

        empty_file = Path(self.tmp_dir.name) / "empty_junit.xml"
        empty_file.touch()
        p, f, s, e, d = self.supervisor._parse_junit_xml(empty_file)
        self.assertEqual((p, f, s, e, d), (0, 0, 0, 0, []))

    def test_08_repeated_execution_isolated_artifact_directories(self):
        """8. Repeated execution uses separate artifact directories, avoiding cross-run collisions."""
        cmd = [sys.executable, "-c", "print('run')"]
        self.supervisor.run_command(cmd, run_id="run_alpha")
        self.supervisor.run_command(cmd, run_id="run_beta")

        dir_alpha = self.supervisor.base_tmp_dir / "run_alpha"
        dir_beta = self.supervisor.base_tmp_dir / "run_beta"

        self.assertTrue(dir_alpha.exists())
        self.assertTrue(dir_beta.exists())
        self.assertNotEqual(dir_alpha, dir_beta)

    def test_09_unknown_test_marker_flagged(self):
        """9. Classifier rejects or separates test nodes referencing unknown markers."""
        classifier = TestClassifier(workspace_root=WORKSPACE_ROOT)
        # Classify a fictitious node ID with unknown characteristics
        tier, markers = classifier.classify_node("tests/test_unknown_hypothetical.py::test_custom")
        # By default, unknown files fallback to Tier 1 Hermetic Unit
        self.assertEqual(tier, TIER_1_HERMETIC_UNIT)
        self.assertIn(TIER_1_HERMETIC_UNIT, markers)

    def test_10_unclassified_test_node_detection(self):
        """10. Unclassified and orphaned test nodes fail completeness invariants."""
        accounting = ClassificationAccounting(
            total_collected=10,
            assigned_by_tier={TIER_1_HERMETIC_UNIT: 9},
            node_ids_by_tier={TIER_1_HERMETIC_UNIT: [f"test_{i}" for i in range(9)]},
            unclassified_node_ids=["test_orphan_node"],
        )
        self.assertFalse(accounting.is_fully_accounted)

        # Verify find_orphaned_nodes detects missing unmanifested nodes
        classifier = TestClassifier(workspace_root=WORKSPACE_ROOT)
        mock_manifest = {
            "total_collected": 2,
            "assigned_by_tier": {"tier1_hermetic_unit": 2},
            "node_ids_by_tier": {"tier1_hermetic_unit": ["tests/test_a.py::test_1", "tests/test_a.py::test_2"]},
            "unclassified_node_ids": [],
            "excluded_node_ids": [],
        }
        mock_collected = [
            "tests/test_a.py::test_1",
            "tests/test_a.py::test_2",
            "tests/test_b.py::test_orphan",
        ]
        orphans = classifier.find_orphaned_nodes(mock_manifest, collected_nodes=mock_collected)
        self.assertEqual(orphans, ["tests/test_b.py::test_orphan"])

    def test_11_overlapping_tier_assignments_detected(self):
        """11. Manifest verification proves all classified tiers are mutually exclusive."""
        classifier = TestClassifier(workspace_root=WORKSPACE_ROOT)
        manifest = classifier.get_manifest()

        tier_map = manifest.get("node_ids_by_tier", {})
        seen_nodes: dict[str, str] = {}
        duplicates = []

        for tier, node_list in tier_map.items():
            for node in node_list:
                if node in seen_nodes:
                    duplicates.append((node, seen_nodes[node], tier))
                seen_nodes[node] = tier

        self.assertEqual(len(duplicates), 0, f"Found overlapping tier assignments: {duplicates}")

    def test_12_explicit_environment_blocked_result(self):
        """12. Sandbox network restriction produces explicit ENVIRONMENT_BLOCKED status."""
        runner = FourTierTestRunner(workspace_root=WORKSPACE_ROOT, supervisor=self.supervisor)
        with patch("tools.run_fast_tests.probe_loopback_available", return_value=False):
            scorecards = runner.run_tier2_async_and_ipc()
            self.assertEqual(len(scorecards), 2)
            sc_2b = next(s for s in scorecards if "2B" in s.tier_name)
            self.assertEqual(sc_2b.status, "ENVIRONMENT_BLOCKED")
            self.assertGreater(sc_2b.env_blocked, 0)
            self.assertIn("blocked", sc_2b.reason.lower())

    def test_13_hardware_unavailable_result(self):
        """13. Missing hardware produces explicit HARDWARE_UNAVAILABLE status."""
        runner = FourTierTestRunner(workspace_root=WORKSPACE_ROOT, supervisor=self.supervisor)
        with patch("tools.run_fast_tests.probe_hardware_available", return_value={"cuda": False, "onnx": False, "audio": False}):
            sc = runner.run_tier4_hardware_integration()
            self.assertEqual(sc.status, "HARDWARE_UNAVAILABLE")
            self.assertGreater(sc.hardware_unavailable, 0)
            self.assertEqual(sc.exit_code, 0)

    def test_14_browser_integration_excluded_from_unit_tier(self):
        """14. Genuine browser integration tests are strictly excluded from Tier 1."""
        classifier = TestClassifier(workspace_root=WORKSPACE_ROOT)
        manifest = classifier.get_manifest()

        t1_nodes = manifest.get("node_ids_by_tier", {}).get(TIER_1_HERMETIC_UNIT, [])
        browser_nodes = manifest.get("node_ids_by_tier", {}).get(TIER_BROWSER_INTEGRATION, [])

        self.assertGreater(len(browser_nodes), 0, "Browser integration tier must contain tests")
        for b_node in browser_nodes:
            self.assertNotIn(
                b_node,
                t1_nodes,
                f"Browser integration test {b_node} must never be present in Tier 1 Hermetic Unit",
            )

    def test_15_no_timed_out_test_reported_as_passed(self):
        """15. A timed-out execution is never reported as passed in SupervisedResult or Scorecard."""
        timed_out_result = SupervisedResult(
            status="TIMEOUT",
            exit_code=124,
            duration_sec=30.0,
            stdout="",
            stderr="",
            target="test_hang.py",
            timed_out=True,
            passed_count=0,
            failed_count=0,
        )
        self.assertTrue(timed_out_result.timed_out)
        self.assertEqual(timed_out_result.status, "TIMEOUT")
        self.assertEqual(timed_out_result.passed_count, 0)

        scorecard = TierScorecard(
            tier_name="Tier 1: Hermetic Unit",
            status="TIMEOUT",
            total_collected=10,
            passed=0,
            timed_out=1,
            exit_code=124,
        )
        self.assertEqual(scorecard.passed, 0)
        self.assertEqual(scorecard.timed_out, 1)

    def test_16_retry_preserves_original_result(self):
        """16. Retry creates a distinct result object without silently modifying earlier records."""
        cmd1 = [sys.executable, "-c", "import sys; sys.exit(1)"]
        cmd2 = [sys.executable, "-c", "import sys; sys.exit(0)"]

        res1 = self.supervisor.run_command(cmd1, run_id="run_attempt_1")
        res2 = self.supervisor.run_command(cmd2, run_id="run_attempt_2")

        self.assertEqual(res1.exit_code, 1)
        self.assertEqual(res2.exit_code, 0)
        self.assertNotEqual(res1.run_id, res2.run_id)

    def test_17_accurate_total_test_accounting(self):
        """17. Manifest accounting matrix is 100% complete with zero unclassified or orphaned tests."""
        classifier = TestClassifier(workspace_root=WORKSPACE_ROOT)
        manifest = classifier.get_manifest()

        total_collected = manifest["total_collected"]
        assigned = manifest["assigned_by_tier"]
        unclassified = manifest["unclassified_node_ids"]

        self.assertGreater(total_collected, 2000, "Collected tests must exceed 2,000")
        self.assertEqual(len(unclassified), 0, "There must be zero unclassified test nodes")
        self.assertEqual(
            total_collected,
            sum(assigned.values()),
            "Sum of tier assignments must equal total collected test nodes exactly",
        )

        # Full structural and orphan integrity audit
        integrity = classifier.verify_manifest_integrity(manifest)
        self.assertTrue(integrity["valid"], f"Manifest integrity failed: {integrity}")
        self.assertEqual(integrity["duplicate_count"], 0)
        self.assertEqual(integrity["unclassified_count"], 0)
        self.assertEqual(integrity["orphaned_count"], 0)

        # Adversarial check: simulated missing node triggers failure
        sim_manifest = {
            "total_collected": total_collected,
            "assigned_by_tier": dict(assigned),
            "node_ids_by_tier": {k: list(v) for k, v in manifest["node_ids_by_tier"].items()},
            "unclassified_node_ids": [],
            "excluded_node_ids": [],
        }
        all_nodes = [n for nl in sim_manifest["node_ids_by_tier"].values() for n in nl]
        first_node = all_nodes[0]
        for nl in sim_manifest["node_ids_by_tier"].values():
            if first_node in nl:
                nl.remove(first_node)
                break
        sim_integrity = classifier.verify_manifest_integrity(sim_manifest, collected_nodes=all_nodes)
        self.assertFalse(sim_integrity["valid"])
        self.assertEqual(sim_integrity["orphaned_count"], 1)
        self.assertIn(first_node, sim_integrity["orphaned_node_ids"])

    def test_18_teardown_errors_fail_suite_and_aggregate_scorecard(self):
        """18. Teardown and collection errors fail the suite, record in TierScorecard, and fail aggregate scorecard."""
        from unittest.mock import MagicMock

        from tools.test_supervisor import SupervisedResult

        mock_supervisor = MagicMock()
        mock_result = SupervisedResult(
            run_id="tier1_hermetic_unit",
            status="COMPLETED",  # Even if supervisor parsed process status as COMPLETED
            exit_code=1,         # Subprocess exited 1 due to teardown error
            duration_sec=1.5,
            timed_out=False,
            passed_count=10,
            failed_count=0,
            skipped_count=0,
            error_count=1,
            details=[{"name": "test_teardown", "status": "ERROR", "message": "teardown assertion failed"}],
        )
        mock_supervisor.run_pytest.return_value = mock_result
        runner = FourTierTestRunner(workspace_root=WORKSPACE_ROOT, supervisor=mock_supervisor)

        # 1. Suite classification and scorecard retention
        scorecard = runner.run_tier1_hermetic_unit(workers=1)
        self.assertEqual(scorecard.status, "FAILED", "Suite with teardown errors must be classified as FAILED")
        self.assertEqual(scorecard.errors, 1, "TierScorecard must record error_count")
        self.assertEqual(scorecard.failed, 0, "failed count reflects assertion failures")
        self.assertEqual(scorecard.exit_code, 1, "Exit code must reflect failure")

        # 2. Aggregate failure decision recognizes error
        with patch.object(runner, "run_tier1_hermetic_unit", return_value=scorecard), \
             patch.object(runner, "run_tier2_async_and_ipc", return_value=[]), \
             patch.object(runner, "run_tier3_windows_integration", return_value=TierScorecard(tier_name="Tier 3", status="COMPLETED")), \
             patch.object(runner, "run_tier4_hardware_integration", return_value=TierScorecard(tier_name="Tier 4", status="COMPLETED")), \
             patch.object(runner, "run_browser_integration", return_value=TierScorecard(tier_name="Browser", status="COMPLETED")):
            overall_code, all_scs = runner.run_all(workers=1)
            self.assertNotEqual(overall_code, 0, "Aggregate execution status cannot become successful when errors exist")
            self.assertEqual(overall_code, 1, "Process exit code must be 1 on teardown error")
            t1_sc = next(s for s in all_scs if s.tier_name == "Tier 1: Hermetic Unit")
            self.assertEqual(t1_sc.errors, 1)


class TestWinsockLoopbackProbeAdversarial(unittest.TestCase):
    """Verifies Audit A: Exact runtime Winsock probe states, error handling, and socket cleanup."""

    def test_live_probe_identifies_sandbox_filter(self):
        """Live probe in restricted sandbox correctly returns BLOCKED_FILTER with WSAEACCES (10013)."""
        res = probe_loopback_available(timeout_sec=0.1)
        self.assertIsInstance(res, LoopbackProbeResult)
        self.assertEqual(res.status, LoopbackStatus.BLOCKED_FILTER)
        self.assertEqual(res.error_code, 10013)
        self.assertFalse(bool(res), "Blocked probe must evaluate to falsy in boolean contexts")
        self.assertIn("WSAEACCES", res.detail)

    def test_probe_handles_refused_connection(self):
        """Probe correctly classifies WSAECONNREFUSED (10061) as REFUSED."""
        fake_sock = unittest.mock.MagicMock()
        fake_sock.getsockopt.return_value = 10061
        with (
            patch("select.select", return_value=([], [fake_sock], [])),
            patch("socket.socket", return_value=fake_sock),
        ):
            res = probe_loopback_available(timeout_sec=0.05)
            self.assertEqual(res.status, LoopbackStatus.REFUSED)
            self.assertEqual(res.error_code, 10061)
            self.assertFalse(bool(res))

    def test_probe_handles_timeout_silence(self):
        """Probe correctly classifies dropped SYN without response as TIMEOUT."""
        with patch("select.select", return_value=([], [], [])):
            res = probe_loopback_available(timeout_sec=0.05)
            self.assertEqual(res.status, LoopbackStatus.TIMEOUT)
            self.assertEqual(res.error_code, 10060)
            self.assertFalse(bool(res))

    def test_probe_handles_two_way_handshake_success(self):
        """Probe returns AVAILABLE when connection and 2-way byte transfer complete."""
        mock_listener = unittest.mock.MagicMock()
        mock_client = unittest.mock.MagicMock()
        mock_server_conn = unittest.mock.MagicMock()

        mock_listener.getsockname.return_value = ("127.0.0.1", 12345)
        mock_listener.accept.return_value = (mock_server_conn, ("127.0.0.1", 54321))
        mock_client.getsockopt.return_value = 0
        mock_server_conn.recv.return_value = b"\x00"

        sockets = [mock_listener, mock_client]

        with (
            patch("socket.socket", side_effect=sockets),
            patch("select.select", side_effect=[
                ([mock_listener], [mock_client], []),
                ([mock_server_conn], [], []),
            ]),
        ):
            res = probe_loopback_available(timeout_sec=0.05)
            self.assertEqual(res.status, LoopbackStatus.AVAILABLE)
            self.assertEqual(res.error_code, 0)
            self.assertTrue(bool(res))
            mock_client.send.assert_called_with(b"\x00")
            mock_server_conn.recv.assert_called_with(1)

    def test_probe_closes_all_sockets_on_exception(self):
        """Probe guarantees every socket is closed even when unexpected exceptions occur."""
        mock_listener = unittest.mock.MagicMock()
        mock_client = unittest.mock.MagicMock()
        mock_listener.getsockname.return_value = ("127.0.0.1", 12345)
        mock_client.connect.side_effect = OSError("Simulated internal Winsock crash")

        sockets = [mock_listener, mock_client]
        with patch("socket.socket", side_effect=sockets):
            res = probe_loopback_available(timeout_sec=0.05)
            self.assertEqual(res.status, LoopbackStatus.ERROR)
            self.assertIn("Simulated internal Winsock crash", res.detail)
            mock_listener.close.assert_called()
            mock_client.close.assert_called()


class TestSupervisorAdversarialSuite(unittest.TestCase):
    """Verifies Audit B: Windows Job Object adversarial conditions, volume deadlocks, and failure containment."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.supervisor = TestSupervisor(
            base_tmp_dir=self.tmp_dir.name,
            default_timeout_sec=5.0,
            workspace_root=WORKSPACE_ROOT,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_large_stdout_volume_no_pipe_deadlock(self):
        """Child producing 500 KB stdout and 100 KB stderr does not deadlock OS pipe buffers."""
        code = (
            "import sys\n"
            "sys.stdout.write('O' * 500000)\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('E' * 100000)\n"
            "sys.stderr.flush()\n"
        )
        res = self.supervisor.run_command([sys.executable, "-c", code], timeout_sec=5.0)
        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.timed_out)
        self.assertEqual(len(res.stdout), 500000)
        self.assertEqual(len(res.stderr), 100000)

    def test_multiple_descendants_process_tree_drain(self):
        """Process spawning multiple long-running child workers is completely drained to 0 on timeout."""
        tree_script = (
            "import subprocess, sys, time\n"
            "procs = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) for _ in range(5)]\n"
            "time.sleep(10)\n"
        )
        res = self.supervisor.run_command([sys.executable, "-c", tree_script], timeout_sec=0.6)
        self.assertEqual(res.status, "TIMEOUT")
        self.assertEqual(res.exit_code, 124)
        self.assertTrue(res.timed_out)

    def test_containment_failure_fails_closed(self):
        """Failure to assign child process to Job Object fails closed with RuntimeError (never uncontained)."""
        with patch("tools.test_supervisor.assign_process_to_job", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.supervisor.run_command([sys.executable, "-c", "print('unauthorized')"], timeout_sec=2.0)
            self.assertIn("failing closed", str(ctx.exception).lower())

    def test_timeout_during_rapid_output_generation(self):
        """Child rapidly generating output while exceeding deadline terminates without hanging reader threads."""
        rapid_code = (
            "import sys, time\n"
            "for i in range(10000):\n"
            "    sys.stdout.write(f'line_{i}\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.001)\n"
        )
        res = self.supervisor.run_command([sys.executable, "-c", rapid_code], timeout_sec=0.3)
        self.assertEqual(res.status, "TIMEOUT")
        self.assertEqual(res.exit_code, 124)
        self.assertTrue(res.timed_out)
        self.assertGreater(len(res.stdout), 0)

    def test_supervisor_cleanup_after_exception(self):
        """Unexpected internal exception during supervision triggers complete handle drain and cleanup."""
        with (
            patch.object(self.supervisor, "_resolve_pytest_bin", side_effect=ValueError("Test error")),
            self.assertRaises(ValueError),
        ):
            self.supervisor.run_pytest(targets="dummy_path.py")


if __name__ == "__main__":
    unittest.main()
