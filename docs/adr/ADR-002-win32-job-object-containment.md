# ADR-002: Win32 Job Object Process Containment and Orphan Reaping

## Status
PROPOSED

## Date
2026-11-02

## Scope
Subprocess management, developer terminal command execution (`dev_shell.py`), background task workers (`task_manager.py`), Antigravity sidecar processes, and tool sandboxing on Windows.

---

## Context and Problem Statement
When Jarvis spawns command-line processes (PowerShell scripts, Python tools, compilers, language servers), those processes frequently spawn descendant background processes (grandchildren). On Windows, terminating a parent process via standard Python APIs (`subprocess.Popen.terminate()` or `os.kill()`) terminates only the immediate child, leaving grandchildren orphaned in the background. These orphaned processes accumulate silently, holding open network sockets, locking files, and exhausting CPU/RAM.

Furthermore, post-spawn process tree enumeration (e.g. via `psutil` or `taskkill /T /F`) suffers from time-of-check-to-time-of-use (TOCTOU) race conditions and PID recycling vulnerabilities.

---

## Decision
Enforce kernel-level process containment using native Win32 Job Objects configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.

1. **Suspended Process Creation:** All sandboxed commands are spawned suspended using the Win32 `CREATE_SUSPENDED` (`0x00000004`) process creation flag via `_spawn_suspended()` in [`job_object_sandbox.py`](../../jarvis/skills/job_object_sandbox.py).
2. **Job Object Configuration:** A dedicated Win32 Job Object is created via `CreateJobObjectW` and configured via `SetInformationJobObject` with `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`:
   - `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (`0x00002000`): Instructs the Windows NT kernel to forcibly terminate all processes currently associated with the Job when the last handle to the Job Object is closed.
   - `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` (`0x00000400`): Forces clean termination upon uncaught fatal errors, suppressing interactive Windows Error Reporting dialogs.
3. **Atomic Assignment Before Execution:** The suspended process is assigned to the Job Object via `AssignProcessToJobObject` while its primary thread is still suspended.
4. **Fail-Closed Guarantee:** If `AssignProcessToJobObject` fails for any reason (e.g. system permissions, nested job restrictions):
   - The suspended process is immediately killed via `TerminateProcess` without ever executing a single instruction.
   - A `RuntimeError("Failed to assign child process to Job Object")` is raised.
   - The child process is never resumed.
5. **Thread Resumption:** Only after verified assignment to the Job Object is the process's primary thread resumed via `ResumeThread`.
6. **Bounded Teardown & Drain:** On task completion, cancellation, or timeout, [`JobHandleHolder.drain()`](../../jarvis/skills/job_object_sandbox.py) polls active process count (`QueryInformationJobObject`), executes `_terminate_and_drain()`, and closes the Job Object handle.

---

## Alternatives Considered
- **`subprocess.Popen` + `taskkill /F /T`:** Rejected due to non-deterministic process termination, race conditions with fast-exiting intermediate children, and PID reuse collision risks.
- **`psutil.Process.children(recursive=True)` Enumeration:** Rejected because descendant processes detached before enumeration complete escape cleanup.
- *(Historical alternatives from initial pre-M-01 architecture: Historical rationale unavailable).*

---

## Consequences and Trade-Offs
- **Positive:**
  - Guaranteed kernel-level termination of entire process hierarchies (children, grandchildren, great-grandchildren).
  - Complete elimination of orphaned background processes, leaked listening ports, and dangling file locks.
  - Fail-closed security boundary: untrusted commands cannot escape containment by executing before Job assignment.
- **Negative / Costs:**
  - Windows-specific implementation relying on Win32 C-types API calls (`kernel32.dll`).
  - Cross-platform CI pipelines (e.g. Linux GitHub Actions runners) cannot execute real Win32 Job Object calls and require mock or fallback harnesses.

---

## Binding Invariants
- No sandboxed command may execute instructions before verified assignment to a Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- If Job Object assignment fails, the suspended process must be killed immediately (`TerminateProcess`) and fail closed.
- Job Object handles must be explicitly drained and closed during teardown.

---

## Implementation References
- [`job_object_sandbox.py:270`](../../jarvis/skills/job_object_sandbox.py#L270) (`_spawn_suspended`)
- [`job_object_sandbox.py:315`](../../jarvis/skills/job_object_sandbox.py#L315) (`_resume_process`)
- [`job_object_sandbox.py:465`](../../jarvis/skills/job_object_sandbox.py#L465) (`SandboxedProcess` lifecycle)
- [`job_object_sandbox.py:700`](../../jarvis/skills/job_object_sandbox.py#L700) (`spawn_sandboxed_process`)

---

## Production Call Sites
- `jarvis/skills/dev_shell.py` (Developer shell and PowerShell command execution)
- `jarvis/core/task_manager.py` (Background command tasks)
- `jarvis/core/antigravity_bridge.py` (Antigravity daemon sidecar process containment)
- `jarvis/core/pipeline/tool_orchestrator.py` (Tool actuation dispatch)

---

## Verification Evidence
- [`test_job_object_sandbox_hardening.py`](../../tests/test_job_object_sandbox_hardening.py):
  - **Test Collection Total:** 45 tests collected.
  - **Live Windows Host Execution (2026-11-02):** **32 passed, 13 skipped in 7.52s**.
  - **Exact Categorical Test Breakdown:**
    1. *Native Windows Kernel Process Tree & Orphan Reaping Tests (7 tests in `TestJobSandboxRealWindows`):* Pure unmocked Windows kernel execution verifying that terminating a job or timing out reaps detached grandchild processes, orphan processes after parent exit, PowerShell child trees, cooperative cancellation tokens, stdout-holding grandchildren, concurrent independent jobs, and background task grandchild trees (`test_timeout_kills_grandchild_and_drains`, `test_orphan_after_exit_is_reaped`, `test_execute_powershell_timeout_leaves_no_children`, `test_cancellation_kills_child_and_grandchild`, `test_normal_completion_with_pipe_holding_grandchild`, `test_concurrent_independent_jobs`, `test_background_task_cancellation_kills_grandchild`).
    2. *Real Windows Process Execution with Injected API Failures (4 tests in `TestJobSandboxRealWindows`):* Real Windows processes spawned suspended or executed with injected API failures to verify fail-closed cleanup, ensuring child PIDs are killed if `AssignProcessToJobObject` fails, background task job creation fails, or `kernel32` is unavailable (`test_failed_assignment_kills_child_before_running`, `test_background_task_failed_assignment_kills_child_fail_closed`, `test_background_task_failed_job_creation_kills_child_fail_closed`, `test_job_object_unavailable_fallback_in_dev_shell`).
    3. *Pre-Spawn Short-Circuit Test (1 test in `TestJobSandboxRealWindows`):* Verifies that a pre-cancelled cancellation token aborts before `_spawn_suspended` without launching a child (`test_pre_cancelled_token_never_spawns`).
    4. *Live Windows Server Integration Tests (7 tests in `TestJobSandboxRealWindows`):* Tests Uvicorn server access log redaction, query parameter sanitation, and timing on live Windows loopback sockets (`test_access_log_redaction_*` and `test_actual_uvicorn_access_log_output_captures_and_redacts`). These tests validate server middleware rather than Job Object process containment, but share this test class because they require live Windows OS network sockets.
    5. *Tool Routing and Containment Policy Tests (13 tests in `TestExecutePowershellRouting` and `TestRunProjectTestsContainment`):* Unit tests verifying command routing, argument validation, error propagation, and containment schema gates.
    6. *Mocked Fake-Kernel POSIX Tests (13 tests in `TestJobSandboxContainment`):* Decorated with `@unittest.skipUnless(LINUX, "fake-kernel tests drive real POSIX process groups")`. These tests utilize a `FakeKernel32` mock fixture specifically for Linux CI runners and are dynamically skipped on native Windows.
- [`test_milestone1_security_hardening.py`](../../tests/test_milestone1_security_hardening.py) (16 of 16 tests passed).

---

## Known Limitations and Unresolved Questions
- Windows restricts processes from being assigned to multiple Job Objects if the parent job prohibits breakaway (`JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`).
- Process containment via Job Objects provides lifecycle and resource termination; it does not provide filesystem virtualization or capability isolation (which requires AppContainer).

---

## Related ADRs
- None.
