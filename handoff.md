# Session Handoff: Jarvis 2.0 — PR #5 AppContainer Security Review Remediation

**Date:** 2026-10-28  
**Host Environment:** Windows 11 x64 | Intel Core i5-12500H | NVIDIA RTX 3050 Laptop GPU (4GB VRAM) | Python 3.12.10  
**Protected Base on `main`:** Commit [`88b491c`](https://github.com/Youssef1473/Jarvis2.0/commit/88b491c) (Merge pull request #4: Option B code quality modernization and exception hardening)  
**Active Feature Branch:** `feat/appcontainer-capability-sandbox`  
**Target Pull Request:** [PR #5 — Windows AppContainer Isolation and Capability Sandbox](https://github.com/Youssef1473/Jarvis2.0/pull/5)  
**Remediation Commit:** Prepared for push to `feat/appcontainer-capability-sandbox` (do NOT merge PR #5, do NOT touch `main`)  

---

## 1. Remediation Summary (PR #5 Review Findings)

All 7 security and architectural review items have been resolved and empirically verified with new regression tests:

1. **Explicit Grants Only (Fix 1):**
   - Removed implicit automatic appending of arbitrary `cwd` to `config.grant_paths` in `AppContainerProcessLauncher.spawn_suspended_appcontainer`.
   - Access grants are restricted strictly to explicitly approved directories (such as the ephemeral per-run scratch directory), ensuring directory access cannot silently expand to the repository or user profile.
   - Verified via `test_cwd_outside_approved_grant_list_never_receives_acl_grant`.

2. **Strict Kernel Denial Assertion (Fix 2):**
   - Hardened `test_live_windows_containment_denied_access` in `tests/test_appcontainer_sandbox.py`.
   - Removed generic `except Exception:` catcher. Strictly checks `PermissionError` (with `winerror in (5, None)` or `errno == 13`) or `OSError` with `winerror == 5`. Any unexpected error aborts with non-zero exit code.
   - Proves that allowed scratch writes succeed, forbidden user home writes fail with kernel access denial, and forbidden files never exist on host.

3. **Attribute-List Lifetime and RAII Cleanup (Fix 3):**
   - Kept `SECURITY_CAPABILITIES`, `cap_structs`, and `handle_array` alive in scope until `DeleteProcThreadAttributeList` is called.
   - Attribute list cleanup is executed immediately inside a `finally:` block in `spawn_suspended_appcontainer` following `CreateProcessW` return (on both success and failure paths), completely eliminating double cleanup in `AppContainerProcessHandle`.
   - Verified via `test_attribute_list_backing_lifetimes_and_single_cleanup`.

4. **All Standard Handles Valid Under `STARTF_USESTDHANDLES` (Fix 4):**
   - Enforced Windows NT invariant: when `STARTF_USESTDHANDLES` is active, all three handles (`hStdInput`, `hStdOutput`, `hStdError`) must be valid.
   - Unredirected or `DEVNULL` streams are populated with inheritable Win32 `\\.\NUL` device handles opened via `CreateFileW(r"\\.\NUL", ...)`.
   - Parent closes its end of `\\.\NUL` handles immediately after `CreateProcessW` in `finally:`, and child inherits valid handles.
   - Verified via `test_all_standard_handles_valid_with_stdin_only_pipe_and_devnull`.

5. **Scoped Runtime Access & Preflight Validation (Fix 5):**
   - Implemented `AppContainerProfileManager.has_runtime_access()` and `ensure_runtime_access()` to grant minimal read-only permissions `(OI)(CI)(RX)` scoped strictly to the AppContainer SID (`*sid_str`), tracking grants and revoking them during cleanup.
   - Added `preflight_runtime_check() -> tuple[bool, str]` validating end-to-end profile creation, SID resolution, and deletion.
   - Cleaned up any stale developer ACEs from Python runtime paths.
   - Verified via `test_runtime_access_scoped_grant_and_cleanup`.

6. **Native Pipe Lifecycle & Deadlock Prevention (Fix 6):**
   - In `AppContainerProcessWrapper.communicate()`: closes `_stdin_write` when `input=None` to ensure EOF is immediately signaled to the child process.
   - In `communicate(input=...)`: writes stdin in a dedicated background thread `t_in` in chunks up to 64KB, completely eliminating pipe buffer deadlocks with large inputs.
   - In `wait()`: raises `RuntimeError` on `WAIT_FAILED` with `ctypes.get_last_error()`, and raises `RuntimeError` if process wait finishes with no returncode.
   - Verified via `test_communicate_input_none_sends_eof_and_large_input_no_deadlock`.

7. **Atomic Setup Rollback & Profile Ownership (Fix 7):**
   - `get_or_create_profile` returns `tuple[int, str, bool]` with `is_new_profile` (`True` if freshly created, `False` on reuse collision `0x800700B7`).
   - If any path grant fails during setup, all previously applied grants in that run are revoked, the SID is freed, and newly created profiles are cleaned up.
   - `AppContainerProcessHandle.cleanup()` only deletes profiles if `self.config.auto_cleanup_profile and self.is_new_profile`, preserving shared or reused profiles.
   - Defensively handled `mock.patch("subprocess.Popen")` in `SandboxedProcess.terminate_tree` via `type(self.proc).__name__ == "AppContainerProcessWrapper"`.
   - Verified via `test_acl_grant_failure_rollback_previous_grants` and `test_existing_profile_reuse_does_not_delete_shared_profile`.

---

## 2. Empirical Verification Results

| Suite / Check | Command | Result |
| :--- | :--- | :--- |
| **AppContainer Test Suite** | `python tools/run_fast_tests.py tests/test_appcontainer_sandbox.py` | **19 passed in 31.44s (Exit code: 0)** |
| **Job Object Sandbox Suite** | `python tools/run_fast_tests.py tests/test_job_object_sandbox.py` | **6 passed in 2.62s (Exit code: 0)** |
| **Self-Evolution Sandbox Suite** | `python tools/run_fast_tests.py tests/test_self_evolution_sandbox.py` | **35 passed in 13.18s (Exit code: 0)** |
| **Process Containment Suite** | `python tools/run_fast_tests.py tests/test_oneshot_process_containment.py` | **23 passed in 0.85s (Exit code: 0)** |
| **Multi-LLM Responsiveness Suite** | `python tools/run_fast_tests.py tests/test_multi_llm_responsiveness.py` | **15 passed in 2.23s (Exit code: 0)** |
| **Security Boundaries Suite** | `python tools/run_fast_tests.py tests/test_security_boundaries.py` | **94 passed in 4.02s (Exit code: 0)** |
| **Lint Rules (UP, C4, F841, I, RUF022)** | `python tools/lint.py check --select UP,C4,F841,I,RUF022 jarvis/` | **All checks passed (Exit code: 0)** |
| **AST Invariant Scanner** | `python tools/diagnostics.py scan jarvis tools` | **Clean (Exit code: 0)** |
| **Git Diff Whitespace Check** | `git diff --check` | **Clean (Exit code: 0)** |

---

## 3. Working Tree Inventory

### `git status --short`
```text
 M handoff.md
 M jarvis/core/evolution/appcontainer.py
 M jarvis/skills/job_object_sandbox.py
 M tests/test_appcontainer_sandbox.py
```

### `git diff --stat`
```text
 handoff.md                            |  94 +++---
 jarvis/core/evolution/appcontainer.py | 496 ++++++++++++++++++++++++----------
 jarvis/skills/job_object_sandbox.py   |   2 +-
 tests/test_appcontainer_sandbox.py    | 198 +++++++++++++-
 4 files changed, 576 insertions(+), 202 deletions(-)
```

---

## 4. Next Step: Push to Remote Feature Branch

To push the remediation commit to the open PR branch without touching `main`:

```powershell
git add jarvis/core/evolution/appcontainer.py jarvis/skills/job_object_sandbox.py tests/test_appcontainer_sandbox.py handoff.md
git commit -m "fix(security): remediate PR #5 AppContainer isolation review findings"
git push origin feat/appcontainer-capability-sandbox
```
