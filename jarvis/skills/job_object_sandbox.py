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
from ctypes import wintypes
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.JobObjectSandbox")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
ntdll = ctypes.WinDLL("ntdll") if sys.platform == "win32" else None

# Job Object Information Classes
JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9

# Job Object Limits
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

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


_declare_prototypes()


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter else 0


def _resolve_safe_affinity_mask(requested_affinity: int = 0xFF00) -> Optional[int]:
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
        return targeted if targeted > 0 else proc_mask.value
    return None


# ---------------------------------------------------------------- OS seams
# Small wrappers around the pieces that touch the OS process model.  Unit tests
# replace these to exercise the containment logic on any host.
def _spawn_suspended(command: List[str], cwd: str, env: Optional[Dict[str, str]] = None) -> subprocess.Popen:
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
    return ntdll.NtResumeProcess(_process_handle(proc)) == 0


def resume_process(proc: subprocess.Popen) -> bool:
    """Public helper: resumes a suspended child via NtResumeProcess."""
    return _resume_process(proc)


# ---------------------------------------------------------------- job helpers
def _active_process_count(h_job) -> Optional[int]:
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


def create_process_job_object(
    max_memory_mb: Optional[int] = None,
    max_processes: Optional[int] = None,
    kill_on_close: bool = True,
    requested_affinity: int = 0,
) -> Optional[int]:
    """Creates a Win32 Job Object configured with process/memory limits and KILL_ON_JOB_CLOSE."""
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

    ext_info.BasicLimitInformation.LimitFlags = limit_flags
    if not kernel32.SetInformationJobObject(
        h_job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(ext_info),
        ctypes.sizeof(ext_info),
    ):
        kernel32.CloseHandle(h_job)
        return None
    return int(h_job)


def assign_process_to_job(h_job: Any, proc: subprocess.Popen) -> bool:
    """Assigns a subprocess to the given Job Object handle."""
    if kernel32 is None or not h_job:
        return False
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


def _tail(text: str, limit: int) -> str:
    return text[-limit:] if text else ""


@SkillRegistry.register(
    name="run_job_sandboxed_process",
    description="Executes a command inside a kernel-governed Windows Job Object pinned to E-Cores with hard RAM and process caps.",
    risk_level=1,
)
def run_job_sandboxed_process(
    command: List[str],
    cwd: Optional[str] = None,
    max_memory_mb: int = 1536,
    max_processes: int = 16,
    timeout_sec: float = 15.0,
    requested_affinity: int = 0xFF00,
    cancel_token: Optional[Any] = None,
    tail_chars: int = 2000,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Runs a command inside an isolated Win32 Job Object with verified process-tree cessation.

    ``requested_affinity=0`` disables core pinning.  ``cancel_token`` is any object
    with an ``is_cancelled`` attribute; once it flips the whole job is terminated.
    Result ``status``: ok | failed | timeout | cancelled | error.
    """
    if not command:
        return {"status": "error", "reason": "Empty command provided"}
    if kernel32 is None:
        return {"status": "error", "reason": "Job Object sandbox requires Windows"}
    if cancel_token is not None and getattr(cancel_token, "is_cancelled", False):
        return {
            "status": "cancelled",
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

    proc: Optional[subprocess.Popen] = None
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
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
            stdout, stderr = _collect_output(proc)
        else:
            active = _active_process_count(h_job)
            if active is not None and active > 0:
                orphans = active
                drained = _terminate_and_drain(h_job)
            try:
                proc.wait(timeout=1.0)
            except Exception:
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
            "job_drained": drained,
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
