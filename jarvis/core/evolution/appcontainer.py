"""Windows AppContainer Isolation and Capability Sandbox for Jarvis 2.0.

Provides kernel-level AppContainer isolation with explicitly bounded capabilities,
dedicated filesystem Access Control Lists (ACLs), and integration with Win32 Job Objects.

Guarantees:
1. Least Privilege: Default capability set is empty (zero network, zero device, zero credential access).
2. Fail Closed: Any profile derivation, attribute list, process creation, or Job Object assignment
   failure aborts execution immediately (kills suspended child, releases handles, raises RuntimeError).
   Never falls back to uncontained execution when AppContainer isolation is requested.
3. Zero Spawn-Before-Assign Window: Process is spawned with CREATE_SUSPENDED and assigned to
   the Win32 Job Object before resumption.
4. Non-Inheriting Parent Isolation: Uses PROC_THREAD_ATTRIBUTE_HANDLE_LIST to restrict handle
   inheritance strictly to explicitly permitted standard I/O handles.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.Evolution.AppContainer")

# Windows API DLLs
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True) if sys.platform == "win32" else None
userenv = ctypes.WinDLL("userenv", use_last_error=True) if sys.platform == "win32" else None
ole32 = ctypes.WinDLL("ole32", use_last_error=True) if sys.platform == "win32" else None

# Process creation and attribute list constants
PROC_THREAD_ATTRIBUTE_NUMBER = 0x0000FFFF
PROC_THREAD_ATTRIBUTE_THREAD = 0x00010000
PROC_THREAD_ATTRIBUTE_INPUT = 0x00020000
PROC_THREAD_ATTRIBUTE_ADDITIVE = 0x00040000

# Attribute 9 = SecurityCapabilities; Attribute 2 = HandleList
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = (9 & PROC_THREAD_ATTRIBUTE_NUMBER) | PROC_THREAD_ATTRIBUTE_INPUT
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = (2 & PROC_THREAD_ATTRIBUTE_NUMBER) | PROC_THREAD_ATTRIBUTE_INPUT

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001

# Error and Status Codes
ERROR_ALREADY_EXISTS = 183
HRESULT_ERROR_ALREADY_EXISTS = 0x800700B7
ERROR_INSUFFICIENT_BUFFER = 122
S_OK = 0
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

# File Access and Creation constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080

# Known Well-Known AppContainer Capability SIDs
CAPABILITY_SID_MAP: dict[str, str] = {
    "internetClient": "S-1-15-3-1",
    "internetClientServer": "S-1-15-3-2",
    "privateNetworkClientServer": "S-1-15-3-3",
    "picturesLibrary": "S-1-15-3-4",
    "videosLibrary": "S-1-15-3-5",
    "musicLibrary": "S-1-15-3-6",
    "documentsLibrary": "S-1-15-3-7",
    "enterpriseAuthentication": "S-1-15-3-8",
    "sharedUserCertificates": "S-1-15-3-9",
    "removableStorage": "S-1-15-3-10",
    "appointments": "S-1-15-3-11",
    "contacts": "S-1-15-3-12",
}


class AppContainerSecurityError(RuntimeError):
    """Raised when AppContainer profile creation, configuration, or containment fails."""


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]



def _declare_prototypes() -> None:
    if kernel32 is None or advapi32 is None or userenv is None:
        return

    # kernel32
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]

    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]

    kernel32.GetHandleInformation.restype = wintypes.BOOL
    kernel32.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]

    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]

    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]

    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]

    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    # advapi32
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]

    advapi32.FreeSid.restype = ctypes.c_void_p
    advapi32.FreeSid.argtypes = [ctypes.c_void_p]

    # userenv
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]


_declare_prototypes()


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def create_pipe_pair(inherit_read: bool = False, inherit_write: bool = False) -> tuple[int, int]:
    """Creates an anonymous Win32 pipe pair returning (read_handle, write_handle)."""
    if kernel32 is None:
        raise AppContainerSecurityError("Win32 kernel32 is unavailable.")

    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.bInheritHandle = True

    h_read = wintypes.HANDLE()
    h_write = wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(h_read), ctypes.byref(h_write), ctypes.byref(sa), 0):
        last_err = ctypes.get_last_error()
        raise AppContainerSecurityError(f"CreatePipe failed with WinError {last_err}")

    if not inherit_read:
        kernel32.SetHandleInformation(h_read, HANDLE_FLAG_INHERIT, 0)
    if not inherit_write:
        kernel32.SetHandleInformation(h_write, HANDLE_FLAG_INHERIT, 0)

    return int(h_read.value), int(h_write.value)


def _open_nul_handle(access: int) -> int:
    """Opens an inheritable Win32 file handle to \\\\.\\NUL with requested access flags."""
    if kernel32 is None:
        raise AppContainerSecurityError("Win32 kernel32 is unavailable.")

    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = True

    h = kernel32.CreateFileW(
        r"\\.\NUL",
        wintypes.DWORD(access),
        wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE),
        ctypes.byref(sa),
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(FILE_ATTRIBUTE_NORMAL),
        None,
    )
    if h in (None, 0, -1, ctypes.c_void_p(-1).value, 18446744073709551615):
        last_err = ctypes.get_last_error()
        raise AppContainerSecurityError(f"CreateFileW on \\\\.\\NUL failed with WinError {last_err}")

    return int(h)


@dataclass
class AppContainerConfig:
    """Explicit configuration parameters for AppContainer isolation."""
    profile_name: str = ""
    display_name: str = "Jarvis AppContainer Sandbox"
    description: str = "Jarvis 2.0 Contained Execution Boundary"
    capabilities: list[str] = field(default_factory=list)  # Empty by default (least privilege)
    grant_paths: list[str | Path] = field(default_factory=list)  # Explicitly granted directories
    auto_cleanup_profile: bool = True  # Automatically delete container profile on exit

    def __post_init__(self) -> None:
        if not self.profile_name:
            import secrets
            self.profile_name = f"J_SB_{os.getpid()}_{secrets.token_hex(4)}"
        elif len(self.profile_name) > 40:
            import hashlib
            h = hashlib.sha256(self.profile_name.encode()).hexdigest()[:10]
            self.profile_name = f"{self.profile_name[:25]}_{h}"


# Cache for directories with well-known AppContainer package ACEs
_WELL_KNOWN_PACKAGE_ACE_CACHE: dict[str, bool] = {}


class AppContainerProfileManager:
    """Manages creation, reuse, SID resolution, ACL assignment, and deletion of AppContainer profiles."""

    @staticmethod
    def is_supported() -> bool:
        """Returns True if the host OS provides native Win32 AppContainer isolation APIs."""
        if sys.platform != "win32":
            return False
        return bool(userenv and advapi32 and kernel32 and hasattr(userenv, "CreateAppContainerProfile"))

    @classmethod
    def get_or_create_profile(cls, config: AppContainerConfig) -> tuple[int, str, bool]:
        """Creates or reuses an AppContainer profile, returning (p_sid_handle, sid_string, is_new_profile).

        Fails closed on any error.
        """
        if not cls.is_supported():
            raise AppContainerSecurityError(
                "AppContainer isolation is only supported on Windows hosts; fail-closed policy rejects fallback."
            )

        appcontainer_sid = ctypes.c_void_p()
        name_w = wintypes.LPCWSTR(config.profile_name)
        disp_w = wintypes.LPCWSTR(config.display_name)
        desc_w = wintypes.LPCWSTR(config.description)

        hr = userenv.CreateAppContainerProfile(
            name_w,
            disp_w,
            desc_w,
            None,
            0,
            ctypes.byref(appcontainer_sid),
        )

        # 0x800700B7 = HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
        hr_u = hr & 0xFFFFFFFF
        is_new_profile = False
        if hr_u == HRESULT_ERROR_ALREADY_EXISTS or hr == -2147024713:
            logger.debug("AppContainer profile '%s' already exists; deriving SID", config.profile_name)
            derive_hr = userenv.DeriveAppContainerSidFromAppContainerName(
                name_w,
                ctypes.byref(appcontainer_sid),
            )
            if derive_hr != S_OK or not appcontainer_sid.value:
                last_err = ctypes.get_last_error()
                raise AppContainerSecurityError(
                    f"Failed to derive SID for existing AppContainer profile '{config.profile_name}': "
                    f"hr=0x{derive_hr & 0xFFFFFFFF:08X}, last_err={last_err}"
                )
            is_new_profile = False
        elif hr != S_OK or not appcontainer_sid.value:
            last_err = ctypes.get_last_error()
            raise AppContainerSecurityError(
                f"CreateAppContainerProfile failed for '{config.profile_name}': "
                f"hr=0x{hr_u:08X}, last_err={last_err}"
            )
        else:
            is_new_profile = True

        sid_str_w = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(appcontainer_sid, ctypes.byref(sid_str_w)):
            cls.free_sid(appcontainer_sid.value)
            raise AppContainerSecurityError(
                f"ConvertSidToStringSidW failed for AppContainer '{config.profile_name}'"
            )

        sid_str = str(sid_str_w.value)
        kernel32.LocalFree(sid_str_w)
        return int(appcontainer_sid.value), sid_str, is_new_profile

    @classmethod
    def get_required_runtime_paths(cls) -> list[Path]:
        """Returns unique resolved paths required for Python interpreter runtime."""
        paths: list[Path] = []
        for prefix in (sys.base_prefix, sys.prefix):
            if prefix:
                try:
                    p = Path(prefix).resolve()
                    if p.exists() and p not in paths:
                        paths.append(p)
                except (OSError, ValueError):
                    pass
        return paths

    @staticmethod
    def check_effective_path_access(
        path: str | Path,
        sid_str: str,
        require_write: bool = True,
    ) -> tuple[str, str | None]:
        """Inspects effective permissions for sid_str on path via icacls.

        Returns (status, details):
          - "NONE": No explicit ACE exists for this SID.
          - "DENIED": An explicit DENY ACE exists for this SID.
          - "SUFFICIENT": An existing ALLOW ACE provides adequate permissions.
          - "INADEQUATE": An existing ALLOW ACE provides insufficient permissions (e.g. read-only when write needed).
        """
        import re

        p = Path(path).resolve()
        if not p.exists():
            return "NONE", None
        try:
            res = subprocess.run(["icacls", str(p)], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                err_msg = res.stderr.strip() or f"icacls exited with code {res.returncode}"
                return "UNREADABLE", err_msg

            pattern = rf"{re.escape(sid_str)}:([^\r\n]+)"
            matches = [m.strip() for m in re.findall(pattern, res.stdout)]
            if not matches:
                return "NONE", None

            # Explicit DENY check: any DENY or (N) for this SID blocks access
            for m in matches:
                if "(DENY)" in m or "(N)" in m:
                    return "DENIED", m

            has_full = any("(F)" in m or "(M)" in m for m in matches)
            has_read = any("(RX)" in m or "(R)" in m or "(F)" in m or "(M)" in m for m in matches)

            if require_write:
                if has_full:
                    return "SUFFICIENT", ", ".join(matches)
                return "INADEQUATE", ", ".join(matches)
            else:
                if has_read:
                    return "SUFFICIENT", ", ".join(matches)
                return "INADEQUATE", ", ".join(matches)
        except (OSError, ValueError) as exc:
            return "UNREADABLE", str(exc)

    @classmethod
    def has_path_access(cls, path: str | Path, sid_str: str, require_write: bool = True) -> bool:
        """Returns True if the specified path already has an explicit ALLOW ACE with sufficient permissions."""
        status, _ = cls.check_effective_path_access(path, sid_str, require_write=require_write)
        return status == "SUFFICIENT"

    @classmethod
    def check_runtime_path_effective_access(
        cls,
        path: str | Path,
        sid_str: str | None = None,
    ) -> tuple[str, str | None]:
        """Inspects effective read access for Python runtime paths via icacls.

        Evaluation order (fail-closed, deny-first):
          1. Explicit DENY on specific sid_str OR broad ALL APPLICATION PACKAGES / S-1-15-2-1 -> returns ("DENIED", ...).
          2. Explicit ALLOW on specific sid_str with read permission (RX, R, F, M) -> returns ("SUFFICIENT", ...).
          3. ALL APPLICATION PACKAGES / S-1-15-2-1 with read permission (RX, R, F, M) -> returns ("SUFFICIENT", ...).
          4. If matches exist but have insufficient permission (e.g. W only) -> returns ("INADEQUATE", ...).
          5. If icacls fails, access denied reading DACL, or unreadable -> returns ("UNREADABLE", ...).
          6. If no matching ACE exists -> returns ("NONE", None).
        """
        import re

        p = Path(path).resolve()
        if not p.exists():
            return "NOT_FOUND", None

        try:
            res = subprocess.run(["icacls", str(p)], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                err_msg = res.stderr.strip() or f"icacls exited with code {res.returncode}"
                return "UNREADABLE", err_msg

            out = res.stdout
            if not out.strip() or ("Failed processing" in out and "Successfully processed 0 files" in out):
                return "UNREADABLE", out.strip()

            # 1. Extract matches for specific AppContainer SID if provided
            matches_sid: list[str] = []
            if sid_str:
                pattern_sid = rf"{re.escape(sid_str)}:([^\r\n]+)"
                matches_sid = [m.strip() for m in re.findall(pattern_sid, out)]

            # 2. Extract matches for broad group: ALL APPLICATION PACKAGES or S-1-15-2-1
            pattern_pkg = r"(?:ALL APPLICATION PACKAGES|S-1-15-2-1):([^\r\n]+)"
            matches_pkg = [m.strip() for m in re.findall(pattern_pkg, out, re.IGNORECASE)]

            # 3. Evaluate DENY ACEs FIRST (both specific SID and broad package)
            # Explicit DENY takes unconditional precedence over all group or specific allowances.
            for m in matches_sid:
                if "(DENY)" in m or "(N)" in m:
                    return "DENIED", m

            for m in matches_pkg:
                if "(DENY)" in m or "(N)" in m:
                    return "DENIED", m

            # 4. Evaluate ALLOW ACEs
            has_read_sid = any(
                any(p in m for p in ("(RX)", "(R)", "(F)", "(M)"))
                for m in matches_sid
            )
            if has_read_sid:
                return "SUFFICIENT", ", ".join(matches_sid)

            has_read_pkg = any(
                any(p in m for p in ("(RX)", "(R)", "(F)", "(M)"))
                for m in matches_pkg
            )
            if has_read_pkg:
                return "SUFFICIENT", ", ".join(matches_pkg)

            if matches_pkg:
                return "INADEQUATE", ", ".join(matches_pkg)
            if matches_sid:
                return "INADEQUATE", ", ".join(matches_sid)

            return "NONE", None
        except (OSError, ValueError) as exc:
            return "UNREADABLE", str(exc)

    @classmethod
    def has_runtime_path_access(cls, path: str | Path, sid_str: str | None = None) -> bool:
        """Checks whether a single runtime path has AppContainer access (via ALL APPLICATION PACKAGES or specific SID)."""
        status, _ = cls.check_runtime_path_effective_access(path, sid_str=sid_str)
        return status == "SUFFICIENT"

    @classmethod
    def has_runtime_access(cls, sid_str: str | None = None) -> bool:
        """Returns True only if EVERY required Python runtime path is accessible to AppContainers."""
        required = cls.get_required_runtime_paths()
        if not required:
            return False
        return all(cls.has_runtime_path_access(p, sid_str) for p in required)

    @classmethod
    def ensure_runtime_access(cls, sid_str: str) -> list[Path]:
        """Ensures minimal scoped read-only access (OI)(CI)(RX) to all required Python runtime directories.

        Validates each required path individually, granting scoped access to sid_str only if needed.
        Returns list of newly granted paths so they can be tracked and revoked upon cleanup.
        Fails closed (raises AppContainerSecurityError) if any required runtime location cannot be granted or verified.
        """
        newly_granted: list[Path] = []
        required_paths = cls.get_required_runtime_paths()
        for p in required_paths:
            status, perms = cls.check_runtime_path_effective_access(p, sid_str=sid_str)
            if status == "SUFFICIENT":
                continue

            if status == "DENIED":
                for granted_p in newly_granted:
                    cls.revoke_path_access(granted_p, sid_str)
                raise AppContainerSecurityError(
                    f"Runtime directory '{p}' has an explicit pre-existing DENY ACE for SID '{sid_str}' ({perms}); "
                    f"fail-closed policy aborts launch."
                )

            if status == "UNREADABLE":
                for granted_p in newly_granted:
                    cls.revoke_path_access(granted_p, sid_str)
                raise AppContainerSecurityError(
                    f"Runtime directory '{p}' security descriptor could not be read ({perms}); "
                    f"fail-closed policy aborts launch."
                )

            # Grant minimal read-only access scoped strictly to sid_str (non-recursive for fast inheritance)
            ok = cls.grant_path_access(p, sid_str, access_mode="(OI)(CI)(RX)", recursive=False)
            if not ok:
                for granted_p in newly_granted:
                    cls.revoke_path_access(granted_p, sid_str)
                raise AppContainerSecurityError(f"Failed to grant required AppContainer runtime access to: '{p}'")
            newly_granted.append(p)
            if not cls.has_runtime_path_access(p, sid_str):
                for granted_p in newly_granted:
                    cls.revoke_path_access(granted_p, sid_str)
                raise AppContainerSecurityError(f"Runtime access verification failed after grant on: '{p}'")
        return newly_granted

    @classmethod
    def preflight_runtime_check(cls) -> tuple[bool, str]:
        """Verifies that native Windows AppContainer isolation is fully functional on the host.

        Performs an end-to-end dry-run: profile creation, SID conversion, runtime accessibility validation
        for all required prefixes, and profile deletion.
        """
        if not cls.is_supported():
            return False, "Host environment does not support Win32 AppContainer isolation APIs."
        import secrets
        probe_name = f"J_PREFLIGHT_{os.getpid()}_{secrets.token_hex(4)}"
        probe_cfg = AppContainerConfig(profile_name=probe_name, auto_cleanup_profile=False)
        try:
            p_sid, sid_str, _is_new = cls.get_or_create_profile(probe_cfg)
            runtime_grants: list[Path] = []
            try:
                runtime_grants = cls.ensure_runtime_access(sid_str)
            finally:
                for rg in runtime_grants:
                    cls.revoke_path_access(rg, sid_str)
                cls.free_sid(p_sid)
                cls.delete_profile(probe_name)
            return True, f"AppContainer runtime isolation verified (SID: {sid_str[:16]}...)"
        except (OSError, RuntimeError, AppContainerSecurityError) as exc:
            return False, f"AppContainer preflight probe failed: {exc}"

    @staticmethod
    def free_sid(p_sid: int | None) -> None:
        """Frees an allocated Windows Security Identifier (SID) safely."""
        if p_sid and advapi32:
            try:
                advapi32.FreeSid(ctypes.c_void_p(p_sid))
            except (OSError, ValueError, AttributeError) as e:
                logger.debug("FreeSid cleanup notice: %s", e)

    @classmethod
    def delete_profile(cls, profile_name: str) -> bool:
        """Deletes an AppContainer profile from the host system."""
        if not cls.is_supported():
            return False
        try:
            hr = userenv.DeleteAppContainerProfile(wintypes.LPCWSTR(profile_name))
            return hr == S_OK
        except (OSError, ValueError, AttributeError) as e:
            logger.debug("DeleteAppContainerProfile exception for '%s': %s", profile_name, e)
            return False

    @classmethod
    def profile_exists(cls, profile_name: str) -> bool:
        """Returns True if an AppContainer profile with the specified name exists on the host."""
        if not cls.is_supported():
            return False
        try:
            sid_ptr = ctypes.c_void_p()
            hr = userenv.DeriveAppContainerSidFromAppContainerName(
                wintypes.LPCWSTR(profile_name),
                ctypes.byref(sid_ptr),
            )
            if hr != S_OK or not sid_ptr.value:
                return False

            sid_str_w = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str_w)):
                cls.free_sid(sid_ptr.value)
                return False

            sid_str = str(sid_str_w.value)
            kernel32.LocalFree(sid_str_w)
            cls.free_sid(sid_ptr.value)

            # 1. Check via GetAppContainerFolderPath
            if hasattr(userenv, "GetAppContainerFolderPath"):
                try:
                    userenv.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)]
                    userenv.GetAppContainerFolderPath.restype = ctypes.c_long
                    path_p = wintypes.LPWSTR()
                    res = userenv.GetAppContainerFolderPath(wintypes.LPCWSTR(sid_str), ctypes.byref(path_p))
                    if res == S_OK:
                        if ole32 and hasattr(ole32, "CoTaskMemFree"):
                            ole32.CoTaskMemFree(path_p)
                        return True
                except Exception as e:
                    logger.debug("GetAppContainerFolderPath check notice for '%s': %s", profile_name, e)

            # 2. Check HKCU AppContainer registry mapping
            import winreg
            reg_path = rf"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings\{sid_str}"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path):
                    return True
            except OSError:
                pass

            return False
        except Exception as exc:
            logger.debug("profile_exists exception for '%s': %s", profile_name, exc)
            return False

    @staticmethod
    def grant_path_access(
        path: str | Path,
        sid_str: str,
        access_mode: str = "(OI)(CI)(F)",
        recursive: bool = False,
    ) -> bool:
        """Grants the AppContainer SID explicit access to the specified directory using icacls.

        Does NOT touch or modify any parent directories.
        """
        p = Path(path).resolve()
        if not p.exists():
            return False
        try:
            cmd = ["icacls", str(p), "/grant", f"*{sid_str}:{access_mode}"]
            if recursive:
                cmd.append("/t")
            cmd.append("/q")
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                logger.error("Failed to grant AppContainer ACL on '%s': %s", p, res.stderr)
                return False
            return True
        except (OSError, ValueError) as e:
            logger.error("Exception executing icacls grant on '%s': %s", p, e)
            return False

    @staticmethod
    def revoke_path_access(path: str | Path, sid_str: str, recursive: bool = False) -> bool:
        """Revokes the AppContainer SID access from the specified directory."""
        p = Path(path).resolve()
        if not p.exists():
            return True
        try:
            cmd = ["icacls", str(p), "/remove", f"*{sid_str}"]
            if recursive:
                cmd.append("/t")
            cmd.append("/q")
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return res.returncode == 0
        except (OSError, ValueError) as e:
            logger.debug("Exception executing icacls remove on '%s': %s", p, e)
            return False


class AppContainerProcessHandle:
    """Manages the lifetime and cleanup of a child process created inside an AppContainer."""

    def __init__(
        self,
        h_process: int,
        h_thread: int,
        pid: int,
        tid: int,
        p_sid: int,
        sid_str: str,
        capability_sids: list[int],
        config: AppContainerConfig,
        applied_grants: list[Path] | None = None,
        runtime_grants: list[Path] | None = None,
        is_new_profile: bool = True,
        attr_buf: Any = None,
    ) -> None:
        self.h_process = h_process
        self.h_thread = h_thread
        self.pid = pid
        self.tid = tid
        self.p_sid = p_sid
        self.sid_str = sid_str
        self.capability_sids = capability_sids
        self.config = config
        self.applied_grants = applied_grants or []
        self.runtime_grants = runtime_grants or []
        self.is_new_profile = is_new_profile
        self.attr_buf = attr_buf
        self._cleaned_up = False

    def resume(self) -> bool:
        """Resumes the suspended thread of the AppContainer process."""
        if not self.h_thread or kernel32 is None:
            return False
        prev_count = kernel32.ResumeThread(wintypes.HANDLE(self.h_thread))
        return prev_count != 0xFFFFFFFF

    def terminate(self, exit_code: int = 1) -> bool:
        """Terminates the AppContainer process."""
        if not self.h_process or kernel32 is None:
            return False
        return bool(kernel32.TerminateProcess(wintypes.HANDLE(self.h_process), exit_code))

    def cleanup(self) -> None:
        """Releases attribute buffers, frees SIDs, closes handles, and deletes the profile."""
        if self._cleaned_up:
            return
        self._cleaned_up = True

        if self.attr_buf and kernel32:
            try:
                kernel32.DeleteProcThreadAttributeList(self.attr_buf)
            except (OSError, ValueError, AttributeError) as e:
                logger.debug("DeleteProcThreadAttributeList cleanup notice: %s", e)
            self.attr_buf = None

        if self.p_sid:
            AppContainerProfileManager.free_sid(self.p_sid)
            self.p_sid = 0

        for cap_sid in self.capability_sids:
            if kernel32:
                try:
                    kernel32.LocalFree(ctypes.c_void_p(cap_sid))
                except (OSError, ValueError, AttributeError) as err:
                    logger.debug("LocalFree capability SID notice: %s", err)
        self.capability_sids.clear()

        # Revoke only runtime grants introduced by this invocation
        for path in self.runtime_grants:
            AppContainerProfileManager.revoke_path_access(path, self.sid_str)
        self.runtime_grants.clear()

        # Revoke only user path grants introduced by this invocation
        for path in self.applied_grants:
            AppContainerProfileManager.revoke_path_access(path, self.sid_str)
        self.applied_grants.clear()

        if self.h_thread and kernel32:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(self.h_thread))
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("CloseHandle h_thread notice: %s", err)
            self.h_thread = 0

        if self.h_process and kernel32:
            try:
                kernel32.TerminateProcess(wintypes.HANDLE(self.h_process), 1)
                kernel32.WaitForSingleObject(wintypes.HANDLE(self.h_process), 1000)
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("TerminateProcess on cleanup notice: %s", err)
            try:
                kernel32.CloseHandle(wintypes.HANDLE(self.h_process))
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("CloseHandle h_process notice: %s", err)
            self.h_process = 0

        # Only delete profile if auto_cleanup_profile is enabled AND this process created it
        if self.config.auto_cleanup_profile and self.is_new_profile:
            AppContainerProfileManager.delete_profile(self.config.profile_name)


class AppContainerProcessWrapper:
    """Popen-compatible process wrapper for an active AppContainer child process.

    Provides communicate(), poll(), wait(), terminate(), and kill() matching subprocess.Popen.
    """

    def __init__(
        self,
        handle: AppContainerProcessHandle,
        h_job: int | None = None,
        job_holder: Any | None = None,
        stdin_write: int | None = None,
        stdout_read: int | None = None,
        stderr_read: int | None = None,
        text: bool = True,
        encoding: str | None = "utf-8",
        errors: str | None = "replace",
    ) -> None:
        self._handle_obj = handle
        self._job_holder = job_holder
        self._h_job = h_job
        if self._job_holder is None and h_job is not None:
            if hasattr(h_job, "close") and hasattr(h_job, "value"):
                self._job_holder = h_job
                self._h_job = h_job.value
        elif self._job_holder is not None and self._h_job is None:
            self._h_job = getattr(self._job_holder, "value", None)

        self.pid = handle.pid
        self._h_process = handle.h_process
        self._handle = handle.h_process
        self._stdin_write = stdin_write
        self._stdout_read = stdout_read
        self._stderr_read = stderr_read
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self._text = text
        self._encoding = encoding or "utf-8"
        self._errors = errors or "replace"
        self.returncode: int | None = None
        self._terminated = False
        self._closed = False
        self._close_result = True

    @property
    def handle(self) -> AppContainerProcessHandle:
        """Returns the underlying AppContainerProcessHandle managing this process."""
        return self._handle_obj

    @property
    def h_job(self) -> int | None:
        if self._job_holder is not None:
            return self._job_holder.value
        return self._h_job

    @h_job.setter
    def h_job(self, val: Any) -> None:
        if val is None:
            self._job_holder = None
            self._h_job = None
        elif hasattr(val, "close") and hasattr(val, "value"):
            self._job_holder = val
            self._h_job = val.value
        else:
            self._h_job = int(val)

    @property
    def job_holder(self) -> Any | None:
        return self._job_holder

    @job_holder.setter
    def job_holder(self, val: Any) -> None:
        self._job_holder = val
        if val is not None and hasattr(val, "value"):
            self._h_job = val.value

    def poll(self) -> int | None:
        """Checks if child process has terminated, setting and returning returncode."""
        if self.returncode is not None:
            return self.returncode
        if not self._h_process or kernel32 is None:
            return None
        ec = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(wintypes.HANDLE(self._h_process), ctypes.byref(ec)):
            return None
        if ec.value == STILL_ACTIVE:
            return None
        self.returncode = int(ec.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Waits for child process to terminate, raising TimeoutExpired if timeout expires."""
        if self.returncode is not None:
            return self.returncode
        ms = int(timeout * 1000) if timeout is not None else 0xFFFFFFFF
        if kernel32:
            res = kernel32.WaitForSingleObject(wintypes.HANDLE(self._h_process), wintypes.DWORD(ms))
            if res == WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(self._handle_obj.config.profile_name, timeout)
            if res == WAIT_FAILED:
                last_err = ctypes.get_last_error()
                raise RuntimeError(f"WaitForSingleObject failed: WinError {last_err}")
        self.poll()
        if self.returncode is None:
            raise RuntimeError(f"Process wait completed for PID {self.pid} but exit code is unavailable.")
        return self.returncode

    def communicate(
        self,
        input: str | bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[str | bytes, str | bytes]:
        """Sends input to child stdin and drains stdout and stderr pipes without deadlocks."""
        raw_input: bytes | None = None
        if input is not None:
            raw_input = input.encode(self._encoding, errors=self._errors) if isinstance(input, str) else input

        stdin_exception: Exception | None = None

        def _write_stdin() -> None:
            nonlocal stdin_exception
            if not self._stdin_write or not kernel32:
                return
            try:
                if raw_input:
                    offset = 0
                    total = len(raw_input)
                    while offset < total:
                        chunk_len = min(65536, total - offset)
                        chunk = raw_input[offset : offset + chunk_len]
                        written = wintypes.DWORD(0)
                        ok = kernel32.WriteFile(
                            wintypes.HANDLE(self._stdin_write),
                            chunk,
                            chunk_len,
                            ctypes.byref(written),
                            None,
                        )
                        if not ok:
                            last_err = ctypes.get_last_error()
                            # 109 = ERROR_BROKEN_PIPE, 232 = ERROR_NO_DATA
                            # Child closed stdin or exited early; preserve standard broken pipe semantics
                            if last_err in (109, 232):
                                break
                            stdin_exception = RuntimeError(
                                f"WriteFile to AppContainer stdin failed: WinError {last_err} "
                                f"(wrote {offset}/{total} bytes)"
                            )
                            break
                        if written.value == 0:
                            stdin_exception = RuntimeError(
                                f"WriteFile to AppContainer stdin wrote 0 bytes at offset {offset}/{total}"
                            )
                            break
                        offset += written.value
            except (OSError, ValueError, RuntimeError) as e:
                stdin_exception = e
            finally:
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(self._stdin_write))
                except (OSError, ValueError, AttributeError) as err:
                    logger.debug("CloseHandle _stdin_write notice: %s", err)
                self._stdin_write = None

        t_in = threading.Thread(target=_write_stdin) if self._stdin_write else None
        if t_in:
            t_in.start()

        out_bytes = bytearray()
        err_bytes = bytearray()

        def _drain(h_pipe: int, acc: bytearray) -> None:
            if not h_pipe or not kernel32:
                return
            buf = ctypes.create_string_buffer(4096)
            read_bytes = wintypes.DWORD(0)
            while True:
                ok = kernel32.ReadFile(wintypes.HANDLE(h_pipe), buf, 4096, ctypes.byref(read_bytes), None)
                if not ok or read_bytes.value == 0:
                    break
                acc.extend(buf.raw[:read_bytes.value])
            kernel32.CloseHandle(wintypes.HANDLE(h_pipe))

        t_out = threading.Thread(target=_drain, args=(self._stdout_read, out_bytes)) if self._stdout_read else None
        t_err = threading.Thread(target=_drain, args=(self._stderr_read, err_bytes)) if self._stderr_read else None
        if t_out:
            t_out.start()
        if t_err:
            t_err.start()

        try:
            self.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.terminate()
            raise

        if t_in:
            t_in.join(timeout=1.0)
        if t_out:
            t_out.join(timeout=1.0)
            self._stdout_read = None
        if t_err:
            t_err.join(timeout=1.0)
            self._stderr_read = None

        if stdin_exception is not None:
            raise stdin_exception

        if self._text:
            return out_bytes.decode(self._encoding, errors=self._errors), err_bytes.decode(self._encoding, errors=self._errors)
        return bytes(out_bytes), bytes(err_bytes)

    def terminate(self, timeout: float = 2.0) -> bool:
        """Terminates the process and job tree, waiting for confirmed native process termination.

        Returns True only if the process was confirmed terminated (WAIT_OBJECT_0 and non-STILL_ACTIVE exit code).
        Returns False if wait timed out, failed, or exit code could not be verified.
        """
        if self._job_holder is not None:
            try:
                self._job_holder.terminate(1)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("job_holder terminate notice: %s", err)
        elif self._h_job and kernel32:
            try:
                kernel32.TerminateJobObject(wintypes.HANDLE(int(self._h_job)), 1)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("TerminateJobObject notice: %s", err)

        if hasattr(self, "_handle_obj") and self._handle_obj:
            try:
                self._handle_obj.terminate(1)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("handle_obj terminate notice: %s", err)

        # Wait for native process to enter signaled state
        if not self._h_process or kernel32 is None:
            return False

        ms = int(timeout * 1000) if timeout else 2000
        wait_res = None
        try:
            wait_res = kernel32.WaitForSingleObject(wintypes.HANDLE(self._h_process), wintypes.DWORD(ms))
        except (OSError, ValueError, RuntimeError) as err:
            logger.debug("WaitForSingleObject notice: %s", err)
            return False

        if wait_res == WAIT_OBJECT_0:
            ec = wintypes.DWORD(0)
            if kernel32.GetExitCodeProcess(wintypes.HANDLE(self._h_process), ctypes.byref(ec)):
                if ec.value != STILL_ACTIVE:
                    self.returncode = int(ec.value)
                    return True
                logger.warning(
                    "Process signaled termination (WAIT_OBJECT_0) but GetExitCodeProcess reported STILL_ACTIVE for PID %s",
                    self.pid,
                )
                return False
            last_err = ctypes.get_last_error()
            logger.warning("GetExitCodeProcess failed for PID %s (WinError %d)", self.pid, last_err)
            return False
        elif wait_res == WAIT_TIMEOUT:
            logger.warning("WaitForSingleObject timed out (%.2fs) waiting for termination of PID %s", timeout, self.pid)
            return False
        else:
            last_err = ctypes.get_last_error()
            logger.warning("WaitForSingleObject failed for PID %s (wait_res=%s, WinError %d)", self.pid, wait_res, last_err)
            return False

    def kill(self) -> bool:
        """Kills the process."""
        return self.terminate()

    def terminate_tree(self, exit_code: int = 1) -> bool:
        """Terminates the process and all descendant processes in the job tree."""
        term_ok = True
        if self.returncode is None:
            term_ok = self.terminate(timeout=2.0)
        close_ok = self.close()
        return term_ok and close_ok

    def close(self) -> bool:
        """Releases underlying AppContainer resources exactly once."""
        if self._closed:
            return self._close_result
        self._closed = True

        term_ok = True
        if self.returncode is None:
            term_ok = self.terminate()
        job_ok = True
        if self._job_holder is not None:
            try:
                job_ok = self._job_holder.close(exit_code=1)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("job_holder close notice: %s", err)
                job_ok = False
            self._job_holder = None
            self._h_job = None
        elif self._h_job:
            from jarvis.skills.job_object_sandbox import terminate_and_close_job
            try:
                job_ok = terminate_and_close_job(self._h_job)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("terminate_and_close_job notice: %s", err)
                job_ok = False
            self._h_job = None
        if self._stdin_write and kernel32:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(self._stdin_write))
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("CloseHandle _stdin_write notice: %s", err)
            self._stdin_write = None
        if self._stdout_read and kernel32:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(self._stdout_read))
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("CloseHandle _stdout_read notice: %s", err)
            self._stdout_read = None
        if self._stderr_read and kernel32:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(self._stderr_read))
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("CloseHandle _stderr_read notice: %s", err)
            self._stderr_read = None

        try:
            self._handle_obj.cleanup()
        except Exception as err:
            logger.debug("handle_obj cleanup notice: %s", err)

        self._close_result = term_ok and job_ok
        return self._close_result


class AppContainerProcessLauncher:
    """Spawns an isolated child process under AppContainer constraints."""

    @classmethod
    def spawn_suspended_appcontainer(
        cls,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin_handle: int | None = None,
        stdout_handle: int | None = None,
        stderr_handle: int | None = None,
        config: AppContainerConfig | None = None,
    ) -> AppContainerProcessHandle:
        """Spawns a process in CREATE_SUSPENDED state with PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES.

        Fails closed on any configuration or process creation failure.
        """
        if not AppContainerProfileManager.is_supported():
            raise AppContainerSecurityError(
                "AppContainer isolation is only supported on Windows hosts; fail-closed policy rejects fallback."
            )

        if config is None:
            config = AppContainerConfig(profile_name=f"Jarvis_Sandbox_{os.getpid()}_{id(command)}")

        # 1. Resolve Profile SID
        p_sid, sid_str, is_new_profile = AppContainerProfileManager.get_or_create_profile(config)

        # 2. Grant explicit access to approved directories with atomic rollback
        # Note: cwd is NEVER automatically added; only explicitly approved grant_paths are granted.
        # Pre-existing grants on reused profiles are preserved.
        applied_grants: list[Path] = []
        runtime_grants: list[Path] = []
        capability_sids: list[int] = []
        opened_nul_handles: list[int] = []
        attr_buf = None
        attr_list_initialized = False
        spawned_handle = None

        try:
            for path in config.grant_paths:
                p = Path(path).resolve()
                if not p.exists():
                    raise AppContainerSecurityError(f"Granted path does not exist: '{p}'")

                status, perms = AppContainerProfileManager.check_effective_path_access(p, sid_str, require_write=True)
                if status == "DENIED":
                    raise AppContainerSecurityError(
                        f"Target path '{p}' has an explicit pre-existing DENY ACE for SID '{sid_str}' ({perms}); "
                        f"cannot safely establish AppContainer write boundary without modifying security descriptor."
                    )
                elif status == "INADEQUATE":
                    raise AppContainerSecurityError(
                        f"Target path '{p}' has pre-existing insufficient permissions for SID '{sid_str}' "
                        f"(requires Full/Write '(F)' or '(M)', found '{perms}'); "
                        f"failing closed to preserve existing ACL integrity."
                    )
                elif status == "SUFFICIENT":
                    logger.debug("Path '%s' already has sufficient pre-existing access for SID '%s' (%s); preserving existing ACE", p, sid_str, perms)
                else:  # status == "NONE"
                    if not AppContainerProfileManager.grant_path_access(p, sid_str, access_mode="(OI)(CI)(F)"):
                        raise AppContainerSecurityError(f"Failed to grant AppContainer SID access to target directory: '{p}'")
                    applied_grants.append(p)

            # Ensure minimal scoped runtime access for AppContainer SID
            runtime_grants = AppContainerProfileManager.ensure_runtime_access(sid_str)

            # 3. Resolve Capability SIDs - fail closed immediately on invalid capability
            cap_structs = None
            if config.capabilities:
                cap_structs = (SID_AND_ATTRIBUTES * len(config.capabilities))()
                for idx, cap_name in enumerate(config.capabilities):
                    cap_sid_str = CAPABILITY_SID_MAP.get(cap_name, cap_name)
                    cap_sid = ctypes.c_void_p()
                    if not advapi32.ConvertStringSidToSidW(wintypes.LPCWSTR(cap_sid_str), ctypes.byref(cap_sid)) or not cap_sid.value:
                        last_err = ctypes.get_last_error()
                        raise AppContainerSecurityError(
                            f"Invalid or unresolvable capability '{cap_name}' (SID string '{cap_sid_str}'): "
                            f"ConvertStringSidToSidW failed with WinError {last_err}"
                        )
                    cap_structs[idx].Sid = cap_sid.value
                    cap_structs[idx].Attributes = 0x00000004  # SE_GROUP_ENABLED
                    capability_sids.append(int(cap_sid.value))

            sec_caps = SECURITY_CAPABILITIES()
            sec_caps.AppContainerSid = ctypes.c_void_p(p_sid)
            if cap_structs is not None:
                sec_caps.Capabilities = ctypes.cast(cap_structs, ctypes.POINTER(SID_AND_ATTRIBUTES))
                sec_caps.CapabilityCount = len(capability_sids)
            else:
                sec_caps.Capabilities = None
                sec_caps.CapabilityCount = 0
            sec_caps.Reserved = 0

            # 4. Standard handles invariant: when STARTF_USESTDHANDLES is active, ensure all three
            # handles (stdin, stdout, stderr) hold valid handles. Provide inheritable \\.\NUL device handles
            # for any unredirected or DEVNULL streams.
            handles_to_inherit: list[wintypes.HANDLE] = []
            use_std_handles = any(h is not None for h in (stdin_handle, stdout_handle, stderr_handle))

            final_stdin = stdin_handle
            final_stdout = stdout_handle
            final_stderr = stderr_handle

            if use_std_handles:
                if final_stdin is None:
                    final_stdin = _open_nul_handle(GENERIC_READ)
                    opened_nul_handles.append(final_stdin)
                if final_stdout is None:
                    final_stdout = _open_nul_handle(GENERIC_WRITE)
                    opened_nul_handles.append(final_stdout)
                if final_stderr is None:
                    final_stderr = _open_nul_handle(GENERIC_WRITE)
                    opened_nul_handles.append(final_stderr)

                handles_to_inherit = [
                    wintypes.HANDLE(final_stdin),
                    wintypes.HANDLE(final_stdout),
                    wintypes.HANDLE(final_stderr),
                ]

            attr_count = 1 + (1 if handles_to_inherit else 0)
            attr_size = ctypes.c_size_t(0)
            kernel32.InitializeProcThreadAttributeList(None, attr_count, 0, ctypes.byref(attr_size))
            attr_buf = ctypes.create_string_buffer(attr_size.value)
            if not kernel32.InitializeProcThreadAttributeList(attr_buf, attr_count, 0, ctypes.byref(attr_size)):
                last_err = ctypes.get_last_error()
                raise AppContainerSecurityError(f"InitializeProcThreadAttributeList failed: error {last_err}")
            attr_list_initialized = True

            if not kernel32.UpdateProcThreadAttribute(
                attr_buf,
                0,
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(sec_caps),
                ctypes.sizeof(sec_caps),
                None,
                None,
            ):
                last_err = ctypes.get_last_error()
                raise AppContainerSecurityError(f"UpdateProcThreadAttribute (Capabilities) failed: error {last_err}")

            if handles_to_inherit:
                handle_array = (wintypes.HANDLE * len(handles_to_inherit))(*handles_to_inherit)
                if not kernel32.UpdateProcThreadAttribute(
                    attr_buf,
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handle_array,
                    ctypes.sizeof(handle_array),
                    None,
                    None,
                ):
                    last_err = ctypes.get_last_error()
                    raise AppContainerSecurityError(f"UpdateProcThreadAttribute (HandleList) failed: error {last_err}")

            si_ex = STARTUPINFOEXW()
            si_ex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            if use_std_handles:
                si_ex.StartupInfo.dwFlags |= STARTF_USESTDHANDLES
                si_ex.StartupInfo.hStdInput = wintypes.HANDLE(final_stdin)
                si_ex.StartupInfo.hStdOutput = wintypes.HANDLE(final_stdout)
                si_ex.StartupInfo.hStdError = wintypes.HANDLE(final_stderr)

            si_ex.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)
            pi = PROCESS_INFORMATION()

            flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW
            inherit_handles = bool(handles_to_inherit)

            cmd_str = subprocess.list2cmdline(command)
            cwd_w = wintypes.LPCWSTR(str(cwd)) if cwd else None

            env_block = None
            env_buf = None
            if env is not None:
                flags |= CREATE_UNICODE_ENVIRONMENT
                env_copy = dict(env)
                if "LOCALAPPDATA" not in env_copy and "LOCALAPPDATA" in os.environ:
                    env_copy["LOCALAPPDATA"] = os.environ["LOCALAPPDATA"]
                sorted_entries = sorted(
                    [f"{k}={v}" for k, v in env_copy.items()],
                    key=lambda s: s.split("=", 1)[0].upper(),
                )
                env_str = "\0".join(sorted_entries) + "\0\0"
                env_buf = ctypes.create_unicode_buffer(env_str)
                env_block = ctypes.cast(env_buf, ctypes.c_void_p)

            cp_ok = kernel32.CreateProcessW(
                None,
                wintypes.LPWSTR(cmd_str),
                None,
                None,
                inherit_handles,
                flags,
                env_block,
                cwd_w,
                ctypes.byref(si_ex.StartupInfo),
                ctypes.byref(pi),
            )

            if not cp_ok:
                last_err = ctypes.get_last_error()
                raise AppContainerSecurityError(f"CreateProcessW in AppContainer failed: WinError {last_err}")

            spawned_handle = AppContainerProcessHandle(
                h_process=int(pi.hProcess),
                h_thread=int(pi.hThread),
                pid=int(pi.dwProcessId),
                tid=int(pi.dwThreadId),
                p_sid=p_sid,
                sid_str=sid_str,
                capability_sids=capability_sids,
                config=config,
                applied_grants=applied_grants,
                runtime_grants=runtime_grants,
                is_new_profile=is_new_profile,
            )
            return spawned_handle
        finally:
            if attr_list_initialized and attr_buf and kernel32:
                try:
                    kernel32.DeleteProcThreadAttributeList(attr_buf)
                except (OSError, ValueError, AttributeError) as err:
                    logger.debug("DeleteProcThreadAttributeList notice: %s", err)
                attr_list_initialized = False

            for h_nul in opened_nul_handles:
                if h_nul and kernel32:
                    try:
                        kernel32.CloseHandle(wintypes.HANDLE(h_nul))
                    except (OSError, ValueError, AttributeError) as err:
                        logger.debug("CloseHandle NUL notice: %s", err)

            if spawned_handle is None:
                cls._cleanup_pre_spawn(
                    p_sid=p_sid,
                    capability_sids=capability_sids,
                    config=config,
                    sid_str=sid_str,
                    applied_grants=applied_grants,
                    runtime_grants=runtime_grants,
                    is_new_profile=is_new_profile,
                )

    @classmethod
    def _cleanup_pre_spawn(
        cls,
        p_sid: int,
        capability_sids: list[int],
        config: AppContainerConfig,
        sid_str: str,
        applied_grants: list[Path] | None = None,
        runtime_grants: list[Path] | None = None,
        is_new_profile: bool = True,
    ) -> None:
        """Cleans up SIDs, profile, and ACLs if process creation aborts prior to spawn.

        Must only be invoked AFTER DeleteProcThreadAttributeList has run (or if attribute list was never initialized).
        """
        AppContainerProfileManager.free_sid(p_sid)
        if kernel32:
            for cap_sid in capability_sids:
                try:
                    kernel32.LocalFree(ctypes.c_void_p(cap_sid))
                except (OSError, ValueError, AttributeError) as err:
                    logger.debug("LocalFree capability SID notice: %s", err)
        if runtime_grants:
            for path in runtime_grants:
                AppContainerProfileManager.revoke_path_access(path, sid_str)
        if applied_grants:
            for path in applied_grants:
                AppContainerProfileManager.revoke_path_access(path, sid_str)
        if config.auto_cleanup_profile and is_new_profile:
            AppContainerProfileManager.delete_profile(config.profile_name)
