"""Comprehensive test suite for Windows AppContainer capability isolation and sandboxing.

Covers:
1. Least-privilege configuration defaults (empty capability set).
2. Capability SID resolution and mapping.
3. Profile creation, reuse (HRESULT 0x800700B7 fallback), and deletion.
4. Fail-closed security on profile creation failure (no unrestricted fallback).
5. Fail-closed security on Job Object assignment failure (process terminated, handles closed).
6. Attribute list lifecycle and memory cleanup.
7. Non-Windows fail-closed validation.
8. Backward-compatible preservation of legacy Job Object sandbox (use_appcontainer=False).
9. Dedicated working directory ACL granting and revocation.
10. Live Windows containment integration: granted write succeeds, ungranted write fails with Access Denied.
11. Timeout and process tree termination inside AppContainer.
12. Batch execution under AppContainer (run_sandboxed_batch).
13. Dynamic custom skill execution under AppContainer (execute_sandboxed_custom_skill).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis.core.evolution.appcontainer import (
    CAPABILITY_SID_MAP,
    AppContainerConfig,
    AppContainerProcessLauncher,
    AppContainerProfileManager,
    AppContainerSecurityError,
)
from jarvis.core.evolution.sandbox import SkillSandbox
from jarvis.skills.job_object_sandbox import (
    SandboxedProcess,
    run_sandboxed_batch,
)

IS_WIN = sys.platform == "win32"


class TestAppContainerConfiguration(unittest.TestCase):
    """Verifies configuration invariants and least-privilege defaults."""

    def test_appcontainer_config_least_privilege_defaults(self):
        """Default capabilities must be empty (least privilege: 0 network, 0 devices, 0 credentials)."""
        cfg = AppContainerConfig()
        self.assertEqual(cfg.capabilities, [])
        self.assertEqual(cfg.grant_paths, [])
        self.assertTrue(cfg.auto_cleanup_profile)
        self.assertTrue(cfg.profile_name.startswith("J_SB_") or cfg.profile_name.startswith("Jarvis_"))
        self.assertIn("Sandbox", cfg.display_name)
        self.assertIn("Boundary", cfg.description)

    def test_capability_sid_resolution(self):
        """Verifies well-known capability SID mappings match Windows security specifications."""
        expected_mappings = {
            "internetClient": "S-1-15-3-1",
            "internetClientServer": "S-1-15-3-2",
            "privateNetworkClientServer": "S-1-15-3-3",
            "picturesLibrary": "S-1-15-3-4",
            "videosLibrary": "S-1-15-3-5",
            "musicLibrary": "S-1-15-3-6",
            "documentsLibrary": "S-1-15-3-7",
            "enterpriseAuthentication": "S-1-15-3-8",
            "sharedUserCertificates": "S-1-15-3-9",
            "removableStorage": "S-1-15-3-10",
            "appointments": "S-1-15-3-11",
            "contacts": "S-1-15-3-12",
        }
        for cap_name, expected_sid in expected_mappings.items():
            self.assertEqual(
                CAPABILITY_SID_MAP.get(cap_name),
                expected_sid,
                f"Capability mapping mismatch for '{cap_name}'",
            )


class TestAppContainerProfileLifecycle(unittest.TestCase):
    """Tests profile creation, derivation, and deletion lifecycle."""

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_profile_creation_reuse_and_cleanup(self):
        """Creates an AppContainer profile, derives existing SID on reuse, and deletes cleanly."""
        unique_name = f"Jarvis_Test_Profile_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(profile_name=unique_name, auto_cleanup_profile=False)

        p_sid1, sid_str1, is_new1 = AppContainerProfileManager.get_or_create_profile(cfg)
        self.assertTrue(is_new1)
        try:
            self.assertGreater(p_sid1, 0)
            self.assertTrue(sid_str1.startswith("S-1-15-2-"))

            # Reuse: attempting to create existing profile must derive identical SID
            p_sid2, sid_str2, is_new2 = AppContainerProfileManager.get_or_create_profile(cfg)
            self.assertFalse(is_new2)
            try:
                self.assertEqual(sid_str1, sid_str2)
            finally:
                AppContainerProfileManager.free_sid(p_sid2)
        finally:
            AppContainerProfileManager.free_sid(p_sid1)
            deleted = AppContainerProfileManager.delete_profile(unique_name)
            self.assertTrue(deleted)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_existing_profile_reuse_does_not_delete_shared_profile(self):
        """Reusing an existing profile sets is_new_profile=False and preserves it after child exit."""
        shared_name = f"Jarvis_Shared_{os.getpid()}_{time.time_ns()}"
        base_cfg = AppContainerConfig(profile_name=shared_name, auto_cleanup_profile=False)
        p_sid, sid_str, is_new = AppContainerProfileManager.get_or_create_profile(base_cfg)
        self.assertTrue(is_new)
        AppContainerProfileManager.free_sid(p_sid)

        try:
            # Run child process with auto_cleanup_profile=True pointing to existing profile
            child_cfg = AppContainerConfig(profile_name=shared_name, auto_cleanup_profile=True)
            handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                config=child_cfg,
            )
            self.assertFalse(handle.is_new_profile)
            handle.resume()
            handle.cleanup()

            # Profile must STILL exist because child process was not its creator
            p_sid_reuse, sid_reuse, is_new_reuse = AppContainerProfileManager.get_or_create_profile(base_cfg)
            self.assertFalse(is_new_reuse)
            self.assertEqual(sid_str, sid_reuse)
            AppContainerProfileManager.free_sid(p_sid_reuse)
        finally:
            AppContainerProfileManager.delete_profile(shared_name)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_dedicated_working_directory_isolation(self):
        """Dedicated scratch directory is granted access for AppContainer SID and cleanly revoked."""
        temp_dir = tempfile.mkdtemp(prefix="jarvis_acl_test_")
        test_path = Path(temp_dir)
        unique_name = f"Jarvis_Test_ACL_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(profile_name=unique_name)

        p_sid, sid_str, _is_new = AppContainerProfileManager.get_or_create_profile(cfg)
        try:
            # Grant access to temp directory
            granted = AppContainerProfileManager.grant_path_access(test_path, sid_str)
            self.assertTrue(granted)

            # Revoke access
            revoked = AppContainerProfileManager.revoke_path_access(test_path, sid_str)
            self.assertTrue(revoked)
        finally:
            AppContainerProfileManager.free_sid(p_sid)
            AppContainerProfileManager.delete_profile(unique_name)
            shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_runtime_access_scoped_grant_and_cleanup(self):
        """Preflight check passes and ensure_runtime_access grants are scoped and cleanly tracked."""
        ok, msg = AppContainerProfileManager.preflight_runtime_check()
        self.assertTrue(ok, f"Preflight runtime check failed: {msg}")
        self.assertIn("verified", msg.lower())

        unique_name = f"Jarvis_Runtime_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(profile_name=unique_name, auto_cleanup_profile=True)
        p_sid, sid_str, _is_new = AppContainerProfileManager.get_or_create_profile(cfg)
        try:
            granted = AppContainerProfileManager.ensure_runtime_access(sid_str)
            self.assertIsInstance(granted, list)
            for p in granted:
                self.assertTrue(p.exists())
                AppContainerProfileManager.revoke_path_access(p, sid_str)
        finally:
            AppContainerProfileManager.free_sid(p_sid)
            AppContainerProfileManager.delete_profile(unique_name)


class TestAppContainerFailClosedSecurity(unittest.TestCase):
    """Verifies fail-closed invariants: any error halts execution without uncontained fallback."""

    def test_non_windows_fail_closed(self):
        """When executed on non-Windows host, use_appcontainer=True immediately raises RuntimeError."""
        with mock.patch("sys.platform", "linux"):
            proc_mgr = SandboxedProcess(
                [sys.executable, "-c", "print(1)"],
                use_appcontainer=True,
            )
            with self.assertRaises(RuntimeError) as ctx:
                proc_mgr.start()
            self.assertIn("AppContainer isolation is only supported on Windows", str(ctx.exception))

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_fail_closed_on_profile_failure(self):
        """If profile creation fails, execution raises RuntimeError and never runs uncontained."""
        cfg = AppContainerConfig(profile_name="Jarvis_Fail_Test")
        with mock.patch.object(
            AppContainerProfileManager,
            "get_or_create_profile",
            side_effect=AppContainerSecurityError("Simulated profile failure"),
        ):
            proc_mgr = SandboxedProcess(
                [sys.executable, "-c", "print(1)"],
                use_appcontainer=True,
                appcontainer_config=cfg,
            )
            with self.assertRaises(RuntimeError) as ctx:
                proc_mgr.start()
            self.assertIn("AppContainer launch failed", str(ctx.exception))

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_fail_closed_on_job_assignment_failure(self):
        """If Job Object assignment fails, child is killed immediately and RuntimeError is raised."""
        cfg = AppContainerConfig(profile_name=f"Jarvis_JobFail_{os.getpid()}_{time.time_ns()}")
        with mock.patch("jarvis.skills.job_object_sandbox.kernel32.AssignProcessToJobObject", return_value=0):
            proc_mgr = SandboxedProcess(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                use_appcontainer=True,
                appcontainer_config=cfg,
            )
            with self.assertRaises(RuntimeError) as ctx:
                proc_mgr.start()
            self.assertIn("Failed to assign AppContainer process to Job Object", str(ctx.exception))
            # Child must be terminated and cleaned up
            self.assertIsNone(proc_mgr.proc)
            self.assertIsNone(proc_mgr.h_job)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_acl_grant_failure_rollback_previous_grants(self):
        """If a path grant fails during setup, all previously granted paths are revoked in rollback."""
        temp_dir1 = tempfile.mkdtemp(prefix="jarvis_rollback_1_")
        path1 = Path(temp_dir1)
        invalid_path = Path("Z:\\NonExistent_Drive\\Invalid_Dir_Path")
        cfg = AppContainerConfig(
            profile_name=f"Jarvis_Rollback_{os.getpid()}_{time.time_ns()}",
            grant_paths=[path1, invalid_path],
            auto_cleanup_profile=True,
        )
        with self.assertRaises(AppContainerSecurityError):
            AppContainerProcessLauncher.spawn_suspended_appcontainer(
                command=[sys.executable, "-c", "print(1)"],
                config=cfg,
            )
        shutil.rmtree(temp_dir1, ignore_errors=True)

    def test_legacy_sandbox_preservation(self):
        """use_appcontainer=False preserves existing Win32 Job Object execution path."""
        res = run_sandboxed_batch(
            [sys.executable, "-c", "print('legacy_job_sandbox_active')"],
            capture_output=True,
            use_appcontainer=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("legacy_job_sandbox_active", res.stdout)


class TestAppContainerExecutionAndContainment(unittest.TestCase):
    """Executes live contained processes and verifies Windows NT security boundaries."""

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_live_windows_containment_denied_access(self):
        """Proves NT kernel containment: AppContainer writes to granted scratch, but is strictly denied write to user home."""
        scratch_dir = tempfile.mkdtemp(prefix="jarvis_containment_scratch_")
        scratch_path = Path(scratch_dir)
        user_home_target = Path.home() / f"jarvis_forbidden_canary_{os.getpid()}_{time.time_ns()}.txt"

        unique_profile = f"Jarvis_Containment_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(
            profile_name=unique_profile,
            grant_paths=[scratch_path],
            capabilities=[],  # Least privilege: empty
            auto_cleanup_profile=True,
        )

        allowed_target = scratch_path / "allowed.txt"

        # Python test script executed inside AppContainer:
        # 1. Writes to allowed scratch file (should succeed).
        # 2. Attempts to write to user home directory (must be denied by kernel).
        script = f"""
import sys
from pathlib import Path

# 1. Write to allowed path
try:
    Path(r'{allowed_target}').write_text('scratch_write_success', encoding='utf-8')
except Exception as e:
    sys.stderr.write(f'ALLOWED_WRITE_FAILED: {{e}}\\n')
    sys.exit(10)

# 2. Attempt write to user home forbidden path
try:
    Path(r'{user_home_target}').write_text('forbidden_leak', encoding='utf-8')
    sys.stderr.write('SECURITY_VIOLATION: Ungranted write succeeded!\\n')
    sys.exit(20)
except PermissionError as pe:
    # Strictly require expected Windows access-denied error code (winerror 5, or errno 13 when winerror is None)
    winerr = getattr(pe, 'winerror', None)
    if winerr == 5 or (winerr is None and pe.errno == 13):
        print('KERNEL_CONTAINMENT_VERIFIED: Access denied with winerror=5')
        sys.exit(0)
    else:
        sys.stderr.write(f'UNEXPECTED_PERMISSION_ERROR: winerror={{winerr}}, errno={{pe.errno}}\\n')
        sys.exit(21)
except OSError as oe:
    winerr = getattr(oe, 'winerror', None)
    if winerr == 5 or (winerr is None and oe.errno == 13):
        print(f'KERNEL_CONTAINMENT_VERIFIED: Access denied with winerror=5')
        sys.exit(0)
    else:
        sys.stderr.write(f'UNEXPECTED_OS_ERROR: winerror={{winerr}}, errno={{oe.errno}}\\n')
        sys.exit(22)
"""
        try:
            res = run_sandboxed_batch(
                [sys.executable, "-c", script],
                capture_output=True,
                use_appcontainer=True,
                appcontainer_config=cfg,
            )

            # Allowed write must exist and contain expected data
            self.assertTrue(allowed_target.exists(), "Allowed scratch file was not created!")
            self.assertEqual(allowed_target.read_text(encoding="utf-8"), "scratch_write_success")

            # Forbidden file MUST NEVER exist
            self.assertFalse(user_home_target.exists(), "SECURITY VIOLATION: Forbidden file was created on host!")

            # Contained process must confirm kernel denial
            self.assertEqual(res.returncode, 0, f"Containment verification script failed: {res.stderr}")
            self.assertIn("KERNEL_CONTAINMENT_VERIFIED", res.stdout)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
            if user_home_target.exists():
                user_home_target.unlink(missing_ok=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_cwd_outside_approved_grant_list_never_receives_acl_grant(self):
        """Passing cwd to spawn_suspended_appcontainer must NOT append it to config.grant_paths."""
        unapproved_cwd = tempfile.mkdtemp(prefix="jarvis_unapproved_cwd_")
        scratch_dir = tempfile.mkdtemp(prefix="jarvis_scratch_")
        unique_name = f"Jarvis_Cwd_Test_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(
            profile_name=unique_name,
            grant_paths=[Path(scratch_dir)],
            auto_cleanup_profile=True,
        )
        try:
            handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                cwd=unapproved_cwd,
                config=cfg,
            )
            try:
                self.assertNotIn(unapproved_cwd, cfg.grant_paths)
                self.assertNotIn(Path(unapproved_cwd), cfg.grant_paths)
                self.assertEqual(len(cfg.grant_paths), 1)
                self.assertEqual(Path(cfg.grant_paths[0]).resolve(), Path(scratch_dir).resolve())
            finally:
                handle.resume()
                handle.cleanup()
        finally:
            shutil.rmtree(unapproved_cwd, ignore_errors=True)
            shutil.rmtree(scratch_dir, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_attribute_list_backing_lifetimes_and_single_cleanup(self):
        """Attribute list is deleted in launcher finally block; handle cleanup avoids double-free."""
        cfg = AppContainerConfig(
            profile_name=f"Jarvis_Attr_{os.getpid()}_{time.time_ns()}",
            auto_cleanup_profile=True,
        )
        handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
            command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            config=cfg,
        )
        try:
            self.assertIsNone(handle.attr_buf)
            self.assertTrue(handle.resume())
        finally:
            handle.cleanup()

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_all_standard_handles_valid_with_stdin_only_pipe_and_devnull(self):
        """When stdin is PIPE and stdout/stderr are unredirected/DEVNULL, all standard handles are valid."""
        cfg = AppContainerConfig(
            profile_name=f"Jarvis_StdH_{os.getpid()}_{time.time_ns()}",
            auto_cleanup_profile=True,
        )
        proc_mgr = SandboxedProcess(
            [sys.executable, "-c", "import sys; data = sys.stdin.read(); sys.exit(0 if data == 'ok' else 1)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            use_appcontainer=True,
            appcontainer_config=cfg,
        )
        proc_mgr.start()
        self.assertIsNotNone(proc_mgr.proc)
        _stdout, _stderr = proc_mgr.proc.communicate(input="ok", timeout=5.0)
        self.assertEqual(proc_mgr.proc.returncode, 0)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_communicate_input_none_sends_eof_and_large_input_no_deadlock(self):
        """communicate(None) sends EOF without hang; large input (>128KB) avoids pipe deadlocks."""
        cfg = AppContainerConfig(
            profile_name=f"Jarvis_Comm_{os.getpid()}_{time.time_ns()}",
            auto_cleanup_profile=True,
        )
        # Part A: sys.stdin.read() with communicate(None) must immediately observe EOF
        res_eof = run_sandboxed_batch(
            [sys.executable, "-c", "import sys; txt = sys.stdin.read(); print(f'EOF_READ_LEN:{len(txt)}')"],
            input=None,
            capture_output=True,
            timeout=5.0,
            use_appcontainer=True,
            appcontainer_config=cfg,
        )
        self.assertEqual(res_eof.returncode, 0)
        self.assertIn("EOF_READ_LEN:0", res_eof.stdout)

        # Part B: 128KB payload roundtrip without pipe buffer deadlock
        large_payload = "A" * 131072
        res_large = run_sandboxed_batch(
            [sys.executable, "-c", "import sys; data = sys.stdin.read(); print(f'RECEIVED:{len(data)}')"],
            input=large_payload,
            capture_output=True,
            timeout=10.0,
            use_appcontainer=True,
            appcontainer_config=cfg,
        )
        self.assertEqual(res_large.returncode, 0)
        self.assertIn(f"RECEIVED:{len(large_payload)}", res_large.stdout)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_timeout_and_termination_in_appcontainer(self):
        """Process executing infinite sleep inside AppContainer is cleanly terminated on timeout."""
        cfg = AppContainerConfig(
            profile_name=f"Jarvis_Timeout_{os.getpid()}_{time.time_ns()}",
            auto_cleanup_profile=True,
        )

        proc_mgr = SandboxedProcess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            use_appcontainer=True,
            appcontainer_config=cfg,
        )

        # Phase 1 & 2: Setup, process creation, Job Object assignment, and resumption
        t_setup_start = time.monotonic()
        proc_mgr.start()
        setup_elapsed = time.monotonic() - t_setup_start
        self.assertIsNotNone(proc_mgr.proc)
        self.assertIsNotNone(proc_mgr.h_job)

        # Phase 3: Execution timeout and prompt teardown
        t_timeout_start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            proc_mgr.proc.communicate(timeout=0.5)
        timeout_elapsed = time.monotonic() - t_timeout_start

        # Directly verify bounded termination from after successful startup (< 3.0s for 0.5s timeout)
        self.assertLess(
            timeout_elapsed,
            3.0,
            f"AppContainer timeout did not terminate promptly! (timeout duration: {timeout_elapsed:.3f}s, setup duration: {setup_elapsed:.3f}s)",
        )

        # Verify child is terminated promptly
        self.assertIsNotNone(proc_mgr.proc.poll())
        self.assertEqual(proc_mgr.proc.returncode, 1)

        # Verify Job Object has zero active processes before closing
        self.assertIsNotNone(proc_mgr.job_holder)
        self.assertEqual(proc_mgr.job_holder.get_active_process_count(), 0)

        # Final cleanup - handles and Job Object cleaned up once
        proc_mgr.terminate_tree()
        self.assertIsNone(proc_mgr.h_job)
        self.assertTrue(proc_mgr._closed)
        self.assertTrue(proc_mgr.job_holder is None or proc_mgr.job_holder.is_closed)

        # Idempotent cleanup check
        self.assertTrue(proc_mgr.terminate_tree())

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_sandboxed_batch_stdin_stdout_pipeline(self):
        """Verifies full standard I/O pipe round-trip through AppContainer and Job Object."""
        cfg = AppContainerConfig(
            profile_name=f"Jarvis_Pipes_{os.getpid()}_{time.time_ns()}",
            auto_cleanup_profile=True,
        )
        res = run_sandboxed_batch(
            [sys.executable, "-c", "import sys; print(f'ECHO: {sys.stdin.read().strip()}')"],
            input="hello_from_parent_host\n",
            capture_output=True,
            use_appcontainer=True,
            appcontainer_config=cfg,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("ECHO: hello_from_parent_host", res.stdout)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_execute_sandboxed_custom_skill_with_appcontainer(self):
        """Verifies SkillSandbox executes a custom skill with opt-in use_appcontainer=True."""
        skill_name = f"appcontainer_test_skill_{os.getpid()}"
        code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{skill_name}", description="AppContainer verified skill")
def {skill_name}(x: int = 5, y: int = 7) -> int:
    return x * y
"""
        res = SkillSandbox.validate_and_promote(skill_name, code, user_approved=True)
        self.assertTrue(res.success, f"Skill promotion failed: {res.message}")

        try:
            # Execute through SkillSandbox with use_appcontainer=True
            out = SkillSandbox.execute_sandboxed_custom_skill(
                clean_name=skill_name,
                tool_name=skill_name,
                kwargs={"x": 6, "y": 9},
                timeout_seconds=5.0,
                use_appcontainer=True,
            )
            self.assertEqual(out, 54)
        finally:
            SkillSandbox.rollback_snapshot(skill_name, None)


class TestAppContainerAttributeListLifecycle(unittest.TestCase):
    """Verifies attribute list initialization, mutation, disposal ordering, and fault injection."""

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_failed_second_stage_init_does_not_delete_list(self):
        """Failed second-stage InitializeProcThreadAttributeList skips delete and cleans up SIDs/profile."""
        from jarvis.core.evolution import appcontainer
        orig_init = appcontainer.kernel32.InitializeProcThreadAttributeList
        orig_del = appcontainer.kernel32.DeleteProcThreadAttributeList

        init_count = 0
        def mock_init(buf, count, flags, size_ptr):
            nonlocal init_count
            init_count += 1
            if init_count == 2:
                return 0
            return orig_init(buf, count, flags, size_ptr)

        delete_called = []
        def mock_del(buf):
            delete_called.append(True)
            return orig_del(buf)

        cfg = AppContainerConfig(profile_name=f"J_Fault1_{os.getpid()}_{time.time_ns()}")
        with mock.patch.object(appcontainer.kernel32, "InitializeProcThreadAttributeList", side_effect=mock_init), \
             mock.patch.object(appcontainer.kernel32, "DeleteProcThreadAttributeList", side_effect=mock_del):
            with self.assertRaises(AppContainerSecurityError):
                AppContainerProcessLauncher.spawn_suspended_appcontainer([sys.executable, "-c", "pass"], config=cfg)
        self.assertEqual(len(delete_called), 0, "DeleteProcThreadAttributeList called on uninitialized buffer!")

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_failed_capabilities_update_deletes_list_before_sid_cleanup(self):
        """Failed UpdateProcThreadAttribute deletes attribute list before freeing referenced SIDs."""
        from jarvis.core.evolution import appcontainer
        orig_del = appcontainer.kernel32.DeleteProcThreadAttributeList
        orig_cleanup = AppContainerProcessLauncher._cleanup_pre_spawn

        events: list[str] = []
        def mock_del(buf):
            events.append("delete_attr_list")
            return orig_del(buf)

        def mock_cleanup(*args, **kwargs):
            events.append("cleanup_pre_spawn")
            return orig_cleanup(*args, **kwargs)

        cfg = AppContainerConfig(profile_name=f"J_Fault2_{os.getpid()}_{time.time_ns()}")
        with mock.patch.object(appcontainer.kernel32, "UpdateProcThreadAttribute", return_value=0), \
             mock.patch.object(appcontainer.kernel32, "DeleteProcThreadAttributeList", side_effect=mock_del), \
             mock.patch.object(AppContainerProcessLauncher, "_cleanup_pre_spawn", side_effect=mock_cleanup):
            with self.assertRaises(AppContainerSecurityError):
                AppContainerProcessLauncher.spawn_suspended_appcontainer([sys.executable, "-c", "pass"], config=cfg)
        self.assertEqual(events, ["delete_attr_list", "cleanup_pre_spawn"])

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_failed_handle_list_update_deletes_list_before_sid_cleanup(self):
        """Failed UpdateProcThreadAttribute for handle list deletes attribute list before SID cleanup."""
        from jarvis.core.evolution import appcontainer
        orig_update = appcontainer.kernel32.UpdateProcThreadAttribute
        orig_del = appcontainer.kernel32.DeleteProcThreadAttributeList
        orig_cleanup = AppContainerProcessLauncher._cleanup_pre_spawn

        events: list[str] = []
        def mock_update(attr_list, flags, attr, lp_value, cb_size, prev, ret_size):
            if attr == appcontainer.PROC_THREAD_ATTRIBUTE_HANDLE_LIST:
                return 0
            return orig_update(attr_list, flags, attr, lp_value, cb_size, prev, ret_size)

        def mock_del(buf):
            events.append("delete_attr_list")
            return orig_del(buf)

        def mock_cleanup(*args, **kwargs):
            events.append("cleanup_pre_spawn")
            return orig_cleanup(*args, **kwargs)

        cfg = AppContainerConfig(profile_name=f"J_Fault3_{os.getpid()}_{time.time_ns()}")
        with mock.patch.object(appcontainer.kernel32, "UpdateProcThreadAttribute", side_effect=mock_update), \
             mock.patch.object(appcontainer.kernel32, "DeleteProcThreadAttributeList", side_effect=mock_del), \
             mock.patch.object(AppContainerProcessLauncher, "_cleanup_pre_spawn", side_effect=mock_cleanup):
            with self.assertRaises(AppContainerSecurityError):
                AppContainerProcessLauncher.spawn_suspended_appcontainer(
                    [sys.executable, "-c", "pass"],
                    stdin_handle=123,
                    config=cfg,
                )
        self.assertEqual(events, ["delete_attr_list", "cleanup_pre_spawn"])

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_failed_create_process_deletes_list_before_sid_cleanup(self):
        """Failed CreateProcessW deletes attribute list before freeing SIDs and cleaning profile."""
        from jarvis.core.evolution import appcontainer
        orig_del = appcontainer.kernel32.DeleteProcThreadAttributeList
        orig_cleanup = AppContainerProcessLauncher._cleanup_pre_spawn

        events: list[str] = []
        def mock_del(buf):
            events.append("delete_attr_list")
            return orig_del(buf)

        def mock_cleanup(*args, **kwargs):
            events.append("cleanup_pre_spawn")
            return orig_cleanup(*args, **kwargs)

        cfg = AppContainerConfig(profile_name=f"J_Fault4_{os.getpid()}_{time.time_ns()}")
        with mock.patch.object(appcontainer.kernel32, "CreateProcessW", return_value=0), \
             mock.patch.object(appcontainer.kernel32, "DeleteProcThreadAttributeList", side_effect=mock_del), \
             mock.patch.object(AppContainerProcessLauncher, "_cleanup_pre_spawn", side_effect=mock_cleanup):
            with self.assertRaises(AppContainerSecurityError):
                AppContainerProcessLauncher.spawn_suspended_appcontainer([sys.executable, "-c", "pass"], config=cfg)
        self.assertEqual(events, ["delete_attr_list", "cleanup_pre_spawn"])

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_failed_nul_handle_creation_cleans_up_all_resources(self):
        """Failed NUL handle creation triggers unified cleanup of grants, SIDs, and profile."""
        from jarvis.core.evolution import appcontainer

        cleanup_called = []
        orig_cleanup = AppContainerProcessLauncher._cleanup_pre_spawn

        def mock_cleanup(*args, **kwargs):
            cleanup_called.append(kwargs)
            return orig_cleanup(*args, **kwargs)

        cfg = AppContainerConfig(profile_name=f"J_FaultNul_{os.getpid()}_{time.time_ns()}")
        with (
            mock.patch.object(appcontainer, "_open_nul_handle", side_effect=OSError("NUL device failure")),
            mock.patch.object(AppContainerProcessLauncher, "_cleanup_pre_spawn", side_effect=mock_cleanup),
            self.assertRaises(OSError),
        ):
            AppContainerProcessLauncher.spawn_suspended_appcontainer(
                [sys.executable, "-c", "pass"],
                stdin_handle=123,
                config=cfg,
            )
        self.assertEqual(len(cleanup_called), 1, "_cleanup_pre_spawn must be called on NUL handle failure")
        self.assertTrue(cleanup_called[0].get("is_new_profile", False))


class TestAppContainerACLOwnership(unittest.TestCase):
    """Verifies ownership-aware ACL management for reused profiles and directory grants."""

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_reused_profile_preserves_preexisting_acl_grants(self):
        """Reusing a profile with an existing ACL must preserve the ACL after the child process exits."""
        temp_dir = tempfile.mkdtemp(prefix="jarvis_acl_reuse_")
        target_path = Path(temp_dir).resolve()
        shared_name = f"Jarvis_ReuseACL_{os.getpid()}_{time.time_ns()}"
        base_cfg = AppContainerConfig(profile_name=shared_name, auto_cleanup_profile=False)

        # 1. Create base profile and initial ACL grant
        p_sid, sid_str, is_new = AppContainerProfileManager.get_or_create_profile(base_cfg)
        self.assertTrue(is_new)
        AppContainerProfileManager.free_sid(p_sid)

        try:
            # Explicitly grant access to target directory
            granted = AppContainerProfileManager.grant_path_access(target_path, sid_str)
            self.assertTrue(granted)
            self.assertTrue(AppContainerProfileManager.has_path_access(target_path, sid_str))

            # 2. Spawn second invocation reusing that profile and path
            child_cfg = AppContainerConfig(
                profile_name=shared_name,
                grant_paths=[target_path],
                auto_cleanup_profile=True,
            )
            handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                config=child_cfg,
            )
            self.assertFalse(handle.is_new_profile)
            self.assertNotIn(target_path, handle.applied_grants)
            handle.resume()

            # 3. Clean up the second invocation
            handle.cleanup()

            # 4. Verify the original ACL remains on target_path and profile is still valid
            self.assertTrue(
                AppContainerProfileManager.has_path_access(target_path, sid_str),
                "Pre-existing ACL was prematurely removed during child cleanup!",
            )
            p_reuse, _, is_new_reuse = AppContainerProfileManager.get_or_create_profile(base_cfg)
            self.assertFalse(is_new_reuse)
            AppContainerProfileManager.free_sid(p_reuse)
        finally:
            # 5. Clean up base profile and revoke ACL
            AppContainerProfileManager.revoke_path_access(target_path, sid_str)
            AppContainerProfileManager.delete_profile(shared_name)
            shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_temporary_profile_revokes_own_acl_grants(self):
        """Temporary invocation revokes all newly introduced ACLs and deletes its profile on cleanup."""
        temp_dir = tempfile.mkdtemp(prefix="jarvis_acl_temp_")
        target_path = Path(temp_dir).resolve()
        temp_name = f"Jarvis_TempACL_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(
            profile_name=temp_name,
            grant_paths=[target_path],
            auto_cleanup_profile=True,
        )

        handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
            command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            config=cfg,
        )
        self.assertTrue(handle.is_new_profile)
        self.assertIn(target_path, handle.applied_grants)
        sid_str = handle.sid_str

        self.assertTrue(AppContainerProfileManager.has_path_access(target_path, sid_str))
        handle.resume()
        handle.cleanup()

        self.assertFalse(
            AppContainerProfileManager.has_path_access(target_path, sid_str),
            "ACL was not revoked upon temporary profile cleanup!",
        )
        shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_preexisting_inadequate_readonly_ace_fails_closed(self):
        """Pre-existing read-only ACE for AppContainer SID on write target fails closed to preserve ACL integrity."""
        temp_dir = tempfile.mkdtemp(prefix="jarvis_inadequate_")
        target_path = Path(temp_dir).resolve()
        shared_name = f"J_Inad_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(profile_name=shared_name, auto_cleanup_profile=False)
        p_sid, sid_str, _ = AppContainerProfileManager.get_or_create_profile(cfg)
        AppContainerProfileManager.free_sid(p_sid)

        try:
            # Explicitly grant read-only (RX) permissions to simulate existing inadequate ACE
            subprocess.run(["icacls", str(target_path), "/grant", f"*{sid_str}:(OI)(CI)(RX)"], check=True, capture_output=True)
            child_cfg = AppContainerConfig(
                profile_name=shared_name,
                grant_paths=[target_path],
                auto_cleanup_profile=False,
            )
            with self.assertRaises(AppContainerSecurityError) as ctx:
                AppContainerProcessLauncher.spawn_suspended_appcontainer([sys.executable, "-c", "pass"], config=child_cfg)
            self.assertIn("insufficient permissions", str(ctx.exception))
        finally:
            AppContainerProfileManager.revoke_path_access(target_path, sid_str)
            AppContainerProfileManager.delete_profile(shared_name)
            shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_preexisting_deny_ace_fails_closed(self):
        """Pre-existing explicit DENY ACE for AppContainer SID aborts launch and fails closed."""
        temp_dir = tempfile.mkdtemp(prefix="jarvis_deny_")
        target_path = Path(temp_dir).resolve()
        shared_name = f"J_Deny_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(profile_name=shared_name, auto_cleanup_profile=False)
        p_sid, sid_str, _ = AppContainerProfileManager.get_or_create_profile(cfg)
        AppContainerProfileManager.free_sid(p_sid)

        try:
            # Explicitly add DENY ACE
            subprocess.run(["icacls", str(target_path), "/deny", f"*{sid_str}:(OI)(CI)(W)"], check=True, capture_output=True)
            child_cfg = AppContainerConfig(
                profile_name=shared_name,
                grant_paths=[target_path],
                auto_cleanup_profile=False,
            )
            with self.assertRaises(AppContainerSecurityError) as ctx:
                AppContainerProcessLauncher.spawn_suspended_appcontainer([sys.executable, "-c", "pass"], config=child_cfg)
            self.assertIn("DENY ACE", str(ctx.exception))
        finally:
            AppContainerProfileManager.revoke_path_access(target_path, sid_str)
            AppContainerProfileManager.delete_profile(shared_name)
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestAppContainerRuntimeAccess(unittest.TestCase):
    """Verifies multi-prefix Python runtime access detection, validation, and rollback."""

    def test_required_runtime_paths_includes_both_prefixes(self):
        """get_required_runtime_paths resolves and includes both sys.base_prefix and sys.prefix."""
        d1 = tempfile.mkdtemp(prefix="jarvis_base_prefix_")
        d2 = tempfile.mkdtemp(prefix="jarvis_prefix_")
        try:
            with mock.patch("sys.base_prefix", d1), mock.patch("sys.prefix", d2):
                paths = AppContainerProfileManager.get_required_runtime_paths()
                self.assertIn(Path(d1).resolve(), paths)
                self.assertIn(Path(d2).resolve(), paths)
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_ensure_runtime_access_fails_closed_if_one_prefix_denied(self):
        """If one prefix is accessible but another prefix fails grant, ensure_runtime_access fails closed."""
        d1 = Path(tempfile.mkdtemp()).resolve()
        d2 = Path(tempfile.mkdtemp()).resolve()
        try:
            with mock.patch.object(AppContainerProfileManager, "get_required_runtime_paths", return_value=[d1, d2]), \
                 mock.patch.object(AppContainerProfileManager, "check_runtime_path_effective_access", side_effect=[("SUFFICIENT", "(RX)"), ("NONE", None)]), \
                 mock.patch.object(AppContainerProfileManager, "grant_path_access", return_value=False):
                with self.assertRaises(AppContainerSecurityError) as ctx:
                    AppContainerProfileManager.ensure_runtime_access("S-1-15-2-1234")
                self.assertIn(str(d2), str(ctx.exception))
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_ensure_runtime_access_rolls_back_grants_on_failure(self):
        """If setup fails after granting one path, that granted path is rolled back immediately."""
        d1 = Path(tempfile.mkdtemp()).resolve()
        d2 = Path(tempfile.mkdtemp()).resolve()
        try:
            with mock.patch.object(AppContainerProfileManager, "get_required_runtime_paths", return_value=[d1, d2]), \
                 mock.patch.object(AppContainerProfileManager, "check_runtime_path_effective_access", side_effect=[("NONE", None), ("NONE", None)]), \
                 mock.patch.object(AppContainerProfileManager, "has_runtime_path_access", return_value=True), \
                 mock.patch.object(AppContainerProfileManager, "grant_path_access", side_effect=[True, False]), \
                 mock.patch.object(AppContainerProfileManager, "revoke_path_access") as mock_revoke:
                with self.assertRaises(AppContainerSecurityError):
                    AppContainerProfileManager.ensure_runtime_access("S-1-15-2-1234")
                mock_revoke.assert_called_once_with(d1, "S-1-15-2-1234")
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_check_runtime_path_broad_package_rx_is_sufficient(self):
        """Broad package ACE with (RX) read permissions evaluates to SUFFICIENT."""
        d = Path(tempfile.mkdtemp()).resolve()
        try:
            mock_out = f"{d} APPLICATION PACKAGE AUTHORITY\\ALL APPLICATION PACKAGES:(OI)(CI)(RX)\nSuccessfully processed 1 files"
            res = subprocess.CompletedProcess(args=["icacls", str(d)], returncode=0, stdout=mock_out, stderr="")
            with mock.patch("subprocess.run", return_value=res):
                status, perms = AppContainerProfileManager.check_runtime_path_effective_access(d, "S-1-15-2-1234")
                self.assertEqual(status, "SUFFICIENT")
                self.assertIn("(RX)", str(perms))
                self.assertTrue(AppContainerProfileManager.has_runtime_path_access(d, "S-1-15-2-1234"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_check_runtime_path_broad_package_inadequate_permission(self):
        """Broad package ACE with only write (W) permission evaluates to INADEQUATE."""
        d = Path(tempfile.mkdtemp()).resolve()
        try:
            mock_out = f"{d} APPLICATION PACKAGE AUTHORITY\\ALL APPLICATION PACKAGES:(OI)(CI)(W)\nSuccessfully processed 1 files"
            res = subprocess.CompletedProcess(args=["icacls", str(d)], returncode=0, stdout=mock_out, stderr="")
            with mock.patch("subprocess.run", return_value=res):
                status, perms = AppContainerProfileManager.check_runtime_path_effective_access(d, "S-1-15-2-1234")
                self.assertEqual(status, "INADEQUATE")
                self.assertIn("(W)", str(perms))
                self.assertFalse(AppContainerProfileManager.has_runtime_path_access(d, "S-1-15-2-1234"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_check_runtime_path_explicit_deny_overrides_broad_package(self):
        """Explicit DENY ACE on AppContainer SID overrides broad package ACE and evaluates to DENIED."""
        d = Path(tempfile.mkdtemp()).resolve()
        try:
            sid = "S-1-15-2-9999"
            mock_out = (
                f"{d} APPLICATION PACKAGE AUTHORITY\\ALL APPLICATION PACKAGES:(OI)(CI)(RX)\n"
                f"     {sid}:(DENY)(W,Rc)\n"
                f"Successfully processed 1 files"
            )
            res = subprocess.CompletedProcess(args=["icacls", str(d)], returncode=0, stdout=mock_out, stderr="")
            with mock.patch("subprocess.run", return_value=res):
                status, perms = AppContainerProfileManager.check_runtime_path_effective_access(d, sid)
                self.assertEqual(status, "DENIED")
                self.assertIn("(DENY)", str(perms))
                self.assertFalse(AppContainerProfileManager.has_runtime_path_access(d, sid))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_check_runtime_path_unreadable_acl_fails_closed(self):
        """When icacls fails or returns access denied, check_runtime_path evaluates to UNREADABLE."""
        d = Path(tempfile.mkdtemp()).resolve()
        try:
            res = subprocess.CompletedProcess(args=["icacls", str(d)], returncode=5, stdout="", stderr="Access is denied.")
            with mock.patch("subprocess.run", return_value=res):
                status, err = AppContainerProfileManager.check_runtime_path_effective_access(d, "S-1-15-2-1234")
                self.assertEqual(status, "UNREADABLE")
                self.assertIn("Access is denied", str(err))
                self.assertFalse(AppContainerProfileManager.has_runtime_path_access(d, "S-1-15-2-1234"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_ensure_runtime_access_fails_closed_on_explicit_deny_with_rollback(self):
        """ensure_runtime_access fails closed immediately on DENIED path and rolls back prior grants."""
        d1 = Path(tempfile.mkdtemp()).resolve()
        d2 = Path(tempfile.mkdtemp()).resolve()
        try:
            with mock.patch.object(AppContainerProfileManager, "get_required_runtime_paths", return_value=[d1, d2]), \
                 mock.patch.object(AppContainerProfileManager, "check_runtime_path_effective_access", side_effect=[("NONE", None), ("DENIED", "(DENY)")]), \
                 mock.patch.object(AppContainerProfileManager, "grant_path_access", return_value=True), \
                 mock.patch.object(AppContainerProfileManager, "has_runtime_path_access", return_value=True), \
                 mock.patch.object(AppContainerProfileManager, "revoke_path_access") as mock_revoke:
                with self.assertRaises(AppContainerSecurityError) as ctx:
                    AppContainerProfileManager.ensure_runtime_access("S-1-15-2-1234")
                self.assertIn("explicit pre-existing DENY ACE", str(ctx.exception))
                mock_revoke.assert_called_once_with(d1, "S-1-15-2-1234")
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_ensure_runtime_access_fails_closed_on_unreadable_with_rollback(self):
        """ensure_runtime_access fails closed immediately on UNREADABLE path and rolls back prior grants."""
        d1 = Path(tempfile.mkdtemp()).resolve()
        d2 = Path(tempfile.mkdtemp()).resolve()
        try:
            with mock.patch.object(AppContainerProfileManager, "get_required_runtime_paths", return_value=[d1, d2]), \
                 mock.patch.object(AppContainerProfileManager, "check_runtime_path_effective_access", side_effect=[("NONE", None), ("UNREADABLE", "DACL unreadable")]), \
                 mock.patch.object(AppContainerProfileManager, "grant_path_access", return_value=True), \
                 mock.patch.object(AppContainerProfileManager, "has_runtime_path_access", return_value=True), \
                 mock.patch.object(AppContainerProfileManager, "revoke_path_access") as mock_revoke:
                with self.assertRaises(AppContainerSecurityError) as ctx:
                    AppContainerProfileManager.ensure_runtime_access("S-1-15-2-1234")
                self.assertIn("could not be read", str(ctx.exception))
                mock_revoke.assert_called_once_with(d1, "S-1-15-2-1234")
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)


class TestAppContainerCapabilities(unittest.TestCase):
    """Verifies capability parsing, resolution, and fail-closed validation."""

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_valid_capability_resolves_and_launches(self):
        """Valid capability name resolves to SID and launches successfully."""
        cfg = AppContainerConfig(
            profile_name=f"J_CapV_{os.getpid()}_{time.time_ns()}",
            capabilities=["internetClient"],
            auto_cleanup_profile=True,
        )
        handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
            command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            config=cfg,
        )
        try:
            self.assertEqual(len(handle.capability_sids), 1)
            self.assertTrue(handle.resume())
        finally:
            handle.cleanup()

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_invalid_capability_fails_closed_before_spawn_and_rolls_back(self):
        """Invalid capability name fails closed with AppContainerSecurityError before CreateProcessW."""
        cfg = AppContainerConfig(
            profile_name=f"J_CapInv_{os.getpid()}_{time.time_ns()}",
            capabilities=["completely_invalid_capability_xyz_999"],
            auto_cleanup_profile=True,
        )
        with self.assertRaises(AppContainerSecurityError) as ctx:
            AppContainerProcessLauncher.spawn_suspended_appcontainer(
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                config=cfg,
            )
        self.assertIn("completely_invalid_capability_xyz_999", str(ctx.exception))


class TestAppContainerCommunicator(unittest.TestCase):
    """Verifies stdin/stdout/stderr pipe communication, error propagation, and EOF semantics."""

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_communicate_partial_write_surfaces_error(self):
        """When WriteFile reports 0 bytes written, communicate raises RuntimeError."""
        from jarvis.core.evolution import appcontainer
        proc_mgr = SandboxedProcess(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            use_appcontainer=True,
        )
        proc_mgr.start()
        try:
            def mock_write(h, buf, n, written_ptr, ov):
                written_ptr._obj.value = 0
                return 1

            with mock.patch.object(appcontainer.kernel32, "WriteFile", side_effect=mock_write):
                with self.assertRaises(RuntimeError) as ctx:
                    proc_mgr.proc.communicate(input="test payload", timeout=2.0)
                self.assertIn("wrote 0 bytes", str(ctx.exception))
        finally:
            proc_mgr.terminate_tree()

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_communicate_failed_write_surfaces_error(self):
        """When WriteFile fails with an unexpected WinError, communicate raises RuntimeError."""
        from jarvis.core.evolution import appcontainer
        proc_mgr = SandboxedProcess(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            use_appcontainer=True,
        )
        proc_mgr.start()
        try:
            with mock.patch.object(appcontainer.kernel32, "WriteFile", return_value=0), \
                 mock.patch("ctypes.get_last_error", return_value=6):
                with self.assertRaises(RuntimeError) as ctx:
                    proc_mgr.proc.communicate(input="test payload", timeout=2.0)
                self.assertIn("WinError 6", str(ctx.exception))
        finally:
            proc_mgr.terminate_tree()

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_communicate_child_closes_stdin_early_preserves_broken_pipe(self):
        """When child closes stdin early, communicate handles broken pipe cleanly without error."""
        res = run_sandboxed_batch(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            input="excess payload data " * 5000,
            capture_output=True,
            use_appcontainer=True,
        )
        self.assertEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
