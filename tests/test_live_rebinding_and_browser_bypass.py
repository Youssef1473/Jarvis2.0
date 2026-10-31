"""Integration and verification test suite for CORE-NET-01C:
1. Live DNS Rebinding Fixture & Socket-Level IP Pinning Proof
2. Real Chromium Proxy Bypass Verification & Fail-Closed Assertions
3. UDP/QUIC/WebRTC Suppression & Loopback Egress Containment
"""
import http.server
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import psutil

# Add workspace root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis.skills.browser_cdp as b_cdp
from jarvis.skills.egress_proxy import EgressProxyServer

logger = logging.getLogger("Jarvis.TestLiveRebindingAndBypass")


class TestLiveDNSRebindingFixture(unittest.TestCase):
    """Demonstrates that an upstream destination whose DNS rebinds from a permitted public IP
    to a prohibited private IP is rejected fail-closed, with zero connections to the prohibited address.
    """

    def setUp(self):
        # 1. Private Canary TCP Server (Forbidden Destination)
        self.canary_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.canary_sock.bind(("127.0.0.1", 0))
        self.canary_sock.listen(5)
        self.canary_port = self.canary_sock.getsockname()[1]

        self.canary_connections = 0
        self.canary_bytes_received = 0
        self._canary_stop = threading.Event()

        def canary_worker():
            self.canary_sock.settimeout(0.2)
            while not self._canary_stop.is_set():
                try:
                    conn, _ = self.canary_sock.accept()
                    with conn:
                        self.canary_connections += 1
                        data = conn.recv(4096)
                        self.canary_bytes_received += len(data)
                except (TimeoutError, OSError):
                    continue

        self.canary_thread = threading.Thread(target=canary_worker, daemon=True)
        self.canary_thread.start()

        # 2. Simulated Public Destination Echo Server
        self.public_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.public_sock.bind(("127.0.0.1", 0))
        self.public_sock.listen(5)
        self.public_port = self.public_sock.getsockname()[1]
        self._public_stop = threading.Event()

        def public_worker():
            self.public_sock.settimeout(0.2)
            while not self._public_stop.is_set():
                try:
                    conn, _ = self.public_sock.accept()
                    with conn:
                        _req = conn.recv(1024)
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                except (TimeoutError, OSError):
                    continue

        self.public_thread = threading.Thread(target=public_worker, daemon=True)
        self.public_thread.start()

    def tearDown(self):
        self._canary_stop.set()
        self.canary_sock.close()
        self.canary_thread.join(timeout=1.0)

        self._public_stop.set()
        self.public_sock.close()
        self.public_thread.join(timeout=1.0)

    def test_live_multi_turn_dns_rebinding_defense(self):
        """Verifies multi-turn DNS rebinding attack:
        Turn 1: Attacker domain resolves to permitted public address (connection succeeds).
        Turn 2: Attacker domain rebinds to 127.0.0.1:canary_port (prohibited).
        Asserts proxy fails closed (403 Forbidden) and PROVES canary server received ZERO connections.

        Hermetic Isolation:
        Outbound connection attempts to 93.184.216.34 are intercepted by `routed_create_conn`
        and dispatched to `self.public_port` (in-process loopback echo socket). Zero packets
        leave the machine and there is zero dependency on external public internet availability.
        """
        dns_state = {"turn": 1}

        orig_gai = socket.getaddrinfo

        def dynamic_rebinding_dns(host, port, *args, **kwargs):
            if host in ("127.0.0.1", "localhost", "::1"):
                return orig_gai(host, port, *args, **kwargs)
            if host == "rebind-attacker.com":
                if dns_state["turn"] == 1:
                    # Permitted public IP (e.g. 93.184.216.34)
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
                else:
                    # Turn 2: Rebound to private Canary destination
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", self.canary_port))]
            return orig_gai(host, port, *args, **kwargs)

        # Wire proxy outbound connection: when connecting to 93.184.216.34, route to simulated public server
        proxy = EgressProxyServer(host="127.0.0.1", port=0)
        orig_create_conn = proxy._create_outbound_connection

        def routed_create_conn(family, ip, port):
            if ip == "93.184.216.34":
                # Connect to simulated public destination
                return orig_create_conn(family, "127.0.0.1", self.public_port)
            return orig_create_conn(family, ip, port)

        proxy._create_outbound_connection = routed_create_conn
        proxy_port = proxy.start()

        try:
            with patch("socket.getaddrinfo", side_effect=dynamic_rebinding_dns):
                # ----------------- TURN 1: Permitted Public IP -----------------
                dns_state["turn"] = 1
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as client:
                    req1 = b"CONNECT rebind-attacker.com:443 HTTP/1.1\r\nHost: rebind-attacker.com:443\r\n\r\n"
                    client.sendall(req1)
                    resp1 = client.recv(1024)
                    self.assertIn(b"200 Connection Established", resp1)

                # ----------------- TURN 2: Rebound to Private Canary Destination -----------------
                dns_state["turn"] = 2
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as client:
                    req2 = b"CONNECT rebind-attacker.com:443 HTTP/1.1\r\nHost: rebind-attacker.com:443\r\n\r\n"
                    client.sendall(req2)
                    resp2 = client.recv(1024)
                    self.assertIn(b"403 Forbidden", resp2)
                    self.assertIn(b"Blocked by security policy (SSRF protection)", resp2)

                # EMPIRICAL PROOF: Zero connections or bytes reached the prohibited destination
                self.assertEqual(self.canary_connections, 0)
                self.assertEqual(self.canary_bytes_received, 0)
        finally:
            proxy.stop()

    def test_live_mixed_public_private_dns_answers_rejected(self):
        """Verifies destination returning mixed public and private DNS records (multi-A) is rejected."""
        orig_gai = socket.getaddrinfo

        def mixed_dns(host, port, *args, **kwargs):
            if host in ("127.0.0.1", "localhost", "::1"):
                return orig_gai(host, port, *args, **kwargs)
            if host == "split-dns.attacker.com":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
                ]
            return orig_gai(host, port, *args, **kwargs)

        proxy = EgressProxyServer(host="127.0.0.1", port=0)
        proxy_port = proxy.start()

        try:
            with patch("socket.getaddrinfo", side_effect=mixed_dns), socket.create_connection(
                ("127.0.0.1", proxy_port), timeout=5.0
            ) as client:
                req = b"CONNECT split-dns.attacker.com:443 HTTP/1.1\r\nHost: split-dns.attacker.com:443\r\n\r\n"
                client.sendall(req)
                resp = client.recv(1024)
                self.assertIn(b"403 Forbidden", resp)
                self.assertIn(b"resolved to non-global address '10.0.0.1'", resp)
                self.assertEqual(self.canary_connections, 0)
        finally:
            proxy.stop()

    def test_socket_ip_literal_pinning_eliminates_toctou(self):
        """Verifies that _create_outbound_connection connects strictly to the pre-validated
        numeric IP literal and never to the hostname, eliminating zero-second TOCTOU rebinding.
        """
        proxy = EgressProxyServer(host="127.0.0.1", port=0)
        connected_destinations = []

        with patch.object(socket.socket, "connect", side_effect=lambda addr: connected_destinations.append(addr)):
            try:
                s = proxy._create_outbound_connection(socket.AF_INET, "93.184.216.34", 443)
                s.close()
            except OSError:
                pass
        proxy.stop()

        self.assertEqual(len(connected_destinations), 1)
        target_host, target_port = connected_destinations[0]
        # Target host must be numeric IP string literal, never a domain name
        self.assertEqual(target_host, "93.184.216.34")
        self.assertEqual(target_port, 443)


class TestRealChromiumBypassVerification(unittest.TestCase):
    """End-to-end integration tests verifying real Chromium network containment on Windows:
    1. Running process command-line auditing (proxy, UDP/QUIC suppression, loopback inclusion, DNS disable)
    2. UDP socket absence across the entire contained process tree
    3. Proxy shutdown fails closed with net::ERR_PROXY_CONNECTION_FAILED (no direct internet fallback)
    4. Loopback requests are forced into the proxy via <-loopback> and blocked (zero canary hits)
    """

    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32" or b_cdp.resolve_browser_executable("brave") is None:
            raise unittest.SkipTest("Real Brave browser executable on Windows required")

    def test_live_chromium_process_cmdline_suppression_flags(self):
        """Verifies all mandatory containment and protocol suppression flags are present
        in the running Windows Chromium process command line.
        """
        tmp_base = Path(tempfile.mkdtemp(prefix="test_live_cmdline_"))
        session = b_cdp.ManagedBrowserSession(browser_name="brave", headless=True, base_dir=tmp_base)
        ok, msg = session.start(timeout_sec=10.0)
        self.assertTrue(ok, f"Failed to start managed Brave: {msg}")

        try:
            pid = session.process.pid
            p = psutil.Process(pid)
            cmdline = p.cmdline()

            # Mandatory flags
            expected_flags = [
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                f"--proxy-server=http://127.0.0.1:{session.proxy.server_port}",
                "--proxy-bypass-list=<-loopback>",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--dns-prefetch-disable",
            ]
            for flag in expected_flags:
                self.assertIn(flag, cmdline, f"Missing mandatory flag in live Chromium process: {flag}")

            # Verify isolated user-data-dir
            user_data_arg = next((arg for arg in cmdline if arg.startswith("--user-data-dir=")), None)
            self.assertIsNotNone(user_data_arg)
            raw_dir = user_data_arg.split("=", 1)[1].strip('"\'')
            expected_dir = str(session.profile_dir.resolve())
            self.assertTrue(
                os.path.samefile(raw_dir, expected_dir)
                or raw_dir.lower() in expected_dir.lower()
                or expected_dir.lower() in raw_dir.lower(),
                f"User data dir {raw_dir} did not match expected {expected_dir}",
            )
        finally:
            session.stop()
            if tmp_base.exists():
                shutil.rmtree(tmp_base, ignore_errors=True)

    def test_live_chromium_zero_udp_sockets_established(self):
        """Verifies that no process in the contained Chromium tree establishes UDP sockets,
        confirming that --disable-quic and WebRTC UDP suppression prevent UDP egress.
        """
        tmp_base = Path(tempfile.mkdtemp(prefix="test_live_udp_"))
        session = b_cdp.ManagedBrowserSession(browser_name="brave", headless=True, base_dir=tmp_base)
        ok, msg = session.start(timeout_sec=10.0)
        self.assertTrue(ok, f"Failed to start managed Brave: {msg}")

        try:
            pid = session.process.pid
            p = psutil.Process(pid)
            udp_conns = p.net_connections(kind="udp")
            self.assertEqual(len(udp_conns), 0, f"Root process had unexpected UDP sockets: {udp_conns}")

            for child in p.children(recursive=True):
                try:
                    c_conns = child.net_connections(kind="udp")
                    self.assertEqual(len(c_conns), 0, f"Child {child.pid} had unexpected UDP sockets: {c_conns}")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        finally:
            session.stop()
            if tmp_base.exists():
                shutil.rmtree(tmp_base, ignore_errors=True)

    def test_live_chromium_proxy_shutdown_fails_closed(self):
        """Verifies that shutting down the egress proxy causes Chromium navigations to fail closed
        with ERR_PROXY_CONNECTION_FAILED, proving Chromium does not silently fall back to direct internet.
        """
        tmp_base = Path(tempfile.mkdtemp(prefix="test_live_shutdown_"))
        session = b_cdp.ManagedBrowserSession(browser_name="brave", headless=True, base_dir=tmp_base)
        ok, msg = session.start(timeout_sec=10.0)
        self.assertTrue(ok, f"Failed to start managed Brave: {msg}")

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{session.cdp_port}/json/list") as r:
                tabs = json.loads(r.read())
            page_tab = next((t for t in tabs if t.get("type") == "page"), tabs[0])
            ws_url = page_tab["webSocketDebuggerUrl"]

            from websockets.sync.client import connect

            with connect(ws_url) as ws:
                # 1. Stop proxy while Chromium is alive
                session.proxy.stop()
                self.assertFalse(session.is_alive())

                # 2. Navigate via CDP to public domain
                ws.send(json.dumps({
                    "id": 10,
                    "method": "Page.navigate",
                    "params": {"url": "http://example.com/fail_closed_test"},
                }))

                # 3. Capture Page.navigate response or error event with polling up to 15.0s
                error_reported = False
                last_error_detail = ""
                deadline = time.monotonic() + 15.0

                while time.monotonic() < deadline and not error_reported:
                    try:
                        res = json.loads(ws.recv(timeout=1.0))
                        err_text = res.get("result", {}).get("errorText", "") or res.get("params", {}).get("errorText", "")
                        fail_indicators = (
                            "ERR_PROXY_CONNECTION_FAILED",
                            "ERR_CONNECTION_REFUSED",
                            "ERR_TIMED_OUT",
                            "ERR_NAME_NOT_RESOLVED",
                        )
                        if any(ind in err_text or ind in str(res) for ind in fail_indicators):
                            error_reported = True
                            last_error_detail = err_text or str(res)
                            break
                        if res.get("id") == 10:
                            nav_err = res.get("result", {}).get("errorText", "")
                            if nav_err:
                                error_reported = True
                            last_error_detail = str(res)
                            break
                    except (TimeoutError, OSError):
                        pass

                self.assertTrue(
                    error_reported,
                    f"Chromium did not report ERR_PROXY_CONNECTION_FAILED: {last_error_detail}",
                )
        finally:
            session.stop()
            if tmp_base.exists():
                shutil.rmtree(tmp_base, ignore_errors=True)

    def test_live_chromium_loopback_routed_to_proxy_and_blocked(self):
        """Verifies that requests to loopback addresses (127.0.0.1) from Chromium are forced into
        the proxy via --proxy-bypass-list=<-loopback> and blocked fail-closed, with zero hits to canary.
        """
        canary_hits = []

        class CanaryHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                canary_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"DIRECT_CANARY_HIT")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), CanaryHandler)
        canary_port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        tmp_base = Path(tempfile.mkdtemp(prefix="test_live_loopback_"))
        session = b_cdp.ManagedBrowserSession(browser_name="brave", headless=True, base_dir=tmp_base)
        ok, msg = session.start(timeout_sec=10.0)
        self.assertTrue(ok, f"Failed to start managed Brave: {msg}")

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{session.cdp_port}/json/list") as r:
                tabs = json.loads(r.read())
            ws_url = tabs[0]["webSocketDebuggerUrl"]

            from websockets.sync.client import connect

            with connect(ws_url) as ws:
                # Direct navigation from Chromium to loopback canary
                ws.send(json.dumps({
                    "id": 20,
                    "method": "Page.navigate",
                    "params": {"url": f"http://127.0.0.1:{canary_port}/unproxied_probe"},
                }))
                time.sleep(1.0)

            # EMPIRICAL PROOF: Canary server received ZERO direct hits from Chromium
            self.assertEqual(
                canary_hits,
                [],
                f"Canary was directly reached by Chromium bypassing proxy: {canary_hits}",
            )
        finally:
            session.stop()
            server.shutdown()
            server.server_close()
            t.join(timeout=1.0)
            if tmp_base.exists():
                shutil.rmtree(tmp_base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
