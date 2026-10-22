"""Asynchronous background task runner and completion monitor for Jarvis 2.0.

Provides non-blocking execution of long-running terminal tasks (builds, tests,
compilations, downloads), streaming stdout/stderr buffers, and dispatching
ambient completion events to the Holographic HUD and proactive voice alerts.
"""
import collections
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jarvis.core.safety import SafetyGate

logger = logging.getLogger("Jarvis.TaskManager")

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent


class BackgroundTask:
    """Represents an active or completed background terminal execution."""

    def __init__(
        self,
        task_id: str,
        command: str,
        name: str,
        process: subprocess.Popen,
        notify_voice: bool = True,
        job_handle: Optional[Any] = None,
    ):
        self.id = task_id
        self.command = command
        self.name = name or f"Task-{task_id}"
        self.process = process
        self.notify_voice = notify_voice
        self.job_handle = job_handle
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.status = "running"  # "running", "completed", "failed", "cancelled"
        self.exit_code: Optional[int] = None
        self.stdout_lines: collections.deque = collections.deque(maxlen=1000)
        self.stderr_lines: collections.deque = collections.deque(maxlen=1000)

    @property
    def duration_seconds(self) -> float:
        if self.end_time is not None:
            return round(self.end_time - self.start_time, 1)
        return round(time.time() - self.start_time, 1)

    def get_output_summary(self, max_lines: int = 10) -> str:
        """Returns the tail of recent output lines."""
        lines = list(self.stdout_lines)[-max_lines:]
        if not lines and self.stderr_lines:
            lines = list(self.stderr_lines)[-max_lines:]
        return "\n".join(lines).strip() or "No output emitted."


class BackgroundTaskManager:
    """Manages asynchronous long-running subprocesses with non-blocking pipe readers."""

    _instance: Optional["BackgroundTaskManager"] = None
    _lock = threading.Lock()

    def __init__(self, orchestrator: Any = None):
        self.orchestrator = orchestrator
        self._tasks: Dict[str, BackgroundTask] = {}
        self._task_counter = 0
        self._tasks_lock = threading.Lock()
        self.safety_gate = SafetyGate()

    @classmethod
    def get_instance(cls, orchestrator: Any = None) -> "BackgroundTaskManager":
        """Returns the singleton instance of BackgroundTaskManager."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(orchestrator)
            elif orchestrator is not None and cls._instance.orchestrator is None:
                cls._instance.orchestrator = orchestrator
            return cls._instance

    def run_task(
        self,
        command: str,
        task_name: str = "",
        notify_voice: bool = True,
        cwd: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """Spawns an asynchronous background task if safe.
        Returns (success, message, task_id).
        """
        clean_cmd = (command or "").strip()
        if not clean_cmd:
            return False, "Command cannot be empty, Sir.", None

        # 1. Evaluate with SafetyGate
        if self.safety_gate.is_command_dangerous(clean_cmd):
            return (
                False,
                "Command blocked by SafetyGate: contains potentially destructive operations, Sir.",
                None,
            )

        # 2. Allocate Task ID
        with self._tasks_lock:
            self._task_counter += 1
            task_id = f"task_{self._task_counter}"

        working_dir = Path(cwd) if cwd else WORKSPACE_DIR
        if not working_dir.exists():
            working_dir = WORKSPACE_DIR

        # 3. Spawn subprocess with detached process group and buffered pipes
        try:
            creation_flags = 0
            if os.name == "nt":
                from jarvis.skills.job_object_sandbox import CREATE_SUSPENDED
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED

            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", clean_cmd],
                cwd=str(working_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line-buffered
                close_fds=True,
                creationflags=creation_flags,
            )

            job_handle = None
            if os.name == "nt":
                try:
                    from jarvis.skills.job_object_sandbox import (
                        assign_process_to_job,
                        create_process_job_object,
                        resume_process,
                        terminate_and_close_job,
                    )
                    h_job = create_process_job_object(kill_on_close=True)
                    if not h_job:
                        # Fail closed: kill unstarted child, drain pipes, abort launch
                        proc.kill()
                        proc.communicate()
                        logger.error("Failed to create Win32 Job Object for task %s; aborting launch.", task_id)
                        return False, "Failed to create Windows Job Object containment for background task, Sir.", None

                    if not assign_process_to_job(h_job, proc):
                        # Fail closed: kill unstarted child, terminate job, abort launch
                        proc.kill()
                        proc.communicate()
                        terminate_and_close_job(h_job)
                        logger.error("Failed to assign process to Job Object for task %s; killing unstarted child.", task_id)
                        return False, "Failed to assign background task to Windows Job Object containment, Sir.", None

                    if not resume_process(proc):
                        # Fail closed: could not resume child
                        proc.kill()
                        proc.communicate()
                        terminate_and_close_job(h_job)
                        logger.error("Failed to resume suspended child for task %s; job terminated.", task_id)
                        return False, "Failed to resume background task after containment, Sir.", None

                    job_handle = h_job
                except Exception as job_err:
                    proc.kill()
                    proc.communicate()
                    logger.error("Unexpected error initializing Job Object for task %s: %s", task_id, job_err)
                    return False, f"Failed to initialize Job Object containment for background task: {job_err}", None

            task = BackgroundTask(
                task_id=task_id,
                command=clean_cmd,
                name=task_name or f"Background Task #{task_id}",
                process=proc,
                notify_voice=notify_voice,
                job_handle=job_handle,
            )

            with self._tasks_lock:
                self._tasks[task_id] = task

            # 4. Spawn non-blocking pipe readers and completion monitor
            threading.Thread(
                target=self._pipe_reader,
                args=(proc.stdout, task.stdout_lines),
                name=f"Reader-stdout-{task_id}",
                daemon=True,
            ).start()

            threading.Thread(
                target=self._pipe_reader,
                args=(proc.stderr, task.stderr_lines),
                name=f"Reader-stderr-{task_id}",
                daemon=True,
            ).start()

            threading.Thread(
                target=self._monitor_completion,
                args=(task,),
                name=f"Monitor-{task_id}",
                daemon=True,
            ).start()

            logger.info("Launched background task '%s' (ID %s, PID %s).", task.name, task_id, proc.pid)
            return True, f"Launched background task '{task.name}' (ID: {task_id}, PID: {proc.pid}), Sir.", task_id

        except Exception as e:
            logger.error("Failed to spawn background task: %s", e)
            return False, f"Failed to launch background task: {e}", None

    def _pipe_reader(self, stream, target_deque: collections.deque):
        """Drains a stream line-by-line into a circular buffer to prevent OS pipe deadlocks."""
        try:
            for line in iter(stream.readline, ""):
                if line:
                    clean = line.rstrip()
                    target_deque.append(clean)
            stream.close()
        except Exception:
            pass

    def _monitor_completion(self, task: BackgroundTask):
        """Monitors process completion, updates telemetry, and coordinates voice notifications."""
        try:
            exit_code = task.process.wait()
            task.end_time = time.time()
            task.exit_code = exit_code
            task.status = "completed" if exit_code == 0 else "failed"

            logger.info(
                "Background task '%s' (ID: %s) concluded with exit code %s in %.1fs.",
                task.name, task.id, exit_code, task.duration_seconds
            )

            # 1. Dispatch ambient HUD telemetry card
            if self.orchestrator and hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                try:
                    self.orchestrator.broadcast_ambient_telemetry({
                        "event": "task_completed",
                        "task_id": task.id,
                        "name": task.name,
                        "status": task.status,
                        "exit_code": exit_code,
                        "duration_s": task.duration_seconds,
                        "summary": task.get_output_summary(max_lines=5),
                    })
                except Exception as b_err:
                    logger.debug("Telemetry broadcast error on task completion: %s", b_err)

            # 2. Coordinate spoken announcement respecting user focus
            if task.notify_voice and self.orchestrator:
                self._notify_user_verbally(task)

        except Exception as e:
            logger.error("Error monitoring background task %s: %s", task.id, e)
            task.status = "failed"
            task.end_time = time.time()
        finally:
            if task.job_handle:
                try:
                    from jarvis.skills.job_object_sandbox import terminate_and_close_job
                    terminate_and_close_job(task.job_handle)
                except Exception:
                    pass
                task.job_handle = None

    def _notify_user_verbally(self, task: BackgroundTask):
        """Delivers a courteous spoken announcement if Sir is not focused on the terminal."""
        try:
            heartbeat = getattr(self.orchestrator, "heartbeat", None)
            if not heartbeat:
                return

            # Check if Sir is already viewing the terminal window
            current_window = heartbeat.get_active_window_title().lower()
            if any(term in current_window for term in ("windows terminal", "powershell", "cmd", "terminal")):
                # Sir is looking directly at terminal; subtle done SFX is sufficient
                if hasattr(self.orchestrator, "sfx"):
                    self.orchestrator.sfx.play_done()
                return

            # Format announcement
            if task.status == "completed":
                msg = f"Sir, your background task '{task.name}' has completed successfully in {task.duration_seconds} seconds."
            else:
                msg = f"Sir, the background task '{task.name}' exited with error code {task.exit_code} after {task.duration_seconds} seconds."

            heartbeat.speak_proactive(msg)

        except Exception as e:
            logger.debug("Failed to deliver verbal notification for task %s: %s", task.id, e)

    def list_tasks(self) -> List[Dict[str, Any]]:
        """Returns details for all active and recent background tasks."""
        with self._tasks_lock:
            results = []
            for t in self._tasks.values():
                results.append({
                    "id": t.id,
                    "name": t.name,
                    "command": t.command,
                    "status": t.status,
                    "duration_seconds": t.duration_seconds,
                    "exit_code": t.exit_code,
                    "summary": t.get_output_summary(max_lines=3),
                })
            return results

    def get_task_output(self, task_id: str, max_lines: int = 40) -> Optional[str]:
        """Returns the formatted stdout/stderr output for a specific task."""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            stdout_part = "\n".join(list(task.stdout_lines)[-max_lines:]).strip()
            stderr_part = "\n".join(list(task.stderr_lines)[-max_lines:]).strip()

            sections = [f"Output for Task '{task.name}' (ID: {task.id}, Status: {task.status.upper()}):"]
            if stdout_part:
                sections.append(f"STDOUT:\n{stdout_part}")
            if stderr_part:
                sections.append(f"STDERR:\n{stderr_part}")
            if not stdout_part and not stderr_part:
                sections.append("No output logged.")

            return "\n\n".join(sections)

    def cancel_task(self, task_id: str) -> bool:
        """Terminates an active background task cleanly."""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task or task.status != "running":
                return False

            try:
                # Terminate Job Object first to guarantee process-tree death on Windows
                if task.job_handle:
                    try:
                        from jarvis.skills.job_object_sandbox import terminate_and_close_job
                        terminate_and_close_job(task.job_handle)
                    except Exception:
                        pass
                    task.job_handle = None

                task.process.terminate()
                try:
                    task.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    task.process.kill()
                task.status = "cancelled"
                task.end_time = time.time()
                logger.info("Cancelled background task '%s' (ID %s).", task.name, task_id)
                return True
            except Exception as e:
                logger.error("Error cancelling task %s: %s", task_id, e)
                return False

    def stop_all(self, timeout: float = 2.0):
        """Cleanly terminates all running tasks on application shutdown."""
        with self._tasks_lock:
            running = [t for t in self._tasks.values() if t.status == "running"]

        for t in running:
            if t.job_handle:
                try:
                    from jarvis.skills.job_object_sandbox import terminate_and_close_job
                    terminate_and_close_job(t.job_handle)
                except Exception:
                    pass
                t.job_handle = None
            try:
                t.process.terminate()
            except Exception:
                pass

        for t in running:
            try:
                t.process.wait(timeout=timeout)
            except Exception:
                try:
                    t.process.kill()
                except Exception:
                    pass
            t.status = "cancelled"
            t.end_time = time.time()

    def stop(self):
        """Alias for stop_all to satisfy orchestrator shutdown protocol."""
        self.stop_all()
