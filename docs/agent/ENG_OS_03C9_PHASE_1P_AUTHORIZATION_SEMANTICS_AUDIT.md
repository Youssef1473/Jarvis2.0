# ENG-OS-03C9 Phase 1P: Final Authorization Semantics & Baseline Freeze Audit

**Audit Date:** 2026-11-08  
**Repository:** `Jarvis_2.0`  
**Active Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Modified Tracked Baseline:** Exactly 62 modified tracked files preserved  
**Executive Verdict:** **CONDITIONALLY ACCEPTED** (Authoring-Stage Baseline Established; Gate 2 Strictly Unauthorized)

---

## 1. Executive Summary & Block Status Register

| Subsystem Block | Prior Status (Phase 1O) | Phase 1P Audit Assessment | Final Status | Smallest Remaining Prerequisite |
| :--- | :--- | :--- | :--- | :--- |
| **BLK-19 (Snapshot Subsystem)** | Conditionally Accepted | Disjoint-union path accounting rigorously verified (3,793 files = 1,665 copied + 2,128 excluded). Collision handling hardened to fail closed with `FileExistsError`. | **CONDITIONALLY ACCEPTED (Authoring Baseline Frozen)** | Human policy authorization on `data/learning_events` (Option A vs Option B). |
| **BLK-21 (Execution Authorization)** | Functionally Verified | Single-use atomic claim (`MoveFileExW`), crash resilience, HMAC-SHA256, and argument vector binding verified across 36 checks. Tautological checks rigorously mapped. | **CONDITIONALLY ACCEPTED (Scope Bound)** | Worker-side independent receipt binding (`tree_digest == receipt.composite_digest`). |
| **BLK-18 (Process Profile Isolation)** | Partially Verified | AppContainer child token witness verified (RID `0x1000`, 2 capabilities). Fail-closed behavior on unavailable desktop profiles verified. | **PARTIALLY VERIFIED** | Retained as partial: AppContainer uses inherited developer token; Context B2 requires dedicated interactive station. |
| **BLK-22 (Overlapped Pipe Drainer)** | Fully Verified | Exact-once retirement, `ERROR_IO_INCOMPLETE (996)` retention, and handle ownership state machine verified (Phase 1H). | **VERIFIED & ACCEPTED** | None. Complete. |
| **Gate 2 (507 Candidate Nodes)** | Unauthorized | Preserved. Zero test collections executed; zero candidate nodes executed. | **STRICTLY UNAUTHORIZED** | Requires separate formal human execution authorization. |

---

## 2. Audit Finding Register

| Finding ID | Severity | Subsystem & File Location | Description & Empirical Evidence | Status & Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| **F-1P-1** | **Medium** | `tools/test_worker_bootstrap.py:554-561` | **Tautological Node Inventory Hash Comparison:** Worker compares `sha256(target_nodes)` against `permit_data["node_inventory_hash"]`. Because `target_nodes` is also read from `permit_data["target_nodes"]`, the check verifies internal payload consistency under HMAC, but does not independently verify that the nodes match the canonical partition from `ExecutionProfileDispatcher`. | Documented limitation. Worker should independently verify against pinned canonical partition digest. |
| **F-1P-2** | **Medium** | `tools/test_worker_bootstrap.py:480-486` | **Disconnected Tree Digest Verification:** Worker inspects `expected_digest = permit_data.get("tree_digest")` and checks `len == 64`. It does not independently read `snapshot_integrity_receipt.json` to assert `composite_snapshot_digest == tree_digest`. | Documented limitation. Worker should verify receipt schema and link receipt composite digest to permit. |
| **F-1P-3** | **Low** | `tools/test_worker_bootstrap.py:633-642` | **Silent Exception Suppression in Permit Finalization:** Consumed marker creation and claimed permit unlinking use `except OSError: pass`. Unforeseen filesystem lock errors could silently pass without logging. | Documented. Claimed permits are permanently non-reusable regardless of unlinking because original path is gone. |
| **F-1P-4** | **Resolved** | `tools/snapshot_test_supervisor.py:510-524` | **Snapshot Collision Handled by Overwrite:** `provision_snapshot()` previously executed `if dest_dir.exists(): cleanup_snapshot(dest_dir, force=True)`, wiping existing directories. | **RESOLVED IN PHASE 1P:** Hardened to raise `FileExistsError` immediately and validate parent directory non-reparse status. |
| **F-1P-5** | **Policy** | `data/learning_events/` (8 files) | **Unresolved Telemetry Retention Decision:** 8 historical JSON telemetry envelopes are currently copied (inventory = 1,665). No candidate tests read them. Option B (exclusion + fresh dir) recommended. | **OPEN DECISION:** Left unchanged pending explicit human authorization. |

---

## 3. Independent Authorization Comparison Matrix

The table below classifies every security validation performed during worker bootstrap, identifying whether each comparison is **Independent** (comparing against a separate trusted source) or **Tautological** (comparing two fields originating from the same authenticated payload).

| Authorization Parameter | Worker Value Source | Expected Value Source | Verification Method | Assessment & Independence Classification |
| :--- | :--- | :--- | :--- | :--- |
| **Worker Executable Path** | `os.path.realpath(sys.executable)` | `permit_data["worker_exe_path"]` | Case-insensitive path normalization comparison | **INDEPENDENT:** Process queries its own OS binary path and asserts equality against permit. |
| **Worker Executable Hash** | Disk read of `sys.executable` | `permit_data["worker_exe_hash"]` | Real-time SHA-256 hash of on-disk binary | **INDEPENDENT:** Hashes actual running binary bytes; prevents binary substitution. |
| **Execution Argument Vector** | `sys.argv[1:]` (stripped of `--permit`) | `permit_data["authorized_execution_vector"]` | Exact ordered list equality | **INDEPENDENT:** Process inspects its own live CLI arguments against permit. |
| **Target Nodes Extraction** | Extracted from `sys.argv` (`::` filter) | `permit_data["target_nodes"]` | Ordered list equality | **INDEPENDENT:** CLI arguments cannot exceed or deviate from target nodes. |
| **Snapshot Root Confinement** | `Path(__file__).resolve().parent.parent` | `permit_data["snapshot_root"]` | Canonical path equality | **INDEPENDENT:** Worker derives its actual CWD / file location and matches permit root. |
| **Original Permit Canonical Name** | `Path(permit_arg).name` | Hardcoded literal `".execution_permit.json"` | Exact string match | **INDEPENDENT:** Prevents launching from `.claimed.*` or `.quarantined.*` permit files. |
| **Permit HMAC Signature** | Ephemeral secret from private named pipe | Recomputed SHA-256 HMAC over canonical JSON | `hmac.compare_digest()` | **INDEPENDENT:** Proves supervisor held the 32-byte secret transferred via Win32 pipe. |
| **Clock Expiration (TTL)** | `time.time()` (OS wall clock) | `permit_data["expires_at"]` & `["issued_at"]` | Clock window & `ttl_sec <= 3600` bounds check | **INDEPENDENT:** Compares live OS clock against permit window; rejects expired/future permits. |
| **Execution Profile Enum** | `permit_data["profile"]` | Hardcoded `valid_profiles` set | Set membership check | **INTERNAL ENUM VALIDATION:** Checks permit string against 4 valid profile names. |
| **Snapshot Tree Digest** | `permit_data["tree_digest"]` | None (checks `len(tree_digest) == 64`) | String length check | **TAUTOLOGICAL:** Worker does not read `snapshot_integrity_receipt.json` or rehash files. |
| **Node Inventory Hash** | `sha256(permit_data["target_nodes"])` | `permit_data["node_inventory_hash"]` | Recomputed SHA-256 string comparison | **TAUTOLOGICAL:** Both inputs read from same JSON object. Proves no internal corruption, but not canonical partition conformity. |

---

## 4. Permit State-Transition Diagram & Crash Semantics

```
               [ Supervisor Initiates Run ]
                           │
                           ▼
              ┌──────────────────────────┐
              │  State 1: ISSUED         │
              │  .execution_permit.json   │
              │  (O_CREAT | O_EXCL)       │
              └────────────┬─────────────┘
                           │
             [ Worker Launch / MoveFileExW ]
                           │
                           ▼
              ┌──────────────────────────┐
              │  State 2: CLAIMED        │
              │  .claimed.<pid>_<ts>_<r> │
              └────────────┬─────────────┘
                           │
                [ Pipe Secret Received ]
                           │
                           ▼
              ┌──────────────────────────┐
              │  State 3: AUTH_VERIFIED  │
              │  (HMAC + Clock + Exe)    │
              └────────────┬─────────────┘
                           │
              [ Consumed Marker Written ]
                           │
                           ▼
              ┌──────────────────────────┐
              │  State 4: CONSUMED       │
              │  .consumed.<permit_id>   │
              └────────────┬─────────────┘
                           │
              [ Claimed File Unlinked ]
                           │
                           ▼
              ┌──────────────────────────┐
              │  State 5: ARMED          │
              │  (Ready for Execution)   │
              └────────────┬─────────────┘
                           │
              [ Candidate Test Execution ]
                           │
                           ▼
              ┌──────────────────────────┐
              │  State 6: COMPLETED      │
              │  (Or Terminated/Failed)  │
              └──────────────────────────┘
```

### Crash Analysis Matrix Across Lifecycle Transitions

| Crash / Abort Point | Transition Window | On-Disk Filesystem State | Replay / Reuse Vulnerability | Fail-Closed Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| **Crash CP-0** | Prior to `MoveFileExW` | `.execution_permit.json` exists in `snapshot_root`. | Low. Permit has not been claimed. | Permit will expire automatically when `ttl_sec` (300s) elapses. Supervisor aborts run and quarantines/cleans snapshot. |
| **Crash CP-1** | Between Step 2 (`MoveFileExW`) and Step 3 (`HMAC`) | `.execution_permit.json` is GONE. File is `.execution_permit.claimed.<id>`. | **Zero.** Original path no longer exists. | Worker 2 looking for `.execution_permit.json` receives `ERROR_FILE_NOT_FOUND` (exit code 127). Claimed permits cannot be passed as `--permit` (line 331). |
| **Crash CP-2** | Between Step 3 (`HMAC`) and Step 4 (`consumed` marker) | File is `.execution_permit.claimed.<id>`. No `.consumed` receipt. | **Zero.** | Permitted file is renamed. Cannot be reclaimed or replayed. |
| **Crash CP-3** | Between Step 4 (`consumed` marker) and Step 5 (`unlink`) | `.execution_permit.consumed.<id>` exists. `.execution_permit.claimed.<id>` exists. | **Zero.** | Original permit is gone. Replay attempts fail closed. |
| **Crash CP-4** | After Step 5 (`unlink`), prior to test completion | `.execution_permit.consumed.<id>` exists. Claimed permit unlinked. | **Zero.** | Permit is permanently consumed. Worker process termination produces non-zero exit code or timeout. |

> [!IMPORTANT]
> **Single Successful Claim vs. Exactly-Once Execution:**  
> A successful permit claim (`MoveFileExW`) guarantees **at-most-once execution authorization**. It does **not** guarantee successful execution completion. If the worker crashes during test execution, the permit remains consumed and cannot be replayed; the supervisor output drainer records process termination and marks the run failed.

---

## 5. Snapshot Identity & Collision Handling Results

In Phase 1P, `provision_snapshot()` in `tools/snapshot_test_supervisor.py` was inspected and hardened:

1. **Collision Refusal (`FileExistsError`):**  
   Line 510 previously executed `if dest_dir.exists(): cleanup_snapshot(dest_dir, force=True)`.  
   Hardened to:
   ```python
   if dest_dir.exists():
       raise FileExistsError(f"SECURITY_GATE_FAILED: Snapshot destination already exists; collision refused: {dest_dir}")
   ```
   *Empirical Verification:* Tested via dedicated probe; pre-existing directory triggers immediate `FileExistsError` without wiping or executing.
2. **Canonical Path Validation:**  
   `dest_dir.resolve()` is executed before any filesystem checks. Parent directory is inspected for symlinks and `FILE_ATTRIBUTE_REPARSE_POINT (0x400)`.
3. **Reparse-Point Boundary Protection:**  
   Source is audited pre-copy (must be 0); destination is audited post-copy (must be 0). Traversal prunes directory entries in-place (`dirs[:] = kept_dirs`).
4. **Snapshot Re-use Prevention:**  
   Each supervisor run provisions a distinct directory name (`jarvis_snap_<uuid>`). Permitted execution is tied to `snapshot_root` string in HMAC payload.

---

## 6. Definition of "Physical Files" & Disjoint-Union Accounting

To resolve **F-1O-A**, the definition of "physical files" and the accounting methodology are empirically defined:

### Operational Definition
- **"Physical Files" = Filesystem File Leaves:** Recursively enumerated files where `os.path.isfile()` or `stat.S_ISREG()` is True.
- **Directories:** Excluded from the file leaf count. Directories are structural traversal paths (1,134 directories traversed in total across the repository).
- **Reparse Points:** Exactly 0 symlinks or NTFS junctions exist in source or destination.

### Disjoint-Union Proof Ledger

$$\text{Total Physical File Leaves (3,793)} = \text{Approved Copied Files (1,665)} + \text{Excluded Files (2,128)}$$

```
Repository Root Physical Leaves: 3,793
├── Approved Snapshot Inventory: 1,665 (43.90%)
└── Excluded Physical Files:     2,128 (56.10%)
    ├── Pruned in 42 Subtrees:   2,127
    └── Excluded Single Files:       1 (.env)
Overlapping Files: 0
Unclassified / Unaccounted Files: 0
Disjoint Union Parity: EXACT (1,665 + 2,128 = 3,793)
```

---

## 7. Final `data/learning_events` Policy Evaluation

### Telemetry Inventory (8 JSON Files)

| File Name | Size (Bytes) | Event Type | Redaction Status | Schema |
| :--- | :--- | :--- | :--- | :--- |
| `061fbc2e59c9ceebf1f11d89.json` | 626 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `20f3ee2a48c69f4598473840.json` | 626 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `78a29e18e72a7f5b67e09fbb.json` | 626 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `8a0d872346812c3ab2b8b7f5.json` | 625 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `8a327621fede38c2e56d44ab.json` | 695 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `b07f38879bb8559e7220307f.json` | 626 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `c1d96b1722129b50906a3723.json` | 626 | `POST_TOOL_USE` | `CLEAN` | 1 |
| `cb2ec53a55a829b3cadc3fc2.json` | 609 | `POST_TOOL_USE` | `CLEAN` | 1 |

### Consumer Analysis & Recommendation
- **Candidate Test Dependencies:** Exactly **0** of the 507 candidate test nodes read from or depend on `data/learning_events`.
- **Code Consumers:** Only `tools/continuity_manager.py` references `data/learning_events` for cross-session learning ingest. `tests/test_continuity_and_lifecycle.py` uses temporary directories (`tmp_path`).
- **Engineering Recommendation (Option B):** Exclude `data/learning_events` under `classify_exclusion_path` and add `data/learning_events` to `FRESH_RUNTIME_DIRS`. This ensures the test snapshot is initialized with a clean, empty directory, reducing baseline inventory from 1,665 to 1,657.
- **Explicit Status:** **UNRESOLVED PENDING USER AUTHORIZATION.** In accordance with strict instructions, zero modifications were made to the policy or inventory count in Phase 1P.

---

## 8. Synthetic Regression & Verification Matrix

All 4 specialized verification suites and static diagnostic tooling were executed live:

```powershell
# 1. Supervisor Preflight Gates (0.41s)
python tools/snapshot_test_supervisor.py --preflight
Result: ALL 5 GATES PASSED (1,665 files audited, composite digest: 2865e07ca1fa...)

# 2. Snapshot Integrity Matrix (0.14s)
python tools/test_snapshot_integrity.py
Result: ALL 18 PROBES PASSED (exit code 0)

# 3. Process Profile Isolation Matrix (0.61s)
python tools/test_profile_isolation.py
Result: ALL 17 PROBES PASSED (exit code 0)

# 4. Permit Authorization Matrix (0.64s)
python tools/test_permit_authorization.py
Result: ALL 36 CHECKS PASSED (exit code 0)

# 5. Overlapped Drainer Remediation Matrix
python tools/test_drainer_remediation.py
Result: ALL 20 CHECKS PASSED (all 14 handle-ownership cases pass)

# 6. Static Linter & Structural AST-Grep Diagnostics
python tools/diagnostics.py check tools/snapshot_test_supervisor.py tools/test_worker_bootstrap.py
Result: ALL CHECKS PASSED (exit code 0)
```

---

## 9. Git Baseline & Invariant Enforcement

- **Branch:** `feat/engineering-maturity-milestone`
- **HEAD Commit:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Modified Tracked Baseline:** Exactly **62 modified tracked files** preserved.
- **Protected Trees (`jarvis/`, `tests/`, `docs/agent/LESSONS.md`):** Zero modifications.
- **Git State Mutations:** Zero `git add`, `git commit`, `git reset`, `git clean`, stash, or checkout calls executed.
- **Gate 2 Invariant:** Exactly **0 candidate test nodes executed (0/507)**; **0 pytest collections executed**.

