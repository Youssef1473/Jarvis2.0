"""Low-level Win32 TOCTOU mitigation, handle containment, and workspace boundary enforcement."""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.Skills.FsConfinement")

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent


def _get_dev_shell_symbol(name: str, fallback: Any) -> Any:
    """Dynamically resolves a symbol from jarvis.skills.dev_shell to support mock patching."""
    mod = sys.modules.get("jarvis.skills.dev_shell")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def _resolve_workspace_path(file_path: str, for_write: bool = False) -> Path:
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        validate_confined_path,
    )
    clean = str(file_path).strip()
    p = Path(clean)
    target = str(WORKSPACE_DIR / p) if not p.is_absolute() else clean
    try:
        safe_path = validate_confined_path(target, str(WORKSPACE_DIR))
    except SecurityBoundaryViolation as err:
        raise ValueError(f"Path must stay inside the Jarvis workspace: {err}") from err
    resolved = Path(safe_path)
    if for_write:
        resolved_norm = resolved.resolve()
        custom_dir = (WORKSPACE_DIR / "jarvis" / "skills" / "custom").resolve()
        core_dir = (WORKSPACE_DIR / "jarvis" / "core").resolve()
        git_dir = (WORKSPACE_DIR / ".git").resolve()
        run_file = (WORKSPACE_DIR / "run.py").resolve()

        if resolved_norm == custom_dir or custom_dir in resolved_norm.parents:
            raise ValueError(
                "Security Violation: Direct modification of 'jarvis/skills/custom/' via generic file tools is prohibited. "
                "Custom skills must go through the dedicated self-evolution pipeline."
            )

        if resolved_norm == git_dir or git_dir in resolved_norm.parents:
            raise ValueError("Security Violation: Direct modification of '.git/' is prohibited.")

        if resolved_norm == run_file or resolved_norm == core_dir or core_dir in resolved_norm.parents:
            from jarvis.core.security_boundaries import (
                DualKeyApprovalManager,
                _active_auth_context,
            )
            mgr = DualKeyApprovalManager.get_instance()
            token = getattr(mgr, "_local", None) and getattr(mgr._local, "authorized_token", None)
            active_ctx = _active_auth_context.get()
            if not active_ctx and not token:
                raise ValueError(
                    f"Security Violation: Modifying core system file or directory '{resolved_norm.name}' requires explicit DualKey human authorization."
                )
    return resolved


def _compute_file_sha256(path: Path) -> str | None:
    """Computes SHA-256 hex digest of file contents, returning None if file does not exist."""
    if not path.is_file():
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, ValueError) as exc:
        logger.debug("Failed to compute SHA256 for %s: %s", path, exc)
        return None


def _is_reparse_point_or_symlink(path: Path) -> bool:
    """Detects symlinks, directory junctions, and Windows reparse points."""
    try:
        if path.is_symlink():
            return True
    except OSError as exc:
        logger.debug("is_symlink check failed on %s: %s", path, exc)

    try:
        st = os.lstat(path)
        if getattr(st, "st_file_attributes", 0) & 0x0400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return True
    except (FileNotFoundError, OSError):
        pass

    try:
        curr = path.parent
        while curr != WORKSPACE_DIR and curr != curr.parent:
            if curr.is_symlink():
                return True
            try:
                pst = os.lstat(curr)
                if getattr(pst, "st_file_attributes", 0) & 0x0400:
                    return True
            except (FileNotFoundError, OSError):
                pass
            curr = curr.parent
    except OSError as exc:
        logger.debug("Parent walk reparse check failed on %s: %s", path, exc)

    return False


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class IO_STATUS_BLOCK(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [
                ("Status", ctypes.c_long),
                ("Pointer", wintypes.LPVOID),
            ]
        _anonymous_ = ("_u",)
        _fields_ = [
            ("_u", _U),
            ("Information", ctypes.c_size_t),
        ]

    class FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _ntdll.NtCreateFile.restype = ctypes.c_long
    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]

    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetFileInformationByHandle.restype = wintypes.BOOL
    _k32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    ]
    _k32.SetFileInformationByHandle.restype = wintypes.BOOL
    _k32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _k32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.argtypes = []
    _k32.DuplicateHandle.restype = wintypes.BOOL
    _k32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]


def _acquire_confined_directory_handle(dir_path: Path) -> tuple[Any, Path]:
    """Validates and acquires an open handle to a directory strictly confined within WORKSPACE_DIR.

    Returns (h_dir, canonical_physical_path).
    Fails closed with SecurityBoundaryViolation if the directory is a symlink, reparse point,
    resolves outside WORKSPACE_DIR, or points into protected paths.
    """
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        validate_confined_path,
    )

    if _is_reparse_point_or_symlink(dir_path):
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Parent directory '{dir_path}' contains a symbolic link or reparse point."
        )

    if sys.platform != "win32":
        safe_path = validate_confined_path(str(dir_path), str(WORKSPACE_DIR))
        return None, Path(safe_path).resolve()

    FILE_SHARE_ALL = 0x00000007  # READ | WRITE | DELETE
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    QUERY_TRAVERSE = 0x0001 | 0x0020 | 0x00100000 | 0x0080

    h_dir = _k32.CreateFileW(
        str(dir_path),
        QUERY_TRAVERSE,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )

    if not h_dir or h_dir == wintypes.HANDLE(-1).value or h_dir == -1:
        if not dir_path.exists():
            safe_path = validate_confined_path(str(dir_path), str(WORKSPACE_DIR))
            return None, Path(safe_path).resolve()
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Failed to obtain handle to directory '{dir_path}' for confinement validation."
        )

    try:
        buf = ctypes.create_unicode_buffer(1024)
        res = _k32.GetFinalPathNameByHandleW(h_dir, buf, 1024, 0)
        if res == 0:
            raise SecurityBoundaryViolation(
                f"Security Boundary Violation: GetFinalPathNameByHandleW failed for directory '{dir_path}'."
            )
        path_str = buf.value
        if path_str.startswith("\\\\?\\UNC\\"):
            path_str = "\\\\" + path_str[8:]
        elif path_str.startswith("\\\\?\\"):
            path_str = path_str[4:]

        safe_path = validate_confined_path(path_str, str(WORKSPACE_DIR))
        canon_p = Path(safe_path).resolve()
        custom_dir = (WORKSPACE_DIR / "jarvis" / "skills" / "custom").resolve()
        core_dir = (WORKSPACE_DIR / "jarvis" / "core").resolve()
        git_dir = (WORKSPACE_DIR / ".git").resolve()

        if canon_p == custom_dir or custom_dir in canon_p.parents:
            raise SecurityBoundaryViolation(
                "Security Boundary Violation: Directory points to 'jarvis/skills/custom/' which is protected."
            )
        if canon_p == git_dir or git_dir in canon_p.parents:
            raise SecurityBoundaryViolation("Security Boundary Violation: Directory points to '.git/' which is protected.")
        if canon_p == core_dir or core_dir in canon_p.parents:
            from jarvis.core.security_boundaries import (
                DualKeyApprovalManager,
                _active_auth_context,
            )

            mgr = DualKeyApprovalManager.get_instance()
            token = getattr(mgr, "_local", None) and getattr(mgr._local, "authorized_token", None)
            active_ctx = _active_auth_context.get()
            if not active_ctx and not token:
                raise SecurityBoundaryViolation(
                    f"Security Boundary Violation: Modifying core system directory '{canon_p.name}' requires explicit DualKey human authorization."
                )

        return h_dir, canon_p
    except Exception:
        _k32.CloseHandle(h_dir)
        raise


def _verify_directory_confinement(dir_path: Path) -> None:
    """Validates that a directory path and its underlying physical handle reside safely inside WORKSPACE_DIR."""
    h_dir, _ = _acquire_confined_directory_handle(dir_path)
    if h_dir is not None and sys.platform == "win32":
        _k32.CloseHandle(h_dir)


def _compute_fd_sha256(fd: int) -> str:
    """Computes SHA-256 hex digest directly through an open file descriptor from offset 0."""
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while chunk := os.read(fd, 65536):
        h.update(chunk)
    os.lseek(fd, pos, os.SEEK_SET)
    return h.hexdigest()


def _get_handle_physical_path(fd: int) -> str | None:
    """Returns the kernel-canonicalized physical path of an open file descriptor on Windows.

    Uses GetFinalPathNameByHandleW, stripping kernel volume prefixes.
    """
    if sys.platform != "win32":
        return None
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        buf = ctypes.create_unicode_buffer(1024)
        res = _k32.GetFinalPathNameByHandleW(wintypes.HANDLE(handle), buf, 1024, 0)
        if res == 0:
            return None
        path_str = buf.value
        if path_str.startswith("\\\\?\\UNC\\"):
            path_str = "\\\\" + path_str[8:]
        elif path_str.startswith("\\\\?\\"):
            path_str = path_str[4:]
        return path_str
    except (OSError, ValueError, AttributeError) as exc:
        logger.debug("Failed to get physical path from handle: %s", exc)
        return None


def _verify_open_file_handle(fd: int, target: Path, for_write: bool = True) -> None:
    """Validates that an opened file descriptor genuinely refers to an authorized path inside workspace.

    Fails closed with SecurityBoundaryViolation if:
    1. The file descriptor attributes indicate a reparse point (0x0400).
    2. GetFinalPathNameByHandleW resolves outside WORKSPACE_DIR.
    3. The final path points into protected areas (.git, custom skills, core, run.py).
    """
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        validate_confined_path,
    )

    # 1. Attribute check on the open handle itself
    try:
        st = os.stat(fd)
        if getattr(st, "st_file_attributes", 0) & 0x0400:
            raise SecurityBoundaryViolation(
                f"Security Boundary Violation: Destination handle for '{target.name}' is a symbolic link or reparse point; file mutation rejected."
            )
    except OSError as os_err:
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Failed to inspect file handle for '{target.name}': {os_err}"
        ) from os_err

    # 2. Kernel-level canonical destination check on Windows
    resolve_path_fn = _get_dev_shell_symbol("_get_handle_physical_path", _get_handle_physical_path)
    physical_path = resolve_path_fn(fd)
    if physical_path:
        try:
            safe_path = validate_confined_path(physical_path, str(WORKSPACE_DIR))
        except SecurityBoundaryViolation as err:
            raise SecurityBoundaryViolation(
                f"Security Boundary Violation: Destination handle resolved outside workspace: {err}"
            ) from err

        # Check protected paths
        if for_write:
            canon_p = Path(safe_path).resolve()
            custom_dir = (WORKSPACE_DIR / "jarvis" / "skills" / "custom").resolve()
            core_dir = (WORKSPACE_DIR / "jarvis" / "core").resolve()
            git_dir = (WORKSPACE_DIR / ".git").resolve()
            run_file = (WORKSPACE_DIR / "run.py").resolve()

            if canon_p == custom_dir or custom_dir in canon_p.parents:
                raise SecurityBoundaryViolation(
                    "Security Boundary Violation: Handle points to 'jarvis/skills/custom/' which is protected."
                )
            if canon_p == git_dir or git_dir in canon_p.parents:
                raise SecurityBoundaryViolation("Security Boundary Violation: Handle points to '.git/' which is protected.")
            if canon_p == run_file or canon_p == core_dir or core_dir in canon_p.parents:
                from jarvis.core.security_boundaries import (
                    DualKeyApprovalManager,
                    _active_auth_context,
                )
                mgr = DualKeyApprovalManager.get_instance()
                token = getattr(mgr, "_local", None) and getattr(mgr._local, "authorized_token", None)
                active_ctx = _active_auth_context.get()
                if not active_ctx and not token:
                    raise SecurityBoundaryViolation(
                        f"Security Boundary Violation: Modifying core system file or directory '{canon_p.name}' requires explicit DualKey human authorization."
                    )


@contextlib.contextmanager
def open_handle_bound_workspace_file(
    target: Path,
    overwrite: bool = False,
    must_exist: bool = False,
) -> Generator[tuple[int, bool], None, None]:
    """Acquires a verified directory handle and opens the child file handle-relative.

    Ensures the validated directory object and the file creation/open belong to one
    continuous handle-bound operation with zero path-resolution TOCTOU vulnerability.
    Yields (fd, was_created).
    If validation fails after file creation, deletion occurs strictly through the open
    file handle via SetFileInformationByHandle without path-based resolution.
    """
    from jarvis.core.security_boundaries import SecurityBoundaryViolation

    # Pre-verification check
    verify_dir_fn = _get_dev_shell_symbol("_verify_directory_confinement", _verify_directory_confinement)
    verify_dir_fn(target.parent)

    verify_file_fn = _get_dev_shell_symbol("_verify_open_file_handle", _verify_open_file_handle)

    if sys.platform != "win32":
        # POSIX directory-relative opening
        dirfd = os.open(str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            flags = os.O_RDWR
            if must_exist:
                pass
            elif not overwrite:
                flags |= os.O_CREAT | os.O_EXCL
            else:
                flags |= os.O_CREAT
            was_absent = not target.exists()
            fd = os.open(target.name, flags, dir_fd=dirfd)
            try:
                verify_file_fn(fd, target, for_write=True)
                yield fd, was_absent
            except Exception:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
        finally:
            os.close(dirfd)
        return

    # Windows NtCreateFile handle-relative opening
    target.parent.mkdir(parents=True, exist_ok=True)
    h_dir, _ = _acquire_confined_directory_handle(target.parent)

    try:
        if must_exist:
            disposition = 1  # FILE_OPEN (must exist)
        elif not overwrite:
            disposition = 2  # FILE_CREATE (must not exist)
        else:
            disposition = 3  # FILE_OPEN_IF (create if absent, open if exists)

        target_filename = target.name
        u_str = UNICODE_STRING(len(target_filename) * 2, len(target_filename) * 2 + 2, target_filename)
        oa = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), h_dir, ctypes.pointer(u_str), 0x40, None, None)
        iosb = IO_STATUS_BLOCK()
        file_handle = wintypes.HANDLE()

        # DesiredAccess: GENERIC_READ | GENERIC_WRITE | SYNCHRONIZE | DELETE (0x00010000)
        # CreateOptions: SYNCHRONOUS_NONALERT | NON_DIR | OPEN_REPARSE_POINT (0x00200000)
        status = _ntdll.NtCreateFile(
            ctypes.byref(file_handle),
            0x80000000 | 0x40000000 | 0x00100000 | 0x00010000,
            ctypes.byref(oa),
            ctypes.byref(iosb),
            None,
            0x80,  # FILE_ATTRIBUTE_NORMAL
            7,     # FILE_SHARE_ALL
            disposition,
            0x20 | 0x40 | 0x00200000,
            None,
            0,
        )

        if status == 0xC0000035:  # STATUS_OBJECT_NAME_COLLISION
            raise SecurityBoundaryViolation(
                f"Preimage Integrity Violation: Destination file '{target.name}' already exists when creating new file. Unexpected file creation rejected."
            )
        if status == 0xC0000034:  # STATUS_OBJECT_NAME_NOT_FOUND
            raise FileNotFoundError(f"File does not exist: {target}")
        if status != 0:
            raise SecurityBoundaryViolation(
                f"Security Boundary Violation: Failed to open handle-relative file '{target.name}': NTSTATUS {hex(status & 0xFFFFFFFF)}"
            )

        was_created = (iosb.Information == 2)
        fd = None
        dup_handle = None

        if was_created:
            dup = wintypes.HANDLE()
            if _k32.DuplicateHandle(
                _k32.GetCurrentProcess(),
                file_handle,
                _k32.GetCurrentProcess(),
                ctypes.byref(dup),
                0x00010000 | 0x80000000,  # DELETE | GENERIC_READ
                False,
                0,
            ):
                dup_handle = dup

        try:
            # 1. Inspect reparse point attributes on the open file handle
            bhfi = BY_HANDLE_FILE_INFORMATION()
            if _k32.GetFileInformationByHandle(file_handle, ctypes.byref(bhfi)) and (bhfi.dwFileAttributes & 0x0400):  # FILE_ATTRIBUTE_REPARSE_POINT
                raise SecurityBoundaryViolation(
                    f"Security Boundary Violation: Destination handle for '{target.name}' is a symbolic link or reparse point; file mutation rejected."
                )

            # 2. Convert handle to CRT file descriptor
            fd = msvcrt.open_osfhandle(file_handle.value, os.O_RDWR)
            # 3. Verify open file descriptor confinement
            verify_file_fn(fd, target, for_write=True)

            yield fd, was_created
        except Exception:
            # Atomic deletion of newly created file through the handle without path resolution
            if was_created:
                disp_info = FILE_DISPOSITION_INFO(True)
                target_h = dup_handle or file_handle
                if target_h and getattr(target_h, "value", None):
                    with contextlib.suppress(OSError):
                        _k32.SetFileInformationByHandle(target_h, 4, ctypes.byref(disp_info), ctypes.sizeof(disp_info))
                elif fd is not None:
                    with contextlib.suppress(OSError):
                        raw_h = msvcrt.get_osfhandle(fd)
                        _k32.SetFileInformationByHandle(wintypes.HANDLE(raw_h), 4, ctypes.byref(disp_info), ctypes.sizeof(disp_info))
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
            elif file_handle and file_handle.value:
                with contextlib.suppress(OSError):
                    _k32.CloseHandle(file_handle)
                file_handle = None
            raise
        finally:
            if dup_handle and getattr(dup_handle, "value", None):
                with contextlib.suppress(OSError):
                    _k32.CloseHandle(dup_handle)
                dup_handle = None
    finally:
        if h_dir is not None:
            _k32.CloseHandle(h_dir)
