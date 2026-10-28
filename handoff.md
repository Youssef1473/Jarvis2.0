# Session Handoff: Jarvis 2.0 — Option C Windows AppContainer Isolation & Capability Sandbox

**Date:** 2026-10-28  
**Host Environment:** Windows 11 x64 | Intel Core i5-12500H | NVIDIA RTX 3050 Laptop GPU (4GB VRAM) | Python 3.12.10  
**Protected Base on `main`:** Commit [`88b491c`](https://github.com/Youssef1473/Jarvis2.0/commit/88b491c) (Merge pull request #4: Option B code quality modernization and exception hardening)  
**Active Feature Branch:** `feat/appcontainer-capability-sandbox`  
**Target Pull Request:** Option C — Windows AppContainer Isolation and Capability Sandbox against `main`  
**Verification Baseline:**
- `tests/test_appcontainer_sandbox.py`: **12 passed in 1.50s (Exit code: 0)**
- `tests/test_job_object_sandbox.py`: **6 passed in 2.31s (Exit code: 0)**
- `tests/test_job_object_sandbox_hardening.py`: **32 passed, 13 skipped in 7.70s (Exit code: 0)**
- `tests/test_self_evolution_sandbox.py`: **35 passed in 11.61s (Exit code: 0)**
- `tests/test_p0_action_bound_authorization.py`: **35 passed in 1.38s (Exit code: 0)**
- `tests/test_security_boundaries.py`: **94 passed in 2.82s (Exit code: 0)**
- `tests/test_exception_hardening.py`: **9 passed in 0.30s (Exit code: 0)**
- Static diagnostics (`UP,C4,F841,I,RUF022` across `jarvis/`): **100% clean (0 errors)**
- AST invariant scanner (`tools/diagnostics.py scan jarvis tools`): **Clean (0 errors)**

---

## 1. Architectural Summary & Deliverables

Option C introduces production-grade, kernel-enforced Windows AppContainer capability sandboxing into Jarvis 2.0 as an opt-in, defense-in-depth isolation boundary around untrusted child processes and self-synthesized custom skills.

### Core Deliverables

1. **Win32 AppContainer Subsystem (`jarvis/core/evolution/appcontainer.py`):**
   - Native 64-bit Win32 bindings for `userenv.dll`, `advapi32.dll`, and `kernel32.dll` with explicit prototypes (`CreateAppContainerProfile`, `DeriveAppContainerSidFromAppContainerName`, `DeleteAppContainerProfile`, `ConvertSidToStringSidW`, `ConvertStringSidToSidW`, `FreeSid`, `InitializeProcThreadAttributeList`, `UpdateProcThreadAttribute`, `DeleteProcThreadAttributeList`).
   - `AppContainerConfig`: dataclass configuring profile names (bounded to <= 45 chars to prevent `E_INVALIDARG 0x80070057`), display name, description, explicit capability lists, grant paths, and auto-cleanup.
   - `AppContainerProfileManager`: manages profile creation, derivation of existing profiles on collision, profile deletion, and scoped directory ACL management via `icacls`.
   - `AppContainerProcessLauncher`: sets up `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` and `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, builds sorted wide-character environment blocks with `CREATE_UNICODE_ENVIRONMENT` and `%LOCALAPPDATA%` virtualized package resolution, and spawns processes suspended (`CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW`).
   - `AppContainerProcessWrapper`: Popen-compatible interface implementing `poll()`, `wait()`, `communicate()`, `terminate()`, `kill()`, and `close()`.
   - `create_pipe_pair()`: creates anonymous Win32 pipe pairs with explicit inheritance flags (`HANDLE_FLAG_INHERIT`).

2. **Canonical Sandbox Integration (`jarvis/skills/job_object_sandbox.py`):**
   - Added `use_appcontainer: bool = False` and `appcontainer_config: Any = None` to `SandboxedProcess` and `run_sandboxed_batch`.
   - Strict `CREATE_SUSPENDED` ordering: AppContainer child process is spawned suspended, assigned to the Win32 Job Object (with memory limits, CPU affinity, priority, and `KILL_ON_JOB_CLOSE`), and resumed via `ResumeThread` only after successful assignment.
   - Fail-closed guarantee: any failure in container creation, Job Object creation, or process assignment immediately kills the suspended child (`TerminateProcess`), releases handles and SIDs, and raises `RuntimeError` (never falls back to uncontained execution).
   - Safe handle restriction: inherits strictly piped standard I/O handles (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`), closing child pipe ends in the parent immediately after spawn.

3. **Self-Evolution Custom Skill Sandbox (`jarvis/core/evolution/sandbox.py`):**
   - Added `use_appcontainer: bool = False` parameter to `SkillSandbox.execute_sandboxed_custom_skill`.
   - Ephemeral scratch staging: stages `isolated_runner.py` and the target custom skill script directly into the dedicated AppContainer scratch directory (`run_scratch`), granting ACLs only to `run_scratch` and keeping the host codebase untouched.
   - Automatic teardown: releases the AppContainer profile and deletes the ephemeral scratch folder on completion.

4. **Comprehensive Regression Test Suite (`tests/test_appcontainer_sandbox.py`):**
   - 12 automated unit and integration tests covering least-privilege defaults, capability SID resolution, profile reuse, fail-closed security, attribute list lifecycle, non-Windows rejection, legacy sandbox preservation, dedicated directory ACL isolation, live Windows NT kernel containment verification, timeout termination, piped standard I/O communication, and dynamic custom skill execution.

---

## 2. Security Invariants Verified

| Invariant | Implementation Mechanism | Observable Evidence |
| :--- | :--- | :--- |
| **Least Privilege Defaults** | `AppContainerConfig.capabilities` defaults to `[]` | Zero network, zero device, zero credential access by default. Tested in `test_appcontainer_config_least_privilege_defaults`. |
| **Zero Spawn-Before-Assign Window** | Process created with `CREATE_SUSPENDED`, assigned to Job Object, resumed via `ResumeThread` | Proves child cannot spawn grandchildren outside the Job Object. |
| **Fail-Closed Security Gate** | Profile or Job Object assignment errors terminate child via `TerminateProcess` and raise `RuntimeError` | Tested via mock injection in `test_fail_closed_on_profile_failure` and `test_fail_closed_on_job_assignment_failure`. |
| **Non-Inheriting Parent Isolation** | `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` restricts handle inheritance strictly to stdio pipes | Parent process handles are not inherited by untrusted child. |
| **NT Kernel Containment** | AppContainer SID denies access to all ungranted filesystem locations | Tested in `test_live_windows_containment_denied_access`: write to granted scratch succeeds; write to user home fails with kernel `PermissionError`. |
| **Profile Name Bounding** | Automatically bounded to <= 45 characters with SHA256 suffix | Prevents Windows `0x80070057` (`E_INVALIDARG`) on long profile names. |
| **Unicode Environment Resolution** | `CREATE_UNICODE_ENVIRONMENT` + case-insensitive sorted entries + `LOCALAPPDATA` | Enables AppContainer process loader to initialize virtual package state without `WinError 203`. |

---

## 3. Working Tree Inventory

### `git status --short`
```text
 M handoff.md
 M jarvis/core/evolution/sandbox.py
 M jarvis/skills/job_object_sandbox.py
?? jarvis/core/evolution/appcontainer.py
?? tests/test_appcontainer_sandbox.py
```

### `git diff --stat`
```text
 jarvis/core/evolution/sandbox.py    |  47 ++++++++---
 jarvis/skills/job_object_sandbox.py | 158 +++++++++++++++++++++++++++++++++++-
 2 files changed, 193 insertions(+), 12 deletions(-)
```

---

## 4. Instructions for PR Submission

To push and open PR #5 against `main` (do NOT merge or touch `main`):

```powershell
git add jarvis/core/evolution/appcontainer.py jarvis/core/evolution/sandbox.py jarvis/skills/job_object_sandbox.py tests/test_appcontainer_sandbox.py handoff.md
git commit -m "feat(security): Option C Windows AppContainer isolation and capability sandbox"
git push origin feat/appcontainer-capability-sandbox
gh pr create --base main --head feat/appcontainer-capability-sandbox --title "feat(security): Option C Windows AppContainer Isolation and Capability Sandbox" --body "## Summary`n`nImplements Option C: Windows AppContainer capability isolation and sandboxing as an explicitly selectable layer around Jarvis 2.0's Win32 Job Object sandbox.`n`n### Key Capabilities`n- Least-privilege defaults: empty capability set (no network, devices, or credentials).`n- CREATE_SUSPENDED spawn with Win32 Job Object assignment before resumption.`n- Scoped directory ACLs via AppContainer SID without touching parent directories.`n- Handle list restrictions via PROC_THREAD_ATTRIBUTE_HANDLE_LIST.`n- Fail-closed policy on profile or assignment failure.`n- Live kernel containment verified: ungranted user paths blocked with PermissionError.`n`n### Verification`n- 12 new targeted tests in tests/test_appcontainer_sandbox.py passing (100%).`n- Full fast test suite passing.`n- 0 lint or AST invariant violations."
```
