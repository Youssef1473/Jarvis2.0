# ENG-OS-03C9 Phase 1Q: Independent Authorization Closure Report

**Audit Date:** 2026-11-08  
**Repository:** `Jarvis_2.0`  
**Active Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Tracked Baseline:** Exactly 62 modified tracked files preserved  
**Executive Verdict:** **CONDITIONALLY ACCEPTED** (BLK-21 Fully Accepted; BLK-19 Authoring Baseline Frozen; Gate 2 Strictly Unauthorized)

---

## 1. Executive Summary & Component Disposition

| Subsystem Component | Prior Status (Phase 1P) | Phase 1Q Audit & Remediation | Final Status | Remaining Prerequisite |
| :--- | :--- | :--- | :--- | :--- |
| **BLK-21 (Execution Authorization)** | Conditionally Accepted (Gaps P1-A, P1-B) | **Fully Remediated:** Independent receipt chain implemented, pinned canonical inventory verified, fail-closed finalization verified across 46 synthetic checks. | **ACCEPTED & FULLY VERIFIED** | None. Architecture frozen. |
| **BLK-19 (Snapshot Subsystem)** | Conditionally Accepted | Preflight gates passed (1,665 files, 0 reparse points). Collision refusal (`FileExistsError`) verified. | **CONDITIONALLY ACCEPTED (Frozen Baseline)** | User policy authorization on `data/learning_events` (1,665 vs 1,657). |
| **BLK-18 (Profile Isolation)** | Partially Verified | 17/17 probes pass. AppContainer token witness verified (RID `0x1000`, 2 capabilities). Fail closed on unavailable profiles. | **PARTIALLY VERIFIED** | Retained as partial: AppContainer uses inherited token; dedicated interactive station for Context B2. |
| **BLK-20 (Lifecycle Management)** | Passing | Preflight and 5 lifecycle scenarios verified; zero process leaks. | **ACCEPTED** | Regression verified. |
| **BLK-22 (Overlapped Pipe Drainer)** | Fully Verified | Exact-once handle retirement, `ERROR_IO_INCOMPLETE (996)` retention verified (20 checks). | **VERIFIED & ACCEPTED** | Complete. |
| **Gate 2 (507 Candidate Nodes)** | Unauthorized | Zero test collections executed; zero candidate nodes executed (0/507). | **STRICTLY UNAUTHORIZED** | Requires separate formal human execution authorization. |

---

## 2. Phase 1Q Finding Register

| Finding ID | Prior Ref | Subsystem & File Location | Description & Remediation | Final Status |
| :--- | :--- | :--- | :--- | :--- |
| **F-1Q-1** | P1-A | `tools/test_worker_bootstrap.py:613-680` | **Tautological Node Inventory Hash Check:** Worker previously checked `sha256(nodes) == permit["node_inventory_hash"]`. Remediated by compiling immutable `PINNED_CANONICAL_PROFILE_DIGESTS` into bootstrap. Worker independently asserts node count and SHA-256 hash against the pinned partition. Tested via Checks 43 & 44. | **RESOLVED & VERIFIED** |
| **F-1Q-2** | P1-B | `tools/test_worker_bootstrap.py:480-575` | **Disconnected Snapshot Receipt Verification:** Worker previously performed format check only. Remediated by reading `snapshot_integrity_receipt.json`, validating 14-field schema, asserting `snapshot_id == SNAPSHOT_ROOT.name`, recomputing composite digest, and binding `tree_digest == receipt.composite_snapshot_digest`. Tested via Checks 37–42. | **RESOLVED & VERIFIED** |
| **F-1Q-3** | P1-C | `tools/test_worker_bootstrap.py:755-785` | **Permit Finalization Silent Error Suppression:** `except OSError: pass` previously masked finalization failures. Remediated: failed consumed-marker creation fails closed with code 138 and quarantines claimed permit; failed claimed-permit unlinking records distinct warning marker and exits with code 139. Tested via Checks 45 & 46. | **RESOLVED & VERIFIED** |
| **F-1Q-4** | P1-D | `tools/snapshot_test_supervisor.py:510-524` | **Crash Recovery & Replay Invariants:** At-most-once permit claim verified. Supervisor crash observation and quarantine matrix established across all 6 transition points. | **VERIFIED** |
| **F-1Q-5** | P1-E | `data/learning_events/` (8 files) | **Telemetry Retention Policy Decision:** Unchanged. Baseline remains frozen at 1,665 files pending explicit user authorization to adopt Option B (1,657 files). | **OPEN USER DECISION** |

---

## 3. Canonical Inventory Trust-Chain Diagram

```
      [ Source Repository: ExecutionProfileDispatcher ]
          │ (Master 507-Node Canonical Partition)
          ▼
   ┌──────────────────────────────────────────────┐
   │ Pinned Canonical Profile Partition Hashes    │
   │ Context A (69):        979c5292ddc8...       │
   │ Context B1 Ordinary:   f1e90f746186...       │
   │ Context B1 Snapshot:   e927cebf2fd1...       │
   │ Context B2:            3ac44a2f1843...       │
   └──────┬────────────────────────────────┬──────┘
          │                                │
 (Embedded in Supervisor)       (Embedded in Worker Bootstrap)
          │                                │
          ▼                                ▼
┌───────────────────────────┐    ┌───────────────────────────────────┐
│ Supervisor Permit Issuer  │    │ Worker Bootstrap Authorization    │
│ 1. Validates health       │    │ 1. Atomic claim (MoveFileExW)     │
│ 2. Extracts profile nodes │    │ 2. Reads pipe secret              │
│ 3. Computes node inv hash │    │ 3. Verifies permit HMAC-SHA256    │
│ 4. Issues signed permit   │───▶│ 4. Verifies target_nodes vs:      │
└───────────────────────────┘    │    a) permit node_inventory_hash  │
                                 │    b) PINNED canonical digest     │
                                 │    c) PINNED node count           │
                                 │ 5. Confinement:                   │
                                 │    ExtractNodes(argv) == target   │
                                 └───────────────────────────────────┘
```

---

## 4. Actual Snapshot-to-Receipt-to-Permit Verification Chain

The worker enforces an unbroken, independent 5-stage verification chain before executing any test:

$$\text{Actual Snapshot Directory} \xrightarrow{\text{Stage 1}} \text{Integrity Receipt} \xrightarrow{\text{Stage 2}} \text{Composite Digest} \xrightarrow{\text{Stage 3}} \text{Permit Tree Digest} \xrightarrow{\text{Stage 4}} \text{Execution Vector}$$

1. **Stage 1 (Identity & Presence):** Worker resolves `SNAPSHOT_ROOT` from its own process location. Asserts `snapshot_integrity_receipt.json` exists on disk.
2. **Stage 2 (Receipt Schema & Confinement):** Validates all 14 required schema fields. Asserts `receipt.snapshot_id == SNAPSHOT_ROOT.name` and `receipt.snapshot_root == str(SNAPSHOT_ROOT)`. Asserts `receipt.verification_status == "VERIFIED_IDENTICAL"`.
3. **Stage 3 (Cryptographic Recomputation):** Worker recomputes composite digest from component digests in receipt:
   $$\text{recomputed} = \text{SHA-256}(\text{source\_before\_digest} : \text{destination\_digest} : \text{snapshot\_id})$$
   Asserts `receipt.composite_snapshot_digest == recomputed`.
4. **Stage 4 (Permit Binding):** Asserts HMAC-authenticated `permit.tree_digest == recomputed`. Asserts `permit.run_id == receipt.run_id`.
5. **Stage 5 (Execution Confinement):** Asserts CLI arguments match `permit.authorized_execution_vector`.

---

## 5. Permit Finalization Failure Matrix

| Operation | Injected Failure Condition | Worker Behavior | Outcome State | Security Invariant |
| :--- | :--- | :--- | :--- | :--- |
| **Write Consumed Marker** | Target file already exists (`FileExistsError`), disk full, or write permission denied (`OSError`). | Fails closed. Calls `_quarantine_permit(claimed, "consumed_marker_write_failed_<id>")`. Exits with **code 138**. | Snapshot contains quarantined permit `.quarantined.consumed_marker_write_failed_*`. No `.consumed` receipt written. | **Fail-Closed:** Operation is aborted before execution. Claimed permit is quarantined and cannot be reclaimed. |
| **Unlink Claimed Permit** | File lock held by external scanner (`WinError 32`), permission denied (`OSError`). | Records distinct warning file `.execution_permit.unlink_failed.<id>`. Exits with **code 139**. | Snapshot contains warning marker `.unlink_failed.*` and `.consumed` receipt. | **Audit Trail Preserved:** Failure is reported distinctly to supervisor; claimed file cannot be re-used because original path is gone. |
| **Normal Finalization** | Clean disk environment. | Consumed marker written with `O_CREAT | O_EXCL`. Claimed file unlinked. Exits with **code 0** (probe) or **code 200** (Gate 2 block). | `.consumed.<permit_id>` written; claimed file deleted. | **Exact-Once Claim:** Single-use lifecycle finalized. |

---

## 6. Crash-Recovery Matrix

| Transition Point | Interruption Window | Resulting Filesystem State | Supervisor Detection & Observation | Action & Quarantine Policy | Reuse / Replay Vulnerability |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Point 1** | Before `MoveFileExW` claim | `.execution_permit.json` exists intact. Worker dead. | Worker process exits non-zero or supervisor times out. | Permit expires via TTL (300s). Supervisor tears down snapshot. | **Low:** Permit is unconsumed, but supervisor destroys snapshot directory. |
| **Point 2** | After claim, before HMAC check | `.execution_permit.claimed.<id>` exists. Original `.execution_permit.json` is GONE. | Supervisor drainer reads broken pipe; worker process dead. | Quarantines snapshot directory (`jarvis_snap_*_QUARANTINED`). | **Zero:** Original permit path does not exist. Claimed files cannot be passed as `--permit` (line 331). |
| **Point 3** | After HMAC check, before consumed marker | `.execution_permit.claimed.<id>` exists. No `.consumed` marker. | Supervisor detects premature worker termination. | Quarantines snapshot directory. | **Zero:** Original permit path is gone. Claimed file cannot be reclaimed. |
| **Point 4** | After consumed marker, before unlink | `.consumed.<id>` exists. `.claimed.<id>` exists. | Supervisor detects premature worker termination. | Quarantines snapshot directory. | **Zero:** Consumed marker confirms permit used. Original permit is gone. |
| **Point 5** | After unlink, before execution | `.consumed.<id>` exists. Claimed file deleted. | Worker exit code non-zero or timeout. | Supervisor marks execution failed; evaluates cleanup. | **Zero:** Permit permanently consumed. |
| **Point 6** | During test execution | `.consumed.<id>` exists. Worker terminates mid-test. | Process exit code recorded; output drainers capture partial stdout/stderr. | Supervisor evaluates conditional teardown; quarantines if descendants survive. | **Zero:** Permit consumed; tests aborted. |

> [!NOTE]
> **At-Most-Once Authorization Contract:**  
> The supervisor and worker protocol guarantees **at-most-once authorization**. In any crash or forced termination, the permit is permanently consumed or quarantined and cannot be replayed. Execution completion is determined solely by process exit code (0) and output drainer accounting.

---

## 7. HMAC Threat-Model Statement

### Protected Invariants
1. **Per-Run Ephemeral Secret:** A 32-byte cryptographically secure random token (`secrets.token_bytes(32)`) is generated in memory by the supervisor for each execution run.
2. **Out-of-Band Transfer:** The secret is transferred directly from supervisor to worker via inherited Win32 private pipe (`sys.stdin.buffer`). It is never written to disk, never logged, and never passed on CLI arguments.
3. **Pre-Launch Tamper Resistance:** An attacker modifying `.execution_permit.json` on disk cannot forge a valid HMAC signature without the in-memory pipe secret.
4. **Permit Substitution Resistance:** Substituting an old or foreign permit fails signature verification.
5. **Early Secret Discard:** The worker explicitly unbinds the secret (`del secret`, `del canonical_bytes`, `del expected_sig`) and closes `sys.stdin` immediately upon signature verification.

### Documented Threat Boundaries (What HMAC Cannot Protect Against)
1. **Compromised Worker Process:** If worker process memory is compromised *after* receiving the pipe secret, HMAC cannot prevent that process from executing arbitrary code within its granted OS token integrity.
2. **Python Memory Zeroization Limits:** Python `bytes` objects are immutable and managed by CPython's arena allocator. `del secret` removes the object reference and decrements the refcount, but does not guarantee immediate hardware-level cryptographic zeroization of RAM bytes.

---

## 8. Verification Results & Regression Matrix

All verification suites executed live and passed cleanly:

```powershell
# 1. Supervisor Preflight Gates (0.36s)
python tools/snapshot_test_supervisor.py --preflight
Result: ALL 5 GATES PASSED (1,665 files audited, composite digest: f0020815f034...)

# 2. Snapshot Integrity Matrix (0.08s)
python tools/test_snapshot_integrity.py
Result: ALL 18 PROBES PASSED (exit code 0)

# 3. Process Profile Isolation Matrix (0.47s)
python tools/test_profile_isolation.py
Result: ALL 17 PROBES PASSED (exit code 0)

# 4. Permit Authorization Matrix (Phase 1Q Expanded: 46 checks)
python tools/test_permit_authorization.py
Result: ALL 46 CHECKS PASSED (exit code 0)
  - Checks 1–36: Baseline authorization & replay checks (PASS)
  - Checks 37–42: Independent snapshot receipt verification (PASS)
  - Checks 43–44: Independent canonical inventory & node count verification (PASS)
  - Checks 45–46: Permit finalization fail-closed error handling (PASS)

# 5. Overlapped Pipe Drainer Matrix
python tools/test_drainer_remediation.py
Result: ALL 20 CHECKS PASSED (all 14 handle-ownership cases pass)

# 6. Static Linter & Structural AST-Grep Diagnostics
python tools/diagnostics.py check tools/snapshot_test_supervisor.py tools/test_worker_bootstrap.py tools/test_permit_authorization.py tools/test_profile_isolation.py
Result: ALL DIAGNOSTICS PASSED (exit code 0)
```

---

## 9. Git Baseline & Invariant Enforcement

- **Branch:** `feat/engineering-maturity-milestone`
- **HEAD Commit:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Modified Tracked Baseline:** Exactly **62 modified tracked files** preserved.
- **Protected Trees (`jarvis/`, `tests/`, `docs/agent/LESSONS.md`):** Zero modifications.
- **Git State Mutations:** Zero `git add`, `git commit`, `git reset`, `git clean`, stash, or checkout calls executed.
- **Gate 2 Invariant:** Exactly **0 candidate test nodes executed (0/507)**; **0 pytest collections executed**.

---

## 10. `data/learning_events` Explicit Disposition

- Current inventory baseline: **1,665 files** preserved.
- No modifications made to `data/learning_events/` policy or snapshot exclusions.
- Decision remains pending explicit human authorization before baseline transition to Option B (1,657 files).

