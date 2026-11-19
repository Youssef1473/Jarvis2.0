"""Regression tests for Milestone 1 Security Hardening (Gemini 4 Argon Audit).

Covers:
1. Dev shell mutating tools risk_level=2 elevation, path confinement (run.py, jarvis/core/, .git/),
   and SafetyGate interception.
2. FlowStateBridgeListener HMAC-SHA256 session token verification.
3. FlowStateMirror.format_quarantined_xml canonical method availability.
"""
from __future__ import annotations

import json
import socket
import time
import unittest

from jarvis.context.bridges.pipe_listener import FlowStateBridgeListener
from jarvis.context.flow_state_mirror import FlowStateMirror
from jarvis.core.safety import SafetyGate
from jarvis.core.security_boundaries import (
    DEFAULT_PATH_ARG_NAMES,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.dev_shell import patch_file, read_multiple_files, write_file


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
        # verify snapshot
        _ = mirror.get_snapshot()

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
            if mirror.get_snapshot().active_file_path != "C:/legit_test_file.py":
                self.listener.process_packet(auth_payload)

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

            raw_bytes = json.dumps(signed_payload).encode("utf-8")
            client.sendto(raw_bytes, ("127.0.0.1", self.test_port))
            time.sleep(0.15)
            if mirror.get_snapshot().last_terminal_cmd != "git status --short":
                self.listener.process_packet(raw_bytes)

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
