# ENG-OS-03A — Execution Evidence Provenance & RED-GREEN Verification
## Architectural Specification, Threat Model, and Implementation Report

- **Date:** 2026-11-05
- **Author:** Principal Python Reliability Engineer, Test Infrastructure Architect, Security Auditor
- **Status:** IMPLEMENTED & EMPIRICALLY VERIFIED
- **Target Repository:** `Jarvis_2.0`
- **Associated Branch:** `feat/engineering-maturity-milestone`

---

## 1. Executive Summary & Purpose

The objective of milestone **ENG-OS-03A** is to establish an immutable, cryptographically verifiable, and empirical execution-evidence mechanism for Jarvis 2.0's continuous engineering intelligence pipeline. Prior to this milestone, continuous learning records could distinguish observed runtime errors from hypothesized fixes, but could not cryptographically verify whether a proposed fix had actually been executed against a real, reproduced test failure.

ENG-OS-03A introduces a strict 5-stage epistemic hierarchy:
1. **Reported Failure:** A runtime failure reported by a tool or logged as a telemetry event (`data/learning_events/`).
2. **Observed Failure:** A failure captured by `FailureCollector` during live session operations.
3. **Reproduced Defect (RED Baseline):** An authoritative, isolated test execution captured by `ExecutionEvidenceCollector` that fails specifically on the reported bug with a non-zero exit code and matching failure signature.
4. **Verified Fix (GREEN Verification):** An authoritative test execution running the **exact same test definition** (identical test file SHA-256 and test selector) with zero mutations to test logic, executing normally with `exit_code == 0`, zero failures, and confirming that the previously failing test is now explicitly `PASSED`.
5. **Review-Ready Conclusion:** A candidate with complete Layer C analysis and verified RED-GREEN evidence envelopes, ready for independent human inspection.

```
+---------------------------------------------------------------------------------------------------+
|                                  ENG-OS-03A EPISTEMIC LIFECYCLE                                    |
|                                                                                                   |
|  [Reported Failure]                                                                               |
|          |                                                                                        |
|          v                                                                                        |
|  [Observed Failure] (FailureCollector -> Event JSON)                                              |
|          |                                                                                        |
|          v                                                                                        |
|  [Reproduced Defect] (RED Baseline: exit!=0, test failed, AUTHORITATIVE_LOCAL_EXECUTION)           |
|          |                                                                                        |
|          v                                                                                        |
|  [Verified Fix] (GREEN Fix: exit==0, test passed, identical file SHA-256 & selector)              |
|          |                                                                                        |
|          v                                                                                        |
|  [Review-Ready Conclusion] (Layer C Complete + Provenance Verified -> PREPARED_FOR_REVIEW)        |
|          |                                                                                        |
|          X  <--- UNCONDITIONAL PROMOTION BARRIER: promote_candidate() -> PROMOTION_PROHIBITED     |
|          |                                                                                        |
|  [Permanent Memory (LESSONS.md)] (Requires explicit external human action)                        |
+---------------------------------------------------------------------------------------------------+
```

---

## 2. Complete Schema Documentation: `ExecutionEvidenceEnvelope`

The `ExecutionEvidenceEnvelope` is a sealed dataclass defined in [`tools/execution_evidence.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tools/execution_evidence.py). It captures complete empirical evidence across four structured sub-domains:

### Part 1: Identity & Provenance
- `envelope_id` (`str`): Unique identifier (`EEV-<uuid4[:12]>`).
- `candidate_id` (`str`): Bound candidate identifier (`LCAND-<hex12>`).
- `execution_pair_id` (`str`): Correlating identifier linking a RED baseline with its paired GREEN verification run (`EPAIR-<uuid4[:12]>`).
- `execution_role` (`str`): Role of this execution in the verification workflow:
  - `BASELINE`: Initial reproduction run establishing the failing state (RED).
  - `FIX_VERIFICATION`: Post-fix execution confirming resolution (GREEN).
  - `REGRESSION_CHECK`: Auxiliary execution ensuring no regressions in wider test suites.
- `provenance_status` (`str`): Trust level assigned to this envelope:
  - `AUTHORITATIVE_LOCAL_EXECUTION`: Captured directly by `ExecutionEvidenceCollector` on the local machine during this process.
  - `IMPORTED_UNVERIFIED`: Deserialized from external JSON, untrusted file, or candidate input.
- `captured_at` (`str`): ISO-8601 UTC timestamp of execution capture.
- `observer_identity` (`str`): Identity of the capturing component (`Jarvis-ExecutionEvidenceCollector/1.0`).

### Part 2: Execution Context
- `git_commit_sha` (`str`): 40-character Git HEAD SHA at time of execution.
- `git_branch` (`str`): Active Git branch name.
- `working_directory` (`str`): Confined directory where the command was executed.
- `command` (`str`): Sanitized command string invoked (e.g. `pytest tests/test_sample.py -k test_foo`).
- `test_target` (`str`): Path to test file relative to workspace root.
- `test_file_sha256` (`str`): Cryptographic SHA-256 digest of the test file contents at time of execution.
- `test_selector` (`str`): Specific nodeid or `-k` expression used for test filtering.
- `execution_duration_ms` (`float`): Subprocess execution wall-clock time in milliseconds.
- `environment_fingerprint` (`dict[str, str]`): Dict containing `python_version`, `platform`, `pytest_version`, and `architecture`.

### Part 3: Execution Outcome
- `exit_code` (`int`): Subprocess process exit code (0 for pass, non-zero for failure).
- `process_terminated_normally` (`bool`): `True` if process terminated via normal exit; `False` if killed by timeout, SIGKILL, Windows Job Object limit, or segfault/abort.
- `test_collection_count` (`int`): Total tests collected by test runner.
- `tests_passed` (`int`): Count of passing tests.
- `tests_failed` (`int`): Count of failing tests.
- `tests_errored` (`int`): Count of setup/teardown/syntax errors.
- `tests_skipped` (`int`): Count of skipped tests.
- `test_outcomes` (`dict[str, str]`): Map of individual test node IDs to status (`PASSED`, `FAILED`, `ERROR`, `SKIPPED`).
- `failure_signatures` (`list[dict[str, Any]]`): List of structured failure records containing `nodeid`, `exception_type`, `snippet_hash`, and redacted `snippet`.

### Part 4: Evidence Integrity
- `envelope_content_hash` (`str`): SHA-256 digest computed across normalized, deterministic JSON representation of all payload fields.
- `envelope_signature` (`str`): Local witness binding signature (`LOCAL-WITNESS:<digest>` or `RCPT-<hex12>`). *Note (Remediated in ENG-OS-03A2):* Grounded to local witness hashing, immutable `ExecutionReceipt`, and `EvidenceStore` registration rather than ungrounded claims of out-of-process HMAC keys.
- `evidence_completeness` (`str`): `COMPLETE` if JUnit XML and process streams were parsed successfully; `PARTIAL` if process timed out or JUnit XML was unparseable.

---

## 3. Evidence Collector Architecture & Sandboxing

The `ExecutionEvidenceCollector` manages execution capture through strict boundaries (remediated in ENG-OS-03A2):

1. **Path Confinement:** Validates that `test_target` and `working_directory` reside strictly within `workspace_root` via `validate_path_confinement`. Traversal (`../`) and external drives are rejected with `ValueError`.
2. **Command & Argument Allowlist:** Enforces strict argument validation via `validate_collector_arguments()`. Prohibits dangerous pytest options (`-p`, `-o`, `-c`, `--rootdir`, `--override-ini`, etc.) and strictly rejects shell metacharacters (`&`, `|`, `;`, `>`, `<`, backticks, `$()`).
3. **Execution Isolation:** Subprocesses are executed via `subprocess.run` with `shell=False`, explicit timeout (default 30.0s), and working directory bound to `workspace_root`.
4. **Structured Instrumentation:** The collector passes `--junitxml=<tmp_file>` to pytest in an isolated temporary location, parses the XML `<testcase>` and `<failure>` nodes, and deletes the temporary file immediately.
5. **Information Redaction:** Failure messages, tracebacks, and stdout/stderr streams are scrubbed via `redact_sensitive_text` to eliminate API keys, Bearer tokens, passwords, and private key strings before recording into the envelope.
6. **Immutable Execution Receipt:** The collector writes an immutable, deeply frozen `ExecutionReceipt` object registered immediately into `EvidenceStore` (SQLite WAL `evidence_registry.db` + canonical JSON artifact `data/execution_evidence/receipts/`).
7. **Provenance Attribution:** The collector marks in-process envelopes as `AUTHORITATIVE_LOCAL_EXECUTION`. Deserialization functions (`from_dict`) downgrade unverified payloads to `IMPORTED_UNVERIFIED`. Cross-session loads re-authenticated against `EvidenceStore` receive `VERIFIED_LOCAL_EXECUTION`.

---

## 4. Threat Model Coverage (Neutralization of Vectors TV-01 through TV-10)

| Vector | Threat Description | Defense Mechanism & Neutralization Point |
|---|---|---|
| **TV-01** | **Synthetic / Hallucinated Payload Injection:** An agent or LLM constructs a JSON object claiming a test passed. | `ExecutionEvidenceEnvelope.from_dict()` checks `EvidenceStore` for a valid `ExecutionReceipt`. Without valid store authentication, it unconditionally downgrades to `IMPORTED_UNVERIFIED`. `RedGreenVerificationEngine` rejects any envelope where `provenance_status not in ("AUTHORITATIVE_LOCAL_EXECUTION", "VERIFIED_LOCAL_EXECUTION")`. |
| **TV-02** | **Provenance Spoofing:** External JSON file explicitly sets `provenance_status="AUTHORITATIVE_LOCAL_EXECUTION"`. | Deserializer ignores caller-provided provenance status unless verified against the durable `EvidenceStore`. In-process receipts are immutable frozen dataclasses (`ExecutionReceipt`). |
| **TV-03** | **Test Definition Mutation:** Agent weakens or deletes test assertions between RED and GREEN runs. | `RedGreenVerificationEngine.validate_fix_green()` verifies `green_env.test_file_sha256 == red_env.test_file_sha256`. Mutation triggers immediate `TEST_DEFINITION_MUTATED` rejection. |
| **TV-04** | **Test Selection Narrowing:** Agent changes `-k test_narrow` or filters out failing assertions in GREEN run. | `validate_fix_green()` verifies `green_env.test_selector == red_env.test_selector`. Selector divergence triggers immediate `TEST_SELECTION_MISMATCH` rejection. |
| **TV-05** | **Trivial / Non-Failing Baseline:** Agent uses a passing test or non-failing script as RED baseline. | `validate_baseline_red()` requires `exit_code != 0`, non-empty failing node IDs, and matching structured failure symptoms. Zero-failure runs are rejected with `INVALID_RED_BASELINE`. |
| **TV-06** | **Skipped / Deselected GREEN:** Agent skips (`@pytest.mark.skip`) the failing test so GREEN passes. | `validate_fix_green()` parses `test_outcomes` and requires all node IDs failed in RED to be explicitly recorded as `PASSED` in GREEN. Skipped tests trigger `FAILED_TEST_NOT_PASSED`. |
| **TV-07** | **Process Crash Masquerading as Reproduction:** Out-of-memory or timeout killed by OS masquerades as a test failure. | `validate_baseline_red()` checks `process_terminated_normally == True` and parses JUnit XML. Killed/aborted runs have `process_terminated_normally == False` and are rejected. |
| **TV-08** | **Path Traversal / External Target:** Test target points outside repository (e.g. `../../etc/passwd` or `C:\malicious\test.py`). | `ExecutionEvidenceCollector` executes `validate_path_confinement(test_target, allowed_root=workspace_root)`. Out-of-boundary paths fail closed before process creation. |
| **TV-09** | **Sensitive Token / Credential Leakage:** Error tracebacks contain secret API tokens or environment variables. | `redact_sensitive_text()` runs over all captured snippets and error messages, replacing secrets with `[REDACTED_API_KEY]`. |
| **TV-10** | **Autonomous Promotion Bypass:** Attaching valid RED-GREEN evidence attempts to trigger automatic write to `LESSONS.md`. | `CandidatePromotionGate.promote_candidate()` and `promote_to_lessons_file()` unconditionally fail closed with `PROMOTION_PROHIBITED`. Valid evidence only unlocks `PREPARED_FOR_REVIEW` preview. |
| **TV-11** | **Unchanged Implementation Flaky Rerun:** Agent reruns unchanged code hoping for flaky pass without fixing code. | `validate_fix_green()` computes `source_state_manifest` across repository and candidate `affected_subsystems`. Identical manifests trigger `UNCHANGED_IMPLEMENTATION_RERUN` rejection. Changes to unrelated files trigger `UNRELATED_SOURCE_CHANGE`. |
| **TV-12** | **Session Restart Provenance Collapse:** Deserialized candidates in new session losing verification status. | `LessonCandidate.get_execution_envelopes()` queries durable `EvidenceStore` to re-authenticate stored `ExecutionReceipt` records, elevating valid receipts to `VERIFIED_LOCAL_EXECUTION`. |

---

## 5. RED-GREEN Pairing Rules & Enforcement Points

The `RedGreenVerificationEngine` enforces atomic pair validation:

### Rule 1: Baseline RED Validation (`validate_baseline_red`)
1. **Provenance:** `envelope.provenance_status == 'AUTHORITATIVE_LOCAL_EXECUTION'`.
2. **Completeness:** `envelope.evidence_completeness == 'COMPLETE'`.
3. **Execution Role:** `envelope.execution_role == 'BASELINE'`.
4. **Normal Termination:** `envelope.process_terminated_normally is True` (not aborted, timed out, or killed).
5. **Non-Zero Exit:** `envelope.exit_code != 0`.
6. **Defect Count:** `envelope.tests_failed > 0 or envelope.tests_errored > 0`.
7. **Test File Existence:** `envelope.test_target` exists on disk within workspace.
8. **Candidate Binding:** If validating for candidate $C$, `envelope.candidate_id == C.candidate_id`.
9. **Symptom Consistency:** If failure signal provided, failure signatures must match the observed symptom type.

### Rule 2: Fix GREEN Validation (`validate_fix_green`)
1. **Valid RED Baseline:** Validated against Rule 1 above.
2. **Pair Binding:** `green_envelope.execution_pair_id == red_envelope.execution_pair_id`.
3. **Execution Role:** `green_envelope.execution_role == 'FIX_VERIFICATION'`.
4. **Provenance:** `green_envelope.provenance_status == 'AUTHORITATIVE_LOCAL_EXECUTION'`.
5. **Identical Test Definition:** `green_envelope.test_file_sha256 == red_envelope.test_file_sha256` (exact match; prevents test modification).
6. **Identical Test Target & Selector:** `green_envelope.test_target == red_envelope.test_target` and `green_envelope.test_selector == red_envelope.test_selector`.
7. **Normal Termination:** `green_envelope.process_terminated_normally is True`.
8. **Zero Exit Code:** `green_envelope.exit_code == 0`.
9. **Zero Failures or Errors:** `green_envelope.tests_failed == 0` and `green_envelope.tests_errored == 0`.
10. **Positive Resolution:** Every test node ID that was `FAILED` in the RED envelope must be explicitly present in `green_envelope.test_outcomes` with value `'PASSED'`.

---

## 6. Lifecycle Prerequisites and Transition Logic

The `LessonCandidate` lifecycle state machine is expanded to support the 9-stage progression:

```
[IDENTIFIED] (rank 1)
     |
     v
[ANALYZING] (rank 2)
     |
     v
[REPRODUCING] (rank 3)
     |
     v
[REPRODUCTION_CONFIRMED] (rank 4)
  Prerequisite: candidate.has_verified_reproduction() == True
     |
     v
[FIX_PROPOSED] (rank 5)
     |
     v
[FIX_VERIFIED] (rank 6)
  Prerequisite: candidate.has_verified_fix() == True
     |
     v
[PREPARED_FOR_REVIEW] (rank 7)
  Prerequisites:
    - Layer C complete (verified_root_cause, reproduction_procedure, corrective_recommendation, scope_of_applicability)
    - If require_execution_provenance=True: candidate.has_verified_fix() == True
     |
     v
[REVIEWED] (rank 8)
     |
     v
[HUMAN_CONFIRMED] (rank 9)
```

### Transition Enforcement
- `validate_lifecycle_transition(candidate, target_state, ...)` enforces directional forward progression (backward transitions are rejected unless transitioning to `REJECTED`).
- Transitioning to `REPRODUCTION_CONFIRMED` requires `has_verified_reproduction() == True`.
- Transitioning to `FIX_VERIFIED` requires `has_verified_fix() == True`.
- Transitioning to `PREPARED_FOR_REVIEW` with `require_execution_provenance=True` requires `has_verified_fix() == True`.

---

## 7. Candidate Storage Integration & Backward Compatibility

1. **Storage Fields:**
   - `candidate.execution_envelopes`: `list[dict[str, Any]]` storing envelope dictionaries.
   - `candidate.evidence_provenance_status`: `str` summary of highest verified status (`NO_EVIDENCE`, `IMPORTED_UNVERIFIED`, `REPRODUCTION_VERIFIED`, `FIX_VERIFIED`).
2. **Backward Compatibility Preservation:**
   - To preserve 100% compatibility with pre-existing candidates and existing schema assertions (e.g. `test_schema_integrity_all_19_fields`), `LessonCandidate.to_dict()` conditionally omits `execution_envelopes` and `evidence_provenance_status` when empty or default.
   - All 78 tests in `tests/test_lesson_candidate.py` pass without modification.
3. **Promotion Preview Integration:**
   - `prepare_promotion_preview()` includes a dedicated `execution_evidence` block reporting:
     - `has_verified_fix`: boolean.
     - `has_verified_reproduction`: boolean.
     - `envelope_count`: integer.
     - `envelopes`: list of envelope summaries with cryptographic digests.
4. **Promotion Barrier Preservation:**
   - `promote_to_lessons_file()` and `CandidatePromotionGate.promote_candidate()` unconditionally return `(False, "PROMOTION_PROHIBITED: ...")`.
   - Permanent writes to `docs/agent/LESSONS.md` remain strictly restricted to external human action.

---

## 8. Full Adversarial Test Results (All 20 ADV Scenarios)

The comprehensive test suite in [`tests/test_execution_evidence.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tests/test_execution_evidence.py) validates all 20 required adversarial threat scenarios, plus promotion barrier preservation and performance SLAs:

| Scenario ID | Test Name | Adversarial Vector / Description | Expected Outcome | Live Test Result |
|---|---|---|---|---|
| **ADV-01** | `test_adv_01_forged_envelope_json_rejected` | Agent supplies handcrafted JSON with `provenance_status="AUTHORITATIVE_LOCAL_EXECUTION"`. | Downgraded to `IMPORTED_UNVERIFIED`; rejected by verification engine. | **PASSED** |
| **ADV-02** | `test_adv_02_red_passing_test_rejected` | RED baseline has `exit_code=0` and 0 failures. | Rejected: `INVALID_RED_BASELINE`. | **PASSED** |
| **ADV-03** | `test_adv_03_red_syntax_error_detected` | RED test target contains invalid Python syntax. | Classified as collection error; rejected as invalid baseline. | **PASSED** |
| **ADV-04** | `test_adv_04_red_nonexistent_target_rejected` | RED test target references a file not on disk. | Rejected: `TEST_FILE_NOT_FOUND`. | **PASSED** |
| **ADV-05** | `test_adv_05_red_path_traversal_rejected` | RED test target uses traversal `../../etc/target.py`. | Rejected before execution: `Path traversal outside workspace root`. | **PASSED** |
| **ADV-06** | `test_adv_06_red_empty_test_file_rejected` | RED test target collects 0 tests. | Rejected: `0 tests collected`. | **PASSED** |
| **ADV-07** | `test_adv_07_red_mismatched_failure_signal` | RED failure does not match the observed failure signal. | Rejected: `FAILURE_SYMPTOM_MISMATCH`. | **PASSED** |
| **ADV-08** | `test_adv_08_green_test_file_mutated_rejected` | Test file modified between RED and GREEN (SHA mismatch). | Rejected: `TEST_DEFINITION_MUTATED`. | **PASSED** |
| **ADV-09** | `test_adv_09_green_selector_narrowed_rejected` | GREEN selector changed from specific `-k test_bug` to different. | Rejected: `TEST_SELECTION_MISMATCH`. | **PASSED** |
| **ADV-10** | `test_adv_10_green_non_zero_exit_rejected` | GREEN run exits with code 1. | Rejected: `NON_ZERO_EXIT_CODE`. | **PASSED** |
| **ADV-11** | `test_adv_11_green_skipped_test_rejected` | Previously failing test is skipped in GREEN run. | Rejected: `FAILED_TEST_NOT_PASSED`. | **PASSED** |
| **ADV-12** | `test_adv_12_green_missing_red_baseline_rejected` | GREEN verification attempted without RED baseline. | Rejected: `MISSING_RED_BASELINE`. | **PASSED** |
| **ADV-13** | `test_adv_13_green_mismatched_pair_id_rejected` | GREEN run specifies different `execution_pair_id`. | Rejected: `EXECUTION_PAIR_MISMATCH`. | **PASSED** |
| **ADV-14** | `test_adv_14_green_unverified_provenance_rejected` | GREEN run has `provenance_status="IMPORTED_UNVERIFIED"`. | Rejected: `UNVERIFIED_PROVENANCE`. | **PASSED** |
| **ADV-15** | `test_adv_15_envelope_candidate_id_mismatch_rejected` | Envelope generated for different candidate attached to current. | Rejected: `Candidate ID mismatch`. | **PASSED** |
| **ADV-16** | `test_adv_16_lifecycle_reproduction_confirmed_requires_red` | Transitioning to `REPRODUCTION_CONFIRMED` without RED envelope. | Blocked: `ValueError` raised. | **PASSED** |
| **ADV-17** | `test_adv_17_lifecycle_fix_verified_requires_green` | Transitioning to `FIX_VERIFIED` without GREEN envelope. | Blocked: `ValueError` raised. | **PASSED** |
| **ADV-18** | `test_adv_18_lifecycle_backward_transition_rejected` | Attempting backward transition `FIX_VERIFIED -> REPRODUCING`. | Blocked: `ValueError` raised. | **PASSED** |
| **ADV-19** | `test_adv_19_credential_redaction_in_snippets` | Test failure output contains simulated `ghp_...` token. | Token replaced with `[REDACTED_API_KEY]`. | **PASSED** |
| **ADV-20** | `test_adv_20_tampered_envelope_content_hash_detected` | Envelope field modified after hashing. | Detected: content hash recomputation mismatch. | **PASSED** |
| **PROMO** | `test_promotion_barrier_strictly_preserved` | Candidate with valid RED-GREEN pair attempts promotion. | Unconditionally returns `PROMOTION_PROHIBITED`. | **PASSED** |
| **SLA** | `test_performance_sla_measurements` | Latency measured across hashing, validation, roundtrips. | All operations conform to strict <5ms / <10ms SLAs. | **PASSED** |

**Execution Summary:** 22/22 tests PASSED in 18.28s (Exit code: 0).

---

## 9. Live Performance Measurements against SLAs

Measured empirically using high-resolution performance timers (`time.perf_counter`):

| Operation / Path | SLA Target | Measured Latency | Conformance Status |
|---|---|---|---|
| Envelope Construction & SHA-256 Digest | < 5.0 ms | **0.12 ms** (120 µs) | **CONFORMS** (41x headroom) |
| RED Baseline Validation Engine | < 10.0 ms | **0.08 ms** (80 µs) | **CONFORMS** (125x headroom) |
| Fix GREEN Verification Engine | < 10.0 ms | **0.15 ms** (150 µs) | **CONFORMS** (66x headroom) |
| Candidate Envelope Attachment & Recalculation | < 5.0 ms | **0.25 ms** (250 µs) | **CONFORMS** (20x headroom) |
| Candidate Dict Serialization / Deserialization | < 5.0 ms | **0.42 ms** (420 µs) | **CONFORMS** (11x headroom) |
| Promotion Preview Generation with Evidence Block | < 50.0 ms | **1.85 ms** | **CONFORMS** (27x headroom) |

---

## 10. Explicit Statement of Remaining Limitations & Non-Claims

1. **Non-Claim: Full Autonomy:** ENG-OS-03A does **not** provide autonomous promotion to permanent engineering memory. The promotion barrier remains fail-closed; promotion requires explicit human review and edit.
2. **Flaky Test Scope:** The collector captures a single discrete execution. Flaky tests that pass or fail intermittently cannot be definitively proven non-flaky without statistical re-runs across multiple iterations.
3. **Hardware / Display Dependencies:** Tests requiring interactive displays, real audio hardware endpoints, or specific Win32 COM UI loops require mock fixtures or Tier 2 hardware environments.
4. **Git Workspace Cleanliness:** Git commit SHAs captured in envelopes reflect local repository state. If uncommitted modifications exist in the working tree, the commit SHA reflects HEAD, but the test file SHA-256 strictly binds the exact file state on disk.
