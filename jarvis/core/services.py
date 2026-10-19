"""Lifecycle management for local dependency processes used by Jarvis."""
import logging
import subprocess
import threading
from typing import Any, Dict, Tuple

logger = logging.getLogger("Jarvis.Services")


class _PsutilProcessHandle:
    """Small Popen-compatible wrapper for a verified local service process."""

    def __init__(self, process: Any):
        self._process = process
        self.pid = process.pid

    def poll(self):
        try:
            return None if self._process.is_running() else 0
        except Exception:
            return 0

    def terminate(self):
        self._process.terminate()

    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)

    def kill(self):
        self._process.kill()


class ManagedServiceManager:
    """Tracks spawned services and verified local listeners for shutdown cleanup."""

    def __init__(self):
        self._services: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def register(self, name: str, process: Any) -> None:
        """Register a spawned service process for shutdown cleanup."""
        with self._lock:
            self._services[name] = process
        logger.info("Registered managed service %s (PID %s).", name, process.pid)

    def adopt_local_listener(self, name: str, port: int, identity_tokens: Tuple[str, ...]) -> bool:
        """Adopt a local listener only when its process identity matches the expected service."""
        try:
            import psutil
        except ImportError:
            logger.warning("Cannot identify existing %s on port %s because psutil is unavailable.", name, port)
            return False

        try:
            for connection in psutil.net_connections(kind="tcp"):
                if connection.status != psutil.CONN_LISTEN or connection.laddr.port != port or not connection.pid:
                    continue
                try:
                    process = psutil.Process(connection.pid)
                    identity = " ".join([process.name(), *process.cmdline()]).lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                if all(token.lower() in identity for token in identity_tokens):
                    self.register(name, _PsutilProcessHandle(process))
                    logger.info("Adopted local %s listener on port %s for shutdown cleanup.", name, port)
                    return True
        except Exception:
            logger.exception("Could not inspect local listeners while adopting %s.", name)
        logger.warning("No verifiable local %s process was found listening on port %s.", name, port)
        return False

    def stop_all(self, timeout_seconds: float = 5.0) -> None:
        """Stop registered services and their child processes, then force-stop stragglers."""
        with self._lock:
            services: Tuple[Tuple[str, Any], ...] = tuple(self._services.items())
            self._services.clear()

        for name, process in services:
            if process.poll() is not None:
                continue
            descendants = []
            try:
                import psutil
                descendants = psutil.Process(process.pid).children(recursive=True)
            except Exception:
                # The primary child can still be stopped without the optional psutil package.
                descendants = []
            try:
                logger.info("Stopping managed service %s (PID %s).", name, process.pid)
                for child in reversed(descendants):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                process.terminate()
                try:
                    process.wait(timeout=timeout_seconds)
                except Exception as wait_error:
                    timeout_errors = [subprocess.TimeoutExpired]
                    try:
                        import psutil
                        timeout_errors.append(psutil.TimeoutExpired)
                    except ImportError:
                        pass
                    if not isinstance(wait_error, tuple(timeout_errors)):
                        raise
                    logger.warning("Managed service %s did not stop in time; terminating it.", name)
                    process.kill()
                    process.wait(timeout=2.0)
                if descendants:
                    import psutil
                    _, still_running = psutil.wait_procs(descendants, timeout=timeout_seconds)
                    for child in still_running:
                        try:
                            child.kill()
                        except Exception:
                            pass
                    if still_running:
                        psutil.wait_procs(still_running, timeout=2.0)
            except Exception:
                logger.exception("Could not stop managed service %s (PID %s).", name, process.pid)

        # Enforce total child process termination: sweep any descendant child of the current Jarvis process
        try:
            import os
            import sys
            import psutil
            # Safety: Under unit testing, never sweep arbitrary child processes not registered
            # with this manager, preventing inadvertent termination of ephemeral test fixtures/canaries.
            if "unittest" in sys.modules and not os.environ.get("JARVIS_FORCE_CHILD_SWEEP"):
                return
            current_proc = psutil.Process(os.getpid())
            remaining_children = current_proc.children(recursive=True)
            if remaining_children:
                logger.info("Sweeping %d remaining child processes belonging to Jarvis...", len(remaining_children))
                for child in remaining_children:
                    try:
                        child.terminate()
                    except Exception:
                        pass
                _, alive = psutil.wait_procs(remaining_children, timeout=timeout_seconds)
                for child in alive:
                    try:
                        logger.warning("Force-killing lingering child process PID %s (%s)", child.pid, child.name())
                        child.kill()
                    except Exception:
                        pass
        except Exception as err:
            logger.debug("Child process sweep notice: %s", err)


def kill_orphaned_jarvis_processes(exclude_pids=None, dry_run: bool = False):
    """Identifies and terminates any orphaned background processes belonging to Jarvis."""
    import os
    import sys
    import psutil

    # Safety: Under unit testing, default to dry-run mode to never terminate live user sessions
    if "unittest" in sys.modules and not os.environ.get("JARVIS_FORCE_ORPHAN_KILL"):
        dry_run = True

    current_pid = os.getpid()
    exclude = set(exclude_pids or set())
    exclude.add(current_pid)
    try:
        parent_pid = os.getppid()
        if parent_pid:
            exclude.add(parent_pid)
    except Exception:
        pass

    try:
        current_proc = psutil.Process(current_pid)
        current_create_time = current_proc.create_time()
    except Exception:
        current_create_time = None

    terminated = []

    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                pid = proc.info['pid']
                if pid in exclude:
                    continue

                # Guard: only terminate processes created before the current launcher
                proc_ctime = proc.info.get('create_time')
                if current_create_time and proc_ctime and proc_ctime >= current_create_time:
                    continue

                cmdline_parts = proc.info.get('cmdline') or []
                cmdline_str = " ".join(cmdline_parts).lower()
                name_str = (proc.info.get('name') or "").lower()

                # Guard: Never kill IDE, language servers, terminal host, or external editor processes
                if any(k in cmdline_str for k in ("antigravity", "language_server", "code.exe", "cursor.exe")):
                    continue

                is_jarvis_proc = False
                # 1. Python process running jarvis entry point or modules
                if "python" in name_str and any(k in cmdline_str for k in ("run.py", "jarvis/")):
                    is_jarvis_proc = True
                # 2. Process bound to Jarvis UI port (20129)
                elif any(k in cmdline_str for k in ("20129", "jarvis-hud")):
                    is_jarvis_proc = True

                if is_jarvis_proc:
                    if dry_run:
                        logger.info("Dry-run orphan check: detected Jarvis process PID %s (%s)", pid, name_str)
                        terminated.append({"pid": pid, "name": name_str, "cmdline": cmdline_str[:80], "dry_run": True})
                    else:
                        logger.warning("Killing orphaned Jarvis process PID %s (%s: %s)", pid, name_str, cmdline_str[:80])
                        proc.terminate()
                        try:
                            proc.wait(timeout=1.5)
                        except psutil.TimeoutExpired:
                            proc.kill()
                        terminated.append({"pid": pid, "name": name_str, "cmdline": cmdline_str[:80]})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        logger.error("Error scanning for orphaned Jarvis processes: %s", e)

    return terminated
