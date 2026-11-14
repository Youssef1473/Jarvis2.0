"""Background task management and long-running job dispatch skills."""
from __future__ import annotations

import hmac
import logging

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.BackgroundTasks")


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
    if not active_token.consume(active_token.tool_name, expected_hash):
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
