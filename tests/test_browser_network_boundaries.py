"""Automated tests for browser network boundary hardening (CORE-SEC-04G).

Contains two distinct test suites:
1. TestBrowserNetworkBoundaries (Mock & Parser Suite):
   - Redirect pre-flight validation (bounded hops, loops, HTTPS->HTTP downgrade, SSRF).
   - Localhost port and cloud metadata shielding across IP representations.
   - CDP URL blocklist pattern generation.
   - Fail-closed behavior on guard initialization failure.
   - Centralized policy coverage for execute_browser_javascript without brittle regex scanning.
   - Supplementary pre-flight integration in os_control.open_browser_tab.

2. TestLiveChromiumNetworkGuards (Genuine Chromium Integration Suite):
   - Real browser process spawned with isolated temporary profile on non-standard port.
   - Verifies Chromium accepts Network.enable and Network.setBlockedURLs payload.
   - Verifies native C++ network drop while CDP session is held open.
   - Records empirical disconnect lifecycle behavior (session state reset on detach).
   - Verifies Chromium rejects unauthorized WebSocket Origin headers (403 Forbidden).
   - Verifies Chromium accepts authorized Origin headers and clients without Origin.
   - Verifies _ACTIVE_TAB_SESSIONS persistence keeps network guard active across operations.
"""
import contextlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from websockets.sync.client import connect

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.security_boundaries import SecurityBoundaryViolation
from jarvis.skills import browser_cdp as b_cdp
from jarvis.skills import os_control as os_ctrl


class MockRedirectServer:
    """Lightweight in-process HTTP server for deterministic redirect testing."""

    def __init__(self):
        handler_cls = self._make_handler()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _make_handler(self):
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress test noise

            def do_HEAD(self):
                self._dispatch()

            def do_GET(self):
                self._dispatch()

            def _dispatch(self):
                if self.path == "/to-hud":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:20129/api/shutdown")
                    self.end_headers()
                elif self.path == "/to-metadata":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                    self.end_headers()
                elif self.path == "/to-private-rfc1918":
                    self.send_response(302)
                    self.send_header("Location", "http://192.168.1.1/admin")
                    self.end_headers()
                elif self.path == "/loop-a":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/loop-b")
                    self.end_headers()
                elif self.path == "/loop-b":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/loop-a")
                    self.end_headers()
                elif self.path == "/chain-1":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/chain-2")
                    self.end_headers()
                elif self.path == "/chain-2":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/chain-3")
                    self.end_headers()
                elif self.path == "/chain-3":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/chain-4")
                    self.end_headers()
                elif self.path == "/chain-4":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/chain-5")
                    self.end_headers()
                elif self.path == "/chain-5":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/chain-6")
                    self.end_headers()
                elif self.path == "/chain-6":
                    self.send_response(200)
                    self.end_headers()
                elif self.path == "/safe-hop-1":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{parent.port}/safe-hop-2")
                    self.end_headers()
                elif self.path == "/safe-hop-2":
                    self.send_response(200)
                    self.end_headers()
                elif self.path == "/downgrade":
                    self.send_response(302)
                    self.send_header("Location", "http://example.com/unencrypted")
                    self.end_headers()
                elif self.path == "/head-rejected":
                    # 405 Method Not Allowed on HEAD, succeeds on GET
                    if self.command == "HEAD":
                        self.send_response(405)
                        self.end_headers()
                    else:
                        self.send_response(200)
                        self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

        return Handler

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()


class TestBrowserNetworkBoundaries(unittest.TestCase):
    """MOCK & LOGIC SUITE: Verifies redirect pre-flight, DevTools origin controls, and CDP tab guards."""

    @classmethod
    def setUpClass(cls):
        cls.mock_srv = MockRedirectServer()
        cls.mock_srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.mock_srv.stop()

    def setUp(self):
        self._managed_port_patcher = patch.object(b_cdp, "get_managed_cdp_port", return_value=9222)
        self._managed_port_patcher.start()

    def tearDown(self):
        self._managed_port_patcher.stop()

    def test_redirect_to_protected_localhost_port_rejected(self):
        """Verifies pre-flight detects and blocks redirect hops targeting protected localhost ports."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/to-hud"
        is_valid, _final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url)
        self.assertFalse(is_valid)
        self.assertIn("20129", str(err_msg))

    def test_redirect_to_cloud_metadata_rejected(self):
        """Verifies redirect hop to 169.254.169.254 is rejected."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/to-metadata"
        is_valid, _final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url)
        self.assertFalse(is_valid)
        self.assertIn("169.254.169.254", str(err_msg))

    def test_redirect_to_private_rfc1918_rejected_from_remote(self):
        """Verifies redirect hop to private RFC 1918 is rejected when initial URL is remote."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/to-private-rfc1918"
        is_valid, _final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url)
        self.assertFalse(is_valid)
        self.assertTrue("192.168.1.1" in str(err_msg) or "non-loopback" in str(err_msg))

    def test_https_to_http_redirect_downgrade_rejected(self):
        """Verifies HTTPS to HTTP downgrade is aborted by the redirect validator."""
        validator = b_cdp.NavigationRedirectValidator("https://secure.example.com")
        req = MagicMock()
        req.full_url = "https://secure.example.com/checkout"
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            validator.redirect_request(req, None, 302, "Found", {}, "http://insecure.example.com/checkout")
        self.assertIn("Insecure redirect downgrade from HTTPS to HTTP blocked", str(ctx.exception))

    def test_redirect_loop_detected_and_aborted(self):
        """Verifies cyclical redirects (A -> B -> A) are aborted immediately."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/loop-a"
        is_valid, _final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url)
        self.assertFalse(is_valid)
        self.assertIn("Redirect loop detected", str(err_msg))

    def test_max_hop_limit_exceeded_aborted(self):
        """Verifies redirect chains exceeding maximum 4 hops are terminated."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/chain-1"
        is_valid, _final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url, max_hops=4)
        self.assertFalse(is_valid)
        self.assertIn("maximum 4 hops", str(err_msg))

    def test_alternate_ip_representations_normalized_and_blocked(self):
        """Verifies octal, decimal, hex, and IPv6 loopback variants on blocked ports are rejected."""
        variants = [
            "http://0177.0.0.1:20129",         # Octal 127.0.0.1
            "http://2130706433:20129",         # Decimal integer 127.0.0.1
            "http://[::1]:20129",              # IPv6 loopback
            "http://[::ffff:127.0.0.1]:20129", # IPv4-mapped IPv6
            "http://[::1]:9222",               # IPv6 CDP port
        ]
        for v in variants:
            res = b_cdp.sanitize_browser_url(v)
            self.assertEqual(res, "about:blank", f"Failed to sanitize variant: {v}")

    def test_cdp_blocked_url_patterns_cover_protected_ports(self):
        """Verifies get_cdp_blocked_url_patterns covers all sensitive local ports and cloud metadata."""
        patterns = b_cdp.get_cdp_blocked_url_patterns()
        required_ports = ["20129", "9222", "11434", "2375", "2376", "5432", "6379", "6443"]
        for p in required_ports:
            self.assertTrue(
                any(f":{p}" in pat for pat in patterns),
                f"Missing blocked URL pattern for port {p}"
            )
        self.assertTrue(any("169.254.169.254" in pat for pat in patterns))
        self.assertTrue(any("javascript:*" in pat for pat in patterns))
        self.assertTrue(any("file://*" in pat for pat in patterns))

    def test_cdp_navigation_installs_network_guards(self):
        """Verifies open_or_navigate_tab_cdp configures Network.enable and Network.setBlockedURLs."""
        fake_tab = {
            "id": "tab1",
            "type": "page",
            "title": "Blank",
            "url": "about:blank",
            "ws_url": "ws://127.0.0.1:9222/devtools/page/tab1"
        }
        mock_ws = MagicMock()
        mock_ws_enter = MagicMock()
        mock_ws_enter.__enter__.return_value = mock_ws

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=[fake_tab]), \
             patch("websockets.sync.client.connect", return_value=mock_ws_enter), \
             patch("urllib.request.urlopen"):

            res = b_cdp.open_or_navigate_tab_cdp("https://github.com", new_tab=False)
            self.assertIsNotNone(res)
            self.assertIn("Navigated existing tab", res)

            sent_messages = [json.loads(c[0][0]) for c in mock_ws.send.call_args_list]
            methods = [m.get("method") for m in sent_messages]
            self.assertIn("Network.enable", methods)
            self.assertIn("Network.setBlockedURLs", methods)
            self.assertIn("Page.navigate", methods)

    def test_cdp_new_tab_guard_failure_fails_closed(self):
        """Verifies creating a new tab fails closed if guard initialization encounters error."""
        fake_new_tab_resp = json.dumps({
            "id": "tab_failed_guard",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/tab_failed_guard",
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_new_tab_resp
        mock_resp.__enter__.return_value = mock_resp

        mock_ws = MagicMock()
        mock_ws.recv.return_value = json.dumps({"id": 1001, "error": {"message": "Internal error"}})
        mock_ws_enter = MagicMock()
        mock_ws_enter.__enter__.return_value = mock_ws

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=[]), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("websockets.sync.client.connect", return_value=mock_ws_enter), \
             patch.object(b_cdp, "close_cdp_tab") as mock_close:

            res = b_cdp.open_or_navigate_tab_cdp("https://github.com", new_tab=True)
            self.assertIn("Navigation blocked: Failed to initialize browser network security guard", res)
            mock_close.assert_called_once_with("tab_failed_guard", host="127.0.0.1", port=9222)

    def test_cdp_tab_guard_handles_exception_cleanly(self):
        """Verifies configure_cdp_tab_network_guards returns False cleanly on network exception."""
        failing_ws = MagicMock()
        failing_ws.send.side_effect = ConnectionResetError("Remote host closed WebSocket")
        ok = b_cdp.configure_cdp_tab_network_guards(failing_ws)
        self.assertFalse(ok)

    def test_bootstrap_browser_origins_restricted_no_wildcard(self):
        """Verifies bootstrap_cdp_browser never passes wildcard origin '--remote-allow-origins=*'."""
        with patch.object(b_cdp, "is_cdp_available", side_effect=[False, True]), \
             patch("jarvis.skills.os_control._build_app_index", return_value={"brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"}), \
             patch("subprocess.Popen") as mock_popen, \
             patch("time.sleep"):

            success, _msg = b_cdp.bootstrap_cdp_browser(browser_name="brave", port=9222, dedicated_profile=True)
            self.assertTrue(success)
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]

            self.assertNotIn("--remote-allow-origins=*", args)
            allow_orig_arg = next(a for a in args if a.startswith("--remote-allow-origins="))
            self.assertIn("http://127.0.0.1:20129", allow_orig_arg)
            self.assertIn("http://localhost:20129", allow_orig_arg)
            self.assertIn("chrome-devtools://devtools", allow_orig_arg)

    def test_legitimate_local_dev_redirect_allowed(self):
        """Verifies local developer redirects within permitted ports succeed."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/safe-hop-1"
        is_valid, final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url)
        self.assertTrue(is_valid)
        self.assertIsNone(err_msg)
        self.assertIn("/safe-hop-2", final_url)

    def test_head_method_rejected_falls_back_to_get_cleanly(self):
        """Verifies that servers rejecting HEAD (405) safely fall back to GET without error."""
        target_url = f"http://127.0.0.1:{self.mock_srv.port}/head-rejected"
        is_valid, _final_url, err_msg = b_cdp.validate_browser_navigation_chain(target_url)
        self.assertTrue(is_valid)
        self.assertIsNone(err_msg)

    def test_execute_cdp_javascript_configures_network_guard(self):
        """Verifies execute_cdp_javascript applies tab network guards before script evaluation."""
        fake_tab = {
            "id": "tab_js",
            "type": "page",
            "title": "Calculator",
            "url": "https://calc.org",
            "ws_url": "ws://127.0.0.1:9222/devtools/page/tab_js"
        }
        mock_ws = MagicMock()
        mock_ws.recv.return_value = json.dumps({"id": 202, "result": {"result": {"value": 100}}})
        mock_ws_enter = MagicMock()
        mock_ws_enter.__enter__.return_value = mock_ws

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=[fake_tab]), \
             patch("websockets.sync.client.connect", return_value=mock_ws_enter):

            res = b_cdp.execute_cdp_javascript("dynamic_nav()", target_id_or_title="Calculator")
            self.assertEqual(res, 100)

            sent_messages = [json.loads(c[0][0]) for c in mock_ws.send.call_args_list]
            methods = [m.get("method") for m in sent_messages]
            self.assertIn("Network.enable", methods)
            self.assertIn("Network.setBlockedURLs", methods)
            self.assertIn("Runtime.evaluate", methods)

    def test_os_control_open_browser_tab_blocks_redirect_to_disallowed(self):
        """Verifies open_browser_tab blocks navigation when redirect pre-flight fails."""
        bad_redirect_url = f"http://127.0.0.1:{self.mock_srv.port}/to-hud"
        res = os_ctrl.open_browser_tab(bad_redirect_url, browser="brave")
        self.assertIn("Navigation blocked: Destination URL redirected to disallowed address", res)
        self.assertIn("20129", res)

    def test_existing_tab_guard_failure_aborts_navigation(self):
        """Verifies open_or_navigate_tab_cdp fails closed if guard setup fails on reused tab."""
        fake_tabs = [
            {"id": "tab_blank", "type": "page", "title": "New Tab", "url": "about:blank", "ws_url": "ws://127.0.0.1:9222/devtools/page/tab_blank"}
        ]
        b_cdp.clear_all_tab_sessions()
        try:
            with patch.object(b_cdp, "is_cdp_available", return_value=True), \
                 patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs), \
                 patch.object(b_cdp, "validate_browser_navigation_chain", return_value=(True, "https://example.com", None)), \
                 patch.object(b_cdp, "configure_cdp_tab_network_guards", return_value=False):
                mock_ws = MagicMock()
                mock_ws_enter = MagicMock()
                mock_ws_enter.__enter__.return_value = mock_ws

                with patch("websockets.sync.client.connect", return_value=mock_ws_enter):
                    res = b_cdp.open_or_navigate_tab_cdp("https://example.com", new_tab=False)
                    self.assertIsNotNone(res)
                    self.assertIn("Navigation blocked: Failed to initialize browser network security guard", res)
                    # Page.navigate must NEVER have been sent!
                    sent = [json.loads(c[0][0]) for c in mock_ws.send.call_args_list if c[0]]
                    methods = [m.get("method") for m in sent]
                    self.assertNotIn("Page.navigate", methods)
                    # Registry must not contain an active session for the failed tab
                    self.assertIsNone(b_cdp.get_tab_session("tab_blank"))
        finally:
            b_cdp.clear_all_tab_sessions()

    def test_replaced_session_closed_exactly_once(self):
        """Verifies that registering a replacement session closes the old session exactly once."""
        b_cdp.clear_all_tab_sessions()
        try:
            mock_ws_1 = MagicMock()
            mock_ws_2 = MagicMock()
            sess_1 = b_cdp.CDPTabSession(mock_ws_1, "tab_replace")
            sess_2 = b_cdp.CDPTabSession(mock_ws_2, "tab_replace")

            b_cdp.register_tab_session("tab_replace", sess_1)
            self.assertIs(b_cdp.get_tab_session("tab_replace"), sess_1)
            self.assertEqual(mock_ws_1.close.call_count, 0)

            # Register replacement
            b_cdp.register_tab_session("tab_replace", sess_2)
            self.assertIs(b_cdp.get_tab_session("tab_replace"), sess_2)
            self.assertEqual(mock_ws_1.close.call_count, 1)
            self.assertEqual(mock_ws_2.close.call_count, 0)
        finally:
            b_cdp.clear_all_tab_sessions()

    def test_stale_connection_cleanup_does_not_remove_newer_entry(self):
        """Verifies that removing a session with expected_session identity prevents removing newer replacements."""
        b_cdp.clear_all_tab_sessions()
        try:
            mock_ws_1 = MagicMock()
            mock_ws_2 = MagicMock()
            sess_1 = b_cdp.CDPTabSession(mock_ws_1, "tab_stale")
            sess_2 = b_cdp.CDPTabSession(mock_ws_2, "tab_stale")

            b_cdp.register_tab_session("tab_stale", sess_2)

            # An old cleanup job for sess_1 attempts removal
            removed = b_cdp.remove_tab_session("tab_stale", expected_session=sess_1)
            self.assertIsNone(removed)
            # sess_2 must remain untouched
            self.assertIs(b_cdp.get_tab_session("tab_stale"), sess_2)
            self.assertEqual(mock_ws_2.close.call_count, 0)

            # Removing with matching identity succeeds
            removed_2 = b_cdp.remove_tab_session("tab_stale", expected_session=sess_2)
            self.assertIs(removed_2, sess_2)
            self.assertIsNone(b_cdp.get_tab_session("tab_stale"))
            self.assertEqual(mock_ws_2.close.call_count, 1)
        finally:
            b_cdp.clear_all_tab_sessions()

    def test_concurrent_registry_access(self):
        """Verifies thread-safe registry operations under heavy concurrent register/get/remove load."""
        b_cdp.clear_all_tab_sessions()
        barrier = threading.Barrier(8)
        errors = []

        def worker(w_id):
            try:
                barrier.wait(timeout=2.0)
                tab_id = f"tab_{w_id % 2}"
                for i in range(50):
                    mock_ws = MagicMock()
                    sess = b_cdp.CDPTabSession(mock_ws, tab_id)
                    b_cdp.register_tab_session(tab_id, sess)
                    cur = b_cdp.get_tab_session(tab_id)
                    if cur:
                        _ = cur.is_alive
                    if i % 3 == 0:
                        b_cdp.remove_tab_session(tab_id, expected_session=sess)
            except (RuntimeError, OSError, AssertionError, TimeoutError) as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        self.assertEqual(errors, [])
        b_cdp.clear_all_tab_sessions()
        self.assertEqual(len(b_cdp._ACTIVE_TAB_SESSIONS), 0)

    def test_external_tab_closure_or_browser_restart_cleans_up_retained_sessions(self):
        """Verifies prune_stale_tab_sessions purges sessions for closed tabs and on browser restarts."""
        b_cdp.clear_all_tab_sessions()
        try:
            ws_a = MagicMock()
            ws_b = MagicMock()
            sess_a = b_cdp.CDPTabSession(ws_a, "tab_a")
            sess_b = b_cdp.CDPTabSession(ws_b, "tab_b")
            b_cdp.register_tab_session("tab_a", sess_a)
            b_cdp.register_tab_session("tab_b", sess_b)

            # 1. External tab closure: list_cdp_tabs only sees tab_a
            fake_tabs = [{"id": "tab_a", "type": "page", "title": "A", "url": "https://a.test", "ws_url": ""}]
            with patch.object(b_cdp, "is_cdp_available", return_value=True), \
                 patch("urllib.request.urlopen") as mock_open:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(fake_tabs).encode("utf-8")
                mock_resp.__enter__.return_value = mock_resp
                mock_open.return_value = mock_resp

                tabs = b_cdp.list_cdp_tabs()
                self.assertEqual(len(tabs), 1)
                self.assertIsNotNone(b_cdp.get_tab_session("tab_a"))
                # tab_b was pruned and closed
                self.assertIsNone(b_cdp.get_tab_session("tab_b"))
                self.assertEqual(ws_b.close.call_count, 1)

            # 2. Browser restart: is_cdp_available returns False
            with patch.object(b_cdp, "is_cdp_available", return_value=False):
                tabs2 = b_cdp.list_cdp_tabs()
                self.assertEqual(tabs2, [])
                # All sessions pruned
                self.assertIsNone(b_cdp.get_tab_session("tab_a"))
                self.assertEqual(ws_a.close.call_count, 1)
        finally:
            b_cdp.clear_all_tab_sessions()

    def test_event_draining_does_not_steal_command_responses_or_mix_ids(self):
        """Verifies CDPTabSession background reader drains asynchronous events while correlating command responses."""
        import queue

        class FakeWebSocket:
            def __init__(self):
                self._in_q = queue.Queue()
                self._out_q = queue.Queue()
                self._test_real_reader = True
                self._closed = False

            def send(self, msg):
                data = json.loads(msg)
                cmd_id = data.get("id")
                method = data.get("method")
                self._out_q.put((cmd_id, method))
                # Emit asynchronous network events BEFORE and AFTER the command response
                self._in_q.put(json.dumps({"method": "Network.requestWillBeSent", "params": {"requestId": "123"}}))
                self._in_q.put(json.dumps({"id": cmd_id, "result": {"success": True, "method": method}}))
                self._in_q.put(json.dumps({"method": "Network.responseReceived", "params": {"requestId": "123"}}))

            def recv(self, timeout=0.2):
                if self._closed:
                    raise OSError("WebSocket closed")
                try:
                    return self._in_q.get(timeout=timeout)
                except queue.Empty:
                    raise TimeoutError()

            def close(self):
                self._closed = True

        fake_ws = FakeWebSocket()
        session = b_cdp.CDPTabSession(fake_ws, "tab_event_test")
        try:
            resp1 = session.send_command("Page.navigate", {"url": "https://example.com"})
            self.assertEqual(resp1.get("id"), 1)
            self.assertTrue(resp1.get("result", {}).get("success"))
            self.assertEqual(resp1.get("result", {}).get("method"), "Page.navigate")

            resp2 = session.send_command("Runtime.evaluate", {"expression": "1+1"})
            self.assertEqual(resp2.get("id"), 2)
            self.assertTrue(resp2.get("result", {}).get("success"))
            self.assertEqual(resp2.get("result", {}).get("method"), "Runtime.evaluate")
        finally:
            session.close()

    def test_reader_shutdown_does_not_leak_threads(self):
        """Verifies that CDPTabSession.close() safely terminates background reader thread without leaks."""
        import queue

        class MockSocketForThread:
            def __init__(self):
                self._q = queue.Queue()
                self._test_real_reader = True
                self._closed = False

            def recv(self, timeout=0.2):
                if self._closed:
                    raise OSError("Closed")
                try:
                    return self._q.get(timeout=timeout)
                except queue.Empty:
                    raise TimeoutError()

            def close(self):
                self._closed = True

        ws = MockSocketForThread()
        session = b_cdp.CDPTabSession(ws, "tab_leak_test")
        self.assertIsNotNone(session._reader_thread)
        self.assertTrue(session._reader_thread.is_alive())

        session.close()
        self.assertFalse(session.is_alive)
        self.assertFalse(session._reader_thread.is_alive())


def _find_browser_exe():
    from jarvis.skills.os_control import _build_app_index
    apps = _build_app_index()
    for b in ("brave", "chrome"):
        if apps.get(b) and os.path.exists(apps[b]):
            return apps[b]
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ]
    # Check Playwright-managed headless Chromium before system Edge
    import glob
    playwright_candidates = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win\chrome.exe"))
    candidates.extend(playwright_candidates)
    if apps.get("msedge") and os.path.exists(apps["msedge"]):
        candidates.append(apps["msedge"])
    candidates.append(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"))

    for p in candidates:
        if os.path.exists(p):
            return p
    return None


BROWSER_EXE = _find_browser_exe()
LIVE_CDP_PORT = 9998
TEST_BLOCKED_PORT = 20129


def _recv_response(ws, expected_id, timeout=15.0):
    start = time.time()
    while time.time() - start < timeout:
        remaining = max(0.1, timeout - (time.time() - start))
        try:
            raw = ws.recv(timeout=remaining)
        except (TimeoutError, EOFError):
            break
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                data = json.loads(raw)
                if data.get("id") == expected_id:
                    return data
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                continue
    raise TimeoutError(f"Timed out waiting for response id {expected_id}")


class TestLiveChromiumNetworkGuards(unittest.TestCase):
    """GENUINE CHROMIUM INTEGRATION SUITE: Isolated real browser process on non-standard port 9998."""

    @classmethod
    def setUpClass(cls):
        if not BROWSER_EXE:
            raise unittest.SkipTest("No supported Chromium browser executable located on host.")

        cls.blocked_hits = []
        cls.allowed_hits = []

        class BlockedHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(b"BLOCKED_RESPONSE")))
                self.send_header("Connection", "close")
                self.end_headers()
            def do_GET(self):
                cls.blocked_hits.append(self.path)
                body = b"BLOCKED_RESPONSE"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        class AllowedHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_HEAD(self):
                body = b"<html><body><h1>Allowed Page</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
            def do_GET(self):
                cls.allowed_hits.append(self.path)
                body = b"<html><body><h1>Allowed Page</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        cls.blocked_srv = http.server.ThreadingHTTPServer(("127.0.0.1", TEST_BLOCKED_PORT), BlockedHandler)
        cls.t_blocked = threading.Thread(target=lambda: cls.blocked_srv.serve_forever(poll_interval=0.05), daemon=True)
        cls.t_blocked.start()

        cls.allowed_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AllowedHandler)
        cls.allowed_port = cls.allowed_srv.server_port
        cls.t_allowed = threading.Thread(target=lambda: cls.allowed_srv.serve_forever(poll_interval=0.05), daemon=True)
        cls.t_allowed.start()

        cls.temp_dir = tempfile.mkdtemp(prefix="live_cdp_test_")
        cls.browser_cmd = [
            BROWSER_EXE,
            f"--remote-debugging-port={LIVE_CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://127.0.0.1:20129,http://localhost:20129,chrome-devtools://devtools,devtools://devtools",
            f"--user-data-dir={cls.temp_dir}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ]
        cls.proc = subprocess.Popen(cls.browser_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ready = False
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{LIVE_CDP_PORT}/json/version", timeout=0.2)
                ready = True
                break
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.1)

        if not ready:
            cls.tearDownClass()
            raise RuntimeError(f"Browser failed to initialize CDP on port {LIVE_CDP_PORT}")
        b_cdp._ACTIVE_MANAGED_PORTS.add(LIVE_CDP_PORT)

    @classmethod
    def tearDownClass(cls):
        b_cdp._ACTIVE_MANAGED_PORTS.discard(LIVE_CDP_PORT)
        if hasattr(cls, "proc") and cls.proc:
            try:
                cls.proc.kill()
                cls.proc.wait(timeout=2.0)
            except (subprocess.SubprocessError, OSError):
                pass
        if hasattr(cls, "blocked_srv"):
            with contextlib.suppress(Exception):
                cls.blocked_srv.shutdown()
                cls.blocked_srv.server_close()
        if hasattr(cls, "allowed_srv"):
            with contextlib.suppress(Exception):
                cls.allowed_srv.shutdown()
                cls.allowed_srv.server_close()
        if hasattr(cls, "temp_dir") and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_live_01_real_chromium_accepts_network_guard_payload(self):
        """Verifies real Chromium returns success result for configure_cdp_tab_network_guards payload."""
        req = urllib.request.Request(f"http://127.0.0.1:{LIVE_CDP_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            tab_data = json.loads(resp.read().decode("utf-8"))
        ws_url = tab_data["webSocketDebuggerUrl"]

        with connect(ws_url, open_timeout=5.0) as ws:
            patterns = b_cdp.get_cdp_blocked_url_patterns()
            # 1. Network.enable
            ws.send(json.dumps({"id": 101, "method": "Network.enable", "params": {}}))
            r1 = _recv_response(ws, 101)
            self.assertEqual(r1.get("id"), 101)
            self.assertNotIn("error", r1)

            # 2. Network.setBlockedURLs
            ws.send(json.dumps({"id": 102, "method": "Network.setBlockedURLs", "params": {"urls": patterns}}))
            r2 = _recv_response(ws, 102)
            self.assertEqual(r2.get("id"), 102)
            self.assertNotIn("error", r2, f"Chromium rejected patterns: {r2.get('error')}")

    def test_live_02_held_open_cdp_session_blocks_request_and_permits_allowed(self):
        """Verifies that while CDP connection is held open, requests to blocked port are dropped."""
        req = urllib.request.Request(f"http://127.0.0.1:{LIVE_CDP_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            tab_data = json.loads(resp.read().decode("utf-8"))
        ws_url = tab_data["webSocketDebuggerUrl"]

        with connect(ws_url, open_timeout=5.0) as ws:
            ok = b_cdp.configure_cdp_tab_network_guards(ws)
            self.assertTrue(ok)

            allowed_url = f"http://127.0.0.1:{self.allowed_port}/test_held_open"
            ws.send(json.dumps({"id": 201, "method": "Page.navigate", "params": {"url": allowed_url}}))
            _recv_response(ws, 201)
            time.sleep(0.3)

            # Attempt fetch to blocked port 20129
            fetch_blocked_js = f"""
            (() => {{
                return fetch("http://127.0.0.1:{TEST_BLOCKED_PORT}/probe_held_open", {{mode: 'no-cors'}})
                    .then(() => "FETCH_RESOLVED")
                    .catch(e => "FETCH_REJECTED: " + e.message);
            }})()
            """
            ws.send(json.dumps({
                "id": 202,
                "method": "Runtime.evaluate",
                "params": {"expression": fetch_blocked_js, "awaitPromise": True, "returnByValue": True}
            }))
            r_blocked = _recv_response(ws, 202)
            blocked_val = r_blocked.get("result", {}).get("result", {}).get("value")
            self.assertIn("FETCH_REJECTED", blocked_val)
            self.assertNotIn("/probe_held_open", self.blocked_hits, "Request unexpectedly reached the blocked server!")

            # Attempt fetch to allowed test server
            fetch_allowed_js = f"""
            (() => {{
                return fetch("http://127.0.0.1:{self.allowed_port}/probe_allowed", {{mode: 'no-cors'}})
                    .then(() => "FETCH_RESOLVED")
                    .catch(e => "FETCH_REJECTED: " + e.message);
            }})()
            """
            ws.send(json.dumps({
                "id": 203,
                "method": "Runtime.evaluate",
                "params": {"expression": fetch_allowed_js, "awaitPromise": True, "returnByValue": True}
            }))
            r_allowed = _recv_response(ws, 203)
            allowed_val = r_allowed.get("result", {}).get("result", {}).get("value")
            self.assertEqual("FETCH_RESOLVED", allowed_val)
            self.assertIn("/probe_allowed", self.allowed_hits)

    def test_live_03_cdp_disconnect_lifecycle_behavior(self):
        """Tests whether Network.setBlockedURLs rules persist in Chromium after WebSocket disconnect."""
        req = urllib.request.Request(f"http://127.0.0.1:{LIVE_CDP_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            tab_data = json.loads(resp.read().decode("utf-8"))
        ws_url = tab_data["webSocketDebuggerUrl"]

        with connect(ws_url, open_timeout=5.0) as ws:
            b_cdp.configure_cdp_tab_network_guards(ws)
            allowed_url = f"http://127.0.0.1:{self.allowed_port}/test_disconnect_behavior"
            ws.send(json.dumps({"id": 301, "method": "Page.navigate", "params": {"url": allowed_url}}))
            _recv_response(ws, 301)
            time.sleep(0.3)

            # Schedule an in-page fetch 400ms in the future
            schedule_js = f"""
            setTimeout(() => {{
                fetch("http://127.0.0.1:{TEST_BLOCKED_PORT}/probe_post_disconnect", {{mode: 'no-cors'}});
            }}, 400);
            """
            ws.send(json.dumps({"id": 302, "method": "Runtime.evaluate", "params": {"expression": schedule_js}}))
            _recv_response(ws, 302)

        # Connection is now CLOSED
        # Wait deterministically up to 5.0s for the scheduled in-page fetch to reach the test server
        deadline = time.monotonic() + 5.0
        while "/probe_post_disconnect" not in self.blocked_hits and time.monotonic() < deadline:
            time.sleep(0.05)

        # In Chromium CDP, detaching the DevTools agent resets session domain state (Network.disable).
        # We record the empirical finding:
        post_disconnect_reached = "/probe_post_disconnect" in self.blocked_hits
        # Document the exact observed behavior
        self.assertTrue(
            post_disconnect_reached,
            "Empirical expectation: Detached WebSocket resets session domain state in native Chromium."
        )

    def test_live_04_devtools_origin_restriction_rejects_unauthorized_origin(self):
        """Verifies Chromium rejects WebSocket connections presenting an unauthorized Origin."""
        req = urllib.request.Request(f"http://127.0.0.1:{LIVE_CDP_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            tab_data = json.loads(resp.read().decode("utf-8"))
        ws_url = tab_data["webSocketDebuggerUrl"]

        with self.assertRaises(Exception) as ctx:
            connect(
                ws_url,
                open_timeout=3.0,
                additional_headers={"Origin": "http://evil-attacker.com"},
            )
        err_str = str(ctx.exception).lower()
        self.assertTrue("403" in err_str or "forbidden" in err_str or "handshake" in err_str or "rejected" in err_str)

    def test_live_05_devtools_origin_restriction_accepts_authorized_client(self):
        """Verifies Chromium accepts WebSocket connections from authorized Origin or client without Origin."""
        req = urllib.request.Request(f"http://127.0.0.1:{LIVE_CDP_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            tab_data = json.loads(resp.read().decode("utf-8"))
        ws_url = tab_data["webSocketDebuggerUrl"]

        # Standard Python client without Origin header is accepted
        with connect(ws_url, open_timeout=3.0) as ws:
            ws.send(json.dumps({"id": 501, "method": "Browser.getVersion"}))
            res = json.loads(ws.recv(timeout=5.0))
            self.assertEqual(res.get("id"), 501)

        # Authorized Origin header (Jarvis UI on 20129) is accepted
        with connect(
            ws_url,
            open_timeout=3.0,
            additional_headers={"Origin": "http://127.0.0.1:20129"},
        ) as ws:
            ws.send(json.dumps({"id": 502, "method": "Browser.getVersion"}))
            res = json.loads(ws.recv(timeout=5.0))
            self.assertEqual(res.get("id"), 502)

    def test_live_06_open_or_navigate_tab_cdp_persistent_guard(self):
        """Verifies open_or_navigate_tab_cdp retains active guard in _ACTIVE_TAB_SESSIONS across operations."""
        allowed_url = f"http://127.0.0.1:{self.allowed_port}/test_live_persistent_guard"
        res = b_cdp.open_or_navigate_tab_cdp(allowed_url, new_tab=True, host="127.0.0.1", port=LIVE_CDP_PORT)
        self.assertIsNotNone(res)
        self.assertTrue("Navigated existing tab" in res or "Opened browser tab" in res)

        self.assertTrue(len(b_cdp._ACTIVE_TAB_SESSIONS) > 0)

        eval_res = b_cdp.execute_cdp_javascript(
            f"""
            fetch("http://127.0.0.1:{TEST_BLOCKED_PORT}/probe_from_navigated_tab", {{mode: 'no-cors'}})
                .then(() => "FETCH_RESOLVED")
                .catch(e => "FETCH_REJECTED: " + e.message)
            """,
            host="127.0.0.1",
            port=LIVE_CDP_PORT,
        )
        self.assertIn("FETCH_REJECTED", str(eval_res))
        self.assertNotIn("/probe_from_navigated_tab", self.blocked_hits, "Request reached server on blocked port!")


if __name__ == "__main__":
    unittest.main()
