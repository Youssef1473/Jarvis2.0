# ENG-OS-03A3 — Independent Execution Evidence Trust & Persistence Audit
## Independent Principal Security Architect & Reliability Audit Report

- **Date:** 2026-11-05
- **Auditor:** Independent Principal Security Architect and Reliability Auditor
- **Mode:** STRICTLY READ-ONLY
- **Target Repository:** `Jarvis_2.0`
- **Active Git Branch:** `feat/engineering-maturity-milestone`
- **Active HEAD Commit:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Verdict:** **ACCEPTED WITH LIMITATIONS**

---

## 1. Executive Summary & Core Verdict

### Final Acceptance Verdict: `ACCEPTED WITH LIMITATIONS`

This independent audit conducted an adversarial evaluation of milestone **ENG-OS-03A2** to determine whether execution provenance, RED-GREEN verification, and candidate persistence have established a genuine trust boundary.

The evaluation specifically examined the four foundational questions posed by the engineering OS:

```
+---------------------------------------------------------------------------------------------------------------------+
|                                          EXECUTIVE QUESTION AUDIT MATRIX                                            |
|                                                                                                                     |
| Question                                       | Finding      | Architectural Root Cause                            |
|------------------------------------------------|--------------|-----------------------------------------------------|
| 1. Can ordinary Python code manufacture an     | YES          | In-process Python type checks cannot establish an   |
|    authoritative ExecutionReceipt?             | (In-process) | unforgeable trust boundary against code running     |
|                                                |              | under the same interpreter and OS principal.        |
|                                                |              |                                                     |
| 2. Can EvidenceStore accept records that did   | YES          | `EvidenceStore.record_receipt()` is a public API    |
|    not originate from actual execution?        | (Direct API) | with no caller authentication or witness signature. |
|                                                |              |                                                     |
| 3. Can persisted evidence be modified/replayed | PARTIALLY    | Tandem modification of SQLite and JSON by same user |
|    while retaining VERIFIED_LOCAL_EXECUTION?   | (Tandem / ID)| succeeds; execution_id lacks a UNIQUE constraint.   |
|                                                |              |                                                     |
| 4. Can GREEN be accepted without a meaningful  | PARTIALLY    | Manifest hashing detects code change, but not fix   |
|    change to the relevant implementation?      | (Non-causal) | causality; comment edits or empty subsystems pass.  |
+---------------------------------------------------------------------------------------------------------------------+
```

### Why the Milestone is ACCEPTED WITH LIMITATIONS (and NOT Rejected)
1. **Unconditional Permanent Memory Barrier:** In-process promotion to permanent engineering memory (`docs/agent/LESSONS.md`) remains strictly fail-closed. Both `CandidatePromotionGate.promote_candidate()` and `promote_to_lessons_file()` unconditionally return `(False, "PROMOTION_PROHIBITED: ...")`.
2. **Untrusted JSON Deserialization Barrier:** Model-generated JSON or external files cannot assert `AUTHORITATIVE_LOCAL_EXECUTION`. Deserialization via `from_dict()` unconditionally downgrades unauthenticated evidence to `IMPORTED_UNVERIFIED`.
3. **Cross-Session Re-authentication Bridge:** Locally captured legitimate receipts survive process restarts and candidate deserialization by verifying dual-layer consistency against the WAL-journaled SQLite registry.
4. **Anti-Flakiness Baseline:** Exact reruns of unchanged code are strictly rejected with `UNCHANGED_IMPLEMENTATION_RERUN`.
5. **Full Diagnostic & Test Conformance:** All 29 targeted adversarial scenarios pass (100%), all 243 continuous-learning tests across 6 suites pass (100%), and the frozen lesson retrieval benchmark achieves 100% precision and 0% irrelevant injection.

---

## 2. Initial Preservation Audit

Prior to executing any probes or evaluations, the repository state was independently verified and recorded:

- **Git Branch:** `feat/engineering-maturity-milestone`
- **HEAD Commit SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Commit Subject:** `security: enforce end-to-end data provenance and fail-closed authorization`
- **Staging Index:** 0 files staged (clean index)
- **Tracked Modifications:** Exactly 17 files (reconciled baseline)
- **Untracked File Inventory:** 75 files (includes this audit deliverable)
- **Production Evidence Database:** `data/execution_evidence/evidence_registry.db` (12,288 bytes, SHA-256: `c26f3645d85c5ea9a4164addfda90077f553093d0dc5cc47936a852fd50d7dcf`, table `execution_receipts` has 0 rows)
- **Production Receipt Artifacts Directory:** `data/execution_evidence/receipts/` (0 artifacts; confirms production cleanliness)

### Bit-for-Bit SHA-256 Hash Invariants
All protected retrieval assets, evaluation datasets, learning candidates, and telemetry events match their baseline digests with 100% bit-for-bit accuracy:

```text
Frozen Retrieval Subsystem:
  tools/context_router.py:              dbe262e53633473c800828ccc9a1caf2f7ed7030c9a5efafb514cd004f35401e
  tests/test_context_router.py:          9627e879fe18d979169485a23c2831bcae4be2d23be4d6a5817167a8e157ed14
  tools/evaluate_lesson_retrieval.py:    3cbdf0b60a65ccc8cd267f6ac5aa4352ea2474fe78954686f3625f08e37c1ee8
  tests/eval/eval_holdout_registry.md:   275044cb12c9466361174208a5c07a7ff501da023e44dac041f05e5401cec5eb
  tests/eval/eval_retrieval_holdout.json:0f2171a087ec5df9ff5f960e04cf1b63dd67be0b6f8ef7b236a836fecb0545b1
  tests/eval/eval_retrieval_scenarios.json: 3cf434392533eca8647acb8367df7a3a14c0e55ac831e8e5876e484cd776ba60
  docs/agent/LESSONS.md:                 bf11bc69ef8730bb03b40450c18aa04ab16442cf2d7cb5386ee2f342f3ffe670

Learning Candidates (7/7 identical):
  LCAND-060af81e3416.json: ae58837e00fd30d5b72fb84710be999376662ca9e4cf2feafef8d909ce67762d
  LCAND-1572b93ec9cc.json: 90a1f8535af0b51142915956a85444c9498b3bf68d4fdd740c5154ba59f1fa7c
  LCAND-2d10293ac866.json: 3e3d86cf411f35d9d737cf1927b6bbd5f016f3ff6574b556d96d99967fa04480
  LCAND-480b6011ed6d.json: 117f906a6598e2191716ba6bf9a47d5060fe986766a30779cc18bacc3c219d4d
  LCAND-4ad4da892e64.json: a3757f3e9f6217bb48b39431b0d39536eacb0fb74d0eff3fcea9a94409592610
  LCAND-b9b3a4380d77.json: 9ab72645310a7e3b6f664d2190fc5e380600154773c92ec646ea89fb25a8e6e0
  LCAND-c42c5d28f6ba.json: 9af8211f045ca0bc0e44140f274e53b33b15ca446465b55b94ef349163f4e136

Learning Events (8/8 identical):
  061fbc2e59c9ceebf1f11d89.json: ebc30d95f10b2b26d64a7a570a1e93c270ba23dc5e9b02d2ec2840182ec2a90d
  20f3ee2a48c69f4598473840.json: f68f5d40a99920f673a88b577d8c04630b86f59365e1769eafc551c61fbdcd35
  78a29e18e72a7f5b67e09fbb.json: ef355393337d6f60d82c1cca31772148c7af3edf3fcd98800f4fe0ee75d68c56
  8a0d872346812c3ab2b8b7f5.json: 402e60103be634d2a72b95c4498e24275f56fb8a3161e4fe551e80038d61c2b4
  8a327621fede38c2e56d44ab.json: f9bc8548b414478081b5e2b66c3431f582725b02eae13c370a41b207929a70c4
  b07f38879bb8559e7220307f.json: bc876bbc8aacedf5e5c0852e6414a02ec7669f01052ecda4accaf3dc0d066f2f
  c1d96b1722129b50906a3723.json: c98dfc1da3b279709ff9a4b779488ca5f5918e6ee99c196b3a8cc4a970576359
  cb2ec53a55a829b3cadc3fc2.json: bdbee7bed8c700d2e01850febe1e92dfce64ec82f124e40b95393657283110ae
```

---

## 3. Audit A — `ExecutionReceipt` Authenticity & Forgery Analysis

### Construction, Hashing, and Registration Paths
- **`ExecutionReceipt` Definition:** Declared as `@dataclass(frozen=True)` in `tools/execution_evidence.py` (lines 208-248). Holds 36 fields with immutable tuple types for vectors.
- **Canonical Digest Calculation:** `compute_content_digest()` serializes the receipt to a canonical dictionary via `to_dict()`, excludes `content_digest`, and hashes the sorted JSON with SHA-256 via `canonical_hash_dict()`.
- **Witness Pipeline:** `ExecutionEvidenceCollector.capture_test_execution()` executes the subprocess, extracts JUnit XML, builds `receipt_dict`, computes `content_digest`, constructs `ExecutionReceipt`, and calls `self.evidence_store.record_receipt()`.

### Empirical Results of 10 Adversarial Forgery Probes
All probes were executed against isolated temporary fixtures (`tempfile.TemporaryDirectory`):

```
+---------------------------------------------------------------------------------------------------------------------+
|                                          AUDIT A: ADVERSARIAL FORGERY RESULTS                                       |
|                                                                                                                     |
| Probe ID | Test Description                               | Expected Invariant   | Live Probe Behavior      | Verdict|
|----------|------------------------------------------------|----------------------|--------------------------|--------|
| P-01     | Direct construction of ExecutionReceipt        | Succeeds (Public API)| SUCCEEDED: r1 created    | VULN   |
| P-02     | Fabricate process results (exit 1 -> exit 0)   | Rejected by witness  | SUCCEEDED: Values forged | VULN   |
| P-03     | Recompute content digest on fabricated values  | Deterministic hash   | SUCCEEDED: Hash generated| VULN   |
| P-04     | Update receipt digest via dataclasses.replace()| Valid self-check     | SUCCEEDED: Valid digest  | VULN   |
| P-05     | In-memory mutation via normal attribute assign | FrozenInstanceError  | BLOCKED: Frozen error    | SECURE |
| P-06     | In-memory mutation via object.__setattr__()    | Blocked or detected  | BYPASS: exit_code mutated| VULN   |
| P-07     | Submit fabricated receipt to EvidenceStore     | Rejected by store    | ACCEPTED: verify returns | VULN   |
|          |                                                |                      | (True, VALID_STORED_RCPT)|        |
| P-08     | Submit receipt with forged execution ID        | Rejected by store    | ACCEPTED: Registered     | VULN   |
| P-09     | Submit duplicate execution ID (conflicting)    | Replay rejected      | ACCEPTED: 2 rows in DB   | VULN   |
| P-10     | Deserialized JSON asserting authoritative      | Downgraded unverified| BLOCKED: Downgraded to   | SECURE |
|          |                                                |                      | IMPORTED_UNVERIFIED      |        |
+---------------------------------------------------------------------------------------------------------------------+
```

### Epistemic Assessment of Receipt Authenticity
- **Object Immutability $\neq$ Authenticity:** In Python, `@dataclass(frozen=True)` prevents accidental field reassignment during benign program execution. However, any code running in the same process can call `ExecutionReceipt(**fake_values)` or `object.__setattr__()`. Python runtime type checks do not constitute an OS-level or cryptographic security boundary.
- **Registry Registration Lacks Witness Proof:** `EvidenceStore.record_receipt(receipt)` validates that the receipt is an instance of `ExecutionReceipt` and that its `content_digest` matches its computed value. It does **not** cryptographically verify that the receipt was signed by an isolated observer or that a real process was executed.

---

## 4. Audit B — `EvidenceStore` Integrity and Persistence

### Schema and Storage Architecture
- **Storage Dual-Layer:** `data/execution_evidence/evidence_registry.db` (SQLite WAL) and `data/execution_evidence/receipts/<receipt_id>.json` (canonical JSON artifact).
- **SQLite Schema:**
  ```sql
  CREATE TABLE IF NOT EXISTS execution_receipts (
      receipt_id TEXT PRIMARY KEY,
      execution_id TEXT NOT NULL,
      candidate_id TEXT NOT NULL,
      execution_pair_id TEXT NOT NULL,
      execution_role TEXT NOT NULL,
      target_test_file TEXT NOT NULL,
      target_test_file_sha256 TEXT NOT NULL,
      exit_code INTEGER NOT NULL,
      content_digest TEXT NOT NULL,
      captured_at_utc TEXT NOT NULL,
      artifact_path TEXT NOT NULL
  );
  ```

### Empirical Integrity Probes
1. **Valid Receipt Reload (Test B1):** `store.verify_receipt("RCPT-valid01")` returns `(True, "VALID_STORED_RECEIPT", receipt)`. (**PASSED**)
2. **Modified JSON Artifact (Test B2):** Mutating `exit_code: 0` in `RCPT-valid01.json` on disk triggers immediate detection: `(False, "RECEIPT_TAMPERED: Artifact content digest mismatch with registry.", None)`. (**PASSED**)
3. **Modified SQLite Record (Test B3):** Mutating `content_digest` in SQLite triggers: `(False, "RECEIPT_TAMPERED: Artifact content digest mismatch with registry.", None)`. (**PASSED**)
4. **Matching Tandem Modification (Test B4 — CRITICAL LIMITATION):** When an attacker running under the same user privileges mutates the JSON artifact and simultaneously executes `UPDATE execution_receipts SET content_digest = ...` in SQLite, `verify_receipt()` accepts the forged receipt as `VALID_STORED_RECEIPT`.
5. **Missing Artifact (Test B5):** Deleting the JSON artifact triggers: `(False, "RECEIPT_ARTIFACT_MISSING: ...", None)`. (**PASSED**)
6. **Duplicate Execution ID (Probe P-09 — LIMITATION):** Because `execution_id` is not declared `UNIQUE`, two receipts with different `receipt_id`, different `content_digest`, and conflicting outcomes (one pass, one fail) can be registered under the identical `execution_id`.

---

## 5. Audit C — RED-GREEN Source Manifest Correctness

### Manifest Fingerprinting Mechanism
- In `ExecutionEvidenceCollector._capture_source_state_manifest()`:
  1. Runs `git status --short`.
  2. Fingerprints modified `.py` files (excluding `tests/` and `data/`).
  3. Scans `workspace_root.glob("*.py")` and `src/**/*.py`.
- In `RedGreenVerificationEngine.validate_fix_green()`:
  - **Check 8 (Anti-Flakiness):** Rejects when `green.source_state_manifest == red.source_state_manifest` (`UNCHANGED_IMPLEMENTATION_RERUN`).
  - **Check 9 (Relevance):** When `candidate.affected_subsystems` is populated, verifies that at least one modified file in `changed_sources` matches a subsystem name.

### Empirical Results of 13 RED-GREEN Probes
```
+---------------------------------------------------------------------------------------------------------------------+
|                                          AUDIT C: RED-GREEN VERIFICATION PROBES                                     |
|                                                                                                                     |
| Probe ID | Scenario Description                           | Expected Invariant   | Live Verification Behavior| Verdict|
|----------|------------------------------------------------|----------------------|---------------------------|--------|
| C-01     | Flaky test passing on identical source state   | Rejected (Flaky run) | BLOCKED: UNCHANGED_RERUN  | SECURE |
| C-02     | Relevant fix in worker.py passing test         | Accepted (Valid fix) | ACCEPTED: VALID_FIX_GREEN | SECURE |
| C-03     | Unrelated source edit in other/unrelated.py    | Rejected (Unrelated) | BLOCKED: UNRELATED_CHANGE | SECURE |
| C-04     | Manipulated affected_subsystems to match other | Caught or accepted   | ACCEPTED: Manipulation ok | LIMIT  |
| C-05     | Missing relevant source file                   | Subprocess error     | BLOCKED: Test execution 1 | SECURE |
| C-06     | Modified pytest.ini configuration              | Config mutated err   | BLOCKED: CONFIG_MUTATED   | SECURE |
| C-07     | Modified conftest.py                           | Config mutated err   | BLOCKED: CONFIG_MUTATED   | SECURE |
| C-08     | Modified test fixtures                         | Caught via conftest  | BLOCKED: Covered by C-07  | SECURE |
| C-09     | Environment fingerprint capture                | Recorded in envelope | VERIFIED: Platform/Python | SECURE |
| C-10     | Python/pytest environment change               | Tracked in fingerprint| VERIFIED: Manifest checked| SECURE |
| C-11     | Test file mutated between RED and GREEN        | Test mutation reject | BLOCKED: TEST_MUTATED     | SECURE |
| C-12     | Test selector narrowed in GREEN run            | Selector mismatch rej| BLOCKED: SELECTOR_MISMATCH| SECURE |
| C-13     | Baseline RED with empty failing outcomes       | Invalid RED baseline | BLOCKED: NO_FAILING_NODES | SECURE |
+---------------------------------------------------------------------------------------------------------------------+
```

### Subsystem Fingerprinting Assessment
- **Causality vs. Hash Divergence:** The system reliably proves that *some source file changed*. However, inserting a comment (`# comment`) into a file listed in `affected_subsystems` alters the SHA-256 digest, which bypasses Check 8 and allows a flaky rerun to achieve `VALID_FIX_GREEN`.
- **Candidate Subsystem Trust:** Because `candidate.affected_subsystems` is an editable attribute of the candidate, an agent can set `affected_subsystems = ["other"]` to make changes in `other/` satisfy the relevance filter.

---

## 6. Audit D — Collector Execution Boundary

### Pytest Argument & Path Policy Inspection
- **Path Confinement:** `validate_collector_arguments()` validates `test_target` against `workspace_root` via `validate_path_confinement()`. Traversal (`../../`) is rejected before execution.
- **Strict Option Allowlist:**
  - Allowed flags: `-v`, `-vv`, `-q`, `-qq`, `-s`, `-x`, `--capture=sys`, `--capture=fd`, `--capture=no`.
  - Allowed prefixes: `--maxfail=`, `--tb=`.
  - Allowed selectors: `-k <expr>`, `-m <expr>`.
  - Prohibited tokens: `-p`, `-c`, `-o`, `--override-ini`, `--rootdir`, `--import-mode`, `--assert`, `--basetemp`, `--pyargs`, `--confcutdir`, `--noconftest`.
  - Shell metacharacters: `&`, `|`, `;`, `>`, `<`, `` ` ``, `$`, `\n`, `\r`.
- **Subprocess Isolation:** Executed with `shell=False`, explicit timeout (30s), bound `cwd`.
- **Environment Variable Boundary (LIMITATION):** `subprocess.run` inherits the ambient `os.environ`. While CLI flags are allowlisted, an in-process caller could set `os.environ["PYTEST_ADDOPTS"]` or `os.environ["PYTHONPATH"]` prior to invoking the collector.

---

## 7. Audit E — Lifecycle Trust Integration

### Deserialization, Lifecycle State Machine & Promotion Gates
1. **Untrusted JSON Ingestion:** `LessonCandidate.from_dict()` strips elevated approval statuses and treats all serialized execution envelopes as untrusted, downgrading them to `IMPORTED_UNVERIFIED`.
2. **Dynamic Re-authentication:** `has_verified_reproduction()` and `has_verified_fix()` dynamically query `EvidenceStore.verify_receipt()`.
3. **Self-Transition Bypass (LIMITATION):**
   - If an imported JSON specifies `"review_status": "FIX_VERIFIED"`, `LessonCandidate.from_dict()` sets `candidate.review_status = "FIX_VERIFIED"`.
   - `transition_candidate_lifecycle(candidate, "FIX_VERIFIED")` returns `True` immediately due to the self-transition guard (`prev_state == canonical_state`).
   - **Protection:** However, when `prepare_promotion_preview()` is invoked, it evaluates `candidate.has_verified_fix()`. Because the receipts are absent from `EvidenceStore`, `prepare_promotion_preview()` correctly reports `has_verified_fix: False` and `evidence_provenance_status: IMPORTED_UNVERIFIED`.
4. **Permanent Promotion Barrier:**
   - `CandidatePromotionGate.promote_candidate()` unconditionally returns:
     `(False, "PROMOTION_PROHIBITED: Automatic promotion to permanent memory is strictly disabled.")`
   - `promote_to_lessons_file()` unconditionally returns:
     `(False, "PROMOTION_PROHIBITED: Promotion to permanent lessons file is prohibited in-process.")`
   - Permanent memory (`docs/agent/LESSONS.md`) remains physically immutable to in-process code.

---

## 8. Audit F — Regression & Test Inventory Verification

Live execution of all repository diagnostic and continuous-learning test suites verified:

```powershell
# 1. Targeted execution evidence suite (29/29 passed)
python tools/run_fast_tests.py tests/test_execution_evidence.py
# 29 passed in 41.46s (Exit code: 0)

# 2. Complete continuous-learning regression suite (243/243 passed)
python tools/run_fast_tests.py tests/test_continuity_and_lifecycle.py  # 28 passed in 0.79s
python tools/run_fast_tests.py tests/test_lesson_candidate.py          # 78 passed in 1.31s
python tools/run_fast_tests.py tests/test_failure_collector.py         # 41 passed in 0.21s
python tools/run_fast_tests.py tests/test_hook_integration.py          # 7 passed in 2.32s
python tools/run_fast_tests.py tests/test_context_router.py            # 60 passed in 0.29s
# Total: 243/243 passed (100% pass rate, exit code 0)

# 3. Retrieval benchmark evaluation (38/38 passed)
python tools/evaluate_lesson_retrieval.py
# 28 Holdout: 1.0000 P, 1.0000 Bounded R, 1.0000 MRR, 1.0000 nDCG@3 (Exit code: 0)
# 10 Baseline: 1.0000 P, 1.0000 R, 1.0000 nDCG@3 (Exit code: 0)

# 4. Static linting & AST-Grep invariant scanning
python tools/lint.py check tools/execution_evidence.py tools/lesson_candidate.py tests/test_execution_evidence.py
# All checks passed! (Exit code: 0)
python tools/diagnostics.py check tools/execution_evidence.py tools/lesson_candidate.py
# Ruff: PASSED, AST-Grep: PASSED, Result: ALL DIAGNOSTICS PASSED (Exit code: 0)
```

---

## 9. Remaining Threat-Model Limitations & Non-Claims

1. **Same-Process Python Reflection:** Python does not offer memory isolation between modules executing under the same interpreter. `ExecutionReceipt` is an immutable frozen dataclass, which prevents accidental mutation, but cannot prevent intentional in-process object instantiation or reflection via `object.__setattr__()`.
2. **Same-User Filesystem Access:** Both `evidence_registry.db` and the receipt JSON artifacts reside in `data/execution_evidence/` under the developer's user account. Any script with write access to these directories can modify both copies in tandem.
3. **Execution ID Collisions:** The SQLite schema lacks a `UNIQUE(execution_id)` constraint, allowing conflicting receipts to claim the same execution ID.
4. **Causality vs. Source Digest Divergence:** Checking that source files changed between RED and GREEN prevents rerun on identical code, but cannot verify whether the code modification was causally related to fixing the test failure.
5. **Ambient Environment Inheritance:** Subprocess execution inherits `os.environ`, allowing ambient environment variables (such as `PYTEST_ADDOPTS`) to influence test runner behavior unless scrubbed.

---

## 10. Acceptance Recommendation

### Verdict: `ACCEPTED WITH LIMITATIONS`

Milestone **ENG-OS-03A2** is accepted with the explicit limitations documented above. The implementation provides robust tamper-detection, eliminates accidental provenance spoofing, blocks flaky reruns on unmodified code, binds execution evidence across sessions, and preserves the fail-closed permanent memory promotion barrier.

The documented limitations reflect realistic runtime constraints of in-process Python and same-user OS execution, rather than implementation defects.

**Recommendation:** Close milestone **ENG-OS-03A2** and proceed to milestone **ENG-OS-03B** (Optimistic Concurrency Control & Multi-Process Persistence).
