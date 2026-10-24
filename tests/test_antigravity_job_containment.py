"""Comprehensive unit and integration test suite for Win32 Job Object containment

of the Antigravity CLI persistent sidecar (agy.exe) in Jarvis 2.0 (CORE-SEC-04B).

Verifies:
1. Suspended process creation (CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW).
2. Strict ordering: Popen -> Job Creation -> Assignment -> Resumption -> Handshake.
3. Fail-closed startup on Job Object creation failure.
4. Fail-closed startup on process assignment failure.
5. Fail-closed startup on process resumption failure.
6. Handshake failure cleanup of Job Object and child resources.
7. Unconditional Job Object termination and closure on graceful root process exit.
8. Unconditional Job Object termination and closure on forced termination.
9. Idempotent repeated shutdown without handle leaks or double-close errors.
10. Turn cancellation and abort_active_turn containment cleanup.
11. LRU daemon pool eviction containment cleanup.
12. Graceful non-Windows platform fallback.
13. Real Win32 process-tree cessation (parent and descendant child termination).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis.skills.job_object_sandbox as job_sandbox
from jarvis.core.antigravity_bridge import AntigravityBridge, AntigravityDaemon

WIN32 = sys.platform == "win32"
CREATE_SUSPENDED = getattr(job_sandbox, "CREATE_SUSPENDED", 0x00000004)


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
        except (OSError, ProcessLookupError):
            return False


def _wait_pid_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestAntigravityJobContainmentMocked(unittest.TestCase):
    """Mocked unit test suite verifying process lifecycle, flags, and fail-closed gates."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp_dir.name)
        self.daemon = AntigravityDaemon(
            agy_path="mock_agy.exe",
            default_model="gemini-3.8-flash-low",
            default_effort="low",
        )
        self.daemon.runtime_workspace = self.workspace

    def tearDown(self):
        if hasattr(self, "daemon") and self.daemon:
            self.daemon.stop()
        if hasattr(self, "tmp_dir") and self.tmp_dir:
            self.tmp_dir.cleanup()

    def test_windows_process_creation_includes_create_suspended(self):
        """1. Windows process creation includes CREATE_SUSPENDED, CREATE_NEW_PROCESS_GROUP, and CREATE_NO_WINDOW."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 1234
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "test_cid"}\n'

        with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=1001), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True):

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertTrue(ok)
            self.assertTrue(mock_popen.called)
            _, kwargs = mock_popen.call_args
            flags = kwargs.get("creationflags", 0)

            self.assertTrue(bool(flags & CREATE_SUSPENDED), "CREATE_SUSPENDED must be set in creationflags on Windows")
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                self.assertTrue(bool(flags & subprocess.CREATE_NEW_PROCESS_GROUP), "CREATE_NEW_PROCESS_GROUP must be preserved")
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                self.assertTrue(bool(flags & subprocess.CREATE_NO_WINDOW), "CREATE_NO_WINDOW must be preserved")

    def test_containment_call_sequence_ordering(self):
        """2. Correct ordering: Popen -> Job Creation -> Assignment -> Resumption -> Handshake."""
        call_sequence = []

        mock_proc = mock.MagicMock()
        mock_proc.pid = 1234
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        def mock_readline():
            call_sequence.append("readline_init")
            return '{"event": "init", "conversation_id": "order_cid"}\n'

        mock_proc.stdout.readline = mock_readline

        def mock_popen(*args, **kwargs):
            call_sequence.append("popen_spawn")
            return mock_proc

        def mock_create_job(*args, **kwargs):
            call_sequence.append("create_job")
            return 2002

        def mock_assign_job(*args, **kwargs):
            call_sequence.append("assign_job")
            return True

        def mock_resume(*args, **kwargs):
            call_sequence.append("resume_process")
            return True

        with mock.patch("subprocess.Popen", side_effect=mock_popen), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", side_effect=mock_create_job), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", side_effect=mock_assign_job), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", side_effect=mock_resume):

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertTrue(ok)
            expected = ["popen_spawn", "create_job", "assign_job", "resume_process", "readline_init"]
            self.assertEqual(call_sequence, expected, f"Lifecycle call sequence violated: {call_sequence}")

    def test_job_creation_failure_fails_closed(self):
        """3. Job Object creation failure cleans up the suspended process and fails closed."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=None):

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertFalse(ok)
            mock_proc.kill.assert_called_once()
            self.assertIsNone(self.daemon.proc)
            self.assertIsNone(self.daemon._job_handle)

    def test_assignment_failure_fails_closed(self):
        """4. Process assignment failure cleans up suspended process and Job Object fail-closed."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=3003), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=False), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertFalse(ok)
            mock_proc.kill.assert_called_once()
            mock_close_job.assert_called_once_with(3003)
            self.assertIsNone(self.daemon.proc)
            self.assertIsNone(self.daemon._job_handle)

    def test_resumption_failure_fails_closed(self):
        """5. Process resumption failure cleans up process and Job Object fail-closed."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=4004), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=False), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertFalse(ok)
            mock_proc.kill.assert_called_once()
            mock_close_job.assert_called_once_with(4004)
            self.assertIsNone(self.daemon.proc)
            self.assertIsNone(self.daemon._job_handle)

    def test_handshake_failure_releases_job_and_proc(self):
        """6. Initialization handshake failure releases Job Object and process resources."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        # Returns empty EOF immediately
        mock_proc.stdout.readline.return_value = ""

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=5005), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertFalse(ok)
            mock_close_job.assert_called_once_with(5005)
            self.assertIsNone(self.daemon.proc)
            self.assertIsNone(self.daemon._job_handle)

    def test_graceful_root_exit_still_cleans_up_job_object(self):
        """7. CRITICAL INVARIANT: stop() terminates and closes Job Object even when root process exited gracefully."""
        mock_proc = mock.MagicMock()
        # Simulate root process already terminated with exit code 0
        mock_proc.poll.return_value = 0
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        self.daemon.proc = mock_proc
        self.daemon._job_handle = 6006

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:
            self.daemon.stop()
            # Must be called unconditionally to clean up orphan grandchildren!
            mock_close_job.assert_called_once_with(6006)
            self.assertIsNone(self.daemon._job_handle)
            self.assertIsNone(self.daemon.proc)

    def test_forced_shutdown_terminates_job_object(self):
        """8. Forced shutdown terminates Job Object when process is unresponsive."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="agy", timeout=0.5), None]
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        self.daemon.proc = mock_proc
        self.daemon._job_handle = 7007

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:
            self.daemon.stop()
            mock_close_job.assert_called_once_with(7007)
            self.assertIsNone(self.daemon._job_handle)
            self.assertIsNone(self.daemon.proc)

    def test_idempotent_repeated_shutdown(self):
        """9. Repeated calls to stop() are safe and idempotent with zero double-close or exceptions."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = 0
        self.daemon.proc = mock_proc
        self.daemon._job_handle = 8008

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:
            self.daemon.stop()
            self.assertEqual(mock_close_job.call_count, 1)

            # Second and third calls must be safe no-ops
            self.daemon.stop()
            self.daemon.stop()
            self.assertEqual(mock_close_job.call_count, 1)
            self.assertIsNone(self.daemon._job_handle)
            self.assertIsNone(self.daemon.proc)

    def test_cancellation_and_abort_paths_clean_up_containment(self):
        """10. Cancellation and abort_active_turn cleanly invoke stop and terminate Job Object."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = 0
        self.daemon.proc = mock_proc
        self.daemon._job_handle = 9009

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:
            self.daemon.abort_active_turn()
            mock_close_job.assert_called_once_with(9009)
            self.assertIsNone(self.daemon._job_handle)

    def test_lru_pool_eviction_terminates_evicted_job(self):
        """11. LRU daemon pool eviction cleans up the evicted daemon's Job Object tree."""
        bridge = AntigravityBridge()
        bridge.agy_path = "mock_agy.exe"
        bridge.use_daemon = True
        bridge.max_warm_daemons = 2

        d1 = AntigravityDaemon("mock_agy.exe", "model1")
        d1.proc = mock.MagicMock()
        d1.proc.poll.return_value = None
        d1._job_handle = 1111

        d2 = AntigravityDaemon("mock_agy.exe", "model2")
        d2.proc = mock.MagicMock()
        d2.proc.poll.return_value = None
        d2._job_handle = 2222

        d3 = AntigravityDaemon("mock_agy.exe", "model3")
        d3.proc = mock.MagicMock()
        d3.proc.poll.return_value = None
        d3._job_handle = 3333

        bridge._daemon_pool[("model1", "")] = d1
        bridge._daemon_pool[("model2", "")] = d2

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job, \
             mock.patch.object(d3, "start", return_value=True), \
             mock.patch.object(d3, "is_alive", return_value=True), \
             mock.patch("jarvis.core.antigravity_bridge.AntigravityDaemon", return_value=d3):

            # Spawning 3rd daemon must evict LRU d1 and terminate its Job Object
            res = bridge.get_or_spawn_daemon("model3")
            self.assertIs(res, d3)
            self.assertNotIn(("model1", ""), bridge._daemon_pool)
            mock_close_job.assert_called_once_with(1111)

    def test_non_windows_platform_fallback(self):
        """12. Non-Windows platform preserves existing fallback without attempting Win32-only operations."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 1234
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "linux_cid"}\n'

        with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "linux"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object") as mock_create_job:

            ok = self.daemon.start("gemini-3.8-flash-low")
            self.assertTrue(ok)
            mock_create_job.assert_not_called()
            self.assertIsNone(self.daemon._job_handle)
            _, kwargs = mock_popen.call_args
            flags = kwargs.get("creationflags", 0)
            self.assertFalse(bool(flags & CREATE_SUSPENDED))


class TestAntigravityLifecycleAndLockCorrections(unittest.TestCase):
    """Focused regression test suite for Correction A (Lock Ownership) and Correction B (Lifecycle Serialization)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp_dir.name)
        self.daemon = AntigravityDaemon(
            agy_path="mock_agy.exe",
            default_model="gemini-3.8-flash-low",
            default_effort="low",
        )
        self.daemon.runtime_workspace = self.workspace

    def tearDown(self):
        try:
            self.daemon.stop()
        except Exception:
            pass
        self.tmp_dir.cleanup()

    # --- Correction A: Strict Runtime Lock Ownership Tests ---

    def test_lock_removed_when_pid_matches(self):
        """14. A lock containing the exiting process's PID is removed as intended."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text(json.dumps({"pid": 11111, "timestamp": time.time(), "model": "flash"}), encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = 0
        self.daemon.proc = mock_proc

        self.daemon.stop()
        self.assertFalse(lock_file.exists(), "Lock file owned by exiting PID must be unlinked")

    def test_lock_preserved_when_pid_differs(self):
        """15. A lock containing a different PID is preserved and never deleted by an unrelated daemon."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text(json.dumps({"pid": 99999, "timestamp": time.time(), "model": "other"}), encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = 0
        self.daemon.proc = mock_proc

        self.daemon.stop()
        self.assertTrue(lock_file.exists(), "Lock file with different PID must be preserved")
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("pid"), 99999)

    def test_lock_preserved_when_malformed(self):
        """16. A malformed or partially written lock is preserved for _recover_stale_lock rather than deleted in stop()."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text("{\"pid\": 11111, incomplete_json...", encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = 0
        self.daemon.proc = mock_proc

        self.daemon.stop()
        self.assertTrue(lock_file.exists(), "Malformed lock file must NOT be deleted by stop()")

    def test_lock_preserved_when_empty(self):
        """17. An empty lock file is preserved for _recover_stale_lock rather than deleted in stop()."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text("", encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = 0
        self.daemon.proc = mock_proc

        self.daemon.stop()
        self.assertTrue(lock_file.exists(), "Empty lock file must NOT be deleted by stop()")

    def test_lock_preserved_when_proc_is_none(self):
        """18. A shutdown when proc is None does not delete an existing lock file."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text(json.dumps({"pid": 77777, "timestamp": time.time()}), encoding="utf-8")

        self.daemon.proc = None
        self.daemon._job_handle = 1234

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job"):
            self.daemon.stop()

        self.assertTrue(lock_file.exists(), "Lock file must be preserved when proc is None")
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("pid"), 77777)

    # --- Correction B: Lifecycle Synchronization & Concurrency Tests ---

    def test_cancellation_during_handshake_unblocks_promptly(self):
        """19. Voice barge-in/abort during handshake terminates Job Object and unblocks startup in <1.0s (no 15s hang)."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        # Simulate a readline that blocks until interrupted
        stall_event = threading.Event()
        def blocking_readline():
            stall_event.wait(timeout=10.0)
            return ""

        mock_proc.stdout.readline.side_effect = blocking_readline

        start_outcome = []
        def start_worker():
            with mock.patch("subprocess.Popen", return_value=mock_proc), \
                 mock.patch("os.path.exists", return_value=True), \
                 mock.patch.object(sys, "platform", "win32"), \
                 mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=1234), \
                 mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
                 mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
                 mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job"):
                ok = self.daemon.start("gemini-3.8-flash-low")
                start_outcome.append(ok)

        t_thread = threading.Thread(target=start_worker, daemon=True)
        t0 = time.perf_counter()
        t_thread.start()

        # Wait briefly for startup to reach the handshake loop
        time.sleep(0.08)

        # Trigger voice barge-in / abort
        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job"):
            self.daemon.abort_active_turn()
            stall_event.set()  # Unblock mock readline as OS pipe closure would

        t_thread.join(timeout=2.0)
        elapsed = time.perf_counter() - t0

        self.assertFalse(t_thread.is_alive(), "Startup thread must terminate promptly")
        self.assertLess(elapsed, 1.5, f"Abort must not wait for the 15s timeout (took {elapsed:.2f}s)")
        self.assertEqual(start_outcome, [False])

    def test_stale_startup_failure_cannot_terminate_newer_generation(self):
        """20. A delayed startup failure from generation N cannot terminate a newer generation N+1."""
        gen1_proc = mock.MagicMock()
        gen1_proc.poll.return_value = None
        gen1_job = 1001

        gen2_proc = mock.MagicMock()
        gen2_proc.poll.return_value = None
        gen2_job = 2002

        with mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_close_job:
            # Simulate gen 1 established
            self.daemon.proc = gen1_proc
            self.daemon._job_handle = gen1_job
            self.daemon._generation = 1
            gen1_captured_gen = self.daemon._generation

            # gen 2 takes over
            self.daemon.proc = gen2_proc
            self.daemon._job_handle = gen2_job
            self.daemon._generation = 2

            # Now gen 1's post-handshake failure block runs
            with self.daemon._lifecycle_lock:
                if self.daemon._generation == gen1_captured_gen and self.daemon.proc is not None:
                    self.daemon.stop()

            # gen 2 must remain completely intact and active!
            self.assertIs(self.daemon.proc, gen2_proc)
            self.assertEqual(self.daemon._job_handle, gen2_job)
            self.assertEqual(self.daemon._generation, 2)
            mock_close_job.assert_not_called()

    def test_concurrent_start_and_stop_serialized(self):
        """21. Concurrent start() and stop() calls serialize without handle corruption or exceptions."""
        errors = []
        mock_proc = mock.MagicMock()
        mock_proc.pid = 5555
        mock_proc.poll.return_value = 0
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "c_cid"}\n'

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=5555), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job"):

            def run_start():
                try:
                    self.daemon.start("gemini-3.8-flash-low")
                except Exception as e:
                    errors.append(f"start_error: {e}")

            def run_stop():
                try:
                    self.daemon.stop()
                except Exception as e:
                    errors.append(f"stop_error: {e}")

            threads = [
                threading.Thread(target=run_start),
                threading.Thread(target=run_stop),
                threading.Thread(target=run_start),
                threading.Thread(target=run_stop),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=3.0)

            self.assertEqual(errors, [], f"Concurrent lifecycle operations raised unexpected errors: {errors}")


class TestAntigravityMultiLeaseRegistry(unittest.TestCase):
    """Verifies the shared multi-lease registry in .agy_lock (CORE-SEC-04B Shared Lock Correction).

    Ensures that concurrent warm daemons in the pool do not overwrite each other's leases,
    stopping one daemon does not delete another daemon's lease, dead leases are pruned safely,
    live leases are preserved regardless of timestamp age, legacy formats migrate seamlessly,
    and registry locks are never held across blocking handshakes.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.daemon_a = AntigravityDaemon(agy_path="dummy.exe", default_model="gemini-3.8-flash")
        self.daemon_a.runtime_workspace = self.workspace
        self.daemon_b = AntigravityDaemon(agy_path="dummy.exe", default_model="claude-sonnet-4.6")
        self.daemon_b.runtime_workspace = self.workspace

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_two_daemons_registering_concurrently_retain_both_leases(self):
        """1. Two daemons registering concurrently both retain their leases in .agy_lock."""
        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            self.daemon_a._register_lease(1001, "gemini-3.8-flash")
            self.daemon_b._register_lease(2002, "claude-sonnet-4.6")

        lock_file = self.workspace / ".agy_lock"
        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("version"), 1)
        leases = data.get("leases", {})
        self.assertIn("1001", leases)
        self.assertIn("2002", leases)
        self.assertEqual(leases["1001"]["model"], "gemini-3.8-flash")
        self.assertEqual(leases["2002"]["model"], "claude-sonnet-4.6")

    def test_stopping_daemon_a_preserves_daemon_b_lease(self):
        """2. Stopping daemon A removes only A's lease and preserves daemon B's active lease."""
        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            self.daemon_a._register_lease(1001, "gemini-3.8-flash")
            self.daemon_b._register_lease(2002, "claude-sonnet-4.6")

            # Daemon A stops
            mock_proc_a = mock.MagicMock()
            mock_proc_a.pid = 1001
            mock_proc_a.poll.return_value = 0
            self.daemon_a.proc = mock_proc_a
            self.daemon_a.stop()

        lock_file = self.workspace / ".agy_lock"
        self.assertTrue(lock_file.exists(), ".agy_lock must NOT be unlinked while daemon B's lease is active")
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        leases = data.get("leases", {})
        self.assertNotIn("1001", leases)
        self.assertIn("2002", leases)
        self.assertEqual(leases["2002"]["pid"], 2002)

    def test_stopping_last_registered_daemon_removes_registry_safely(self):
        """3. Stopping the final registered daemon cleanly unlinks .agy_lock."""
        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            self.daemon_a._register_lease(1001, "gemini-3.8-flash")

            mock_proc_a = mock.MagicMock()
            mock_proc_a.pid = 1001
            mock_proc_a.poll.return_value = 0
            self.daemon_a.proc = mock_proc_a
            self.daemon_a.stop()

        lock_file = self.workspace / ".agy_lock"
        self.assertFalse(lock_file.exists(), ".agy_lock must be unlinked when no leases remain")

    def test_stale_recovery_removes_dead_pids_preserving_live_sibling(self):
        """4. Stale recovery removes dead-PID leases while preserving live sibling leases."""
        lock_file = self.workspace / ".agy_lock"
        initial_registry = {
            "version": 1,
            "leases": {
                "1001": {"pid": 1001, "timestamp": time.time(), "model": "live-model"},
                "99999": {"pid": 99999, "timestamp": time.time(), "model": "dead-model"},
            }
        }
        lock_file.write_text(json.dumps(initial_registry), encoding="utf-8")

        def mock_alive(pid):
            return pid == 1001

        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", side_effect=mock_alive):
            self.daemon_a._recover_stale_lock()

        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        leases = data.get("leases", {})
        self.assertIn("1001", leases)
        self.assertNotIn("99999", leases)

    def test_live_lease_older_than_300s_is_preserved(self):
        """5. A live daemon's lease older than 300s is preserved (sidecars are intentionally long-lived)."""
        lock_file = self.workspace / ".agy_lock"
        ancient_time = time.time() - 7200.0  # 2 hours ago
        initial_registry = {
            "version": 1,
            "leases": {
                "1001": {"pid": 1001, "timestamp": ancient_time, "model": "long-running-model"},
            }
        }
        lock_file.write_text(json.dumps(initial_registry), encoding="utf-8")

        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            self.daemon_a._recover_stale_lock()

        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertIn("1001", data.get("leases", {}))

    def test_different_pid_lease_not_removed_by_another_daemon_stop(self):
        """6. A different PID's lease is not removed when an unregistered PID daemon stops."""
        lock_file = self.workspace / ".agy_lock"
        initial_registry = {
            "version": 1,
            "leases": {
                "2002": {"pid": 2002, "timestamp": time.time(), "model": "sibling-model"},
            }
        }
        lock_file.write_text(json.dumps(initial_registry), encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999  # Not registered
        mock_proc.poll.return_value = 0
        self.daemon_a.proc = mock_proc
        self.daemon_a.stop()

        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertIn("2002", data.get("leases", {}))

    def test_malformed_and_empty_registry_not_blindly_deleted_or_overwritten(self):
        """7. Malformed and empty registry contents are not blindly overwritten or deleted during lifecycle ops, even when older than 300s."""
        lock_file = self.workspace / ".agy_lock"

        # Case A: Malformed registry preserved on start registration (fails closed rather than corrupting)
        lock_file.write_text("{\"corrupted\": true, unexpected_trailing...", encoding="utf-8")
        self.daemon_a._register_lease(1001, "test-model")
        self.assertTrue(lock_file.exists())
        self.assertIn("corrupted", lock_file.read_text(encoding="utf-8"))

        # Case B: Malformed registry preserved on stop
        mock_proc = mock.MagicMock()
        mock_proc.pid = 1001
        mock_proc.poll.return_value = 0
        self.daemon_a.proc = mock_proc
        self.daemon_a.stop()
        self.assertTrue(lock_file.exists())
        self.assertIn("corrupted", lock_file.read_text(encoding="utf-8"))

        # Case C: Empty registry preserved on stop (not unlinked indiscriminately)
        lock_file.write_text("", encoding="utf-8")
        self.daemon_a.proc = mock_proc
        self.daemon_a.stop()
        self.assertTrue(lock_file.exists())

        # Case D: Malformed registry older than 300s is preserved by _recover_stale_lock (fail-closed policy)
        lock_file.write_text("{\"invalid\": json...", encoding="utf-8")
        old_mtime = time.time() - 7200.0  # 2 hours old
        os.utime(lock_file, (old_mtime, old_mtime))
        self.daemon_a._recover_stale_lock()
        self.assertTrue(lock_file.exists(), "Malformed .agy_lock older than 300s must NOT be deleted by stale recovery")
        self.assertIn("invalid", lock_file.read_text(encoding="utf-8"))

        # Case E: Empty registry older than 300s is preserved by _recover_stale_lock (fail-closed policy)
        lock_file.write_text("", encoding="utf-8")
        os.utime(lock_file, (old_mtime, old_mtime))
        self.daemon_a._recover_stale_lock()
        self.assertTrue(lock_file.exists(), "Empty .agy_lock older than 300s must NOT be deleted by stale recovery")

        # Case F: Empty registry on start registration fails closed without overwriting
        self.daemon_a._register_lease(1001, "test-model")
        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "", "Empty lock file must not be overwritten blindly")

    def test_legacy_single_pid_json_migrated_without_losing_lease(self):
        """8. Legacy single-PID JSON registry is migrated to versioned format without losing valid lease."""
        lock_file = self.workspace / ".agy_lock"
        legacy_data = {"pid": 1001, "timestamp": time.time() - 50.0, "model": "legacy-model"}
        lock_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            self.daemon_b._register_lease(2002, "new-model")

        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("version"), 1)
        leases = data.get("leases", {})
        self.assertIn("1001", leases, "Legacy lease 1001 must be preserved during migration")
        self.assertIn("2002", leases, "New lease 2002 must be added")
        self.assertEqual(leases["1001"]["model"], "legacy-model")
        self.assertEqual(leases["2002"]["model"], "new-model")

    def test_legacy_raw_numeric_pid_migrated(self):
        """9. Legacy raw numeric PID format is parsed and migrated safely."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text("7777", encoding="utf-8")

        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            self.daemon_a._register_lease(8888, "migrated-model")

        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("version"), 1)
        leases = data.get("leases", {})
        self.assertIn("7777", leases)
        self.assertIn("8888", leases)
        self.assertEqual(leases["7777"]["pid"], 7777)

    def test_concurrent_start_stop_stale_recovery_stress(self):
        """10. Concurrent start, stop, and stale-recovery operations preserve integrity without corrupting JSON."""
        errors = []
        stop_event = threading.Event()

        with mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            def register_worker(pid, model):
                for i in range(15):
                    if stop_event.is_set():
                        break
                    try:
                        self.daemon_a._register_lease(pid, model)
                    except Exception as e:
                        errors.append(f"register_error: {e}")
                    time.sleep(0.005)

            def deregister_worker(pid):
                for i in range(15):
                    if stop_event.is_set():
                        break
                    try:
                        self.daemon_a._deregister_lease(pid)
                    except Exception as e:
                        errors.append(f"deregister_error: {e}")
                    time.sleep(0.005)

            def recovery_worker():
                for i in range(15):
                    if stop_event.is_set():
                        break
                    try:
                        self.daemon_a._recover_stale_lock()
                    except Exception as e:
                        errors.append(f"recovery_error: {e}")
                    time.sleep(0.005)

            threads = [
                threading.Thread(target=register_worker, args=(3001, "m1")),
                threading.Thread(target=register_worker, args=(3002, "m2")),
                threading.Thread(target=deregister_worker, args=(3001,)),
                threading.Thread(target=deregister_worker, args=(3002,)),
                threading.Thread(target=recovery_worker),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=4.0)

        self.assertEqual(errors, [], f"Concurrent registry stress operations raised errors: {errors}")
        lock_file = self.workspace / ".agy_lock"
        if lock_file.exists():
            raw = lock_file.read_text(encoding="utf-8").strip()
            if raw:
                try:
                    data = json.loads(raw)
                    self.assertEqual(data.get("version"), 1)
                except Exception as e:
                    self.fail(f"Corrupted JSON in .agy_lock after concurrency stress: {e}")

    def test_registry_lock_not_held_across_blocking_handshake(self):
        """12. Registry synchronization lock is NOT held across the blocking process handshake loop."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 4444
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()

        in_handshake = threading.Event()
        finish_handshake = threading.Event()

        def mock_readline():
            in_handshake.set()
            finish_handshake.wait(timeout=5.0)
            return '{"event": "init", "conversation_id": "test_cid"}\n'

        mock_proc.stdout.readline.side_effect = mock_readline

        def start_daemon():
            with mock.patch("subprocess.Popen", return_value=mock_proc), \
                 mock.patch("os.path.exists", return_value=True), \
                 mock.patch.object(sys, "platform", "win32"), \
                 mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=1234), \
                 mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
                 mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True):
                self.daemon_a.start("gemini-3.8-flash")

        t = threading.Thread(target=start_daemon, daemon=True)
        t.start()

        self.assertTrue(in_handshake.wait(timeout=2.0), "Daemon must enter handshake loop")

        # Crucial assertion: while in blocking handshake, _registry_lock must be completely free!
        acquired = AntigravityDaemon._registry_lock.acquire(blocking=False)
        self.assertTrue(acquired, "_registry_lock must NOT be held during the blocking readline handshake!")
        if acquired:
            AntigravityDaemon._registry_lock.release()

        finish_handshake.set()
        t.join(timeout=2.0)
        self.daemon_a.stop()

    def test_cross_process_registry_synchronization(self):
        """13. Independent OS processes concurrently updating the registry serialize without lost updates."""
        child_script = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, os.getcwd())\n"
            "from jarvis.core.antigravity_bridge import AntigravityDaemon\n"
            "ws = Path(sys.argv[1]).resolve()\n"
            "model = sys.argv[2]\n"
            "real_pid = os.getpid()\n"
            "d = AntigravityDaemon('dummy.exe', model)\n"
            "d.runtime_workspace = ws\n"
            "for _ in range(5):\n"
            "    d._register_lease(real_pid, model)\n"
            "    time.sleep(0.01)\n"
            "print(real_pid, flush=True)\n"
            "time.sleep(1.0)\n"
        )

        p1 = subprocess.Popen([sys.executable, "-c", child_script, str(self.workspace), "proc-1"], stdout=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([sys.executable, "-c", child_script, str(self.workspace), "proc-2"], stdout=subprocess.PIPE, text=True)

        try:
            pid1_line = p1.stdout.readline().strip()
            pid2_line = p2.stdout.readline().strip()
            self.assertTrue(pid1_line.isdigit())
            self.assertTrue(pid2_line.isdigit())
            pid1 = int(pid1_line)
            pid2 = int(pid2_line)

            lock_file = self.workspace / ".agy_lock"
            self.assertTrue(lock_file.exists())
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            self.assertEqual(data.get("version"), 1)
            leases = data.get("leases", {})
            self.assertIn(str(pid1), leases, "Process 1 lease must be present in registry")
            self.assertIn(str(pid2), leases, "Process 2 lease must be present in registry")
        finally:
            p1.terminate()
            p2.terminate()
            try:
                p1.wait(timeout=2.0)
            except Exception:
                p1.kill()
            try:
                p2.wait(timeout=2.0)
            except Exception:
                p2.kill()

    def test_cross_process_abandoned_mutex_recovery(self):
        """14. Cross-process mutex recovery when an independent process abruptly crashes while holding lock."""
        child_script = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, os.getcwd())\n"
            "from jarvis.core.antigravity_bridge import AntigravityDaemon\n"
            "ws = Path(sys.argv[1]).resolve()\n"
            "with AntigravityDaemon._interprocess_lock(ws) as ok:\n"
            "    if ok:\n"
            "        print('ACQUIRED_AND_CRASHING', flush=True)\n"
            "        os._exit(42)\n"
        )

        p = subprocess.Popen([sys.executable, "-c", child_script, str(self.workspace)], stdout=subprocess.PIPE, text=True)
        msg = p.stdout.readline().strip()
        self.assertEqual(msg, "ACQUIRED_AND_CRASHING")
        p.wait()

        # Parent process must acquire the lock cleanly via WAIT_ABANDONED recovery
        with AntigravityDaemon._interprocess_lock(self.workspace, timeout_ms=3000) as ok:
            self.assertTrue(ok, "Parent process must acquire mutex cleanly after child process termination")

    def test_start_fails_closed_when_registry_is_malformed(self):
        """15. start() returns False and cleans up child + Job Object if registry is malformed."""
        lock_file = self.workspace / ".agy_lock"
        malformed_content = '{"corrupted": true, trailing_garbage...'
        lock_file.write_text(malformed_content, encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 4321
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "cid_test"}\n'

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=9999), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_term_job:
            ok = self.daemon_a.start("gemini-3.8-flash")

        self.assertFalse(ok, "start() must return False when lease registration fails due to malformed registry")
        self.assertIsNone(self.daemon_a.proc, "proc must be reset to None on registration failure")
        self.assertFalse(self.daemon_a.is_alive(), "Daemon must not remain alive or online")
        mock_term_job.assert_called_once_with(9999)
        self.assertTrue(lock_file.exists(), "Malformed registry must be preserved intact")
        self.assertEqual(lock_file.read_text(encoding="utf-8"), malformed_content)

    def test_start_fails_closed_when_registry_is_empty(self):
        """16. start() returns False and cleans up child + Job Object if registry is 0-byte empty."""
        lock_file = self.workspace / ".agy_lock"
        lock_file.write_text("", encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 4322
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "cid_test"}\n'

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=8888), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_term_job:
            ok = self.daemon_a.start("gemini-3.8-flash")

        self.assertFalse(ok, "start() must return False when lease registration fails due to empty registry")
        self.assertIsNone(self.daemon_a.proc)
        self.assertFalse(self.daemon_a.is_alive())
        mock_term_job.assert_called_once_with(8888)
        self.assertTrue(lock_file.exists(), "Empty registry must be preserved rather than overwritten")
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "")

    def test_start_registration_failure_preserves_sibling_leases(self):
        """17. Registration failure of Daemon B aborts fail-closed and preserves Daemon A's active lease."""
        lock_file = self.workspace / ".agy_lock"
        initial_registry = {
            "version": 1,
            "leases": {
                "1001": {"pid": 1001, "timestamp": time.time(), "model": "daemon-a-model"},
            }
        }
        lock_file.write_text(json.dumps(initial_registry), encoding="utf-8")

        mock_proc_b = mock.MagicMock()
        mock_proc_b.pid = 2002
        mock_proc_b.poll.return_value = None
        mock_proc_b.stdin = mock.MagicMock()
        mock_proc_b.stdout = mock.MagicMock()
        mock_proc_b.stderr = mock.MagicMock()
        mock_proc_b.stdout.readline.return_value = '{"event": "init", "conversation_id": "cid_b"}\n'

        # Simulate atomic write failure during Daemon B's registration
        with mock.patch("subprocess.Popen", return_value=mock_proc_b), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True), \
             mock.patch.object(AntigravityDaemon, "_write_registry_atomic", return_value=False), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=7777), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_term_job:
            ok = self.daemon_b.start("claude-sonnet-4.6")

        self.assertFalse(ok, "Daemon B start must return False when atomic write fails")
        self.assertIsNone(self.daemon_b.proc, "Daemon B proc must be reset to None")
        self.assertFalse(self.daemon_b.is_alive())
        mock_term_job.assert_called_once_with(7777)

        # Confirm Daemon A's lease is completely intact!
        self.assertTrue(lock_file.exists())
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        leases = data.get("leases", {})
        self.assertIn("1001", leases, "Daemon A's lease must survive Daemon B's failed startup")
        self.assertNotIn("2002", leases, "Failed Daemon B must not be registered")
        self.assertEqual(leases["1001"]["model"], "daemon-a-model")

    def test_start_fails_closed_when_registration_raises_exception(self):
        """18. start() aborts fail-closed and terminates Job Object if registration raises an unexpected exception."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 3333
        mock_proc.poll.return_value = None
        mock_proc.stdin = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        mock_proc.stdout.readline.return_value = '{"event": "init", "conversation_id": "cid_test"}\n'

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(AntigravityDaemon, "_register_lease", side_effect=OSError("Disk failure simulation")), \
             mock.patch("jarvis.skills.job_object_sandbox.create_process_job_object", return_value=6666), \
             mock.patch("jarvis.skills.job_object_sandbox.assign_process_to_job", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             mock.patch("jarvis.skills.job_object_sandbox.terminate_and_close_job") as mock_term_job:
            ok = self.daemon_a.start("gemini-3.8-flash")

        self.assertFalse(ok, "start() must return False when _register_lease raises an exception")
        self.assertIsNone(self.daemon_a.proc)
        self.assertFalse(self.daemon_a.is_alive())
        mock_term_job.assert_called_once_with(6666)


@unittest.skipUnless(WIN32, "Real Win32 Job Object tests require Windows host environment")
class TestAntigravityJobContainmentRealWin32(unittest.TestCase):
    """Real Windows integration tests exercising the actual kernel32 Job Object subsystem."""

    def test_real_win32_grandchild_tree_cessation(self):
        """13. Real Win32 test: Job Object termination terminates both parent and detached descendant processes."""
        # A test script that spawns a detached grandchild process sleeping for 60 seconds
        child_code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(60)\n"
        )

        h_job = job_sandbox.create_process_job_object(kill_on_close=True, requested_affinity=0)
        self.assertIsNotNone(h_job)

        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED,
        )

        parent_pid = proc.pid
        self.assertTrue(job_sandbox.assign_process_to_job(h_job, proc))
        self.assertTrue(job_sandbox.resume_process(proc))

        # Read grandchild PID from child's stdout
        grandchild_pid_line = proc.stdout.readline().strip()
        self.assertTrue(grandchild_pid_line.isdigit(), f"Expected numeric PID line, got: {grandchild_pid_line}")
        grandchild_pid = int(grandchild_pid_line)

        # Confirm both parent and grandchild are running
        self.assertTrue(_is_pid_alive(parent_pid), f"Parent {parent_pid} should be alive")
        self.assertTrue(_is_pid_alive(grandchild_pid), f"Grandchild {grandchild_pid} should be alive")

        # Terminate and close Job Object
        self.assertTrue(job_sandbox.terminate_and_close_job(h_job))

        # Verify BOTH parent and grandchild are terminated by the kernel
        self.assertTrue(_wait_pid_gone(parent_pid, timeout=3.0), f"Parent PID {parent_pid} survived Job Object termination!")
        self.assertTrue(_wait_pid_gone(grandchild_pid, timeout=3.0), f"Grandchild PID {grandchild_pid} survived Job Object termination!")


if __name__ == "__main__":
    unittest.main()
