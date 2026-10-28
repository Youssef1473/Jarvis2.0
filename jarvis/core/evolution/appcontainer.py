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


# Module-level cache for runtime AppContainer access verification
_RUNTIME_ACCESS_VERIFIED: bool | None = None


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
    def preflight_runtime_check(cls) -> tuple[bool, str]:
        """Verifies that native Windows AppContainer isolation is fully functional on the host.

        Performs an end-to-end dry-run: profile creation, SID conversion, and profile deletion.
        """
        if not cls.is_supported():
            return False, "Host environment does not support Win32 AppContainer isolation APIs."
        import secrets
        probe_name = f"J_PREFLIGHT_{os.getpid()}_{secrets.token_hex(4)}"
        probe_cfg = AppContainerConfig(profile_name=probe_name, auto_cleanup_profile=False)
        try:
            p_sid, sid_str, _is_new = cls.get_or_create_profile(probe_cfg)
            cls.free_sid(p_sid)
            cls.delete_profile(probe_name)
            return True, f"AppContainer runtime isolation verified (SID: {sid_str[:16]}...)"
        except (OSError, RuntimeError, AppContainerSecurityError) as exc:
            return False, f"AppContainer preflight probe failed: {exc}"

    @classmethod
    def has_runtime_access(cls) -> bool:
        """Returns True if the Python runtime directory is already accessible to AppContainers."""
        global _RUNTIME_ACCESS_VERIFIED
        if _RUNTIME_ACCESS_VERIFIED is not None:
            return _RUNTIME_ACCESS_VERIFIED
        try:
            p = str(Path(sys.prefix).resolve())
            res = subprocess.run(["icacls", p], capture_output=True, text=True, check=False)
            _RUNTIME_ACCESS_VERIFIED = "ALL APPLICATION PACKAGES" in res.stdout or "S-1-15-2-1" in res.stdout
        except (OSError, ValueError):
            _RUNTIME_ACCESS_VERIFIED = False
        return bool(_RUNTIME_ACCESS_VERIFIED)

    @classmethod
    def ensure_runtime_access(cls, sid_str: str) -> list[Path]:
        """Ensures minimal scoped read-only access (OI)(CI)(RX) to Python runtime directories.

        Returns list of newly granted paths so they can be tracked and revoked upon cleanup.
        """
        runtime_paths: list[Path] = []
        for prefix in (sys.base_prefix, sys.prefix):
            p = Path(prefix).resolve()
            if (
                p.exists()
                and p not in runtime_paths
                and cls.grant_path_access(p, sid_str, access_mode="(OI)(CI)(RX)", recursive=False)
            ):
                runtime_paths.append(p)
        return runtime_paths

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

        # Revoke runtime grants
        for path in self.runtime_grants:
            AppContainerProfileManager.revoke_path_access(path, self.sid_str)
        self.runtime_grants.clear()

        # Revoke path access from explicitly granted paths
        for path in self.config.grant_paths:
            AppContainerProfileManager.revoke_path_access(path, self.sid_str)

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
        stdin_write: int | None = None,
        stdout_read: int | None = None,
        stderr_read: int | None = None,
        text: bool = True,
        encoding: str | None = "utf-8",
        errors: str | None = "replace",
    ) -> None:
        self._handle_obj = handle
        self._h_job = h_job
        self.pid = handle.pid
        self._h_process = handle.h_process
        self._handle = handle.h_process
        self.h_job = h_job
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

        def _write_stdin() -> None:
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
                        if not ok or written.value == 0:
                            break
                        offset += written.value
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

        if self._text:
            return out_bytes.decode(self._encoding, errors=self._errors), err_bytes.decode(self._encoding, errors=self._errors)
        return bytes(out_bytes), bytes(err_bytes)

    def terminate(self) -> None:
        """Terminates the process and job tree."""
        if self._h_job:
            from jarvis.skills.job_object_sandbox import terminate_and_close_job
            try:
                terminate_and_close_job(self._h_job)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("terminate_and_close_job notice: %s", err)
            self._h_job = None
        self._handle_obj.terminate(1)
        self.returncode = 1

    def kill(self) -> None:
        """Kills the process."""
        self.terminate()

    def terminate_tree(self, exit_code: int = 1) -> bool:
        """Terminates the process and all descendant processes in the job tree."""
        self.terminate()
        return True

    def close(self) -> None:
        """Releases underlying AppContainer resources."""
        self.terminate()
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
        self._handle_obj.cleanup()


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
        applied_grants: list[Path] = []
        runtime_grants: list[Path] = []
        try:
            for path in config.grant_paths:
                p = Path(path).resolve()
                if not p.exists():
                    raise AppContainerSecurityError(f"Granted path does not exist: '{p}'")
                if not AppContainerProfileManager.grant_path_access(p, sid_str, access_mode="(OI)(CI)(F)"):
                    raise AppContainerSecurityError(f"Failed to grant AppContainer SID access to target directory: '{p}'")
                applied_grants.append(p)

            # Ensure minimal scoped runtime access for AppContainer SID if not already accessible
            if not AppContainerProfileManager.has_runtime_access():
                runtime_grants = AppContainerProfileManager.ensure_runtime_access(sid_str)
        except Exception:
            for p in applied_grants:
                AppContainerProfileManager.revoke_path_access(p, sid_str)
            for p in runtime_grants:
                AppContainerProfileManager.revoke_path_access(p, sid_str)
            AppContainerProfileManager.free_sid(p_sid)
            if config.auto_cleanup_profile and is_new_profile:
                AppContainerProfileManager.delete_profile(config.profile_name)
            raise

        # 3. Resolve Capability SIDs
        capability_sids: list[int] = []
        cap_array = None
        if config.capabilities:
            cap_structs = (SID_AND_ATTRIBUTES * len(config.capabilities))()
            for idx, cap_name in enumerate(config.capabilities):
                cap_sid_str = CAPABILITY_SID_MAP.get(cap_name, cap_name)
                cap_sid = ctypes.c_void_p()
                if advapi32.ConvertStringSidToSidW(wintypes.LPCWSTR(cap_sid_str), ctypes.byref(cap_sid)):
                    cap_structs[idx].Sid = cap_sid.value
                    cap_structs[idx].Attributes = 0x00000004  # SE_GROUP_ENABLED
                    capability_sids.append(int(cap_sid.value))
                else:
                    logger.warning("Unrecognized capability SID string: '%s'", cap_sid_str)
            cap_array = cap_structs

        sec_caps = SECURITY_CAPABILITIES()
        sec_caps.AppContainerSid = ctypes.c_void_p(p_sid)
        if cap_array is not None:
            sec_caps.Capabilities = ctypes.cast(cap_array, ctypes.POINTER(SID_AND_ATTRIBUTES))
            sec_caps.CapabilityCount = len(config.capabilities)
        else:
            sec_caps.Capabilities = None
            sec_caps.CapabilityCount = 0
        sec_caps.Reserved = 0

        # 4. Standard handles invariant: when STARTF_USESTDHANDLES is active, ensure all three
        # handles (stdin, stdout, stderr) hold valid handles. Provide inheritable \\.\NUL device handles
        # for any unredirected or DEVNULL streams.
        opened_nul_handles: list[int] = []
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

        attr_buf = None
        handle_array = None
        try:
            attr_count = 1 + (1 if handles_to_inherit else 0)
            attr_size = ctypes.c_size_t(0)
            kernel32.InitializeProcThreadAttributeList(None, attr_count, 0, ctypes.byref(attr_size))
            attr_buf = ctypes.create_string_buffer(attr_size.value)
            if not kernel32.InitializeProcThreadAttributeList(attr_buf, attr_count, 0, ctypes.byref(attr_size)):
                last_err = ctypes.get_last_error()
                cls._cleanup_pre_spawn(p_sid, capability_sids, config, sid_str, runtime_grants, is_new_profile)
                raise AppContainerSecurityError(f"InitializeProcThreadAttributeList failed: error {last_err}")

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
                cls._cleanup_pre_spawn(p_sid, capability_sids, config, sid_str, runtime_grants, is_new_profile)
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
                    cls._cleanup_pre_spawn(p_sid, capability_sids, config, sid_str, runtime_grants, is_new_profile)
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
                cls._cleanup_pre_spawn(p_sid, capability_sids, config, sid_str, runtime_grants, is_new_profile)
                raise AppContainerSecurityError(f"CreateProcessW in AppContainer failed: WinError {last_err}")

            return AppContainerProcessHandle(
                h_process=int(pi.hProcess),
                h_thread=int(pi.hThread),
                pid=int(pi.dwProcessId),
                tid=int(pi.dwThreadId),
                p_sid=p_sid,
                sid_str=sid_str,
                capability_sids=capability_sids,
                config=config,
                runtime_grants=runtime_grants,
                is_new_profile=is_new_profile,
            )
        finally:
            if attr_buf and kernel32:
                try:
                    kernel32.DeleteProcThreadAttributeList(attr_buf)
                except (OSError, ValueError, AttributeError) as err:
                    logger.debug("DeleteProcThreadAttributeList notice: %s", err)
            for h_nul in opened_nul_handles:
                if h_nul and kernel32:
                    try:
                        kernel32.CloseHandle(wintypes.HANDLE(h_nul))
                    except (OSError, ValueError, AttributeError) as err:
                        logger.debug("CloseHandle NUL notice: %s", err)

    @classmethod
    def _cleanup_pre_spawn(
        cls,
        p_sid: int,
        capability_sids: list[int],
        config: AppContainerConfig,
        sid_str: str,
        runtime_grants: list[Path] | None = None,
        is_new_profile: bool = True,
    ) -> None:
        """Cleans up SIDs, profile, and ACLs if process creation aborts prior to spawn."""
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
        for path in config.grant_paths:
            AppContainerProfileManager.revoke_path_access(path, sid_str)
        if config.auto_cleanup_profile and is_new_profile:
            AppContainerProfileManager.delete_profile(config.profile_name)
