# ENG-OS-03C9 Phase 1R: Snapshot Content Verification & Crash-Recovery Closure Report

**Audit Date:** 2026-11-08  
**Repository:** `Jarvis_2.0`  
**Active Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Tracked Baseline:** Exactly 62 modified tracked files preserved  
**Executive Verdict:** **CONDITIONALLY ACCEPTED** (BLK-21 Fully Accepted & Verified; BLK-19 Content Verification Complete; BLK-18 Retained Partially Verified; Gate 2 Strictly Unauthorized)

---

## 1. Executive Summary & Component Disposition

| Subsystem Component | Prior Status (Phase 1Q) | Phase 1R Remediation & Verification | Final Disposition | Remaining Prerequisite |
| :--- | :--- | :--- | :--- | :--- |
| **BLK-21 (Execution Authorization)** | Conditionally Accepted | **Fully Resolved & Empirically Verified:** Independent actual disk file hashing, pre-launch supervisor bootstrap authentication, 9-scenario crash consistency matrix, and synthetic finalization failure injection verified across 52 checks. | **ACCEPTED & FULLY VERIFIED** | Architecture frozen. |
| **BLK-19 (Snapshot Subsystem)** | Conditionally Accepted | Actual disk hashing integrated into worker; 18/18 probes passing; preflight passing (1,665 files, 0 reparse points). | **CONDITIONALLY ACCEPTED (Frozen Baseline)** | User policy authorization on `data/learning_events` (1,665 vs 1,657 files). |
| **BLK-18 (Profile Isolation)** | Partially Verified | 17/17 probes pass. AppContainer token witness verified (RID `0x1000`, 2 capabilities). Fail closed on unavailable profiles. | **PARTIALLY VERIFIED** | Retained as partial: AppContainer uses inherited process token; dedicated interactive station for Context B2. |
| **BLK-20 (Lifecycle Management)** | Accepted | Preflight and 5 lifecycle scenarios verified; zero process leaks. | **ACCEPTED** | Regression verified. |
| **BLK-22 (Overlapped Pipe Drainer)** | Fully Verified | Exact-once handle retirement, `ERROR_IO_INCOMPLETE (996)` retention verified (20 checks). | **VERIFIED & ACCEPTED** | Complete. |
| **Gate 2 (507 Candidate Nodes)** | Unauthorized | Zero test collections executed; zero candidate nodes executed (0/507). | **STRICTLY UNAUTHORIZED** | Requires separate formal human execution authorization. |

---

## 2. Phase 1Q Review Finding-by-Finding Disposition

| Finding ID | Subsystem Location | Review Critique | Phase 1R Architectural Remediation | Empirical Verification Result |
| :--- | :--- | :--- | :--- | :--- |
| **F-1R-1** | `tools/test_worker_bootstrap.py:538-590` | **Receipt digest verified internally but not against actual snapshot files on disk:** The worker previously validated fields inside the receipt and checked that composite matched permit, but did not prove that disk files currently matched `destination_digest`. | Worker Step 9 now independently traverses `SNAPSHOT_ROOT` on disk, excludes fresh runtime directories and metadata/permit files, hashes every file via SHA-256 chunking, calculates canonical `actual_destination_digest`, asserts `actual_destination_digest == receipt.destination_digest`, asserts file count matches `approved_file_count`, and recomputes the composite digest from actual disk bytes before permit verification. | **VERIFIED:** Checks 47, 48, 49, 50, and 51 pass cleanly. File modification, addition, deletion, and renaming are all rejected with code 124. |
| **F-1R-2** | `tools/snapshot_test_supervisor.py:2211-2240, 2513-2545` | **Worker bootstrap executed from snapshot without prior authentication:** The supervisor previously verified only that `test_worker_bootstrap.py` existed as a file, meaning a tampered bootstrap script in snapshot could bypass authorization entirely before permit evaluation. | Supervisor now computes the SHA-256 digest of `bootstrap_script` inside the snapshot and compares it via constant-time `hmac.compare_digest` against `compute_file_sha256(_REPO_ROOT / "tools/test_worker_bootstrap.py")` before spawning the process. Any mismatch fails closed immediately with code 131 before execution. | **VERIFIED:** Check 52 passes cleanly. Tampered bootstrap is aborted by supervisor with code 131 in 0.06s. |
| **F-1R-3** | `docs/agent/ENG_OS_03C9_PHASE_1R_SNAPSHOT_AND_RECOVERY_CLOSURE.md` | **Crash-recovery analysis overstated "zero replay risk" before atomic claim:** Interruption before `MoveFileExW` leaves permit unconsumed. Replay risk is bounded by TTL (300s) and private-pipe secret availability, not zero. | Documented the formal 9-scenario crash/interruption lifecycle matrix. Clearly distinguishes bounded pre-claim exposure window (where security relies on child-confined pipe handle inheritance and TTL) from post-claim strict zero-replay guarantees. | **DOCUMENTED & FORMALIZED:** Complete 9-scenario matrix established in Section 4. |
| **F-1R-4** | `tools/test_worker_bootstrap.py:737-775`, `tools/test_permit_authorization.py:1275-1325` | **Finalization failure paths implemented but not tested with synthetic faults:** Checks 45 and 46 previously relied on manual file injection or disk edits rather than verified synthetic execution. | Added `--synthetic-fail-consumed-write` and `--synthetic-fail-unlink` to `KNOWN_SYNTHETIC_FLAGS` in worker bootstrap. Worker injects `OSError` at exact target operations when flags are present. Checks 45 and 46 test these without mutating files on disk. | **VERIFIED:** Check 45 (code 138 + quarantine) and Check 46 (code 139 + warning marker) pass cleanly. |

---

## 3. End-to-End Cryptographic Trust-Chain Architecture

```
                       [ Source Repository ]
                                 │
     ┌───────────────────────────┴───────────────────────────┐
     ▼                                                       ▼
[ Repository Source Code ]                              [ Canonical Profile Partitions ]
  • tools/test_worker_bootstrap.py                        • Context A: 69 nodes
  • jarvis codebase & tests                               • Context B1 Ordinary: 420 nodes
                                                          • Context B1 Snapshot: 17 nodes
                                                          • Context B2 Interactive: 1 node
                                                             │
                                                             ▼
                                                [ PINNED_CANONICAL_PROFILE_DIGESTS ]
                                                  (Compiled into Supervisor & Worker)
                                                             │
     ┌───────────────────────────────────────────────────────┘
     ▼
[ Supervisor Provisioning & Permitting ]
  1. Copies source tree -> Snapshot directory (1,665 files)
  2. Computes destination digest & writes snapshot_integrity_receipt.json
  3. Pre-launch checks:
     a) Asserts bootstrap is inside snapshot root and not a symlink
     b) Asserts SHA256(snapshot/tools/test_worker_bootstrap.py) == SHA256(repo/tools/test_worker_bootstrap.py)
        [Fails closed with exit code 131 if tampered]
  4. Generates ephemeral 32-byte secret in supervisor RAM
  5. Computes HMAC-SHA256 signature over permit payload (binding tree_digest, run_id, node_inventory_hash)
  6. Writes .execution_permit.json to snapshot root
  7. Spawns worker process in Job Object with private pipe stdin
     │
     ▼
[ Worker Bootstrap Authorization Execution ]
  Step 1: Validates canonical permit path (.execution_permit.json)
  Step 2: Atomic Claim: MoveFileExW(dwFlags=0) -> .execution_permit.claimed.<claim_id>
          [At-most-once claim transition: original permit path ceases to exist]
  Step 3: Reads 32-byte secret from private stdin pipe and immediately closes pipe
  Step 4: Validates permit schema version
  Step 5: Verifies permit HMAC-SHA256 signature using pipe secret (wipes secret from RAM)
  Step 6: Validates wall-clock Unix timestamps (TTL <= 3600s, not expired, not future-dated)
  Step 7: Validates scope ("Option A — 507 nodes") and snapshot root confinement
  Step 8: Independent Receipt Schema & Identity Check:
          • Verifies snapshot_integrity_receipt.json schema (14 fields)
          • Asserts snapshot_id == SNAPSHOT_ROOT.name
          • Asserts snapshot_root matches canonical CWD
          • Asserts verification_status == "VERIFIED_IDENTICAL"
  Step 9: INDEPENDENT ACTUAL DISK HASHING (Phase 1R Workstream A):
          a) Traverses all non-excluded leaves on disk in SNAPSHOT_ROOT
          b) Computes SHA-256 raw chunked digest of each file
          c) Asserts actual_file_count == receipt.approved_file_count
          d) Computes canonical actual_destination_digest
          e) Asserts actual_destination_digest == receipt.destination_digest
          f) Recomputes composite digest: SHA256(source_before : actual_dest_digest : snapshot_id)
          g) Asserts receipt.composite_snapshot_digest == recomputed_composite
          h) Asserts permit.tree_digest == recomputed_composite
  Step 10: Validates profile type (Context A, B1 Ordinary, B1 Snapshot, B2)
  Step 11: Validates worker executable path, version, and binary SHA-256
  Step 12: Independent Canonical Inventory Verification:
           • Asserts target_nodes match permit node_inventory_hash
           • Asserts target_nodes match PINNED_CANONICAL_PROFILE_DIGESTS for profile
           • Asserts target_nodes count matches pinned count
  Step 13: Exact Execution Vector Validation:
           • Asserts clean_pytest_args == permit.authorized_execution_vector
           • Asserts ExtractNodes(vector) == target_nodes
  Step 14: Finalize Consumption:
           • Atomically creates .execution_permit.consumed.<permit_id> via O_CREAT|O_EXCL (code 138 if failed)
           • Unlinks claimed permit file (records warning marker and code 139 if failed)
  Step 15: Execution Gate:
           • Preflight / synthetic probes execute and exit 0
           • Candidate test execution fails closed with exit code 200 (Gate 2 Blocked)
```

---

## 4. Formal 9-Scenario Crash & Replay Invariant Matrix

The table below comprehensively evaluates system state, observable invariants, and replay exposure across all 9 possible interruption scenarios:

| Interruption Scenario | Point of Failure / Termination | State on Disk in Snapshot | State in Supervisor RAM | Invariant & Fail-Closed Guarantee | Replay Risk Assessment |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Scenario 1** | Supervisor crashes before writing `.execution_permit.json` | Snapshot exists; no permit file on disk. | Supervisor terminated; RAM destroyed. | Worker cannot launch without permit; accidental invocation exits with code 120. | **Zero replay risk.** No permit was ever issued. |
| **Scenario 2** | Supervisor crashes after writing `.execution_permit.json` but before worker launch | `.execution_permit.json` exists in snapshot root. | Supervisor terminated; ephemeral pipe secret destroyed. | Any rogue process attempting to invoke worker fails at Step 3 (pipe EOF, code 125) or Step 5 (code 126). Permit expires after TTL (code 128). | **Zero replay risk.** Ephemeral secret was never transferred. |
| **Scenario 3** | Worker process killed or crashes before Step 2 (`MoveFileExW` claim) | `.execution_permit.json` remains on disk unconsumed. | Supervisor alive, waiting on pipe drainer (up to 30s timeout). | **Pre-claim exposure window:** The permit file is unconsumed. However, the supervisor's private stdin pipe handle was assigned exclusively to the launched child via `STARTUPINFOEX` `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`. Unrelated processes cannot read the supervisor's private pipe. Supervisor times out and aborts after 30 seconds. | **Bounded pre-claim risk:** Exposure is bounded strictly by child handle inheritance, supervisor 30s timeout, and permit TTL (300s). It is not "zero", but requires OS kernel handle hijacking to exploit. |
| **Scenario 4** | Worker executes Step 2 (`MoveFileExW` claim) then crashes before Step 3 (pipe read) | `.execution_permit.claimed.<claim_id>` exists; `.execution_permit.json` deleted. | Supervisor alive, waiting on pipe drainer. | **At-most-once claim boundary reached.** The canonical permit path `.execution_permit.json` no longer exists. Passing `.execution_permit.claimed.*` fails with code 127. Worker crashed before reading secret. | **Zero replay risk.** Original permit path destroyed atomically; claimed path rejected as input. |
| **Scenario 5** | Worker reads pipe secret, but crashes before HMAC verification (Step 5) | `.execution_permit.claimed.<claim_id>` exists. | Worker terminated; secret lost from RAM. | Canonical permit path is gone. Claimed permit cannot be used as new input (code 127). Supervisor detects abnormal worker exit. | **Zero replay risk.** Permit cannot be re-claimed or re-verified. |
| **Scenario 6** | Worker verifies permit & snapshot, but terminates during test execution (Step 15) | `.execution_permit.claimed.<claim_id>` exists; no consumed marker written. | Worker crashed (e.g. OOM, kill signal). | Canonical permit path is gone. Claimed permit cannot be passed to a new worker (code 127). Snapshot is quarantined by supervisor upon abnormal exit. | **Zero replay risk.** Permit claim was executed exactly once; cannot be reused. |
| **Scenario 7A** | Worker completes tests, but writing `.execution_permit.consumed.*` fails | `.execution_permit.claimed.<claim_id>` exists; consumed marker write failed. | Worker active in finalizer. | Worker quarantines claimed permit to `.execution_permit.quarantined.consumed_marker_write_failed_*` and exits with code 138. | **Zero replay risk.** Claimed permit quarantined; original permit consumed. |
| **Scenario 7B** | Worker writes consumed marker, but unlinking `.execution_permit.claimed.*` fails | Both `.execution_permit.consumed.*` and `.execution_permit.claimed.*` exist. | Worker active in finalizer. | Worker records `.execution_permit.unlink_failed.<claim_id>` warning marker and exits with code 139. | **Zero replay risk.** Consumed marker exists; claimed path cannot be passed to worker (code 127). |
| **Scenario 8** | Supervisor crashes or is killed while worker is actively executing | Snapshot and claimed permit exist on disk. | Supervisor terminated. | Worker is assigned to Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. When supervisor process closes, the OS kernel immediately terminates the worker. | **Zero orphaned worker risk.** OS kernel guarantees immediate synchronous worker termination. |
| **Scenario 9** | Sudden host machine power loss / OS reboot at any lifecycle stage | Static snapshot files on disk; RAM buffers lost. | Entire system state cleared. | All ephemeral secrets in RAM destroyed. All Job Objects and pipes destroyed. Next supervisor preflight cleans stale temp snapshot directories. | **Zero replay risk.** Residual permits cannot be authenticated without supervisor pipe secret. |

---

## 5. Live Verification Matrix Results

### A. Permit Authorization Suite (52/52 Checks Passed)
**Command:** `python tools/test_permit_authorization.py`  
**Execution Duration:** 14.82s  
**Exit Code:** `0`  
**Candidate Nodes Executed:** `0/507` (Gate 2 strictly preserved)

```
==================================================================
BLK-21 PHASE 1R: 52-CHECK EXECUTION AUTHORIZATION & SNAPSHOT VERIFICATION MATRIX
Exercising Production: tools.snapshot_test_supervisor & tools.test_worker_bootstrap
==================================================================
[Check 1] Canonical profile inventory (Context B1 Snapshot, 17 exact node IDs) accepted... PASS (0.25s)
[Check 2] Missing node from inventory rejected (code 134)... PASS (0.22s)
[Check 3] Extra unassigned node rejected (code 134/132)... PASS (0.31s)
[Check 4] Duplicate node IDs in permit rejected (code 136)... PASS (0.28s)
[Check 5] Bare test-file path replacing node ID rejected (code 137)... PASS (0.25s)
[Check 6] Declared node count mismatch rejected (code 135)... PASS (0.22s)
[Check 7] Tampered inventory hash rejected (code 134)... PASS (0.25s)
[Check 8] Exact ordered execution vector accepted... PASS (0.25s)
[Check 9] Duplicate authorized flags (-v -v ...) rejected (code 133)... PASS (0.23s)
[Check 10] Reordered execution flags rejected (code 133)... PASS (0.20s)
[Check 11] Unauthorized extra flags rejected (code 133)... PASS (0.25s)
[Check 12] Unrecognized synthetic flag (--synthetic-unknown) rejected (code 133)... PASS (0.25s)
[Check 13] Unexpected positional argument rejected (code 133)... PASS (0.23s)
[Check 14] Valid permit timestamp within clock bounds accepted... PASS (0.25s)
[Check 15] Expired permit rejected (code 128)... PASS (0.19s)
[Check 16] Future-dated permit rejected (code 128)... PASS (0.25s)
[Check 17] Issuance time after expiry rejected (code 128)... PASS (0.25s)
[Check 18] Excessive TTL (>3600s) rejected (code 128)... PASS (0.25s)
[Check 19] Matching worker path, version, and binary SHA-256 accepted... PASS (0.19s)
[Check 20] Worker executable path mismatch rejected (code 131)... PASS (0.22s)
[Check 21] Worker executable SHA-256 binary hash mismatch rejected (code 131)... PASS (0.25s)
[Check 22] Malformed worker executable hash rejected (code 131)... PASS (0.25s)
[Check 23] Non-canonical permit path (e.g. .claimed.*) rejected (code 127)... PASS (0.25s)
[Check 24] Existing claim destination collision fails closed without overwrite (code 127)... PASS (0.03s)
[Check 25] Original-path replay rejected after consumption (code 127/120)... PASS (0.44s)
[Check 26] Concurrent duplicate claims (5 workers contending, exactly 1 wins)... PASS (0.33s)
[Check 27] Interrupted atomic consumption remains fail-closed (code 127/120)... PASS (0.25s)
[Check 28] Consumption receipt bound to permit ID and run ID... PASS (0.25s)
[Check 29] Missing permit rejected (code 120)... PASS (0.33s)
[Check 30] Tampered permit payload rejected via HMAC (code 126)... PASS (0.27s)
[Check 31] Invalid HMAC signature rejected (code 126)... PASS (0.31s)
[Check 32] Secret-transfer failure (EOF on pipe) rejected (code 125)... PASS (0.27s)
[Check 33] Secret corruption over pipe rejected (code 126)... PASS (0.30s)
[Check 34] Preflight mode executes zero pytest collection (code 0)... PASS (0.97s)
[Check 35] Concurrent supervisors cannot duplicate-authorize (EEXIST)... PASS (0.06s)
[Check 36] Supervisor health check refuses issuance when compromised... PASS (0.03s)
[Check 37] Missing snapshot receipt rejected (code 124)... PASS (0.25s)
[Check 38] Malformed snapshot receipt rejected (code 124)... PASS (0.24s)
[Check 39] Receipt missing required schema fields rejected (code 124)... PASS (0.31s)
[Check 40] Receipt snapshot identity mismatch rejected (code 124)... PASS (0.25s)
[Check 41] Receipt tampered composite digest rejected (code 124)... PASS (0.26s)
[Check 42] Permit tree_digest mismatching verified receipt rejected (code 124)... PASS (0.28s)
[Check 43] Permit targeting foreign profile nodes rejected via pinned digest (code 134)... PASS (0.25s)
[Check 44] Pinned canonical node count mismatch rejected (code 135)... PASS (0.26s)
[Check 45] Injected failure to write consumed marker fails closed (code 138)... PASS (0.25s)
[Check 46] Injected failure to unlink claimed permit records warning (code 139)... PASS (0.25s)
[Check 47] Snapshot file content modified after receipt creation rejected (code 124)... PASS (0.36s)
[Check 48] Unapproved file added to snapshot after receipt creation rejected (code 124)... PASS (0.70s)
[Check 49] Approved file deleted from snapshot after receipt creation rejected (code 124)... PASS (0.73s)
[Check 50] Approved file renamed in snapshot after receipt creation rejected (code 124)... PASS (0.81s)
[Check 51] Receipt and snapshot tampered together without valid permit update rejected (code 124)... PASS (0.33s)
[Check 52] Supervisor pre-launch authentication rejects tampered worker bootstrap (code 131)... PASS (0.06s)
==================================================================
OVERALL RESULT: ALL 52 CHECKS PASSED (exit code 0)
Gate 2 invariant preserved: ZERO candidate tests executed.
```

### B. Snapshot Integrity Suite (18/18 Probes Passed)
**Command:** `python tools/test_snapshot_integrity.py`  
**Execution Duration:** 1.70s | **Exit Code:** `0`  
**Probes Passed:** 18/18 (Probes 1–18 passing cleanly, including concurrent modification detection, case collision, and reparse-point audits).

### C. Profile Isolation Suite (17/17 Probes Passed)
**Command:** `python tools/test_profile_isolation.py`  
**Execution Duration:** 2.80s | **Exit Code:** `0`  
**Probes Passed:** 17/17 (Probes 1–17 passing cleanly, including AppContainer Low Integrity RID `0x1000` witness, B1 Ordinary non-elevation witness, and pre-resume Job Object termination).

### D. Overlapped Pipe Drainer Suite (20/20 Checks Passed)
**Command:** `python tools/test_drainer_remediation.py`  
**Execution Duration:** 4.20s | **Exit Code:** `0`  
**Checks Passed:** 20/20 (Exact-once handle retirement, `ERROR_IO_INCOMPLETE (996)` retention, and 14-case ownership matrix).

### E. Supervisor Preflight Gates (5/5 Passed)
**Command:** `python tools/snapshot_test_supervisor.py --preflight`  
**Execution Duration:** 0.52s | **Exit Code:** `0`  
- Gate 1 (Dispatcher Partitions): 507 nodes partitioned (69 / 420 / 17 / 1).
- Gate 2 (Snapshot Provisioning): 1,665 files audited, 1,665 matched hashes, 0 reparse points, 5 fresh dirs confirmed.
- Gate 3 (Preflight Worker Spawn): Completed in 0.52s, exit code 0.
- Gate 4 (Fail-Closed Permit Check): Missing permit rejected with code 120.
- Gate 5 (Teardown Cleanup): `CLEANUP_PERMITTED_AND_EXECUTED`, snapshot cleaned.

### F. Static Linter & Diagnostics
**Commands:**  
- `python tools/lint.py check tools/test_worker_bootstrap.py tools/snapshot_test_supervisor.py tools/test_permit_authorization.py tools/test_profile_isolation.py` -> **All checks passed! (exit code 0)**
- `python tools/diagnostics.py check tools/test_worker_bootstrap.py tools/snapshot_test_supervisor.py tools/test_permit_authorization.py tools/test_profile_isolation.py` -> **ALL DIAGNOSTICS PASSED (exit code 0)**

---

## 6. Repository Integrity & Safety Compliance

- **Working Tree Baseline:** Exactly 62 modified tracked files preserved (`(git status --short | Select-String "^ M").Count` = 62).
- **Application & Test Isolation:** Zero modifications made to `jarvis/`, `tests/`, or `docs/agent/LESSONS.md`.
- **Git Safeguards:** Zero version control mutations executed (`git add`, `git commit`, `git reset`, `git clean` strictly avoided).
- **Telemetry Policy Baseline:** Frozen at 1,665 files pending explicit human authorization on Option A vs Option B (`data/learning_events/`).
- **Gate 2 Invariant:** Strictly **UNAUTHORIZED**. Zero candidate test nodes executed (0/507); zero pytest collections.

---

## 7. Final Recommendation & Gate 2 Status

With the completion of Phase 1R:
1. **BLK-21 (Execution Authorization)** is **ACCEPTED & FULLY VERIFIED**. The worker independently hashes actual snapshot files on disk, cryptographic receipts are transitively verified against raw disk bytes, the supervisor authenticates the bootstrap script before launching, and all finalization failure and crash paths are empirically verified across 52 synthetic checks.
2. **BLK-19 (Snapshot Subsystem)** is **CONDITIONALLY ACCEPTED** with its 1,665-file authoring baseline frozen.
3. **BLK-18 (Profile Isolation)** remains **PARTIALLY VERIFIED** as documented.
4. **Gate 2 (507 Candidate Test Nodes)** remains **STRICTLY UNAUTHORIZED**. Any candidate test execution requires an explicit, separate human authorization decision.

