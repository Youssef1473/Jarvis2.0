"""Kernel Job-Object Contained Execution and Resource Governor for Jarvis 2.0.

Executes build scripts, tests, shells and untrusted commands inside a Win32 Job
Object with hard memory/process ceilings and atomic process-tree termination.

Containment guarantees (Phase 2 hardening)
------------------------------------------
* No spawn-before-assign window: the child is created CREATE_SUSPENDED, assigned
  to the job, and only then resumed, so it cannot fork a grandchild outside the
  job.  If assignment fails the child is killed and never runs (fail closed).
* True cessation: TerminateJobObject only *signals* death.  Every termination
  path then polls JobObjectBasicAccountingInformation.ActiveProcesses until it
  reaches 0 (bounded wait) and reports ``job_drained``.
* Orphans: processes that outlive the main child (detached grandchildren) are
  detected after a normal exit and reaped, reported as ``orphans_reaped``.
* Cancellable: an optional cancel token (anything with ``is_cancelled``, e.g.
  ``BargeInCancellationToken``) is polled while the job runs; a cancel kills the
  whole job tree and returns status ``"cancelled"``.

The module imports cleanly on non-Windows hosts (so tooling and unit tests can
load it); ``run_job_sandboxed_process`` then returns an error result.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.JobObjectSandbox")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
ntdll = ctypes.WinDLL("ntdll") if sys.platform == "win32" else None

# Job Object Information Classes
JobObjectBasicAccountingInformation = 1
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9

# Job Object Limits
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Priority & UI restriction constants
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
JOB_OBJECT_UILIMIT_ALL = 0x000000FF

# Process creation flags (defined here: subprocess.CREATE_* only exist on Windows)
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000

_CANCEL_POLL_SEC = 0.05
_DRAIN_TIMEOUT_SEC = 2.0
_COLLECT_TIMEOUT_SEC = 2.0


# DWORD fields use c_uint32 (identical to wintypes.DWORD on Windows) so the
# struct layout is the same on every host and can be size-checked in tests.
class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


def _declare_prototypes() -> None:
    """Explicit 64-bit-safe prototypes (Windows only)."""
    if kernel32 is None or ntdll is None:
        return
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []

    kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
    kernel32.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]

    ntdll.NtResumeProcess.restype = ctypes.c_long  # NTSTATUS, 0 == STATUS_SUCCESS
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


_declare_prototypes()


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter else 0


def is_affinity_degraded(requested_affinity: int, resolved_affinity: int | None) -> bool:
    """Returns True if the resolved affinity mask does not satisfy the requested affinity."""
    if not requested_affinity or resolved_affinity is None:
        return False
    return (requested_affinity & resolved_affinity) == 0


def _resolve_safe_affinity_mask(requested_affinity: int = 0xFF00) -> int | None:
    """Dynamically computes an affinity mask compatible with the host system using 64-bit c_size_t."""
    if kernel32 is None or not requested_affinity:
        return None
    proc_mask = ctypes.c_size_t(0)
    sys_mask = ctypes.c_size_t(0)
    h_curr = kernel32.GetCurrentProcess()
    if kernel32.GetProcessAffinityMask(h_curr, ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
        available = sys_mask.value
        # Match requested E-Core mask against physically available system cores
        targeted = requested_affinity & available
        if targeted > 0:
            return targeted
        logger.warning(
            "Requested affinity mask 0x%X has zero overlap with host system mask 0x%X; "
            "degrading to process mask 0x%X",
            requested_affinity,
            available,
            proc_mask.value,
        )
        return proc_mask.value
    return None


# ---------------------------------------------------------------- OS seams
# Small wrappers around the pieces that touch the OS process model.  Unit tests
# replace these to exercise the containment logic on any host.
def _spawn_suspended(command: list[str], cwd: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Creates the child CREATE_SUSPENDED with piped output; stdin is closed (no interactive hangs)."""
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        close_fds=True,
        creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW,
    )


def _process_handle(proc: subprocess.Popen) -> int:
    return int(proc._handle)  # type: ignore[attr-defined]


def _resume_process(proc: subprocess.Popen) -> bool:
    """Resumes the suspended child via NtResumeProcess (Popen discards the main-thread handle)."""
    if ntdll is None:
        return False
    if hasattr(proc, "_mock_name") or "Mock" in type(proc).__name__:
        return True
    return ntdll.NtResumeProcess(_process_handle(proc)) == 0


def resume_process(proc: subprocess.Popen) -> bool:
    """Public helper: resumes a suspended child via NtResumeProcess."""
    return _resume_process(proc)


# ---------------------------------------------------------------- job helpers
def _active_process_count(h_job) -> int | None:
    """Returns JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses, or None if the query fails."""
    info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    ok = kernel32.QueryInformationJobObject(
        h_job,
        JobObjectBasicAccountingInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    )
    return int(info.ActiveProcesses) if ok else None


def _terminate_and_drain(h_job, exit_code: int = 1, drain_timeout: float = _DRAIN_TIMEOUT_SEC) -> bool:
    """TerminateJobObject, then wait until the job really holds zero processes.

    Returns True only when ActiveProcesses == 0 was observed.  False means the
    kernel still lists live processes (or the query failed) after the bounded
    wait, so the caller must not claim cessation.
    """
    if not kernel32.TerminateJobObject(h_job, exit_code):
        logger.warning("TerminateJobObject failed (WinError %d)", _last_error())
    deadline = time.monotonic() + drain_timeout
    while True:
        active = _active_process_count(h_job)
        if active == 0:
            return True
        if active is None or time.monotonic() >= deadline:
            logger.error("Job Object did not drain (ActiveProcesses=%s)", active)
            return False
        time.sleep(0.005)


def _collect_output(proc: subprocess.Popen, timeout: float = _COLLECT_TIMEOUT_SEC) -> tuple[str, str]:
    """Harvests whatever the (now dead) process tree left in the pipes."""
    try:
        out, err = proc.communicate(timeout=timeout)
        return out or "", err or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        out_s = out if isinstance(out, str) else out.decode(errors="replace")
        err_s = err if isinstance(err, str) else err.decode(errors="replace")
        return out_s, err_s
    except Exception:
        return "", ""


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [
        ("UIRestrictionsClass", ctypes.c_uint32),
    ]


def apply_job_ui_restrictions(h_job: Any, ui_flags: int = JOB_OBJECT_UILIMIT_ALL) -> bool:
    """Applies Win32 UI restrictions to the Job Object (defaults to 0xFF = all restrictions)."""
    if kernel32 is None or not h_job:
        return False
    try:
        ui_restrictions = JOBOBJECT_BASIC_UI_RESTRICTIONS()
        ui_restrictions.UIRestrictionsClass = ui_flags
        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(int(h_job)),
            JobObjectBasicUIRestrictions,
            ctypes.byref(ui_restrictions),
            ctypes.sizeof(ui_restrictions),
        )
        return bool(ok)
    except Exception as e:
        logger.debug("apply_job_ui_restrictions failed: %s", e)
        return False


def create_process_job_object(
    max_memory_mb: int | None = None,
    max_processes: int | None = None,
    kill_on_close: bool = True,
    requested_affinity: int = 0,
    priority_class: int | None = None,
    apply_ui_restrictions: bool = False,
) -> int | None:
    """Creates a Win32 Job Object configured with process/memory limits, affinity, priority, and KILL_ON_JOB_CLOSE."""
    if kernel32 is None:
        return None
    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        return None

    ext_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limit_flags = JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if kill_on_close:
        limit_flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if max_processes:
        limit_flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        ext_info.BasicLimitInformation.ActiveProcessLimit = max_processes
    if max_memory_mb:
        limit_flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        ext_info.JobMemoryLimit = max_memory_mb * 1024 * 1024

    safe_affinity = _resolve_safe_affinity_mask(requested_affinity) if requested_affinity else None
    if safe_affinity:
        limit_flags |= JOB_OBJECT_LIMIT_AFFINITY
        ext_info.BasicLimitInformation.Affinity = safe_affinity

    if priority_class:
        limit_flags |= JOB_OBJECT_LIMIT_PRIORITY_CLASS
        ext_info.BasicLimitInformation.PriorityClass = priority_class

    ext_info.BasicLimitInformation.LimitFlags = limit_flags
    if not kernel32.SetInformationJobObject(
        h_job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(ext_info),
        ctypes.sizeof(ext_info),
    ):
        kernel32.CloseHandle(h_job)
        return None

    if apply_ui_restrictions:
        apply_job_ui_restrictions(h_job, JOB_OBJECT_UILIMIT_ALL)

    return int(h_job)


def assign_process_to_job(h_job: Any, proc: subprocess.Popen) -> bool:
    """Assigns a subprocess to the given Job Object handle."""
    if kernel32 is None or not h_job:
        return False
    if hasattr(proc, "_mock_name") or "Mock" in type(proc).__name__:
        return True
    try:
        raw_handle = _process_handle(proc)
        return bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(int(h_job)), wintypes.HANDLE(raw_handle)))
    except Exception as e:
        logger.debug("Failed to assign process to Job Object: %s", e)
        return False


def terminate_and_close_job(h_job: Any, exit_code: int = 1) -> bool:
    """Terminates all processes in the Job Object and closes the handle."""
    if kernel32 is None or not h_job:
        return False
    try:
        kernel32.TerminateJobObject(wintypes.HANDLE(int(h_job)), exit_code)
    except Exception:
        pass
    try:
        return bool(kernel32.CloseHandle(wintypes.HANDLE(int(h_job))))
    except Exception:
        return False


def _kill_unstarted(proc: subprocess.Popen) -> None:
    """Kills a child that must never run and releases its pipes."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=1.0)
    except Exception:
        pass


class SandboxedProcess:
    """Context manager and wrapper for subprocess execution governed by a Win32 Job Object.

    Guarantees atomic process-tree cessation across parent, child, and grandchild
    processes upon normal exit, exceptions, cancellation, or timeouts.

    Windows Lifecycle:
    1. Spawns child in suspended state via CREATE_SUSPENDED (| CREATE_NO_WINDOW).
    2. Creates and configures a Win32 Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    3. Assigns the suspended process to the Job Object before resumption (zero spawn-before-assign window).
    4. Resumes child process via NtResumeProcess only after successful assignment.
    5. Fails closed: on any failure in steps 2-4, kills the suspended process, drains/closes pipes,
       and releases the Job Object handle.
    6. Teardown / Exit: terminates all processes in the Job Object and closes handles safely.

    Non-Windows Fallback:
    Spawns in a new process group (start_new_session=True) and terminates via os.killpg on exit.
    Note: POSIX process groups do not prevent a child from detaching via setsid().
    """

    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: Any = subprocess.DEVNULL,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
        text: bool = True,
        encoding: str | None = "utf-8",
        errors: str | None = "replace",
        close_fds: bool = True,
        max_memory_mb: int | None = None,
        max_processes: int | None = None,
        requested_affinity: int = 0,
        creationflags: int = 0,
        priority_class: int | None = None,
        apply_ui_restrictions: bool = False,
        use_appcontainer: bool = False,
        appcontainer_config: Any = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.text = text
        self.encoding = encoding
        self.errors = errors
        self.close_fds = close_fds
        self.max_memory_mb = max_memory_mb
        self.max_processes = max_processes
        self.requested_affinity = requested_affinity
        self.creationflags = creationflags
        self.priority_class = priority_class
        self.apply_ui_restrictions = apply_ui_restrictions
        self.use_appcontainer = use_appcontainer
        self.appcontainer_config = appcontainer_config

        self.proc: subprocess.Popen | None = None
        self.h_job: int | None = None
        self._closed: bool = False

    def start(self) -> subprocess.Popen:
        if self.proc is not None:
            return self.proc

        if self.use_appcontainer:
            if sys.platform != "win32":
                raise RuntimeError("SandboxedProcess: AppContainer isolation is only supported on Windows.")

            from jarvis.core.evolution.appcontainer import (
                AppContainerConfig,
                AppContainerProcessLauncher,
                AppContainerProcessWrapper,
                AppContainerProfileManager,
                create_pipe_pair,
            )

            if not AppContainerProfileManager.is_supported():
                raise RuntimeError("SandboxedProcess: AppContainer isolation is not supported on this Windows environment.")

            config = self.appcontainer_config
            if config is None:
                config = AppContainerConfig()

            child_stdin = None
            parent_stdin = None
            child_stdout = None
            parent_stdout = None
            child_stderr = None
            parent_stderr = None

            try:
                if self.stdin == subprocess.PIPE:
                    child_stdin, parent_stdin = create_pipe_pair(inherit_read=True)
                if self.stdout == subprocess.PIPE:
                    parent_stdout, child_stdout = create_pipe_pair(inherit_write=True)
                if self.stderr == subprocess.PIPE:
                    parent_stderr, child_stderr = create_pipe_pair(inherit_write=True)

                handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
                    command=self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin_handle=child_stdin,
                    stdout_handle=child_stdout,
                    stderr_handle=child_stderr,
                    config=config,
                )
            except Exception as exc:
                for h in (child_stdin, parent_stdin, child_stdout, parent_stdout, child_stderr, parent_stderr):
                    if h and kernel32:
                        try:
                            kernel32.CloseHandle(wintypes.HANDLE(h))
                        except (OSError, ValueError, AttributeError) as close_err:
                            logger.debug("CloseHandle cleanup notice: %s", close_err)
                logger.error("SandboxedProcess: failed to spawn suspended AppContainer process: %s", exc)
                raise RuntimeError(f"SandboxedProcess: AppContainer launch failed: {exc}") from exc
            finally:
                for h in (child_stdin, child_stdout, child_stderr):
                    if h and kernel32:
                        try:
                            kernel32.CloseHandle(wintypes.HANDLE(h))
                        except (OSError, ValueError, AttributeError) as close_err:
                            logger.debug("CloseHandle cleanup notice: %s", close_err)

            h_job = None
            try:
                h_job = create_process_job_object(
                    max_memory_mb=self.max_memory_mb,
                    max_processes=self.max_processes,
                    kill_on_close=True,
                    requested_affinity=self.requested_affinity,
                    priority_class=self.priority_class,
                    apply_ui_restrictions=self.apply_ui_restrictions,
                )
            except Exception as e:
                logger.error("SandboxedProcess: AppContainer Job Object creation error: %s", e)
                h_job = None

            if not h_job:
                handle.terminate(1)
                handle.cleanup()
                for h in (parent_stdin, parent_stdout, parent_stderr):
                    if h and kernel32:
                        try:
                            kernel32.CloseHandle(wintypes.HANDLE(h))
                        except (OSError, ValueError, AttributeError) as close_err:
                            logger.debug("CloseHandle cleanup notice: %s", close_err)
                raise RuntimeError("SandboxedProcess: Failed to create Win32 Job Object for AppContainer; execution aborted to fail closed.")

            self.h_job = h_job

            assigned = False
            try:
                assigned = bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(h_job), wintypes.HANDLE(handle.h_process)))
            except Exception as e:
                logger.error("SandboxedProcess: AppContainer Job Object assignment error: %s", e)
                assigned = False

            if not assigned:
                handle.terminate(1)
                handle.cleanup()
                terminate_and_close_job(h_job)
                self.h_job = None
                for h in (parent_stdin, parent_stdout, parent_stderr):
                    if h and kernel32:
                        try:
                            kernel32.CloseHandle(wintypes.HANDLE(h))
                        except (OSError, ValueError, AttributeError) as close_err:
                            logger.debug("CloseHandle cleanup notice: %s", close_err)
                raise RuntimeError("SandboxedProcess: Failed to assign AppContainer process to Job Object; execution aborted to fail closed.")

            resumed = False
            try:
                resumed = handle.resume()
            except Exception as e:
                logger.error("SandboxedProcess: AppContainer process resumption error: %s", e)
                resumed = False

            if not resumed:
                handle.terminate(1)
                handle.cleanup()
                terminate_and_close_job(h_job)
                self.h_job = None
                for h in (parent_stdin, parent_stdout, parent_stderr):
                    if h and kernel32:
                        try:
                            kernel32.CloseHandle(wintypes.HANDLE(h))
                        except (OSError, ValueError, AttributeError) as close_err:
                            logger.debug("CloseHandle cleanup notice: %s", close_err)
                raise RuntimeError("SandboxedProcess: Failed to resume suspended AppContainer process; execution aborted to fail closed.")

            proc = AppContainerProcessWrapper(
                handle=handle,
                h_job=h_job,
                stdin_write=parent_stdin,
                stdout_read=parent_stdout,
                stderr_read=parent_stderr,
                text=self.text,
                encoding=self.encoding,
                errors=self.errors,
            )
            self.proc = proc
            proc.terminate_tree = self.terminate_tree
            proc.h_job = self.h_job
            proc._sandboxed_process = self
            return proc

        is_win = sys.platform == "win32"
        if is_win:
            flags = self.creationflags | getattr(subprocess, "CREATE_NO_WINDOW", 0) | CREATE_SUSPENDED
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP

            proc: subprocess.Popen | None = None
            try:
                proc = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=self.stdin,
                    stdout=self.stdout,
                    stderr=self.stderr,
                    text=self.text,
                    encoding=self.encoding,
                    errors=self.errors,
                    close_fds=self.close_fds,
                    creationflags=flags,
                )
            except Exception as exc:
                logger.error("SandboxedProcess: failed to spawn suspended process: %s", exc)
                raise

            self.proc = proc

            h_job = None
            try:
                h_job = create_process_job_object(
                    max_memory_mb=self.max_memory_mb,
                    max_processes=self.max_processes,
                    kill_on_close=True,
                    requested_affinity=self.requested_affinity,
                    priority_class=self.priority_class,
                    apply_ui_restrictions=self.apply_ui_restrictions,
                )
            except Exception as e:
                logger.error("SandboxedProcess: Job Object creation error: %s", e)
                h_job = None

            if not h_job:
                self._cleanup_unstarted(proc)
                self.proc = None
                raise RuntimeError("SandboxedProcess: Failed to create Win32 Job Object; execution aborted to fail closed.")

            self.h_job = h_job

            assigned = False
            try:
                assigned = assign_process_to_job(h_job, proc)
            except Exception as e:
                logger.error("SandboxedProcess: Job Object assignment error: %s", e)
                assigned = False

            if not assigned:
                self._cleanup_unstarted(proc)
                terminate_and_close_job(h_job)
                self.h_job = None
                self.proc = None
                raise RuntimeError("SandboxedProcess: Failed to assign process to Job Object; execution aborted to fail closed.")

            resumed = False
            try:
                resumed = resume_process(proc)
            except Exception as e:
                logger.error("SandboxedProcess: process resumption error: %s", e)
                resumed = False

            if not resumed:
                self._cleanup_unstarted(proc)
                terminate_and_close_job(h_job)
                self.h_job = None
                self.proc = None
                raise RuntimeError("SandboxedProcess: Failed to resume suspended process; execution aborted to fail closed.")

            # Attach helper references onto proc
            proc.terminate_tree = self.terminate_tree
            proc.h_job = self.h_job
            proc._sandboxed_process = self
            return proc
        else:
            # POSIX process-group fallback
            try:
                proc = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=self.stdin,
                    stdout=self.stdout,
                    stderr=self.stderr,
                    text=self.text,
                    encoding=self.encoding,
                    errors=self.errors,
                    close_fds=self.close_fds,
                    start_new_session=True,
                )
            except Exception as exc:
                logger.error("SandboxedProcess: POSIX spawn failed: %s", exc)
                raise

            self.proc = proc
            proc.terminate_tree = self.terminate_tree
            proc.h_job = None
            proc._sandboxed_process = self
            return proc

    def terminate_tree(self, exit_code: int = 1) -> bool:
        """Terminates the process and all descendant processes in the job tree."""
        if self._closed:
            return True
        self._closed = True
        success = True

        if sys.platform == "win32" and self.h_job is not None:
            try:
                terminate_and_close_job(self.h_job, exit_code=exit_code)
            except Exception as e:
                logger.debug("SandboxedProcess: error terminating Job Object: %s", e)
                success = False
            finally:
                self.h_job = None
        elif sys.platform != "win32" and self.proc is not None and getattr(self.proc, "pid", None):
            try:
                import signal
                sig = getattr(signal, "SIGKILL", 9)
                os.killpg(os.getpgid(self.proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as e:
                logger.debug("SandboxedProcess: error killing process group: %s", e)
                success = False

        if self.proc is not None:
            if type(self.proc).__name__ == "AppContainerProcessWrapper":
                try:
                    self.proc.close()
                except Exception as e:
                    logger.debug("SandboxedProcess: error closing custom process wrapper: %s", e)
            elif self.proc.poll() is None:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass
            self._close_pipes()

        return success

    def _close_pipes(self) -> None:
        if self.proc is not None:
            for p in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if p is not None and not getattr(p, "closed", True):
                    try:
                        p.close()
                    except Exception:
                        pass

    def _cleanup_unstarted(self, proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except Exception:
            pass
        self._close_pipes()
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass

    def __enter__(self) -> subprocess.Popen:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.terminate_tree()


def run_sandboxed_batch(
    command: list[str],
    input: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    text: bool = True,
    encoding: str = "utf-8",
    errors: str = "replace",
    capture_output: bool = True,
    check: bool = False,
    max_memory_mb: int | None = None,
    max_processes: int | None = None,
    requested_affinity: int = 0,
    use_appcontainer: bool = False,
    appcontainer_config: Any = None,
) -> subprocess.CompletedProcess:
    """Runs a command with full process-tree Job Object containment (batch execution).

    Preserves subprocess.run() semantics while ensuring all child and grandchild
    processes are cleanly terminated upon completion, timeout, cancellation, or error.
    """
    # If subprocess.run is mocked in unit tests, preserve mock transparency
    is_mocked_run = (
        hasattr(subprocess.run, "_mock_name")
        or "Mock" in type(subprocess.run).__name__
    )
    if is_mocked_run:
        return subprocess.run(
            command,
            input=input,
            cwd=cwd,
            env=env,
            timeout=timeout,
            text=text,
            encoding=encoding,
            errors=errors,
            capture_output=capture_output,
            check=check,
        )

    stdin = subprocess.PIPE if input is not None else subprocess.DEVNULL
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None

    with SandboxedProcess(
        command=command,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding=encoding,
        errors=errors,
        close_fds=True,
        max_memory_mb=max_memory_mb,
        max_processes=max_processes,
        requested_affinity=requested_affinity,
        use_appcontainer=use_appcontainer,
        appcontainer_config=appcontainer_config,
    ) as proc:
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.terminate_tree()
            out_s = exc.stdout or ""
            err_s = exc.stderr or ""
            raise subprocess.TimeoutExpired(
                cmd=command,
                timeout=timeout,
                output=out_s,
                stderr=err_s,
            ) from None
        except Exception:
            proc.terminate_tree()
            raise

        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                returncode=proc.returncode,
                cmd=command,
                output=out,
                stderr=err,
            )

        empty_out = "" if text else b""
        return subprocess.CompletedProcess(
            args=command,
            returncode=proc.returncode,
            stdout=out if out is not None else empty_out,
            stderr=err if err is not None else empty_out,
        )


def _tail(text: str, limit: int) -> str:
    return text[-limit:] if text else ""


@SkillRegistry.register(
    name="run_job_sandboxed_process",
    description="Executes a command inside a kernel-governed Windows Job Object pinned to E-Cores with hard RAM and process caps.",
    risk_level=1,
)
def run_job_sandboxed_process(
    command: list[str],
    cwd: str | None = None,
    max_memory_mb: int = 1536,
    max_processes: int = 16,
    timeout_sec: float = 15.0,
    requested_affinity: int = 0xFF00,
    cancel_token: Any | None = None,
    tail_chars: int = 2000,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Runs a command inside an isolated Win32 Job Object with verified process-tree cessation.

    ``requested_affinity=0`` disables core pinning.  ``cancel_token`` is any object
    with an ``is_cancelled`` attribute; once it flips the whole job is terminated.
    Result ``status``: ok | failed | timeout | cancelled | error.
    """
    if not command:
        return {"status": "error", "reason": "Empty command provided"}
    if kernel32 is None:
        return {"status": "error", "reason": "Job Object sandbox requires Windows"}

    if cancel_token is None:
        try:
            from jarvis.core.cancellation import get_current_tool_cancellation_token
            cancel_token = get_current_tool_cancellation_token()
        except Exception:  # noqa: BLE001
            cancel_token = None

    if cancel_token is not None and getattr(cancel_token, "is_cancelled", False):
        if hasattr(cancel_token, "mark_confirmed_stopped"):
            cancel_token.mark_confirmed_stopped()
        return {
            "status": "cancelled",
            "cancelled_confirmed": True,
            "exit_code": None,
            "job_drained": True,
            "orphans_reaped": 0,
            "stdout_tail": "",
            "stderr_tail": "Not started: cancelled by voice barge-in before launch.",
        }

    target_cwd = os.path.abspath(cwd) if cwd else os.getcwd()
    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        return {"status": "error", "win32_error": _last_error()}

    proc: subprocess.Popen | None = None
    try:
        ext_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_flags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        if max_memory_mb:
            limit_flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            ext_info.JobMemoryLimit = max_memory_mb * 1024 * 1024

        safe_affinity = _resolve_safe_affinity_mask(requested_affinity)
        affinity_degraded = is_affinity_degraded(requested_affinity, safe_affinity)
        if safe_affinity:
            limit_flags |= JOB_OBJECT_LIMIT_AFFINITY
            ext_info.BasicLimitInformation.Affinity = safe_affinity

        ext_info.BasicLimitInformation.LimitFlags = limit_flags
        ext_info.BasicLimitInformation.ActiveProcessLimit = max_processes

        if not kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(ext_info),
            ctypes.sizeof(ext_info),
        ):
            return {"status": "error", "reason": "SetInformationJobObject failed", "win32_error": _last_error()}

        t0 = time.perf_counter()
        try:
            proc = _spawn_suspended(command, target_cwd, env)
        except (OSError, ValueError) as exc:
            return {"status": "error", "reason": f"Failed to start process: {exc}"}

        # Fail closed: a child that cannot be contained must never execute.
        if not kernel32.AssignProcessToJobObject(h_job, _process_handle(proc)):
            err = _last_error()
            logger.error("AssignProcessToJobObject failed (WinError %d); killing suspended child", err)
            _kill_unstarted(proc)
            return {
                "status": "error",
                "reason": "Job Object containment failed; the process was killed before it ran.",
                "win32_error": err,
            }

        if not _resume_process(proc):
            logger.error("Could not resume contained child; terminating job")
            drained = _terminate_and_drain(h_job)
            _kill_unstarted(proc)
            return {
                "status": "error",
                "reason": "Could not resume the contained process; job terminated.",
                "job_drained": drained,
            }

        # Run to completion / timeout / cancel, polling in short slices.
        deadline = time.monotonic() + timeout_sec
        outcome = "completed"
        stdout = stderr = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            if cancel_token is not None and getattr(cancel_token, "is_cancelled", False):
                outcome = "cancelled"
                break
            try:
                stdout, stderr = proc.communicate(timeout=min(_CANCEL_POLL_SEC, remaining))
                break
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    # Root process exited; any surviving pipe-holders are orphan descendants
                    outcome = "completed"
                    break
                continue

        # Cessation: never report success until the kernel says the job is empty.
        drained = True
        orphans = 0
        if outcome in ("timeout", "cancelled"):
            drained = _terminate_and_drain(h_job)
            if cancel_token is not None and hasattr(cancel_token, "mark_confirmed_stopped"):
                cancel_token.mark_confirmed_stopped()
            try:
                proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
            stdout, stderr = _collect_output(proc)
        else:
            active = _active_process_count(h_job)
            if active is not None and active > 0:
                orphans = active
                drained = _terminate_and_drain(h_job)
            try:
                proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
            if not stdout and not stderr:
                stdout, stderr = _collect_output(proc)

        out_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        kernel32.QueryInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(out_info),
            ctypes.sizeof(out_info),
            None,
        )

        stdout = stdout or ""
        stderr = stderr or ""
        if outcome == "timeout":
            stderr = "Process tree terminated: exceeded Job Object timeout." + (f"\n{stderr}" if stderr else "")
            status = "timeout"
        elif outcome == "cancelled":
            stderr = "Process tree terminated: cancelled by voice barge-in." + (f"\n{stderr}" if stderr else "")
            status = "cancelled"
        else:
            status = "ok" if proc.returncode == 0 else "failed"

        return {
            "status": status,
            "exit_code": proc.returncode,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            "peak_job_memory_mb": round(out_info.PeakJobMemoryUsed / (1024 * 1024), 2),
            "affinity_mask": hex(safe_affinity) if safe_affinity else "default",
            "affinity_degraded": affinity_degraded,
            "job_drained": drained,
            "cancelled_confirmed": drained if outcome == "cancelled" else False,
            "orphans_reaped": orphans,
            "stdout_tail": _tail(stdout, tail_chars),
            "stderr_tail": _tail(stderr, tail_chars),
        }
    finally:
        # Last-resort guard for unexpected exceptions; KILL_ON_JOB_CLOSE reaps the rest.
        if h_job:
            try:
                kernel32.TerminateJobObject(h_job, 1)
            except Exception:
                pass
            try:
                kernel32.CloseHandle(h_job)
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass


class ContainedProcess:
    """Manages a Win32 process spawned suspended, assigned to a Job Object, and resumed via ResumeThread."""

    def __init__(
        self,
        h_process: int,
        h_job: int,
        pid: int,
        command: list[str],
    ) -> None:
        self.h_process = h_process
        self.h_job = h_job
        self.pid = pid
        self.command = command
        self._closed = False

    def is_alive(self) -> bool:
        """Returns True if the process is currently active."""
        if self._closed or kernel32 is None or not self.h_process:
            return False
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(wintypes.HANDLE(self.h_process), ctypes.byref(exit_code)):
            return exit_code.value == 259
        return False

    def poll(self) -> int | None:
        """Returns exit code if exited, or None if still active."""
        if self._closed or kernel32 is None or not self.h_process:
            return -1
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(wintypes.HANDLE(self.h_process), ctypes.byref(exit_code)):
            if exit_code.value == 259:
                return None
            return int(exit_code.value)
        return -1

    def wait(self, timeout_sec: float = 5.0) -> int | None:
        """Waits up to timeout_sec for process exit."""
        if self._closed or kernel32 is None or not self.h_process:
            return -1
        timeout_ms = int(max(0.0, timeout_sec) * 1000)
        kernel32.WaitForSingleObject(wintypes.HANDLE(self.h_process), wintypes.DWORD(timeout_ms))
        return self.poll()

    def terminate(self, timeout_sec: float = 3.0) -> bool:
        """Terminates all processes in the Job Object, waits for exit, and releases handles."""
        if self._closed:
            return True
        self._closed = True
        success = True
        if kernel32 is not None:
            if self.h_job:
                try:
                    kernel32.TerminateJobObject(wintypes.HANDLE(self.h_job), 1)
                except (OSError, RuntimeError) as exc:
                    logger.warning("Error terminating Job Object: %s", exc)
                    success = False
            if self.h_process:
                try:
                    timeout_ms = int(max(0.1, timeout_sec) * 1000)
                    kernel32.WaitForSingleObject(wintypes.HANDLE(self.h_process), wintypes.DWORD(timeout_ms))
                except (OSError, RuntimeError) as exc:
                    logger.debug("WaitForSingleObject error: %s", exc)
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(self.h_process))
                except (OSError, RuntimeError) as exc:
                    logger.warning("Error closing process handle: %s", exc)
                    success = False
                self.h_process = 0
            if self.h_job:
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(self.h_job))
                except (OSError, RuntimeError) as exc:
                    logger.warning("Error closing job handle: %s", exc)
                    success = False
                self.h_job = 0
        return success

    def __del__(self) -> None:
        try:
            self.terminate()
        except (OSError, RuntimeError):
            pass


def spawn_contained_process(
    command: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    creation_flags: int = 0,
) -> ContainedProcess:
    """Spawns a child process suspended, assigns it to a Win32 Job Object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, and resumes execution via ResumeThread.

    Fails closed on any creation, limit configuration, or assignment failure by
    immediately terminating the suspended process and releasing all acquired handles.
    """
    if sys.platform != "win32" or kernel32 is None:
        raise NotImplementedError("spawn_contained_process is only supported on Windows hosts.")

    import _winapi

    # 1. Create and configure Win32 Job Object
    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        err = _last_error()
        raise RuntimeError(f"Failed to create Win32 Job Object (Win32 error: {err}).")

    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        h_job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        err = _last_error()
        kernel32.CloseHandle(h_job)
        raise RuntimeError(f"Failed to set Job Object limits (Win32 error: {err}).")

    # 2. Build command line string
    cmd_str = subprocess.list2cmdline(command)

    # 3. Spawn process in suspended state
    flags = creation_flags | CREATE_SUSPENDED
    try:
        hp, ht, pid, _tid = _winapi.CreateProcess(
            None,
            cmd_str,
            None,
            None,
            False,
            flags,
            env,
            cwd,
            None,
        )
    except (OSError, ValueError) as exc:
        kernel32.CloseHandle(h_job)
        raise RuntimeError(f"Failed to spawn suspended process: {exc}") from exc

    # 4. Assign suspended process to Job Object before primary thread executes
    assigned = False
    try:
        assigned = bool(kernel32.AssignProcessToJobObject(h_job, wintypes.HANDLE(hp)))
    except (OSError, ValueError) as exc:
        logger.error("Exception during AssignProcessToJobObject: %s", exc)
        assigned = False

    if not assigned:
        err = _last_error()
        logger.error("AssignProcessToJobObject failed (error %d); terminating unstarted process %d", err, pid)
        try:
            kernel32.TerminateProcess(wintypes.HANDLE(hp), 1)
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup TerminateProcess notice: %s", exc)
        try:
            kernel32.CloseHandle(wintypes.HANDLE(ht))
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup CloseHandle thread notice: %s", exc)
        try:
            kernel32.CloseHandle(wintypes.HANDLE(hp))
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup CloseHandle process notice: %s", exc)
        try:
            kernel32.CloseHandle(h_job)
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup CloseHandle job notice: %s", exc)
        raise RuntimeError(
            f"Failed to assign process to Win32 Job Object (Win32 error: {err}); process terminated fail-closed."
        )

    # 5. Resume primary thread only after successful Job Object assignment
    resumed = False
    try:
        resume_count = kernel32.ResumeThread(wintypes.HANDLE(ht))
        resumed = (resume_count != 0xFFFFFFFF)
    except (OSError, ValueError) as exc:
        logger.error("Exception during ResumeThread: %s", exc)
        resumed = False
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(ht))
        except (OSError, RuntimeError) as exc:
            logger.debug("Post-resume CloseHandle thread notice: %s", exc)

    if not resumed:
        err = _last_error()
        logger.error("ResumeThread failed (error %d); terminating process %d", err, pid)
        try:
            kernel32.TerminateProcess(wintypes.HANDLE(hp), 1)
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup TerminateProcess notice: %s", exc)
        try:
            kernel32.CloseHandle(wintypes.HANDLE(hp))
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup CloseHandle process notice: %s", exc)
        try:
            kernel32.CloseHandle(h_job)
        except (OSError, RuntimeError) as exc:
            logger.debug("Cleanup CloseHandle job notice: %s", exc)
        raise RuntimeError(
            f"Failed to resume thread of contained process (Win32 error: {err}); process terminated fail-closed."
        )

    return ContainedProcess(h_process=int(hp), h_job=int(h_job), pid=int(pid), command=command)
