"""Kernel Restart Manager file-lock and TCP port forensics for Jarvis 2.0.

Provides sub-2ms inspection and surgical release of file locks via Rstrtmgr.dll
and port conflicts via iphlpapi.dll without spawning external subprocesses.
"""
import ctypes
import os
from ctypes import wintypes
from typing import Any

from jarvis.skills.base import SkillRegistry

# Windows DLLs
rstrtmgr = ctypes.WinDLL("Rstrtmgr.dll", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)

# Constants
CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
ERROR_MORE_DATA = 234
ERROR_SUCCESS = 0

# TCP Table Constants
AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5

# System Critical Process Protection
PROTECTED_PROCESS_NAMES: set[str] = {
    "system", "system idle process", "csrss.exe", "lsass.exe",
    "services.exe", "smss.exe", "wininit.exe", "winlogon.exe",
    "agy.exe", "ollama.exe", "python.exe", "pythonw.exe"
}


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", FILETIME)]


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", wintypes.DWORD),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


# Function signatures for 64-bit safety
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]

kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]


def _get_process_image_path(pid: int) -> str | None:
    """Retrieves the full executable path of a PID using QueryFullProcessImageNameW."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h_proc)


@SkillRegistry.register(
    name="inspect_file_locks",
    description="Identifies exact Windows processes locking a file or directory in <2ms via Rstrtmgr.dll.",
    risk_level=0,
)
def inspect_file_locks(file_path: str) -> dict[str, Any]:
    """Queries the Windows Restart Manager API to pinpoint processes holding locks on file_path."""
    norm_path = os.path.abspath(file_path)
    session_handle = wintypes.DWORD(0)
    session_key = (wintypes.WCHAR * (CCH_RM_SESSION_KEY + 1))()

    res = rstrtmgr.RmStartSession(ctypes.byref(session_handle), 0, session_key)
    if res != ERROR_SUCCESS:
        return {"status": "error", "win32_error": res, "file_path": norm_path, "holders": []}

    try:
        path_ptr = ctypes.c_wchar_p(norm_path)
        resource_array = (ctypes.c_wchar_p * 1)(path_ptr)
        res = rstrtmgr.RmRegisterResources(session_handle, 1, resource_array, 0, None, 0, None)
        if res != ERROR_SUCCESS:
            return {"status": "error", "win32_error": res, "file_path": norm_path, "holders": []}

        proc_info_needed = wintypes.UINT(0)
        proc_info_count = wintypes.UINT(0)
        reboot_reasons = wintypes.DWORD(0)

        # Initial size probe
        res = rstrtmgr.RmGetList(
            session_handle,
            ctypes.byref(proc_info_needed),
            ctypes.byref(proc_info_count),
            None,
            ctypes.byref(reboot_reasons),
        )

        if res == ERROR_SUCCESS and proc_info_needed.value == 0:
            return {"status": "unlocked", "file_path": norm_path, "holders": []}

        if res != ERROR_MORE_DATA:
            return {"status": "error", "win32_error": res, "file_path": norm_path, "holders": []}

        # Allocate with safety buffer (+4) to handle races
        alloc_count = proc_info_needed.value + 4
        proc_info_count.value = alloc_count
        rg_affected = (RM_PROCESS_INFO * alloc_count)()

        res = rstrtmgr.RmGetList(
            session_handle,
            ctypes.byref(proc_info_needed),
            ctypes.byref(proc_info_count),
            rg_affected,
            ctypes.byref(reboot_reasons),
        )

        if res != ERROR_SUCCESS:
            return {"status": "error", "win32_error": res, "file_path": norm_path, "holders": []}

        holders: list[dict[str, Any]] = []
        for i in range(proc_info_count.value):
            info = rg_affected[i]
            pid = int(info.Process.dwProcessId)
            full_path = _get_process_image_path(pid)
            holders.append({
                "pid": pid,
                "app_name": str(info.strAppName),
                "service_name": str(info.strServiceShortName) if info.strServiceShortName else None,
                "app_type": int(info.ApplicationType),
                "is_restartable": bool(info.bRestartable),
                "image_path": full_path,
            })

        return {
            "status": "locked" if holders else "unlocked",
            "file_path": norm_path,
            "holders": holders,
        }
    finally:
        rstrtmgr.RmEndSession(session_handle)


@SkillRegistry.register(
    name="inspect_port_holders",
    description="Identifies the PID and process listening on an active TCP port in <2ms via iphlpapi.dll.",
    risk_level=0,
)
def inspect_port_holders(port: int) -> dict[str, Any]:
    """Queries the Windows Extended TCP table to find processes listening on port."""
    buf_size = wintypes.DWORD(0)
    # Probe required buffer size
    res = iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(buf_size), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
    )
    if buf_size.value == 0:
        return {"status": "error", "reason": "Failed to query TCP table size", "port": port}

    buf = ctypes.create_string_buffer(buf_size.value)
    res = iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(buf_size), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
    )
    if res != ERROR_SUCCESS:
        return {"status": "error", "win32_error": res, "port": port}

    # First DWORD is dwNumEntries
    num_entries = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    table_offset = ctypes.sizeof(wintypes.DWORD)
    row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)

    holders: list[dict[str, Any]] = []
    for i in range(num_entries):
        row_ptr = ctypes.byref(buf, table_offset + (i * row_size))
        row = ctypes.cast(row_ptr, ctypes.POINTER(MIB_TCPROW_OWNER_PID)).contents
        # Convert port from network byte order
        local_port = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
        if local_port == port:
            pid = int(row.dwOwningPid)
            full_path = _get_process_image_path(pid)
            holders.append({
                "pid": pid,
                "state": int(row.dwState),
                "image_path": full_path,
            })

    return {
        "status": "listening" if holders else "free",
        "port": port,
        "holders": holders,
    }


@SkillRegistry.register(
    name="release_file_lock",
    description="Surgically releases non-critical locks on a file or folder, strictly shielding OS critical PIDs.",
    risk_level=2,
)
def release_file_lock(file_path: str, allow_kill: bool = False) -> dict[str, Any]:
    """Inspects file locks and optionally terminates non-critical holder processes."""
    inspection = inspect_file_locks(file_path)
    if inspection.get("status") != "locked":
        return inspection

    holders = inspection.get("holders", [])
    current_pid = os.getpid()

    actions: list[dict[str, Any]] = []
    for holder in holders:
        pid = holder["pid"]
        app_name = (holder.get("app_name") or "").lower()

        # Critical Protection Checks
        if pid in (0, 4, current_pid):
            actions.append({"pid": pid, "action": "blocked", "reason": "Self or Core OS PID"})
            continue

        if any(crit in app_name for crit in PROTECTED_PROCESS_NAMES):
            actions.append({"pid": pid, "action": "blocked", "reason": "Protected system process"})
            continue

        if not allow_kill:
            actions.append({"pid": pid, "action": "pending_approval", "app_name": holder.get("app_name")})
            continue

        # Surgical termination
        PROCESS_TERMINATE = 0x0001
        h_proc = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h_proc:
            actions.append({"pid": pid, "action": "failed", "win32_error": ctypes.get_last_error()})
            continue

        try:
            terminated = kernel32.TerminateProcess(h_proc, 1)
            actions.append({
                "pid": pid,
                "action": "terminated" if terminated else "failed",
                "app_name": holder.get("app_name"),
            })
        finally:
            kernel32.CloseHandle(h_proc)

    return {
        "status": "processed",
        "file_path": file_path,
        "actions": actions,
    }
