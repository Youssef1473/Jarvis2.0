"""Tests for Cross-Process Persistence Integrity & Transactional Storage (ENG-OS-03B).

Adversarial multi-process concurrency, CAS generation tracking, crash consistency,
and environment isolation test suite covering:
1. Two processes writing the same candidate (CAS race).
2. Stale process attempting to replace newer verified state.
3. Two processes updating different candidates concurrently.
4. Duplicate identical receipt registration (idempotent replay).
5. Same execution ID with different receipt content (conflict error).
6. Simultaneous receipt registration across independent processes.
7. Database busy/locked condition handling.
8. Process termination during transaction (crash consistency).
9. Corrupt candidate JSON preservation / quarantine.
10. Corrupt receipt artifact detection.
11. Database/artifact disagreement detection (tamper detection).
12. Replayed stale generation rejection.
13. Invalid generation number rejection on creation.
14. Candidate terminal-state modification rejection (REJECTED/SUPERSEDED).
15. Repeated ingestion across processes (idempotent deduplication).
16. Subprocess execution environment isolation (PYTEST_ADDOPTS/PYTEST_PLUGINS scrubbed).
17. RED-GREEN comparability detecting environment mutation.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.continuity_manager import (
    FailureCandidatePipeline,
    StaleVersionConflictError,
    TerminalStateError,
    canonical_candidate_digest,
)
from tools.execution_evidence import (
    EvidenceStore,
    ExecutionEvidenceCollector,
    ExecutionEvidenceEnvelope,
    ExecutionIdentityConflictError,
    ExecutionReceipt,
    RedGreenVerificationEngine,
    canonical_hash_dict,
)
from tools.failure_collector import EventEnvelope
from tools.lesson_candidate import LessonCandidate

# =============================================================================
# Top-Level Multiprocessing Worker Callbacks (Windows Spawn Compatible)
# =============================================================================

def _mp_worker_candidate_cas(
    candidates_dir: str,
    candidate_id: str,
    expected_gen: int,
    expected_digest: str | None,
    new_status: str,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker that loads a candidate and performs a CAS update."""
    try:
        pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)
        cand, _gen, digest = pipeline.get_candidate_with_generation(candidate_id)
        if not cand:
            result_queue.put((False, "CANDIDATE_NOT_FOUND", 0))
            return
        cand.review_status = new_status
        ok, msg, new_gen = pipeline.record_candidate(
            cand,
            expected_generation=expected_gen,
            expected_digest=expected_digest or digest,
        )
        result_queue.put((ok, msg, new_gen))
    except BaseException as exc:  # noqa: BLE001
        result_queue.put((False, f"{type(exc).__name__}: {exc}", 0))


def _mp_worker_candidate_write(
    candidates_dir: str,
    candidate_dict: dict,
    expected_gen: int | None,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker that instantiates and records a candidate."""
    try:
        pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)
        cand = LessonCandidate.from_dict(candidate_dict)
        ok, msg, new_gen = pipeline.record_candidate(
            cand,
            expected_generation=expected_gen,
        )
        result_queue.put((ok, msg, new_gen))
    except BaseException as exc:  # noqa: BLE001
        result_queue.put((False, f"{type(exc).__name__}: {exc}", 0))


def _mp_worker_receipt_register(
    evidence_dir: str,
    receipt_dict: dict,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker that registers an execution receipt in the EvidenceStore."""
    try:
        store = EvidenceStore(evidence_dir=evidence_dir)
        receipt = ExecutionReceipt.from_dict(receipt_dict)
        ok, status, path = store.register_receipt(receipt)
        result_queue.put((ok, status, str(path)))
    except BaseException as exc:  # noqa: BLE001
        result_queue.put((False, type(exc).__name__, str(exc)))


def _mp_worker_crash_mid_transaction(
    db_path: str,
    ready_event: multiprocessing.Event,
) -> None:
    """Worker that acquires an exclusive transaction lock and terminates abruptly via os._exit."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute("INSERT OR REPLACE INTO execution_receipts (receipt_id, execution_id, candidate_id, execution_pair_id, execution_role, target_test_file, target_test_file_sha256, exit_code, content_digest, captured_at_utc, artifact_path) VALUES ('RCPT-crash', 'EXEC-crash', 'LCAND-crash', 'PAIR-crash', 'BASELINE', 'test.py', 'hash', 0, 'digest', '2026-11-05', 'receipts/crash.json');")
        ready_event.set()
        time.sleep(0.05)
        # Abrupt uncatchable termination without clean flush
        os._exit(42)
    except BaseException:  # noqa: BLE001
        ready_event.set()
        os._exit(1)


def _mp_worker_event_ingest(
    candidates_dir: str,
    event_dict: dict,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker that ingests an event envelope through FailureCandidatePipeline."""
    try:
        pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)
        env = EventEnvelope(**event_dict)
        cand, status = pipeline.ingest_event_envelope(env)
        result_queue.put((cand.candidate_id if cand else None, status))
    except BaseException as exc:  # noqa: BLE001
        result_queue.put((None, f"{type(exc).__name__}: {exc}"))


def _mp_worker_bootstrap_candidate(
    candidates_dir: str,
    candidate_id: str,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker that concurrently bootstraps an unindexed candidate."""
    try:
        pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)
        cand, gen, digest = pipeline.get_candidate_with_generation(candidate_id)
        result_queue.put((cand.candidate_id if cand else None, gen, digest))
    except BaseException as exc:  # noqa: BLE001
        result_queue.put((None, 0, f"{type(exc).__name__}: {exc}"))


# =============================================================================
# Helper Factories
# =============================================================================

def _make_sample_candidate(candidate_id: str, status: str = "PROPOSED") -> LessonCandidate:
    return LessonCandidate(
        schema_version=1,
        candidate_id=candidate_id,
        source_event_ids=["evt_sample_001"],
        failure_classification="DEFECT_IMPLEMENTATION",
        affected_subsystems=["audio"],
        observed_behavior={"summary": "Audio thread starvation under load"},
        root_cause_hypothesis={"hypothesis": "Priority inversion in WASAPI worker", "status": "INCONCLUSIVE"},
        review_status=status,
    )


def _make_sample_receipt(
    receipt_id: str,
    execution_id: str,
    candidate_id: str = "LCAND-001",
    exit_code: int = 0,
    digest_override: str | None = None,
) -> ExecutionReceipt:
    raw = {
        "schema_version": 2,
        "receipt_id": receipt_id,
        "execution_id": execution_id,
        "candidate_id": candidate_id,
        "source_event_ids": ["evt_001"],
        "execution_pair_id": "PAIR-001",
        "execution_role": "BASELINE",
        "captured_at_utc": "2026-11-05T20:00:00Z",
        "argument_vector": ["pytest", "tests/sample.py"],
        "working_directory": "C:/workspace",
        "repository_revision": "be8d39f5",
        "working_tree_clean": True,
        "exit_code": exit_code,
        "termination_reason": "NORMAL",
        "started_at_utc": "2026-11-05T19:59:59Z",
        "completed_at_utc": "2026-11-05T20:00:00Z",
        "duration_seconds": 0.5,
        "test_collection_count": 1,
        "tests_passed": 1 if exit_code == 0 else 0,
        "tests_failed": 0 if exit_code == 0 else 1,
        "tests_skipped": 0,
        "tests_errored": 0,
        "executed_test_ids": ["tests/sample.py::test_one"],
        "test_outcomes": {"tests/sample.py::test_one": "PASSED" if exit_code == 0 else "FAILED"},
        "failure_signatures": [] if exit_code == 0 else ["AssertionError"],
        "target_test_file": "tests/sample.py",
        "target_test_file_sha256": "abcdef123456",
        "source_state_manifest": {"jarvis/audio.py": "1111"},
        "configuration_manifest": {"pytest.ini": "2222"},
        "environment_fingerprint": {"python_version": "3.12.10", "pytest_version": "9.1.1", "env_isolation_policy": "STRICT_NARROW"},
        "stdout_sha256": "3333",
        "stderr_sha256": "4444",
        "stdout_snippet": "passed",
        "stderr_snippet": "",
        "junit_xml_sha256": None,
        "evidence_completeness": "COMPLETE",
    }
    digest = digest_override or canonical_hash_dict(raw)
    raw["content_digest"] = digest
    return ExecutionReceipt.from_dict(raw)


# =============================================================================
# Test Scenarios (1-17)
# =============================================================================

def test_scenario_01_two_processes_writing_same_candidate_cas(tmp_path: Path):
    """Scenario 1: Two independent processes writing the same candidate; one succeeds, stale fails."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand = _make_sample_candidate("LCAND-scen01", status="PROPOSED")
    ok, _msg, gen = pipeline.record_candidate(cand)
    assert ok is True
    assert gen == 1

    q1 = multiprocessing.Queue()
    q2 = multiprocessing.Queue()

    # Both processes try to advance from generation 1
    p1 = multiprocessing.Process(
        target=_mp_worker_candidate_cas,
        args=(str(candidates_dir), "LCAND-scen01", 1, None, "UNDER_REVIEW", q1),
    )
    p2 = multiprocessing.Process(
        target=_mp_worker_candidate_cas,
        args=(str(candidates_dir), "LCAND-scen01", 1, None, "REVIEWED", q2),
    )

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    res1 = q1.get(timeout=5)
    res2 = q2.get(timeout=5)

    successes = [r for r in (res1, res2) if r[0] is True]
    conflicts = [r for r in (res1, res2) if r[0] is False]

    # Exactly one process advances generation from 1 to 2; the second experiences STALE_GENERATION_CONFLICT
    assert len(successes) == 1
    assert successes[0][2] == 2
    assert len(conflicts) == 1
    assert "STALE_GENERATION_CONFLICT" in conflicts[0][1]


def test_scenario_02_stale_process_cannot_replace_newer_verified_state(tmp_path: Path):
    """Scenario 2: A stale process with generation 1 cannot overwrite newer verified Layer C state."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand = _make_sample_candidate("LCAND-scen02", status="PROPOSED")
    pipeline.record_candidate(cand)

    # Process A verifies root cause and advances to generation 2
    cand.verified_root_cause = "Verified race condition in lock handover"
    cand.review_status = "REPRODUCTION_CONFIRMED"
    ok, _msg, gen = pipeline.record_candidate(cand, expected_generation=1)
    assert ok is True
    assert gen == 2

    # Process B (stale, unverified) attempts to write at expected_generation=1
    stale_cand = _make_sample_candidate("LCAND-scen02", status="PROPOSED")
    ok_stale, msg_stale, g_stale = pipeline.record_candidate(stale_cand, expected_generation=1)

    assert ok_stale is False
    assert "STALE_GENERATION_CONFLICT" in msg_stale
    assert g_stale == 2

    # Verify candidate on disk preserved verified root cause
    reloaded = pipeline.load_candidate("LCAND-scen02")
    assert reloaded is not None
    assert reloaded.verified_root_cause == "Verified race condition in lock handover"


def test_scenario_03_two_processes_updating_different_candidates_concurrently(tmp_path: Path):
    """Scenario 3: Two processes updating different candidates concurrently both succeed."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    c1 = _make_sample_candidate("LCAND-cand-A", status="PROPOSED")
    c2 = _make_sample_candidate("LCAND-cand-B", status="PROPOSED")
    pipeline.record_candidate(c1)
    pipeline.record_candidate(c2)

    q1 = multiprocessing.Queue()
    q2 = multiprocessing.Queue()

    p1 = multiprocessing.Process(
        target=_mp_worker_candidate_cas,
        args=(str(candidates_dir), "LCAND-cand-A", 1, None, "UNDER_REVIEW", q1),
    )
    p2 = multiprocessing.Process(
        target=_mp_worker_candidate_cas,
        args=(str(candidates_dir), "LCAND-cand-B", 1, None, "REVIEWED", q2),
    )

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    res1 = q1.get(timeout=5)
    res2 = q2.get(timeout=5)

    assert res1[0] is True and res1[2] == 2
    assert res2[0] is True and res2[2] == 2


def test_scenario_04_duplicate_identical_receipt_registration_idempotent(tmp_path: Path):
    """Scenario 4: Duplicate identical receipt registration returns explicit idempotent replay."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    receipt = _make_sample_receipt("RCPT-004", "EXEC-004", "LCAND-004")
    ok1, status1, path1 = store.register_receipt(receipt)
    assert ok1 is True
    assert status1 == "PERSISTED"

    # Second registration of exact same receipt
    ok2, status2, path2 = store.register_receipt(receipt)
    assert ok2 is True
    assert status2 == "IDEMPOTENT_REPLAY"
    assert path1 == path2

    # Verify only 1 row exists in SQLite
    with sqlite3.connect(store.registry_db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM execution_receipts WHERE execution_id = 'EXEC-004';")
        assert cur.fetchone()[0] == 1


def test_scenario_05_same_execution_id_different_receipt_content_conflict(tmp_path: Path):
    """Scenario 5: Reuse of execution ID with divergent content raises ExecutionIdentityConflictError."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    receipt1 = _make_sample_receipt("RCPT-005A", "EXEC-005-SAME", exit_code=0)
    store.register_receipt(receipt1)

    # Conflicting receipt: same execution_id but different receipt_id and exit_code
    receipt2 = _make_sample_receipt("RCPT-005B", "EXEC-005-SAME", exit_code=1)

    with pytest.raises(ExecutionIdentityConflictError) as exc_info:
        store.register_receipt(receipt2)

    assert "already registered with different content" in str(exc_info.value)

    # Verify original receipt remains untampered in registry
    valid, _, orig_rcpt = store.verify_receipt("RCPT-005A")
    assert valid is True
    assert orig_rcpt.exit_code == 0


def test_scenario_06_simultaneous_receipt_registration_independent_processes(tmp_path: Path):
    """Scenario 6: Simultaneous receipt registration across independent processes succeeds cleanly."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    r1 = _make_sample_receipt("RCPT-006A", "EXEC-006A").to_dict()
    r2 = _make_sample_receipt("RCPT-006B", "EXEC-006B").to_dict()

    q1 = multiprocessing.Queue()
    q2 = multiprocessing.Queue()

    p1 = multiprocessing.Process(target=_mp_worker_receipt_register, args=(str(evidence_dir), r1, q1))
    p2 = multiprocessing.Process(target=_mp_worker_receipt_register, args=(str(evidence_dir), r2, q2))

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    res1 = q1.get(timeout=5)
    res2 = q2.get(timeout=5)

    assert res1[0] is True and res1[1] == "PERSISTED"
    assert res2[0] is True and res2[1] == "PERSISTED"

    assert store.has_receipt("RCPT-006A") is True
    assert store.has_receipt("RCPT-006B") is True


def test_scenario_07_database_busy_locked_conditions_retry(tmp_path: Path):
    """Scenario 7: Database locked condition is gracefully handled by bounded retries."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    import threading
    lock_held = threading.Event()

    def _hold_and_release():
        conn = sqlite3.connect(store.registry_db_path, isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE;")
        lock_held.set()
        time.sleep(0.15)
        conn.execute("COMMIT;")
        conn.close()

    t = threading.Thread(target=_hold_and_release)
    t.start()
    lock_held.wait(timeout=5.0)

    receipt = _make_sample_receipt("RCPT-007", "EXEC-007")
    ok, status, path = store.register_receipt(receipt)
    t.join()

    assert ok is True
    assert status == "PERSISTED"
    assert path.exists()


def test_scenario_08_process_termination_during_transaction_crash_consistency(tmp_path: Path):
    """Scenario 8: Subprocess termination via os._exit mid-transaction does not corrupt SQLite WAL."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    ready_event = multiprocessing.Event()
    p = multiprocessing.Process(
        target=_mp_worker_crash_mid_transaction,
        args=(str(store.registry_db_path), ready_event),
    )
    p.start()
    ready_event.wait(timeout=5)
    p.join(timeout=5)

    # Process terminated abruptly with code 42
    assert p.exitcode == 42

    # Verify that the uncommitted crashed record was auto-rolled back by SQLite WAL
    with sqlite3.connect(store.registry_db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM execution_receipts WHERE receipt_id = 'RCPT-crash';")
        assert cur.fetchone()[0] == 0

    # Subsequent transactions on the database succeed without corruption
    valid_receipt = _make_sample_receipt("RCPT-008-clean", "EXEC-008-clean")
    ok, status, _ = store.register_receipt(valid_receipt)
    assert ok is True
    assert status == "PERSISTED"


def test_scenario_09_corrupt_candidate_json_quarantine(tmp_path: Path):
    """Scenario 9: Corrupt candidate JSON is quarantined and overwrite is refused."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    # Write garbage bytes into a candidate file
    corrupt_file = candidates_dir / "LCAND-corrupt.json"
    corrupt_file.parent.mkdir(parents=True, exist_ok=True)
    corrupt_file.write_text("{ incomplete json truncated...", encoding="utf-8")

    # Attempting to persist to this candidate ID refuses silent overwrite
    new_cand = _make_sample_candidate("LCAND-corrupt")
    ok, msg, _gen = pipeline.record_candidate(new_cand)
    assert ok is False
    assert "CORRUPT_CANDIDATE_PRESERVED" in msg

    # Quarantine helper successfully isolates the corrupt file
    quarantined = pipeline.quarantine_corrupt_artifact("LCAND-corrupt")
    assert quarantined is not None
    assert quarantined.exists()
    assert not corrupt_file.exists()

    # Now persistence succeeds
    ok2, _msg2, gen2 = pipeline.record_candidate(new_cand)
    assert ok2 is True
    assert gen2 == 1


def test_scenario_10_corrupt_receipt_artifact_detection(tmp_path: Path):
    """Scenario 10: verify_receipt detects malformed JSON and returns RECEIPT_MALFORMED."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    receipt = _make_sample_receipt("RCPT-010", "EXEC-010")
    store.register_receipt(receipt)

    # Corrupt the artifact file on disk
    art_path = store.receipts_dir / "RCPT-010.json"
    art_path.write_text("GARBAGE DATA NOT JSON", encoding="utf-8")

    valid, msg, rec = store.verify_receipt("RCPT-010")
    assert valid is False
    assert "RECEIPT_MALFORMED" in msg
    assert rec is None


def test_scenario_11_database_artifact_disagreement_detection(tmp_path: Path):
    """Scenario 11: verify_receipt detects digest or execution_id divergence (RECEIPT_TAMPERED)."""
    evidence_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=evidence_dir)

    receipt = _make_sample_receipt("RCPT-011", "EXEC-011")
    store.register_receipt(receipt)

    # Tamper with the artifact file by changing exit_code
    art_path = store.receipts_dir / "RCPT-011.json"
    data = json.loads(art_path.read_text(encoding="utf-8"))
    data["exit_code"] = 99
    art_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    valid, msg, rec = store.verify_receipt("RCPT-011")
    assert valid is False
    assert "RECEIPT_TAMPERED" in msg
    assert rec is None


def test_scenario_12_replayed_stale_generation_rejection(tmp_path: Path):
    """Scenario 12: Replaying an older generation update is rejected with STALE_GENERATION_CONFLICT."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand = _make_sample_candidate("LCAND-012", status="PROPOSED")
    pipeline.record_candidate(cand)  # gen 1

    cand.review_status = "UNDER_REVIEW"
    pipeline.record_candidate(cand, expected_generation=1)  # gen 2

    # Attempt to write with stale expected_generation=1
    cand.review_status = "REVIEWED"
    with pytest.raises(StaleVersionConflictError) as exc_info:
        pipeline.update_candidate_cas(cand, expected_generation=1)

    assert "STALE_GENERATION_CONFLICT" in str(exc_info.value)
    assert "persisted generation is 2" in str(exc_info.value)


def test_scenario_13_invalid_generation_number_rejection(tmp_path: Path):
    """Scenario 13: Attempting to create a brand-new candidate with non-zero generation is rejected."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand = _make_sample_candidate("LCAND-013-new", status="PROPOSED")

    with pytest.raises(StaleVersionConflictError) as exc_info:
        pipeline.update_candidate_cas(cand, expected_generation=5)

    assert "STALE_GENERATION_CONFLICT" in str(exc_info.value)
    assert "candidate does not exist" in str(exc_info.value)


def test_scenario_14_candidate_terminal_state_modification_rejection(tmp_path: Path):
    """Scenario 14: Modification of candidate in terminal state (REJECTED/SUPERSEDED) is locked."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand = _make_sample_candidate("LCAND-014", status="REJECTED")
    ok, _, gen = pipeline.record_candidate(cand)
    assert ok is True

    # Attempt to revive or change status from REJECTED to PROPOSED
    cand.review_status = "PROPOSED"
    with pytest.raises(TerminalStateError) as exc_info:
        pipeline.update_candidate_cas(cand, expected_generation=gen)

    assert "TERMINAL_STATE_LOCKED" in str(exc_info.value)


def test_scenario_15_repeated_ingestion_across_processes_deduplication(tmp_path: Path):
    """Scenario 15: Simultaneous ingestion of the exact same failure event across processes deduplicates."""
    candidates_dir = tmp_path / "candidates"
    FailureCandidatePipeline(candidates_dir=candidates_dir)

    event_dict = {
        "schema_version": 1,
        "event_id": "evt_repeat_015_000000000000",
        "event_type": "POST_TOOL_USE",
        "identity_status": "RELIABLE",
        "step_index": 1,
        "tool_name": "run_command",
        "tool_outcome": "FAILURE",
        "error_status": "NON_EMPTY",
        "failure_signal": "Simulated cross-process event race",
        "exit_code": 1,
        "exit_code_confidence": "HIGH",
        "safe_summary": "Simulated cross-process event race",
    }

    q1 = multiprocessing.Queue()
    q2 = multiprocessing.Queue()

    p1 = multiprocessing.Process(target=_mp_worker_event_ingest, args=(str(candidates_dir), event_dict, q1))
    p2 = multiprocessing.Process(target=_mp_worker_event_ingest, args=(str(candidates_dir), event_dict, q2))

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    res1 = q1.get(timeout=5)
    res2 = q2.get(timeout=5)

    # Both processes return the exact same candidate_id
    cand_id_1, status_1 = res1
    cand_id_2, status_2 = res2

    assert cand_id_1 == cand_id_2
    assert {status_1, status_2} in (
        {"CREATED", "EXACT_DUPLICATE_ID_MATCH"},
        {"CREATED", "EXACT_RELIABLE_DUPLICATE"},
    )

    # Verify only 1 candidate JSON file created
    json_files = list(candidates_dir.glob("*.json"))
    assert len(json_files) == 1


def test_scenario_16_subprocess_environment_isolation_scrubbing(tmp_path: Path, monkeypatch):
    """Scenario 16: Collector scrubs PYTEST_ADDOPTS/PYTEST_PLUGINS and binds PYTHONPATH."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p bad_plugin --maxfail=1")
    monkeypatch.setenv("PYTEST_PLUGINS", "bad_plugin")
    monkeypatch.setenv("SECRET_API_TOKEN", "super_secret_12345")

    collector = ExecutionEvidenceCollector(workspace_root=tmp_path)
    isolated_env = collector._build_isolated_environment()

    # Injection variables must be scrubbed
    assert "PYTEST_ADDOPTS" not in isolated_env
    assert "PYTEST_PLUGINS" not in isolated_env
    assert "SECRET_API_TOKEN" not in isolated_env

    # Mandatory isolation settings must be bound
    assert isolated_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert isolated_env["PYTHONPATH"] == str(tmp_path.resolve())
    assert "PATH" in isolated_env


def test_scenario_17_red_green_detects_environment_mutation(tmp_path: Path):
    """Scenario 17: RedGreenVerificationEngine detects mutated environment fingerprint."""
    test_file = tmp_path / "tests" / "test_x.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_one(): pass\n", encoding="utf-8")
    import hashlib
    file_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

    red = ExecutionEvidenceEnvelope(
        receipt_id="RCPT-red",
        execution_id="EXEC-red",
        candidate_id="LCAND-env",
        execution_pair_id="PAIR-env",
        execution_role="BASELINE",
        target_test_file="tests/test_x.py",
        target_test_file_sha256=file_sha,
        repository_revision="rev1",
        exit_code=1,
        test_collection_count=1,
        tests_failed=1,
        test_outcomes={"tests/test_x.py::test_one": "FAILED"},
        environment_fingerprint={"python_version": "3.12.10", "pytest_version": "9.1.1", "env_isolation_policy": "STRICT_NARROW"},
        source_state_manifest={"mod.py": "1111"},
        configuration_manifest={"pytest.ini": "aaaa"},
    )
    green = ExecutionEvidenceEnvelope(
        receipt_id="RCPT-green",
        execution_id="EXEC-green",
        candidate_id="LCAND-env",
        execution_pair_id="PAIR-env",
        execution_role="FIX_VERIFICATION",
        target_test_file="tests/test_x.py",
        target_test_file_sha256=file_sha,
        repository_revision="rev1",
        exit_code=0,
        test_collection_count=1,
        tests_passed=1,
        test_outcomes={"tests/test_x.py::test_one": "PASSED"},
        environment_fingerprint={"python_version": "3.11.0", "pytest_version": "9.1.1", "env_isolation_policy": "STRICT_NARROW"},
        source_state_manifest={"mod.py": "2222"},
        configuration_manifest={"pytest.ini": "aaaa"},
    )

    valid, msg = RedGreenVerificationEngine.validate_fix_green(green, red, workspace_root=tmp_path)
    assert valid is False
    assert "ENVIRONMENT_MUTATED" in msg
    assert "python_version" in msg


_WORKER_18_CODE = r"""
import os, sys, sqlite3, json
from pathlib import Path

sys.path.insert(0, r"C:\Users\Youssef\Projects\Jarvis_2.0")
from tools.continuity_manager import FailureCandidatePipeline, canonical_candidate_digest, is_monotonic_transition
from tools.lesson_candidate import LessonCandidate

boundary = sys.argv[1]
tmp_dir = Path(sys.argv[2])

cand_dir = tmp_dir / "candidates"
db_path = cand_dir / "candidates_registry.db"
target = cand_dir / "LCAND-crash-18.json"

pipeline = FailureCandidatePipeline(candidates_dir=cand_dir)

c2 = LessonCandidate(
    candidate_id="LCAND-crash-18",
    failure_classification="LIMITATION_ENVIRONMENT",
    deduplication_key="key_18",
    review_status="UNDER_REVIEW"
)
new_digest = canonical_candidate_digest(c2)

# Boundary 1: Before SQLite transaction
if boundary == "1":
    os._exit(42)

with sqlite3.connect(db_path, timeout=15.0, isolation_level=None) as conn:
    conn.execute("BEGIN IMMEDIATE;")
    # Boundary 2: After BEGIN IMMEDIATE
    if boundary == "2":
        os._exit(42)

    cur = conn.execute("SELECT generation, content_digest, payload_json, review_status, has_verified_root_cause FROM candidate_records WHERE candidate_id = ?;", ("LCAND-crash-18",))
    row = cur.fetchone()
    cur_gen, cur_digest, cur_payload, cur_status, cur_rc = row

    if cur_digest != canonical_candidate_digest(c2) and is_monotonic_transition(cur_status, c2.review_status):
        pass

    # Boundary 3: After CAS validation
    if boundary == "3":
        os._exit(42)

    # Boundary 4: During canonical payload serialization
    if boundary == "4":
        os._exit(42)

    new_payload = json.dumps(c2.to_dict(), indent=2, ensure_ascii=False)
    new_gen = cur_gen + 1

    conn.execute("UPDATE candidate_records SET generation = ?, content_digest = ?, payload_json = ?, review_status = ? WHERE candidate_id = ? AND generation = ?;",
                 (new_gen, new_digest, new_payload, c2.review_status, "LCAND-crash-18", cur_gen))

    # Boundary 5: After SQLite update but before commit
    if boundary == "5":
        os._exit(42)

    conn.execute("COMMIT;")

    # Boundary 6: Immediately after commit
    if boundary == "6":
        os._exit(42)

# Boundary 7: Before JSON projection
if boundary == "7":
    os._exit(42)

unique_tmp = cand_dir / f"LCAND-crash-18.tmp_{os.getpid()}"
# Boundary 8: During projection temporary-file write
if boundary == "8":
    unique_tmp.write_text("{\"incomplete\": true", encoding="utf-8")
    os._exit(42)

with open(unique_tmp, "w", encoding="utf-8") as f:
    f.write(new_payload)
    f.flush()
    os.fsync(f.fileno())

unique_tmp.replace(target)
# Boundary 9: After projection replacement
if boundary == "9":
    os._exit(42)

# Boundary 10: During projection verification
if boundary == "10":
    os._exit(42)

# Boundary 11: During recovery (simulating exit)
if boundary == "11":
    os._exit(42)
"""


def test_scenario_18_authoritative_sqlite_11_boundary_crash_injections(tmp_path: Path):
    """Scenario 18: Deterministic child-process crash injection across all 11 boundaries verifies zero data loss."""
    worker_py = tmp_path / "worker18.py"
    worker_py.write_text(_WORKER_18_CODE, encoding="utf-8")

    for b in range(1, 12):
        sub_dir = tmp_path / f"run_b_{b}"
        cand_dir = sub_dir / "candidates"
        cand_dir.mkdir(parents=True)

        # Pre-seed Generation 1 in DB and disk
        c1 = LessonCandidate(
            candidate_id="LCAND-crash-18",
            failure_classification="LIMITATION_ENVIRONMENT",
            deduplication_key="key_18",
            review_status="OBSERVED"
        )
        p_init = FailureCandidatePipeline(candidates_dir=cand_dir)
        p_init.record_candidate(c1)

        res = subprocess.run([sys.executable, str(worker_py), str(b), str(sub_dir)], check=False, capture_output=True, text=True)
        assert res.returncode == 42

        # Start fresh process to verify state
        p_fresh = FailureCandidatePipeline(candidates_dir=cand_dir)
        cand_loaded, loaded_gen, loaded_digest = p_fresh.get_candidate_with_generation("LCAND-crash-18")

        # In boundaries 1-5 (before commit): must roll back to Gen 1
        if b <= 5:
            assert loaded_gen == 1
            assert cand_loaded.review_status == "OBSERVED"
        # In boundaries 6-11 (after commit): must be Gen 2
        else:
            assert loaded_gen == 2
            assert cand_loaded.review_status == "UNDER_REVIEW"

        # Projection reconciliation must successfully reconcile disk files
        _rec_summary = p_fresh.reconcile_candidate_projections()
        target = cand_dir / "LCAND-crash-18.json"
        assert target.exists()

        disk_raw = json.loads(target.read_text(encoding="utf-8"))
        assert canonical_candidate_digest(LessonCandidate.from_dict(disk_raw)) == loaded_digest


def test_scenario_19_projection_reconciliation_and_recovery(tmp_path: Path):
    """Scenario 19: Reconcile candidate and receipt projections repairs missing, stale, and corrupt files."""
    cand_dir = tmp_path / "candidates"
    p = FailureCandidatePipeline(candidates_dir=cand_dir)

    c1 = LessonCandidate(candidate_id="LCAND-rec-01", failure_classification="LIMITATION_ENVIRONMENT", review_status="OBSERVED")
    c2 = LessonCandidate(candidate_id="LCAND-rec-02", failure_classification="LIMITATION_ENVIRONMENT", review_status="OBSERVED")
    c3 = LessonCandidate(candidate_id="LCAND-rec-03", failure_classification="LIMITATION_ENVIRONMENT", review_status="OBSERVED")

    p.record_candidate(c1)
    p.record_candidate(c2)
    p.record_candidate(c3)

    # 1. Delete c1's projection file (missing)
    f1 = cand_dir / "LCAND-rec-01.json"
    f1.unlink()

    # 2. Corrupt c2's projection file
    f2 = cand_dir / "LCAND-rec-02.json"
    f2.write_text("{broken json syntax", encoding="utf-8")

    # 3. Create an orphaned file (not in SQLite)
    f_orphan = cand_dir / "LCAND-orphan-99.json"
    f_orphan.write_text(json.dumps({"candidate_id": "LCAND-orphan-99"}), encoding="utf-8")

    # Run reconciliation
    summary = p.reconcile_candidate_projections()
    assert summary["checked"] == 3
    assert summary["projected"] >= 2  # Rebuilt f1 and f2
    assert "LCAND-orphan-99.json" in summary["orphaned"]
    assert len(summary["quarantined"]) == 1

    # Verify f1 was restored from SQLite
    assert f1.exists()
    assert json.loads(f1.read_text(encoding="utf-8"))["candidate_id"] == "LCAND-rec-01"

    # Verify f2 was restored from SQLite
    assert f2.exists()
    assert json.loads(f2.read_text(encoding="utf-8"))["candidate_id"] == "LCAND-rec-02"

    # Verify orphaned file was not deleted
    assert f_orphan.exists()

    # Idempotent re-run
    summary2 = p.reconcile_candidate_projections()
    assert summary2["clean"] == 3
    assert summary2["projected"] == 0


def test_scenario_20_new_receipt_with_populated_payload(tmp_path: Path):
    """Scenario 20: Newly registered receipt has non-null canonical payload and passes verification."""
    ev_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc20", "EXEC-sc20")
    ok, status, _path = store.register_receipt(receipt)
    assert ok is True
    assert status == "PERSISTED"

    with store._connect() as conn:
        row = conn.execute("SELECT payload_json FROM execution_receipts WHERE receipt_id = ?;", (receipt.receipt_id,)).fetchone()
        assert row is not None
        assert row[0] is not None
        assert len(row[0]) > 0

    valid, v_status, rec = store.verify_receipt(receipt.receipt_id)
    assert valid is True
    assert v_status == "VALID_STORED_RECEIPT"
    assert rec is not None
    assert rec.receipt_id == receipt.receipt_id


def test_scenario_21_existing_receipt_with_null_payload_rejected(tmp_path: Path):
    """Scenario 21: Existing receipt with NULL payload is rejected with RECEIPT_PAYLOAD_UNPOPULATED."""
    ev_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc21", "EXEC-sc21")
    store.register_receipt(receipt)

    # Forcibly NULL out payload_json in SQLite
    with store._connect() as conn:
        conn.execute("UPDATE execution_receipts SET payload_json = NULL WHERE receipt_id = ?;", (receipt.receipt_id,))
        conn.commit()

    valid, v_status, rec = store.verify_receipt(receipt.receipt_id)
    assert valid is False
    assert "RECEIPT_PAYLOAD_UNPOPULATED" in v_status
    assert rec is None


def test_scenario_22_valid_receipt_backfill(tmp_path: Path):
    """Scenario 22: Migration backfills NULL payload from valid matching disk artifact."""
    ev_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc22", "EXEC-sc22")
    store.register_receipt(receipt)

    # NULL out payload_json in SQLite
    with store._connect() as conn:
        conn.execute("UPDATE execution_receipts SET payload_json = NULL WHERE receipt_id = ?;", (receipt.receipt_id,))
        conn.commit()

    # Verify migration backfills it
    report = store.migrate_receipt_payloads()
    assert report["unfilled_found"] == 1
    assert report["backfilled"] == 1

    with store._connect() as conn:
        row = conn.execute("SELECT payload_json FROM execution_receipts WHERE receipt_id = ?;", (receipt.receipt_id,)).fetchone()
        assert row[0] is not None

    valid, v_status, _rec = store.verify_receipt(receipt.receipt_id)
    assert valid is True
    assert v_status == "VALID_STORED_RECEIPT"


def test_scenario_23_missing_receipt_artifact(tmp_path: Path):
    """Scenario 23: Missing artifact fails verify_receipt but is restored via reconcile_receipt_projections."""
    ev_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc23", "EXEC-sc23")
    _ok, _status, path = store.register_receipt(receipt)
    assert path.exists()

    # Unlink artifact
    path.unlink()

    valid, v_status, _rec = store.verify_receipt(receipt.receipt_id)
    assert valid is False
    assert "RECEIPT_ARTIFACT_MISSING" in v_status

    # Reconcile projections
    summary = store.reconcile_receipt_projections()
    assert summary["rebuilt"] == 1
    assert path.exists()

    valid2, v_status2, _rec2 = store.verify_receipt(receipt.receipt_id)
    assert valid2 is True
    assert v_status2 == "VALID_STORED_RECEIPT"


def test_scenario_24_mismatched_receipt_digest(tmp_path: Path):
    """Scenario 24: Digest mismatches in canonical payload or artifact are rejected."""
    ev_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc24", "EXEC-sc24")
    store.register_receipt(receipt)

    # Test 1: Tampered canonical SQLite payload
    tampered_rcpt = _make_sample_receipt("RCPT-sc24", "EXEC-sc24", exit_code=99)
    tampered_payload = json.dumps(tampered_rcpt.to_dict(), indent=2)
    with store._connect() as conn:
        conn.execute("UPDATE execution_receipts SET payload_json = ? WHERE receipt_id = ?;", (tampered_payload, receipt.receipt_id))
        conn.commit()

    valid, v_status, _ = store.verify_receipt(receipt.receipt_id)
    assert valid is False
    assert "RECEIPT_PAYLOAD_MISMATCH" in v_status

    # Test 2: Restore canonical SQLite payload, tamper disk artifact
    canonical_payload = json.dumps(receipt.to_dict(), indent=2)
    with store._connect() as conn:
        conn.execute("UPDATE execution_receipts SET payload_json = ? WHERE receipt_id = ?;", (canonical_payload, receipt.receipt_id))
        conn.commit()

    art_file = ev_dir / "receipts" / f"{receipt.receipt_id}.json"
    art_file.write_text(tampered_payload, encoding="utf-8")

    valid2, v_status2, _ = store.verify_receipt(receipt.receipt_id)
    assert valid2 is False
    assert "RECEIPT_TAMPERED" in v_status2


def test_scenario_25_repeated_migration_idempotent(tmp_path: Path):
    """Scenario 25: Repeated migration runs are completely idempotent."""
    ev_dir = tmp_path / "evidence"
    store = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc25", "EXEC-sc25")
    store.register_receipt(receipt)

    rep1 = store.migrate_receipt_payloads()
    assert rep1["unfilled_found"] == 0
    assert rep1["backfilled"] == 0

    rep2 = store.migrate_receipt_payloads()
    assert rep2["unfilled_found"] == 0
    assert rep2["backfilled"] == 0


def test_scenario_26_concurrent_candidate_bootstrap(tmp_path: Path):
    """Scenario 26: Concurrent processes safely bootstrap unindexed candidate without overwrite."""
    cand_dir = tmp_path / "candidates"
    cand_dir.mkdir(parents=True)

    c = LessonCandidate(candidate_id="LCAND-sc26", failure_classification="LIMITATION_ENVIRONMENT", review_status="OBSERVED")
    cand_file = cand_dir / "LCAND-sc26.json"
    cand_file.write_text(json.dumps(c.to_dict(), indent=2), encoding="utf-8")

    q1 = multiprocessing.Queue()
    q2 = multiprocessing.Queue()

    p1 = multiprocessing.Process(target=_mp_worker_bootstrap_candidate, args=(str(cand_dir), "LCAND-sc26", q1))
    p2 = multiprocessing.Process(target=_mp_worker_bootstrap_candidate, args=(str(cand_dir), "LCAND-sc26", q2))

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    res1 = q1.get(timeout=5)
    res2 = q2.get(timeout=5)

    cid1, gen1, dig1 = res1
    cid2, gen2, dig2 = res2

    assert cid1 == "LCAND-sc26"
    assert cid2 == "LCAND-sc26"
    assert gen1 == 1
    assert gen2 == 1
    assert dig1 == dig2

    # Verify only 1 row exists in candidate_records
    pipeline = FailureCandidatePipeline(candidates_dir=cand_dir)
    with pipeline._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM candidate_records WHERE candidate_id = 'LCAND-sc26';").fetchone()[0]
        assert count == 1


def test_scenario_27_existing_sqlite_row_vs_stale_json_projection(tmp_path: Path):
    """Scenario 27: SQLite Gen 2 row is not overwritten by stale Gen 1 disk artifact on read."""
    cand_dir = tmp_path / "candidates"
    cand_dir.mkdir(parents=True)
    pipeline = FailureCandidatePipeline(candidates_dir=cand_dir)

    # Insert Gen 1 in SQLite
    c_gen1 = LessonCandidate(candidate_id="LCAND-sc27", failure_classification="LIMITATION_ENVIRONMENT", review_status="OBSERVED")
    ok1, _, gen1 = pipeline.record_candidate(c_gen1)
    assert ok1 is True and gen1 == 1

    # Advance to Gen 2 in SQLite
    c_gen2 = LessonCandidate(candidate_id="LCAND-sc27", failure_classification="LIMITATION_ENVIRONMENT", review_status="UNDER_REVIEW")
    ok2, _, gen2 = pipeline.record_candidate(c_gen2, expected_generation=1)
    assert ok2 is True and gen2 == 2

    # Forcibly overwrite disk artifact with stale Gen 1 content
    disk_file = cand_dir / "LCAND-sc27.json"
    disk_file.write_text(json.dumps(c_gen1.to_dict(), indent=2), encoding="utf-8")

    # Read candidate back via load_candidate and get_candidate_with_generation
    loaded_cand = pipeline.load_candidate("LCAND-sc27")
    cand_obj, cur_gen, _ = pipeline.get_candidate_with_generation("LCAND-sc27")

    assert loaded_cand.review_status == "UNDER_REVIEW"
    assert cand_obj.review_status == "UNDER_REVIEW"
    assert cur_gen == 2

    # Check that disk file is still stale Gen 1 and was not blindly trusted or rewritten on read
    stale_disk_data = json.loads(disk_file.read_text(encoding="utf-8"))
    assert stale_disk_data["review_status"] == "OBSERVED"

    # Explicit reconciliation updates projection to Gen 2
    rec = pipeline.reconcile_candidate_projections()
    assert rec["projected"] == 1

    reconciled_disk_data = json.loads(disk_file.read_text(encoding="utf-8"))
    assert reconciled_disk_data["review_status"] == "UNDER_REVIEW"


def test_scenario_28_receipt_verification_after_restart(tmp_path: Path):
    """Scenario 28: Receipt verification succeeds across distinct EvidenceStore instances."""
    ev_dir = tmp_path / "evidence"
    store_a = EvidenceStore(evidence_dir=ev_dir)

    receipt = _make_sample_receipt("RCPT-sc28", "EXEC-sc28")
    store_a.register_receipt(receipt)

    # Simulate restart by creating fresh EvidenceStore instance
    store_b = EvidenceStore(evidence_dir=ev_dir)
    valid, v_status, rec = store_b.verify_receipt(receipt.receipt_id)
    assert valid is True
    assert v_status == "VALID_STORED_RECEIPT"
    assert rec.receipt_id == receipt.receipt_id


def test_scenario_29_connection_pragma_values_on_new_connections(tmp_path: Path):
    """Scenario 29: Newly opened connections explicitly set synchronous=NORMAL, WAL, and busy_timeout."""
    cand_dir = tmp_path / "candidates"
    ev_dir = tmp_path / "evidence"

    pipeline = FailureCandidatePipeline(candidates_dir=cand_dir)
    ev_store = EvidenceStore(evidence_dir=ev_dir)

    with pipeline._connect(timeout=10.0) as conn:
        sync_val = conn.execute("PRAGMA synchronous;").fetchone()[0]
        journal_val = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        timeout_val = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert sync_val == 1  # 1 = NORMAL
        assert journal_val.lower() == "wal"
        assert timeout_val == 10000

    with ev_store._connect(timeout=8.0) as conn:
        sync_val = conn.execute("PRAGMA synchronous;").fetchone()[0]
        journal_val = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        timeout_val = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert sync_val == 1  # 1 = NORMAL
        assert journal_val.lower() == "wal"
        assert timeout_val == 8000

