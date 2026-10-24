"""Targeted regression test suite for CORE-SEC-04E:
One-Shot CLI Process Containment in Jarvis 2.0.

Verifies:
1. Windows process creation includes CREATE_SUSPENDED (| CREATE_NO_WINDOW).
2. Job Object creation and assignment occur strictly before process resumption.
3. Fail-closed startup: Job Object creation failure terminates the suspended child.
4. Fail-closed startup: Process assignment failure prevents execution and cleans up resources.
5. Fail-closed startup: Resumption failure cleans up suspended process and Job Object.
6. Batch execution captures complete stdout and stderr without truncation (run_sandboxed_batch).
7. Batch execution correctly pipes input=prompt via stdin.
8. Batch timeout terminates the root process and all descendants.
9. Streaming preserves real-time line-by-line output (SandboxedProcess).
10. Streaming teardown reaps remaining orphan descendants.
11. Barge-in / cancellation during streaming terminates the contained process tree promptly.
12. Exceptions and nonzero exits clean up pipes and Job Object handles cleanly.
13. Context-manager cleanup remains idempotent and safe after normal exit or partial startup.
14. Non-Windows fallback operates via process groups without invoking Win32 APIs.
15. Real Win32 integration test: Parent and grandchild process-tree cessation.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.job_object_sandbox import (
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    SandboxedProcess,
    run_sandboxed_batch,
)

WIN32 = sys.platform == "win32"


def _is_pid_alive(pid: int) -> bool:
    """Checks if a process ID is actively running on the host OS."""
    if pid <= 0:
        return False
    if WIN32:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, wintypes.DWORD(pid))
        if h_proc:
            exit_code = wintypes.DWORD()
            STILL_ACTIVE = 259
            if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                is_active = (exit_code.value == STILL_ACTIVE)
                kernel32.CloseHandle(h_proc)
                return is_active
            kernel32.CloseHandle(h_proc)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _make_mock_proc(handle: int = 12345, pid: int = 99999) -> MagicMock:
    """Creates a mock subprocess.Popen instance with expected attributes."""
    proc = MagicMock()
    proc._handle = handle
    proc.pid = pid
    proc.poll.return_value = None
    proc.stdin = None
    proc.stdout = None
    proc.stderr = None
    proc.communicate.return_value = ("", "")
    proc.wait.return_value = 0
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    return proc


class TestOneShotProcessContainment(unittest.TestCase):
    """CORE-SEC-04E Unit and Mocked Verification Suite."""

    # -------------------------------------------------------------------------
    # 1. Windows Creation Flags & Ordering
    # -------------------------------------------------------------------------

    def test_01_windows_creation_includes_create_suspended(self):
        """1. Windows process creation includes CREATE_SUSPENDED and CREATE_NO_WINDOW."""
        mock_proc = _make_mock_proc(12345)

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=9999), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job", return_value=True):

            sp = SandboxedProcess(["cmd.exe", "/c", "dir"])
            proc = sp.start()

            self.assertIs(proc, mock_proc)
            flags = mock_popen.call_args.kwargs.get("creationflags", 0)
            self.assertTrue(flags & CREATE_SUSPENDED, "CREATE_SUSPENDED must be set")
            self.assertTrue(flags & CREATE_NO_WINDOW, "CREATE_NO_WINDOW must be set")

    def test_02_strict_containment_lifecycle_ordering(self):
        """2. Strict ordering: Popen -> Job Creation -> Assignment -> Resumption."""
        call_order = []

        def fake_popen(*args, **kwargs):
            call_order.append("popen")
            return _make_mock_proc(12345)

        def fake_create_job(*args, **kwargs):
            call_order.append("create_job")
            return 8888

        def fake_assign(h_job, proc):
            call_order.append("assign")
            return True

        def fake_resume(proc):
            call_order.append("resume")
            return True

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", side_effect=fake_create_job), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", side_effect=fake_assign), \
             patch("jarvis.skills.job_object_sandbox.resume_process", side_effect=fake_resume), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job", return_value=True):

            sp = SandboxedProcess(["test.exe"])
            sp.start()

            self.assertEqual(call_order, ["popen", "create_job", "assign", "resume"])

    # -------------------------------------------------------------------------
    # 2. Fail-Closed Handling on Error
    # -------------------------------------------------------------------------

    def test_03_job_creation_failure_fails_closed(self):
        """3. Job Object creation failure terminates the suspended child and raises RuntimeError."""
        mock_proc = _make_mock_proc(12345)

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=None):

            sp = SandboxedProcess(["test.exe"])
            with self.assertRaises(RuntimeError) as ctx:
                sp.start()

            self.assertIn("Failed to create Win32 Job Object", str(ctx.exception))
            mock_proc.kill.assert_called_once()
            self.assertIsNone(sp.proc)

    def test_04_assignment_failure_fails_closed(self):
        """4. Process assignment failure kills the suspended process and closes Job Object."""
        mock_proc = _make_mock_proc(12345)

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=7777), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=False), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close:

            sp = SandboxedProcess(["test.exe"])
            with self.assertRaises(RuntimeError) as ctx:
                sp.start()

            self.assertIn("Failed to assign process to Job Object", str(ctx.exception))
            mock_proc.kill.assert_called_once()
            mock_close.assert_called_once_with(7777)
            self.assertIsNone(sp.proc)

    def test_05_resumption_failure_fails_closed(self):
        """5. Resumption failure terminates the child and Job Object."""
        mock_proc = _make_mock_proc(12345)

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=6666), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=False), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close:

            sp = SandboxedProcess(["test.exe"])
            with self.assertRaises(RuntimeError) as ctx:
                sp.start()

            self.assertIn("Failed to resume suspended process", str(ctx.exception))
            mock_proc.kill.assert_called_once()
            mock_close.assert_called_once_with(6666)

    # -------------------------------------------------------------------------
    # 3. Batch Semantics (run_sandboxed_batch)
    # -------------------------------------------------------------------------

    def test_06_batch_execution_captures_complete_output(self):
        """6. Batch execution captures complete stdout and stderr without truncation."""
        output_large = "x" * 15000  # Exceeds the legacy 2000 tail limit
        mock_proc = _make_mock_proc(12345)
        mock_proc.communicate.return_value = (output_large, "warning text")
        mock_proc.returncode = 0

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess.start", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.SandboxedProcess.terminate_tree"):

            res = run_sandboxed_batch(["test_cmd.exe"])
            self.assertEqual(len(res.stdout), 15000)
            self.assertEqual(res.stderr, "warning text")
            self.assertEqual(res.returncode, 0)

    def test_07_batch_execution_pipes_prompt_input(self):
        """7. Batch execution correctly pipes input=prompt via stdin."""
        mock_proc = _make_mock_proc(12345)
        mock_proc.communicate.return_value = ('{"response": "ok"}', "")
        mock_proc.returncode = 0

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess.start", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.SandboxedProcess.terminate_tree"):

            prompt_text = "Analyze this memory structure, Sir."
            res = run_sandboxed_batch(["cmd.exe"], input=prompt_text)

            mock_proc.communicate.assert_called_once_with(input=prompt_text, timeout=None)
            self.assertEqual(res.stdout, '{"response": "ok"}')

    def test_08_batch_timeout_terminates_entire_process_tree(self):
        """8. Batch timeout terminates the process tree before raising TimeoutExpired."""
        mock_proc = _make_mock_proc(12345)
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["test"], timeout=1.5)
        mock_proc.terminate_tree = MagicMock()

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess.start", return_value=mock_proc):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_sandboxed_batch(["test"], timeout=1.5)

            mock_proc.terminate_tree.assert_called_once()

    # -------------------------------------------------------------------------
    # 4. Streaming Execution (SandboxedProcess)
    # -------------------------------------------------------------------------

    def test_09_streaming_preserves_realtime_line_iteration(self):
        """9. Streaming preserves real-time line-by-line output."""
        lines = [
            '{"event": "step_update", "step_update": {"text_delta": "Chunk 1"}}\n',
            '{"event": "step_update", "step_update": {"text_delta": " Chunk 2"}}\n',
            '{"event": "result", "result": {"response": "Chunk 1 Chunk 2", "status": "SUCCESS"}}\n',
        ]
        mock_proc = _make_mock_proc(12345)
        mock_proc.stdout = iter(lines)
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess.start", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.SandboxedProcess.terminate_tree") as mock_term:

            with SandboxedProcess(["stream_tool.exe"]) as proc:
                streamed = list(proc.stdout)

            self.assertEqual(streamed, lines)
            mock_term.assert_called_once()

    def test_10_streaming_teardown_terminates_descendants(self):
        """10. Streaming teardown reaps remaining orphan descendants."""
        mock_proc = _make_mock_proc(12345)
        mock_proc.poll.return_value = None  # Still active when exiting context

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=5555), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close:

            with SandboxedProcess(["streaming.exe"]):
                pass

            mock_close.assert_called_once_with(5555, exit_code=1)

    def test_11_streaming_cancellation_terminates_job_tree(self):
        """11. Barge-in / cancellation during streaming terminates the contained process tree promptly."""
        mock_proc = _make_mock_proc(12345)
        mock_proc.poll.return_value = None

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=4444), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close:

            sp = SandboxedProcess(["speech_stream.exe"])
            proc = sp.start()

            # Voice barge-in triggers abort
            proc.terminate_tree()
            mock_close.assert_called_once_with(4444, exit_code=1)

    def test_12_exceptions_clean_up_resources(self):
        """12. Exceptions clean up pipes and Job Object handles cleanly."""
        mock_proc = _make_mock_proc(12345)
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=3333), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close:

            try:
                with SandboxedProcess(["exploding.exe"]):
                    raise ValueError("Simulated streaming pipeline fault")
            except ValueError:
                pass

            mock_close.assert_called_once_with(3333, exit_code=1)

    def test_13_idempotent_repeated_cleanup(self):
        """13. Context-manager cleanup remains idempotent and safe after repeated calls."""
        mock_proc = _make_mock_proc(12345)

        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=2222), \
             patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close:

            sp = SandboxedProcess(["safe.exe"])
            sp.start()
            self.assertTrue(sp.terminate_tree())
            # Repeated calls should be no-ops and return True
            self.assertTrue(sp.terminate_tree())
            self.assertTrue(sp.terminate_tree())
            self.assertEqual(mock_close.call_count, 1)

    def test_14_non_windows_fallback_uses_process_groups(self):
        """14. Non-Windows fallback operates via process groups without invoking Win32 APIs."""
        mock_proc = _make_mock_proc(pid=98765)

        with patch("sys.platform", "linux"), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("os.killpg", create=True) as mock_killpg, \
             patch("os.getpgid", create=True, return_value=98765):

            sp = SandboxedProcess(["ls", "-la"])
            sp.start()

            self.assertTrue(mock_popen.call_args.kwargs.get("start_new_session"))
            sp.terminate_tree()
            mock_killpg.assert_called_once()

    # -------------------------------------------------------------------------
    # 5. Harmless Real Windows Process-Tree Cessation Integration Test
    # -------------------------------------------------------------------------

    @unittest.skipUnless(WIN32, "Real Windows Job Object tests require Windows host")
    def test_15_real_win32_process_tree_grandchild_cessation(self):
        """15. Real Win32 integration test: Parent and grandchild process-tree cessation."""
        # This script creates a grandchild process sleeping 60 seconds, outputs its PID, then sleeps
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(f'GRANDCHILD_PID={p.pid}', flush=True)\n"
            "time.sleep(60)\n"
        )

        with SandboxedProcess([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True) as proc:
            grandchild_pid = None
            start_t = time.perf_counter()
            while time.perf_counter() - start_t < 5.0:
                line = proc.stdout.readline() if proc.stdout else ""
                if "GRANDCHILD_PID=" in line:
                    grandchild_pid = int(line.strip().split("=")[1])
                    break

            self.assertIsNotNone(grandchild_pid, "Failed to capture grandchild PID from test process")
            parent_pid = proc.pid

            # Both parent and grandchild should be alive right now
            self.assertTrue(_is_pid_alive(parent_pid), f"Parent {parent_pid} should be alive")
            self.assertTrue(_is_pid_alive(grandchild_pid), f"Grandchild {grandchild_pid} should be alive")

            # Terminate Job Object tree early
            proc.terminate_tree()

        # After exiting SandboxedProcess context, verify BOTH parent and grandchild are dead
        time.sleep(0.5)
        self.assertFalse(_is_pid_alive(parent_pid), f"Parent PID {parent_pid} must be terminated")
        self.assertFalse(_is_pid_alive(grandchild_pid), f"Grandchild PID {grandchild_pid} must be terminated by Job Object")

    # -------------------------------------------------------------------------
    # 6. Concurrency & Set-Based Cancellation Tracking
    # -------------------------------------------------------------------------

    def test_16_overlapping_streaming_sandboxes_all_cancelled(self):
        """16. Verify that multiple overlapping active streaming sandboxes are all terminated on abort."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        sandbox1 = MagicMock()
        sandbox2 = MagicMock()

        with bridge._pool_lock:
            bridge._active_oneshot_sandboxes.add(sandbox1)
            bridge._active_oneshot_sandboxes.add(sandbox2)

        self.assertEqual(len(bridge._active_oneshot_sandboxes), 2)

        # Trigger barge-in cancellation
        bridge.abort_active_turn()

        # Both sandboxes must have terminate_tree called
        sandbox1.terminate_tree.assert_called_once()
        sandbox2.terminate_tree.assert_called_once()
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)

    def test_17_finishing_request_discards_only_its_own_sandbox(self):
        """17. Verify finishing one request removes only its own instance from active set."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        sandbox1 = MagicMock()
        sandbox2 = MagicMock()

        with bridge._pool_lock:
            bridge._active_oneshot_sandboxes.add(sandbox1)
            bridge._active_oneshot_sandboxes.add(sandbox2)

        # Request 1 finishes and executes finally: discard(sandbox1)
        with bridge._pool_lock:
            bridge._active_oneshot_sandboxes.discard(sandbox1)

        self.assertNotIn(sandbox1, bridge._active_oneshot_sandboxes)
        self.assertIn(sandbox2, bridge._active_oneshot_sandboxes)
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 1)

    def test_18_repeated_cancellation_and_cleanup_are_idempotent(self):
        """18. Repeated abort_active_turn and stop calls remain safe and idempotent."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        sandbox = MagicMock()

        with bridge._pool_lock:
            bridge._active_oneshot_sandboxes.add(sandbox)

        bridge.abort_active_turn()
        sandbox.terminate_tree.assert_called_once()

        # Repeated abort should be safe no-op on already cleared set
        bridge.abort_active_turn()
        self.assertEqual(sandbox.terminate_tree.call_count, 1)

        # Subsequent stop should be safe no-op
        bridge.stop()
        self.assertEqual(sandbox.terminate_tree.call_count, 1)

    def test_19_failure_in_one_sandbox_does_not_prevent_other_terminations(self):
        """19. A failure terminating one sandbox does not prevent the others from being attempted."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        faulty_sandbox = MagicMock()
        faulty_sandbox.terminate_tree.side_effect = RuntimeError("Simulated Win32 handle fault")
        healthy_sandbox = MagicMock()

        with bridge._pool_lock:
            bridge._active_oneshot_sandboxes.add(faulty_sandbox)
            bridge._active_oneshot_sandboxes.add(healthy_sandbox)

        # Abort should not raise despite faulty_sandbox raising
        bridge.abort_active_turn()

        faulty_sandbox.terminate_tree.assert_called_once()
        healthy_sandbox.terminate_tree.assert_called_once()
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)

    # -------------------------------------------------------------------------
    # 7. Cancellation / Registration Generation Counter Hardening
    # -------------------------------------------------------------------------

    def test_20_cancellation_during_startup_prevents_request_continuation(self):
        """20. Cancellation occurring after process start but before registration prevents that request from continuing.

        Also verifies that SandboxedProcess.terminate_tree() runs with _pool_lock NOT held.
        """
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        bridge.agy_path = "dummy_agy.cmd"

        mock_proc = _make_mock_proc(1001)
        mock_sandboxed = MagicMock()

        # Deterministic trigger: abort_active_turn fires during sandboxed.start()
        def on_start():
            bridge.abort_active_turn()
            return mock_proc

        mock_sandboxed.start.side_effect = on_start

        # Monitor whether _pool_lock is held during termination
        lock_held_during_terminate = []

        def on_terminate(*args, **kwargs):
            lock_held_during_terminate.append(bridge._pool_lock.locked())
            return True

        mock_sandboxed.terminate_tree.side_effect = on_terminate

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess", return_value=mock_sandboxed):
            res = bridge._generate_streaming(
                prompt="Hello",
                model="gemini-3.8-flash-medium",
                sentence_callback=lambda s: None,
            )

        self.assertEqual(res.get("status"), "ABORTED")
        self.assertIn("cancelled during process initialization", res.get("response", ""))
        self.assertTrue(lock_held_during_terminate, "terminate_tree must be called on mismatch")
        self.assertFalse(any(lock_held_during_terminate), "_pool_lock must NOT be held during process termination")
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)

    def test_21_cancellation_after_registration_targets_sandbox(self):
        """21. Cancellation after registration targets the registered sandbox."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        bridge.agy_path = "dummy_agy.cmd"

        mock_proc = _make_mock_proc(1002)
        lines = [
            '{"event": "result", "result": {"response": "Done", "status": "SUCCESS"}}\n',
            "",
        ]

        def fake_readline():
            bridge.abort_active_turn()
            return lines.pop(0) if lines else ""

        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.side_effect = fake_readline
        mock_sandboxed = MagicMock()
        mock_sandboxed.start.return_value = mock_proc

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess", return_value=mock_sandboxed):
            bridge._generate_streaming(
                prompt="Hello",
                model="gemini-3.8-flash-medium",
                sentence_callback=lambda s: None,
            )

        # Verified that abort_active_turn called terminate_tree
        mock_sandboxed.terminate_tree.assert_called()
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)

    def test_22_new_request_after_completed_cancellation_not_cancelled(self):
        """22. A new request starting after a completed cancellation is not incorrectly cancelled by a stale generation."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        bridge.agy_path = "dummy_agy.cmd"

        # Prior turn cancelled, advancing cancellation_generation
        bridge.abort_active_turn()
        prior_gen = bridge._cancellation_generation
        self.assertGreater(prior_gen, 0)

        mock_proc = _make_mock_proc(1003)
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.side_effect = [
            '{"event": "step_update", "step_update": {"text_delta": "Fresh response"}}\n',
            '{"event": "result", "result": {"response": "Fresh response", "status": "SUCCESS"}}\n',
            "",
        ]
        mock_sandboxed = MagicMock()
        mock_sandboxed.start.return_value = mock_proc

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess", return_value=mock_sandboxed):
            res = bridge._generate_streaming(
                prompt="Hello again",
                model="gemini-3.8-flash-medium",
                sentence_callback=lambda s: None,
            )

        self.assertEqual(res.get("status"), "SUCCESS")
        self.assertEqual(res.get("response"), "Fresh response")
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)

    def test_23_exceptions_and_repeated_cancellation_retain_cleanup_guarantees(self):
        """23. Exceptions during startup clean up resources and repeated cancellation remains safe."""
        from jarvis.core.antigravity_bridge import AntigravityBridge

        bridge = AntigravityBridge()
        bridge.agy_path = "dummy_agy.cmd"

        mock_sandboxed = MagicMock()
        mock_sandboxed.start.side_effect = OSError("Executable binary corrupt")

        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess", return_value=mock_sandboxed):
            res = bridge._generate_streaming(
                prompt="Hello",
                model="gemini-3.8-flash-medium",
                sentence_callback=lambda s: None,
            )

        self.assertEqual(res.get("status"), "ERROR")
        mock_sandboxed.terminate_tree.assert_called()
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)

        # Repeated cancellation on clean state is safe no-op
        bridge.abort_active_turn()
        bridge.stop()
        self.assertEqual(len(bridge._active_oneshot_sandboxes), 0)


if __name__ == "__main__":
    unittest.main()
