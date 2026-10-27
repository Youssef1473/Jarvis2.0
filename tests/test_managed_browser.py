"""Unit and integration test suite for CORE-NET-01A: Managed Browser Lifecycle,
Win32 Job Object Process Containment, Dynamic CDP Discovery, and Automated Fallback Removal.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil

# Add workspace root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis.skills.browser_cdp as b_cdp
import jarvis.skills.os_control as os_ctrl
from jarvis.skills.job_object_sandbox import (
    ContainedProcess,
    JobObjectExtendedLimitInformation,
    kernel32,
    spawn_contained_process,
)


class TestContainedProcessLifecycle(unittest.TestCase):
    """Verifies Win32 Job Object process containment, suspended startup, and fail-closed termination."""

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_spawn_contained_process_normal_lifecycle(self):
        """Verifies process is created suspended, assigned to Job Object, resumed, and terminates cleanly."""
        proc = spawn_contained_process(["cmd.exe", "/c", "exit 0"])
        self.assertIsInstance(proc, ContainedProcess)
        self.assertGreater(proc.pid, 0)
        self.assertGreater(proc.h_process, 0)
        self.assertGreater(proc.h_job, 0)

        # Wait for completion
        code = proc.wait(timeout_sec=5.0)
        self.assertEqual(code, 0)
        self.assertFalse(proc.is_alive())

        # Terminate should be idempotent and close handles
        self.assertTrue(proc.terminate())
        self.assertEqual(proc.h_process, 0)
        self.assertEqual(proc.h_job, 0)

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_spawn_contained_process_job_object_limits_configured(self):
        """Verifies Job Object is configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
        with patch.object(kernel32, "SetInformationJobObject", wraps=kernel32.SetInformationJobObject) as mock_set_info:
            proc = spawn_contained_process(["cmd.exe", "/c", "exit 0"])
            try:
                mock_set_info.assert_called_once()
                args = mock_set_info.call_args[0]
                self.assertEqual(args[1], JobObjectExtendedLimitInformation)
            finally:
                proc.terminate()

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_spawn_contained_process_assignment_failure_terminates_suspended(self):
        """Verifies that if Job Object assignment fails, the suspended process is killed immediately and never runs."""
        with patch.object(kernel32, "AssignProcessToJobObject", return_value=0), \
             patch.object(kernel32, "TerminateProcess", wraps=kernel32.TerminateProcess) as mock_term, \
             patch.object(kernel32, "ResumeThread", wraps=kernel32.ResumeThread) as mock_resume, \
             patch.object(kernel32, "CloseHandle", wraps=kernel32.CloseHandle) as mock_close:

            with self.assertRaises(RuntimeError) as ctx:
                spawn_contained_process(["cmd.exe", "/c", "exit 0"])

            self.assertIn("Failed to assign process to Win32 Job Object", str(ctx.exception))
            self.assertIn("process terminated fail-closed", str(ctx.exception))

            # ResumeThread MUST NEVER have been called because assignment failed
            mock_resume.assert_not_called()

            # TerminateProcess MUST have been called on the suspended process
            mock_term.assert_called_once()

            # Handles (thread, process, job) MUST have been closed
            self.assertGreaterEqual(mock_close.call_count, 3)

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_spawn_contained_process_nested_job_assignment_failure(self):
        """Simulates parent process in restrictive job object causing ERROR_ACCESS_DENIED (5) on child assignment."""
        with patch.object(kernel32, "AssignProcessToJobObject", return_value=0), \
             patch("jarvis.skills.job_object_sandbox._last_error", return_value=5), \
             patch.object(kernel32, "TerminateProcess", wraps=kernel32.TerminateProcess) as mock_term, \
             patch.object(kernel32, "ResumeThread") as mock_resume:

            with self.assertRaises(RuntimeError) as ctx:
                spawn_contained_process(["cmd.exe", "/c", "exit 0"])

            self.assertIn("Win32 error: 5", str(ctx.exception))
            mock_resume.assert_not_called()
            mock_term.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_spawn_contained_process_resume_thread_failure_fails_closed(self):
        """Verifies that if ResumeThread fails (0xFFFFFFFF), process is terminated and fails closed."""
        with patch.object(kernel32, "ResumeThread", return_value=0xFFFFFFFF), \
             patch.object(kernel32, "TerminateProcess", wraps=kernel32.TerminateProcess) as mock_term:

            with self.assertRaises(RuntimeError) as ctx:
                spawn_contained_process(["cmd.exe", "/c", "exit 0"])

            self.assertIn("Failed to resume thread of contained process", str(ctx.exception))
            mock_term.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_contained_process_terminate_kills_tree(self):
        """Verifies calling terminate() terminates the running contained process tree."""
        proc = spawn_contained_process(["cmd.exe", "/c", "ping -n 15 127.0.0.1 > nul"])
        try:
            self.assertTrue(proc.is_alive())
            self.assertTrue(proc.terminate())
            self.assertFalse(proc.is_alive())
            self.assertEqual(proc.h_process, 0)
        finally:
            proc.terminate()


class TestManagedBrowserSession(unittest.TestCase):
    """Verifies dedicated profile isolation, dynamic CDP discovery, and fail-closed timeout handling."""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_managed_browser_"))

    def tearDown(self):
        b_cdp.stop_managed_browser()
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_profile_isolation_per_run(self):
        """Verifies each session creates a unique, isolated profile directory under the base path and cleans up."""
        session = b_cdp.ManagedBrowserSession(browser_name="brave", base_dir=self.tmp_dir)

        # Mock spawn_contained_process and DevToolsActivePort discovery
        mock_proc = MagicMock(spec=ContainedProcess)
        mock_proc.pid = 9999
        mock_proc.is_alive.return_value = True

        def fake_start(*args, **kwargs):
            session.profile_dir.mkdir(parents=True, exist_ok=True)
            port_file = session.profile_dir / "DevToolsActivePort"
            port_file.write_text("54321\n/devtools/browser/fake-id\n", encoding="utf-8")
            return mock_proc

        with patch("jarvis.skills.browser_cdp.resolve_browser_executable", return_value=r"C:\fake\brave.exe"), \
             patch("jarvis.skills.job_object_sandbox.spawn_contained_process", side_effect=fake_start), \
             patch.object(b_cdp, "is_cdp_available", return_value=True):

            ok, _msg = session.start(timeout_sec=2.0)
            self.assertTrue(ok)
            self.assertEqual(session.cdp_port, 54321)
            self.assertTrue(session.profile_dir.exists())
            self.assertTrue("session_" in session.profile_dir.name)

            # Stopping session purges profile directory
            session.stop()
            self.assertFalse(session.profile_dir and session.profile_dir.exists())

    def test_dynamic_port_discovery_valid(self):
        """Verifies DevToolsActivePort is correctly parsed for integer port and WebSocket path."""
        session = b_cdp.ManagedBrowserSession(base_dir=self.tmp_dir)
        session.profile_dir = self.tmp_dir / "session_test"
        session.profile_dir.mkdir(parents=True, exist_ok=True)

        port_file = session.profile_dir / "DevToolsActivePort"
        port_file.write_text("48999\n/devtools/browser/abc-123\n", encoding="utf-8")

        port, ws_path, err = session._poll_devtools_active_port(session.profile_dir, timeout_sec=1.0)
        self.assertEqual(port, 48999)
        self.assertEqual(ws_path, "/devtools/browser/abc-123")
        self.assertIsNone(err)

    def test_dynamic_port_discovery_malformed_fails_closed(self):
        """Verifies non-integer or empty DevToolsActivePort fails closed."""
        session = b_cdp.ManagedBrowserSession(base_dir=self.tmp_dir)
        session.profile_dir = self.tmp_dir / "session_test_malformed"
        session.profile_dir.mkdir(parents=True, exist_ok=True)

        port_file = session.profile_dir / "DevToolsActivePort"

        # Case 1: Non-numeric port
        port_file.write_text("not_a_port\n/devtools/browser/abc\n", encoding="utf-8")
        port, _ws_path, err = session._poll_devtools_active_port(session.profile_dir, timeout_sec=0.2)
        self.assertIsNone(port)
        self.assertIn("Malformed DevToolsActivePort", err)

        # Case 2: Out of range port (0 or >65535)
        port_file.write_text("99999\n/devtools/browser/abc\n", encoding="utf-8")
        port, _ws_path, err = session._poll_devtools_active_port(session.profile_dir, timeout_sec=0.2)
        self.assertIsNone(port)
        self.assertIn("Malformed DevToolsActivePort", err)

    def test_dynamic_port_discovery_timeout_fails_closed(self):
        """Verifies timeout when DevToolsActivePort is not created causes process termination and fail-closed result."""
        session = b_cdp.ManagedBrowserSession(base_dir=self.tmp_dir)

        mock_proc = MagicMock(spec=ContainedProcess)
        mock_proc.pid = 8888
        mock_proc.is_alive.return_value = True

        with patch("jarvis.skills.browser_cdp.resolve_browser_executable", return_value=r"C:\fake\brave.exe"), \
             patch("jarvis.skills.job_object_sandbox.spawn_contained_process", return_value=mock_proc):

            ok, msg = session.start(timeout_sec=0.2)
            self.assertFalse(ok)
            self.assertIn("DevToolsActivePort discovery failed", msg)
            mock_proc.terminate.assert_called_once()
            self.assertIsNone(session.cdp_port)

    def test_command_line_loopback_binding_and_debugging_flags(self):
        """Verifies mandatory --remote-debugging-port=0, --remote-debugging-address=127.0.0.1, and user-data-dir flags."""
        captured_cmd = []

        def fake_spawn(cmd):
            captured_cmd.extend(cmd)
            user_data_arg = next(a for a in cmd if "--user-data-dir=" in a)
            profile_dir = Path(user_data_arg.split("=", 1)[1])
            (profile_dir / "DevToolsActivePort").write_text("51234\n/devtools/browser/id\n", encoding="utf-8")
            m = MagicMock(spec=ContainedProcess)
            m.is_alive.return_value = True
            return m

        with patch("jarvis.skills.browser_cdp.resolve_browser_executable", return_value=r"C:\fake\brave.exe"), \
             patch("jarvis.skills.job_object_sandbox.spawn_contained_process", side_effect=fake_spawn), \
             patch.object(b_cdp, "is_cdp_available", return_value=True):

            session = b_cdp.ManagedBrowserSession(browser_name="brave", base_dir=self.tmp_dir)
            ok, _msg = session.start(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertIn("--remote-debugging-port=0", captured_cmd)
            self.assertIn("--remote-debugging-address=127.0.0.1", captured_cmd)
            self.assertTrue(any("--user-data-dir=" in a for a in captured_cmd))
            self.assertIn("--no-first-run", captured_cmd)
            session.stop()


class TestAutomatedBrowserFallbackRemoval(unittest.TestCase):
    """Verifies removal of silent personal browser fallbacks on the automated path while preserving manual user operations."""

    def test_open_browser_tab_automated_fails_closed_when_cdp_unavailable(self):
        """Verifies automated=True never falls back to UIA, named pipes, personal subprocess launch, or default shell."""
        with patch.object(b_cdp, "is_cdp_available", return_value=False), \
             patch.object(b_cdp, "get_managed_browser_session", return_value=None), \
             patch.object(b_cdp, "validate_browser_navigation_chain", return_value=(True, "https://example.com", None)), \
             patch("subprocess.Popen") as mock_popen, \
             patch("webbrowser.open_new_tab") as mock_webbrowser, \
             patch("jarvis.skills.windows_uia.focus_window_uia") as mock_uia:

            result = os_ctrl.open_browser_tab(url="https://example.com", browser="brave", automated=True)

            # Must fail closed with explicit security policy notice
            self.assertIn("Navigation failed", result)
            self.assertIn("automated fallback to personal browser is disabled for security", result)

            # Personal browser fallbacks must NEVER have been called
            mock_popen.assert_not_called()
            mock_webbrowser.assert_not_called()
            mock_uia.assert_not_called()

    def test_open_browser_tab_interactive_retains_fallback(self):
        """Verifies automated=False (interactive user voice/UI request) retains backward-compatible desktop launch."""
        with patch.object(b_cdp, "is_cdp_available", return_value=False), \
             patch.object(b_cdp, "validate_browser_navigation_chain", return_value=(True, "https://example.com", None)), \
             patch("subprocess.Popen"), \
             patch("webbrowser.open_new_tab"):

            result = os_ctrl.open_browser_tab(url="https://example.com", browser="brave", automated=False)

            self.assertIsInstance(result, str)
            self.assertTrue(
                "active brave window" in result.lower()
                or "launched" in result.lower()
                or "opened" in result.lower()
                or "default" in result.lower()
            )


class TestToolOrchestratorAutomatedEnforcement(unittest.TestCase):
    """Verifies that ToolOrchestrator strips adversarial automated arguments and enforces automated=True."""

    def setUp(self):
        from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
        self.orchestrator = ToolOrchestrator()

    def test_orchestrator_sync_injects_automated_and_strips_model_override(self):
        """Verifies synchronous execute_step strips automated=False from LLM arguments and forces automated=True."""
        with patch.object(b_cdp, "is_cdp_available", return_value=False), \
             patch.object(b_cdp, "get_managed_browser_session", return_value=None), \
             patch.object(b_cdp, "validate_browser_navigation_chain", return_value=(True, "https://example.com", None)), \
             patch("subprocess.Popen") as mock_popen, \
             patch("webbrowser.open_new_tab") as mock_webbrowser:

            # Even if the LLM plan attempts to pass automated=False to evade containment:
            result = self.orchestrator.execute_step(
                "open_browser_tab",
                {"url": "https://example.com", "automated": False},
                step_index=1,
            )

            # Must fail closed via automated=True security path
            self.assertIn("Navigation failed", str(result))
            self.assertIn("automated fallback to personal browser is disabled for security", str(result))
            mock_popen.assert_not_called()
            mock_webbrowser.assert_not_called()

    def test_orchestrator_async_injects_automated_and_strips_model_override(self):
        """Verifies asynchronous execute_step_async strips automated=False and forces automated=True."""
        with patch.object(b_cdp, "is_cdp_available", return_value=False), \
             patch.object(b_cdp, "get_managed_browser_session", return_value=None), \
             patch.object(b_cdp, "validate_browser_navigation_chain", return_value=(True, "https://example.com", None)), \
             patch("subprocess.Popen") as mock_popen, \
             patch("webbrowser.open_new_tab") as mock_webbrowser:

            result = asyncio.run(
                self.orchestrator.execute_step_async(
                    "open_browser_tab",
                    {"url": "https://example.com", "automated": False},
                    step_index=1,
                )
            )

            self.assertIn("Navigation failed", str(result))
            self.assertIn("automated fallback to personal browser is disabled for security", str(result))
            mock_popen.assert_not_called()
            mock_webbrowser.assert_not_called()


class TestStaleProfilePruning(unittest.TestCase):
    """Verifies cleanup of orphaned session directories on startup and per-run lifecycle."""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_pruning_"))

    def tearDown(self):
        b_cdp.stop_managed_browser()
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_clean_stale_managed_profiles_prunes_abandoned_dirs(self):
        """Verifies directories older than max_age_sec are deleted while recent or non-session dirs are untouched."""
        stale_dir_1 = self.tmp_dir / "session_stale1"
        stale_dir_2 = self.tmp_dir / "session_stale2"
        recent_dir = self.tmp_dir / "session_recent"
        other_dir = self.tmp_dir / "other_folder"

        for d in (stale_dir_1, stale_dir_2, recent_dir, other_dir):
            d.mkdir(parents=True, exist_ok=True)

        now = time.time()
        # Set stale dirs mtime to 1000s in the past
        os.utime(str(stale_dir_1), (now - 1000, now - 1000))
        os.utime(str(stale_dir_2), (now - 1000, now - 1000))
        # Keep recent_dir fresh
        os.utime(str(recent_dir), (now, now))

        pruned = b_cdp.clean_stale_managed_profiles(base_dir=self.tmp_dir, max_age_sec=300.0)
        self.assertEqual(pruned, 2)
        self.assertFalse(stale_dir_1.exists())
        self.assertFalse(stale_dir_2.exists())
        self.assertTrue(recent_dir.exists())
        self.assertTrue(other_dir.exists())

    def test_clean_stale_managed_profiles_preserves_active_session(self):
        """Verifies active session directory is never pruned even if older than max_age_sec."""
        active_session_dir = self.tmp_dir / "session_active"
        active_session_dir.mkdir(parents=True, exist_ok=True)

        now = time.time()
        os.utime(str(active_session_dir), (now - 1000, now - 1000))

        mock_session = MagicMock()
        mock_session.profile_dir = active_session_dir

        with patch.object(b_cdp, "_MANAGED_BROWSER_SESSION", mock_session):
            pruned = b_cdp.clean_stale_managed_profiles(base_dir=self.tmp_dir, max_age_sec=300.0)
            self.assertEqual(pruned, 0)
            self.assertTrue(active_session_dir.exists())


class TestWindowsChromiumIntegration(unittest.TestCase):
    """End-to-end integration test verifying real Windows Chromium containment, dynamic CDP, and teardown."""

    @unittest.skipUnless(
        sys.platform == "win32" and b_cdp.resolve_browser_executable("brave") is not None,
        "Real Brave browser executable on Windows required",
    )
    def test_live_windows_brave_contained_lifecycle(self):
        """Verifies real Brave launches suspended into Job Object, dynamic port responds, and stopping cleans up."""
        tmp_base = Path(tempfile.mkdtemp(prefix="test_live_brave_"))
        try:
            session = b_cdp.ManagedBrowserSession(browser_name="brave", headless=True, base_dir=tmp_base)
            ok, msg = session.start(timeout_sec=10.0)
            self.assertTrue(ok, f"Failed to start managed Brave: {msg}")
            self.assertIsNotNone(session.cdp_port)
            self.assertGreater(session.cdp_port, 1024)
            self.assertTrue(session.is_alive())

            # Verify dynamic port responds on loopback
            self.assertTrue(b_cdp.is_cdp_available("127.0.0.1", session.cdp_port))

            # Verify process exists in Windows
            pid = session.process.pid
            self.assertTrue(psutil.pid_exists(pid))

            # Stop session and verify process tree termination and profile deletion
            profile_dir = session.profile_dir
            session.stop()

            self.assertFalse(session.is_alive())
            self.assertFalse(psutil.pid_exists(pid))
            self.assertFalse(profile_dir.exists())
        finally:
            if tmp_base.exists():
                shutil.rmtree(tmp_base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
