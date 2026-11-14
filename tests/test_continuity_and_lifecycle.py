"""End-to-End Integration Tests for Jarvis Continuity & Evidence-Gated Learning (ENG-OS-02 Phase 3D2).

Validates the full lifecycle and architectural invariants across 14 scenarios:
Observed Event -> Evidence Collection -> Candidate Generation -> Root-Cause Verification ->
Specialist Review -> Human Approval -> Controlled Promotion -> Future Session Retrieval.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools.context_router import (
    retrieve_relevant_lessons,
)
from tools.continuity_manager import (
    CandidatePromotionGate,
    FailureCandidatePipeline,
    ProjectContinuityEngine,
    get_approved_event_directories,
    run_continuity_hook,
    run_session_start_hook,
)
from tools.failure_collector import (
    EventEnvelope,
    parse_stop,
    process_hook_payload,
)
from tools.lesson_candidate import (
    LIFECYCLE_STATES,
    LessonCandidate,
    check_candidate_deduplication,
    collect_evidence_reference,
    is_evidence_equivalent,
    is_monotonic_transition,
    transition_candidate_lifecycle,
    validate_lifecycle_transition,
    verify_root_cause,
)

# =============================================================================
# Helper Fixtures & Builders
# =============================================================================

def _build_verified_candidate(
    candidate_id: str = "LCAND-testverified01",
    tool_name: str = "run_command",
    exit_code: int = 1,
    failure_signal: str = "WinError -2147024891: Access is denied",
) -> LessonCandidate:
    """Creates a candidate with Layer C fully verified for promotion preview testing."""
    cand = LessonCandidate(
        candidate_id=candidate_id,
        source_event_ids=["evt-001"],
        observed_behavior={
            "tool_name": tool_name,
            "exit_code": exit_code,
            "failure_signal": failure_signal,
            "summary": f"Observed failure in {tool_name}",
            "timestamp": "2026-11-05T00:00:00Z",
        },
        failure_classification="LIMITATION_ENVIRONMENT",
        root_cause_hypothesis={
            "hypothesis": "Process handle opened without ACCESS_MASK",
            "rationale": "Failed due to Windows handle access control.",
            "supporting_evidence": ["Access is denied in OpenProcess"],
            "status": "CONFIRMED",
        },
        review_status="UNDER_REVIEW",
    )
    verify_root_cause(
        candidate=cand,
        verified_root_cause="OpenProcess was called with PROCESS_ALL_ACCESS inside restricted AppContainer.",
        reproduction_procedure="Call OpenProcess with PROCESS_ALL_ACCESS on PID 1234 from low-integrity token.",
        corrective_recommendation="Request minimal access rights (PROCESS_QUERY_LIMITED_INFORMATION) when querying state.",
        regression_test="tests/test_win32_process_access.py::test_restricted_open_process",
        scope_of_applicability="All Windows process management routines in jarvis/core/process.py.",
        confidence_level="HIGH",
    )
    transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW", reason="Verified by reproduction test.")
    return cand


def _setup_mock_lessons_file(root: Path) -> Path:
    """Prepares a minimal canonical docs/agent/LESSONS.md under root."""
    lessons_dir = root / "docs" / "agent"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    lessons_file = lessons_dir / "LESSONS.md"
    content = (
        "# Engineering Lessons Learned\n\n"
        "## LES-001: SQLite WAL Concurrency Locking\n\n"
        "- **Lesson ID:** `LES-001`\n"
        "- **Subsystem Domain:** Persistence / SQLite\n"
        "- **Date Recorded:** 2026-11-01\n"
        "- **Status:** ACTIVE\n"
        "- **Observed Problem:**\n"
        "  Multiple threads attempting concurrent writes caused database locked errors.\n"
        "- **Reproduction Evidence:**\n"
        "  Concurrency stress test with 10 parallel worker threads.\n"
        "- **Verified Root Cause & Unresolved Mechanisms:**\n"
        "  - *Verified Mechanism:* SQLite single writer constraint under WAL mode.\n"
        "- **Prevention Rules:**\n"
        "  1. Serialize all writes through SingleWriterActor queue.\n"
        "- **Supporting Files & Commands:**\n"
        "  - `tests/test_sqlite_wal.py`\n"
        "- **Confidence & Applicability Boundaries:**\n"
        "  - *Confidence:* **HIGH**\n"
        "  - *Applicability:* All SQLite database access across the repository.\n"
    )
    lessons_file.write_text(content, encoding="utf-8")
    return lessons_file


# =============================================================================
# 14 Lifecycle & Continuity Scenarios
# =============================================================================

def test_scenario_01_failed_tool_execution_produces_evidence_backed_envelope(tmp_path: Path):
    """Scenario 1: Failed tool execution produces evidence-backed event envelope."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "conversationId": "conv-test-01",
        "stepIdx": 1,
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "pytest tests/test_failure.py"},
        },
        "toolOutput": "Running tests...\nFAIL: test_something",
        "error": "exit status 1: AssertionError: 42 != 43\nStack trace line 12",
    }

    _, envelope = process_hook_payload(payload, custom_storage_dir=events_dir)

    assert envelope is not None
    assert len(envelope.event_id) == 24
    assert envelope.tool_name == "run_command"
    assert envelope.exit_code == 1
    assert "AssertionError" in (envelope.failure_signal or "")
    assert envelope.schema_version == 1

    # Verify bounded evidence persisted to disk
    persisted_path = events_dir / f"{envelope.event_id}.json"
    assert persisted_path.exists()
    assert persisted_path.stat().st_size <= 65536


def test_scenario_02_event_resolves_to_candidate_without_inventing_root_cause(tmp_path: Path):
    """Scenario 2: Event resolves to candidate without inventing root cause."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    raw_env = EventEnvelope(
        schema_version=1,
        event_id="evt002a1b2c3d4e5f6789012",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-002",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="FileNotFoundError: [WinError 2] The system cannot find the file specified",
        exit_code=2,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:00:00Z",
        evidence_references={"stderr": "FileNotFoundError: script_missing.py does not exist"},
        safe_summary="Tool 'run_command' failed with FileNotFoundError",
    )

    cand, status = pipeline.ingest_event_envelope(raw_env)
    assert cand is not None
    assert status == "CREATED"

    # Layer A: Observed facts match
    obs = cand.layer_a_facts["observed_behavior"]
    assert obs["tool_name"] == "run_command"
    assert obs["exit_code"] == 2
    assert "FileNotFoundError" in obs["failure_signal"]

    # Layer B: Proposals exist but are unverified / inconclusive
    hyp = cand.layer_b_proposals["root_cause_hypothesis"]
    assert hyp.get("hypothesis") != ""
    assert cand.layer_b_proposals["failure_classification"] == "INCONCLUSIVE"

    # Layer C: Verified root cause must NOT be invented
    assert cand.layer_c_verified["verified_root_cause"] is None
    assert cand.layer_c_verified["reproduction_procedure"] == ""
    assert cand.layer_c_verified["corrective_recommendation"] == ""
    assert cand.review_status == "OBSERVED"


def test_scenario_03_missing_evidence_produces_inconclusive_candidate(tmp_path: Path):
    """Scenario 3: Missing evidence produces inconclusive candidate."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    # Envelope with empty evidence dict and generic signal
    envelope = EventEnvelope(
        schema_version=1,
        event_id="evt003empty00000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-003",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="Generic exit status 1",
        exit_code=1,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:10:00Z",
        evidence_references={},
        safe_summary="Generic exit status 1",
    )

    cand, status = pipeline.ingest_event_envelope(envelope)
    assert cand is not None
    assert status == "CREATED"
    assert cand.review_status == "OBSERVED"
    assert cand.verified_root_cause is None
    assert cand.confidence_level in ("UNCONFIRMED", "LOW")


def test_scenario_04_candidate_survives_simulated_session_boundary(tmp_path: Path):
    """Scenario 4: Candidate survives simulated session boundary (persistent JSON round-trip)."""
    candidates_dir = tmp_path / "candidates"

    # Session 1: Ingest candidate
    pipeline_s1 = FailureCandidatePipeline(candidates_dir=candidates_dir)
    env = EventEnvelope(
        schema_version=1,
        event_id="evt004session000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-004",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="IndexError: list index out of range",
        exit_code=1,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:20:00Z",
        evidence_references={"stderr": "IndexError"},
        safe_summary="Tool 'run_command' failed with IndexError",
    )
    cand_s1, status = pipeline_s1.ingest_event_envelope(env)
    assert cand_s1 is not None
    assert status == "CREATED"
    cand_id = cand_s1.candidate_id

    # Session 2: Fresh pipeline instance pointing to same directory
    pipeline_s2 = FailureCandidatePipeline(candidates_dir=candidates_dir)
    loaded = pipeline_s2.load_candidate(cand_id)

    assert loaded is not None
    assert loaded.candidate_id == cand_s1.candidate_id
    assert loaded.deduplication_key == cand_s1.deduplication_key
    assert loaded.layer_a_facts == cand_s1.layer_a_facts
    assert loaded.layer_b_proposals == cand_s1.layer_b_proposals
    assert loaded.layer_c_verified == cand_s1.layer_c_verified
    assert loaded.review_status == cand_s1.review_status


def test_scenario_05_repeated_events_do_not_overwrite_unrelated_candidates(tmp_path: Path):
    """Scenario 5: Repeated events do not overwrite unrelated candidates, and duplicates are handled."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    # Event A
    env_a = EventEnvelope(
        schema_version=1,
        event_id="evt005aaaaa00000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-005a",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="AssertionError: Expected true got false",
        exit_code=1,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:30:00Z",
        evidence_references={"stderr": "AssertionError"},
        safe_summary="run_test_a failed",
    )
    cand_a, status_a = pipeline.ingest_event_envelope(env_a)
    assert cand_a is not None
    assert status_a == "CREATED"

    # Unrelated Event B
    env_b = EventEnvelope(
        schema_version=1,
        event_id="evt005bbbbb00000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-005b",
        step_index=2,
        tool_name="read_file",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="PermissionError: [WinError 5] Access is denied",
        exit_code=0,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:31:00Z",
        evidence_references={"error": "Access is denied"},
        safe_summary="read_file failed",
    )
    cand_b, status_b = pipeline.ingest_event_envelope(env_b)
    assert cand_b is not None
    assert status_b == "CREATED"
    assert cand_b.candidate_id != cand_a.candidate_id

    # Verify Candidate A is completely preserved on disk
    loaded_a = pipeline.load_candidate(cand_a.candidate_id)
    assert loaded_a is not None
    assert loaded_a.observed_behavior["failure_signal"] == "AssertionError: Expected true got false"

    # Duplicate Event A (identical reliable identity: same tool, step_index, exit_code)
    env_a_dup = EventEnvelope(
        schema_version=1,
        event_id="evt005aaaaa_dup0000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-005a",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="AssertionError: Expected true got false",
        exit_code=1,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:32:00Z",
        evidence_references={"stderr": "AssertionError"},
        safe_summary="run_test_a duplicate failed",
    )
    cand_dup, status_dup = pipeline.ingest_event_envelope(env_a_dup)
    assert cand_dup is not None
    assert status_dup == "EXACT_RELIABLE_DUPLICATE"
    assert cand_dup.candidate_id == cand_a.candidate_id


def test_scenario_06_conflicting_evidence_prevents_automatic_verification(tmp_path: Path):
    """Scenario 6: Conflicting evidence prevents automatic verification and preview generation."""
    cand = LessonCandidate(
        candidate_id="LCAND-conflict01",
        source_event_ids=["evt-c1", "evt-c2"],
        observed_behavior={"summary": "Conflicting timeout vs memory error"},
        failure_classification="CONCURRENCY_THREADING",
        root_cause_hypothesis={
            "hypothesis": "Deadlock in worker thread",
            "rationale": "Contradictory traces observed across runs.",
            "status": "INCONCLUSIVE",
        },
        review_status="OBSERVED",
    )

    # Attempt to preview unverified candidate must fail closed
    try:
        CandidatePromotionGate.prepare_preview(cand)
        err_raised = False
    except ValueError as e:
        err_raised = True
        assert "lacks verified_root_cause" in str(e)
    assert err_raised, "Expected ValueError when preparing preview for unverified candidate."


def test_scenario_07_candidate_cannot_approve_itself():
    """Scenario 7: Candidate cannot approve itself; approval requires external Action Manifest Digest binding."""
    cand = _build_verified_candidate()

    # Self-approval attempt with arbitrary or candidate-derived string
    success, msg = CandidatePromotionGate.verify_approval_token(
        candidate=cand,
        approval_token="SELF_APPROVED_BY_AGENT",
        preview_digest_or_manifest="sha256_different_digest_string",
    )
    assert not success
    assert "TOKEN_DIGEST_MISMATCH" in msg

    # Empty token fails closed
    success_empty, msg_empty = CandidatePromotionGate.verify_approval_token(
        candidate=cand,
        approval_token="",
        preview_digest_or_manifest="some_digest",
    )
    assert not success_empty
    assert msg_empty == "TOKEN_OR_DIGEST_EMPTY"


def test_scenario_08_modified_candidate_invalidates_approval_bound_to_previous_contents(tmp_path: Path):
    """Scenario 8: Modified candidate invalidates approval bound to previous contents (digest mismatch)."""
    mock_lessons = _setup_mock_lessons_file(tmp_path)
    cand = _build_verified_candidate(candidate_id="LCAND-mutation01")

    # Generate preview and manifest for the original candidate state
    preview = CandidatePromotionGate.prepare_preview(
        cand, lessons_file=mock_lessons, workspace_root=tmp_path
    )
    action_digest = preview["action_manifest_digest"]

    # Valid approval bound to the original state
    valid, msg = CandidatePromotionGate.verify_approval_token(
        candidate=cand,
        approval_token=action_digest,
        preview_digest_or_manifest=preview,
    )
    assert valid, f"Expected approval to bind, got: {msg}"

    # Mutate candidate content (e.g., change prevention rule)
    cand.corrective_recommendation = "MALICIOUS OR UNVERIFIED NEW RULE INJECTED POST-APPROVAL"

    # Verify that the previous approval token is now REJECTED
    valid_after_mutation, msg_after = CandidatePromotionGate.verify_approval_token(
        candidate=cand,
        approval_token=action_digest,
        preview_digest_or_manifest=preview,
    )
    assert not valid_after_mutation
    assert "CANDIDATE_MUTATED" in msg_after


def test_scenario_09_approved_candidate_produces_preview_without_disk_writes(tmp_path: Path):
    """Scenario 9: Approved candidate can produce proposed promotion diff without disk writes."""
    mock_lessons = _setup_mock_lessons_file(tmp_path)
    initial_content = mock_lessons.read_text(encoding="utf-8")
    initial_sha = hashlib.sha256(initial_content.encode("utf-8")).hexdigest()

    cand = _build_verified_candidate(candidate_id="LCAND-previewonly01")

    # Generate preview
    preview = CandidatePromotionGate.prepare_preview(
        cand, lessons_file=mock_lessons, workspace_root=tmp_path, title="Win32 Process Rights"
    )

    assert "proposed_diff" in preview
    assert "@@" in preview["proposed_diff"]
    assert "action_manifest_digest" in preview
    assert preview["status"] == "PREVIEW_ONLY"

    # Strictly verify the target file on disk was NOT mutated
    post_content = mock_lessons.read_text(encoding="utf-8")
    post_sha = hashlib.sha256(post_content.encode("utf-8")).hexdigest()
    assert initial_sha == post_sha
    assert initial_content == post_content


def test_scenario_10_unapproved_candidate_cannot_modify_permanent_lessons(tmp_path: Path):
    """Scenario 10: Unapproved candidate cannot modify permanent lessons (fail-closed promotion)."""
    mock_lessons = _setup_mock_lessons_file(tmp_path)
    initial_bytes = mock_lessons.read_bytes()

    cand = _build_verified_candidate(candidate_id="LCAND-promoteattempt01")

    # In-process promote_candidate call must return PROMOTION_PROHIBITED
    success, msg = CandidatePromotionGate.promote_candidate(
        cand, lessons_file=mock_lessons, approval_token="FAKE_OR_VALID_TOKEN"
    )

    assert not success
    assert "PROMOTION_PROHIBITED" in msg

    # File on disk must remain pristine
    assert mock_lessons.read_bytes() == initial_bytes


def test_scenario_11_new_session_recovers_state_and_identifies_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Scenario 11: New session recovers state and identifies drift against live Git."""
    state_file = tmp_path / "PROJECT_STATE.md"
    state_file.write_text(
        "# Project State Snapshot\n\n"
        "## 1. Verified Working Tree Baseline\n"
        "- **Active Git Branch:** `test-branch`\n"
        "- **Recorded HEAD Commit SHA:** `12345678`\n"
        "- **Validation Timestamp:** 2026-11-05T00:00:00Z\n\n"
        "### Unstaged Tracked Modifications (1 files):\n"
        "- `file_a.py`\n\n"
        "### Exact Untracked File Inventory (1 files tracked by category):\n"
        "- `file_b.py`\n",
        encoding="utf-8",
    )

    # 1. Aligned scenario
    def mock_git_aligned(root: Path | None = None) -> dict[str, Any]:
        return {
            "available": True,
            "branch": "test-branch",
            "head": "12345678",
            "tracked_modified_count": 1,
            "untracked_count": 1,
            "staged_count": 0,
            "tracked_modified_files": ["file_a.py"],
            "untracked_files": ["file_b.py"],
            "staged_files": [],
        }

    monkeypatch.setattr(ProjectContinuityEngine, "get_live_git_state", mock_git_aligned)
    report_aligned = ProjectContinuityEngine.verify_project_state(state_file, workspace_root=tmp_path)
    assert report_aligned["status"] == "ALIGNED"
    assert len(report_aligned["discrepancies"]) == 0

    # 2. Diverged scenario (HEAD moved and extra file untracked)
    def mock_git_diverged(root: Path | None = None) -> dict[str, Any]:
        return {
            "available": True,
            "branch": "test-branch",
            "head": "99999999",
            "tracked_modified_count": 1,
            "untracked_count": 2,
            "staged_count": 0,
            "tracked_modified_files": ["file_a.py"],
            "untracked_files": ["file_b.py", "file_drift.py"],
            "staged_files": [],
        }

    monkeypatch.setattr(ProjectContinuityEngine, "get_live_git_state", mock_git_diverged)
    report_diverged = ProjectContinuityEngine.verify_project_state(state_file, workspace_root=tmp_path)
    assert report_diverged["status"] == "DIVERGED"
    assert len(report_diverged["discrepancies"]) >= 2
    assert any("HEAD" in d for d in report_diverged["discrepancies"])


def test_scenario_12_malformed_or_missing_continuity_artifacts_fail_safely(tmp_path: Path):
    """Scenario 12: Malformed or missing continuity artifacts fail safely (FAILED_SAFE / UNVERIFIED_STATE)."""
    # Missing file
    res_missing = ProjectContinuityEngine.verify_project_state(tmp_path / "nonexistent_state.md", workspace_root=tmp_path)
    assert res_missing["status"] == "FAILED_SAFE"
    assert res_missing["recovery_state"] == "UNVERIFIED_STATE"
    assert "state artifact unavailable" in res_missing["summary"]

    # Corrupted / empty file
    corrupt_file = tmp_path / "corrupt_state.md"
    corrupt_file.write_text("Not valid markdown header\nRandom corrupt text...", encoding="utf-8")
    res_corrupt = ProjectContinuityEngine.verify_project_state(corrupt_file, workspace_root=tmp_path)
    assert res_corrupt["status"] == "FAILED_SAFE"
    assert res_corrupt["recovery_state"] == "UNVERIFIED_STATE"
    assert "UNVERIFIED_STATE" in res_corrupt["recovery_state"]


def test_scenario_13_secret_bearing_and_path_traversal_handled_safely(tmp_path: Path):
    """Scenario 13: Secret-bearing evidence is redacted and path traversal is rejected."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "conversationId": "conv-secret-test",
        "stepIdx": 2,
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "curl -H 'Authorization: Bearer my_secret_token_abcdef123456'"},
        },
        "toolOutput": "",
        "error": "exit status 1: Authorization: Bearer my_secret_token_abcdef123456 failed",
    }

    _, envelope = process_hook_payload(payload, custom_storage_dir=events_dir)
    assert envelope is not None

    # Check evidence redaction
    assert "my_secret_token_abcdef123456" not in (envelope.failure_signal or "")
    assert "[REDACTED" in (envelope.failure_signal or "")

    # Path traversal rejection in evidence collection
    traversal_path = tmp_path / ".." / ".." / "windows" / "win.ini"
    res = collect_evidence_reference(
        target_path=traversal_path,
        allowed_root=tmp_path,
    )
    assert res["status"] == "UNAVAILABLE"
    assert "REJECTED" in (res["error"] or "") or "CONFINEMENT" in (res["error"] or "")


def test_scenario_14_dynamic_retrieval_never_treats_unpromoted_candidates_as_active_lessons(tmp_path: Path):
    """Scenario 14: Dynamic retrieval never treats unpromoted candidates as active lessons."""
    mock_lessons = _setup_mock_lessons_file(tmp_path)

    # Create candidate in a candidates directory with distinct keywords
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    raw_env = EventEnvelope(
        schema_version=1,
        event_id="evt014audio00000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-014",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="AudioEngineWASAPIBufferUnderrun in audio stream",
        exit_code=1,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T01:50:00Z",
        evidence_references={"stderr": "AudioEngineWASAPIBufferUnderrun"},
        safe_summary="AudioEngineWASAPIBufferUnderrun in audio stream",
    )
    cand, _ = pipeline.ingest_event_envelope(raw_env)
    assert cand is not None

    # Call retrieve_relevant_lessons for prompt matching the candidate keyword
    retrieved = retrieve_relevant_lessons(
        prompt="AudioEngineWASAPIBufferUnderrun in audio stream",
        matched_categories=["AUDIO"],
        registry_path=mock_lessons,
        allow_external_fixtures=True,
    )

    # Verify that the candidate was NOT retrieved (only LES-001 from mock_lessons is present in registry)
    retrieved_ids = [entry["id"] for entry in retrieved]
    assert cand.candidate_id not in retrieved_ids
    assert not any("AudioEngineWASAPIBufferUnderrun" in entry["title"] for entry in retrieved)


# =============================================================================
# Phase 3D2B Remediation Scenarios (AUD-01 to AUD-06)
# =============================================================================

def test_scenario_15_repeated_event_ingestion_preserves_verified_candidate_data(tmp_path: Path):
    """Scenario 15 (Critical AUD-01): Repeated ingestion must NEVER overwrite or downgrade a verified candidate."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    ev1 = EventEnvelope(
        schema_version=1,
        event_id="evt015repro00000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        conversation_reference="conv-015",
        step_index=3,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="ProcessLookupError: [WinError 87] The parameter is incorrect",
        exit_code=1,
        exit_code_confidence="HIGH",
        timestamp="2026-11-05T02:00:00Z",
        evidence_references={"stderr": "ProcessLookupError in OpenProcess"},
        safe_summary="ProcessLookupError in OpenProcess",
    )

    # 1. Initial ingestion
    cand, status = pipeline.ingest_event_envelope(ev1)
    assert cand is not None
    assert status == "CREATED"
    assert cand.review_status == "OBSERVED"
    assert cand.verified_root_cause is None

    # 2. Complete Layer C verification & transition lifecycle
    verify_root_cause(
        candidate=cand,
        verified_root_cause="Handle closed prior to OpenProcess call; PID recycled.",
        reproduction_procedure="Close process handle, sleep 500ms, call OpenProcess.",
        corrective_recommendation="Hold process handle reference until lifecycle cleanup.",
        regression_test="tests/test_process_lifecycle.py::test_handle_retention",
        scope_of_applicability="All Win32 process handle management in jarvis/core/process.py.",
        confidence_level="HIGH",
    )
    transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")
    persisted, msg = pipeline.persist_candidate(cand)
    assert persisted, f"Failed to persist verified candidate: {msg}"

    # Verify on disk before re-ingestion
    loaded_before = pipeline.load_candidate(cand.candidate_id)
    assert loaded_before is not None
    assert loaded_before.review_status == "PREPARED_FOR_REVIEW"
    assert loaded_before.verified_root_cause == "Handle closed prior to OpenProcess call; PID recycled."

    # 3. Simulate subsequent session re-ingesting the exact same event
    cand2, status2 = pipeline.ingest_event_envelope(ev1)
    assert cand2 is not None
    assert status2 == "EXACT_RELIABLE_DUPLICATE"
    assert cand2.candidate_id == cand.candidate_id

    # 4. CRITICAL INVARIANT VERIFICATION: Inspect disk contents post re-ingestion
    loaded_after = pipeline.load_candidate(cand.candidate_id)
    assert loaded_after is not None
    assert loaded_after.review_status == "PREPARED_FOR_REVIEW"
    assert loaded_after.verified_root_cause == "Handle closed prior to OpenProcess call; PID recycled."
    assert loaded_after.reproduction_procedure == "Close process handle, sleep 500ms, call OpenProcess."
    assert loaded_after.confidence_level == "HIGH"


def test_scenario_16_persist_candidate_refuses_state_downgrades_and_layer_c_erasure(tmp_path: Path):
    """Scenario 16: persist_candidate actively refuses downgrades and Layer C erasure."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand = _build_verified_candidate(candidate_id="LCAND-downgrade01")
    cand.review_status = "PREPARED_FOR_REVIEW"
    success, msg = pipeline.persist_candidate(cand)
    assert success
    assert msg == "PERSISTED"

    # Attempt 1: Persist unverified candidate with same ID (Layer C erasure attempt)
    unverified_cand = LessonCandidate(
        candidate_id="LCAND-downgrade01",
        source_event_ids=["evt-downgrade"],
        observed_behavior={"tool_name": "run_command"},
        failure_classification="INCONCLUSIVE",
        root_cause_hypothesis={},
        verified_root_cause=None,  # Layer C stripped
        review_status="PREPARED_FOR_REVIEW",
    )
    ok_erasure, msg_erasure = pipeline.persist_candidate(unverified_cand)
    assert not ok_erasure
    assert "REFUSED_DOWNGRADE_PRESERVED" in msg_erasure
    assert "verified root cause" in msg_erasure

    # Attempt 2: Persist candidate with regressed lifecycle state (e.g. back to OBSERVED)
    regressed_cand = _build_verified_candidate(candidate_id="LCAND-downgrade01")
    regressed_cand.review_status = "OBSERVED"  # Regressed
    ok_regress, msg_regress = pipeline.persist_candidate(regressed_cand)
    assert not ok_regress
    assert "REFUSED_DOWNGRADE_PRESERVED" in msg_regress
    assert "Cannot downgrade candidate status" in msg_regress

    # Verify original candidate on disk is completely untouched
    loaded = pipeline.load_candidate("LCAND-downgrade01")
    assert loaded is not None
    assert loaded.review_status == "PREPARED_FOR_REVIEW"
    assert loaded.verified_root_cause is not None


def test_scenario_17_monotonic_lifecycle_transitions_and_regression_rejection():
    """Scenario 17: is_monotonic_transition and transition_candidate_lifecycle enforce forward progression."""
    # Monotonic forward transitions
    assert is_monotonic_transition("OBSERVED", "NEEDS_EVIDENCE")
    assert is_monotonic_transition("OBSERVED", "PROPOSED")
    assert is_monotonic_transition("OBSERVED", "UNDER_REVIEW")
    assert is_monotonic_transition("UNDER_REVIEW", "REVIEWED")
    assert is_monotonic_transition("REVIEWED", "PREPARED_FOR_REVIEW")
    assert is_monotonic_transition("PREPARED_FOR_REVIEW", "PREPARED_FOR_REVIEW")
    assert is_monotonic_transition("PREPARED_FOR_REVIEW", "REJECTED")
    assert is_monotonic_transition("PREPARED_FOR_REVIEW", "SUPERSEDED")

    # Non-monotonic backward transitions
    assert not is_monotonic_transition("PREPARED_FOR_REVIEW", "OBSERVED")
    assert not is_monotonic_transition("REVIEWED", "PROPOSED")
    assert not is_monotonic_transition("UNDER_REVIEW", "OBSERVED")
    assert not is_monotonic_transition("REJECTED", "OBSERVED")
    assert not is_monotonic_transition("SUPERSEDED", "PREPARED_FOR_REVIEW")

    # Exception enforcement in transition_candidate_lifecycle
    cand = LessonCandidate(candidate_id="LCAND-transition01", review_status="PREPARED_FOR_REVIEW")
    with pytest.raises(ValueError, match="Non-monotonic lifecycle regression"):
        transition_candidate_lifecycle(cand, "OBSERVED")

    # Monotonic transition succeeds
    transition_candidate_lifecycle(cand, "REJECTED", reason="Invalidated by human")
    assert cand.review_status == "REJECTED"


def test_scenario_18_candidate_id_collision_conflict_handling(tmp_path: Path):
    """Scenario 18: Candidate ID collisions with conflicting contents are quarantined with distinct IDs."""
    candidates_dir = tmp_path / "candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    cand1 = LessonCandidate(
        candidate_id="LCAND-collisiontest",
        source_event_ids=["evt-1"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1},
        deduplication_key="REL:run_command:step_1:1",
    )
    pipeline.persist_candidate(cand1)

    cand2 = LessonCandidate(
        candidate_id="LCAND-collisiontest",  # Identical ID
        source_event_ids=["evt-2"],  # Conflicting source event
        observed_behavior={"tool_name": "write_file", "exit_code": 0},  # Conflicting tool
        deduplication_key="REL:write_file:step_2:0",
    )

    is_dup, dup_status, existing = check_candidate_deduplication(cand2, [cand1])
    assert not is_dup
    assert dup_status == "CANDIDATE_ID_COLLISION_CONFLICT"
    assert existing is not None


def test_scenario_19_corrupted_candidate_files_handled_safely(tmp_path: Path):
    """Scenario 19: Corrupted candidate files do not crash listing and refuse silent overwrite."""
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    # Create a corrupt JSON file
    corrupt_file = candidates_dir / "LCAND-corrupt01.json"
    corrupt_file.write_text("{NOT_VALID_JSON: true,,,", encoding="utf-8")

    # list_candidates ignores corrupt file without raising
    cands = pipeline.list_candidates()
    assert len(cands) == 0

    # persist_candidate refuses to overwrite corrupt file
    new_cand = LessonCandidate(candidate_id="LCAND-corrupt01", review_status="OBSERVED")
    success, msg = pipeline.persist_candidate(new_cand)
    assert not success
    assert "CORRUPT_CANDIDATE_PRESERVED" in msg
    assert "{NOT_VALID_JSON: true,,," in corrupt_file.read_text(encoding="utf-8")


def test_scenario_20_multi_directory_event_discovery_and_nonexistent_directory_error(tmp_path: Path):
    """Scenario 20 (AUD-03): Multi-directory event scanning and explicit error for nonexistent directories."""
    candidates_dir = tmp_path / "candidates"
    repo_events_dir = tmp_path / "data" / "learning_events"
    repo_events_dir.mkdir(parents=True, exist_ok=True)

    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    # 1. Nonexistent directory returns explicit DIRECTORY_NOT_FOUND error
    bad_dir = tmp_path / "nonexistent_events_dir"
    res_bad = pipeline.scan_and_ingest_events(events_dir=bad_dir)
    assert len(res_bad) == 1
    assert res_bad[0]["error"] == "DIRECTORY_NOT_FOUND"
    assert str(bad_dir) in res_bad[0]["path"]

    # 2. Event in repo directory
    approved_dirs = get_approved_event_directories(workspace_root=tmp_path)
    assert any(tmp_path in d.parents or d == repo_events_dir for d in approved_dirs)

    ev1 = EventEnvelope(
        schema_version=1,
        event_id="evt020repo000000000000000",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        step_index=1,
        tool_name="run_command",
        tool_outcome="FAILURE",
        error_status="NON_EMPTY",
        failure_signal="Simulated failure in repo",
        exit_code=1,
        exit_code_confidence="HIGH",
        safe_summary="Simulated failure in repo",
    )
    (repo_events_dir / f"{ev1.event_id}.json").write_text(
        json.dumps(ev1.to_dict()), encoding="utf-8"
    )

    # Scan with workspace_root pointing to tmp_path
    res = pipeline.scan_and_ingest_events(workspace_root=tmp_path)
    assert len(res) == 1
    assert res[0]["event_id"] == ev1.event_id
    assert res[0]["status"] == "CREATED"

    # Re-scan must report duplicate without failing
    res_dup = pipeline.scan_and_ingest_events(workspace_root=tmp_path)
    assert len(res_dup) == 1
    assert res_dup[0]["status"] == "EXACT_RELIABLE_DUPLICATE"


def test_scenario_21_stop_hook_payload_processing_and_hooks_json_registration(tmp_path: Path):
    """Scenario 21 (AUD-04): Stop hook parses termination envelopes and hooks.json registers Stop."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    # 1. Stop payload with error termination
    payload_err = {
        "conversationId": "conv-stop-test",
        "executionNum": 4,
        "terminationReason": "ERROR",
        "error": "Task exceeded invocation budget or model error",
        "fullyIdle": True,
    }
    direct_envelope = parse_stop(payload_err)
    assert direct_envelope is not None
    assert direct_envelope.event_type == "STOP"

    _, envelope = process_hook_payload(payload_err, custom_storage_dir=events_dir)
    assert envelope is not None
    assert envelope.event_type == "STOP"
    assert envelope.collection_status == "OBSERVED_FAILURE"
    assert envelope.execution_number == 4
    assert envelope.termination_reason == "ERROR"

    # Persisted to disk
    stop_file = events_dir / f"{envelope.event_id}.json"
    assert stop_file.exists()

    # 2. Stop payload with routine completion (NO_TOOL_CALL): filtered out
    payload_success = {
        "conversationId": "conv-stop-test2",
        "executionNum": 5,
        "terminationReason": "NO_TOOL_CALL",
        "error": "",
        "fullyIdle": True,
    }
    _, env_success = process_hook_payload(payload_success, custom_storage_dir=events_dir)
    assert env_success is not None
    assert env_success.collection_status == "SUCCESS"
    success_file = events_dir / f"{env_success.event_id}.json"
    assert not success_file.exists(), "Routine success must not be persisted to disk."

    # 3. Verify .agents/hooks.json contains Stop registration
    hooks_file = Path(__file__).resolve().parent.parent / ".agents" / "hooks.json"
    assert hooks_file.exists()
    hooks_data = json.loads(hooks_file.read_text(encoding="utf-8"))
    fc_hook = hooks_data.get("failure-collector", {})
    assert "Stop" in fc_hook
    assert isinstance(fc_hook["Stop"], list)
    assert len(fc_hook["Stop"]) >= 1
    assert fc_hook["Stop"][0]["command"].startswith("if exist tools\\failure_collector.py")


def test_scenario_22_continuity_pre_invocation_hook_drift_detection_and_turn_suppression(monkeypatch: pytest.MonkeyPatch):
    """Scenario 22 (AUD-05): Continuity PreInvocation hook emits drift warnings and suppresses duplicate turns."""
    import io

    # 1. invocationNum > 0 suppresses injection
    stdin_suppressed = io.StringIO(json.dumps({"invocationNum": 2}))
    stdout_suppressed = io.StringIO()
    stderr_suppressed = io.StringIO()
    code = run_continuity_hook(stdin_stream=stdin_suppressed, stdout_stream=stdout_suppressed, stderr_stream=stderr_suppressed)
    assert code == 0
    resp_suppressed = json.loads(stdout_suppressed.getvalue())
    assert resp_suppressed == {"injectSteps": []}

    # 2. invocationNum == 0 with DIVERGED state emits drift warning
    def mock_verify_diverged(*args, **kwargs) -> dict[str, Any]:
        return {
            "status": "DIVERGED",
            "summary": "Live Git baseline diverged from recorded state.",
            "live_git": {"branch": "feat/my-branch"},
            "discrepancies": ["Tracked modified count mismatch: 17 != 16"],
        }

    monkeypatch.setattr(ProjectContinuityEngine, "verify_project_state", mock_verify_diverged)
    stdin_diverged = io.StringIO(json.dumps({"invocationNum": 0}))
    stdout_diverged = io.StringIO()
    stderr_diverged = io.StringIO()
    code_div = run_continuity_hook(stdin_stream=stdin_diverged, stdout_stream=stdout_diverged, stderr_stream=stderr_diverged)
    assert code_div == 0
    resp_div = json.loads(stdout_diverged.getvalue())
    assert len(resp_div["injectSteps"]) == 1
    msg = resp_div["injectSteps"][0]["ephemeralMessage"]
    assert "[PROJECT_CONTINUITY_DRIFT" in msg
    assert "Tracked modified count mismatch" in msg

    # 3. invocationNum == 0 with ALIGNED state emits empty injectSteps
    def mock_verify_aligned(*args, **kwargs) -> dict[str, Any]:
        return {"status": "ALIGNED", "summary": "Working tree aligned."}

    monkeypatch.setattr(ProjectContinuityEngine, "verify_project_state", mock_verify_aligned)
    stdin_aligned = io.StringIO(json.dumps({"invocationNum": 0}))
    stdout_aligned = io.StringIO()
    code_aligned = run_continuity_hook(stdin_stream=stdin_aligned, stdout_stream=stdout_aligned, stderr_stream=stderr_diverged)
    assert code_aligned == 0
    resp_aligned = json.loads(stdout_aligned.getvalue())
    assert resp_aligned == {"injectSteps": []}


def test_scenario_23_content_integrity_digest_terminology_and_fail_closed_gate():
    """Scenario 23 (AUD-02): Content Integrity Digest enforces tamper detection; promote_candidate fails closed."""
    cand = _build_verified_candidate()
    preview = CandidatePromotionGate.prepare_preview(cand)
    manifest_digest = preview["action_manifest_digest"]

    # verify_content_integrity_digest matches
    ok, msg = CandidatePromotionGate.verify_content_integrity_digest(
        candidate=cand,
        approval_token=manifest_digest,
        preview_digest_or_manifest=preview,
    )
    assert ok
    assert "APPROVAL_BOUND" in msg

    # verify_approval_token alias works identically
    ok_alias, msg_alias = CandidatePromotionGate.verify_approval_token(
        candidate=cand,
        approval_token=manifest_digest,
        preview_digest_or_manifest=preview,
    )
    assert ok_alias
    assert "APPROVAL_BOUND" in msg_alias

    # promote_candidate unconditionally fails closed
    ok_prom, msg_prom = CandidatePromotionGate.promote_candidate(
        candidate=cand,
        approval_token=manifest_digest,
    )
    assert not ok_prom
    assert "PROMOTION_PROHIBITED" in msg_prom


def test_scenario_24_evidence_aware_deduplication_and_conflict_isolation(tmp_path: Path):
    """Scenario 24 (Phase 3D2D): Evidence-aware deduplication distinguishes duplicates from evidence conflicts."""
    cand_base = LessonCandidate(
        candidate_id="LCAND-dedup01",
        source_event_ids=["evt-001"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1, "failure_signal": "Access is denied"},
        evidence_references=[{"ref_key": "log", "target": "error.log"}],
        deduplication_key="REL:run_command:step_1:1",
    )

    # 1. Exact duplicate with matching evidence
    cand_identical = LessonCandidate(
        candidate_id="LCAND-dedup01",
        source_event_ids=["evt-001"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1, "failure_signal": "Access is denied"},
        evidence_references=[{"ref_key": "log", "target": "error.log"}],
        deduplication_key="REL:run_command:step_1:1",
    )
    assert is_evidence_equivalent(cand_base, cand_identical)
    is_dup, status, existing = check_candidate_deduplication(cand_identical, [cand_base])
    assert is_dup
    assert status == "EXACT_RELIABLE_DUPLICATE"
    assert existing is cand_base

    # 2. Same candidate ID but differing event ID -> CANDIDATE_ID_COLLISION_CONFLICT
    cand_diff_event = LessonCandidate(
        candidate_id="LCAND-dedup01",
        source_event_ids=["evt-002"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1, "failure_signal": "Access is denied"},
        deduplication_key="REL:run_command:step_1:1",
    )
    is_dup, status, existing = check_candidate_deduplication(cand_diff_event, [cand_base])
    assert not is_dup
    assert status == "CANDIDATE_ID_COLLISION_CONFLICT"

    # 3. Same candidate ID and event ID but differing failure signal -> CANDIDATE_EVIDENCE_CONFLICT
    cand_diff_signal = LessonCandidate(
        candidate_id="LCAND-dedup01",
        source_event_ids=["evt-001"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1, "failure_signal": "Resource busy"},
        evidence_references=[{"ref_key": "log", "target": "error.log"}],
        deduplication_key="REL:run_command:step_1:1",
    )
    is_dup, status, existing = check_candidate_deduplication(cand_diff_signal, [cand_base])
    assert not is_dup
    assert status == "CANDIDATE_EVIDENCE_CONFLICT"

    # 4. Same candidate ID and event ID but differing evidence references -> CANDIDATE_EVIDENCE_CONFLICT
    cand_diff_refs = LessonCandidate(
        candidate_id="LCAND-dedup01",
        source_event_ids=["evt-001"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1, "failure_signal": "Access is denied"},
        evidence_references=[{"ref_key": "trace", "target": "trace.log"}],
        deduplication_key="REL:run_command:step_1:1",
    )
    is_dup, status, existing = check_candidate_deduplication(cand_diff_refs, [cand_base])
    assert not is_dup
    assert status == "CANDIDATE_EVIDENCE_CONFLICT"

    # 5. Distinct candidate ID with same REL deduplication key but differing evidence -> CANDIDATE_EVIDENCE_CONFLICT
    cand_rel_conflict = LessonCandidate(
        candidate_id="LCAND-dedup02",
        source_event_ids=["evt-003"],
        observed_behavior={"tool_name": "run_command", "exit_code": 1, "failure_signal": "Different defect"},
        deduplication_key="REL:run_command:step_1:1",
    )
    is_dup, status, existing = check_candidate_deduplication(cand_rel_conflict, [cand_base])
    assert not is_dup
    assert status == "CANDIDATE_EVIDENCE_CONFLICT"

    # 6. Pipeline ingestion: CANDIDATE_EVIDENCE_CONFLICT is quarantined with _evdiff_ suffix
    pipeline = FailureCandidatePipeline(candidates_dir=tmp_path / "candidates")
    ev1 = EventEnvelope(
        event_id="evt-base-01",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        step_index=1,
        tool_name="run_command",
        exit_code=1,
        failure_signal="Access is denied",
        collection_status="OBSERVED_FAILURE",
    )
    created1, s1 = pipeline.ingest_event_envelope(ev1)
    assert created1 is not None
    assert s1 == "CREATED"
    orig_id = created1.candidate_id

    # Ingest event with identical fingerprint but conflicting failure signal
    ev_conflict = EventEnvelope(
        event_id="evt-base-02",
        event_type="POST_TOOL_USE",
        identity_status="RELIABLE",
        step_index=1,
        tool_name="run_command",
        exit_code=1,
        failure_signal="Device not ready: drive dismounted",
        collection_status="OBSERVED_FAILURE",
    )
    created2, s2 = pipeline.ingest_event_envelope(ev_conflict)
    assert created2 is not None
    assert s2 == "CREATED"
    assert "_evdiff_" in created2.candidate_id
    assert created2.candidate_id != orig_id

    # Verify original candidate remains intact on disk
    loaded1 = pipeline.load_candidate(orig_id)
    assert loaded1 is not None
    assert loaded1.observed_behavior["failure_signal"] == "Access is denied"


def test_scenario_25_semantic_lifecycle_prerequisites_enforcement():
    """Scenario 25 (Phase 3D2D): Enforces semantic prerequisites across lifecycle transitions."""
    cand = LessonCandidate(
        candidate_id="LCAND-lifecycle-sem01",
        review_status="OBSERVED",
        root_cause_hypothesis={},
    )

    # 1. Direct jump from OBSERVED to PREPARED_FOR_REVIEW is rejected
    with pytest.raises(ValueError, match="cannot jump directly to 'PREPARED_FOR_REVIEW'"):
        transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")

    # 2. Transition to PROPOSED requires non-empty hypothesis
    with pytest.raises(ValueError, match="must contain a non-empty hypothesis"):
        transition_candidate_lifecycle(cand, "PROPOSED")

    cand.root_cause_hypothesis = {"hypothesis": "Race condition in thread handle reclamation"}
    transition_candidate_lifecycle(cand, "PROPOSED")
    assert cand.review_status == "PROPOSED"

    # 3. Transition to REVIEWED requires review_findings
    with pytest.raises(ValueError, match="review_findings must not be empty"):
        transition_candidate_lifecycle(cand, "REVIEWED")

    cand.review_findings.append({
        "specialist": "security-auditor",
        "findings": "Verified boundary isolation.",
    })
    transition_candidate_lifecycle(cand, "REVIEWED")
    assert cand.review_status == "REVIEWED"

    # 4. Transition to PREPARED_FOR_REVIEW requires complete Layer C data
    with pytest.raises(ValueError, match="verified_root_cause is missing or empty"):
        transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")

    cand.verified_root_cause = "Thread handle closed prematurely."
    with pytest.raises(ValueError, match="reproduction_procedure is missing or empty"):
        transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")

    cand.reproduction_procedure = "Spawn 2 workers and close handle during barrier."
    with pytest.raises(ValueError, match="corrective_recommendation is missing or empty"):
        transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")

    cand.corrective_recommendation = "Synchronize on completion event before closing handle."
    with pytest.raises(ValueError, match="regression_test is missing or empty"):
        transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")

    cand.regression_test = "tests/test_threading.py::test_handle_race"
    transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")
    assert cand.review_status == "PREPARED_FOR_REVIEW"

    # 5. Non-monotonic regression (PREPARED_FOR_REVIEW -> OBSERVED) is rejected
    with pytest.raises(ValueError, match="Non-monotonic lifecycle regression"):
        transition_candidate_lifecycle(cand, "OBSERVED")

    # 6. Terminal state egress is rejected
    transition_candidate_lifecycle(cand, "REJECTED", reason="Invalidated by benchmark")
    assert cand.review_status == "REJECTED"

    with pytest.raises(ValueError, match="Cannot transition out of terminal state 'REJECTED'"):
        transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")

    with pytest.raises(ValueError, match="Cannot transition out of terminal state 'REJECTED'"):
        transition_candidate_lifecycle(cand, "OBSERVED")


def test_scenario_26_comprehensive_lifecycle_transition_matrix():
    """Scenario 26 (Phase 3D2D): Evaluates the complete lifecycle transition matrix across all state pairs."""
    # Build candidate with full Layer C data and findings to test structural matrix rules
    cand = LessonCandidate(
        candidate_id="LCAND-matrix-test",
        root_cause_hypothesis={"hypothesis": "Valid hypothesis for test"},
        verified_root_cause="Verified cause",
        reproduction_procedure="Repro steps",
        corrective_recommendation="Fix recommendation",
        regression_test="tests/test_foo.py",
        review_findings=[{"finding": "ok"}],
    )

    for from_s in LIFECYCLE_STATES:
        for to_s in LIFECYCLE_STATES:
            cand.review_status = from_s
            valid, msg = validate_lifecycle_transition(cand, to_s)

            # Self-transitions are always valid
            if from_s == to_s:
                assert valid, f"Self transition {from_s} -> {to_s} must be valid"
                continue

            # Terminal states (REJECTED, SUPERSEDED) cannot be transitioned out of
            if from_s in ("REJECTED", "SUPERSEDED"):
                assert not valid, f"Transition out of terminal state {from_s} -> {to_s} must be rejected"
                assert "terminal state" in msg
                continue

            # Active states can transition to REJECTED or SUPERSEDED
            if to_s in ("REJECTED", "SUPERSEDED"):
                assert valid, f"Transition {from_s} -> {to_s} must be allowed"
                continue

            # Direct jump from OBSERVED or NEEDS_EVIDENCE to PREPARED_FOR_REVIEW must be rejected
            if from_s in ("OBSERVED", "NEEDS_EVIDENCE") and to_s == "PREPARED_FOR_REVIEW":
                assert not valid, f"Direct jump {from_s} -> {to_s} must be rejected"
                assert "cannot jump directly" in msg
                continue

            # Monotonic rank regression check
            if not is_monotonic_transition(from_s, to_s):
                assert not valid, f"Regression {from_s} -> {to_s} must be rejected"
                assert "Non-monotonic" in msg


def test_scenario_27_thread_safe_and_process_safe_candidate_persistence(tmp_path: Path):
    """Scenario 27 (Phase 3D2D): Multi-threaded concurrent candidate persistence guarantees zero file collisions."""
    import concurrent.futures

    candidates_dir = tmp_path / "concurrent_candidates"
    pipeline = FailureCandidatePipeline(candidates_dir=candidates_dir)

    # 1. 20 concurrent threads writing distinct candidates
    def _write_candidate(idx: int) -> tuple[bool, str]:
        cand = LessonCandidate(
            candidate_id=f"LCAND-thread-{idx:03d}",
            source_event_ids=[f"evt-{idx:03d}"],
            observed_behavior={"tool_name": "run_command", "exit_code": 1, "thread_idx": idx},
            deduplication_key=f"REL:run_command:step_{idx}:1",
        )
        return pipeline.persist_candidate(cand)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_write_candidate, i) for i in range(20)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert all(success for success, _ in results), f"Thread persistence failure: {results}"
    stored = pipeline.list_candidates()
    assert len(stored) == 20

    # Verify no leaked .tmp_* files
    tmp_files = list(candidates_dir.glob("*.tmp_*"))
    assert len(tmp_files) == 0, f"Leaked temporary files found: {tmp_files}"

    # 2. 10 concurrent threads updating the same candidate ID
    def _update_same_candidate(thread_id: int) -> tuple[bool, str]:
        c = LessonCandidate(
            candidate_id="LCAND-shared-concurrency",
            source_event_ids=["evt-shared"],
            observed_behavior={"tool_name": "run_command", "exit_code": 1, "last_thread": thread_id},
            deduplication_key="REL:run_command:step_99:1",
            review_status="OBSERVED",
        )
        return pipeline.persist_candidate(c)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        shared_futures = [executor.submit(_update_same_candidate, i) for i in range(10)]
        shared_results = [f.result() for f in concurrent.futures.as_completed(shared_futures)]

    assert all(success for success, _ in shared_results), f"Shared persistence failure: {shared_results}"
    loaded = pipeline.load_candidate("LCAND-shared-concurrency")
    assert loaded is not None
    assert loaded.candidate_id == "LCAND-shared-concurrency"


def test_scenario_28_session_start_hook_lifecycle_and_turn_zero_deduplication(tmp_path: Path, monkeypatch):
    """Scenario 28 (Phase 3D2D): SessionStart executes once and PreInvocation skips duplicate check on turn 0."""
    import io
    import time

    from tools.continuity_manager import _get_session_marker_path

    conv_id = f"test-session-{time.time()}"
    marker = _get_session_marker_path(conv_id)
    if marker.exists():
        marker.unlink()

    call_count = {"verify": 0}

    def mock_verify_state(*args, **kwargs) -> dict[str, Any]:
        call_count["verify"] += 1
        return {"status": "ALIGNED", "summary": "Working tree aligned."}

    monkeypatch.setattr(ProjectContinuityEngine, "verify_project_state", mock_verify_state)

    # 1. SessionStart hook executes
    stdin_session = io.StringIO(json.dumps({"conversationId": conv_id}))
    stdout_session = io.StringIO()
    stderr_session = io.StringIO()
    code_start = run_session_start_hook(stdin_stream=stdin_session, stdout_stream=stdout_session, stderr_stream=stderr_session)

    assert code_start == 0
    assert json.loads(stdout_session.getvalue().strip()) == {"injectSteps": []}
    assert call_count["verify"] == 1
    assert marker.exists()

    # 2. PreInvocation hook runs on turn 0: detects session marker and skips duplicate verify
    stdin_turn0 = io.StringIO(json.dumps({"conversationId": conv_id, "invocationNum": 0}))
    stdout_turn0 = io.StringIO()
    stderr_turn0 = io.StringIO()
    code_turn0 = run_continuity_hook(stdin_stream=stdin_turn0, stdout_stream=stdout_turn0, stderr_stream=stderr_turn0)

    assert code_turn0 == 0
    assert json.loads(stdout_turn0.getvalue().strip()) == {"injectSteps": []}
    # verify_project_state was NOT called a second time
    assert call_count["verify"] == 1

    # Cleanup marker
    if marker.exists():
        marker.unlink()

