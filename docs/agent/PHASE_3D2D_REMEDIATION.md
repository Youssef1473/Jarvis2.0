# ENG-OS-02 Phase 3D2D: Candidate Evidence Integrity, Semantic Lifecycle Enforcement & Hook Contract Remediation Report

**Date:** 2026-11-05  
**Role:** Principal Python Reliability Engineer and Security Architect  
**Workspace:** `C:\Users\Youssef\Projects\Jarvis_2.0`  
**Active Git Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Verification Verdict:** **ALL REMEDIATIONS EMPIRICALLY VERIFIED — 214/214 TESTS PASSING (100%)**

---

## 1. Executive Summary & Scope Confinement

Phase 3D2D delivers the architectural and security remediation of the residual defects independently identified during the Phase 3D2C audit.

### Remediated Defect Matrix
| Audit Finding | Defect Description | Subsystem | Remediation Architecture | Verification Evidence |
| :--- | :--- | :--- | :--- | :--- |
| **AUD-D2C-01** | Evidence-blind deduplication short-circuiting on `deduplication_key` | `tools/lesson_candidate.py` | Decoupled event identity, candidate identity, evidence equivalence (`is_evidence_equivalent`), and fingerprint. Quarantined evidence conflicts under distinct `_evdiff_` IDs. | Scenario 24 (100% passing) |
| **AUD-D2C-02** | Rank-only progression allowing direct jumps and unverified lifecycle advances | `tools/lesson_candidate.py` | `validate_lifecycle_transition()` enforcing semantic prerequisites (Layer C completeness for `PREPARED_FOR_REVIEW`, findings for `REVIEWED`, hypothesis for `PROPOSED`, terminal state protection). | Scenarios 25 & 26 (100% passing) |
| **AUD-D2C-03** | Intra-process file collisions from shared `.tmp_{pid}` filenames under concurrency | `tools/continuity_manager.py` | `_PERSISTENCE_LOCK = threading.RLock()` serialization + unique thread- and UUID-based temporary filenames (`.tmp_{pid}_{tid}_{uuid}`) + Windows atomic retry. | Scenario 27 (100% passing, 30 concurrent threads) |
| **AUD-D2C-04** | Stop hook output emitting `{}` instead of documented Antigravity non-continuation schema | `tools/failure_collector.py` | Stop hook returns `{"decision": "stop"}` (exit 0) across all 8 execution scenarios, while `PostToolUse` maintains `{}`. | `TestStopHookContractCompliance` (100% passing) |
| **AUD-D2C-05** | Lack of dedicated `SessionStart` hook for initial continuity recovery | `tools/continuity_manager.py` & `.agents/hooks.json` | Registered `SessionStart` handler (`hook-session-start`) with session marker tracking to eliminate duplicate turn-0 subprocess execution. | Scenario 28 & `hooks.json` verification |

### Boundary Invariant Preservation
- **Frozen Retrieval Boundary Intact:** `tools/context_router.py`, `tests/test_context_router.py`, `tools/evaluate_lesson_retrieval.py`, `tests/eval/*`, and `docs/agent/LESSONS.md` remain 100% untouched.
- **Fail-Closed Promotion:** `CandidatePromotionGate.promote_candidate()` unconditionally returns `PROMOTION_PROHIBITED`.
- **Zero Broad Staging:** Only explicitly designated files are modified.

---

## 2. Workstream A: Evidence-Aware Deduplication

### Root Cause Analysis (AUD-D2C-01)
In Phase 3D2C, `check_candidate_deduplication()` evaluated deduplication using:
```python
obs_match = (
    existing.observed_behavior.get("tool_name") == candidate.observed_behavior.get("tool_name")
    and existing.observed_behavior.get("exit_code") == candidate.observed_behavior.get("exit_code")
    and existing.source_event_ids == candidate.source_event_ids
)
if obs_match or existing.deduplication_key == key:
    return True, "EXACT_RELIABLE_DUPLICATE", existing
```
This exhibited two critical flaws:
1. `obs_match` ignored `failure_signal` and `evidence_references`.
2. `or existing.deduplication_key == key` short-circuited `obs_match`, causing candidates with conflicting event IDs or differing failure signals to be classified as exact duplicates.

### Architectural Resolution
Four distinct concepts were decoupled:
1. **Event Identity:** `source_event_ids` tracking the specific execution events that produced the observation.
2. **Candidate Identity:** `candidate_id` tracking the unique candidate unit.
3. **Evidence Equivalence:** `is_evidence_equivalent(cand_a, cand_b) -> bool`:
   - Matches `tool_name`
   - Matches `exit_code`
   - Matches normalized `failure_signal`
   - Matches canonical JSON-serialized `evidence_references`
   - Matches normalized `collection_status`
4. **Deduplication Fingerprint:** `deduplication_key` (`REL:...` or `INCOMPLETE_DISCRIMINATED:...`).

### Implemented Logic
```python
for existing in existing_candidates:
    if existing.candidate_id == candidate.candidate_id:
        if sorted(existing.source_event_ids) != sorted(candidate.source_event_ids):
            return False, "CANDIDATE_ID_COLLISION_CONFLICT", existing
        if not is_evidence_equivalent(existing, candidate):
            return False, "CANDIDATE_EVIDENCE_CONFLICT", existing
        return True, "EXACT_RELIABLE_DUPLICATE", existing

    if existing.deduplication_key == key:
        if is_reliable:
            if is_evidence_equivalent(existing, candidate):
                return True, "EXACT_RELIABLE_DUPLICATE", existing
            return False, "CANDIDATE_EVIDENCE_CONFLICT", existing
        return False, "POTENTIAL_DUPLICATE_INCOMPLETE_IDENTITY", existing

return False, "UNIQUE", None
```

In `FailureCandidatePipeline.ingest_event_envelope()`, if `dup_status == "CANDIDATE_EVIDENCE_CONFLICT"`, the incoming candidate is quarantined under a distinct ID:
```python
candidate.candidate_id = f"{candidate.candidate_id}_evdiff_{uuid.uuid4().hex[:6]}"
```
This guarantees that conflicting evidence is preserved, visible for inspection, and never silently discarded or overwritten.

---

## 3. Workstream B: Semantic Lifecycle Enforcement

### Root Cause Analysis (AUD-D2C-02)
Previously, candidate lifecycle progression only validated numerical rank:
`LIFECYCLE_RANK.get(to_state, 0) >= LIFECYCLE_RANK.get(from_state, 0)`
This allowed invalid direct jumps (e.g. `OBSERVED` directly to `PREPARED_FOR_REVIEW` without verified root cause, reproduction, or tests).

### Architectural Resolution
Implemented `validate_lifecycle_transition(candidate, new_state, enforce_monotonic=True) -> tuple[bool, str]` enforcing:
1. **Direct Skip Rejection:** `OBSERVED` and `NEEDS_EVIDENCE` cannot transition directly to `PREPARED_FOR_REVIEW`.
2. **Layer C Completeness for `PREPARED_FOR_REVIEW`:** Requires non-empty `verified_root_cause`, `reproduction_procedure`, `corrective_recommendation`, and `regression_test`.
3. **Finding Requirement for `REVIEWED`:** Requires `len(candidate.review_findings) > 0`.
4. **Hypothesis Requirement for `PROPOSED`:** Requires non-empty `root_cause_hypothesis.hypothesis`.
5. **Terminal State Egress Protection:** Terminal states `REJECTED` and `SUPERSEDED` cannot transition to any active state.
6. **Monotonic Regression Rejection:** Transitions to lower ranks are rejected.
7. **Full Matrix Verification:** Verified all 64 pairs in the lifecycle transition matrix.

---

## 4. Workstream C: Thread-Safe & Process-Safe Candidate Persistence

### Root Cause Analysis (AUD-D2C-03)
`persist_candidate()` generated temporary files using:
`tmp_target = self.candidates_dir / f"{target.stem}.tmp_{os.getpid()}"`
When multiple threads in the same process attempted to persist candidates concurrently, they shared the exact same temporary file path. On Windows, this caused file locking collisions (`[WinError 32] The process cannot access the file because it is being used by another process`).

### Architectural Resolution
1. **Intra-Process Serialization:** Added module-level `_PERSISTENCE_LOCK = threading.RLock()` guarding file write operations within the process.
2. **Unique Filename Generation:**
   ```python
   unique_suffix = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
   tmp_target = self.candidates_dir / f"{target.stem}.tmp_{unique_suffix}"
   ```
3. **Windows Atomic Replacement Retry:** Added a 3-attempt backoff retry loop for `tmp_target.replace(target)` to absorb transient OS file locks.
4. **Guaranteed Temp Cleanup:** Added cleanup in a `finally` block and automatic pruning of stale `.tmp_*` files during `list_candidates()`.
5. **High-Concurrency Empirical Verification:** Verified with 20 parallel threads writing distinct candidates and 10 parallel threads writing to the same candidate ID simultaneously with 0 errors.

---

## 5. Workstream D: Antigravity Stop Hook Contract Compliance

### Root Cause Analysis (AUD-D2C-04)
Previously, `failure_collector.py` emitted `{}` on stdout for all hook events. According to the documented Antigravity lifecycle hook specification:
- `PostToolUse` expects `{}`.
- `Stop` expects `{"decision": "continue", "reason": "..."}` to continue or any non-continue value (canonically `{"decision": "stop"}`) to terminate execution normally. Emitting `{}` was non-compliant with the Stop contract schema.

### Architectural Resolution
1. In `process_hook_payload()`:
   - Identifies event type via `"terminationReason" in payload or "fullyIdle" in payload`.
   - Returns `{"decision": "stop"}` for Stop events.
   - Returns `{}` for `PostToolUse`.
2. In `main()`:
   - Serializes the hook response dictionary to stdout (`{"decision": "stop"}\n` for Stop, `{}\n` for PostToolUse).
   - Preserves exit code 0.
3. Verified all 8 required Stop scenarios in `TestStopHookContractCompliance`:
   - `NO_TOOL_CALL` (normal completion)
   - `ERROR` (failure termination)
   - `USER_CANCELED` (user abort)
   - `TIMEOUT` (timeout termination)
   - `MAX_INVOCATIONS` (budget termination)
   - Empty termination (`""`)
   - Unknown termination (`CUSTOM_STOP_SIGNAL`)
   - Missing terminationReason with `fullyIdle=True`

---

## 6. Workstream E: SessionStart Hook Lifecycle & Turn-0 Deduplication

### Architectural Integration
1. Registered `SessionStart` in `.agents/hooks.json`:
   ```json
   "continuity-checker": {
     "enabled": true,
     "SessionStart": [
       {
         "type": "command",
         "command": "if exist tools\\continuity_manager.py (python tools\\continuity_manager.py hook-session-start) else (python ..\\tools\\continuity_manager.py hook-session-start)",
         "timeout": 5
       }
     ],
     "PreInvocation": [
       {
         "type": "command",
         "command": "if exist tools\\continuity_manager.py (python tools\\continuity_manager.py hook-check) else (python ..\\tools\\continuity_manager.py hook-check)",
         "timeout": 5
       }
     ]
   }
   ```
2. Implemented `run_session_start_hook()` in `tools/continuity_manager.py`:
   - Runs once at session startup.
   - Verifies `PROJECT_STATE.md` baseline against live Git ground truth.
   - Injects ephemeral drift warning if diverged or stale; emits empty `injectSteps` if aligned.
   - Records an ephemeral session continuity marker (`jarvis_session_{conv_digest}_continuity.marker`).
3. In `run_continuity_hook()`:
   - At `invocationNum == 0`, checks if the session marker is present and recent (<300s).
   - If present, emits `{"injectSteps": []}` immediately, eliminating redundant Git state inspection and subprocess overhead.
   - If marker is absent (host does not invoke `SessionStart`), seamlessly executes the fallback check.

---

## 7. Comprehensive Verification Evidence

### Diagnostic & Static Code Analysis
- `python tools/lint.py check tools/lesson_candidate.py tools/continuity_manager.py tools/failure_collector.py`: **PASSED (exit 0, 0 errors)**
- `python tools/diagnostics.py check ...`: **PASSED (Ruff & AST-Grep passed)**

### Subsystem Unit & Integration Test Suites
```powershell
python tools/run_fast_tests.py tests/test_continuity_and_lifecycle.py  # 28 passed in 0.65s
python tools/run_fast_tests.py tests/test_lesson_candidate.py          # 78 passed in 0.78s
python tools/run_fast_tests.py tests/test_failure_collector.py         # 41 passed in 0.21s
python tools/run_fast_tests.py tests/test_hook_integration.py          #  7 passed in 2.55s
python tools/run_fast_tests.py tests/test_context_router.py           # 60 passed in 0.31s
```
**Total Passing Tests:** **214 passed across 5 test suites (100% success rate, 0 failures, 0 errors).**

### Frozen Benchmark Evaluation (Zero Regression)
`python tools/evaluate_lesson_retrieval.py`:
- **Holdout Benchmark (28 Scenarios):**
  - Item-Level Retrieval Precision: **1.0000 (21/21)**
  - Capacity-Bounded Recall: **1.0000 (17/17)**
  - Normalized Discounted Cumulative Gain (nDCG@3): **1.0000**
  - Correct Abstention Rate (k=0): **1.0000 (11/11)**
  - Irrelevant Injection Rate: **0.0000 (0/28)**
- **Baseline Benchmark (10 Scenarios):**
  - Precision: **1.0000 (3/3)**
  - Recall: **1.0000 (3/3)**
  - Exact-Set Accuracy: **1.0000 (10/10)**

---

## 8. Conclusion & Milestone Status

Phase 3D2D resolves all residual concerns from Phase 3D2C with complete mathematical and empirical proof:
1. Deduplication respects evidence equivalence and isolates conflicting observations without data loss.
2. Lifecycle management strictly enforces semantic prerequisites and prevents unverified promotions.
3. Candidate persistence is fully serialized and immune to intra-process concurrency collisions on Windows.
4. Hook contracts match Antigravity specifications for both `PostToolUse` and `Stop`.
5. Session continuity is integrated cleanly via `SessionStart` without redundant execution.
6. The frozen retrieval boundary remains 100% verified and unaffected.
