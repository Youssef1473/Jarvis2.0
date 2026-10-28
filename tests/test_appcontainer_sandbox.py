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

        p_sid1, sid_str1 = AppContainerProfileManager.get_or_create_profile(cfg)
        try:
            self.assertGreater(p_sid1, 0)
            self.assertTrue(sid_str1.startswith("S-1-15-2-"))

            # Reuse: attempting to create existing profile must derive identical SID
            p_sid2, sid_str2 = AppContainerProfileManager.get_or_create_profile(cfg)
            try:
                self.assertEqual(sid_str1, sid_str2)
            finally:
                AppContainerProfileManager.free_sid(p_sid2)
        finally:
            AppContainerProfileManager.free_sid(p_sid1)
            deleted = AppContainerProfileManager.delete_profile(unique_name)
            self.assertTrue(deleted)

    @unittest.skipUnless(IS_WIN, "AppContainer APIs require Windows")
    def test_dedicated_working_directory_isolation(self):
        """Dedicated scratch directory is granted access for AppContainer SID and cleanly revoked."""
        temp_dir = tempfile.mkdtemp(prefix="jarvis_acl_test_")
        test_path = Path(temp_dir)
        unique_name = f"Jarvis_Test_ACL_{os.getpid()}_{time.time_ns()}"
        cfg = AppContainerConfig(profile_name=unique_name)

        p_sid, sid_str = AppContainerProfileManager.get_or_create_profile(cfg)
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
except PermissionError:
    # Kernel correctly blocked write
    print('KERNEL_CONTAINMENT_VERIFIED: PermissionError observed')
    sys.exit(0)
except Exception as e:
    # Any other access error (e.g., OSError 13) also proves blocked access
    print(f'KERNEL_CONTAINMENT_VERIFIED: Access denied with {{type(e).__name__}}')
    sys.exit(0)
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
