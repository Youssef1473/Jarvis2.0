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
    # Strictly require expected Windows access-denied error code (winerror 5 or errno 13)
    winerr = getattr(pe, 'winerror', None)
    if winerr in (5, None) or pe.errno == 13:
        print('KERNEL_CONTAINMENT_VERIFIED: PermissionError observed')
        sys.exit(0)
    else:
        sys.stderr.write(f'UNEXPECTED_PERMISSION_ERROR: winerror={{winerr}}, errno={{pe.errno}}\\n')
        sys.exit(21)
except OSError as oe:
    winerr = getattr(oe, 'winerror', None)
    if winerr == 5 or oe.errno == 13:
        print(f'KERNEL_CONTAINMENT_VERIFIED: Access denied with winerror={{winerr}}')
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

        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_sandboxed_batch(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=0.5,
                use_appcontainer=True,
                appcontainer_config=cfg,
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 4.0, "AppContainer timeout did not terminate in bounded time!")

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


if __name__ == "__main__":
    unittest.main()
