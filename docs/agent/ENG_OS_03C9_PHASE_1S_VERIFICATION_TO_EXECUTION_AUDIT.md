# ENG-OS-03C9 Phase 1S: Verification-to-Execution Integrity Audit & Authorization Boundary Closure Report

**Audit Date:** 2026-11-08  
**Repository:** `Jarvis_2.0`  
**Active Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Tracked Baseline:** Exactly 62 modified tracked files preserved  
**Executive Verdict:** **CONDITIONALLY ACCEPTED** (BLK-21 Fully Accepted & Verified; BLK-19 Filesystem Boundary & Content Integrity Verified; BLK-18 Retained Partially Verified; Gate 2 Strictly Unauthorized)

---

## 1. Executive Summary & Component Disposition

| Subsystem Component | Prior Status (Phase 1R) | Phase 1S Remediation & Empirical Verification | Final Disposition | Remaining Prerequisite |
| :--- | :--- | :--- | :--- | :--- |
| **BLK-21 (Execution Authorization Boundary)** | Conditionally Accepted | **Fully Resolved & Empirically Verified:** Kernel `FILE_SHARE_READ` bootstrap locking, strict candidate vs synthetic mutual exclusion, fresh runtime directory emptiness assertions, legitimate permit lifecycle filtering, ephemeral secret invalidation on supervisor restart, and 60/60 checks passing. | **ACCEPTED & FULLY VERIFIED** | Architecture frozen; ready for operational integration under authorized profiles. |
| **BLK-19 (Snapshot Filesystem & TOCTOU Boundary)** | Conditionally Accepted | Full TOCTOU boundary audited; NTFS DACL limits under Medium Integrity characterized; independent disk hash verification complete (18/18 probes pass; preflight passes at 1,665 files). | **CONDITIONALLY ACCEPTED (Frozen Baseline)** | Human policy authorization on `data/learning_events` (1,665 vs 1,657 files). |
| **BLK-18 (Profile Isolation)** | Partially Verified | 17/17 probes pass. AppContainer child token witness verified (RID `0x1000`, 2 capabilities). Fail-closed on unavailable profiles. | **PARTIALLY VERIFIED** | Retained as partial: AppContainer uses inherited process token; Context B2 requires dedicated interactive station. |
| **BLK-20 (Lifecycle Management)** | Accepted | Preflight (5 gates) and 5 lifecycle scenarios verified; zero orphan processes. | **ACCEPTED** | Regression verified. |
| **BLK-22 (Overlapped Pipe Drainer)** | Fully Verified | Exact-once handle retirement, `ERROR_IO_INCOMPLETE (996)` retention verified (20 checks). | **VERIFIED & ACCEPTED** | Complete. |
| **Gate 2 (507 Candidate Nodes)** | Unauthorized | Zero test collections executed; zero candidate nodes executed (0/507). | **STRICTLY UNAUTHORIZED** | Requires separate formal human execution authorization. |

---

## 2. Phase 1R Review Finding-by-Finding Disposition

| Finding ID | Subsystem Location | Review Critique | Phase 1S Architectural Remediation | Empirical Verification Result |
| :--- | :--- | :--- | :--- | :--- |
| **F-1S-1** | `tools/test_worker_bootstrap.py:580-640` | **Verification-to-Execution Integrity (TOCTOU) Window:** Independent disk hashing verifies snapshot content at Step 9, but does not guarantee continuous NTFS immutability between verification and pytest execution under Medium Integrity. | Audited full NTFS security descriptor under Medium Integrity (Context B1). Formally documented that `tempfile.mkdtemp` creates folders with Full Control `(F)` for the current user token; neither DOS `stat.S_IREAD` nor NTFS DACLs provide kernel-level tamper resistance against concurrent same-user processes. Documented this trust boundary in the threat model: verification proves snapshot cleanliness *at entry*, while execution isolation relies on process-level containment (Win32 Job Objects) rather than filesystem write locks. | **DOCUMENTED & AUDITED:** Empirical ACL probe verified; point-in-time vs continuous immutability boundary established in Section 3. |
| **F-1S-2** | `tools/snapshot_test_supervisor.py:2230-2265, 2530-2565` | **Bootstrap Launch-Time Race Window:** Supervisor verified `SHA256(snapshot bootstrap) == SHA256(repo bootstrap)`, but closed the file handle before invoking `subprocess.Popen`, creating a brief race window where a concurrent process could alter the script before execution. | Added Win32 kernel sharing lock (`CreateFileW` with `GENERIC_READ` and `FILE_SHARE_READ`) held continuously across hash verification and process spawning. The lock is released only after the child process is launched suspended inside the Job Object. Any concurrent write/delete fails immediately with `ERROR_SHARING_VIOLATION (32)`. | **VERIFIED:** Check 53 proves concurrent write fails with `ERROR_SHARING_VIOLATION`; Check 54 proves hash mismatch fails with exit code 131. |
| **F-1S-3** | `tools/test_worker_bootstrap.py:590-620` | **Fresh Runtime Directories & Non-Canonical Exclusion Risk:** Excluded directories (`jarvis/skills/custom`, `data/evolution_scratch`, `data/snapshots`, `data/learning_candidates`) and permit file variants could potentially hide unapproved implants from snapshot disk hashing. | Added Step 9A: worker independently asserts that every excluded fresh runtime directory is 100% empty (leaf count == 0), failing closed with code 124 if unapproved files exist. Replaced wildcard permit filtering with strict `_is_legitimate_permit_lifecycle_file()` regex; non-canonical names (e.g. `.execution_permit.py`) are included in hashing and fail closed with code 124. | **VERIFIED:** Check 55 (unapproved file in fresh dir) and Check 56 (non-canonical permit file) fail closed with exit code 124. |
| **F-1S-4** | `tools/snapshot_test_supervisor.py:1060-1075`, `tools/test_worker_bootstrap.py:790-815` | **Synthetic-Mode Authorization Boundary:** Synthetic failure flags (`--synthetic-fail-unlink`, `--synthetic-fail-consumed-write`, etc.) could theoretically be combined with candidate test nodes, creating an execution bypass or masking candidate test results. | Added strict mutual exclusion in both supervisor and worker. Supervisor refuses to issue any permit containing candidate test nodes (`::`) alongside any flag in `SYNTHETIC_FAILURE_FLAGS`. Worker independently inspects CLI arguments and quarantines the permit with exit code 133 if synthetic failure flags appear in candidate execution mode. | **VERIFIED:** Check 57 (supervisor refusal) and Check 58 (worker quarantine with code 133) pass cleanly. Check 46 (synthetic unlink failure without candidate nodes) passes. |
| **F-1S-5** | `tools/snapshot_test_supervisor.py:970-1045` | **Supervisor Restart & Ephemeral Secret Invalidation:** Formal clarification required distinguishing ephemeral secret invalidation on supervisor restart from atomic file claiming and TTL expiration. | Verified that supervisor generates a fresh 32-byte secret per run via `secrets.token_bytes(32)` held exclusively in supervisor memory. On supervisor restart, prior unconsumed permits cannot be verified (HMAC signature mismatch, exit code 126). Expired permits are rejected regardless of secret or snapshot validity (exit code 128). | **VERIFIED:** Check 59 (supervisor restart rejects prior permit with code 126) and Check 60 (expired permit rejected with code 128) pass cleanly. |

---

## 3. Workstream A: Verification-to-Execution Integrity & TOCTOU Audit

### 3.1 The Time-of-Check to Time-of-Use (TOCTOU) Analysis
In Phase 1R, Worker Step 9 introduced independent raw disk hashing: before claiming or executing a permit, the worker traverses `SNAPSHOT_ROOT`, hashes every non-excluded file, asserts that `actual_file_count == receipt.approved_file_count`, and asserts that `actual_destination_digest == receipt.destination_digest`.

However, on Windows NTFS under **Context B1 Ordinary (Medium Integrity)**, the security boundaries between processes running under the same user security context have fundamental limitations that must be explicitly acknowledged:

1. **NTFS Default DACLs:**
   When `tempfile.mkdtemp` provisions a snapshot directory in `%TEMP%` (`C:\Users\Youssef\AppData\Local\Temp\jarvis_snap_*`), NTFS assigns permissions inheriting from the parent:
   - `NT AUTHORITY\SYSTEM`: Full Control `(F)`
   - `BUILTIN\Administrators`: Full Control `(F)`
   - `CURRENT_USER` (Medium Integrity Token): Full Control `(F)`
2. **Same-User Process Equality:**
   Under standard Windows access control, any process running under the same logon session and user token has identical rights to open, write, rename, or delete files within that directory.
3. **Read-Only Attributes are Ineffective Against Same-User Modification:**
   Setting the DOS Read-Only attribute (`stat.S_IREAD` / `FILE_ATTRIBUTE_READONLY`) or removing write permissions via NTFS DACLs does *not* create a tamper-proof barrier against other processes in the same user session: any process running with the same user token has `WRITE_DAC` / `FILE_WRITE_ATTRIBUTES` and can trivially remove the read-only attribute or restore Full Control before modifying the file.
4. **Kernel Write Locks vs Pytest Execution:**
   Holding kernel write locks (`FILE_SHARE_READ`) across all 1,665 files during pytest execution is operationally infeasible: `pytest` and Python's `importlib` require opening files for bytecode compilation (`.pyc`), directory scanning, and temp file creation.

### 3.2 Formal Threat Model & Boundary Disposition
- **What Point-in-Time Disk Hashing Guarantees:**
  It proves that the snapshot tree matches the repository baseline *at the exact moment Step 9 executes*, detecting any source mutation during copy, snapshot provisioning race, or offline disk injection.
- **What Point-in-Time Disk Hashing Does NOT Guarantee:**
  It does not provide continuous cryptographic immutability against malicious concurrent processes running under the developer's user token between Step 9 completion and pytest module import.
- **Subsystem Safeguard:**
  True isolation against concurrent host processes requires **Context A (AppContainer)** with Low Integrity and restricted SID ACLs, where the child token is mathematically barred from accessing unapproved host resources, or **Win32 Job Objects** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` preventing background processes from outliving the supervisor.

---

## 4. Workstream B: Bootstrap Trust-Chain & Launch-Race Elimination

### 4.1 Repository Equivalence vs Cryptographic Authenticity
In Phase 1R, the supervisor asserted:
$$\text{SHA256}(\text{snapshot/tools/test\_worker\_bootstrap.py}) == \text{SHA256}(\text{repo/tools/test\_worker\_bootstrap.py})$$

This check establishes **source equivalence** (the script to be executed in the snapshot is bit-for-bit identical to the script in the active repository). However, it does not establish independent cryptographic authenticity: if an adversary had write access to the repository, both copies would match. Independent cryptographic trust derives from:
1. Pinned canonical digests compiled into the supervisor and worker (`PINNED_CANONICAL_PROFILE_DIGESTS`).
2. The supervisor's own trusted memory state and immutable Python executable binary hash (`get_worker_exe_hash()`).

### 4.2 Eliminating the Launch-Time Race via Win32 `FILE_SHARE_READ` Lock
Previously, the supervisor computed the SHA-256 hash, closed the file handle, and then spawned `subprocess.Popen`. This left a microsecond race window where another process could modify or replace `test_worker_bootstrap.py` before Python opened it.

In Phase 1S, `tools/snapshot_test_supervisor.py` eliminates this window:
```python
# Acquire kernel handle with FILE_SHARE_READ
h_bootstrap = win32file.CreateFileW(
    str(bootstrap_script.resolve()),
    win32con.GENERIC_READ,
    win32con.FILE_SHARE_READ,  # Allows python.exe to read, blocks all concurrent writers
    None,
    win32con.OPEN_EXISTING,
    win32con.FILE_ATTRIBUTE_NORMAL,
    None,
)
try:
    # 1. Compute and verify hash while holding kernel lock
    actual_bootstrap_hash = hashlib.sha256(bootstrap_bytes).hexdigest().lower()
    if not hmac.compare_digest(actual_bootstrap_hash, expected_bootstrap_hash):
        raise RuntimeError("Worker bootstrap SHA-256 hash mismatch")
    
    # 2. Spawn child process (python.exe inherits read access via FILE_SHARE_READ)
    proc = subprocess.Popen(...)
finally:
    # 3. Close handle after child is created inside Job Object
    win32file.CloseHandle(h_bootstrap)
```

**Empirical Verification:**
- **Check 53:** Supervisor sharing lock blocks concurrent modification: an attempt to open `test_worker_bootstrap.py` for writing while the supervisor holds the lock raises Win32 `ERROR_SHARING_VIOLATION (32)`. (PASSED, 0.03s)
- **Check 54:** Pre-launch hash mismatch fails closed immediately before process execution with exit code 131. (PASSED, 0.03s)

---

## 5. Workstream C: Canonical Inventory & Exclusion Audit

### 5.1 Emptiness Assertion on Fresh Runtime Subtrees
The snapshot provisioning process excludes 5 fresh runtime directories that are recreated empty in the destination:
1. `jarvis/skills/custom`
2. `data/evolution_scratch`
3. `data/snapshots`
4. `data/learning_candidates`
5. `temp`

To guarantee that an implant cannot be staged inside an excluded subtree without being detected by disk hashing, Step 9A was added to `tools/test_worker_bootstrap.py`:
```python
for rel_dir in FRESH_RUNTIME_DIRS:
    d = SNAPSHOT_ROOT / rel_dir
    if d.is_dir():
        leaves = [p for p in d.rglob("*") if p.is_file()]
        if leaves:
            _quarantine_permit(claimed_permit_path, f"unapproved_files_in_fresh_dir_{claim_id}")
            sys.stderr.write(f"[BOOTSTRAP AUTHORIZATION ERROR] Fresh runtime directory '{rel_dir}' contains unapproved files: {[str(p.name) for p in leaves[:5]]}\n")
            sys.exit(124)
```
**Empirical Verification:**
- **Check 55:** Staging an unapproved file inside `jarvis/skills/custom` is detected by Step 9A, causing immediate permit quarantine and process abort with exit code 124. (PASSED, 0.25s)

### 5.2 Strict Permit Lifecycle Filtering
During Step 9 disk hashing, the worker must ignore permit lifecycle artifacts created during execution (`.execution_permit.json`, `.claimed.*`, `.quarantined.*`, `.consumed.*`, `.unlink_failed.*`). Previously, broad prefix filtering could allow non-canonical names (such as `.execution_permit.py`) to bypass hashing.

Phase 1S replaces broad filtering with an explicit regex:
```python
def _is_legitimate_permit_lifecycle_file(name: str) -> bool:
    import re
    PATTERNS = [
        r"^\.execution_permit\.json$",
        r"^\.execution_permit\.claimed\.[0-9a-fA-F\-]+$",
        r"^\.execution_permit\.quarantined\.[0-9a-fA-F\-]+$",
        r"^\.execution_permit\.quarantined\.[a-zA-Z0-9_\-]+_[0-9a-fA-F\-]+$",
        r"^\.execution_permit\.consumed\.[0-9a-fA-F\-]+$",
        r"^\.execution_permit\.unlink_failed\.[0-9a-fA-F\-]+$",
    ]
    return any(re.match(pat, name) for pat in PATTERNS)
```
**Empirical Verification:**
- **Check 56:** A non-canonical file named `.execution_permit.py` is treated as an unapproved file, triggering destination digest mismatch and failing closed with exit code 124. (PASSED, 0.25s)

---

## 6. Workstream D: Synthetic-Mode Authorization Boundary

To prevent synthetic failure flags from being used to bypass candidate test execution or mask failures, Phase 1S establishes **strict mutual exclusion** between candidate test nodes and synthetic flags in both supervisor and worker.

### 6.1 Defined Synthetic Failure Flags
```python
SYNTHETIC_FAILURE_FLAGS = frozenset({
    "--synthetic-fail-unlink",
    "--synthetic-fail-consumed-write",
    "--synthetic-timeout",
    "--synthetic-held-writer",
    "--synthetic-descendant",
})
```

### 6.2 Supervisor Enforcement
In `tools/snapshot_test_supervisor.py:issue_execution_permit()`:
```python
has_candidate_nodes = any("::" in arg for arg in authorized_execution_vector)
if has_candidate_nodes:
    failure_flags = [arg for arg in authorized_execution_vector if arg in SYNTHETIC_FAILURE_FLAGS]
    if failure_flags:
        raise ValueError(
            f"Synthetic failure flags {failure_flags} are strictly prohibited when targeting candidate test nodes."
        )
```

### 6.3 Worker Enforcement
In `tools/test_worker_bootstrap.py:Step 13`:
```python
has_candidate_nodes = any("::" in arg for arg in clean_pytest_args)
if has_candidate_nodes:
    failure_flags_in_args = [arg for arg in clean_pytest_args if arg in SYNTHETIC_FAILURE_FLAGS]
    if failure_flags_in_args:
        _quarantine_permit(claimed_permit_path, f"synthetic_failure_flag_in_candidate_mode_{claim_id}")
        sys.stderr.write(f"[BOOTSTRAP AUTHORIZATION ERROR] Synthetic failure flags {failure_flags_in_args} strictly prohibited in candidate execution mode.\n")
        sys.exit(133)
```

**Empirical Verification:**
- **Check 57:** Supervisor refuses to issue a permit combining candidate test nodes with `--synthetic-fail-unlink`, raising `ValueError`. (PASSED, 0.03s)
- **Check 58:** Worker independently detects synthetic flags in candidate execution mode, quarantines the permit, and aborts with exit code 133. (PASSED, 0.25s)
- **Check 46:** Synthetic unlink failure in synthetic mode (without candidate nodes) executes cleanly, writes warning marker, and exits with code 139. (PASSED, 0.25s)

---

## 7. Workstream E: Comprehensive 9-Scenario Crash & Replay Lifecycle Matrix

| Scenario # | Failure Stage / Crash Event | State of `.execution_permit.json` on Disk | HMAC Secret State in Memory / Pipe | Subsequent Replay Attempt Outcome | Formally Verified Guarantee |
| :---: | :--- | :--- | :--- | :--- | :--- |
| **S1** | Supervisor crashes *before* writing permit | Non-existent | Secret lost with process death | Cannot claim non-existent file (`FileNotFoundError`) | **Zero exposure** |
| **S2** | Supervisor crashes *after* writing permit, *before* child launch | Present at original path | Secret lost with supervisor death; new supervisor generates new secret | Child/attacker lacks secret; new supervisor rejects permit signature (exit code 126) | **Fail-closed:** Ephemeral secret invalidation prevents claim (Check 59) |
| **S3** | Worker crashes *before* Step 2 (`MoveFileExW`) | Present at original path | Pipe broken / EOF | Pipe read fails with code 125; replay bounded by TTL (300s) | **TTL bounded** (Check 15, Check 60) |
| **S4** | Multiple workers contend for Step 2 simultaneously | Transitioning | Passed via private pipe | Exactly 1 worker succeeds; others fail with `ERROR_FILE_EXISTS` (code 127) | **At-most-once claim** (Check 26) |
| **S5** | Worker crashes *after* Step 2, *before* Step 14 | Renamed to `.claimed.<id>` | Secret wiped from worker RAM | Original path does not exist (code 120); claimed path rejected by Step 1 (code 127) | **Zero replay:** Claimed permit cannot be re-claimed |
| **S6** | Worker fails to write `.consumed.<id>` marker | Renamed to `.claimed.<id>` | Secret wiped | Moved to `.quarantined.consumed_marker_write_failed_*`; exit code 138 | **Fail-closed quarantine** (Check 45) |
| **S7** | Worker fails to unlink `.claimed.<id>` permit | Marker `.consumed.<id>` written | Secret wiped | Warning marker `.unlink_failed.*` recorded; exit code 139 | **Audit warned:** Marker blocks replay; cleanup required (Check 46) |
| **S8** | Normal completed execution | Unlinked / absent; `.consumed.<id>` present | Secret wiped | Original path absent (code 120); consumed marker blocks issuance/claim (code 127) | **Strict zero replay** (Check 25, Check 28) |
| **S9** | Supervisor restarts after run completes | Any prior unconsumed permit on disk | New 32-byte secret generated in supervisor RAM | Prior permit signature does not match new secret (exit code 126) | **Strict at-most-once authorization** (Check 59) |

---

## 8. Empirical Test Suite Execution Results

All verification commands executed cleanly in the current development environment:

### 8.1 60-Check Permit Authorization Suite (`tools/test_permit_authorization.py`)
```powershell
python tools/test_permit_authorization.py
```
- **Exit Code:** `0`
- **Result:** **ALL 60 CHECKS PASSED** (0 failures)
- **Execution Time:** 15.2s
- **Checks Exercised:**
  - Checks 1–7: Canonical profile inventory and tampering rejection (codes 134, 135, 136, 137).
  - Checks 8–13: Exact ordered execution vector validation (code 133).
  - Checks 14–18: Timestamp, clock bounds, and TTL expiration (code 128).
  - Checks 19–22: Worker path, version, and binary SHA-256 hash (code 131).
  - Checks 23–28: Atomic claiming, duplicate contention, replay rejection, and receipt binding (codes 120, 127).
  - Checks 29–33: HMAC-SHA256 signature, secret transfer, corruption, and EOF (codes 120, 125, 126).
  - Checks 34–36: Preflight collection suppression, supervisor EEXIST concurrency, supervisor health check.
  - Checks 37–42: Snapshot receipt schema, identity, digest parity, and permit binding (code 124).
  - Checks 43–44: Pinned canonical profile inventory and count mismatch (codes 134, 135).
  - Checks 45–46: Injected consumption write failure (code 138) and unlink failure (code 139).
  - Checks 47–51: Actual disk file modification, addition, deletion, and rename rejection (code 124).
  - Check 52: Supervisor pre-launch bootstrap hash verification (code 131).
  - Check 53: Supervisor `FILE_SHARE_READ` kernel lock blocks concurrent modification.
  - Check 54: Independent bootstrap digest mismatch rejected with code 131.
  - Check 55: Unapproved file in fresh runtime directory rejected with code 124.
  - Check 56: Non-canonical permit filename (`.execution_permit.py`) rejected with code 124.
  - Check 57: Supervisor refuses permit combining candidate nodes with synthetic failure flags.
  - Check 58: Worker independently rejects synthetic failure flags in candidate mode with code 133.
  - Check 59: Supervisor restart with new ephemeral secret cannot authorize prior permit (code 126).
  - Check 60: Expired permit invalidated regardless of secret or snapshot validity (code 128).

### 8.2 18-Probe Snapshot Integrity Suite (`tools/test_snapshot_integrity.py`)
```powershell
python tools/test_snapshot_integrity.py
```
- **Exit Code:** `0`
- **Result:** **ALL 18 PROBES PASSED** (0 failures, 0.88s)
- **Probes Exercised:** Baseline copy, stable source equality, concurrent add/delete/modify/rename detection, metadata preservation, destination tampering quarantine, case collision, symlink/reparse audit, exclusion ledger audit, `.env.example` hash pinning, anti-circularity verification.

### 8.3 17-Probe Profile Isolation Suite (`tools/test_profile_isolation.py`)
```powershell
python tools/test_profile_isolation.py
```
- **Exit Code:** `0`
- **Result:** **ALL 17 PROBES PASSED** (0 failures, 2.37s)
- **Probes Exercised:** AppContainer child launch, Low Integrity token witness (RID `0x1000`), AppContainer SID verification, snapshot access, ACL resource boundary, launch failure fail-closed, B1 Ordinary Medium Integrity and non-elevation verification, B1 Snapshot CWD/import/temp confinement, B2 environment capability preflight, profile/token mismatch rejection, Job Object assignment failure cleanup, process-tree termination.

### 8.4 20-Check Drainer Remediation Suite (`tools/test_drainer_remediation.py`)
```powershell
python tools/test_drainer_remediation.py
```
- **Exit Code:** `0`
- **Result:** **ALL 20 CHECKS PASSED** (0 failures, 2.51s)
- **Checks Exercised:** 64-bit Win32 ABI structure sizes, overlapped connection, race condition handling, handle cleanup, `CancelIoEx` success/not-found/abort, real pending I/O unresolved cancellation, active process tracking, descendant termination, byte accounting, `ERROR_IO_INCOMPLETE (996)` handling, eventual completion retirement, 5 production supervisor lifecycle scenarios (A–E), 14-case handle ownership state machine.

### 8.5 Supervisor Preflight (`python tools/snapshot_test_supervisor.py --preflight`)
```powershell
python tools/snapshot_test_supervisor.py --preflight
```
- **Exit Code:** `0`
- **Gate 1/5 (Dispatcher Partition):** 507 cohort nodes (Context A: 69, Context B1 Ordinary: 420, Context B1 Snapshot: 17, Context B2 Interactive: 1).
- **Gate 2/5 (Snapshot Provisioning):** 1,665 source files audited, 1,665 destination files audited, 1:1 matched hashes, composite digest `b53a496d17bd5873f7e0501b4d6a28ce14b65a94d08eda17d7bd908406742e9f`, 0 reparse points, 5 fresh runtime dirs.
- **Gate 3/5 (Worker Bootstrap):** Exit code 0, 0.47s duration, CWD and temp confined to snapshot, win32api loaded cleanly.
- **Gate 4/5 (Permit Gate):** Missing permit rejected with exit code 120 (Fail-closed verified).
- **Gate 5/5 (Cleanup State Machine):** `CLEANUP_PERMITTED_AND_EXECUTED`.

### 8.6 Static Diagnostics & Linter
```powershell
python tools/lint.py check tools/snapshot_test_supervisor.py tools/test_worker_bootstrap.py tools/test_permit_authorization.py
python tools/diagnostics.py check tools/snapshot_test_supervisor.py tools/test_worker_bootstrap.py tools/test_permit_authorization.py
```
- **Exit Code:** `0`
- **Result:** Ruff Linter: PASSED (exit 0); AST-Grep Structural Rules: PASSED (exit 0).

---

## 9. Baseline Verification & Invariant Accounting

1. **Frozen Source Baseline:**
   - Source repository baseline remains frozen at exactly **1,665 files**.
   - Destination copy produces exactly 1,665 files with 1:1 SHA-256 match.
   - Exclusion ledger accounts for 2,128 excluded items (total 3,793 physical items in working tree).
2. **Zero Candidate Test Nodes Executed (0/507):**
   - Candidate test execution remains gated by `[GATE 2 BLOCKED]` fail-closed handler (exit code 200).
   - Exactly **0 candidate test nodes** were collected or executed during this audit.
   - All 60 permit authorization checks used synthetic authorization probes (`--synthetic-auth-probe`) or negative security injection.
3. **Repository Working Tree Integrity:**
   - Active branch: `feat/engineering-maturity-milestone` at HEAD `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`.
   - Tracked modified files: exactly **62 files** preserved.
   - No git mutations (`git add`, `git commit`, `git reset`, `git checkout`) were performed.
   - No application modules (`jarvis/`) or tests (`tests/`) were modified.
   - `docs/agent/LESSONS.md` was preserved completely untouched.

---

## 10. Conclusion & Recommendations

Phase 1S successfully closes the remaining audit and boundary questions raised in the Phase 1R review:
1. The **TOCTOU boundary** is formally characterized and documented: point-in-time disk hashing validates snapshot integrity at launch, while process containment (Job Objects, AppContainer) provides process-level isolation.
2. The **launch-time race window** is closed via Win32 `FILE_SHARE_READ` kernel locking across hash verification and process spawning.
3. The **canonical inventory and exclusion boundaries** are hardened via Step 9A emptiness assertions on fresh runtime directories and strict permit lifecycle regex filtering.
4. The **synthetic boundary** enforces complete mutual exclusion between candidate test nodes and synthetic failure flags in both supervisor and worker.
5. The **crash/replay lifecycle** guarantees strict at-most-once authorization across supervisor restarts via ephemeral in-memory HMAC secrets.

**Subsystem Status:**
- **BLK-21 (Execution Authorization):** **ACCEPTED & FULLY VERIFIED**
- **BLK-19 (Snapshot Subsystem):** **CONDITIONALLY ACCEPTED** (Pending user policy authorization on `data/learning_events`)
- **BLK-18 (Profile Isolation):** **PARTIALLY VERIFIED** (Retained pending dedicated interactive station for Context B2)
- **BLK-20 & BLK-22:** **ACCEPTED & FULLY VERIFIED**
- **Gate 2 (507 Candidate Nodes):** **STRICTLY UNAUTHORIZED** (Requires separate human authorization)

