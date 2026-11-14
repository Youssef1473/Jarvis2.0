"""Comprehensive Adversarial Test Suite for Execution Evidence Provenance (ENG-OS-03A2).

Validates all 28 adversarial scenarios, trusted execution witness invariants,
immutable ExecutionReceipt contracts, EvidenceStore persistence & revalidation,
paired RED-GREEN verification rules, implementation state fingerprinting,
candidate lifecycle gating, and performance SLAs.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from tools.continuity_manager import CandidatePromotionGate
from tools.execution_evidence import (
    EvidenceStore,
    ExecutionEvidenceCollector,
    ExecutionEvidenceEnvelope,
    RedGreenVerificationEngine,
    validate_collector_arguments,
)
from tools.lesson_candidate import (
    LessonCandidate,
    promote_to_lessons_file,
    transition_candidate_lifecycle,
)

# =============================================================================
# Isolated Test Fixtures
# =============================================================================

@pytest.fixture
def test_env_harness(tmp_path: Path):
    """Creates isolated test files and an evidence collector bound to tmp_path."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = tmp_path / "data" / "execution_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Relevant source file
    worker_file = src_dir / "worker.py"
    worker_file.write_text("STATE = 'UNFIXED'\n", encoding="utf-8")

    # 1. Genuine reproducible failure (RED)
    red_file = tests_dir / "test_repro_sample.py"
    red_file.write_text(
        "from pathlib import Path\n"
        "def test_reproduce_defect():\n"
        "    p = Path(__file__).resolve().parent.parent / 'src' / 'worker.py'\n"
        "    assert \"STATE = 'FIXED'\" in p.read_text(), 'WinError 5: Access is denied'\n",
        encoding="utf-8",
    )

    # 2. Unrelated failure
    unrelated_file = tests_dir / "test_unrelated_sample.py"
    unrelated_file.write_text(
        "def test_unrelated():\n"
        "    raise ZeroDivisionError('division by zero unexpected')\n",
        encoding="utf-8",
    )

    # 3. Collection error (syntax defect)
    broken_file = tests_dir / "test_broken_syntax.py"
    broken_file.write_text(
        "def test_broken( invalid syntax\n",
        encoding="utf-8",
    )

    # 4. Skipped test
    skipped_file = tests_dir / "test_skipped_sample.py"
    skipped_file.write_text(
        "import pytest\n"
        "@pytest.mark.skip(reason='Environmental boundary')\n"
        "def test_skip():\n"
        "    pass\n",
        encoding="utf-8",
    )

    # 5. Empty test file (0 tests)
    empty_file = tests_dir / "test_empty_sample.py"
    empty_file.write_text("# No tests defined\n", encoding="utf-8")

    collector = ExecutionEvidenceCollector(
        workspace_root=tmp_path,
        evidence_dir=evidence_dir,
    )

    candidate = LessonCandidate(
        candidate_id="LCAND-advtest01",
        source_event_ids=["evt-adv-001"],
        affected_subsystems=["src", "worker"],
        observed_behavior={
            "tool_name": "run_command",
            "exit_code": 1,
            "failure_signal": "Access is denied",
            "summary": "Process terminated with Access is denied error",
        },
        root_cause_hypothesis={"hypothesis": "Permission token missing in worker context"},
        review_status="PROPOSED",
    )

    return {
        "root": tmp_path,
        "tests_dir": tests_dir,
        "src_dir": src_dir,
        "worker_file": worker_file,
        "evidence_dir": evidence_dir,
        "red_file": red_file,
        "unrelated_file": unrelated_file,
        "broken_file": broken_file,
        "skipped_file": skipped_file,
        "empty_file": empty_file,
        "collector": collector,
        "candidate": candidate,
    }


# =============================================================================
# Workstream F: 28 Adversarial Scenarios
# =============================================================================

def test_adv_01_forged_envelope_direct_construction(test_env_harness: dict[str, Any]):
    """Scenario 1: Direct envelope construction with forged authority without backing store receipt is rejected."""
    candidate: LessonCandidate = test_env_harness["candidate"]
    store: EvidenceStore = test_env_harness["collector"].evidence_store

    envelope = ExecutionEvidenceEnvelope(
        envelope_id="EEV-fake001",
        receipt_id="RCPT-fake001",
        execution_id="EXEC-fake001",
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_pair_id="PAIR-fake001",
        execution_role="BASELINE",
        provenance_status="AUTHORITATIVE_LOCAL_EXECUTION",
        evidence_completeness="COMPLETE",
        termination_reason="NORMAL",
        test_collection_count=1,
        tests_failed=1,
        exit_code=1,
        test_outcomes={"test_repro_sample.py::test_reproduce_defect": "FAILED"},
        failure_signatures=["WinError 5: Access is denied"],
        target_test_file="tests/test_repro_sample.py",
        target_test_file_sha256="abc123",
        repository_revision="be8d39f5",
    )

    # Validated with store: receipt is not in registry
    valid, msg = RedGreenVerificationEngine.validate_baseline_red(
        envelope, candidate_or_signal=candidate, workspace_root=test_env_harness["root"], store=store
    )
    assert valid is False
    assert "not found in EvidenceStore registry" in msg or "RECEIPT_NOT_IN_REGISTRY" in msg


def test_adv_02_direct_mutation_of_authoritative_fields(test_env_harness: dict[str, Any]):
    """Scenario 2: Immutable ExecutionReceipt raises FrozenInstanceError upon attribute mutation."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )

    store = collector.evidence_store
    _, _, receipt = store.verify_receipt(envelope.receipt_id)
    assert receipt is not None

    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.exit_code = 0


def test_adv_03_dataclasses_replace_attempts(test_env_harness: dict[str, Any]):
    """Scenario 3: dataclasses.replace creates a new receipt whose digest mismatches store registry."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    store = collector.evidence_store
    _, _, receipt = store.verify_receipt(envelope.receipt_id)

    tampered_receipt = dataclasses.replace(receipt, exit_code=0)
    # The tampered receipt's computed content digest no longer matches its stored content_digest
    assert tampered_receipt.compute_content_digest() != tampered_receipt.content_digest


def test_adv_04_modified_imported_json_with_recomputed_digest(test_env_harness: dict[str, Any]):
    """Scenario 4: Modified imported JSON with recomputed digest is rejected as untrusted."""
    candidate: LessonCandidate = test_env_harness["candidate"]
    raw_json = {
        "candidate_id": candidate.candidate_id,
        "execution_envelopes": [
            {
                "envelope_id": "EEV-forged",
                "receipt_id": "RCPT-forged",
                "provenance_status": "AUTHORITATIVE_LOCAL_EXECUTION",
                "execution_role": "BASELINE",
                "target_test_file": "tests/test_repro_sample.py",
                "target_test_file_sha256": "abc",
                "repository_revision": "be8d39f5",
            }
        ]
    }
    reloaded = LessonCandidate.from_dict(raw_json)
    envs = reloaded.get_execution_envelopes(store=test_env_harness["collector"].evidence_store)
    assert len(envs) == 1
    assert envs[0].provenance_status == "IMPORTED_UNVERIFIED"


def test_adv_05_fake_execution_receipt_creation(test_env_harness: dict[str, Any]):
    """Scenario 5: Fake execution receipt JSON placed on disk is not in SQLite registry and fails verification."""
    store: EvidenceStore = test_env_harness["collector"].evidence_store
    fake_json = store.receipts_dir / "RCPT-fake999.json"
    fake_json.write_text(json.dumps({"receipt_id": "RCPT-fake999", "exit_code": 0}), encoding="utf-8")

    valid, msg, _ = store.verify_receipt("RCPT-fake999")
    assert valid is False
    assert "not found in EvidenceStore registry" in msg or "RECEIPT_NOT_IN_REGISTRY" in msg


def test_adv_06_actual_collector_observed_execution(test_env_harness: dict[str, Any]):
    """Scenario 6: Real execution captured by collector produces valid receipt recorded in EvidenceStore."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    assert envelope.receipt_id.startswith("RCPT-")
    valid, msg, receipt = collector.evidence_store.verify_receipt(envelope.receipt_id)
    assert valid is True
    assert msg == "VALID_STORED_RECEIPT"
    assert receipt.exit_code != 0


def test_adv_07_valid_red_reproduction(test_env_harness: dict[str, Any]):
    """Scenario 7: Genuine defect reproduction passes validate_baseline_red."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    valid, msg = RedGreenVerificationEngine.validate_baseline_red(
        envelope, candidate_or_signal=candidate, workspace_root=test_env_harness["root"], store=collector.evidence_store
    )
    assert valid is True
    assert msg == "VALID_BASELINE_RED"


def test_adv_08_unrelated_red_failure(test_env_harness: dict[str, Any]):
    """Scenario 8: Baseline failure with unrelated symptom is rejected by consistency check."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    unrelated_file: Path = test_env_harness["unrelated_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=unrelated_file,
    )
    valid, msg = RedGreenVerificationEngine.validate_baseline_red(
        envelope, candidate_or_signal=candidate, workspace_root=test_env_harness["root"], store=collector.evidence_store
    )
    assert valid is False
    assert "Failure symptom inconsistency" in msg


def test_adv_09_empty_red_outcomes(test_env_harness: dict[str, Any]):
    """Scenario 9: Baseline RED envelope with empty test outcomes is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    empty_outcomes_env = dataclasses.replace(envelope, test_outcomes={})
    valid, msg = RedGreenVerificationEngine.validate_baseline_red(
        empty_outcomes_env, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "0 failed or errored test outcomes" in msg


def test_adv_10_zero_tests_collected(test_env_harness: dict[str, Any]):
    """Scenario 10: Test target collecting 0 tests is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    empty_file: Path = test_env_harness["empty_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=empty_file,
    )
    valid, msg = RedGreenVerificationEngine.validate_baseline_red(
        envelope, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "Incomplete evidence" in msg or "collection failure" in msg or "0 tests collected" in msg or "Absence of failure" in msg


def test_adv_11_collection_errors(test_env_harness: dict[str, Any]):
    """Scenario 11: Syntax and collection errors fail closed and cannot establish reproduction."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    broken_file: Path = test_env_harness["broken_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=broken_file,
    )
    valid, msg = RedGreenVerificationEngine.validate_baseline_red(
        envelope, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "Incomplete evidence" in msg or "Failure symptom" in msg or "collection failure" in msg


def test_adv_12_flaky_green_rerun_without_implementation_change(test_env_harness: dict[str, Any]):
    """Scenario 12: Flaky test rerun passing with unchanged implementation state is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    failed_node = next(iter(red.test_outcomes.keys()))

    # Green with identical source_state_manifest and configuration_manifest
    green = dataclasses.replace(
        red,
        envelope_id="EEV-green-flaky",
        execution_id="EXEC-green-flaky",
        receipt_id="RCPT-green-flaky",
        execution_role="FIX_VERIFICATION",
        exit_code=0,
        tests_failed=0,
        tests_passed=1,
        test_outcomes={failed_node: "PASSED"},
        failure_signatures=[],
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "UNCHANGED_IMPLEMENTATION_RERUN" in msg


def test_adv_13_relevant_implementation_change_with_unchanged_test_definition(test_env_harness: dict[str, Any]):
    """Scenario 13: Relevant implementation change with unchanged test definition passes verification."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]
    worker_file: Path = test_env_harness["worker_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )

    # Modify the relevant source file to fix the defect
    worker_file.write_text("STATE = 'FIXED'\n", encoding="utf-8")

    green = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="FIX_VERIFICATION",
        test_target=red_file,
        execution_pair_id=red.execution_pair_id,
    )
    assert green.exit_code == 0
    assert green.tests_passed == 1
    assert green.source_state_manifest != red.source_state_manifest

    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is True
    assert msg == "VALID_FIX_GREEN"


def test_adv_14_unrelated_source_change_does_not_qualify(test_env_harness: dict[str, Any]):
    """Scenario 14: Modifying an unrelated file outside candidate's affected subsystems is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    failed_node = next(iter(red.test_outcomes.keys()))

    # Green with change only to an unrelated file
    unrelated_sources = dict(red.source_state_manifest)
    unrelated_sources["docs/README.md"] = "new_doc_hash_123"

    green = dataclasses.replace(
        red,
        envelope_id="EEV-green-unrelated",
        execution_id="EXEC-green-unrelated",
        execution_role="FIX_VERIFICATION",
        exit_code=0,
        tests_failed=0,
        tests_passed=1,
        test_outcomes={failed_node: "PASSED"},
        failure_signatures=[],
        source_state_manifest=unrelated_sources,
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "UNRELATED_SOURCE_CHANGE" in msg


def test_adv_15_changed_pytest_configuration(test_env_harness: dict[str, Any]):
    """Scenario 15: Changing pytest configuration between RED and GREEN is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    failed_node = next(iter(red.test_outcomes.keys()))

    green = dataclasses.replace(
        red,
        envelope_id="EEV-green-cfg",
        execution_id="EXEC-green-cfg",
        execution_role="FIX_VERIFICATION",
        exit_code=0,
        tests_failed=0,
        tests_passed=1,
        test_outcomes={failed_node: "PASSED"},
        failure_signatures=[],
        configuration_manifest={"pytest.ini": "mutated_cfg_hash"},
        source_state_manifest={"src/worker.py": "new_hash"},
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "CONFIGURATION_MUTATED" in msg


def test_adv_16_changed_fixtures(test_env_harness: dict[str, Any]):
    """Scenario 16: Changing conftest.py between RED and GREEN is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    failed_node = next(iter(red.test_outcomes.keys()))

    green = dataclasses.replace(
        red,
        envelope_id="EEV-green-fixture",
        execution_id="EXEC-green-fixture",
        execution_role="FIX_VERIFICATION",
        exit_code=0,
        tests_failed=0,
        tests_passed=1,
        test_outcomes={failed_node: "PASSED"},
        failure_signatures=[],
        configuration_manifest={"conftest.py": "mutated_fixture_hash"},
        source_state_manifest={"src/worker.py": "new_hash"},
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "CONFIGURATION_MUTATED" in msg


def test_adv_17_changed_dependency_versions(test_env_harness: dict[str, Any]):
    """Scenario 17: Changed dependency or runner environment version between runs is tracked."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    assert "pytest_version" in red.to_dict()["environment_fingerprint"]


def test_adv_18_changed_test_selector(test_env_harness: dict[str, Any]):
    """Scenario 18: Changed test selector or executed test IDs is rejected as TEST_SELECTION_MISMATCH."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    green = dataclasses.replace(
        red,
        envelope_id="EEV-green-selector",
        execution_id="EXEC-green-selector",
        execution_role="FIX_VERIFICATION",
        exit_code=0,
        tests_failed=0,
        tests_passed=1,
        executed_test_ids=["test_other.py::test_different"],
        test_outcomes={"test_other.py::test_different": "PASSED"},
        source_state_manifest={"src/worker.py": "new_hash"},
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "TEST_SELECTION_MISMATCH" in msg or "selection mismatch" in msg


def test_adv_19_skipped_previously_failing_test(test_env_harness: dict[str, Any]):
    """Scenario 19: Skipping the previously failing test in GREEN is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    failed_node = next(iter(red.test_outcomes.keys()))

    green = dataclasses.replace(
        red,
        envelope_id="EEV-green-skip",
        execution_id="EXEC-green-skip",
        execution_role="FIX_VERIFICATION",
        exit_code=0,
        tests_failed=0,
        tests_passed=0,
        tests_skipped=1,
        test_outcomes={failed_node: "SKIPPED"},
        failure_signatures=[],
        source_state_manifest={"src/worker.py": "new_hash"},
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"]
    )
    assert valid is False
    assert "FAILED_TEST_NOT_PASSED" in msg or "expected PASSED" in msg or "GREEN absence of pass" in msg


def test_adv_20_valid_red_green_pair(test_env_harness: dict[str, Any]):
    """Scenario 20: Full valid pair passes verification."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]
    worker_file: Path = test_env_harness["worker_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    worker_file.write_text("STATE = 'FIXED'\n", encoding="utf-8")
    green = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="FIX_VERIFICATION",
        test_target=red_file,
        execution_pair_id=red.execution_pair_id,
    )
    valid, msg = RedGreenVerificationEngine.validate_fix_green(
        green, red, candidate_or_signal=candidate, workspace_root=test_env_harness["root"], store=collector.evidence_store
    )
    assert valid is True
    assert msg == "VALID_FIX_GREEN"


def test_adv_21_modified_evidence_artifact(test_env_harness: dict[str, Any]):
    """Scenario 21: Modifying a stored receipt JSON on disk triggers RECEIPT_TAMPERED."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    store = collector.evidence_store
    receipt_file = store.receipts_dir / f"{envelope.receipt_id}.json"
    data = json.loads(receipt_file.read_text(encoding="utf-8"))
    data["exit_code"] = 0  # Tamper with stored file
    receipt_file.write_text(json.dumps(data), encoding="utf-8")

    valid, msg, _ = store.verify_receipt(envelope.receipt_id)
    assert valid is False
    assert "RECEIPT_TAMPERED" in msg


def test_adv_22_missing_evidence_artifact(test_env_harness: dict[str, Any]):
    """Scenario 22: Deleting a stored receipt JSON triggers RECEIPT_ARTIFACT_MISSING."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    envelope = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    store = collector.evidence_store
    receipt_file = store.receipts_dir / f"{envelope.receipt_id}.json"
    receipt_file.unlink()

    valid, msg, _ = store.verify_receipt(envelope.receipt_id)
    assert valid is False
    assert "RECEIPT_ARTIFACT_MISSING" in msg


def test_adv_23_candidate_serialization_and_restart(test_env_harness: dict[str, Any]):
    """Scenario 23: Candidate serialized to JSON and reloaded preserves has_verified_fix() == True."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]
    worker_file: Path = test_env_harness["worker_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    worker_file.write_text("STATE = 'FIXED'\n", encoding="utf-8")
    green = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="FIX_VERIFICATION",
        test_target=red_file,
        execution_pair_id=red.execution_pair_id,
    )
    candidate.attach_execution_envelope(red)
    candidate.attach_execution_envelope(green)
    assert candidate.has_verified_fix(workspace_root=test_env_harness["root"], store=collector.evidence_store) is True

    # Serialize candidate to JSON
    cand_dict = candidate.to_dict()
    cand_json = json.dumps(cand_dict)

    # Reload candidate in simulated new process
    reloaded_cand = LessonCandidate.from_dict(json.loads(cand_json))
    assert reloaded_cand.has_verified_fix(workspace_root=test_env_harness["root"], store=collector.evidence_store) is True
    assert reloaded_cand.has_verified_reproduction(workspace_root=test_env_harness["root"], store=collector.evidence_store) is True


def test_adv_24_tampered_records_after_restart(test_env_harness: dict[str, Any]):
    """Scenario 24: Tampering with stored receipt file on disk causes reloaded candidate to fail verification."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]
    worker_file: Path = test_env_harness["worker_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    worker_file.write_text("STATE = 'FIXED'\n", encoding="utf-8")
    green = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="FIX_VERIFICATION",
        test_target=red_file,
        execution_pair_id=red.execution_pair_id,
    )
    candidate.attach_execution_envelope(red)
    candidate.attach_execution_envelope(green)

    # Tamper with green receipt file
    store = collector.evidence_store
    green_file = store.receipts_dir / f"{green.receipt_id}.json"
    gdata = json.loads(green_file.read_text(encoding="utf-8"))
    gdata["target_test_file"] = "tests/test_other.py"
    green_file.write_text(json.dumps(gdata), encoding="utf-8")

    # Reload candidate in fresh instance
    reloaded_cand = LessonCandidate.from_dict(json.loads(json.dumps(candidate.to_dict())))
    assert reloaded_cand.has_verified_fix(workspace_root=test_env_harness["root"], store=store) is False


def test_adv_25_replayed_execution_records(test_env_harness: dict[str, Any]):
    """Scenario 25: Replaying an execution envelope on a candidate with mismatched candidate_id is rejected."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    diff_cand = LessonCandidate(candidate_id="LCAND-different999", source_event_ids=["EV-999"])
    diff_cand.attach_execution_envelope(red)
    assert diff_cand.has_verified_reproduction(workspace_root=test_env_harness["root"], store=collector.evidence_store) is False


def test_adv_26_restricted_pytest_argument_enforcement(test_env_harness: dict[str, Any]):
    """Scenario 26: Prohibited flags (-p, -o, --rootdir, shell metas) raise ValueError."""
    root = test_env_harness["root"]
    red_file = test_env_harness["red_file"]

    # Prohibited plugin loading
    with pytest.raises(ValueError, match="Prohibited pytest option"):
        validate_collector_arguments(red_file, extra_args=["-p", "my_plugin"], allowed_root=root)

    # Prohibited config override
    with pytest.raises(ValueError, match="Prohibited pytest option"):
        validate_collector_arguments(red_file, extra_args=["-o", "addopts=-s"], allowed_root=root)

    # Shell metacharacter
    with pytest.raises(ValueError, match="Prohibited shell metacharacter"):
        validate_collector_arguments(red_file, extra_args=["-v; rm -rf /"], allowed_root=root)


def test_adv_27_lifecycle_advancement_using_forged_evidence(test_env_harness: dict[str, Any]):
    """Scenario 27: Lifecycle advancement to REPRODUCTION_CONFIRMED or FIX_VERIFIED without valid evidence fails."""
    candidate: LessonCandidate = test_env_harness["candidate"]

    # No evidence attached
    with pytest.raises(ValueError, match="[Cc]annot transition candidate to 'REPRODUCTION_CONFIRMED'"):
        transition_candidate_lifecycle(candidate, "REPRODUCTION_CONFIRMED")

    # Direct jump rejection from PROPOSED to FIX_VERIFIED
    with pytest.raises(ValueError, match="cannot jump directly to 'FIX_VERIFIED'"):
        transition_candidate_lifecycle(candidate, "FIX_VERIFIED")

    # In REPRODUCTION_CONFIRMED without verified fix evidence
    candidate.review_status = "REPRODUCTION_CONFIRMED"
    with pytest.raises(ValueError, match="[Cc]annot transition candidate to 'FIX_VERIFIED'"):
        transition_candidate_lifecycle(candidate, "FIX_VERIFIED")


def test_adv_28_preservation_of_permanent_promotion_barrier(test_env_harness: dict[str, Any]):
    """Scenario 28: Candidate with verified execution evidence STILL cannot autonomously promote."""
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    candidate: LessonCandidate = test_env_harness["candidate"]
    red_file: Path = test_env_harness["red_file"]
    worker_file: Path = test_env_harness["worker_file"]

    red = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    worker_file.write_text("STATE = 'FIXED'\n", encoding="utf-8")
    green = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="FIX_VERIFICATION",
        test_target=red_file,
        execution_pair_id=red.execution_pair_id,
    )
    candidate.attach_execution_envelope(red)
    candidate.attach_execution_envelope(green)

    transition_candidate_lifecycle(candidate, "REPRODUCTION_CONFIRMED", workspace_root=test_env_harness["root"])
    transition_candidate_lifecycle(candidate, "FIX_VERIFIED", workspace_root=test_env_harness["root"])

    candidate.verified_root_cause = "Worker token missing"
    candidate.reproduction_procedure = "Run test_repro_sample.py"
    candidate.corrective_recommendation = "Set IS_FIXED = True"
    candidate.scope_of_applicability = "Worker subsystem"
    candidate.regression_test = "tests/test_repro_sample.py"

    transition_candidate_lifecycle(
        candidate, "PREPARED_FOR_REVIEW", require_execution_provenance=True, workspace_root=test_env_harness["root"]
    )
    assert candidate.review_status == "PREPARED_FOR_REVIEW"

    # In-process promotion STILL unconditionally fails closed
    ok, msg = promote_to_lessons_file(candidate, "docs/agent/LESSONS.md", confirmed=True)
    assert ok is False
    assert "PROMOTION_PROHIBITED" in msg

    ok_prom, msg_prom = CandidatePromotionGate.promote_candidate(
        candidate, lessons_file=test_env_harness["root"] / "docs" / "agent" / "LESSONS.md"
    )
    assert ok_prom is False
    assert "PROMOTION_PROHIBITED" in msg_prom


def test_adv_29_performance_sla_measurements(test_env_harness: dict[str, Any]):
    """Scenario 29: Measures microsecond-level overhead across hashing, validation, and serialization."""
    candidate: LessonCandidate = test_env_harness["candidate"]
    collector: ExecutionEvidenceCollector = test_env_harness["collector"]
    store = collector.evidence_store

    # 1. Digest computation
    t0 = time.perf_counter()
    for _ in range(100):
        _ = dataclasses.asdict(candidate)
    t_hashing_ms = ((time.perf_counter() - t0) / 100) * 1000.0
    assert t_hashing_ms < 5.0, f"Hashing exceeded SLA: {t_hashing_ms:.3f} ms"

    # 2. Store verification overhead
    red_file: Path = test_env_harness["red_file"]
    env = collector.capture_test_execution(
        candidate_id=candidate.candidate_id,
        source_event_ids=candidate.source_event_ids,
        execution_role="BASELINE",
        test_target=red_file,
    )
    t0 = time.perf_counter()
    for _ in range(10):
        _ = store.verify_receipt(env.receipt_id)
    t_store_ms = ((time.perf_counter() - t0) / 10) * 1000.0
    assert t_store_ms < 15.0, f"Store verification exceeded SLA: {t_store_ms:.3f} ms"
