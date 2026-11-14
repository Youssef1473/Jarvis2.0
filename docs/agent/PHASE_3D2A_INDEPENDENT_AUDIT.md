# ENG-OS-02 — PHASE 3D2A: Independent Adversarial Acceptance Audit
## Approval Authenticity, Host Integration, Continuous Learning & Project Continuity

**Auditor:** Independent Principal Security Auditor and Reliability Architect  
**Model:** Gemini 3.8 Flash (High Reasoning) — Strictly Read-Only Audit  
**Workspace:** `C:\Users\Youssef\Projects\Jarvis_2.0`  
**Active Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Date:** 2026-11-05  

---

## 1. Executive Verdict

$$\mathbf{VERDICT:}\quad \mathbf{ACCEPTED\ WITH\ LIMITATIONS}$$

### Core Architecture & Invariant Status
1. **Permanent Memory Protection (PASSED):** `CandidatePromotionGate.promote_candidate()` unconditionally fails closed with `PROMOTION_PROHIBITED`. Neither in-process model calls, scripts, nor subagents can autonomously append to `docs/agent/LESSONS.md`. Permanent memory mutation strictly requires external human editing and Git commits.
2. **Three-Layer Epistemic Separation (PASSED):** `LessonCandidate` strictly segregates Layer A (observed facts), Layer B (provisional hypotheses defaulting to `INCONCLUSIVE`), and Layer C (verified root causes initialized to `None`).
3. **Approval Authenticity (NOT ESTABLISHED):** What the Phase 3D2 report termed an "approval token" is merely an unkeyed SHA-256 digest of the Action Manifest. It provides content integrity (tamper detection), but **zero human authentication**. Any model, script, or subagent can generate a valid token without human involvement.
4. **Data Loss on Re-Ingestion (FAILED / CRITICAL BUG):** Due to `if existing.candidate_id == candidate.candidate_id: continue` in `check_candidate_deduplication`, re-ingesting an already ingested event bypasses deduplication, treats the candidate as `UNIQUE`, and atomically overwrites the candidate on disk. Any verified Layer C root-cause findings and `PREPARED_FOR_REVIEW` review states are **silently erased and reset to `OBSERVED` with `verified_root_cause: None`**.
5. **Host Integration & Directory Disconnect (PARTIAL):** In `.agents/hooks.json`, the `Stop` hook is **unregistered**; only `PostToolUse` for `run_command` is declared. Furthermore, `failure_collector.py` persists events to `<artifactDirectoryPath>/learning_events/`, whereas `continuity_manager.py` scans `WORKSPACE_ROOT/data/learning_events/` by default. Live session events in artifact directories are never seen by the continuity manager without manual directory flags.
6. **Project Continuity Automation (NOT AUTOMATED):** `ProjectContinuityEngine` is a standalone CLI tool. It is not registered in any hook, does not execute upon session start, and its reports are printed to stdout rather than injected into the agent context packet.
7. **Performance Claims (DISPROVEN / EXAGGERATED):**
   - The reported ~12ms continuity check actually measures **~162ms median** (152–181ms) when running live Git subprocesses on Windows (a 13.5x discrepancy).
   - The reported <15ms hook overhead only applies to internal in-process Python logic (median 2.3ms). Out-of-process CLI hook execution takes **134–235ms** due to Python process startup.
   - The Tier 1 full fast suite (`tools/run_fast_tests.py --fast`) stalled and was manually terminated in Phase 3D2.

---

## 2. Actual Component and Hook Registration Matrix

| Subsystem Component | Registered in `.agents/hooks.json`? | Host Invoked in Real Session? | Live Evidence in Workspace | Audit Confidence | Assessment Notes |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **PostToolUse Failure Collector** | **YES** (lines 42–53, matcher: `run_command`) | **YES** | Exit code 0, fail-open `{}` on stdout | **HIGH** | Hook is registered for `run_command`. Writes to `<artifactDir>/learning_events/` if path provided. |
| **Stop Failure Collector** | **NO** | **NO** | Zero entries in `hooks.json` | **CERTAIN** | `parse_stop()` exists in Python source, but is completely absent from `.agents/hooks.json`. |
| **Project Continuity Engine** | **NO** | **NO** | Zero entries in `hooks.json` | **CERTAIN** | Standalone Python class; no `PreInvocation`, `SessionStart`, or CLI wrapper hook registered. |
| **Failure-to-Candidate Ingestion** | **NO** | **NO** | Zero entries in `hooks.json` | **CERTAIN** | Must be triggered manually via `python tools/continuity_manager.py ingest`. |
| **New-Session State Recovery** | **NO** | **NO** | Zero automated discovery | **CERTAIN** | Relies entirely on developer prompt instructions in `AGENTS.md` to run manual CLI commands. |
| **Candidate Review & Gate** | **NO** | **NO** | Manual CLI `preview` only | **HIGH** | In-process gating only; no automated orchestration with Antigravity subagents. |

---

## 3. Critical Audit A — Human Approval Authenticity Analysis

### Detailed Answers to the 10 Mandatory Questions

1. **Token Mechanism:** The approval token is an **unkeyed SHA-256 hex digest** of the Action Manifest JSON string:
   $$\text{approval\_token} \equiv \text{SHA256}(\text{canonical\_manifest\_json})$$
   It is neither an HMAC, a digital signature (RSA/ECDSA/Ed25519), nor a secret-derived token.
2. **Authority / Secret Possession:** **Nobody possesses a secret.** There is no private key, password, or pre-shared secret. Any entity that can read the candidate or invoke `CandidatePromotionGate.prepare_preview()` receives the exact digest.
3. **Model / Agent Token Construction:** **YES.** Any AI model, subagent, script, or subprocess can call `CandidatePromotionGate.prepare_preview(candidate)["action_manifest_digest"]` and pass that exact string back to `verify_approval_token()`. The gate returns `(True, "APPROVAL_BOUND: ...")`.
4. **Approval Identity Forgery:** **YES.** Because the token embeds no identity, any caller can claim to be a "human approver." Identity cannot be established or verified.
5. **Binding to Content, Destination, and Diff:** **YES (Content Integrity).** The Action Manifest embeds `candidate_content_hash`, `canonical_destination`, `diff_hash`, and `proposed_lesson_id`. If the candidate's Layer A, B, or C fields change, `compute_candidate_hash()` detects the drift and returns `(False, "CANDIDATE_MUTATED")`.
6. **Replayability:** **YES.** An unkeyed digest can be presented repeatedly across different sessions for the same candidate state.
7. **Deserialization / Replacement Bypass:** If a candidate JSON is tampered with on disk, its hash changes, causing the Action Manifest Digest to mismatch. However, if an attacker simultaneously updates the stored hash in the manifest, validation succeeds.
8. **Evidence Drift Acceptance:** If the files on disk referenced in `candidate.evidence_references` are modified externally without altering the candidate JSON metadata, `compute_candidate_hash()` does not detect the underlying file drift.
9. **Real Authenticated External Human Approval Mechanism:** **NO.** There is no cryptographic signing, no interactive CLI prompt with human terminal attachment, no OAuth/OIDC identity token, and no Webhook confirmation.
10. **What Constitutes the Human-Controlled Action:** The human action is **entirely external to the code**. Because `CandidatePromotionGate.promote_candidate()` unconditionally fails closed (`PROMOTION_PROHIBITED`), a human must manually copy the proposed diff, edit `docs/agent/LESSONS.md`, and execute a Git commit.

### Epistemic Categorization of Gating Attributes

| Property | Implementation in Phase 3D2 | Real Security Status |
| :--- | :--- | :--- |
| **Content Integrity** | SHA-256 over canonical JSON of candidate fields | **ESTABLISHED** (Strict tamper detection) |
| **Approval Authenticity** | String comparison against unkeyed manifest digest | **NOT ESTABLISHED** (Zero cryptographic proof of human action) |
| **Authorization** | `verify_approval_token` returns `True` for matching hash | **NOT ESTABLISHED** (Any agent can authorize itself in code) |
| **Candidate Identity** | Deterministic SHA-256 prefix `LCAND-{cid_digest}` | **ESTABLISHED** (Deterministic & collision-discriminated) |
| **Approval Freshness** | Manifest timestamp not bound to expiration window | **PARTIAL** (No TTL or nonce) |
| **Promotion Enforcement** | `promote_candidate()` unconditionally returns `False` | **ESTABLISHED** (Fail-closed architectural barrier) |

---

## 4. Critical Audit B & C — Persistence, Provenance & Data Loss Bug

### Empirical Reproduction of Critical Data-Loss Vulnerability

The following adversarial probe demonstrates silent erasure of verified root causes upon re-ingestion:

```python
# 1. Ingest event envelope
cand, status = pipeline.ingest_event_envelope(ev1)
# Result: status="CREATED", review_status="OBSERVED"

# 2. Complete Layer C verification & transition lifecycle
verify_root_cause(cand, "Verified root cause", "Repro", "Fix", "Test", "Scope")
transition_candidate_lifecycle(cand, "PREPARED_FOR_REVIEW")
pipeline.persist_candidate(cand)
# Result: review_status="PREPARED_FOR_REVIEW", verified_root_cause="Verified root cause"

# 3. Simulate subsequent session running 'continuity_manager.py ingest'
cand2, status2 = pipeline.ingest_event_envelope(ev1)
# Result: status="CREATED" (Bypassed duplicate detection!)

# 4. Inspect persisted candidate on disk
loaded = pipeline.load_candidate(cand.candidate_id)
# CRITICAL RESULT: review_status="OBSERVED", verified_root_cause=None
```

### Root Cause Analysis of Data Loss
In `tools/lesson_candidate.py` (lines 744–747):
```python
for existing in existing_candidates:
    if existing.candidate_id == candidate.candidate_id:
        continue  # <--- DEFECT: Skips candidate when candidate_id matches!
    if existing.deduplication_key == key:
        if is_reliable:
            return True, "EXACT_RELIABLE_DUPLICATE", existing
```
Because `create_candidate_from_event()` deterministically derives `candidate_id` from `event_id`, any candidate created from the same event has the exact same `candidate_id`. Line 745 skips it, causing `check_candidate_deduplication()` to return `(False, "UNIQUE", None)`. The pipeline then calls `persist_candidate()`, which atomically overwrites the verified candidate on disk with the fresh unverified candidate.

### Summary of Isolated Test Probes (10 Cases)

| Probe Case | Expected Invariant | Observed Behavior | Status |
| :--- | :--- | :--- | :---: |
| **1. Exact Duplicate** | Idempotent duplicate return | Overwrites candidate if `candidate_id` matches | **DEFECT** |
| **2. Same Invocation, Different Evidence** | Reliable dedup by step/tool | Correctly flagged `EXACT_RELIABLE_DUPLICATE` | **PASSED** |
| **3. Incomplete Event Identity** | Discriminator suffix applied | Key `INCOMPLETE_DISCRIMINATED:{id}` assigned | **PASSED** |
| **4. Missing Timestamp** | Safe empty default | Preserved as empty string; no unhandled exception | **PASSED** |
| **5. Missing Evidence** | Inconclusive classification | Created with `review_status="OBSERVED"`, `confidence="UNCONFIRMED"` | **PASSED** |
| **6. Multiple Failures (Same Step)** | Unique event identification | Discriminated by execution/step index formula | **PASSED** |
| **7. Cross-Session Duplicates** | Survives session boundary | Loaded from disk and deduplicated | **PASSED** |
| **8. Conflicting Evidence** | Prevents verification | `prepare_preview()` raises `ValueError` if Layer C incomplete | **PASSED** |
| **9. Corrupted Candidate File** | Fail-safe listing | `list_candidates()` ignores corrupt JSON without crashing | **PASSED** |
| **10. Identifier Collision** | Preserve distinct records | Suffix `_collision_{pid}` appended for discriminated duplicates | **PASSED** |

---

## 5. Critical Audit D — Project Continuity Engine Analysis

### Inspection Findings
1. **Invocation Context:** `ProjectContinuityEngine` is **never invoked automatically**. No hook or daemon calls it. A developer or agent must manually run `python tools/continuity_manager.py verify-state`.
2. **Context Packet Integration:** When executed, the engine outputs text to stdout (or JSON if `--json` is supplied). It **does not inject** its findings into Antigravity's prompt context, transcript, or PreInvocation packet.
3. **Ground Truth Authority:** The engine accurately respects Git as the sole empirical authority. It invokes `git branch --show-current`, `git rev-parse HEAD`, and `git status --short --untracked-files=all`.
4. **Staleness & Broken References:** Lines 238–248 correctly check referenced file existence on disk (`target_file.exists()`). If files documented in `PROJECT_STATE.md` are deleted, the status is set to `STALE`.
5. **Fail-Safe Robustness:** If `PROJECT_STATE.md` is deleted, corrupted, or lacks baseline headers, `verify_project_state()` returns `status="FAILED_SAFE"`, `recovery_state="UNVERIFIED_STATE"`.

---

## 6. Critical Audit E — Performance and Test Evidence

### Reconciled Test Inventory

The reported total of **198 passing tests** was independently re-executed and verified:

```powershell
python tools/run_fast_tests.py tests/test_continuity_and_lifecycle.py
# Output: 14 passed in 0.53s (Exit code: 0)

python tools/run_fast_tests.py tests/test_failure_collector.py
# Output: 39 passed in 0.20s (Exit code: 0)

python tools/run_fast_tests.py tests/test_lesson_candidate.py
# Output: 78 passed in 0.72s (Exit code: 0)

python tools/run_fast_tests.py tests/test_context_router.py
# Output: 60 passed in 0.28s (Exit code: 0)

python tools/run_fast_tests.py tests/test_hook_integration.py
# Output: 7 passed in 3.91s (Exit code: 0)
```
$$14 + 39 + 78 + 60 + 7 = 198 \text{ tests (All Passing)}$$

### Benchmark Evaluation (28 Holdout Scenarios + 10 Baseline)

```powershell
python tools/evaluate_lesson_retrieval.py
```
- **Holdout (28 scenarios):** Precision: 1.0000, Capacity-Bounded Recall: 1.0000, Unconstrained Recall: 0.9853 (HOLDOUT-23 capped at 3), MRR: 1.0000, nDCG@3: 1.0000, Irrelevant Injection: 0.0000.
- **Baseline (10 scenarios):** Precision: 1.0000, Recall: 1.0000, MRR: 1.0000, Irrelevant Injection: 0.0000.

### Empirical Latency Verification vs. Reported Claims

| Performance Metric | Reported Claim in 3D2 | Empirical Benchmark Reality | Discrepancy Factor | Diagnosis |
| :--- | :---: | :---: | :---: | :--- |
| **`verify_project_state`** | `~12ms` | **162.0 ms median** (152.6–181.7 ms) | **13.5x slower** | Reported claim measured in-memory regex parse only, omitting 3 Windows `git.exe` process spawns. |
| **Hook Overhead** | `<15ms` | **134.7 ms failure, 235.7 ms success** | **9x–15x slower** | Reported claim measured in-process Python logic (2.29ms). Actual out-of-process CLI hook startup takes >120ms. |
| **Internal Logic** | Unspecified | **2.29 ms median** (P95: 3.96ms) | Within spec | Pure in-process logic (JSON, redaction, dedup) is highly optimized. |
| **Tier 1 Full Suite** | Implied green | **STALLED & TERMINATED** | Failed to complete | `run_fast_tests.py --fast` stalled at ~17% due to loopback socket tests and was killed. |

---

## 7. Frozen Retrieval Boundary Checksums

Every protected file was verified bit-for-bit identical to its historical Phase 3D1D/3D2 baseline:

| File Path | Recorded Baseline SHA-256 | Current Live SHA-256 | Status |
| :--- | :--- | :--- | :---: |
| `docs/agent/LESSONS.md` | `bf11bc69ef8730bb03b40450c18aa04ab16442cf2d7cb5386ee2f342f3ffe670` | `bf11bc69ef8730bb03b40450c18aa04ab16442cf2d7cb5386ee2f342f3ffe670` | **MATCH** |
| `tests/eval/eval_retrieval_scenarios.json` | `3cf434392533eca8647acb8367df7a3a14c0e55ac831e8e5876e484cd776ba60` | `3cf434392533eca8647acb8367df7a3a14c0e55ac831e8e5876e484cd776ba60` | **MATCH** |
| `tests/eval/eval_holdout_registry.md` | `275044cb12c9466361174208a5c07a7ff501da023e44dac041f05e5401cec5eb` | `275044cb12c9466361174208a5c07a7ff501da023e44dac041f05e5401cec5eb` | **MATCH** |
| `tests/eval/eval_retrieval_holdout.json` | `0f2171a087ec5df9ff5f960e04cf1b63dd67be0b6f8ef7b236a836fecb0545b1` | `0f2171a087ec5df9ff5f960e04cf1b63dd67be0b6f8ef7b236a836fecb0545b1` | **MATCH** |
| `tools/context_router.py` | `dbe262e53633473c800828ccc9a1caf2f7ed7030c9a5efafb514cd004f35401e` | `dbe262e53633473c800828ccc9a1caf2f7ed7030c9a5efafb514cd004f35401e` | **MATCH** |
| `tests/test_context_router.py` | `9627e879fe18d979169485a23c2831bcae4be2d23be4d6a5817167a8e157ed14` | `9627e879fe18d979169485a23c2831bcae4be2d23be4d6a5817167a8e157ed14` | **MATCH** |
| `tools/evaluate_lesson_retrieval.py` | `3cbdf0b60a65ccc8cd267f6ac5aa4352ea2474fe78954686f3625f08e37c1ee8` | `3cbdf0b60a65ccc8cd267f6ac5aa4352ea2474fe78954686f3625f08e37c1ee8` | **MATCH** |

---

## 8. Exact Git Inventory & Preservation Baseline

- **Active Git Branch:** `feat/engineering-maturity-milestone`
- **HEAD Commit SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Staged Files:** 0 files (zero staging executed).
- **Tracked Modified Files (17 files):**
  `.agents/agents/security-auditor.md`, `.agents/agents/systems-architect.md`, `.agents/agents/test-guardian.md`, `.agents/hooks.json`, `.agents/rules/graphify.md`, `.agents/skills/evidence-first-context-recovery/SKILL.md`, `.agents/skills/verification-runner/SKILL.md`, `.agents/skills/windows-automation/SKILL.md`, `AGENTS.md`, `JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt`, `ROADMAP.md`, `docs/architecture/PROJECT_MAP.md`, `handoff.md`, `jarvis/core/win32_affinity.py`, `jarvis/skills/base.py`, `jarvis/skills/windows_uia.py`, `tests/test_windows_uia.py`.
- **Newly Created / Untracked Files Tracked in Phase 3D2:**
  - `tools/continuity_manager.py` (591 lines, 27,330 bytes)
  - `docs/agent/PHASE_3D2_ASSESSMENT.md` (164 lines, 15,533 bytes)
  - `tests/test_continuity_and_lifecycle.py` (608 lines, 25,120 bytes)
  - `data/learning_candidates/` (7 JSON files)
  - `data/learning_events/` (8 JSON files)

---

## 9. Findings Ranked by Severity

### Finding 1: Silent Overwrite & Erasure of Verified Candidates on Re-Ingestion
- **Severity:** **CRITICAL**
- **Component:** `tools/lesson_candidate.py:745` and `tools/continuity_manager.py:331`
- **Impact:** Re-running `python tools/continuity_manager.py ingest` erases all Layer C verified root causes, reproduction procedures, and review statuses from existing candidates, reverting them to unverified `OBSERVED` state with `verified_root_cause: None`.
- **Evidence:** Empirically proven in Section 4 test probe.

### Finding 2: Unkeyed SHA-256 Digest Misrepresented as "Human Approval Authenticity"
- **Severity:** **HIGH**
- **Component:** `CandidatePromotionGate.verify_approval_token`
- **Impact:** Any AI model or subagent can forge a "valid" approval token by hashing the Action Manifest. While `promote_candidate` prevents disk writes, the codebase claims to verify "human approval" when it only verifies content integrity.
- **Evidence:** Detailed analysis in Section 3.

### Finding 3: Failure Event Storage Directory Disconnect
- **Severity:** **HIGH**
- **Component:** `tools/failure_collector.py:606` vs `tools/continuity_manager.py:55`
- **Impact:** Real Antigravity hook invocations write failure envelopes to `<artifactDirectoryPath>/learning_events/`. However, `continuity_manager.py ingest` scans `WORKSPACE_ROOT/data/learning_events/` by default. Real failures collected during user sessions are silently ignored by the pipeline unless `--events-dir` is explicitly passed.

### Finding 4: Stop Hook Unregistered in `.agents/hooks.json`
- **Severity:** **MEDIUM**
- **Component:** `.agents/hooks.json`
- **Impact:** Premature agent terminations, timeouts, and cancellations are never captured by the failure collector during live sessions, despite `parse_stop()` being implemented in `failure_collector.py`.

### Finding 5: Project Continuity Engine Not Automated
- **Severity:** **MEDIUM**
- **Component:** `tools/continuity_manager.py:ProjectContinuityEngine`
- **Impact:** Session startup continuity relies on the AI agent reading prompts and manually executing shell commands. No programmatic hook triggers state reconciliation or injects recovery status into context.

### Finding 6: Exaggerated Execution Latency Claims in Phase 3D2 Report
- **Severity:** **LOW**
- **Component:** Performance Documentation
- **Impact:** Continuity verification was reported at 12ms (actually ~162ms). Hook execution was reported at <15ms (actually ~135–235ms out-of-process). Conflates in-process logic microbenchmarks with real Windows process execution overhead.

---

## 10. Explicit Acceptance Criteria & Focused Remediation Plan

To advance from `ACCEPTED WITH LIMITATIONS` to full final acceptance, a focused remediation phase (**Phase 3D2B**) must address the following:

1. **Fix Candidate Deduplication Bug:**
   In `tools/lesson_candidate.py` line 745, when `existing.candidate_id == candidate.candidate_id`:
   - If the existing candidate has Layer C findings (`verified_root_cause is not None`) or review status beyond `OBSERVED`, return `(True, "EXACT_RELIABLE_DUPLICATE", existing)` to **prevent overwriting verified memory**.
2. **Harmonize Event Directories:**
   Update `tools/continuity_manager.py` to check both `WORKSPACE_ROOT/data/learning_events` and `<appDataDir>/brain/*/learning_events` or configure `hooks.json` to pass a standardized repo-relative storage path.
3. **Register Stop Hook in `.agents/hooks.json`:**
   Add the `Stop` event declaration to `.agents/hooks.json` under `failure-collector`.
4. **Clarify Security Terminology:**
   Update documentation to explicitly state that the Action Manifest Digest enforces **Cryptographic Content Integrity**, and that **Human Authorization** is enforced solely by the fail-closed barrier in `promote_candidate()` requiring manual file edits.
5. **Session Startup Integration:**
   Add a fast, non-blocking check to `context_router.py` or a dedicated PreInvocation hook that runs `ProjectContinuityEngine` and injects drift warnings into the context packet if state has `DIVERGED`.
