"""Developer, codebase stewardship, and shell execution tools for Windows."""
from __future__ import annotations

import hmac
import logging
import os
import subprocess

from jarvis.skills import job_object_sandbox
from jarvis.skills.background_tasks import (
    cancel_background_task,
    get_background_task_output,
    list_background_tasks,
    run_background_task,
    run_command_in_background,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.code_intelligence import (
    _find_ast_grep,
    _find_ruff,
    ast_search_codebase,
    check_code_diagnostics,
    git_diff,
    git_status,
    query_codebase_graph,
    run_project_tests,
)
from jarvis.skills.fs_confinement import (
    WORKSPACE_DIR,
    _acquire_confined_directory_handle,
    _compute_fd_sha256,
    _compute_file_sha256,
    _get_handle_physical_path,
    _is_reparse_point_or_symlink,
    _resolve_workspace_path,
    _verify_directory_confinement,
    _verify_open_file_handle,
    open_handle_bound_workspace_file,
)
from jarvis.skills.fs_operations import (
    list_directory,
    patch_file,
    read_file,
    read_multiple_files,
    search_codebase,
    write_file,
)
from jarvis.skills.job_object_sandbox import (
    run_sandboxed_batch,
)

logger = logging.getLogger("Jarvis.Skills.DevShell")

POWERSHELL_JOB_MAX_MEMORY_MB = 4096
POWERSHELL_JOB_MAX_PROCESSES = 64
POWERSHELL_OUTPUT_TAIL_CHARS = 16000


def _verify_action_authorization(tool_name: str, command: str) -> None:
    """Verifies that an execution command is authorized by human approval context."""
    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            f"Authorization Required: {tool_name} requires explicit human approval."
        )
    if active_token.tool_name != tool_name:
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not '{tool_name}'."
        )
    clean_args = {"command": command}
    expected_hash = compute_action_hash(tool_name, clean_args)
    if not hmac.compare_digest(active_token.action_hash, expected_hash):
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: Command arguments do not match approved action hash (anti-tampering violation)."
        )

    if not active_token.consume_for_process({tool_name}):
        raise SecurityBoundaryViolation(
            f"Single-Use Authorization Violation: {tool_name} authorization grant was already consumed or invalidated."
        )


def _mark_subprocess_output_tainted(source: str, command: str, status: str | None) -> None:
    """Marks the active turn tainted after child process output generation."""
    from jarvis.core.security_boundaries import TurnTaintTracker

    TurnTaintTracker.get_instance().mark_tainted(
        source=source,
        metadata={"command": command[:64], "status": status},
    )


def _diagnose_locked_files(command: str) -> list[str]:
    """Inspects file lock holders for files referenced in command when WinError 32 occurs."""
    diagnostics = []
    try:
        from jarvis.skills.rstrtmgr_lock_surgeon import inspect_file_locks

        tokens = [t.strip("\"'") for t in command.split() if "." in t and not t.startswith("-")]
        for tok in tokens:
            if os.path.exists(tok):
                locks = inspect_file_locks(tok)
                if locks.get("status") == "locked":
                    holders = [f"PID {h['pid']} ({h['app_name']})" for h in locks.get("holders", [])]
                    diagnostics.append(f"LOCK DIAGNOSTIC (Rstrtmgr): '{tok}' locked by {', '.join(holders)}")
    except (ImportError, OSError) as exc:
        logger.debug("Restart Manager lock diagnosis skipped: %s", exc)
    return diagnostics


@SkillRegistry.register(
    name="execute_powershell",
    description="Executes a PowerShell terminal command safely inside the Jarvis workspace and returns stdout and stderr.",
    risk_level=2,
)
def execute_powershell(command: str, timeout_seconds: int = 25) -> str:
    """Executes a PowerShell command inside a Job Object so every child it spawns dies with it."""
    _verify_action_authorization("execute_powershell", command)

    from jarvis.core.cancellation import get_current_tool_cancellation_token

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

    res = job_object_sandbox.run_job_sandboxed_process(
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
        _mark_subprocess_output_tainted(
            source="subprocess_output:execute_powershell",
            command=command,
            status=status,
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
            lock_diags = _diagnose_locked_files(command)
            out.extend(lock_diags)
    return "\n\n".join(out) if out else "Command completed with no output."


@SkillRegistry.register(
    name="execute_sandboxed_powershell",
    description="Executes a PowerShell command inside a kernel Job Object pinned to E-Cores (0xFF00) with hard memory caps.",
    risk_level=1,
)
def execute_sandboxed_powershell(command: str, max_memory_mb: int = 1536, timeout_seconds: int = 25) -> str:
    """Runs PowerShell inside an isolated Win32 Job Object shielding P-cores."""
    _verify_action_authorization("execute_sandboxed_powershell", command)

    from jarvis.core.cancellation import get_current_tool_cancellation_token

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

    res = job_object_sandbox.run_job_sandboxed_process(
        command=["powershell", "-NoProfile", "-Command", command],
        cwd=str(WORKSPACE_DIR),
        max_memory_mb=max_memory_mb,
        timeout_sec=effective_timeout,
        cancel_token=token,
    )
    status = res.get("status")
    has_output = bool((res.get("stdout_tail") or "").strip() or (res.get("stderr_tail") or "").strip())
    if status in ("ok", "failed", "timeout", "cancelled") or has_output:
        _mark_subprocess_output_tainted(
            source="subprocess_output:execute_sandboxed_powershell",
            command=command,
            status=status,
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


__all__ = [
    "POWERSHELL_JOB_MAX_MEMORY_MB",
    "POWERSHELL_JOB_MAX_PROCESSES",
    "POWERSHELL_OUTPUT_TAIL_CHARS",
    "WORKSPACE_DIR",
    "_acquire_confined_directory_handle",
    "_compute_fd_sha256",
    "_compute_file_sha256",
    "_find_ast_grep",
    "_find_ruff",
    "_get_handle_physical_path",
    "_is_reparse_point_or_symlink",
    "_resolve_workspace_path",
    "_verify_action_authorization",
    "_verify_directory_confinement",
    "_verify_open_file_handle",
    "ast_search_codebase",
    "cancel_background_task",
    "check_code_diagnostics",
    "execute_powershell",
    "execute_sandboxed_powershell",
    "get_background_task_output",
    "git_diff",
    "git_status",
    "list_background_tasks",
    "list_directory",
    "open_handle_bound_workspace_file",
    "patch_file",
    "query_codebase_graph",
    "read_file",
    "read_multiple_files",
    "run_background_task",
    "run_command_in_background",
    "run_project_tests",
    "run_sandboxed_batch",
    "search_codebase",
    "subprocess",
    "write_file",
]
