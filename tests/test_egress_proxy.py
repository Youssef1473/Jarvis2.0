"""Hermetic unit and integration test suite for CORE-NET-01B: Local Egress Proxy,
Destination IP Pinning, DNS Rebinding Defense, Port Control, and Protocol Handling.
"""
from __future__ import annotations

import socket
import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Add workspace root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.security_boundaries import SecurityBoundaryViolation
from jarvis.skills.egress_proxy import (
    EgressProxyServer,
)


class TestEgressProxyCore(unittest.TestCase):
    """Hermetic unit tests for proxy lifecycle, input validation, and destination pinning."""

    def setUp(self):
        self.proxy = EgressProxyServer(host="127.0.0.1", port=0)
        self.proxy_port = self.proxy.start()

    def tearDown(self):
        self.proxy.stop()

    def _send_raw_proxy_request(self, payload: bytes) -> bytes:
        """Sends raw bytes to the proxy and returns the full response bytes."""
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2.0) as s:
            s.sendall(payload)
            response = bytearray()
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"200 Connection Established" in response and b"\r\n\r\n" in response:
                        break
                except (TimeoutError, OSError):
                    break
            return bytes(response)

    def test_proxy_startup_and_loopback_binding(self):
        """Verifies proxy binds strictly to 127.0.0.1 on a dynamic port and reports alive."""
        self.assertTrue(self.proxy.is_alive())
        self.assertGreater(self.proxy_port, 1024)
        self.assertEqual(self.proxy.host, "127.0.0.1")

        # Reject non-loopback binding configuration
        with self.assertRaises(SecurityBoundaryViolation):
            EgressProxyServer(host="0.0.0.0", port=0)

    def test_outbound_ip_pinning_and_zero_second_dns(self):
        """Verifies outbound TCP connection connects to numeric IP literal and never to the hostname."""
        synthetic_ip = "93.184.216.34"
        s1, s2 = socket.socketpair()
        s2.close()

        try:
            # Mock resolve_and_validate_host_port to return synthetic global IP
            with patch("jarvis.skills.egress_proxy.resolve_and_validate_host_port", return_value=(synthetic_ip, socket.AF_INET, 443)) as mock_val, \
                 patch.object(self.proxy, "_create_outbound_connection", return_value=s1) as mock_out:

                req = b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
                resp = self._send_raw_proxy_request(req)

                # Must have validated host and port
                mock_val.assert_called_once_with(
                    hostname="example.com",
                    port=443,
                    allow_local_mcp=False,
                    allow_localhost=False,
                    disallowed_ports=self.proxy.disallowed_ports,
                    allowed_ports=self.proxy.allowed_ports,
                )

                # Socket connect MUST be called with synthetic IP literal tuple, NEVER "example.com"
                mock_out.assert_called_once_with(socket.AF_INET, synthetic_ip, 443)
                self.assertIn(b"200 Connection Established", resp)
        finally:
            try:
                s1.close()
            except OSError:
                pass

    def test_dns_rebinding_defense(self):
        """Verifies a host resolving publicly on attempt 1 and privately on attempt 2 is rejected fail-closed."""
        s1, s2 = socket.socketpair()
        s2.close()

        try:
            with patch("jarvis.skills.egress_proxy.resolve_and_validate_host_port") as mock_val, \
                 patch.object(self.proxy, "_create_outbound_connection", return_value=s1) as mock_out:

                mock_val.return_value = ("93.184.216.34", socket.AF_INET, 443)
                req1 = b"CONNECT rebinding-target.com:443 HTTP/1.1\r\nHost: rebinding-target.com:443\r\n\r\n"
                resp1 = self._send_raw_proxy_request(req1)
                self.assertIn(b"200 Connection Established", resp1)
                mock_out.assert_called_once_with(socket.AF_INET, "93.184.216.34", 443)

                # Attempt 2: Rebound to private loopback IP -> validator raises SecurityBoundaryViolation
                mock_out.reset_mock()
                mock_val.side_effect = SecurityBoundaryViolation("SSRF Protection: Host resolved to non-global address '127.0.0.1'")
                req2 = b"CONNECT rebinding-target.com:443 HTTP/1.1\r\nHost: rebinding-target.com:443\r\n\r\n"
                resp2 = self._send_raw_proxy_request(req2)
                self.assertIn(b"403 Forbidden", resp2)
                self.assertIn(b"SSRF protection", resp2)
                mock_out.assert_not_called()
        finally:
            try:
                s1.close()
            except OSError:
                pass

    def test_mixed_dns_answers_rejected_fail_closed(self):
        """Verifies destination returning mixed public and private DNS records is rejected with 403 Forbidden."""
        real_gai = socket.getaddrinfo

        def mock_gai(host, port, *args, **kwargs):
            if host in ("127.0.0.1", "localhost", "::1"):
                return real_gai(host, port, *args, **kwargs)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
            ]

        with patch("socket.getaddrinfo", side_effect=mock_gai), \
             patch.object(self.proxy, "_create_outbound_connection") as mock_out:

            req = b"CONNECT dual-homed.example.com:443 HTTP/1.1\r\nHost: dual-homed.example.com:443\r\n\r\n"
            resp = self._send_raw_proxy_request(req)

            # Outbound connect must NEVER be called
            mock_out.assert_not_called()
            self.assertIn(b"403 Forbidden", resp)
            self.assertIn(b"non-global address", resp)

    def test_private_and_loopback_destinations_blocked(self):
        """Verifies loopback, RFC 1918, link-local, cloud metadata, and IPv4-mapped IPv6 are blocked."""
        prohibited_targets = [
            "127.0.0.1:443",
            "localhost:443",
            "10.0.0.1:443",
            "192.168.1.1:443",
            "172.16.0.1:443",
            "169.254.169.254:80",
            "[::1]:443",
            "[::ffff:127.0.0.1]:80",
        ]
        for target in prohibited_targets:
            req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("latin1")
            resp = self._send_raw_proxy_request(req)
            self.assertIn(b"403 Forbidden", resp, f"Target {target} was not blocked!")

    def test_port_allowlist_and_prohibited_ports(self):
        """Verifies non-whitelisted ports (SMTP 25, SSH 22) and disallowed ports (20129, 9222) are rejected."""
        test_ports = [
            (25, "SMTP port 25 must be blocked"),
            (22, "SSH port 22 must be blocked"),
            (20129, "Jarvis UI control port must be blocked"),
            (9222, "CDP port must be blocked"),
            (11434, "Ollama port must be blocked"),
        ]
        for port, msg in test_ports:
            req = f"CONNECT 93.184.216.34:{port} HTTP/1.1\r\nHost: 93.184.216.34:{port}\r\n\r\n".encode("latin1")
            resp = self._send_raw_proxy_request(req)
            self.assertIn(b"403 Forbidden", resp, msg)

    def test_malformed_input_rejected_without_sanitization(self):
        """Verifies embedded user-info, null bytes, CRLF, and ambiguous authorities return 400 Bad Request."""
        malformed_requests = [
            # Embedded user-info credentials (@)
            b"CONNECT admin:secret@example.com:443 HTTP/1.1\r\n\r\n",
            # Null bytes in request line
            b"CONNECT example.com\x00:443 HTTP/1.1\r\n\r\n",
            # Missing port in CONNECT
            b"CONNECT example.com HTTP/1.1\r\n\r\n",
            # Negative / invalid port
            b"CONNECT example.com:999999 HTTP/1.1\r\n\r\n",
            # Malformed IPv6 bracket
            b"CONNECT [2001:db8::1:443 HTTP/1.1\r\n\r\n",
            # Path in CONNECT target
            b"CONNECT example.com:443/secret HTTP/1.1\r\n\r\n",
            # Malformed Host headers
            b"GET http://example.com/ HTTP/1.1\r\nHost: user@example.com\r\n\r\n",
            b"GET http://example.com/ HTTP/1.1\r\nHost: example.com:notaport\r\n\r\n",
            b"GET http://example.com/ HTTP/1.1\r\nHost: [2001:db8::1\r\n\r\n",
            b"GET http://example.com/ HTTP/1.1\r\nHost: example.com:99999\r\n\r\n",
            # Conflicting Host headers
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: other.com:443\r\nHost: third.com:443\r\n\r\n",
            # Disallowed scheme in plain HTTP forwarding
            b"GET ftp://example.com/file.txt HTTP/1.1\r\nHost: example.com\r\n\r\n",
        ]
        for req in malformed_requests:
            resp = self._send_raw_proxy_request(req)
            self.assertIn(b"400 Bad Request", resp)

    def test_request_header_size_limit_fail_closed(self):
        """Verifies headers exceeding MAX_HEADER_BYTES (32KB) fail closed with 400 or 431."""
        oversized_headers = (
            b"GET http://example.com/ HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            + (b"X-Junk: " + b"A" * 128 + b"\r\n") * 260
            + b"\r\n\r\n"
        )
        resp = self._send_raw_proxy_request(oversized_headers)
        self.assertTrue(b"400 Bad Request" in resp or b"431" in resp)

    def test_dns_resolution_failure_returns_502(self):
        """Verifies unresolvable host returns 502 Bad Gateway."""
        orig_gai = socket.getaddrinfo

        def mock_gai(host, port, *args, **kwargs):
            if host in ("127.0.0.1", "localhost", "::1"):
                return orig_gai(host, port, *args, **kwargs)
            raise socket.gaierror(-2, "Name or service not known")

        with patch("socket.getaddrinfo", side_effect=mock_gai):
            req = b"CONNECT nonexistent-domain-xyz123.com:443 HTTP/1.1\r\nHost: nonexistent-domain-xyz123.com:443\r\n\r\n"
            resp = self._send_raw_proxy_request(req)
            self.assertIn(b"502 Bad Gateway", resp)
            self.assertIn(b"DNS resolution failed", resp)

    def test_upstream_connect_timeout_returns_504(self):
        """Verifies upstream connect timeout returns 504 Gateway Timeout."""
        with patch("jarvis.skills.egress_proxy.resolve_and_validate_host_port", return_value=("93.184.216.34", socket.AF_INET, 443)), \
             patch.object(self.proxy, "_create_outbound_connection", side_effect=TimeoutError("timed out")):

            req = b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
            resp = self._send_raw_proxy_request(req)
            self.assertIn(b"504 Gateway Timeout", resp)


class TestEgressProxyProtocolsAndSplice(unittest.TestCase):
    """Verifies end-to-end data transfer, HTTP methods, body forwarding, and WebSocket upgrade using local echo sockets."""

    def test_connect_tunnel_bidirectional_splicing(self):
        """Spins up a local TCP echo server and verifies CONNECT tunnel splices bidirectional payload accurately."""
        # 1. Local echo server on ephemeral port
        echo_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo_sock.bind(("127.0.0.1", 0))
        echo_sock.listen(1)
        echo_port = echo_sock.getsockname()[1]

        def echo_worker():
            try:
                conn, _ = echo_sock.accept()
                with conn:
                    data = conn.recv(1024)
                    if data:
                        conn.sendall(b"ECHO:" + data)
            except OSError:
                pass
            finally:
                echo_sock.close()

        echo_thread = threading.Thread(target=echo_worker, daemon=True)
        echo_thread.start()

        # 2. Test proxy configured with isolated test validator allowing this specific echo server
        def test_validator(host: str, port: int) -> tuple[str, int, int]:
            if port == echo_port:
                return "127.0.0.1", socket.AF_INET, echo_port
            raise SecurityBoundaryViolation("Blocked")

        proxy = EgressProxyServer(
            host="127.0.0.1",
            port=0,
            destination_validator=test_validator,
        )
        proxy_port = proxy.start()

        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as client:
                # Issue CONNECT
                client.sendall(f"CONNECT target.test:{echo_port} HTTP/1.1\r\nHost: target.test:{echo_port}\r\n\r\n".encode("latin1"))

                # Read 200 response
                resp = client.recv(1024)
                self.assertIn(b"200 Connection Established", resp)

                # Send raw payload through the established tunnel
                client.sendall(b"HelloSecureWorld")

                # Receive echo response
                echo_reply = client.recv(1024)
                self.assertEqual(echo_reply, b"ECHO:HelloSecureWorld")
        finally:
            proxy.stop()
            echo_thread.join(timeout=1.0)

    def test_http_forwarding_methods_and_post_body(self):
        """Verifies HTTP forwarding for POST with body and GET with response streaming."""
        # 1. Local HTTP server on ephemeral port
        received_data = {}

        class DummyServer:
            def __init__(self):
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.bind(("127.0.0.1", 0))
                self.sock.listen(1)
                self.port = self.sock.getsockname()[1]
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

            def _run(self):
                try:
                    conn, _ = self.sock.accept()
                    with conn:
                        buf = bytearray()
                        while b"\r\n\r\n" not in buf:
                            c = conn.recv(1024)
                            if not c:
                                break
                            buf.extend(c)
                        header_part, _, body_part = bytes(buf).partition(b"\r\n\r\n")
                        req_str = header_part.decode("latin1")
                        lines = req_str.splitlines()
                        received_data["request_line"] = lines[0]

                        # Read body if Content-Length present
                        cl = 0
                        for line in lines[1:]:
                            if line.lower().startswith("content-length:"):
                                cl = int(line.split(":", 1)[1].strip())
                        body = bytearray(body_part)
                        while len(body) < cl:
                            chunk = conn.recv(cl - len(body))
                            if not chunk:
                                break
                            body.extend(chunk)
                        received_data["body"] = bytes(body)

                        # Respond with HTTP 200 and echo body
                        resp = (
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: text/plain\r\n"
                            b"Content-Length: " + str(len(received_data["body"])).encode("latin1") + b"\r\n"
                            b"Connection: close\r\n\r\n" + received_data["body"]
                        )
                        conn.sendall(resp)
                except OSError:
                    pass
                finally:
                    self.sock.close()

        server = DummyServer()

        def test_val(host: str, port: int) -> tuple[str, int, int]:
            if port == server.port:
                return "127.0.0.1", socket.AF_INET, server.port
            raise SecurityBoundaryViolation("Blocked")

        proxy = EgressProxyServer(host="127.0.0.1", port=0, destination_validator=test_val)
        proxy_port = proxy.start()

        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as client:
                body_payload = b"payload_field=jarvis_secure_test_123"
                post_req = (
                    f"POST http://target.test:{server.port}/api/submit HTTP/1.1\r\n"
                    f"Host: target.test:{server.port}\r\n"
                    f"Content-Length: {len(body_payload)}\r\n"
                    "Content-Type: application/x-www-form-urlencoded\r\n\r\n"
                ).encode("latin1") + body_payload

                client.sendall(post_req)

                resp = bytearray()
                while True:
                    c = client.recv(1024)
                    if not c:
                        break
                    resp.extend(c)

                self.assertIn(b"200 OK", resp)
                self.assertIn(b"payload_field=jarvis_secure_test_123", resp)
                # Upstream must have received origin-form request line
                self.assertEqual(received_data["request_line"], "POST /api/submit HTTP/1.1")
                self.assertEqual(received_data["body"], body_payload)
        finally:
            proxy.stop()
            server.thread.join(timeout=1.0)

    def test_rfc9112_absolute_uri_authority_replaces_mismatched_host_header(self):
        """Verifies RFC 9112 Section 3.2.1 compliance: When request target is absolute-form URI,
        proxy forwards using target URI authority and replaces forwarded Host header with target authority.
        """
        received_data: dict[str, Any] = {}

        class InspectServer:
            def __init__(self):
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.bind(("127.0.0.1", 0))
                self.sock.listen(1)
                self.port = self.sock.getsockname()[1]
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

            def _run(self):
                try:
                    conn, _ = self.sock.accept()
                    with conn:
                        buf = bytearray()
                        while b"\r\n\r\n" not in buf:
                            c = conn.recv(1024)
                            if not c:
                                break
                            buf.extend(c)
                        header_part, _, _ = bytes(buf).partition(b"\r\n\r\n")
                        req_str = header_part.decode("latin1")
                        lines = req_str.splitlines()
                        received_data["request_line"] = lines[0]
                        headers: dict[str, str] = {}
                        for line in lines[1:]:
                            if ":" in line:
                                k, v = line.split(":", 1)
                                headers[k.strip().lower()] = v.strip()
                        received_data["headers"] = headers

                        resp = (
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: text/plain\r\n"
                            b"Content-Length: 2\r\n"
                            b"Connection: close\r\n\r\nOK"
                        )
                        conn.sendall(resp)
                except OSError:
                    pass
                finally:
                    self.sock.close()

        server = InspectServer()

        # Validator verifies connection is routed strictly to target authority
        def test_val(host: str, port: int) -> tuple[str, int, int]:
            if host == "authoritative-target.com" and port == server.port:
                return "127.0.0.1", socket.AF_INET, server.port
            raise SecurityBoundaryViolation(f"Unexpected destination: {host}:{port}")

        proxy = EgressProxyServer(host="127.0.0.1", port=0, destination_validator=test_val)
        proxy_port = proxy.start()

        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as client:
                # Absolute-form URI target specifies authoritative-target.com:port
                # Host header specifies a mismatched / spoofed host
                req = (
                    f"GET http://authoritative-target.com:{server.port}/resource/check HTTP/1.1\r\n"
                    f"Host: spoofed-mismatched-domain.com:8443\r\n"
                    f"User-Agent: DeterministicRFCClient/1.0\r\n\r\n"
                ).encode("latin1")
                client.sendall(req)

                resp = bytearray()
                while True:
                    c = client.recv(1024)
                    if not c:
                        break
                    resp.extend(c)

                self.assertIn(b"200 OK", resp)
                # Upstream must receive the path from the target URI
                self.assertEqual(received_data["request_line"], "GET /resource/check HTTP/1.1")
                # Forwarded Host header MUST be replaced with target URI authority per RFC 9112
                expected_host = f"authoritative-target.com:{server.port}"
                self.assertEqual(received_data["headers"].get("host"), expected_host)
        finally:
            proxy.stop()
            server.thread.join(timeout=1.0)


    def test_websocket_upgrade_transition_to_splice(self):
        """Verifies plain HTTP ws:// Upgrade request (status 101) transitions to raw bidirectional stream."""
        echo_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo_sock.bind(("127.0.0.1", 0))
        echo_sock.listen(1)
        echo_port = echo_sock.getsockname()[1]

        def ws_upstream_worker():
            try:
                conn, _ = echo_sock.accept()
                with conn:
                    # Read HTTP upgrade request
                    _req = conn.recv(2048)
                    # Send 101 Switching Protocols
                    conn.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\n"
                        b"Connection: Upgrade\r\n"
                        b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
                    )
                    # Now echo raw frames
                    frame = conn.recv(1024)
                    if frame:
                        conn.sendall(b"WS_ECHO:" + frame)
            except OSError:
                pass
            finally:
                echo_sock.close()

        ws_thread = threading.Thread(target=ws_upstream_worker, daemon=True)
        ws_thread.start()

        def test_val(host: str, port: int) -> tuple[str, int, int]:
            if port == echo_port:
                return "127.0.0.1", socket.AF_INET, echo_port
            raise SecurityBoundaryViolation("Blocked")

        proxy = EgressProxyServer(host="127.0.0.1", port=0, destination_validator=test_val)
        proxy_port = proxy.start()

        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as client:
                ws_req = (
                    f"GET http://target.test:{echo_port}/chat HTTP/1.1\r\n"
                    f"Host: target.test:{echo_port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("latin1")
                client.sendall(ws_req)

                # Receive 101 Switching Protocols
                header_resp = client.recv(1024)
                self.assertIn(b"101 Switching Protocols", header_resp)
                self.assertIn(b"Upgrade: websocket", header_resp)

                # Send raw payload post-upgrade
                client.sendall(b"FramePayloadData")
                reply = client.recv(1024)
                self.assertEqual(reply, b"WS_ECHO:FramePayloadData")
        finally:
            proxy.stop()
            ws_thread.join(timeout=1.0)


class TestManagedBrowserProxyIntegration(unittest.TestCase):
    """Verifies proxy integration into ManagedBrowserSession lifecycle and command line flags."""

    def test_managed_browser_cmdline_includes_proxy_and_egress_suppression_flags(self):
        """Verifies --proxy-server, --proxy-bypass-list=<-loopback>, and UDP/QUIC/WebRTC suppression flags are set."""
        from jarvis.skills.browser_cdp import ManagedBrowserSession

        session = ManagedBrowserSession(browser_name="brave")
        captured_cmd = []

        def fake_spawn(cmd):
            captured_cmd.extend(cmd)
            m = MagicMock()
            m.is_alive.return_value = True
            m.pid = 9999
            return m

        with patch("jarvis.skills.browser_cdp.resolve_browser_executable", return_value=r"C:\fake\brave.exe"), \
             patch("jarvis.skills.job_object_sandbox.spawn_contained_process", side_effect=fake_spawn), \
             patch.object(session, "_poll_devtools_active_port", return_value=(59999, "/ws", None)), \
             patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True):

            ok, _ = session.start(timeout_sec=1.0)
            try:
                self.assertTrue(ok)
                self.assertIsNotNone(session.proxy)
                self.assertTrue(session.proxy.is_alive())
                proxy_port = session.proxy.server_port

                # Check all mandatory flags in captured command line
                self.assertIn(f"--proxy-server=http://127.0.0.1:{proxy_port}", captured_cmd)
                self.assertIn("--proxy-bypass-list=<-loopback>", captured_cmd)
                self.assertIn("--disable-quic", captured_cmd)
                self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", captured_cmd)
                self.assertIn("--dns-prefetch-disable", captured_cmd)
                self.assertIn("--disable-features=AsyncDns", captured_cmd)
            finally:
                session.stop()
                self.assertFalse(session.is_alive())
                self.assertIsNone(session.proxy)

    def test_proxy_startup_failure_prevents_browser_launch(self):
        """Verifies that if proxy fails to bind/start, Chromium is never spawned (fails closed)."""
        from jarvis.skills.browser_cdp import ManagedBrowserSession

        session = ManagedBrowserSession(browser_name="brave")

        with patch("jarvis.skills.browser_cdp.resolve_browser_executable", return_value=r"C:\fake\brave.exe"), \
             patch("jarvis.skills.egress_proxy.EgressProxyServer.start", side_effect=OSError("Address already in use")), \
             patch("jarvis.skills.job_object_sandbox.spawn_contained_process") as mock_spawn:

            ok, msg = session.start(timeout_sec=1.0)
            self.assertFalse(ok)
            self.assertIn("Failed to start local egress proxy", msg)
            # Browser process MUST NEVER have been spawned
            mock_spawn.assert_not_called()

    def test_proxy_failure_during_run_invalidates_session(self):
        """Verifies that if proxy crashes during a session, session.is_alive() returns False."""
        from jarvis.skills.browser_cdp import ManagedBrowserSession

        session = ManagedBrowserSession(browser_name="brave")

        mock_proc = MagicMock()
        mock_proc.is_alive.return_value = True

        with patch("jarvis.skills.browser_cdp.resolve_browser_executable", return_value=r"C:\fake\brave.exe"), \
             patch("jarvis.skills.job_object_sandbox.spawn_contained_process", return_value=mock_proc), \
             patch.object(session, "_poll_devtools_active_port", return_value=(59999, "/ws", None)), \
             patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True):

            ok, _ = session.start(timeout_sec=1.0)
            try:
                self.assertTrue(ok)
                self.assertTrue(session.is_alive())

                # Simulate proxy failure / stop
                session.proxy.stop()
                self.assertFalse(session.proxy.is_alive())

                # session.is_alive() must now return False fail-closed
                self.assertFalse(session.is_alive())
            finally:
                session.stop()


if __name__ == "__main__":
    unittest.main()
