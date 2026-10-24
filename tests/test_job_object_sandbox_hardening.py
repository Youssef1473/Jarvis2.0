"""Phase 2 hardening tests for the Job Object sandbox and execute_powershell.

Two layers:
  * Fake-kernel tests (Linux): the Win32 calls are replaced by a fake whose "job"
    is a real POSIX process group, so spawn -> assign -> resume ordering, fail-closed
    assignment, drain verification, orphan reaping and cancellation are exercised
    against REAL subprocess trees.
  * Real-Windows tests: run only on win32 against the actual kernel.
"""
import ctypes
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

import jarvis.skills.job_object_sandbox as jsb
from jarvis.skills import dev_shell
from jarvis.skills.base import SkillRegistry

LINUX = sys.platform.startswith("linux")


def _proc_states():
    """Yields (pid, state, pgrp) for every process (Linux /proc)."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                rest = f.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        yield int(entry), rest[0], int(rest[2])


def _group_alive_count(pgid):
    return sum(1 for _pid, state, pgrp in _proc_states() if pgrp == pgid and state != "Z")


def _pid_alive(pid):
    return any(p == pid and state != "Z" for p, state, _g in _proc_states())


class FakeKernel32:
    """Fake kernel32 whose 'job' is a set of POSIX process groups."""

    def __init__(self, calls):
        self.calls = calls
        self.assign_ok = True
        self.never_drain = False
        self.pgids = []
        self.limits = None
        self.last_error = 5

    def CreateJobObjectW(self, attrs, name):
        self.calls.append("create_job")
        return 4242

    def SetInformationJobObject(self, h, cls, ref, size):
        self.calls.append("set_limits")
        self.limits = ref._obj
        return 1

    def AssignProcessToJobObject(self, h, hproc):
        self.calls.append("assign")
        if not self.assign_ok:
            return 0
        self.pgids.append(hproc)  # fake process handle == pid == pgid (start_new_session)
        return 1

    def QueryInformationJobObject(self, h, cls, ref, size, ret):
        if cls == jsb.JobObjectBasicAccountingInformation:
            ref._obj.ActiveProcesses = 1 if self.never_drain else sum(_group_alive_count(g) for g in self.pgids)
        return 1

    def TerminateJobObject(self, h, code):
        self.calls.append("terminate")
        if not self.never_drain:
            for g in self.pgids:
                try:
                    os.killpg(g, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return 1

    def CloseHandle(self, h):
        self.calls.append("close_job")
        return 1

    def GetCurrentProcess(self):
        return 1

    def GetProcessAffinityMask(self, h, proc_mask, sys_mask):
        proc_mask._obj.value = 0xFFFF
        sys_mask._obj.value = 0xFFFF
        return 1


class DelayedToken:
    """Cancel token that flips to cancelled after `delay` seconds."""

    def __init__(self, delay):
        self._at = time.monotonic() + delay

    @property
    def is_cancelled(self):
        return time.monotonic() >= self._at


GRANDCHILD_SCRIPT = textwrap.dedent(
    """
    import subprocess, sys, time
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(p.pid, flush=True)
    %s
    """
)


@unittest.skipUnless(LINUX, "fake-kernel tests drive real POSIX process groups")
class TestJobSandboxContainment(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.procs = []
        self.resume_ok = True
        self.k = FakeKernel32(self.calls)

        def spawn(command, cwd, env=None):
            self.calls.append("spawn")
            proc = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            self.procs.append(proc)
            return proc

        def resume(proc):
            self.calls.append("resume")
            return self.resume_ok

        for p in (
            mock.patch.object(jsb, "kernel32", self.k),
            mock.patch.object(jsb, "_spawn_suspended", spawn),
            mock.patch.object(jsb, "_process_handle", lambda proc: proc.pid),
            mock.patch.object(jsb, "_resume_process", resume),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._kill_everything)

    def _kill_everything(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run(self, script, **kw):
        kw.setdefault("timeout_sec", 10.0)
        return jsb.run_job_sandboxed_process([sys.executable, "-c", script], **kw)

    # ---- layout / ordering -------------------------------------------------
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "x64 layout")
    def test_struct_layouts_match_x64_win32(self):
        self.assertEqual(ctypes.sizeof(jsb.IO_COUNTERS), 48)
        self.assertEqual(ctypes.sizeof(jsb.JOBOBJECT_BASIC_LIMIT_INFORMATION), 64)
        self.assertEqual(ctypes.sizeof(jsb.JOBOBJECT_EXTENDED_LIMIT_INFORMATION), 144)
        self.assertEqual(ctypes.sizeof(jsb.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION), 48)
        self.assertEqual(jsb.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses.offset, 40)

    def test_spawn_then_assign_then_resume(self):
        res = self._run("print('hi')")
        self.assertEqual(res["status"], "ok")
        self.assertIn("hi", res["stdout_tail"])
        order = [c for c in self.calls if c in ("spawn", "assign", "resume")]
        self.assertEqual(order, ["spawn", "assign", "resume"])
        self.assertEqual(self.calls[-1], "close_job")
        self.assertTrue(res["job_drained"])
        self.assertEqual(res["orphans_reaped"], 0)

    def test_assignment_failure_fails_closed(self):
        self.k.assign_ok = False
        res = self._run("import time; time.sleep(30)")
        self.assertEqual(res["status"], "error")
        self.assertIn("containment failed", res["reason"])
        self.assertIn("win32_error", res)
        self.assertNotIn("resume", self.calls, "an uncontained child must never be resumed")
        self.assertIsNotNone(self.procs[0].poll(), "uncontained child must be dead")

    def test_resume_failure_terminates_job(self):
        self.resume_ok = False
        res = self._run("import time; time.sleep(30)")
        self.assertEqual(res["status"], "error")
        self.assertIn("terminate", self.calls)
        self.assertIsNotNone(self.procs[0].poll())

    def test_spawn_failure_is_reported_not_raised(self):
        with mock.patch.object(jsb, "_spawn_suspended", side_effect=FileNotFoundError("powershell")):
            res = jsb.run_job_sandboxed_process(["definitely-not-a-binary"])
        self.assertEqual(res["status"], "error")
        self.assertIn("Failed to start", res["reason"])
        self.assertEqual(self.calls[-1], "close_job")

    # ---- termination / drain ----------------------------------------------
    def test_timeout_kills_whole_tree_and_verifies_drain(self):
        t0 = time.monotonic()
        res = self._run(GRANDCHILD_SCRIPT % "time.sleep(60)", timeout_sec=1.0)
        self.assertLess(time.monotonic() - t0, 6.0)
        self.assertEqual(res["status"], "timeout")
        self.assertTrue(res["job_drained"])
        self.assertIn("Process tree terminated", res["stderr_tail"])
        grandchild = int(res["stdout_tail"].split()[0])  # partial output survives the kill
        self.assertFalse(_pid_alive(grandchild), "grandchild must die with the job")

    def test_undrained_job_is_reported_not_hidden(self):
        self.k.never_drain = True
        orig_drain, orig_collect = jsb._terminate_and_drain, jsb._collect_output
        with mock.patch.object(jsb, "_terminate_and_drain", lambda h, exit_code=1, drain_timeout=0.2: orig_drain(h, exit_code, drain_timeout)), \
             mock.patch.object(jsb, "_collect_output", lambda proc, timeout=0.2: orig_collect(proc, timeout)):
            res = self._run("import time; time.sleep(30)", timeout_sec=0.3)
        self.assertEqual(res["status"], "timeout")
        self.assertFalse(res["job_drained"], "must not claim cessation the kernel did not confirm")

    def test_orphan_after_normal_exit_is_reaped(self):
        res = self._run(GRANDCHILD_SCRIPT % "")  # main exits immediately, grandchild lingers
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["orphans_reaped"], 1)
        self.assertTrue(res["job_drained"])
        self.assertFalse(_pid_alive(int(res["stdout_tail"].split()[0])))

    # ---- cancellation ------------------------------------------------------
    def test_cancel_token_kills_job_promptly(self):
        t0 = time.monotonic()
        res = self._run(GRANDCHILD_SCRIPT % "time.sleep(60)", timeout_sec=30.0, cancel_token=DelayedToken(0.4))
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(res["status"], "cancelled")
        self.assertTrue(res["job_drained"])
        self.assertIn("cancelled by voice barge-in", res["stderr_tail"])
        self.assertFalse(_pid_alive(int(res["stdout_tail"].split()[0])))

    def test_pre_cancelled_token_never_spawns(self):
        res = self._run("print('x')", cancel_token=DelayedToken(-1))
        self.assertEqual(res["status"], "cancelled")
        self.assertNotIn("spawn", self.calls)

    # ---- options -----------------------------------------------------------
    def test_failed_exit_code(self):
        res = self._run("import sys; sys.exit(3)")
        self.assertEqual((res["status"], res["exit_code"]), ("failed", 3))

    def test_zero_affinity_disables_pinning(self):
        res = self._run("print('x')", requested_affinity=0)
        self.assertEqual(res["affinity_mask"], "default")
        flags = self.k.limits.BasicLimitInformation.LimitFlags
        self.assertEqual(flags & jsb.JOB_OBJECT_LIMIT_AFFINITY, 0)
        self.assertTrue(flags & jsb.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)

    def test_tail_chars_limit(self):
        res = self._run("print('a' * 5000)", tail_chars=100)
        self.assertLessEqual(len(res["stdout_tail"]), 100)


class TestExecutePowershellRouting(unittest.TestCase):
    """execute_powershell must go through the Job Object, never bare subprocess.run."""

    def _call(self, sandbox_result, command="Get-Date"):
        with mock.patch.object(jsb, "run_job_sandboxed_process", return_value=sandbox_result) as run, \
             mock.patch.object(dev_shell.subprocess, "run", side_effect=AssertionError("bare subprocess.run used")):
            out = dev_shell.execute_powershell(command, timeout_seconds=25)
        return out, run

    def test_routes_through_job_object_with_expected_arguments(self):
        out, run = self._call({"status": "ok", "stdout_tail": "hello\n", "stderr_tail": ""})
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["command"], ["powershell", "-NoProfile", "-Command", "Get-Date"])
        self.assertEqual(kwargs["timeout_sec"], 25.0)
        self.assertEqual(kwargs["requested_affinity"], 0)
        self.assertEqual(kwargs["max_memory_mb"], dev_shell.POWERSHELL_JOB_MAX_MEMORY_MB)
        self.assertEqual(out, "STDOUT:\nhello")

    def test_nonzero_exit_still_returns_stdout_and_stderr(self):
        out, _ = self._call({"status": "failed", "stdout_tail": "partial", "stderr_tail": "boom"})
        self.assertIn("STDOUT:\npartial", out)
        self.assertIn("STDERR:\nboom", out)

    def test_timeout_message_is_unchanged(self):
        out, _ = self._call({"status": "timeout", "stderr_tail": "Process tree terminated"})
        self.assertEqual(out, "Error: Command timed out after 25 seconds.")

    def test_cancelled_and_error_paths(self):
        out, _ = self._call({"status": "cancelled"})
        self.assertIn("cancelled", out)
        out, _ = self._call({"status": "error", "reason": "Job Object containment failed"})
        self.assertEqual(out, "Failed to execute command: Job Object containment failed")

    def test_empty_output(self):
        out, _ = self._call({"status": "ok", "stdout_tail": "", "stderr_tail": ""})
        self.assertEqual(out, "Command completed with no output.")

    def test_tool_still_registered(self):
        self.assertIsNotNone(SkillRegistry.get_tool("execute_powershell"))


class TestRunProjectTestsContainment(unittest.TestCase):
    """run_project_tests must enforce Job Object containment on Windows and fail closed by default."""

    @mock.patch("sys.platform", "win32")
    def test_windows_fails_closed_on_containment_failure_by_default(self):
        with mock.patch.object(jsb, "run_job_sandboxed_process", return_value={"status": "error", "reason": "Job Object access denied"}), \
             mock.patch.object(dev_shell.subprocess, "run", side_effect=AssertionError("uncontained subprocess.run called")):
            out = dev_shell.run_project_tests("test_dummy.py")
        self.assertIn("Error: Job Object containment failed on Windows", out)
        self.assertIn("Job Object access denied", out)
        self.assertIn("Process-tree containment is required by default", out)
        self.assertIn("Fallback to uncontained execution is disabled", out)

    @mock.patch("sys.platform", "win32")
    def test_windows_explicit_opt_in_arg_allows_fallback_with_warning(self):
        mock_proc = mock.MagicMock(returncode=0, stdout="all 10 passed", stderr="")
        with mock.patch.object(jsb, "run_job_sandboxed_process", return_value={"status": "error", "reason": "No privileges"}), \
             mock.patch.object(dev_shell.subprocess, "run", return_value=mock_proc) as run_mock:
            out = dev_shell.run_project_tests("test_dummy.py", _allow_uncontained=True)
        self.assertTrue(run_mock.called)
        self.assertIn("[WARNING: UNCONTAINED EXECUTION", out)
        self.assertIn("Test Run [PASSED]:\nall 10 passed", out)

    @mock.patch("sys.platform", "win32")
    def test_untrusted_model_tool_call_cannot_activate_containment_bypass(self):
        from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
        from jarvis.core.security_boundaries import TurnTaintTracker
        tracker = TurnTaintTracker.get_instance()
        tracker.reset_turn()
        orchestrator = ToolOrchestrator(taint_tracker=tracker)

        with mock.patch.object(jsb, "run_job_sandboxed_process", return_value={"status": "error", "reason": "Job Object containment denied"}), \
             mock.patch.object(dev_shell.subprocess, "run", side_effect=AssertionError("Model output bypassed containment!")):
            # 1. Model tries passing allow_uncontained=True in tool arguments
            out1 = orchestrator.execute_step("run_project_tests", {"test_file": "test_x.py", "allow_uncontained": True})
            self.assertIn("Job Object containment failed on Windows", str(out1))
            self.assertNotIn("WARNING: UNCONTAINED EXECUTION", str(out1))

            # 2. Model tries passing _allow_uncontained=True in tool arguments
            out2 = orchestrator.execute_step("run_project_tests", {"test_file": "test_x.py", "_allow_uncontained": True})
            self.assertIn("Job Object containment failed on Windows", str(out2))
            self.assertNotIn("WARNING: UNCONTAINED EXECUTION", str(out2))

    def test_tool_schema_excludes_uncontained_override(self):
        schemas = SkillRegistry.get_openai_tools(["run_project_tests"])
        self.assertEqual(len(schemas), 1)
        params = schemas[0]["function"]["parameters"]["properties"]
        self.assertNotIn("allow_uncontained", params)
        self.assertNotIn("_allow_uncontained", params)

    @mock.patch("sys.platform", "win32")
    def test_windows_explicit_opt_in_env_allows_fallback_with_warning(self):
        mock_proc = mock.MagicMock(returncode=1, stdout="1 failed", stderr="error msg")
        with mock.patch.dict(os.environ, {"JARVIS_ALLOW_UNCONTAINED_TESTS": "1"}), \
             mock.patch.object(jsb, "run_job_sandboxed_process", return_value={"status": "error", "reason": "No privileges"}), \
             mock.patch.object(dev_shell.subprocess, "run", return_value=mock_proc) as run_mock:
            out = dev_shell.run_project_tests("test_dummy.py")
        self.assertTrue(run_mock.called)
        self.assertIn("[WARNING: UNCONTAINED EXECUTION", out)
        self.assertIn("Test Run [FAILED (Exit Code 1)]:\n1 failed\nerror msg", out)

    @mock.patch("sys.platform", "win32")
    def test_does_not_fallback_for_unrelated_status(self):
        with mock.patch.object(jsb, "run_job_sandboxed_process", return_value={"status": "corrupt", "reason": "Bad state"}), \
             mock.patch.object(dev_shell.subprocess, "run", side_effect=AssertionError("uncontained subprocess.run called")):
            out = dev_shell.run_project_tests("test_dummy.py", _allow_uncontained=True)
        self.assertIn("Error executing tests: unexpected sandbox status 'corrupt'", out)

    @mock.patch("sys.platform", "linux")
    def test_non_windows_fallback_behavior_preserved(self):
        mock_proc = mock.MagicMock(returncode=0, stdout="linux passed", stderr="")
        with mock.patch.object(jsb, "run_job_sandboxed_process", return_value={"status": "error", "reason": "Job Object sandboxing is only supported on Windows hosts."}), \
             mock.patch.object(dev_shell.subprocess, "run", return_value=mock_proc) as run_mock:
            out = dev_shell.run_project_tests("test_dummy.py")
        self.assertTrue(run_mock.called)
        self.assertNotIn("[WARNING: UNCONTAINED EXECUTION", out)
        self.assertEqual(out, "Test Run [PASSED]:\nlinux passed")


def _wait_pid_gone(pid: int, timeout: float = 2.0) -> bool:
    import psutil
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.01)
    return not psutil.pid_exists(pid)


@unittest.skipUnless(sys.platform == "win32", "needs the real Windows kernel")
class TestJobSandboxRealWindows(unittest.TestCase):
    def test_timeout_kills_grandchild_and_drains(self):
        res = jsb.run_job_sandboxed_process(
            [sys.executable, "-c", GRANDCHILD_SCRIPT % "time.sleep(60)"], timeout_sec=2.0,
        )
        self.assertEqual(res["status"], "timeout")
        self.assertTrue(res["job_drained"])
        grandchild = int(res["stdout_tail"].split()[0])
        self.assertTrue(_wait_pid_gone(grandchild), f"PID {grandchild} was not reaped within deadline")

    def test_orphan_after_exit_is_reaped(self):
        res = jsb.run_job_sandboxed_process([sys.executable, "-c", GRANDCHILD_SCRIPT % ""], timeout_sec=10.0)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["job_drained"])
        grandchild = int(res["stdout_tail"].split()[0])
        self.assertTrue(_wait_pid_gone(grandchild), f"PID {grandchild} was not reaped within deadline")

    def test_execute_powershell_timeout_leaves_no_children(self):
        out = dev_shell.execute_powershell("Start-Sleep -Seconds 60", timeout_seconds=2)
        self.assertIn("timed out", out)

    def test_cancellation_kills_child_and_grandchild(self):
        token = DelayedToken(0.5)
        res = jsb.run_job_sandboxed_process(
            [sys.executable, "-c", GRANDCHILD_SCRIPT % "time.sleep(60)"],
            timeout_sec=15.0,
            cancel_token=token,
        )
        self.assertEqual(res["status"], "cancelled")
        self.assertTrue(res["job_drained"])
        grandchild = int(res["stdout_tail"].split()[0])
        self.assertTrue(_wait_pid_gone(grandchild), f"Grandchild PID {grandchild} was not killed on cancellation")

    def test_normal_completion_with_pipe_holding_grandchild(self):
        # Grandchild inherits stdout/stderr without DEVNULL; parent exits immediately
        script = textwrap.dedent("""
            import subprocess, sys
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(p.pid, flush=True)
            sys.exit(0)
        """)
        t0 = time.monotonic()
        res = jsb.run_job_sandboxed_process([sys.executable, "-c", script], timeout_sec=10.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, "Pipe-holding grandchild must not cause 10s timeout")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["job_drained"])
        self.assertGreaterEqual(res["orphans_reaped"], 1)
        grandchild = int(res["stdout_tail"].split()[0])
        self.assertTrue(_wait_pid_gone(grandchild), f"Orphan PID {grandchild} was not reaped")

    def test_failed_assignment_kills_child_before_running(self):
        with mock.patch.object(jsb.kernel32, "AssignProcessToJobObject", return_value=0):
            res = jsb.run_job_sandboxed_process([sys.executable, "-c", "print('should_never_run')"])
        self.assertEqual(res["status"], "error")
        self.assertIn("Job Object containment failed", res["reason"])

    def test_pre_cancelled_token_never_spawns(self):
        token = DelayedToken(-1)
        with mock.patch.object(jsb, "_spawn_suspended") as mock_spawn:
            res = jsb.run_job_sandboxed_process(["cmd", "/c", "echo 1"], cancel_token=token)
        self.assertEqual(res["status"], "cancelled")
        mock_spawn.assert_not_called()

    def test_concurrent_independent_jobs(self):
        import concurrent.futures
        def _run_long(delay):
            token = DelayedToken(delay)
            return jsb.run_job_sandboxed_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_sec=10.0,
                cancel_token=token,
            )
        def _run_short():
            return jsb.run_job_sandboxed_process(
                [sys.executable, "-c", "print('concurrent_ok')"],
                timeout_sec=5.0,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_run_long, 0.4)
            f2 = ex.submit(_run_short)
            res1 = f1.result(timeout=5.0)
            res2 = f2.result(timeout=5.0)

        self.assertEqual(res1["status"], "cancelled")
        self.assertTrue(res1["job_drained"])
        self.assertEqual(res2["status"], "ok")
        self.assertIn("concurrent_ok", res2["stdout_tail"])

    def test_job_object_unavailable_fallback_in_dev_shell(self):
        with mock.patch.object(jsb, "kernel32", None):
            res = jsb.run_job_sandboxed_process(["cmd", "/c", "echo 1"])
            self.assertEqual(res["status"], "error")
            self.assertIn("Job Object sandbox requires Windows", res["reason"])
            # dev_shell.run_project_tests fails closed on Windows by default
            test_out = dev_shell.run_project_tests(test_file="nonexistent_test_probe_xyz.py")
            self.assertIn("Error: Job Object containment failed on Windows", test_out)
            self.assertIn("Process-tree containment is required by default", test_out)

            # Even with explicit opt-in, if containment cannot be established on Windows, it fails closed
            opt_in_out = dev_shell.run_project_tests(test_file="nonexistent_test_probe_xyz.py", _allow_uncontained=True)
            self.assertIn("Error: Contained fallback execution failed on Windows", opt_in_out)
            self.assertIn("Uncontained execution is strictly prohibited", opt_in_out)

    def test_background_task_cancellation_kills_grandchild(self):
        from jarvis.core.task_manager import BackgroundTaskManager
        mgr = BackgroundTaskManager()
        # Launch powershell task that spawns a background python sleeper process
        ps_cmd = (
            f"$p = Start-Process -FilePath '{sys.executable}' -ArgumentList '-c \"import time; time.sleep(60)\"' -PassThru; "
            "$p.Id; Start-Sleep 60"
        )
        ok, msg, task_id = mgr.run_task(ps_cmd, task_name="Grandchild Leak Test", notify_voice=False)
        self.assertTrue(ok)
        self.assertIsNotNone(task_id)

        # Wait for task to output grandchild PID
        grandchild_pid = None
        t_deadline = time.monotonic() + 5.0
        while time.monotonic() < t_deadline:
            task = mgr._tasks.get(task_id)
            if task and task.stdout_lines:
                for line in task.stdout_lines:
                    line_s = line.strip()
                    if line_s.isdigit():
                        grandchild_pid = int(line_s)
                        break
            if grandchild_pid:
                break
            time.sleep(0.05)

        self.assertIsNotNone(grandchild_pid, "Failed to capture grandchild PID from background task")
        import psutil
        self.assertTrue(psutil.pid_exists(grandchild_pid), "Grandchild must be alive before cancel")

        # Cancel task and verify grandchild is killed via Job Object
        cancelled = mgr.cancel_task(task_id)
        self.assertTrue(cancelled)
        self.assertTrue(_wait_pid_gone(grandchild_pid), f"Grandchild PID {grandchild_pid} was not reaped on task cancel")

    def test_background_task_failed_assignment_kills_child_fail_closed(self):
        from jarvis.core.task_manager import BackgroundTaskManager
        mgr = BackgroundTaskManager()

        real_popen = subprocess.Popen
        spawned_procs = []

        def intercept_popen(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            spawned_procs.append(p)
            return p

        with mock.patch("subprocess.Popen", side_effect=intercept_popen), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=False):
            ok, msg, task_id = mgr.run_task("Start-Sleep -Seconds 60", task_name="Fail Assignment Test", notify_voice=False)

        self.assertFalse(ok)
        self.assertIsNone(task_id)
        self.assertIn("Failed to assign", msg)
        self.assertEqual(len(spawned_procs), 1)
        proc = spawned_procs[0]
        self.assertTrue(_wait_pid_gone(proc.pid), f"Child PID {proc.pid} survived failed Job Object assignment!")

    def test_background_task_failed_job_creation_kills_child_fail_closed(self):
        from jarvis.core.task_manager import BackgroundTaskManager
        mgr = BackgroundTaskManager()

        real_popen = subprocess.Popen
        spawned_procs = []

        def intercept_popen(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            spawned_procs.append(p)
            return p

        with mock.patch("subprocess.Popen", side_effect=intercept_popen), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=None):
            ok, msg, task_id = mgr.run_task("Start-Sleep -Seconds 60", task_name="Fail Creation Test", notify_voice=False)

        self.assertFalse(ok)
        self.assertIsNone(task_id)
        self.assertIn("Failed to create", msg)
        self.assertEqual(len(spawned_procs), 1)
        proc = spawned_procs[0]
        self.assertTrue(_wait_pid_gone(proc.pid), f"Child PID {proc.pid} survived failed Job Object creation!")

    def test_access_log_redaction_preserves_legitimate_query_params(self):
        import asyncio
        from jarvis.ui.server import AccessLogRedactionMiddleware

        received_scope = {}
        async def mock_app(scope, receive, send):
            received_scope.update(scope)

        middleware = AccessLogRedactionMiddleware(mock_app)
        raw_qs = b"sort_key=asc&search_key=ai&category=dev&token=SUPER_SECRET_123&bootstrap=BOOT_456"
        scope = {"type": "http", "query_string": raw_qs}

        asyncio.run(middleware(scope, None, None))

        # Downstream handler received completely unmodified query string
        self.assertEqual(received_scope["query_string"], raw_qs)
        # But scope['query_string'] post-execution is redacted for Uvicorn access logger
        post_qs = scope["query_string"]
        self.assertIn(b"sort_key=asc", post_qs)
        self.assertIn(b"search_key=ai", post_qs)
        self.assertIn(b"category=dev", post_qs)
        self.assertNotIn(b"SUPER_SECRET_123", post_qs)
        self.assertNotIn(b"BOOT_456", post_qs)
        self.assertIn(b"token=[REDACTED]", post_qs)
        self.assertIn(b"bootstrap=[REDACTED]", post_qs)

    def test_access_log_redaction_timing_at_response_start(self):
        import asyncio
        import logging
        from jarvis.ui.server import AccessLogRedactionMiddleware, SensitiveQueryRedactionFilter

        qs_at_endpoint = None
        qs_seen_by_uvicorn_send = None

        async def fake_uvicorn_send(message):
            nonlocal qs_seen_by_uvicorn_send
            if message.get("type") == "http.response.start":
                # Uvicorn formats access log during http.response.start by inspecting scope['query_string']
                qs_seen_by_uvicorn_send = scope.get("query_string")

        async def downstream_endpoint(scope, receive, send):
            nonlocal qs_at_endpoint
            # Downstream endpoint has uncorrupted access to authentic tokens & query params
            qs_at_endpoint = scope.get("query_string")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AccessLogRedactionMiddleware(downstream_endpoint)
        raw_qs = b"sort_key=asc&filter_key=1&token=SUPER_SECRET_TOKEN&bootstrap=BOOT_KEY"
        scope = {"type": "http", "query_string": raw_qs}

        asyncio.run(middleware(scope, None, fake_uvicorn_send))

        # 1. Downstream endpoint saw authentic, unmodified query string
        self.assertEqual(qs_at_endpoint, raw_qs)
        self.assertIn(b"SUPER_SECRET_TOKEN", qs_at_endpoint)

        # 2. Uvicorn saw redacted query string AT http.response.start BEFORE logging
        self.assertIsNotNone(qs_seen_by_uvicorn_send)
        self.assertNotIn(b"SUPER_SECRET_TOKEN", qs_seen_by_uvicorn_send)
        self.assertNotIn(b"BOOT_KEY", qs_seen_by_uvicorn_send)
        self.assertIn(b"token=[REDACTED]", qs_seen_by_uvicorn_send)
        self.assertIn(b"bootstrap=[REDACTED]", qs_seen_by_uvicorn_send)
        # Legitimate parameters are preserved
        self.assertIn(b"sort_key=asc", qs_seen_by_uvicorn_send)
        self.assertIn(b"filter_key=1", qs_seen_by_uvicorn_send)

        # 3. SensitiveQueryRedactionFilter also redacts string and preserves legitimate params
        redaction_filter = SensitiveQueryRedactionFilter()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:54321 - "GET /api?sort_key=asc&token=LEAKED_SECRET&filter_key=all HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        redaction_filter.filter(record)
        self.assertNotIn("LEAKED_SECRET", record.msg)
        self.assertIn("token=[REDACTED]", record.msg)
        self.assertIn("sort_key=asc", record.msg)
        self.assertIn("filter_key=all", record.msg)

    def test_access_log_redaction_percent_encoded_parameters(self):
        import asyncio
        from jarvis.ui.server import (
            AccessLogRedactionMiddleware,
            redact_query_string_bytes,
            redact_query_string_text,
        )

        # 1. Test direct byte parsing with percent-encoded sensitive parameters
        # '%74oken' is 'token', '%62ootstrap' is 'bootstrap', 't%6fken' is 'token', '%42OOTSTRAP' is 'BOOTSTRAP'
        raw_qs = (
            b"%74oken=SYNTHETIC_TOKEN_1&%62ootstrap=SYNTHETIC_BOOT_2&"
            b"t%6fken=SYNTHETIC_TOKEN_3&%42OOTSTRAP=SYNTHETIC_BOOT_4&"
            b"sort_key=asc&filter_key=enabled&token_type=bearer&count=42"
        )
        redacted_qs = redact_query_string_bytes(raw_qs)
        self.assertNotIn(b"SYNTHETIC_TOKEN_1", redacted_qs)
        self.assertNotIn(b"SYNTHETIC_BOOT_2", redacted_qs)
        self.assertNotIn(b"SYNTHETIC_TOKEN_3", redacted_qs)
        self.assertNotIn(b"SYNTHETIC_BOOT_4", redacted_qs)
        self.assertIn(b"%74oken=[REDACTED]", redacted_qs)
        self.assertIn(b"%62ootstrap=[REDACTED]", redacted_qs)
        self.assertIn(b"t%6fken=[REDACTED]", redacted_qs)
        self.assertIn(b"%42OOTSTRAP=[REDACTED]", redacted_qs)
        # Legitimate parameters are preserved
        self.assertIn(b"sort_key=asc", redacted_qs)
        self.assertIn(b"filter_key=enabled", redacted_qs)
        self.assertIn(b"token_type=bearer", redacted_qs)
        self.assertIn(b"count=42", redacted_qs)

        # 2. Test text redaction with percent-encoded parameters
        raw_text = 'GET /api?%74oken=SECRET_ABC&sort_key=asc&%62ootstrap=SECRET_XYZ HTTP/1.1'
        redacted_text = redact_query_string_text(raw_text)
        self.assertNotIn("SECRET_ABC", redacted_text)
        self.assertNotIn("SECRET_XYZ", redacted_text)
        self.assertIn("%74oken=[REDACTED]", redacted_text)
        self.assertIn("%62ootstrap=[REDACTED]", redacted_text)
        self.assertIn("sort_key=asc", redacted_text)

        # 3. Test through AccessLogRedactionMiddleware to verify downstream receives authentic raw query string
        downstream_scope_qs = None
        async def mock_endpoint(scope, receive, send):
            nonlocal downstream_scope_qs
            downstream_scope_qs = scope.get("query_string")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AccessLogRedactionMiddleware(mock_endpoint)
        scope = {"type": "http", "query_string": raw_qs}
        async def dummy_send(msg): pass
        asyncio.run(middleware(scope, None, dummy_send))

        # Downstream application received the authentic unmodified query string
        self.assertEqual(downstream_scope_qs, raw_qs)
        self.assertIn(b"SYNTHETIC_TOKEN_1", downstream_scope_qs)
        # But scope['query_string'] post-start is redacted
        self.assertEqual(scope["query_string"], redacted_qs)

    def test_actual_uvicorn_access_log_output_captures_and_redacts(self):
        import logging
        from jarvis.ui.server import attach_access_log_redaction_filters

        class CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []
                self.formatted_messages = []

            def emit(self, record):
                self.records.append(record)
                self.formatted_messages.append(self.format(record))

        capture = CaptureHandler()
        formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
        capture.setFormatter(formatter)

        uvicorn_access = logging.getLogger("uvicorn.access")
        old_handlers = list(uvicorn_access.handlers)
        old_filters = list(uvicorn_access.filters)
        old_level = uvicorn_access.level

        try:
            uvicorn_access.handlers.clear()
            uvicorn_access.filters.clear()
            uvicorn_access.setLevel(logging.INFO)
            uvicorn_access.addHandler(capture)

            attach_access_log_redaction_filters()

            # Emit log messages matching Uvicorn's access log emission patterns
            uvicorn_access.info(
                '127.0.0.1:51234 - "GET /hud?token=SYNTHETIC_CLEAR_TOKEN&sort_key=asc HTTP/1.1" 200'
            )
            uvicorn_access.info(
                '127.0.0.1:51234 - "GET /hud?%74oken=SYNTHETIC_PERCENT_TOKEN&%62ootstrap=SYNTHETIC_BOOT_TOKEN&page=1 HTTP/1.1" 200'
            )
            uvicorn_access.info(
                '%s - "%s %s HTTP/%s" %d',
                '127.0.0.1:51234',
                'GET',
                '/hud?%74oken=SYNTHETIC_TUPLE_TOKEN&tab=logs',
                '1.1',
                200,
            )

            all_logged_text = "\n".join(capture.formatted_messages)

            # Assert synthetic credentials NEVER appear in any captured log output
            self.assertNotIn("SYNTHETIC_CLEAR_TOKEN", all_logged_text)
            self.assertNotIn("SYNTHETIC_PERCENT_TOKEN", all_logged_text)
            self.assertNotIn("SYNTHETIC_BOOT_TOKEN", all_logged_text)
            self.assertNotIn("SYNTHETIC_TUPLE_TOKEN", all_logged_text)

            # Assert redaction placeholders are present
            self.assertIn("[REDACTED]", all_logged_text)

            # Assert legitimate non-sensitive parameters are intact
            self.assertIn("sort_key=asc", all_logged_text)
            self.assertIn("page=1", all_logged_text)
            self.assertIn("tab=logs", all_logged_text)
        finally:
            uvicorn_access.handlers = old_handlers
            uvicorn_access.filters = old_filters
            uvicorn_access.setLevel(old_level)

    def test_access_log_redaction_oversized_query_beginning_and_end(self):
        import asyncio
        from jarvis.ui.server import (
            AccessLogRedactionMiddleware,
            redact_query_string_bytes,
            redact_query_string_text,
        )

        # 1. Direct byte parsing with query string > 65,536 bytes (~82,000 bytes)
        filler = b"safe_param=valid_unremarkable_data_payload_string_123456789&" * 1500
        raw_qs = (
            b"%74oken=START_OVERSIZED_TOKEN&%62ootstrap=START_OVERSIZED_BOOT&"
            + filler
            + b"token=END_OVERSIZED_TOKEN&%62ootstrap=END_OVERSIZED_BOOT&sort_key=asc"
        )
        self.assertGreater(len(raw_qs), 65536, "Query string must exceed 65,536 bytes")

        redacted_qs = redact_query_string_bytes(raw_qs)
        # Sensitive values at both beginning and end must be redacted
        self.assertNotIn(b"START_OVERSIZED_TOKEN", redacted_qs)
        self.assertNotIn(b"START_OVERSIZED_BOOT", redacted_qs)
        self.assertNotIn(b"END_OVERSIZED_TOKEN", redacted_qs)
        self.assertNotIn(b"END_OVERSIZED_BOOT", redacted_qs)
        self.assertIn(b"%74oken=[REDACTED]", redacted_qs)
        self.assertIn(b"%62ootstrap=[REDACTED]", redacted_qs)
        self.assertIn(b"token=[REDACTED]", redacted_qs)
        # Legitimate query parameters are preserved
        self.assertIn(b"sort_key=asc", redacted_qs)
        self.assertIn(b"safe_param=valid_unremarkable_data_payload_string_123456789", redacted_qs)

        # 2. Text redaction with string > 65,536 characters
        filler_text = "safe_param=valid_payload&" * 3000
        raw_text = (
            "GET /api?%74oken=TEXT_START_SECRET&"
            + filler_text
            + "%62ootstrap=TEXT_END_SECRET&sort_key=asc HTTP/1.1"
        )
        self.assertGreater(len(raw_text), 65536)
        redacted_text = redact_query_string_text(raw_text)
        self.assertNotIn("TEXT_START_SECRET", redacted_text)
        self.assertNotIn("TEXT_END_SECRET", redacted_text)
        self.assertIn("%74oken=[REDACTED]", redacted_text)
        self.assertIn("%62ootstrap=[REDACTED]", redacted_text)
        self.assertIn("sort_key=asc", redacted_text)

        # 3. Middleware test: downstream receives full authentic unredacted query string
        downstream_scope_qs = None
        async def mock_endpoint(scope, receive, send):
            nonlocal downstream_scope_qs
            downstream_scope_qs = scope.get("query_string")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AccessLogRedactionMiddleware(mock_endpoint)
        scope = {"type": "http", "query_string": raw_qs}
        async def dummy_send(msg): pass
        asyncio.run(middleware(scope, None, dummy_send))

        self.assertEqual(downstream_scope_qs, raw_qs)
        self.assertIn(b"START_OVERSIZED_TOKEN", downstream_scope_qs)
        self.assertIn(b"END_OVERSIZED_TOKEN", downstream_scope_qs)
        # But scope['query_string'] at response start is redacted across its entire length
        self.assertEqual(scope["query_string"], redacted_qs)

    def test_end_to_end_live_uvicorn_percent_encoded_access_log_redaction(self):
        import logging
        import socket
        import threading
        import urllib.request
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from jarvis.ui.server import AccessLogRedactionMiddleware, attach_access_log_redaction_filters

        async def endpoint(request):
            return PlainTextResponse("LIVE_OK")

        app = Starlette(routes=[Route("/", endpoint)])
        app = AccessLogRedactionMiddleware(app)

        # Find free port
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        class LogCapture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []
                self.lines = []

            def emit(self, record):
                self.records.append(record)
                self.lines.append(self.format(record))

        capture = LogCapture()
        capture.setFormatter(logging.Formatter("%(name)s - %(message)s"))
        uv_access = logging.getLogger("uvicorn.access")
        old_handlers = list(uv_access.handlers)
        old_filters = list(uv_access.filters)
        old_level = uv_access.level

        try:
            uv_access.handlers.clear()
            uv_access.filters.clear()
            uv_access.setLevel(logging.INFO)
            uv_access.addHandler(capture)
            attach_access_log_redaction_filters()

            config = uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=port,
                access_log=True,
                log_level="info",
                log_config=None,
            )
            server = uvicorn.Server(config)
            server_thread = threading.Thread(target=server.run, daemon=True)
            server_thread.start()

            # Wait for server ready
            time.sleep(0.5)

            # Live request with percent-encoded tokens and authentic query parameters
            req_url = (
                f"http://127.0.0.1:{port}/?"
                "%74oken=LIVE_TEST_TOKEN_SECRET&"
                "%62ootstrap=LIVE_TEST_BOOT_SECRET&"
                "t%6fken=LIVE_TEST_MIXED_SECRET&"
                "sort_key=asc&filter_key=enabled"
            )
            resp = urllib.request.urlopen(req_url, timeout=5.0)
            body = resp.read().decode("utf-8")
            self.assertEqual(body, "LIVE_OK")

            # Allow uvicorn access log emission
            time.sleep(0.2)
            server.should_exit = True
            server_thread.join(timeout=3.0)

            all_captured_logs = "\n".join(capture.lines)

            # Assert synthetic credentials NEVER appear in live access log output
            self.assertNotIn("LIVE_TEST_TOKEN_SECRET", all_captured_logs)
            self.assertNotIn("LIVE_TEST_BOOT_SECRET", all_captured_logs)
            self.assertNotIn("LIVE_TEST_MIXED_SECRET", all_captured_logs)

            # Assert redaction placeholders are present in live log output
            self.assertIn("%74oken=[REDACTED]", all_captured_logs)
            self.assertIn("%62ootstrap=[REDACTED]", all_captured_logs)
            self.assertIn("t%6fken=[REDACTED]", all_captured_logs)

            # Assert legitimate query parameters are intact in live log output
            self.assertIn("sort_key=asc", all_captured_logs)
            self.assertIn("filter_key=enabled", all_captured_logs)
        finally:
            uv_access.handlers = old_handlers
            uv_access.filters = old_filters
            uv_access.setLevel(old_level)

    def test_access_log_redaction_queries_exceeding_10mb_failsafe(self):
        import asyncio
        from jarvis.ui.server import (
            AccessLogRedactionMiddleware,
            redact_query_string_bytes,
            redact_query_string_text,
            OVERSIZED_QUERY_PLACEHOLDER_BYTES,
            OVERSIZED_QUERY_PLACEHOLDER_TEXT,
            MAX_QUERY_STRING_BYTES,
        )

        # 1. Over-limit query string (>10 MB) with credentials near beginning and end
        prefix = b"%74oken=SUPER_SECRET_START_TOKEN&"
        suffix = b"&%62ootstrap=SUPER_SECRET_END_TOKEN"
        filler_needed = MAX_QUERY_STRING_BYTES - len(prefix) - len(suffix) + 512
        raw_qs = prefix + (b"f=1&" * (filler_needed // 4 + 1)) + suffix
        self.assertGreater(len(raw_qs), MAX_QUERY_STRING_BYTES, "Input must exceed 10 MB limit")

        # 2. Byte redaction returns fixed fail-safe placeholder
        redacted_qs = redact_query_string_bytes(raw_qs)
        self.assertEqual(redacted_qs, OVERSIZED_QUERY_PLACEHOLDER_BYTES)
        self.assertNotIn(b"SUPER_SECRET_START_TOKEN", redacted_qs)
        self.assertNotIn(b"SUPER_SECRET_END_TOKEN", redacted_qs)

        # 3. Text redaction returns fixed fail-safe placeholder
        raw_text = (
            "GET /api?%74oken=TEXT_START_OVER_10MB&"
            + ("a=1&" * (MAX_QUERY_STRING_BYTES // 4 + 100))
            + "%62ootstrap=TEXT_END_OVER_10MB HTTP/1.1"
        )
        self.assertGreater(len(raw_text), MAX_QUERY_STRING_BYTES)
        redacted_text = redact_query_string_text(raw_text)
        self.assertEqual(redacted_text, OVERSIZED_QUERY_PLACEHOLDER_TEXT)
        self.assertNotIn("TEXT_START_OVER_10MB", redacted_text)
        self.assertNotIn("TEXT_END_OVER_10MB", redacted_text)

        # 4. Middleware test: authentic query string is preserved for downstream handler,
        # but failsafe placeholder is set on scope['query_string'] at response start
        downstream_scope_qs = None
        async def mock_endpoint(scope, receive, send):
            nonlocal downstream_scope_qs
            downstream_scope_qs = scope.get("query_string")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AccessLogRedactionMiddleware(mock_endpoint)
        scope = {"type": "http", "query_string": raw_qs}
        async def dummy_send(msg): pass
        asyncio.run(middleware(scope, None, dummy_send))

        # Downstream saw unmodified authentic query string (including all credentials)
        self.assertEqual(downstream_scope_qs, raw_qs)
        self.assertIn(b"SUPER_SECRET_START_TOKEN", downstream_scope_qs)
        self.assertIn(b"SUPER_SECRET_END_TOKEN", downstream_scope_qs)

        # Access log formatting scope sees ONLY the fail-safe placeholder
        self.assertEqual(scope["query_string"], OVERSIZED_QUERY_PLACEHOLDER_BYTES)
        self.assertNotIn(b"SUPER_SECRET_START_TOKEN", scope["query_string"])
        self.assertNotIn(b"SUPER_SECRET_END_TOKEN", scope["query_string"])


if __name__ == "__main__":
    unittest.main()
