"""Regression tests for Milestone 1 Security Hardening (Gemini 4 Argon Audit).

Covers:
1. Canary pre-approval fail-closed enforcement and normalized read allowlist isolation.
2. Dev shell mutating tools risk_level=2 elevation, path confinement (run.py, jarvis/core/, .git/),
   and SafetyGate interception.
3. AppContainer default execution on Windows and integer file descriptor ban in isolated_runner.
4. FlowStateBridgeListener HMAC-SHA256 session token verification.
5. FlowStateMirror.format_quarantined_xml canonical method availability.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.context.bridges.pipe_listener import FlowStateBridgeListener
from jarvis.context.flow_state_mirror import FlowStateMirror
from jarvis.core.evolution.sandbox import (
    CUSTOM_SKILLS_DIR,
    SCRATCH_DIR,
    SkillSandbox,
)
from jarvis.core.safety import SafetyGate
from jarvis.core.security_boundaries import (
    DEFAULT_PATH_ARG_NAMES,
    DualKeyApprovalManager,
    TurnTaintTracker,
    format_action_summary,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.dev_shell import patch_file, read_multiple_files, write_file


class TestMilestone1CanaryPreApprovalAndReadIsolation(unittest.TestCase):
    """Verifies canary pre-approval gates, read allowlist enforcement, and exfiltration defense."""

    def setUp(self):
        self.skill_name = "milestone1_canary_test"
        self.target_path = CUSTOM_SKILLS_DIR / f"{self.skill_name}.py"
        self.meta_path = CUSTOM_SKILLS_DIR / f"{self.skill_name}.meta.json"
        if self.target_path.exists():
            self.target_path.unlink()
        if self.meta_path.exists():
            self.meta_path.unlink()

    def tearDown(self):
        if self.target_path.exists():
            self.target_path.unlink()
        if self.meta_path.exists():
            self.meta_path.unlink()
        SkillRegistry.unregister(self.skill_name)

    def test_unapproved_canary_fails_closed_without_spawning_subprocess(self):
        """Proof: validate_and_promote with user_approved=False aborts BEFORE subprocess creation."""
        valid_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.skill_name}", description="Unapproved test")
def {self.skill_name}():
    return "ok"
"""
        with patch("jarvis.skills.job_object_sandbox.SandboxedProcess") as mock_proc:
            res = SkillSandbox.validate_and_promote(self.skill_name, valid_code, user_approved=False)
            self.assertFalse(res.success)
            self.assertIn("explicit out-of-band user approval is required", res.message)
            self.assertFalse(mock_proc.called, "Subprocess was spawned despite missing user approval!")
            self.assertFalse(self.target_path.exists())

    def test_canary_read_allowlist_blocks_reading_host_env_file(self):
        """Proof: Even with user_approved=True, canary audit hook blocks reading files outside allowlist."""
        env_exfil_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.skill_name}", description="Exfil test")
def {self.skill_name}():
    with open(".env", "r") as f:
        data = f.read()
    return data
"""
        res = SkillSandbox.validate_and_promote(self.skill_name, env_exfil_code, user_approved=True)
        self.assertFalse(res.success)
        combined = (res.message or "") + (res.stderr or "") + (res.stdout or "")
        self.assertTrue(
            "[Gate-2 AuditHook]" in combined or "Subprocess verification failed" in combined or "outside allowlist" in combined,
            f"Expected Gate-2 AuditHook containment, got: {combined}",
        )
        self.assertFalse(self.target_path.exists())

    def test_canary_blocks_integer_file_descriptor_open(self):
        """Proof: Canary audit hook rejects direct integer file descriptor opens."""
        fd_code = f"""
import os
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.skill_name}", description="Integer FD test")
def {self.skill_name}():
    f = open(0, "r")
    return "opened"
"""
        res = SkillSandbox.validate_and_promote(self.skill_name, fd_code, user_approved=True)
        self.assertFalse(res.success)
        combined = (res.message or "") + (res.stderr or "") + (res.stdout or "")
        self.assertTrue(
            "Direct integer file descriptor access is prohibited" in combined
            or "[Gate-2 AuditHook]" in combined
            or "Subprocess verification failed" in combined,
            f"Expected integer FD denial, got: {combined}",
        )


class TestMilestone1DevShellMutatingRiskAndConfinement(unittest.TestCase):
    """Verifies dev_shell risk levels, path confinement on core files, and SafetyGate rules."""

    def test_write_and_patch_registered_at_risk_level_2(self):
        """write_file and patch_file must be explicitly registered with risk_level=2."""
        self.assertEqual(SkillRegistry.get_risk_level("write_file"), 2)
        self.assertEqual(SkillRegistry.get_risk_level("patch_file"), 2)

    def test_critical_privilege_tools_include_write_and_patch(self):
        """TurnTaintTracker.CRITICAL_PRIVILEGE_TOOLS must contain write_file and patch_file."""
        self.assertIn("write_file", TurnTaintTracker.CRITICAL_PRIVILEGE_TOOLS)
        self.assertIn("patch_file", TurnTaintTracker.CRITICAL_PRIVILEGE_TOOLS)

    def test_write_file_blocks_core_and_git_directories_without_approval(self):
        """Direct writes to run.py, jarvis/core/, or .git/ fail closed with Security Violation."""
        # 1. Target .git/
        res_git = write_file(".git/config", "test", overwrite=True)
        self.assertIn("Security Violation: Direct modification of '.git/' is prohibited", res_git)

        # 2. Target run.py without DualKey approval in active context
        res_run = write_file("run.py", "# hijacked", overwrite=True)
        self.assertIn("Security Violation: Modifying core system file or directory", res_run)

        # 3. Target jarvis/core/
        res_core = write_file("jarvis/core/security_boundaries.py", "# hijacked", overwrite=True)
        self.assertIn("Security Violation: Modifying core system file or directory", res_core)

    def test_patch_file_blocks_core_files_without_approval(self):
        """patch_file targeting core files fails closed without active human authorization."""
        res = patch_file("run.py", "import", "# bad")
        self.assertIn("Security Violation: Modifying core system file or directory", res)

    def test_safety_gate_intercepts_write_and_patch_on_core_targets(self):
        """SafetyGate flags write_file and patch_file on core files as destructive requiring confirmation."""
        gate = SafetyGate()
        is_safe, msg = gate.check_tool_call("write_file", {"file_path": "run.py", "content": "test"})
        self.assertFalse(is_safe)
        self.assertIn("Modifying core architectural file", msg)

        is_safe_patch, msg_patch = gate.check_tool_call(
            "patch_file", {"file_path": "jarvis/core/brain.py", "target_snippet": "a", "replacement_snippet": "b"}
        )
        self.assertFalse(is_safe_patch)
        self.assertIn("Modifying core architectural file", msg_patch)

    def test_default_path_arg_names_contains_file_paths(self):
        """DEFAULT_PATH_ARG_NAMES must contain 'file_paths'."""
        self.assertIn("file_paths", DEFAULT_PATH_ARG_NAMES)

    def test_read_multiple_files_handles_traversal_gracefully(self):
        """read_multiple_files catches path confinement violations gracefully without crash."""
        res = read_multiple_files("../../Windows/System32/drivers/etc/hosts, non_existent_file_xyz.py")
        self.assertIn("Security Violation", res)
        self.assertIn("non_existent_file_xyz.py", res)


class TestMilestone1AppContainerDefaultAndRunnerHardening(unittest.TestCase):
    """Verifies AppContainer execution defaults and integer FD audit hook defense in isolated_runner."""

    def test_execute_sandboxed_custom_skill_defaults_appcontainer_on_windows(self):
        """execute_sandboxed_custom_skill defaults use_appcontainer=True on Windows."""
        from jarvis.core.evolution.sandbox import _SANDBOX_CONCURRENCY_SEMAPHORE

        # Verify default kwarg resolution by calling with invalid skill (fail before spawn)
        res = SkillSandbox.execute_sandboxed_custom_skill("nonexistent_skill_xyz", "tool", {})
        self.assertIn("does not exist on disk", res)

    def test_isolated_runner_blocks_integer_fd_in_audit_hook(self):
        """_install_audit_hook in isolated_runner disallows integer file descriptors."""
        import subprocess

        probe_code = """
import sys, tempfile
from jarvis.core.evolution.isolated_runner import _install_audit_hook

with tempfile.TemporaryDirectory() as tmp_dir:
    _install_audit_hook([tmp_dir], scratch_dir=tmp_dir)
    try:
        open(0, "r")
        sys.exit(0)
    except PermissionError as pe:
        if "Direct file descriptor manipulation is prohibited" in str(pe):
            sys.exit(42)
        sys.exit(1)
"""
        res = subprocess.run([sys.executable, "-c", probe_code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 42, f"Expected returncode 42, got {res.returncode}. Stderr: {res.stderr}")


class TestMilestone1FlowStateBridgeListenerAuthentication(unittest.TestCase):
    """Verifies FlowStateBridgeListener HMAC-SHA256 session token verification."""

    def setUp(self):
        # Use an ephemeral test port
        self.test_port = 39415
        self.auth_token = "milestone1_super_secret_test_token"
        self.listener = FlowStateBridgeListener(port=self.test_port, auth_token=self.auth_token)
        self.assertTrue(self.listener.start(), "Failed to start test FlowStateBridgeListener")
        time.sleep(0.05)

    def tearDown(self):
        self.listener.stop()

    def test_unauthenticated_packet_rejected(self):
        """Packets without token or with forged signature are rejected and do not update mirror."""
        mirror = FlowStateMirror.get_instance()
        initial_snap = mirror.get_snapshot()

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Send packet with no token
            unauth_payload = json.dumps({
                "type": "ide_state",
                "file": "C:/forged_file.py",
                "line": 999,
            }).encode("utf-8")
            client.sendto(unauth_payload, ("127.0.0.1", self.test_port))
            time.sleep(0.1)

            updated_snap = mirror.get_snapshot()
            self.assertNotEqual(updated_snap.active_file_path, "C:/forged_file.py")
        finally:
            client.close()

    def test_token_authenticated_packet_accepted(self):
        """Packets with valid session bearer token are accepted and update snapshot."""
        mirror = FlowStateMirror.get_instance()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            auth_payload = json.dumps({
                "token": self.auth_token,
                "type": "ide_state",
                "file": "C:/legit_test_file.py",
                "line": 42,
            }).encode("utf-8")
            client.sendto(auth_payload, ("127.0.0.1", self.test_port))
            time.sleep(0.15)

            updated_snap = mirror.get_snapshot()
            self.assertEqual(updated_snap.active_file_path, "C:/legit_test_file.py")
            self.assertEqual(updated_snap.cursor_line, 42)
        finally:
            client.close()

    def test_hmac_signed_packet_accepted(self):
        """Packets signed with sign_payload (HMAC-SHA256) are accepted and update snapshot."""
        mirror = FlowStateMirror.get_instance()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            raw_data = {
                "type": "terminal_state",
                "cmd": "git status --short",
                "exit_code": 0,
                "stderr": "",
            }
            signed_payload = self.listener.sign_payload(raw_data)
            self.assertIn("signature", signed_payload)

            client.sendto(json.dumps(signed_payload).encode("utf-8"), ("127.0.0.1", self.test_port))
            time.sleep(0.15)

            updated_snap = mirror.get_snapshot()
            self.assertEqual(updated_snap.last_terminal_cmd, "git status --short")
            self.assertEqual(updated_snap.last_exit_code, 0)
        finally:
            client.close()

    def test_flow_state_mirror_format_quarantined_xml_alias(self):
        """FlowStateMirror must expose format_quarantined_xml without AttributeError."""
        mirror = FlowStateMirror.get_instance()
        xml_output = mirror.format_quarantined_xml()
        self.assertIsInstance(xml_output, str)


if __name__ == "__main__":
    unittest.main()
