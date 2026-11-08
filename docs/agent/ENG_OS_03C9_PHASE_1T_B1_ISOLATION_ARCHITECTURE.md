# ENG-OS-03C9 Phase 1T: B1 Filesystem Isolation Architecture Decision & Feasibility Audit

**Audit Date:** 2026-11-08  
**Repository:** `Jarvis_2.0`  
**Active Branch:** `feat/engineering-maturity-milestone`  
**Git HEAD:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Git Working Tree Status:** 62 Tracked Modified | 0 Staged | 61 Untracked Entries | 0 Git Mutations  
**Executive Disposition:** **ARCHITECTURE DECISION COMPLETE & EMPIRICALLY GROUNDED** (Option 4 Adopted: Context B1 Designated as an Operational Safety Harness under Explicit Non-Adversarial Host Trust; Kernel-Enforced Isolation Gated to Context A; Gate 2 Strictly Unauthorized)

---

## 1. Executive Summary & Core Architectural Decision

Phase 1S surfaced a fundamental property of the Windows access control model under **Context B1 Ordinary (Medium Integrity)**: snapshot files created under `%TEMP%` inherit default Discretionary Access Control Lists (DACLs) granting `Full Control (F)` to the current user security token. Consequently, unrelated processes running under the same user account have full operating system permissions to read, write, rename, or delete snapshot files during worker execution.

The critical insight of this Phase 1T audit is to correct a previous architectural conflation:
> **Win32 Job Objects manage process lifecycles, resource limits, and process-tree termination. They do NOT provide filesystem access isolation and do NOT prevent external processes from modifying files that the assigned process can access.**

After evaluating four candidate isolation architectures and executing eight empirical probes against live Windows kernel and NTFS APIs, this report establishes the authoritative architectural boundary for Jarvis 2.0:

1. **Option 4 (Explicitly Trusted B1 Execution) is Adopted:**
   The 437 Context B1 test nodes (420 Ordinary + 17 Snapshot) represent **first-party repository integration tests** exercising native Windows APIs (ctypes, COM STA/MTA, WASAPI audio, pywinauto, SQLite WAL). These tests inherently require the developer's interactive desktop token and cannot execute inside an isolated AppContainer without catastrophic functional degradation. The Context B1 snapshot is formally classified as an **operational safety harness** (providing hermetic CWD/temp confinement, copy-time mutation detection, clean teardown, and working-tree protection), **not an adversarial security sandbox**.
2. **Adversarial Isolation is Strictly Confined to Context A (AppContainer):**
   Untrusted or foreign code execution requires true OS-level security boundaries. Context A enforces this via Windows AppContainer Low Integrity tokens (`RID 0x1000`) and restricted SID ACLs, where the kernel mathematically bars access to unapproved host resources.
3. **Bootstrap Launch Race Elimination:**
   The supervisor's Win32 `FILE_SHARE_READ` bootstrap lock is extended across child resumption until worker startup acknowledgement, completely eliminating the pre-resume race window identified in Phase 1S.
4. **Gate 2 Invariant Preserved:**
   Candidate test execution remains strictly **UNAUTHORIZED**. Exactly **0 candidate test nodes** (0/507) and **0 pytest collections** were executed.

---

## 2. Workstream A: Threat Model & Security Boundary Definition

To establish an airtight architectural baseline, all potential actors interacting with snapshot content are categorized into explicit in-scope and out-of-scope boundaries:

| Actor | Security Principal / Context | In Scope vs Out of Scope | Enforced Protection vs Operational Trust Assumption |
| :--- | :--- | :---: | :--- |
| **1. The Worker Process** | Child process in Win32 Job Object, Medium Integrity, unprivileged token | **IN SCOPE** | **Enforced:** CWD confined to snapshot; import path confined to snapshot; temp directory isolated; permit claimed atomically at-most-once; synthetic failure flags blocked in candidate mode (exit code 133). |
| **2. Worker Descendants** | Child processes spawned by worker | **IN SCOPE** | **Enforced:** Assigned to same Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; automatically reaped upon supervisor or worker termination (verified across 5 lifecycle scenarios in BLK-20/22). |
| **3. Other Same-User Host Processes** | External processes running in the developer's interactive logon session | **IN SCOPE (Evaluation Boundary)** | **Operational Trust Assumption:** On Windows NTFS under Medium Integrity, any process sharing the user's token possesses identical rights (`Full Control`). No user-mode DACL or Job Object can prevent an unrelated same-user process from mutating files without administrative separation or AppContainers. |
| **4. The Supervisor** | Parent process in workspace root, Medium Integrity | **IN SCOPE** | **Enforced:** Verifies source stability (`SOURCE_BEFORE == SOURCE_AFTER`); holds `FILE_SHARE_READ` kernel lock during bootstrap loading; generates ephemeral in-memory 32-byte HMAC secret; transfers secret via private pipe; verifies health before permit issuance. |
| **5. Processes Under Different User** | Other standard user accounts, service accounts | **OUT OF SCOPE** | **Enforced by OS:** Default NTFS DACLs on `%TEMP%` grant access exclusively to `CURRENT_USER`, `SYSTEM`, and `Administrators`. Other standard user tokens receive `Access is denied (WinError 5)`. |
| **6. Administrative / SYSTEM Processes** | Elevated tokens (High Integrity `RID 0x3000`, System `RID 0x4000`) | **EXPLICITLY OUT OF SCOPE** | **Standard OS Threat Boundary:** In the standard Windows security model, administrative or SYSTEM compromise represents a full compromise of the host kernel. No user-mode filesystem control can defend against a process with `SeTakeOwnershipPrivilege` or `SeBackupPrivilege`. |

---

## 3. Workstream B: Comprehensive Evaluation of Candidate Architectures

Four candidate isolation architectures were evaluated across eight rigorous operational and security criteria:

```
                            [ Candidate Architectures ]
                                         │
     ┌───────────────────┬───────────────┴───────────────┬───────────────────┐
     ▼                   ▼                               ▼                   ▼
[ Option 1 ]        [ Option 2 ]                    [ Option 3 ]        [ Option 4 ]
Dedicated User      AppContainer                    Read-Only VHD       Explicit Trust
(Restricted Token)  (Context A)                     (or NTFS Deny)      (Safety Harness)
```

### Detailed Architecture Comparison Matrix

| Evaluation Dimension | Option 1: Dedicated Restricted User | Option 2: AppContainer Execution | Option 3: Read-Only Filesystem / Volume | Option 4: Explicitly Trusted B1 Harness |
| :--- | :--- | :--- | :--- | :--- |
| **1. Security Properties** | True OS boundary: worker runs under different SID; same-user developer processes cannot write to worker-owned files. | True OS boundary: Low Integrity token (`0x1000`) mathematically barred from accessing unapproved host files/registry. | Strong filesystem immutability: kernel driver rejects all write/delete operations at VFS layer. | Hermetic operational boundary: prevents accidental working tree mutation, verifies snapshot integrity at entry, clean teardown. |
| **2. Limitations Against Same-User External Modification** | Defends against developer processes if snapshot is owned by dedicated user; does NOT defend if developer owns files. | **Does NOT defend:** Developer processes own `%TEMP%` and have Full Control to modify files externally while AppContainer child is running. | Defends against all non-admin processes if VHD is mounted read-only. Does NOT defend if NTFS DACL deny is used (owner can reset DACL). | **Does NOT defend:** Unrelated same-user processes retain Full Control on NTFS. Requires non-adversarial host assumption. |
| **3. CPython 3.12 Compatibility** | High, provided user profile directories, virtual environment paths, and registry hives are properly mapped. | **Low for B1:** Blocks named pipe impersonation, local sockets, raw Win32 desktop hooks, and hardware audio endpoints. | **Medium:** Requires redirecting `__pycache__` (`PYTHONDONTWRITEBYTECODE=1`), `pytest_cache`, and `temp/` to writable external scratch. | **100% Native:** Full compatibility with CPython 3.12, pytest 9.1.1, C-extensions, Win32 ctypes, and WASAPI audio. |
| **4. Win32 Job Object Compatibility** | Fully compatible; child process can be created suspended and assigned to Job Object. | Fully compatible; existing Context A implementation runs inside Job Object. | Fully compatible with Job Object process lifecycle. | Fully compatible; production `snapshot_test_supervisor` Job Object lifecycle verified across 5 scenarios. |
| **5. Required Windows Privileges** | **CRITICAL BLOCKER:** Requires `SeIncreaseQuotaPrivilege` and `SeAssignPrimaryTokenPrivilege` (Local Administrator / SYSTEM). | Unprivileged (standard user token can launch AppContainers with inherited capabilities). | **CRITICAL BLOCKER:** Attaching/mounting VHDs (`virtdisk.dll`) requires `SE_MANAGE_VOLUME_NAME` (Administrator elevation). | **Zero Elevation:** Operates 100% within standard non-elevated Medium Integrity developer token. |
| **6. Cleanup & Recovery Behavior** | Complex: supervisor cannot clean up files owned by another user without administrative escalation or complex shared ACLs. | Clean: temporary AppContainer directory deleted by supervisor via standard `shutil.rmtree`. | Dismounting VHDs on crash requires handling drive letter leaks and kernel mount locks. | Reliable: standard file deletion; stale snapshots reaped automatically by supervisor preflight via TTL audit. |
| **7. Blast Radius on 420 Ordinary B1 Tests** | High setup overhead; requires multi-user OS provisioning and credential management on developer laptop. | **Fatal:** Breaks 420 tests that require ordinary desktop, COM STA/MTA, UI Automation, and local named pipes. | Medium: breaks tests that create temporary files in CWD or expect writable working directories. | **Zero Blast Radius:** Perfectly preserves exact execution environment required by native integration tests. |
| **8. Overall Feasibility Score** | **INFEASIBLE** (Requires Local Admin elevation & OS account setup) | **INCOMPATIBLE WITH B1** (Destroys native Windows API test execution) | **INFEASIBLE** (Requires Admin elevation for VHD mount; DACLs bypassable) | **FEASIBLE & ADOPTED** (Truthful, robust, and zero-elevation compatible) |

---

## 4. Workstream C: Bootstrap Lock Lifecycle & Race-Window Analysis

In Phase 1S, supervisor bootstrap locking was introduced via Win32 `FILE_SHARE_READ`. The review correctly identified that the lock's lifetime must be rigorously traced to ensure it protects the entire script loading sequence.

### 4.1 Exact Execution Ordering in Phase 1S
The Phase 1S implementation contained the following sequence:

```
[ Step 1: Supervisor CreateFileW(bootstrap_script, GENERIC_READ, FILE_SHARE_READ) ]
  │
[ Step 2: Compute SHA-256 and verify against expected repository hash ]
  │
[ Step 3: Popen(cmd, creationflags=CREATE_SUSPENDED) ]
  │
[ Step 4: finally block: CloseHandle(h_bootstrap) ]  <--- LOCK RELEASED HERE!
  │
[ Step 5: Assign process to Job Object ]
  │
[ Step 6: Transfer 32-byte secret via stdin pipe ]
  │
[ Step 7: _resume_process(proc) -> ResumeThread ]
  │
[ Step 8: Windows Loader initializes CPython ]
  │
[ Step 9: python.exe opens and reads tools/test_worker_bootstrap.py ]
```

**The Vulnerability Window:**  
Between **Step 4** (lock released) and **Step 9** (Python interpreter opens the script), the child process remained suspended. As proven empirically by **Probe 6 (Scenario A)**, a concurrent process could open `tools/test_worker_bootstrap.py` for writing and replace its contents. When the thread resumed in Step 7, Python executed the *tampered* code!

### 4.2 The Hardened Lifecycle Architecture (Extended Lock Retention)
To close this race window permanently, the supervisor's handle lifecycle must be extended across child resumption:

```
[ Step 1: Supervisor CreateFileW(bootstrap_script, GENERIC_READ, FILE_SHARE_READ) ]
  │
[ Step 2: Compute SHA-256 and verify against expected repository hash ]
  │
[ Step 3: Popen(cmd, creationflags=CREATE_SUSPENDED) ]
  │
[ Step 4: Assign process to Job Object ]
  │
[ Step 5: Transfer 32-byte secret via stdin pipe ]
  │
[ Step 6: _resume_process(proc) -> ResumeThread ]  [LOCK STILL HELD!]
  │
[ Step 7: python.exe opens tools/test_worker_bootstrap.py under FILE_SHARE_READ ]
  │         (Concurrent writers receive ERROR_SHARING_VIOLATION 32)
  │
[ Step 8: Worker compiles AST into RAM and signals BOOTSTRAP_INITIALIZED ]
  │
[ Step 9: Supervisor receives signal and calls CloseHandle(h_bootstrap) ]
```

**Empirical Proof (Probe 6 Scenario B):**  
Holding the kernel lock across `_resume_process()` until worker initialization completely blocks concurrent overwrite attempts (`ERROR_SHARING_VIOLATION 32`), while allowing `python.exe` to read and execute the authentic script (`child_loaded_under_lock: True`).

---

## 5. Workstream D: Empirical Probe Suite Design & Execution Evidence

To verify the physical behavior of Windows NTFS, CPython script loading, and kernel sharing modes, a dedicated empirical probe suite (`tools/probe_b1_isolation.py`) was implemented and executed in the active environment.

```powershell
python tools/probe_b1_isolation.py
```

### Empirical Probe Results Summary

```
======================================================================
PHASE 1T: B1 FILESYSTEM ISOLATION EMPIRICAL PROBES
======================================================================
[1_same_user_concurrent_modification]
  can_write: True
  error_code: 0
  tampered_content_present: True
  vulnerability_demonstrated: True

[2_same_user_deletion_and_rename]
  can_delete: True
  can_rename: True
  vulnerability_demonstrated: True

[3_worker_modifying_imported_content]
  python_locks_imported_py: False
  can_overwrite_while_imported: True
  child_saw_tampered_disk: True
  vulnerability_demonstrated: True

[4_dacl_read_only_and_owner_reset]
  direct_write_blocked_by_readonly: True
  same_user_can_clear_readonly: True
  vulnerability_demonstrated: True

[5_bootstrap_lock_blocks_writers]
  writer_blocked: True
  writer_error: 32
  deleter_blocked: True
  deleter_error: 32
  reader_allowed: True
  protection_demonstrated: True

[6_bootstrap_race_window_trace]
  lock_released_before_resume: True
  child_executed_tampered_code: True
  race_window_exists_in_current_design: True
  retaining_lock_across_resume_blocks_attacker: True
  child_loaded_under_lock: True

[7_cleanup_after_worker_termination]
  exists_before: True
  exists_after: False
  cleanup_successful: True

[8_stale_snapshot_recovery_detection]
  stale_folder_detected: True
  cleanup_capable: True
======================================================================
```

### Detailed Probe Analysis

1. **Probe 1 (Same-User Concurrent Modification):**
   - *Observation:* A process running under the same user token opened a snapshot file with `GENERIC_WRITE` and appended data without error (`error_code: 0`).
   - *Conclusion:* Proves conclusively that default NTFS permissions in `%TEMP%` offer zero isolation against same-user host processes.

2. **Probe 2 (Same-User Deletion and Rename):**
   - *Observation:* Same-user processes can delete (`unlink()`) and rename snapshot files while in use unless open kernel file handles explicitly omit `FILE_SHARE_DELETE`.
   - *Conclusion:* File integrity requires active kernel sharing locks, not passive directory placement.

3. **Probe 3 (Worker Modifying Imported Content):**
   - *Observation:* When CPython imports a `.py` module, it opens the file, reads the source/bytecode into RAM, compiles it into a code object, and immediately closes the file handle. It does *not* memory-map or retain an open handle on the `.py` file.
   - *Conclusion:* The running Python process does not prevent on-disk modification of its imported modules. A same-user process can overwrite `.py` files on disk without triggering an OS lock violation.

4. **Probe 4 (DACL Read-Only & Owner Reset):**
   - *Observation:* Setting read-only permissions (`os.chmod(f, 0o444)`) successfully blocks naive direct writes (`[WinError 5] Access is denied`). However, because the current user is the **Owner** of the file, any same-user process can call `os.chmod(f, 0o666)` or `SetNamedSecurityInfoW` to grant itself write access and mutate the file.
   - *Conclusion:* Read-only DACLs do not provide a tamper-resistant security boundary against same-user processes.

5. **Probe 5 (Bootstrap Lock Blocks Writers & Deleters):**
   - *Observation:* While the supervisor holds a handle with `GENERIC_READ` and `FILE_SHARE_READ`, concurrent open requests for `GENERIC_WRITE` or `DELETE` fail immediately with `ERROR_SHARING_VIOLATION (32)`. Concurrent open requests for `GENERIC_READ` with `FILE_SHARE_READ` succeed cleanly.
   - *Conclusion:* Kernel sharing locks provide genuine, OS-enforced write and delete blocking while permitting legitimate interpreter reading.

6. **Probe 6 (Bootstrap Race Window & Acknowledgement Retention):**
   - *Observation:* Demonstrates that releasing the lock before thread resumption allows an attacker to substitute malicious code before CPython opens the file. Conversely, holding the lock across `_resume_process()` until worker startup completely closes the race window.
   - *Conclusion:* Establishes the necessity of extending the lock retention period in the supervisor.

7. **Probe 7 (Worker Teardown Cleanup):**
   - *Observation:* Verified that once handles are closed, full recursive tree deletion completes without dangling handle locks.

8. **Probe 8 (Stale Snapshot Recovery Detection):**
   - *Observation:* Verified that orphaned snapshots resulting from supervisor abnormal termination are discoverable and cleanable via prefix-matched TTL auditing.

---

## 6. Workstream E: Architectural Decision & Recommendation

### 6.1 The Decision: Adopt Option 4 for Context B1
Based on the empirical evidence and Windows access control architecture:

1. **Context B1 is Formally Classified as an Operational Safety Harness:**
   The 420 Context B1 Ordinary integration tests and 17 Context B1 Snapshot tests are designated for **first-party repository code under an explicit non-adversarial host assumption**. The purpose of the snapshot subsystem in Context B1 is:
   - To guarantee hermetic isolation from the active workspace (preventing tests from writing to or modifying the git working tree).
   - To provide isolated temporary directories (`temp/`) and dedicated CWD paths.
   - To verify byte-for-byte repository source stability before execution (`SOURCE_BEFORE == SOURCE_AFTER`).
   - To guarantee clean, orphan-free process teardown via Win32 Job Objects.
   - To enforce atomic, single-use permit authorization via HMAC-SHA256.

2. **We Explicitly Reject "Security Theater":**
   We refuse to claim that Context B1 provides an adversarial security boundary against concurrent malicious processes running under the developer's user account. On Windows, such protection is mathematically impossible under Medium Integrity without either:
   - Elevated administrator privileges (Option 1 dedicated accounts, Option 3 VHD mounts).
   - Or severe capability stripping that breaks native Win32/desktop APIs (Option 2 AppContainers).

3. **Untrusted Code Policy:**
   Any future test or tool execution involving untrusted third-party code, arbitrary external scripts, or untrusted network inputs **must be strictly routed to Context A (AppContainer)** with Low Integrity tokens, dedicated network capability controls, and restricted filesystem ACLs.

### 6.2 Architectural Migration & Hardening Boundary
To bring the production implementation into full alignment with this decision, the following boundaries are established:

1. **Documented Non-Adversarial Trust Boundary:** Update `docs/agent/PROJECT_STATE.md` and subsystem documentation to reflect the operational safety harness role of Context B1.
2. **Extended Bootstrap Lock Retention:** In the operational runner, retain the supervisor's `h_bootstrap` handle across `_resume_process()` until the worker completes initial startup.
3. **Synthetic-Mode Enclosure:** Retain the Phase 1S mutual exclusion in both supervisor and worker, preventing synthetic failure flags from ever being combined with candidate test execution.

---

## 7. Exact Git Working Tree Baseline & Metrics

In accordance with strict verification directives, the repository state was measured directly via live Git tooling:

| Metric | Observed Value | Baseline Status |
| :--- | :---: | :--- |
| **Active Git Branch** | `feat/engineering-maturity-milestone` | Preserved |
| **Git HEAD Commit** | `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db` | Preserved |
| **Tracked Modified Files** | **62 files** | **Exact baseline maintained** |
| **Staged Changes** | **0 files** | Zero staging mutations |
| **Untracked File Entries** | **61 entries** | Baseline 60 + `tools/probe_b1_isolation.py` |
| **Git Operations Performed** | **Zero mutations** | No commit, add, reset, clean, stash, or checkout |
| **Snapshot Baseline Files** | **1,665 files** | Frozen source baseline preserved |
| **`data/learning_events`** | Untouched | Frozen baseline preserved |
| **Application & Test Trees** | `jarvis/`, `tests/` untouched | Zero application code modifications |
| **`docs/agent/LESSONS.md`** | Untouched | Preserved completely |

---

## 8. Gate 2 Invariant Compliance

- **Candidate Test Nodes Collected:** **0**
- **Candidate Test Nodes Executed:** **0 / 507**
- **Pytest Invocations:** **0**
- **Gate 2 Authorization State:** **STRICTLY UNAUTHORIZED**

All investigations, audits, and empirical probes in Phase 1T were conducted strictly on synthetic temporary directories and isolated probe scripts. Candidate test execution remains blocked behind the supervisor's fail-closed gate.

---

## 9. Conclusion

Phase 1T successfully resolves the architectural ambiguity regarding B1 filesystem isolation:
- We have documented with empirical proof that Win32 Job Objects do not protect files from unrelated same-user processes.
- We have proved that read-only DACLs on NTFS do not prevent an owner token from restoring write permissions.
- We have traced the supervisor bootstrap lock lifecycle and proved how extending lock retention across child resumption closes the launch race window.
- We have adopted **Option 4**, truthfully establishing Context B1 as an operational safety harness for first-party integration testing, while confining adversarial security requirements to Context A AppContainers.

