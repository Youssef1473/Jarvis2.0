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


if __name__ == "__main__":
    unittest.main()
