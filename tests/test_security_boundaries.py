"""Unit tests for Closed-Loop Security Boundaries, SSRF Protection, and Taint Tracking."""
import socket
import tempfile
import unittest
from unittest.mock import patch

from jarvis.core.security_boundaries import (
    SecurityBoundaryViolation,
    TurnTaintTracker,
    validate_confined_path,
    validate_egress_url,
)


class TestSecurityBoundaries(unittest.TestCase):
    def test_blocks_illegal_schemes(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("file:///C:/Windows/win.ini")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("javascript:alert(1)")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("data:text/html,<h1>test</h1>")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("ftp://ftp.example.com/file.txt")

    def test_blocks_localhost_and_loopback(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://127.0.0.1:9222")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://localhost:8080")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://sub.localhost")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://[::1]:80")

    def test_blocks_private_and_link_local_ips(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://169.254.169.254/latest/meta-data/")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://192.168.1.1/")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://10.0.0.1/admin")

        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://172.16.0.1/dashboard")

    def test_blocks_ipv4_mapped_ipv6_loopback(self):
        with self.assertRaises(SecurityBoundaryViolation):
            validate_egress_url("http://[::ffff:127.0.0.1]/test")

    def test_blocks_dns_resolving_to_private_ip(self):
        # Mock DNS resolution returning private IP for a public-looking domain
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.50", 80))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                validate_egress_url("http://attacker.example.com/api")
            self.assertIn("non-global address", str(ctx.exception))

    def test_allows_valid_public_domain(self):
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            res = validate_egress_url("https://example.com/news")
            self.assertEqual(res, "https://example.com/news")

    def test_path_confinement_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            with self.assertRaises(SecurityBoundaryViolation):
                validate_confined_path(r"..\..\Windows\System32\cmd.exe", tmp_root)

    def test_path_confinement_blocks_reserved_devices(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            for dev in ("NUL.txt", "CON", "prn.png", "AUX.log", "com1.bin"):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_confined_path(f"{tmp_root}/{dev}", tmp_root)

    def test_path_confinement_allows_safe_subpath(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            safe_target = f"{tmp_root}/sub/screenshot.png"
            res = validate_confined_path(safe_target, tmp_root)
            self.assertIn("screenshot.png", res)

    def test_turn_taint_tracker_blocks_high_privilege_after_untrusted_input(self):
        tracker = TurnTaintTracker.get_instance()
        tracker.reset_turn()
        self.assertFalse(tracker.is_tainted)

        # Untrusted tool execution marks turn as tainted
        tracker.check_tool_execution("browser_get_dom", {})
        self.assertTrue(tracker.is_tainted)

        # High-privilege tool execution without dual_key_confirmed raises SecurityBoundaryViolation
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            tracker.check_tool_execution("execute_powershell", {"command": "dir"})
        self.assertIn("Dual-Key Safety Gate", str(ctx.exception))

        # With dual_key_confirmed=True, execution is permitted
        tracker.check_tool_execution("execute_powershell", {"command": "dir"}, dual_key_confirmed=True)

        tracker.reset_turn()
        self.assertFalse(tracker.is_tainted)


if __name__ == "__main__":
    unittest.main()
