"""Developer, codebase stewardship, and shell execution tools for Windows."""
import contextlib
import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

from jarvis.skills.base import SkillRegistry
from jarvis.skills.job_object_sandbox import run_sandboxed_batch

logger = logging.getLogger("Jarvis.Skills.DevShell")

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent


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


# execute_powershell uses the Job Object for *lifecycle* containment (the whole
# process tree dies on timeout/cancel), not for CPU throttling, so these caps are
# deliberately looser than the E-core-pinned execute_sandboxed_powershell tool.
POWERSHELL_JOB_MAX_MEMORY_MB = 4096
POWERSHELL_JOB_MAX_PROCESSES = 64
POWERSHELL_OUTPUT_TAIL_CHARS = 16000


@SkillRegistry.register(
    name="execute_powershell",
    description="Executes a PowerShell terminal command safely inside the Jarvis workspace and returns stdout and stderr.",
    risk_level=2,
)
def execute_powershell(command: str, timeout_seconds: int = 25) -> str:
    """Executes a PowerShell command inside a Job Object so every child it spawns dies with it."""
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: execute_powershell requires explicit human approval."
        )
    if active_token.tool_name != "execute_powershell":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'execute_powershell'."
        )
    clean_args = {"command": command}
    expected_hash = compute_action_hash("execute_powershell", clean_args)
    if not hmac.compare_digest(active_token.action_hash, expected_hash):
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: Command arguments do not match approved action hash (anti-tampering violation)."
        )

    if not active_token.consume_for_process({"execute_powershell"}):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: execute_powershell authorization grant was already consumed or invalidated."
        )

    from jarvis.core.cancellation import get_current_tool_cancellation_token
    from jarvis.skills.job_object_sandbox import run_job_sandboxed_process

    token = get_current_tool_cancellation_token()
    if token is not None:
        if token.is_cancelled:
            return "Error: Command cancelled before launch: execution deadline expired or interrupt signaled."
        if token.deadline is not None:
            remaining = token.remaining_time()
            if remaining <= 0.0:
                return "Error: Command cancelled before launch: execution deadline expired."
            effective_timeout = min(float(timeout_seconds), remaining)
        else:
            effective_timeout = float(timeout_seconds)
    else:
        effective_timeout = float(timeout_seconds)

    res = run_job_sandboxed_process(
        command=["powershell", "-NoProfile", "-Command", command],
        cwd=str(WORKSPACE_DIR),
        max_memory_mb=POWERSHELL_JOB_MAX_MEMORY_MB,
        max_processes=POWERSHELL_JOB_MAX_PROCESSES,
        timeout_sec=effective_timeout,
        requested_affinity=0,  # no core pinning for general shell work
        cancel_token=token,
        tail_chars=POWERSHELL_OUTPUT_TAIL_CHARS,
    )
    status = res.get("status")
    has_output = bool((res.get("stdout_tail") or "").strip() or (res.get("stderr_tail") or "").strip())
    if status in ("ok", "failed", "timeout", "cancelled") or has_output:
        # Subprocess output boundary: Taint active turn before returning stdout/stderr/diagnostics to LLM
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().mark_tainted(
            source="subprocess_output:execute_powershell",
            metadata={"command": command[:64], "status": status}
        )
    if status == "timeout":
        timeout_disp = int(effective_timeout) if float(effective_timeout).is_integer() else f"{effective_timeout:.1f}"
        return f"Error: Command timed out after {timeout_disp} seconds."
    if status == "cancelled":
        return "Error: Command cancelled by interrupt."
    if status not in ("ok", "failed"):
        return f"Failed to execute command: {res.get('reason') or res.get('stderr_tail') or status}"

    stdout = (res.get("stdout_tail") or "").strip()
    stderr = (res.get("stderr_tail") or "").strip()
    out = []
    if stdout:
        out.append(f"STDOUT:\n{stdout}")
    if stderr:
        out.append(f"STDERR:\n{stderr}")
        if "being used by another process" in stderr or "WinError 32" in stderr:
            try:
                from jarvis.skills.rstrtmgr_lock_surgeon import inspect_file_locks
                tokens = [t.strip("\"'") for t in command.split() if "." in t and not t.startswith("-")]
                for tok in tokens:
                    if os.path.exists(tok):
                        locks = inspect_file_locks(tok)
                        if locks.get("status") == "locked":
                            holders = [f"PID {h['pid']} ({h['app_name']})" for h in locks.get("holders", [])]
                            out.append(f"LOCK DIAGNOSTIC (Rstrtmgr): '{tok}' locked by {', '.join(holders)}")
            except Exception:
                pass
    return "\n\n".join(out) if out else "Command completed with no output."


@SkillRegistry.register(
    name="execute_sandboxed_powershell",
    description="Executes a PowerShell command inside a kernel Job Object pinned to E-Cores (0xFF00) with hard memory caps.",
    risk_level=1,
)
def execute_sandboxed_powershell(command: str, max_memory_mb: int = 1536, timeout_seconds: int = 25) -> str:
    """Runs PowerShell inside an isolated Win32 Job Object shielding P-cores."""
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: execute_sandboxed_powershell requires explicit human approval."
        )
    if active_token.tool_name != "execute_sandboxed_powershell":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'execute_sandboxed_powershell'."
        )
    clean_args = {"command": command}
    expected_hash = compute_action_hash("execute_sandboxed_powershell", clean_args)
    if not hmac.compare_digest(active_token.action_hash, expected_hash):
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: Command arguments do not match approved action hash (anti-tampering violation)."
        )

    if not active_token.consume_for_process({"execute_sandboxed_powershell"}):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: execute_sandboxed_powershell authorization grant was already consumed or invalidated."
        )

    from jarvis.core.cancellation import get_current_tool_cancellation_token
    from jarvis.skills.job_object_sandbox import run_job_sandboxed_process

    token = get_current_tool_cancellation_token()
    if token is not None:
        if token.is_cancelled:
            return "Error: Command cancelled before launch: execution deadline expired or interrupt signaled."
        if token.deadline is not None:
            remaining = token.remaining_time()
            if remaining <= 0.0:
                return "Error: Command cancelled before launch: execution deadline expired."
            effective_timeout = min(float(timeout_seconds), remaining)
        else:
            effective_timeout = float(timeout_seconds)
    else:
        effective_timeout = float(timeout_seconds)

    res = run_job_sandboxed_process(
        command=["powershell", "-NoProfile", "-Command", command],
        cwd=str(WORKSPACE_DIR),
        max_memory_mb=max_memory_mb,
        timeout_sec=effective_timeout,
        cancel_token=token,
    )
    status = res.get("status")
    has_output = bool((res.get("stdout_tail") or "").strip() or (res.get("stderr_tail") or "").strip())
    if status in ("ok", "failed", "timeout", "cancelled") or has_output:
        # Subprocess output boundary: Taint active turn before returning stdout/stderr/diagnostics to LLM
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().mark_tainted(
            source="subprocess_output:execute_sandboxed_powershell",
            metadata={"command": command[:64], "status": status}
        )
    if res.get("status") == "ok":
        out = []
        if res.get("stdout_tail"):
            out.append(f"STDOUT:\n{res['stdout_tail']}")
        if res.get("stderr_tail"):
            out.append(f"STDERR:\n{res['stderr_tail']}")
        out.append(f"[Job Object: {res.get('peak_job_memory_mb', 0)}MB peak RAM | {res.get('duration_ms', 0)}ms | E-Core mask: {res.get('affinity_mask')}]")
        return "\n\n".join(out)
    return f"Sandboxed execution {res.get('status')}: {res.get('stderr_tail') or res.get('reason')}"


@SkillRegistry.register(
    name="list_directory",
    description="Lists files and subdirectories in a given folder path within the codebase."
)
def list_directory(folder_path: str = ".") -> str:
    """Lists files and folders with size in directory."""
    target = _resolve_workspace_path(folder_path)
    if not target.exists():
        return f"Directory does not exist: {target}"
    try:
        items = []
        for p in sorted(target.iterdir()):
            if p.name in (".git", "__pycache__", ".pytest_cache"):
                continue
            if p.is_dir():
                items.append(f"[DIR]  {p.name}/")
            else:
                size_kb = p.stat().st_size / 1024.0
                items.append(f"[FILE] {p.name:<28} ({size_kb:.1f} KB)")
        return "\n".join(items[:60]) or "Directory is empty."
    except Exception as e:
        return f"Error listing directory: {e}"


@SkillRegistry.register(
    name="read_file",
    description="Reads lines from a file with 1-indexed line numbers. By default reads the full file (up to 1000 lines). Specify start_line and line_count to inspect a specific code slice."
)
def read_file(file_path: str, start_line: int = 1, line_count: int = 1000) -> str:
    """Reads lines from a file with line numbers."""
    target = _resolve_workspace_path(file_path)
    if not target.exists() or not target.is_file():
        return f"File does not exist: {target}"
    try:
        with open(target, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        start_idx = max(1, start_line) - 1
        end_idx = min(total, start_idx + max(1, line_count))

        slice_lines = all_lines[start_idx:end_idx]
        formatted = [f"{start_idx + i + 1:>4}: {line.rstrip()}" for i, line in enumerate(slice_lines)]
        header = f"[{target.name} - Lines {start_idx + 1} to {end_idx} of {total}]"
        return header + "\n" + "\n".join(formatted)
    except Exception as e:
        return f"Error reading file: {e}"


@SkillRegistry.register(
    name="read_multiple_files",
    description="Reads several workspace files in one call. Pass comma-separated paths; output is bounded to protect the model's context window."
)
def read_multiple_files(
    file_paths: str,
    max_lines_per_file: int = 220,
    max_total_chars: int = 24000,
) -> str:
    """Reads several files in batch to minimize latency and round-trips."""
    paths = [p.strip() for p in file_paths.split(",") if p.strip()]
    if not paths:
        return "No file paths provided."

    sections = []
    remaining_chars = max(1000, min(max_total_chars, 50000))
    line_limit = max(1, min(max_lines_per_file, 1000))
    for fp in paths[:12]:
        if remaining_chars <= 0:
            sections.append("[Output limit reached; read the remaining files in another call.]")
            break
        try:
            target = _resolve_workspace_path(fp)
        except ValueError as err:
            section = f"=== {fp} ===\n[Security Violation: {err}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)
            continue
        if not target.exists() or not target.is_file():
            section = f"=== {fp} ===\n[File does not exist: {target}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)
            continue
        try:
            with open(target, encoding="utf-8", errors="replace") as f:
                lines = []
                truncated = False
                for line in f:
                    if len(lines) >= line_limit:
                        truncated = True
                        break
                    lines.append(line)
            shown = lines
            formatted = [f"{i + 1:>4}: {line.rstrip()}" for i, line in enumerate(shown)]
            suffix = (
                f"\n... [additional lines omitted after the first {line_limit}; use read_file with start_line to continue]"
                if truncated else ""
            )
            section = f"=== {target.relative_to(WORKSPACE_DIR)} ({len(shown)} lines shown) ===\n" + "\n".join(formatted) + suffix
            if len(section) > remaining_chars:
                section = section[:remaining_chars] + "\n[File text truncated by the batch output limit.]"
            sections.append(section)
            remaining_chars -= len(section)
        except Exception as e:
            section = f"=== {fp} ===\n[Error reading file: {e}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)

    return "\n\n".join(sections)


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
    except Exception:  # noqa: BLE001
        return None


def _is_reparse_point_or_symlink(path: Path) -> bool:
    """Detects symlinks, directory junctions, and Windows reparse points."""
    try:
        if path.is_symlink():
            return True
    except Exception:  # noqa: BLE001
        pass

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
    except Exception:  # noqa: BLE001
        pass

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
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetFileInformationByHandle.restype = wintypes.BOOL
    _k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    _k32.SetFileInformationByHandle.restype = wintypes.BOOL
    _k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.argtypes = []
    _k32.DuplicateHandle.restype = wintypes.BOOL
    _k32.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
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
    _verify_directory_confinement(target.parent)

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
                _verify_open_file_handle(fd, target, for_write=True)
                yield fd, was_absent
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
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
            _verify_open_file_handle(fd, target, for_write=True)

            yield fd, was_created
        except Exception:
            # Atomic deletion of newly created file through the handle without path resolution
            if was_created:
                disp_info = FILE_DISPOSITION_INFO(True)
                target_h = dup_handle or file_handle
                if target_h and getattr(target_h, "value", None):
                    with contextlib.suppress(Exception):
                        _k32.SetFileInformationByHandle(target_h, 4, ctypes.byref(disp_info), ctypes.sizeof(disp_info))
                elif fd is not None:
                    with contextlib.suppress(Exception):
                        raw_h = msvcrt.get_osfhandle(fd)
                        _k32.SetFileInformationByHandle(raw_h, 4, ctypes.byref(disp_info), ctypes.sizeof(disp_info))
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
                with contextlib.suppress(Exception):
                    _k32.CloseHandle(dup_handle)
                dup_handle = None
    finally:
        if h_dir is not None:
            _k32.CloseHandle(h_dir)


def _get_handle_physical_path(fd: int) -> str | None:
    """Returns the kernel-canonicalized physical path of an open file descriptor on Windows.

    Uses GetFinalPathNameByHandleW, stripping kernel volume prefixes.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import msvcrt

        k32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(fd)
        buf = ctypes.create_unicode_buffer(1024)
        res = k32.GetFinalPathNameByHandleW(handle, buf, 1024, 0)
        if res == 0:
            return None
        path_str = buf.value
        if path_str.startswith("\\\\?\\UNC\\"):
            path_str = "\\\\" + path_str[8:]
        elif path_str.startswith("\\\\?\\"):
            path_str = path_str[4:]
        return path_str
    except Exception:  # noqa: BLE001
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
    physical_path = _get_handle_physical_path(fd)
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


@SkillRegistry.register(
    name="write_file",
    description="Creates a new file or completely overwrites an existing file in the workspace with new content.",
    risk_level=2,
)
def write_file(
    file_path: str,
    content: str,
    overwrite: bool = False,
    preimage_digest: str | None = None,
) -> str:
    """Writes content to a file in the workspace."""
    try:
        target = _resolve_workspace_path(file_path, for_write=True)
    except ValueError as val_err:
        return f"Error writing file: {val_err}"

    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: write_file requires explicit human approval."
        )

    if active_token.tool_name != "write_file":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'write_file'."
        )

    norm_path = str(file_path).replace("\\", "/")
    candidate_paths = [file_path]
    if norm_path != file_path:
        candidate_paths.append(norm_path)

    candidate_args = []
    for p in candidate_paths:
        if preimage_digest is not None:
            candidate_args.append({
                "file_path": p,
                "content": content,
                "overwrite": overwrite,
                "preimage_digest": preimage_digest,
            })
        candidate_args.append({"file_path": p, "content": content, "overwrite": overwrite})
        if overwrite is False:
            if preimage_digest is not None:
                candidate_args.append({"file_path": p, "content": content, "preimage_digest": preimage_digest})
            candidate_args.append({"file_path": p, "content": content})

    matched_hash = None
    for cand in candidate_args:
        cand_hash = compute_action_hash("write_file", cand)
        if hmac.compare_digest(active_token.action_hash, cand_hash):
            matched_hash = cand_hash
            break

    if matched_hash is None:
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: File path, content, or overwrite arguments do not match approved action hash."
        )

    if not active_token.consume("write_file", matched_hash):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: write_file authorization grant was already consumed or invalidated."
        )

    if _is_reparse_point_or_symlink(target):
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Destination path '{target.name}' is a symbolic link or reparse point; file mutation rejected."
        )

    if not overwrite:
        if target.exists():
            raise SecurityBoundaryViolation(
                f"Preimage Integrity Violation: Destination file '{target.name}' already exists when creating new file. Unexpected file creation rejected."
            )
    else:
        if preimage_digest is not None:
            if not target.exists():
                raise SecurityBoundaryViolation(
                    f"Preimage Integrity Violation: Target file '{target.name}' was unexpectedly deleted before overwrite."
                )
            live_digest = _compute_file_sha256(target)
            if live_digest != preimage_digest:
                raise SecurityBoundaryViolation(
                    f"Preimage Integrity Violation: Destination file '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Overwrite rejected."
                )

    try:
        backup_created = False
        if target.exists() and overwrite:
            backup_path = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup_path)
            backup_created = True

        with open_handle_bound_workspace_file(target, overwrite=overwrite, must_exist=False) as (fd, was_created):
            if overwrite and not was_created:
                if preimage_digest is not None:
                    live_digest = _compute_fd_sha256(fd)
                    if live_digest != preimage_digest:
                        raise SecurityBoundaryViolation(
                            f"Preimage Integrity Violation: Destination file '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Overwrite rejected."
                        )
            elif overwrite and was_created and preimage_digest is not None:
                raise SecurityBoundaryViolation(
                    f"Preimage Integrity Violation: Target file '{target.name}' was unexpectedly deleted before overwrite."
                )

            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.truncate(0)
                f.write(content)

        lines = content.splitlines()
        backup_msg = f" (backup saved to {target.name}.bak)" if backup_created else ""
        return f"Successfully wrote {len(content)} bytes ({len(lines)} lines) to {target.name}{backup_msg}."
    except SecurityBoundaryViolation:
        raise
    except Exception as e:
        return f"Error writing file: {e}"


@SkillRegistry.register(
    name="patch_file",
    description="Surgically replaces a unique code snippet with new code inside an existing file.",
    risk_level=2,
)
def patch_file(
    file_path: str,
    target_snippet: str,
    replacement_snippet: str,
    preimage_digest: str | None = None,
) -> str:
    """Replaces a unique target_snippet in a file with replacement_snippet."""
    try:
        target = _resolve_workspace_path(file_path, for_write=True)
    except ValueError as val_err:
        return f"Error patching file: {val_err}"

    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: patch_file requires explicit human approval."
        )

    if active_token.tool_name != "patch_file":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'patch_file'."
        )

    norm_path = str(file_path).replace("\\", "/")
    candidate_paths = [file_path]
    if norm_path != file_path:
        candidate_paths.append(norm_path)

    candidate_args = []
    for p in candidate_paths:
        if preimage_digest is not None:
            candidate_args.append({
                "file_path": p,
                "target_snippet": target_snippet,
                "replacement_snippet": replacement_snippet,
                "preimage_digest": preimage_digest,
            })
        candidate_args.append({
            "file_path": p,
            "target_snippet": target_snippet,
            "replacement_snippet": replacement_snippet,
        })

    matched_hash = None
    for cand in candidate_args:
        cand_hash = compute_action_hash("patch_file", cand)
        if hmac.compare_digest(active_token.action_hash, cand_hash):
            matched_hash = cand_hash
            break

    if matched_hash is None:
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: File path or snippet arguments do not match approved action hash."
        )

    if not active_token.consume("patch_file", matched_hash):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: patch_file authorization grant was already consumed or invalidated."
        )

    if _is_reparse_point_or_symlink(target):
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Destination path '{target.name}' is a symbolic link or reparse point; file mutation rejected."
        )

    if not target.exists() or not target.is_file():
        return f"File does not exist: {target}"

    if preimage_digest is not None:
        live_digest = _compute_file_sha256(target)
        if live_digest != preimage_digest:
            raise SecurityBoundaryViolation(
                f"Preimage Integrity Violation: File '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Patch rejected."
            )

    try:
        backup_created = False
        if target.exists():
            backup_path = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup_path)
            backup_created = True

        with open_handle_bound_workspace_file(target, overwrite=True, must_exist=True) as (fd, _was_created):
            if preimage_digest is not None:
                live_digest = _compute_fd_sha256(fd)
                if live_digest != preimage_digest:
                    raise SecurityBoundaryViolation(
                        f"Preimage Integrity Violation: File '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Patch rejected."
                    )

            with os.fdopen(fd, "r+", encoding="utf-8", errors="replace") as f:
                full_content = f.read()
                count = full_content.count(target_snippet)
                if count == 0:
                    return f"Error: Target snippet was not found in '{target.name}'. Please ensure the snippet matches the existing file content exactly."
                if count > 1:
                    return f"Error: Target snippet matched {count} times in '{target.name}'. Please include more surrounding lines to make the snippet unique."

                new_content = full_content.replace(target_snippet, replacement_snippet, 1)
                f.seek(0)
                f.truncate(0)
                f.write(new_content)

        backup_msg = f" (backup saved to {target.name}.bak)" if backup_created else ""
        return f"Successfully patched '{target.name}'. Replaced {len(target_snippet.splitlines())} lines with {len(replacement_snippet.splitlines())} lines{backup_msg}."
    except FileNotFoundError:
        return f"File does not exist: {target}"
    except SecurityBoundaryViolation:
        raise
    except Exception as e:
        return f"Error patching file: {e}"


@SkillRegistry.register(
    name="search_codebase",
    description="Searches source and project files including Python, JavaScript, TypeScript, Markdown, YAML, JSON, TOML, and PowerShell. Skips generated folders and secret .env files."
)
def search_codebase(query: str, folder_path: str = ".", file_pattern: str = "*", max_results: int = 40) -> str:
    """Searches for query in files matching file_pattern."""
    target_dir = _resolve_workspace_path(folder_path)
    if not target_dir.exists() or not target_dir.is_dir():
        return f"Directory does not exist: {target_dir}"
    query = query.strip()
    if not query:
        return "Search query cannot be empty."

    results = []
    pattern_re = re.compile(re.escape(query), re.IGNORECASE)
    ignored_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "data", "node_modules", "dist", "build"}
    text_suffixes = {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".json", ".jsonc", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".md", ".txt", ".html", ".css", ".scss", ".sql", ".ps1",
        ".psm1", ".bat", ".sh", ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".go", ".rs",
        ".xml", ".svg",
    }
    result_limit = max(1, min(max_results, 100))

    try:
        for p in target_dir.rglob(file_pattern):
            try:
                relative = p.relative_to(WORKSPACE_DIR)
            except ValueError:
                continue
            if any(part in ignored_dirs for part in relative.parts):
                continue
            if not p.is_file():
                continue
            if p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"):
                continue
            if p.suffix.lower() not in text_suffixes or p.stat().st_size > 2_000_000:
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern_re.search(line):
                            results.append(f"{relative}:{line_num}: {line.strip()[:180]}")
                            if len(results) >= result_limit:
                                break
            except Exception:
                continue
            if len(results) >= result_limit:
                break

        if not results:
            return f"No matches found for '{query}' in {file_pattern} files."
        return f"Found {len(results)} matches for '{query}':\n" + "\n".join(results)
    except Exception as e:
        return f"Error searching codebase: {e}"


@SkillRegistry.register(
    name="git_diff",
    description="Reviews tracked edits in the Jarvis workspace against HEAD, including staged and unstaged changes. Output is capped for context."
)
def git_diff(max_chars: int = 12000) -> str:
    """Returns a bounded tracked-file diff for reviewing recent edits."""
    try:
        proc = run_sandboxed_batch(
            ["git", "diff", "--no-ext-diff", "--unified=3", "HEAD", "--"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0:
            return f"Git diff failed: {proc.stderr.strip()}"
        if not output:
            return "No tracked changes to review."
        limit = max(1000, min(max_chars, 30000))
        if len(output) > limit:
            return output[:limit] + "\n\n[Diff truncated; request a narrower review.]"
        return output
    except subprocess.TimeoutExpired:
        return "Git diff error: command timed out after 15 seconds."
    except Exception as e:
        return f"Git diff error: {e}"


@SkillRegistry.register(
    name="run_project_tests",
    description="Runs Python test suites using the high-performance parallel test runner (pytest-xdist) to verify code changes."
)
def run_project_tests(
    test_file: str = "",
    run_all: bool = False,
    timeout_seconds: int = 120,
    *,
    _allow_uncontained: bool = False,
) -> str:
    """Executes tests using the high-performance two-tier test runner.

    Process-tree containment under Windows is strictly enforced by default.
    Model-callable tool arguments cannot bypass containment. Trusted internal callers
    may explicitly override containment via the internal parameter `_allow_uncontained=True`,
    or trusted local configuration via `JARVIS_ALLOW_UNCONTAINED_TESTS=1`.
    """
    py_exec = sys.executable
    runner_script = WORKSPACE_DIR / "tools" / "run_fast_tests.py"

    if runner_script.exists():
        cmd = [py_exec, str(runner_script)]
        if test_file:
            cmd.append(test_file)
        elif run_all:
            cmd.append("--all")
        else:
            cmd.append("--fast")
    else:
        # Fallback to standard runner
        if test_file:
            target = f"tests/{test_file}".strip("/")
            cmd = [py_exec, "-m", "pytest", target]
        else:
            cmd = [py_exec, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]

    try:
        from jarvis.core.cancellation import get_current_tool_cancellation_token
        from jarvis.skills.job_object_sandbox import run_job_sandboxed_process

        token = get_current_tool_cancellation_token()
        if token is not None:
            if token.is_cancelled:
                return "Error: Test run cancelled before launch: execution deadline expired or interrupt signaled."
            if token.deadline is not None:
                remaining = token.remaining_time()
                if remaining <= 0.0:
                    return "Error: Test run cancelled before launch: execution deadline expired."
                effective_timeout = min(float(timeout_seconds), remaining)
            else:
                effective_timeout = float(timeout_seconds)
        else:
            effective_timeout = float(timeout_seconds)

        res = run_job_sandboxed_process(
            command=cmd,
            cwd=str(WORKSPACE_DIR),
            max_memory_mb=POWERSHELL_JOB_MAX_MEMORY_MB,
            max_processes=POWERSHELL_JOB_MAX_PROCESSES,
            timeout_sec=effective_timeout,
            requested_affinity=0,
            cancel_token=token,
            tail_chars=POWERSHELL_OUTPUT_TAIL_CHARS,
        )
        status = res.get("status")
        has_output = bool((res.get("stdout_tail") or "").strip() or (res.get("stderr_tail") or "").strip())
        if status in ("ok", "failed", "timeout", "cancelled") or has_output:
            # Subprocess output boundary: Taint active turn before returning test output to LLM
            from jarvis.core.security_boundaries import TurnTaintTracker
            TurnTaintTracker.get_instance().mark_tainted(
                source="subprocess_output:run_project_tests",
                metadata={"test_file": test_file[:64], "status": status}
            )
        if status == "timeout":
            return f"Error: Test run timed out after {effective_timeout:.1f} seconds, Sir."
        if status == "cancelled":
            return "Error: Test run cancelled by user."
        if status in ("ok", "failed"):
            out = (res.get("stdout_tail", "") + "\n" + res.get("stderr_tail", "")).strip()
            run_status = "PASSED" if res.get("exit_code") == 0 else f"FAILED (Exit Code {res.get('exit_code')})"
            return f"Test Run [{run_status}]:\n{out}"

        if status == "error":
            reason = res.get("reason", "Job Object containment failed")
            if sys.platform == "win32":
                uncontained_opt_in = (
                    _allow_uncontained
                    or os.environ.get("JARVIS_ALLOW_UNCONTAINED_TESTS") == "1"
                    or os.environ.get("JARVIS_ALLOW_UNCONTAINED_TEST_RUNNER") == "1"
                )
                if not uncontained_opt_in:
                    return (
                        f"Error: Job Object containment failed on Windows ({reason}). "
                        "Process-tree containment is required by default to prevent orphan processes. "
                        "Fallback to uncontained execution is disabled. "
                        "To override, set JARVIS_ALLOW_UNCONTAINED_TESTS=1 in trusted local environment "
                        "or invoke via trusted internal API (_allow_uncontained=True)."
                    )
                logger.warning(
                    "Executing test runner with uncontained fallback on Windows: %s", reason
                )
        else:
            return f"Error executing tests: unexpected sandbox status '{status}' ({res.get('reason', '')})"

        # Fallback if primary Job Object execution is unavailable (non-Windows hosts, or explicitly opted-in on Windows).
        # When authorized, we attempt process-tree containment via run_sandboxed_batch().
        # If run_sandboxed_batch() fails to establish containment, we fail closed and return an error;
        # under no circumstances do we fall back to bare subprocess.run().
        try:
            proc = run_sandboxed_batch(
                cmd,
                cwd=str(WORKSPACE_DIR),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except RuntimeError as sand_err:
            platform_str = "Windows" if sys.platform == "win32" else "POSIX"
            return (
                f"Error: Contained fallback execution failed on {platform_str} ({sand_err}). "
                "Uncontained execution is strictly prohibited."
            )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        run_status = "PASSED" if proc.returncode == 0 else f"FAILED (Exit Code {proc.returncode})"
        warning_banner = ""
        if sys.platform == "win32":
            warning_banner = (
                "[WARNING: UNCONTAINED EXECUTION — Windows Job Object process-tree containment is DISABLED. "
                "Descendant processes will not be tracked or automatically reaped on timeout/cancel.]\n"
            )
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().mark_tainted(
            source="subprocess_output:run_project_tests",
            metadata={"test_file": test_file[:64], "status": "fallback"}
        )
        return f"{warning_banner}Test Run [{run_status}]:\n{out}"
    except subprocess.TimeoutExpired:
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().mark_tainted(
            source="subprocess_output:run_project_tests",
            metadata={"test_file": test_file[:64], "status": "timeout"}
        )
        return f"Error: Test run timed out after {timeout_seconds} seconds, Sir."
    except Exception as e:
        from jarvis.core.security_boundaries import SecurityBoundaryViolation
        if isinstance(e, SecurityBoundaryViolation):
            raise
        return f"Error executing tests: {e}"


@SkillRegistry.register(
    name="git_status",
    description="Checks git repository status and current branch in the Jarvis workspace."
)
def git_status() -> str:
    """Returns git status and branch."""
    try:
        proc = run_sandboxed_batch(
            ["git", "status", "-s"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch_proc = run_sandboxed_batch(
            ["git", "branch", "--show-current"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = branch_proc.stdout.strip() or "unknown"
        changes = proc.stdout.strip() or "Working tree clean (no uncommitted changes)."
        return f"Branch: {branch}\n\nWorking Tree Status:\n{changes}"
    except subprocess.TimeoutExpired:
        return "Git error: git command timed out."
    except Exception as e:
        return f"Git error: {e}"


@SkillRegistry.register(
    name="run_background_task",
    description="Executes a long-running terminal command asynchronously in the background (builds, tests, compiles, downloads) and proactively notifies Sir when it completes.",
    risk_level=2,
)
def run_background_task(command: str, task_name: str = "", notify_voice: bool = True) -> str:
    """Spawns an asynchronous background task with HUD and completion notifications."""
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: Background command execution requires explicit human approval."
        )
    allowed_tools = {"run_background_task", "run_command_in_background"}
    if active_token.tool_name not in allowed_tools:
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not background task."
        )
    clean_args = {"command": command}
    expected_hash = compute_action_hash(active_token.tool_name, clean_args)
    if not hmac.compare_digest(active_token.action_hash, expected_hash):
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: Background command arguments do not match approved action hash."
        )
    if not active_token.consume(active_token.tool_name, clean_args):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: Background command grant has already been consumed."
        )

    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    _success, msg, _task_id = mgr.run_task(command=command, task_name=task_name, notify_voice=notify_voice)
    return msg


@SkillRegistry.register(
    name="run_command_in_background",
    description="Executes a long-running terminal command asynchronously in the background.",
    risk_level=2,
)
def run_command_in_background(command: str, task_name: str = "", notify_voice: bool = True) -> str:
    """Alias for run_background_task with strict authorization."""
    return run_background_task(command=command, task_name=task_name, notify_voice=notify_voice)


@SkillRegistry.register(
    name="list_background_tasks",
    description="Lists all currently running and recently completed background terminal tasks."
)
def list_background_tasks() -> str:
    """Lists active and recent background tasks with status and duration."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    tasks = mgr.list_tasks()
    if not tasks:
        return "No background tasks currently active or recorded, Sir."

    lines = ["Background Terminal Tasks, Sir:"]
    for t in tasks[-8:]:
        st = t["status"].upper()
        dur = f"{t['duration_seconds']}s"
        name = t["name"]
        t_id = t["id"]
        code = f" (exit: {t['exit_code']})" if t['exit_code'] is not None else ""
        lines.append(f"• [{st}] {name} (ID: {t_id}, Elapsed: {dur}){code}")
    return "\n".join(lines)


@SkillRegistry.register(
    name="get_background_task_output",
    description="Inspects the live or recent stdout/stderr output lines from a background task by ID."
)
def get_background_task_output(task_id: str, max_lines: int = 40) -> str:
    """Retrieves stdout/stderr tail for a specific background task."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    out = mgr.get_task_output(task_id=task_id, max_lines=max_lines)
    if out is None:
        return f"No background task with ID '{task_id}' was found, Sir."
    return out


@SkillRegistry.register(
    name="cancel_background_task",
    description="Terminates an actively running background task by task ID."
)
def cancel_background_task(task_id: str) -> str:
    """Cancels a running background task."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    cancelled = mgr.cancel_task(task_id=task_id)
    if cancelled:
        return f"Background task '{task_id}' has been terminated, Sir."
    return f"Unable to cancel task '{task_id}'. It may not exist or has already completed, Sir."


@SkillRegistry.register(
    name="query_codebase_graph",
    description="Queries the project's Tree-sitter knowledge graph (Graphify) to trace callers, dependencies, blast-radius, and architectural relationships across files in <1.5s."
)
def query_codebase_graph(query: str, mode: str = "query") -> str:
    """Queries the local Graphify knowledge graph (graphify query / path / explain)."""
    graph_path = WORKSPACE_DIR / "graphify-out" / "graph.json"
    if not graph_path.exists():
        return "No codebase knowledge graph found at graphify-out/graph.json. Run 'graphify extract . --code-only' first, Sir."

    graphify_bin = sys.executable.replace("python.exe", "graphify.exe")
    if not os.path.exists(graphify_bin):
        graphify_bin = "graphify"

    cmd = [graphify_bin]
    clean_mode = (mode or "query").strip().lower()
    if clean_mode == "path":
        parts = query.split()
        if len(parts) >= 2:
            cmd.extend(["path", parts[0], parts[1]])
        else:
            cmd.extend(["query", query])
    elif clean_mode == "explain":
        cmd.extend(["explain", query.strip()])
    else:
        cmd.extend(["query", query])

    try:
        proc = run_sandboxed_batch(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            return f"Graph query failed (code {proc.returncode}): {err}"
        return out or err or "No relational nodes identified for that query, Sir."
    except subprocess.TimeoutExpired:
        return "Graph query timed out after 10 seconds, Sir."
    except Exception as e:
        return f"Graph query error: {e}"


def _find_ast_grep() -> Path | None:
    """Locates ast-grep executable across python environment, PATH, and parent workspace."""
    ast_grep_bin = Path(sys.executable).parent / "ast-grep.exe"
    if ast_grep_bin.exists():
        return ast_grep_bin
    sg_bin = Path(sys.executable).parent / "sg.exe"
    if sg_bin.exists():
        return sg_bin
    candidate = shutil.which("ast-grep") or shutil.which("sg")
    if candidate:
        return Path(candidate)
    parent_venv = WORKSPACE_DIR.parent / ".venv" / "Scripts" / "ast-grep.exe"
    if parent_venv.exists():
        return parent_venv
    return None


def _find_ruff() -> Path | None:
    """Locates ruff executable across python environment, PATH, and parent workspace."""
    ruff_bin = Path(sys.executable).parent / "ruff.exe"
    if ruff_bin.exists():
        return ruff_bin
    candidate = shutil.which("ruff")
    if candidate:
        return Path(candidate)
    parent_venv = WORKSPACE_DIR.parent / ".venv" / "Scripts" / "ruff.exe"
    if parent_venv.exists():
        return parent_venv
    return None


@SkillRegistry.register(
    name="ast_search_codebase",
    description="Performs ultra-fast structural AST searches across the codebase using ast-grep. Matches syntax patterns (e.g. 'def $FUNC($$$ARGS): $$$BODY', '$OBJ.method($$$ARGS)') across Python, JavaScript, or JSON in <15ms."
)
def ast_search_codebase(pattern: str, path: str = ".", lang: str | None = None, max_lines: int = 60) -> str:
    """Performs structural AST pattern searching using ast-grep."""
    try:
        resolved_path = _resolve_workspace_path(path) if path and path != "." else WORKSPACE_DIR
    except ValueError as e:
        return f"Invalid path: {e}"

    ast_grep_bin = _find_ast_grep()
    if not ast_grep_bin:
        return "ast-grep binary is not installed in the environment. Run 'pip install ast-grep-cli', Sir."

    cmd = [str(ast_grep_bin), "run", "--color=never", "--heading=never", "-p", pattern]
    if lang:
        cmd.extend(["-l", lang])

    try:
        rel_target = str(resolved_path.relative_to(WORKSPACE_DIR))
        cmd.append("." if rel_target == "." else rel_target)
    except ValueError:
        cmd.append(str(resolved_path))

    try:
        proc = run_sandboxed_batch(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode != 0 and err:
            return f"ast-grep execution error: {err}"

        if not out:
            return f"No AST nodes matched pattern '{pattern}' in '{path}', Sir."

        lines = out.splitlines()
        total_lines = len(lines)
        if total_lines > max_lines:
            truncated = "\n".join(lines[:max_lines])
            return (
                f"{truncated}\n\n"
                f"[Output truncated: showing first {max_lines} of {total_lines} matched lines. "
                f"Narrow the target path or refine pattern, Sir.]"
            )
        return out
    except subprocess.TimeoutExpired:
        return "ast-grep search timed out after 15 seconds, Sir."
    except Exception as e:
        return f"ast-grep search error: {e}"


@SkillRegistry.register(
    name="check_code_diagnostics",
    description="Runs ultra-fast static analysis, compiler syntax checks, and linting across the codebase via Ruff in <20ms.",
    risk_level=0,
    read_only=True,
)
def check_code_diagnostics(target_path: str = ".", select: str | None = None, max_lines: int = 50, **kwargs: Any) -> str:
    """Runs Ruff static analysis and diagnostics on target files or directories."""
    # Reject model-supplied mutation flags outright
    if kwargs.get("fix") or kwargs.get("_fix"):
        return (
            "Execution rejected: Automated code mutation via 'fix=True' is prohibited in "
            "check_code_diagnostics. Use patch_file or write_file for authorized workspace modifications, Sir."
        )

    try:
        resolved_path = _resolve_workspace_path(target_path) if target_path and target_path != "." else WORKSPACE_DIR
    except ValueError as e:
        return f"Invalid path: {e}"

    ruff_bin = _find_ruff()
    if not ruff_bin:
        return "Ruff binary is not installed in the environment. Run 'pip install ruff', Sir."

    cmd = [str(ruff_bin), "check", "--color", "never", "--output-format", "concise"]
    if select:
        cmd.extend(["--select", select])

    try:
        rel_target = str(resolved_path.relative_to(WORKSPACE_DIR))
        cmd.append("." if rel_target == "." else rel_target)
    except ValueError:
        cmd.append(str(resolved_path))

    try:
        proc = run_sandboxed_batch(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode == 0:
            return f"All diagnostics clean: zero lint or syntax violations found across '{target_path}', Sir."

        display_text = out or err
        if not display_text:
            return f"Ruff reported non-zero exit code ({proc.returncode}) without output for '{target_path}', Sir."

        lines = display_text.splitlines()
        total_lines = len(lines)
        if total_lines > max_lines:
            truncated = "\n".join(lines[:max_lines])
            return (
                f"{truncated}\n\n"
                f"[Output truncated: showing first {max_lines} of {total_lines} diagnostic messages. "
                f"Target specific modules to inspect in detail, Sir.]"
            )
        return display_text
    except subprocess.TimeoutExpired:
        return "Ruff diagnostics check timed out after 15 seconds, Sir."
    except Exception as e:
        return f"Ruff diagnostics check error: {e}"



