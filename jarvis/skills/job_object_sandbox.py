"""Kernel Job-Object Contained Execution and Resource Governor for Jarvis 2.0.

Executes build scripts, tests, and untrusted commands inside a Win32 Job Object
pinned to E-Cores (0xFF00) with hard memory ceilings and atomic process termination.
"""
import ctypes
from ctypes import wintypes
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from jarvis.skills.base import SkillRegistry

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Job Object Information Classes
JobObjectExtendedLimitInformation = 9

# Job Object Limits
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


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
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
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


# Explicit 64-bit types
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]

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


def _resolve_safe_affinity_mask(requested_affinity: int = 0xFF00) -> Optional[int]:
    """Dynamically computes an affinity mask compatible with the host system using 64-bit c_size_t."""
    proc_mask = ctypes.c_size_t(0)
    sys_mask = ctypes.c_size_t(0)
    h_curr = kernel32.GetCurrentProcess()
    if kernel32.GetProcessAffinityMask(h_curr, ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
        available = sys_mask.value
        # Match requested E-Core mask against physically available system cores
        targeted = requested_affinity & available
        return targeted if targeted > 0 else proc_mask.value
    return None


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
) -> Dict[str, Any]:
    """Runs a command inside an isolated Win32 Job Object that prevents P-Core starvation and memory leaks."""
    if not command:
        return {"status": "error", "reason": "Empty command provided"}

    target_cwd = os.path.abspath(cwd) if cwd else os.getcwd()
    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        return {"status": "error", "win32_error": ctypes.get_last_error()}

    proc: Optional[subprocess.Popen] = None
    try:
        ext_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_flags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )

        safe_affinity = _resolve_safe_affinity_mask(requested_affinity)
        if safe_affinity:
            limit_flags |= JOB_OBJECT_LIMIT_AFFINITY
            ext_info.BasicLimitInformation.Affinity = safe_affinity

        ext_info.BasicLimitInformation.LimitFlags = limit_flags
        ext_info.BasicLimitInformation.ActiveProcessLimit = max_processes
        ext_info.JobMemoryLimit = max_memory_mb * 1024 * 1024

        res = kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(ext_info),
            ctypes.sizeof(ext_info),
        )
        if not res:
            return {"status": "error", "win32_error": ctypes.get_last_error()}

        t0 = time.perf_counter()
        proc = subprocess.Popen(
            command,
            cwd=target_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        kernel32.AssignProcessToJobObject(h_job, int(proc._handle))

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        except subprocess.TimeoutExpired:
            kernel32.TerminateJobObject(h_job, 1)
            # Mandatory pipe closure and process cleanup to prevent ResourceWarning
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
            stdout, stderr = "", "Process tree terminated: exceeded Job Object timeout."
            timed_out = True

        out_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        kernel32.QueryInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(out_info),
            ctypes.sizeof(out_info),
            None,
        )

        peak_mb = round(out_info.PeakJobMemoryUsed / (1024 * 1024), 2)
        return {
            "status": "timeout" if timed_out else ("ok" if proc.returncode == 0 else "failed"),
            "exit_code": proc.returncode,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            "peak_job_memory_mb": peak_mb,
            "affinity_mask": hex(safe_affinity) if safe_affinity else "default",
            "stdout_tail": stdout[-2000:] if stdout else "",
            "stderr_tail": stderr[-2000:] if stderr else "",
        }
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass
        kernel32.CloseHandle(h_job)
