"""Parent-Supervised Process Isolation and Execution Governor for Jarvis 2.0 Test Runner.

Executes pytest child processes inside a Win32 Job Object configured with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Guarantees:
- Hard wall-clock timeout enforcement.
- Complete process-tree termination on timeout (including worker subprocesses, grandchildren, browsers).
- Clean harvesting of stdout/stderr diagnostics before and after termination.
- Unique temporary artifact directories per run.
- Zero timeouts misreported as passes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

# Import Win32 Job Object sandbox helpers if on Windows
try:
    from jarvis.skills.job_object_sandbox import (
        JobHandleHolder,
        _resume_process,
        _spawn_suspended,
        _terminate_and_drain,
        assign_process_to_job,
        create_process_job_object,
        kernel32,
    )
    HAS_JOB_OBJECT = (kernel32 is not None)
except ImportError:
    HAS_JOB_OBJECT = False


@dataclass
class SupervisedResult:
    """Complete, unambiguous accounting of an isolated supervised test execution."""
    status: str  # "COMPLETED", "TIMEOUT", "ERROR", "ENVIRONMENT_BLOCKED"
    exit_code: int
    duration_sec: float
    stdout: str = ""
    stderr: str = ""
    target: str = ""
    run_id: str = ""
    timed_out: bool = False
    xml_path: str | None = None
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    timed_out_count: int = 0
    environment_blocked_count: int = 0
    active_processes_at_timeout: int | None = None
    diagnostics: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return (
            self.status == "COMPLETED"
            and self.exit_code == 0
            and not self.timed_out
            and self.failed_count == 0
            and self.error_count == 0
        )


class TestSupervisor:
    """Manages child pytest executions under hard process-tree containment and deadlines."""

    def __init__(
        self,
        base_tmp_dir: str | Path = "data/pytest_tmp",
        default_timeout_sec: float = 30.0,
        workspace_root: Path | None = None,
    ):
        self.workspace_root = workspace_root or WORKSPACE_ROOT
        target_tmp = (self.workspace_root / base_tmp_dir).resolve()
        try:
            target_tmp.mkdir(parents=True, exist_ok=True)
            probe_file = target_tmp / f".probe_{uuid.uuid4().hex[:8]}"
            probe_file.touch()
            probe_file.unlink()
            self.base_tmp_dir = target_tmp
        except OSError:
            import tempfile
            self.base_tmp_dir = Path(tempfile.gettempdir()) / "jarvis_pytest_tmp"
            self.base_tmp_dir.mkdir(parents=True, exist_ok=True)
        self.default_timeout_sec = default_timeout_sec

    def _resolve_pytest_bin(self) -> list[str]:
        """Resolves the pytest binary or fallback python -m pytest."""
        candidates = [
            Path(sys.executable).parent / "pytest.exe",
            WORKSPACE_ROOT / ".venv" / "Scripts" / "pytest.exe",
            WORKSPACE_ROOT.parent / ".venv" / "Scripts" / "pytest.exe",
        ]
        for cand in candidates:
            if cand.is_file():
                return [str(cand)]
        py_candidates = [
            Path(sys.executable),
            WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe",
            WORKSPACE_ROOT.parent / ".venv" / "Scripts" / "python.exe",
        ]
        for py in py_candidates:
            if py.is_file():
                return [str(py), "-m", "pytest"]
        return [sys.executable, "-m", "pytest"]

    def _parse_junit_xml(self, xml_file: Path) -> tuple[int, int, int, int, list[dict[str, Any]]]:
        """Parses standard JUnit XML output to extract passed, failed, skipped, and error counts."""
        if not xml_file.exists() or xml_file.stat().st_size == 0:
            return 0, 0, 0, 0, []

        passed = 0
        failed = 0
        skipped = 0
        errors = 0
        details = []

        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()

            # Iterate over all testcase elements
            for tc in root.iter("testcase"):
                name = tc.attrib.get("name", "")
                classname = tc.attrib.get("classname", "")
                node_id = f"{classname}::{name}" if classname else name
                duration = float(tc.attrib.get("time", "0.0") or 0.0)

                failure_elem = tc.find("failure")
                error_elem = tc.find("error")
                skipped_elem = tc.find("skipped")

                if failure_elem is not None:
                    failed += 1
                    status = "FAILED"
                    msg = failure_elem.attrib.get("message", "") or (failure_elem.text or "")
                elif error_elem is not None:
                    errors += 1
                    status = "ERROR"
                    msg = error_elem.attrib.get("message", "") or (error_elem.text or "")
                elif skipped_elem is not None:
                    skipped += 1
                    status = "SKIPPED"
                    msg = skipped_elem.attrib.get("message", "") or (skipped_elem.text or "")
                else:
                    passed += 1
                    status = "PASSED"
                    msg = ""

                details.append({
                    "node_id": node_id,
                    "status": status,
                    "duration": duration,
                    "message": msg.strip()[:200],
                })
        except (ET.ParseError, OSError, ValueError):
            # If XML was truncated or malformed, return whatever was successfully parsed
            return passed, failed, skipped, errors, details

        return passed, failed, skipped, errors, details

    def run_command(
        self,
        command: list[str],
        timeout_sec: float | None = None,
        run_id: str | None = None,
        cwd: str | Path | None = None,
        env_override: dict[str, str] | None = None,
        target_label: str = "",
        junit_xml_path: Path | None = None,
    ) -> SupervisedResult:
        """Runs an arbitrary command in a Job Object with wall-clock deadline enforcement."""
        timeout = timeout_sec if timeout_sec is not None else self.default_timeout_sec
        run_uuid = run_id or f"run_{uuid.uuid4().hex[:12]}"
        run_tmp = self.base_tmp_dir / run_uuid
        run_tmp.mkdir(parents=True, exist_ok=True)

        working_dir = str(cwd or WORKSPACE_ROOT)
        run_env = os.environ.copy()
        if env_override:
            run_env.update(env_override)
        run_env["PYTHONFAULTHANDLER"] = "1"
        run_env["PYTHONUNBUFFERED"] = "1"

        t_start = time.monotonic()
        h_job = None
        holder = None
        proc = None
        timed_out = False
        active_processes_at_timeout = None
        diagnostics = ""

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _drain_stream(stream: Any, chunks: list[str]) -> None:
            try:
                while True:
                    data = stream.read(65536)
                    if not data:
                        break
                    chunks.append(data)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        t_out = None
        t_err = None

        try:
            if sys.platform == "win32":
                if not HAS_JOB_OBJECT:
                    raise RuntimeError("Win32 Job Object support required on Windows; failing closed.")
                h_job = create_process_job_object(kill_on_close=True)
                holder = JobHandleHolder(h_job)
                proc = _spawn_suspended(command, cwd=working_dir, env=run_env)
                if not assign_process_to_job(h_job, proc):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    raise RuntimeError("Failed to assign child process to Job Object; failing closed to prevent unsupervised execution.")
                _resume_process(proc)
            else:
                proc = subprocess.Popen(
                    command,
                    cwd=working_dir,
                    env=run_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                )

            if proc.stdout:
                t_out = threading.Thread(target=_drain_stream, args=(proc.stdout, stdout_chunks), daemon=True)
                t_out.start()
            if proc.stderr:
                t_err = threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_chunks), daemon=True)
                t_err.start()

            # Deadline poll loop
            deadline = t_start + timeout
            while True:
                ret = proc.poll()
                if ret is not None:
                    break
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                time.sleep(0.05)

            duration = time.monotonic() - t_start

            if timed_out:
                diagnostics += f"[SUPERVISOR] Execution exceeded wall-clock timeout of {timeout:.1f}s.\n"
                if holder:
                    active_processes_at_timeout = holder.get_active_process_count()
                    diagnostics += f"[SUPERVISOR] Active processes in job tree at timeout: {active_processes_at_timeout}\n"

                # Terminate and drain all processes in job tree
                if h_job and HAS_JOB_OBJECT:
                    _terminate_and_drain(h_job, exit_code=124, drain_timeout=2.0)
                else:
                    try:
                        proc.kill()
                    except OSError:
                        pass

                # Harvest residual pipe outputs from background reader threads
                if t_out:
                    t_out.join(timeout=0.5)
                if t_err:
                    t_err.join(timeout=0.5)

                out = "".join(stdout_chunks)
                err = "".join(stderr_chunks)

                return SupervisedResult(
                    status="TIMEOUT",
                    exit_code=124,
                    duration_sec=duration,
                    stdout=out,
                    stderr=(err + ("\n" if err and diagnostics else "") + diagnostics).strip(),
                    target=target_label,
                    run_id=run_uuid,
                    timed_out=True,
                    active_processes_at_timeout=active_processes_at_timeout,
                    diagnostics=diagnostics,
                    timed_out_count=1,
                )

            # Normal child completion
            proc.wait()
            if t_out:
                t_out.join(timeout=1.0)
            if t_err:
                t_err.join(timeout=1.0)

            out = "".join(stdout_chunks)
            err = "".join(stderr_chunks)

            passed = failed = skipped = errors = 0
            details = []
            xml_str = None

            if junit_xml_path and junit_xml_path.exists():
                passed, failed, skipped, errors, details = self._parse_junit_xml(junit_xml_path)
                xml_str = str(junit_xml_path)

            return SupervisedResult(
                status="COMPLETED",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout=out,
                stderr=err,
                target=target_label,
                run_id=run_uuid,
                timed_out=False,
                xml_path=xml_str,
                passed_count=passed,
                failed_count=failed,
                skipped_count=skipped,
                error_count=errors,
                details=details,
            )

        finally:
            if holder and not holder.is_closed:
                holder.drain(timeout=1.0)
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

    def run_pytest(
        self,
        targets: str | list[str],
        pytest_args: list[str] | None = None,
        timeout_sec: float | None = None,
        run_id: str | None = None,
        record_junit: bool = True,
    ) -> SupervisedResult:
        """Executes targeted pytest runs with automatic JUnit XML capture and timeout supervision."""
        target_list = [targets] if isinstance(targets, str) else list(targets)
        run_uuid = run_id or f"pytest_{uuid.uuid4().hex[:12]}"
        run_tmp = self.base_tmp_dir / run_uuid
        run_tmp.mkdir(parents=True, exist_ok=True)

        junit_xml = run_tmp / "junit.xml" if record_junit else None
        if junit_xml and junit_xml.is_file():
            try:
                junit_xml.unlink()
            except OSError:
                pass

        cmd = self._resolve_pytest_bin()
        if pytest_args:
            cmd.extend(pytest_args)

        if record_junit and junit_xml:
            cmd.append(f"--junitxml={junit_xml}")

        # Unique basetemp and cache dir to avoid cross-run directory collision and permission issues
        cmd.append(f"--basetemp={run_tmp / 'tmp'}")
        cmd.extend(["-o", f"cache_dir={run_tmp / 'cache'}"])

        cmd.extend(target_list)

        label = ", ".join(target_list) if len(target_list) <= 3 else f"{len(target_list)} targets"
        return self.run_command(
            command=cmd,
            timeout_sec=timeout_sec,
            run_id=run_uuid,
            target_label=label,
            junit_xml_path=junit_xml,
        )
