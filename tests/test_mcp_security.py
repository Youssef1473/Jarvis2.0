"""Security regression tests for MCP HTTP transport, SSRF boundaries, redirect validation,
hop accounting, and DNS rebinding connection pinning.
"""
import concurrent.futures
import io
import json
import os
import socket
import unittest
from unittest.mock import MagicMock, patch
import urllib.parse
import urllib.request

from jarvis.core.security_boundaries import (
    resolve_and_validate_destination,
    SecurityBoundaryViolation,
    validate_egress_url,
    SafeRedirectHandler,
    build_safe_url_opener,
)
from jarvis.core.mcp_manager import MCPClientSession


class FakeSocket:
    """Mock socket that returns predefined HTTP wire bytes."""

    family = socket.AF_INET
    type = socket.SOCK_STREAM
    proto = socket.IPPROTO_TCP

    def __init__(self, raw_bytes: bytes):
        self._file = io.BytesIO(raw_bytes)

    def makefile(self, *args, **kwargs):
        return self._file

    def sendall(self, data: bytes):
        pass

    def fileno(self):
        return -1

    def close(self):
        pass

    def setsockopt(self, *args):
        pass

    def getsockopt(self, level, optname, buflen=None):
        return socket.SOCK_STREAM

    def gettimeout(self):
        return None

    def settimeout(self, timeout):
        pass


class TestInitialDestinationValidation(unittest.TestCase):
    """A. Initial destination validation against SSRF and forbidden address ranges."""

    def test_blocks_ipv4_loopback_and_rfc1918_ranges(self):
        forbidden = [
            "http://127.0.0.1:8000/api",
            "http://127.0.0.2:8000/api",
            "http://127.1.2.3:8000/api",
            "http://10.0.0.1:8000/api",
            "http://10.255.255.254:8000/api",
            "http://172.16.0.1:8000/api",
            "http://172.31.255.255:8000/api",
            "http://192.168.1.1:8000/api",
            "http://192.168.254.254:8000/api",
        ]
        for url in forbidden:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url)

    def test_blocks_ipv6_loopback_unique_local_and_link_local(self):
        forbidden = [
            "http://[::1]:8000/api",
            "http://[fc00::1]:8000/api",
            "http://[fd00::1]:8000/api",
            "http://[fe80::1]:8000/api",
        ]
        for url in forbidden:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url)

    def test_blocks_ipv4_link_local_and_cloud_metadata(self):
        forbidden = [
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.1.1:8080/test",
        ]
        for url in forbidden:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url)

    def test_blocks_unspecified_multicast_reserved_and_cgnat(self):
        forbidden = [
            "http://0.0.0.0:8000/api",
            "http://[::]:8000/api",
            "http://224.0.0.1:8000/api",
            "http://[ff02::1]:8000/api",
            "http://240.0.0.1:8000/api",
            "http://255.255.255.255:8000/api",
            "http://100.64.0.1:8000/api",
        ]
        for url in forbidden:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url)

    def test_blocks_ipv4_mapped_ipv6_representations(self):
        forbidden = [
            "http://[::ffff:127.0.0.1]:8000/api",
            "http://[::ffff:10.0.0.1]:8000/api",
            "http://[::ffff:169.254.169.254]:8000/api",
            "http://[::ffff:192.168.1.1]:8000/api",
        ]
        for url in forbidden:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url)

    def test_blocks_mixed_public_and_forbidden_dns_answers(self):
        # When a hostname resolves to both a public IP and a loopback/private IP,
        # fail-closed validation must reject the destination outright.
        mixed_addr_info = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
        ]
        with patch("socket.getaddrinfo", return_value=mixed_addr_info):
            with self.assertRaises(SecurityBoundaryViolation):
                validate_egress_url("http://mixed.example.com/api")

    def test_rejects_malformed_authorities_and_crlf_null_bytes(self):
        malformed = [
            "http://example.com\r\nHeader: Bad/api",
            "http://example.com\nHeader: Bad/api",
            "http://example.com\x00/api",
            "http://[invalid_bracket/api",
            "ftp://example.com/file",
            "javascript:alert(1)",
            "file:///C:/Windows/win.ini",
            "http://:8080/api",
        ]
        for url in malformed:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url)

    def test_local_mcp_exception_strictly_permits_loopback_only(self):
        # Explicit local-MCP exception permits loopback
        valid_local = [
            "http://127.0.0.1:8000/mcp",
            "http://localhost:8000/mcp",
            "http://[::1]:8000/mcp",
        ]
        for url in valid_local:
            with self.subTest(url=url):
                clean = validate_egress_url(url, allow_local_mcp=True)
                self.assertIn("8000/mcp", clean)

        # But allow_local_mcp MUST NOT permit arbitrary RFC1918 or link-local networks
        forbidden_with_local = [
            "http://192.168.1.100:8000/mcp",
            "http://10.0.0.5:8000/mcp",
            "http://172.16.0.2:8000/mcp",
            "http://169.254.169.254/latest/meta-data/",
        ]
        for url in forbidden_with_local:
            with self.subTest(url=url):
                with self.assertRaises(SecurityBoundaryViolation):
                    validate_egress_url(url, allow_local_mcp=True)


class TestRedirectValidation(unittest.TestCase):
    """B. Hop-by-hop redirect validation, scheme preservation, and sensitive header stripping."""

    def test_redirect_to_loopback_and_private_is_blocked(self):
        targets = [
            "http://127.0.0.1:8080/admin",
            "http://192.168.1.1/router",
            "http://[::1]:9000/status",
            "http://169.254.169.254/meta-data",
        ]
        for target in targets:
            with self.subTest(target=target):
                handler = SafeRedirectHandler(max_hops=3)
                req = urllib.request.Request("https://public.example.com/api")
                with self.assertRaises(SecurityBoundaryViolation):
                    handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_redirect_to_unsupported_scheme_is_blocked(self):
        handler = SafeRedirectHandler(max_hops=3)
        req = urllib.request.Request("https://public.example.com/api")
        with self.assertRaises(SecurityBoundaryViolation):
            handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")

    def test_relative_redirect_is_resolved_and_validated(self):
        handler = SafeRedirectHandler(max_hops=3)
        req = urllib.request.Request("https://public.example.com/v1/mcp")
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            new_req = handler.redirect_request(req, None, 302, "Found", {}, "/v2/mcp")
            self.assertEqual(new_req.full_url, "https://public.example.com/v2/mcp")

    def test_every_redirect_hop_validated_independently(self):
        handler = SafeRedirectHandler(max_hops=4)
        req1 = urllib.request.Request("https://public.example.com/start")
        fake_addr_public = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_public):
            req2 = handler.redirect_request(req1, None, 302, "Found", {}, "https://public2.example.com/hop2")
            self.assertEqual(req2.full_url, "https://public2.example.com/hop2")

        # Second hop redirects to loopback
        with self.assertRaises(SecurityBoundaryViolation):
            handler.redirect_request(req2, None, 302, "Found", {}, "http://127.0.0.1:8080/pwn")

    def test_redirect_hop_limit_enforced(self):
        handler = SafeRedirectHandler(max_hops=2)
        req = urllib.request.Request("https://public.example.com/hop0")
        fake_addr_public = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_public):
            req1 = handler.redirect_request(req, None, 302, "Found", {}, "https://public.example.com/hop1")
            req2 = handler.redirect_request(req1, None, 302, "Found", {}, "https://public.example.com/hop2")
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                handler.redirect_request(req2, None, 302, "Found", {}, "https://public.example.com/hop3")
            self.assertIn("limit exceeded", str(ctx.exception).lower())

    def test_local_mcp_initial_does_not_permit_redirect_to_loopback_or_private(self):
        # Even if initial endpoint was local loopback, redirect handler MUST fail-closed
        handler = SafeRedirectHandler(max_hops=3)
        req = urllib.request.Request("http://127.0.0.1:8000/mcp")
        with self.assertRaises(SecurityBoundaryViolation):
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:9999/other")
        with self.assertRaises(SecurityBoundaryViolation):
            handler.redirect_request(req, None, 302, "Found", {}, "http://192.168.1.1/other")

    def test_redirect_does_not_downgrade_https_to_http(self):
        handler = SafeRedirectHandler(max_hops=3)
        req = urllib.request.Request("https://public.example.com/api")
        fake_addr_public = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))]
        with patch("socket.getaddrinfo", return_value=fake_addr_public):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                handler.redirect_request(req, None, 302, "Found", {}, "http://public.example.com/api")
            self.assertIn("downgrade", str(ctx.exception).lower())

    def test_cross_origin_redirect_strips_sensitive_headers(self):
        handler = SafeRedirectHandler(max_hops=3)
        headers = {
            "Authorization": "Bearer super-secret-mcp-key",
            "Proxy-Authorization": "Basic dXNlcjpwYXNz",
            "Cookie": "session_id=abcdef123456",
            "X-Api-Key": "my-api-key",
            "Accept": "application/json",
        }
        req = urllib.request.Request("https://api.example.com/v1", headers=headers)
        fake_addr_public = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.35", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_public):
            new_req = handler.redirect_request(req, None, 302, "Found", {}, "https://different.example.com/v2")

            # Sensitive auth headers must be stripped on cross-origin redirects
            self.assertNotIn("Authorization", new_req.headers)
            self.assertNotIn("authorization", new_req.headers)
            self.assertNotIn("Proxy-Authorization", new_req.headers)
            self.assertNotIn("Cookie", new_req.headers)
            self.assertNotIn("X-Api-Key", new_req.headers)
            # Safe non-auth headers are retained
            self.assertEqual(new_req.headers.get("Accept"), "application/json")


class TestPerRequestHopAccountingAndConcurrency(unittest.TestCase):
    """C. Per-request hop accounting and thread concurrency isolation."""

    def test_hop_count_is_scoped_per_request_chain(self):
        opener = build_safe_url_opener(max_hops=2)
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

        with patch("socket.getaddrinfo", return_value=fake_addr):
            handler = [h for h in opener.handlers if isinstance(h, SafeRedirectHandler)][0]

            # Request A: 2 hops (under max_hops=2)
            req_a = urllib.request.Request("https://public.example.com/a0")
            req_a1 = handler.redirect_request(req_a, None, 302, "Found", {}, "https://public.example.com/a1")
            req_a2 = handler.redirect_request(req_a1, None, 302, "Found", {}, "https://public.example.com/a2")
            self.assertEqual(req_a2.full_url, "https://public.example.com/a2")

            # Request B on the same opener director: 2 hops.
            # Must succeed and not fail because Request A already consumed 2 hops!
            req_b = urllib.request.Request("https://public.example.com/b0")
            req_b1 = handler.redirect_request(req_b, None, 302, "Found", {}, "https://public.example.com/b1")
            req_b2 = handler.redirect_request(req_b1, None, 302, "Found", {}, "https://public.example.com/b2")
            self.assertEqual(req_b2.full_url, "https://public.example.com/b2")

    def test_concurrent_redirect_chains_isolate_hop_counters(self):
        opener = build_safe_url_opener(max_hops=3)
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        handler = [h for h in opener.handlers if isinstance(h, SafeRedirectHandler)][0]

        def run_chain(chain_id: int):
            req = urllib.request.Request(f"https://public.example.com/chain{chain_id}/0")
            with patch("socket.getaddrinfo", return_value=fake_addr):
                for hop in range(1, 3):
                    req = handler.redirect_request(
                        req, None, 302, "Found", {}, f"https://public.example.com/chain{chain_id}/{hop}"
                    )
            return req.full_url

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_chain, i) for i in range(10)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 10)
        for i, res in enumerate(results):
            self.assertIn(f"/chain{i}/2", res)


class TestDNSRebindingAndConnectionPinning(unittest.TestCase):
    """D. DNS rebinding prevention and socket-level IP connection pinning."""

    def test_socket_connects_to_validated_ip_not_hostname(self):
        """DNS resolution at validation time must pin the socket connection to the validated IP,
        preventing a second DNS lookup from rebinding to loopback/private space.
        """
        opener = build_safe_url_opener()

        # Resolution returns public IP
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))]
        raw_response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 13\r\n\r\n{\"status\":\"ok\"}"

        recorded_connections = []

        def fake_create_connection(address, timeout=None, source_address=None, **kwargs):
            recorded_connections.append(address)
            return FakeSocket(raw_response)

        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            with patch("socket.create_connection", side_effect=fake_create_connection):
                req = urllib.request.Request("http://rebind.example.com/api")
                with opener.open(req) as resp:
                    data = resp.read()
                    self.assertIn(b"ok", data)

        self.assertTrue(len(recorded_connections) >= 1)
        dest_host, dest_port = recorded_connections[0]
        # Crucial security check: The socket MUST connect to the validated IP literal '93.184.216.34'
        # NOT the hostname 'rebind.example.com' (which would trigger unchecked second OS resolution).
        self.assertEqual(dest_host, "93.184.216.34")
        self.assertEqual(dest_port, 80)

    def test_tls_sni_and_hostname_preserved_with_pinned_ip(self):
        """When connecting to a pinned IP for HTTPS, TLS SNI, certificate verification,
        and the HTTP Host header must preserve the original hostname.
        """
        opener = build_safe_url_opener()

        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        raw_response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 13\r\n\r\n{\"status\":\"ok\"}"

        recorded_connections = []
        recorded_sni = []

        def fake_create_connection(address, timeout=None, source_address=None, **kwargs):
            recorded_connections.append(address)
            return FakeSocket(b"raw")

        mock_ssl_sock = FakeSocket(raw_response)
        mock_ctx = MagicMock()

        def fake_wrap_socket(sock, server_hostname=None, **kwargs):
            recorded_sni.append(server_hostname)
            return mock_ssl_sock

        mock_ctx.wrap_socket.side_effect = fake_wrap_socket

        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            with patch("socket.create_connection", side_effect=fake_create_connection):
                with patch("ssl.create_default_context", return_value=mock_ctx), patch(
                    "ssl._create_default_https_context", return_value=mock_ctx
                ), patch("http.client._create_https_context", return_value=mock_ctx):
                    opener = build_safe_url_opener()
                    req = urllib.request.Request("https://secure.example.com/api")
                    with opener.open(req) as resp:
                        data = resp.read()
                        self.assertIn(b"ok", data)

        self.assertTrue(len(recorded_connections) >= 1)
        dest_host, dest_port = recorded_connections[0]
        self.assertEqual(dest_host, "93.184.216.34")
        self.assertEqual(dest_port, 443)
        self.assertIn("secure.example.com", recorded_sni)

    def test_redirect_hop_pins_destination_ip(self):
        """Redirect hops must independently resolve, validate, and pin their destination IP."""
        # Hop 1 redirects to Hop 2
        resp_hop1 = (
            b"HTTP/1.1 302 Found\r\n"
            b"Location: https://second.example.com/data\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        resp_hop2 = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 15\r\n\r\n"
            b"{\"result\":\"done\"}"
        )

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if "first" in host:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
            elif "second" in host:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.35", 443))]
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.36", 443))]

        recorded_connections = []
        responses = [resp_hop1, resp_hop2]

        def fake_create_connection(address, timeout=None, source_address=None, **kwargs):
            recorded_connections.append(address)
            raw = responses.pop(0) if responses else resp_hop2
            return FakeSocket(raw)

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with patch("socket.create_connection", side_effect=fake_create_connection):
                mock_ctx = MagicMock()
                mock_ctx.wrap_socket.side_effect = lambda s, **kw: s
                with patch("ssl.create_default_context", return_value=mock_ctx), patch(
                    "ssl._create_default_https_context", return_value=mock_ctx
                ), patch("http.client._create_https_context", return_value=mock_ctx):
                    opener = build_safe_url_opener(max_hops=2)
                    req = urllib.request.Request("https://first.example.com/start")
                    with opener.open(req) as resp:
                        self.assertEqual(resp.status, 200)

        # Both connections must be made to pinned IP literals
        self.assertEqual(len(recorded_connections), 2)
        self.assertEqual(recorded_connections[0][0], "93.184.216.34")
        self.assertEqual(recorded_connections[1][0], "93.184.216.35")


class TestMCPTransportIntegration(unittest.TestCase):
    """E. Proof of MCPClientSession HTTP transport integration with hardened transport."""

    def test_mcp_client_uses_hardened_opener_not_raw_urlopen(self):
        """MCPClientSession._send_http_request must execute via the safe opener and never
        call unconstrained urllib.request.urlopen.
        """
        session = MCPClientSession(
            name="test_session",
            transport="http",
            endpoint_url="http://127.0.0.1:8000/mcp",
        )

        mock_opener = MagicMock()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener.open.return_value = mock_resp

        with patch("jarvis.core.mcp_manager.build_safe_url_opener", return_value=mock_opener):
            with patch("urllib.request.urlopen") as mock_raw_urlopen:
                res = session._send_http_request({"jsonrpc": "2.0", "id": 1, "method": "test"}, timeout=5.0)
                self.assertIn("result", res)
                self.assertTrue(mock_opener.open.called)
                self.assertFalse(mock_raw_urlopen.called, "urllib.request.urlopen was called as a bypass!")

    def test_mcp_client_rejects_redirect_to_loopback(self):
        """If an external MCP server responds with a redirect to loopback, the MCP client
        must fail closed and abort the request.
        """
        session = MCPClientSession(
            name="external_mcp",
            transport="http",
            endpoint_url="https://public.example.com/mcp",
        )

        # Simulate public host returning redirect to 127.0.0.1
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        resp_302 = (
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://127.0.0.1:8080/pwn\r\n"
            b"Content-Length: 0\r\n\r\n"
        )

        responses = [
            resp_302,
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{\"pwned\":true}",
        ]

        def fake_conn(*args, **kwargs):
            raw = responses.pop(0) if responses else b""
            return FakeSocket(raw)

        with patch("socket.getaddrinfo", return_value=fake_addr):
            with patch("socket.create_connection", side_effect=fake_conn):
                mock_ctx = MagicMock()
                mock_ctx.wrap_socket.side_effect = lambda s, **kw: s
                with patch("ssl.create_default_context", return_value=mock_ctx), patch(
                    "ssl._create_default_https_context", return_value=mock_ctx
                ), patch("http.client._create_https_context", return_value=mock_ctx):
                    with self.assertRaises(Exception) as ctx:
                        session._send_http_request({"jsonrpc": "2.0", "id": 1, "method": "test"}, timeout=5.0)

                    # Error message must indicate security violation / SSRF blockage
                    err_msg = str(ctx.exception)
                    self.assertTrue(
                        "security" in err_msg.lower()
                        or "ssrf" in err_msg.lower()
                        or "blocked" in err_msg.lower()
                        or "non-global" in err_msg.lower(),
                        f"Unexpected error: {err_msg}",
                    )

    def test_implicit_environment_proxies_cannot_bypass_pinning(self):
        """Implicit environment proxies (HTTP_PROXY / HTTPS_PROXY) must not silently intercept
        and bypass the pinned IP / SSRF validation.
        """
        with patch.dict(os.environ, {"HTTP_PROXY": "http://evil-proxy:8080", "HTTPS_PROXY": "http://evil-proxy:8080"}):
            opener = build_safe_url_opener()
            # Ensure no active ProxyHandler with non-empty proxies is in the opener's handlers
            for h in opener.handlers:
                if isinstance(h, urllib.request.ProxyHandler):
                    self.assertEqual(h.proxies, {}, "ProxyHandler in safe opener must have empty proxies!")

    def test_mcp_client_dns_rebinding_to_loopback_blocked(self):
        """SEC-H1-01: When allow_local_mcp=True, a hostname must not be resolved a second
        time allowing DNS rebinding from a validated public IP to 127.0.0.1.
        The request must pin the initially validated IP and connect only to that IP.
        """
        session = MCPClientSession(
            name="rebind_victim",
            transport="http",
            endpoint_url="http://rebind.attacker.com:8000/mcp",
        )

        dns_responses = [
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000))],
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000))],
        ]
        resolved_hosts = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            resolved_hosts.append(host)
            if dns_responses:
                return dns_responses.pop(0)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

        connected_addrs = []

        def fake_create_conn(addr, timeout=None, source_address=None):
            connected_addrs.append(addr)
            return FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}")

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with patch("socket.create_connection", side_effect=fake_create_conn):
                res = session._send_http_request({"jsonrpc": "2.0", "id": 1}, timeout=5.0)

        self.assertEqual(res.get("status"), "ok")
        # DNS must only be resolved once during destination validation
        self.assertEqual(resolved_hosts, ["rebind.attacker.com"], "DNS should only be resolved once!")
        # The socket must connect ONLY to the pre-validated public IP, never loopback
        self.assertTrue(len(connected_addrs) >= 1)
        self.assertEqual(connected_addrs[0], ("93.184.216.34", 8000), "Socket did not connect to pinned IP!")
        for addr in connected_addrs:
            self.assertNotEqual(addr[0], "127.0.0.1", "Rebinding to loopback occurred!")


class TestSafeOpenerSchemes(unittest.TestCase):
    """SEC-H1-02: build_safe_url_opener must not inherit unsafe default handlers (FileHandler, FTPHandler, DataHandler)."""

    def test_safe_opener_blocks_file_scheme_directly(self):
        import tempfile
        from pathlib import Path
        import urllib.error

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("SECRET_PASSWD_DATA")
            tmp_path = Path(tf.name)

        try:
            opener = build_safe_url_opener()
            file_url = tmp_path.as_uri()
            with self.assertRaises(urllib.error.URLError) as ctx:
                opener.open(file_url)
            self.assertIn("unknown url type: file", str(ctx.exception).lower())
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_safe_opener_blocks_data_and_ftp_schemes_directly(self):
        import urllib.error
        opener = build_safe_url_opener()

        with self.assertRaises(urllib.error.URLError) as ctx:
            opener.open("data:text/plain;base64,SGVsbG8sIFdvcmxkIQ==")
        self.assertIn("unknown url type: data", str(ctx.exception).lower())

        with self.assertRaises(urllib.error.URLError) as ctx:
            opener.open("ftp://ftp.example.com/test.txt")
        self.assertIn("unknown url type: ftp", str(ctx.exception).lower())

    def test_safe_opener_handlers_exclude_file_ftp_data(self):
        opener = build_safe_url_opener()
        handler_names = [type(h).__name__ for h in opener.handlers]
        self.assertNotIn("FileHandler", handler_names)
        self.assertNotIn("FTPHandler", handler_names)
        self.assertNotIn("DataHandler", handler_names)


class TestLocalMCPDestinationScope(unittest.TestCase):
    """Verification of local-MCP exception boundary:
    Determines whether allow_local_mcp=True strictly permits only explicitly configured
    local destinations (localhost, IPv4/IPv6 loopback literals) and rejects arbitrary
    hostnames whose DNS resolution returns loopback addresses.
    """

    def test_explicit_ipv4_loopback_permitted_with_local_mcp(self):
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            url, _, pinned_ip = resolve_and_validate_destination("http://127.0.0.1:8000/mcp", allow_local_mcp=True)
            self.assertEqual(pinned_ip, "127.0.0.1")
            self.assertEqual(url, "http://127.0.0.1:8000/mcp")

    def test_explicit_ipv6_loopback_permitted_with_local_mcp(self):
        fake_dns = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 8000, 0, 0))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            url, _, pinned_ip = resolve_and_validate_destination("http://[::1]:8000/mcp", allow_local_mcp=True)
            self.assertEqual(pinned_ip, "::1")
            self.assertEqual(url, "http://[::1]:8000/mcp")

    def test_explicit_localhost_permitted_with_local_mcp(self):
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            url, _, pinned_ip = resolve_and_validate_destination("http://localhost:8000/mcp", allow_local_mcp=True)
            self.assertEqual(pinned_ip, "127.0.0.1")
            self.assertEqual(url, "http://localhost:8000/mcp")

    def test_explicit_localhost_resolving_to_ipv6_loopback_permitted(self):
        fake_dns = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 8000, 0, 0))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            url, _, pinned_ip = resolve_and_validate_destination("http://localhost:8000/mcp", allow_local_mcp=True)
            self.assertEqual(pinned_ip, "::1")
            self.assertEqual(url, "http://localhost:8000/mcp")

    def test_explicit_localhost_resolving_to_public_ipv4_rejected(self):
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://localhost:8000/mcp", allow_local_mcp=True)
            self.assertIn("non-loopback", str(ctx.exception).lower())

    def test_explicit_localhost_resolving_to_public_ipv6_rejected(self):
        fake_dns = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:2800:220:1:248:1893:25c8:1946", 8000, 0, 0))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://localhost:8000/mcp", allow_local_mcp=True)
            self.assertIn("non-loopback", str(ctx.exception).lower())

    def test_explicit_localhost_with_mixed_loopback_first_fails_closed(self):
        fake_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000)),
        ]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://localhost:8000/mcp", allow_local_mcp=True)
            self.assertIn("non-loopback", str(ctx.exception).lower())

    def test_explicit_localhost_with_mixed_public_first_fails_closed(self):
        fake_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000)),
        ]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://localhost:8000/mcp", allow_local_mcp=True)
            self.assertIn("non-loopback", str(ctx.exception).lower())

    def test_mcp_client_session_rejects_localhost_resolving_to_public_ip(self):
        session = MCPClientSession(
            name="localhost_public_mcp",
            transport="http",
            endpoint_url="http://localhost:8000/mcp",
        )
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000))]
        mock_create_conn = MagicMock(side_effect=AssertionError("Socket connection attempted for forbidden destination!"))
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with patch("socket.create_connection", mock_create_conn):
                with self.assertRaises(SecurityBoundaryViolation) as ctx:
                    session._send_http_request({"jsonrpc": "2.0", "id": 1, "method": "test"}, timeout=5.0)
                self.assertIn("non-loopback", str(ctx.exception).lower())
        mock_create_conn.assert_not_called()

    def test_remote_hostname_resolving_to_ipv4_loopback_rejected(self):
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://rebind.attacker.com:8000/mcp", allow_local_mcp=True)
            self.assertTrue(
                "non-global" in str(ctx.exception).lower() or "loopback" in str(ctx.exception).lower(),
                f"Expected SSRF rejection, got: {ctx.exception}",
            )

    def test_remote_hostname_resolving_to_ipv6_loopback_rejected(self):
        fake_dns = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 8000, 0, 0))]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://rebind.attacker.com:8000/mcp", allow_local_mcp=True)
            self.assertTrue(
                "non-global" in str(ctx.exception).lower() or "loopback" in str(ctx.exception).lower(),
                f"Expected SSRF rejection, got: {ctx.exception}",
            )

    def test_remote_hostname_with_mixed_public_and_loopback_fails_closed(self):
        fake_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000)),
        ]
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                resolve_and_validate_destination("http://rebind.attacker.com:8000/mcp", allow_local_mcp=True)
            self.assertTrue(
                "non-global" in str(ctx.exception).lower() or "loopback" in str(ctx.exception).lower(),
                f"Expected fail-closed rejection on mixed answers, got: {ctx.exception}",
            )

    def test_mcp_client_session_rejects_remote_hostname_resolving_to_loopback(self):
        session = MCPClientSession(
            name="rebind_mcp",
            transport="http",
            endpoint_url="http://rebind.attacker.com:8000/mcp",
        )
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8000))]
        mock_create_conn = MagicMock(side_effect=AssertionError("Socket connection attempted for forbidden destination!"))
        with patch("socket.getaddrinfo", return_value=fake_dns):
            with patch("socket.create_connection", mock_create_conn):
                with self.assertRaises(SecurityBoundaryViolation) as ctx:
                    session._send_http_request({"jsonrpc": "2.0", "id": 1, "method": "test"}, timeout=5.0)
                self.assertIn("non-global address '127.0.0.1'", str(ctx.exception))
        # Ensure that no socket connection was ever attempted
        mock_create_conn.assert_not_called()

    def test_connection_pinning_preserves_public_ip_and_avoids_second_resolution(self):
        session = MCPClientSession(
            name="public_mcp",
            transport="http",
            endpoint_url="http://api.example.com:8000/mcp",
        )
        dns_calls = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            dns_calls.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8000))]

        connected_addrs = []

        def fake_create_conn(addr, timeout=None, source_address=None):
            connected_addrs.append(addr)
            return FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}")

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with patch("socket.create_connection", side_effect=fake_create_conn):
                res = session._send_http_request({"jsonrpc": "2.0", "id": 1}, timeout=5.0)

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(dns_calls, [("api.example.com", 8000)], "Only 1 DNS resolution should occur!")
        self.assertEqual(connected_addrs, [("93.184.216.34", 8000)], "Socket must connect strictly to pinned public IP!")


if __name__ == "__main__":
    unittest.main()
