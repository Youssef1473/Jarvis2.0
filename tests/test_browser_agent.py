"""Automated security and regression tests for autonomous browser agent and SSRF egress enforcement."""
import unittest
from unittest.mock import MagicMock, patch

from jarvis.skills import browser_agent


class TestBrowserAgentSSRFSuite(unittest.TestCase):
    """Verifies SSRF boundary enforcement across Playwright navigation, screenshot capture, and HTTP fallback."""

    def test_browse_and_read_webpage_blocks_loopback_destinations(self):
        """Verifies direct requests to loopback IPv4 and IPv6 destinations are rejected immediately."""
        loopback_urls = [
            "http://127.0.0.1:8000/api/status",
            "http://127.0.0.1:20129",
            "http://localhost:3000",
            "http://[::1]:8080",
            "https://127.0.0.1/admin",
        ]
        for url in loopback_urls:
            res = browser_agent.browse_and_read_webpage(url)
            self.assertIn("Security violation blocked browsing operation", res)
            self.assertIn("blocked", res.lower())

    def test_browse_and_read_webpage_blocks_private_and_link_local_networks(self):
        """Verifies RFC 1918 private and cloud metadata link-local destinations are rejected."""
        forbidden_urls = [
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.1/router-login",
            "http://10.0.0.1/internal-dashboard",
            "http://172.16.0.1/metrics",
        ]
        for url in forbidden_urls:
            res = browser_agent.browse_and_read_webpage(url)
            self.assertIn("Security violation blocked browsing operation", res)
            self.assertIn("SSRF Protection", res)

    def test_browse_and_read_webpage_fails_closed_on_empty_and_malformed_url(self):
        """Verifies empty or malformed URLs fail closed gracefully."""
        self.assertIn("cannot be empty", browser_agent.browse_and_read_webpage(""))
        self.assertIn("cannot be empty", browser_agent.browse_and_read_webpage("   "))

    def test_capture_webpage_screenshot_blocks_loopback_and_private_destinations(self):
        """Verifies capture_webpage_screenshot blocks SSRF targets before launching browser."""
        bad_urls = [
            "http://127.0.0.1:20129/hud",
            "http://localhost:8080/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.254",
        ]
        for url in bad_urls:
            res = browser_agent.capture_webpage_screenshot(url, filename="test_leak.png")
            self.assertIn("Security violation blocked screenshot operation", res)

    def test_capture_webpage_screenshot_navigates_to_safe_url(self):
        """Verifies capture_webpage_screenshot navigates to the validated safe_url, not the raw argument."""
        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_pw_context = MagicMock()
        mock_pw_context.__enter__.return_value = mock_playwright

        with patch("playwright.sync_api.sync_playwright", return_value=mock_pw_context), \
             patch("jarvis.core.security_boundaries.validate_confined_path", return_value="data/safe_snapshot.png"):
            res = browser_agent.capture_webpage_screenshot("example.com", filename="safe_snapshot.png")
            self.assertIn("Webpage screenshot captured", res)
            # Verify browser context was initialized blocking service workers
            mock_browser.new_context.assert_called_once_with(service_workers="block")
            # Verify page.goto received normalized and validated https:// URL
            mock_page.goto.assert_called_once()
            called_url = mock_page.goto.call_args[0][0]
            self.assertEqual(called_url, "https://example.com")

    def test_open_url_in_desktop_browser_blocks_dangerous_schemes(self):
        """Verifies open_url_in_desktop_browser only allows http and https schemes."""
        dangerous = [
            "file:///C:/Windows/System32/drivers/etc/hosts",
            "javascript:alert(document.cookie)",
            "data:text/html,<h1>pwned</h1>",
            "vbscript:msgbox(1)",
        ]
        with patch("webbrowser.open") as mock_open:
            for bad_url in dangerous:
                res = browser_agent.open_url_in_desktop_browser(bad_url)
                self.assertIn("Blocked opening URL with unsupported scheme", res)
                mock_open.assert_not_called()

    def test_open_url_in_desktop_browser_allows_safe_http_and_https(self):
        """Verifies open_url_in_desktop_browser opens valid web URLs."""
        with patch("webbrowser.open") as mock_open:
            res = browser_agent.open_url_in_desktop_browser("https://github.com")
            self.assertIn("Opened 'https://github.com'", res)
            mock_open.assert_called_once_with("https://github.com")

    def test_playwright_ssrf_guard_aborts_prohibited_request(self):
        """Verifies _setup_playwright_ssrf_guard route interception aborts requests to prohibited destinations."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)

        mock_context.route.assert_called_once()
        route_pattern, route_handler = mock_context.route.call_args[0]
        self.assertEqual(route_pattern, "**/*")

        # Simulate route for cloud metadata
        mock_route = MagicMock()
        mock_route.request.url = "http://169.254.169.254/latest/meta-data/"
        route_handler(mock_route)

        mock_route.abort.assert_called_once_with("blockedbyclient")
        self.assertIsNotNone(violation[0])
        self.assertIn("169.254.169.254", violation[0])

    def test_playwright_ssrf_guard_aborts_multi_hop_redirect_chain_to_private(self):
        """Verifies _setup_playwright_ssrf_guard follows a multi-hop chain and aborts before requesting private destination."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)
        _, route_handler = mock_context.route.call_args[0]

        mock_route = MagicMock()
        mock_route.request.url = "https://example.com/hop1"

        # Hop 1 redirects to Hop 2, Hop 2 redirects to private RFC 1918 address
        resp_hop1 = MagicMock()
        resp_hop1.status = 302
        resp_hop1.headers = {"location": "https://example.com/hop2"}

        resp_hop2 = MagicMock()
        resp_hop2.status = 302
        resp_hop2.headers = {"location": "http://10.0.0.1/admin"}

        def fake_fetch(*args, **kwargs):
            target = kwargs.get("url", "")
            if "hop1" in target:
                return resp_hop1
            if "hop2" in target:
                return resp_hop2
            self.fail(f"Unexpected network request sent to prohibited destination: {target}")

        mock_route.fetch.side_effect = fake_fetch
        route_handler(mock_route)

        mock_route.abort.assert_called_once_with("blockedbyclient")
        self.assertIsNotNone(violation[0])
        self.assertIn("10.0.0.1", violation[0])

    def test_playwright_ssrf_guard_relative_location_resolution(self):
        """Verifies _setup_playwright_ssrf_guard resolves relative Location headers and blocks loopback."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)
        _, route_handler = mock_context.route.call_args[0]

        mock_route = MagicMock()
        mock_route.request.url = "https://example.com/subpath/entry"

        # Redirect with protocol-relative URL to loopback
        resp_redir = MagicMock()
        resp_redir.status = 302
        resp_redir.headers = {"location": "//127.0.0.1:20129/api/secret"}

        mock_route.fetch.return_value = resp_redir
        route_handler(mock_route)

        mock_route.abort.assert_called_once_with("blockedbyclient")
        self.assertIsNotNone(violation[0])
        self.assertIn("127.0.0.1", violation[0])

    def test_playwright_ssrf_guard_redirect_loop_detection(self):
        """Verifies _setup_playwright_ssrf_guard detects circular redirect loops and halts."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)
        _, route_handler = mock_context.route.call_args[0]

        mock_route = MagicMock()
        mock_route.request.url = "https://example.com/loopA"

        resp_a = MagicMock()
        resp_a.status = 302
        resp_a.headers = {"location": "https://example.com/loopB"}

        resp_b = MagicMock()
        resp_b.status = 302
        resp_b.headers = {"location": "https://example.com/loopA"}

        def fake_fetch(*args, **kwargs):
            target = kwargs.get("url", "")
            return resp_a if "loopA" in target else resp_b

        mock_route.fetch.side_effect = fake_fetch
        route_handler(mock_route)

        mock_route.abort.assert_called_once_with("blockedbyclient")
        self.assertIsNotNone(violation[0])
        self.assertIn("Redirect loop detected", violation[0])

    def test_playwright_ssrf_guard_hop_limit_exceeded(self):
        """Verifies _setup_playwright_ssrf_guard terminates chains that exceed max_hops."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False, max_hops=3)
        _, route_handler = mock_context.route.call_args[0]

        mock_route = MagicMock()
        mock_route.request.url = "https://example.com/hop0"

        def fake_fetch(*args, **kwargs):
            target = kwargs.get("url", "")
            hop_num = int(target[-1]) if target[-1].isdigit() else 0
            next_hop = hop_num + 1
            resp = MagicMock()
            resp.status = 302
            resp.headers = {"location": f"https://example.com/hop{next_hop}"}
            return resp

        mock_route.fetch.side_effect = fake_fetch
        route_handler(mock_route)

        mock_route.abort.assert_called_once_with("blockedbyclient")
        self.assertIsNotNone(violation[0])
        self.assertIn("Redirect limit exceeded", violation[0])

    def test_playwright_ssrf_guard_blocks_https_to_http_downgrade(self):
        """Verifies _setup_playwright_ssrf_guard rejects insecure protocol downgrade on redirect."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)
        _, route_handler = mock_context.route.call_args[0]

        mock_route = MagicMock()
        mock_route.request.url = "https://example.com/secure"

        resp_downgrade = MagicMock()
        resp_downgrade.status = 302
        resp_downgrade.headers = {"location": "http://example.com/insecure"}

        mock_route.fetch.return_value = resp_downgrade
        route_handler(mock_route)

        mock_route.abort.assert_called_once_with("blockedbyclient")
        self.assertIsNotNone(violation[0])
        self.assertIn("Insecure redirect downgrade", violation[0])

    def test_playwright_ssrf_guard_fulfills_safe_public_response(self):
        """Verifies _setup_playwright_ssrf_guard fulfills safe non-redirect responses."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)
        _, route_handler = mock_context.route.call_args[0]

        mock_route = MagicMock()
        mock_route.request.url = "https://example.com/style.css"
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_route.fetch.return_value = mock_response

        route_handler(mock_route)

        mock_route.fulfill.assert_called_once_with(response=mock_response)
        self.assertIsNone(violation[0])

    def test_playwright_websocket_ssrf_guard_blocks_prohibited_endpoints(self):
        """Verifies _setup_playwright_ssrf_guard intercepts WebSockets and closes private endpoints."""
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)

        mock_context.route_web_socket.assert_called_once()
        ws_pattern, ws_handler = mock_context.route_web_socket.call_args[0]
        self.assertEqual(ws_pattern, "**/*")

        # Test loopback WebSocket
        mock_ws_loopback = MagicMock()
        mock_ws_loopback.url = "ws://127.0.0.1:20129/hud/telemetry"
        ws_handler(mock_ws_loopback)
        mock_ws_loopback.close.assert_called_once_with(code=1008, reason="Blocked by SSRF policy")
        mock_ws_loopback.connect_to_server.assert_not_called()
        self.assertIn("127.0.0.1", violation[0])

        # Test cloud metadata WebSocket
        violation[0] = None
        mock_ws_meta = MagicMock()
        mock_ws_meta.url = "ws://169.254.169.254/socket"
        ws_handler(mock_ws_meta)
        mock_ws_meta.close.assert_called_once_with(code=1008, reason="Blocked by SSRF policy")
        mock_ws_meta.connect_to_server.assert_not_called()
        self.assertIn("169.254.169.254", violation[0])

    def test_playwright_websocket_ssrf_guard_allows_safe_wss(self):
        """Verifies _setup_playwright_ssrf_guard connects to safe public WSS endpoints."""
        import socket
        fake_addr = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        mock_context = MagicMock()
        violation = [None]
        browser_agent._setup_playwright_ssrf_guard(mock_context, violation, allow_localhost=False)
        _, ws_handler = mock_context.route_web_socket.call_args[0]

        mock_ws_safe = MagicMock()
        mock_ws_safe.url = "wss://echo.websocket.events"
        with patch("socket.getaddrinfo", return_value=fake_addr):
            ws_handler(mock_ws_safe)
        mock_ws_safe.connect_to_server.assert_called_once()
        mock_ws_safe.close.assert_not_called()
        self.assertIsNone(violation[0])

    def test_browse_and_read_webpage_playwright_reports_ssrf_on_redirect(self):
        """Verifies browse_and_read_webpage surfaces the SSRF violation error when Playwright aborts due to redirect."""
        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_pw_context = MagicMock()
        mock_pw_context.__enter__.return_value = mock_playwright

        def fake_goto(url, **kwargs):
            raise RuntimeError("net::ERR_BLOCKED_BY_CLIENT")

        mock_page.goto.side_effect = fake_goto

        def fake_route(pattern, handler):
            mock_route = MagicMock()
            mock_route.request.url = "https://example.com/redir"
            mock_resp = MagicMock()
            mock_resp.status = 302
            mock_resp.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
            mock_route.fetch.return_value = mock_resp
            handler(mock_route)

        mock_context.route.side_effect = fake_route

        with patch("playwright.sync_api.sync_playwright", return_value=mock_pw_context):
            res = browser_agent.browse_and_read_webpage("https://example.com/redir")
            self.assertIn("Security violation blocked browsing operation", res)
            self.assertIn("169.254.169.254", res)

    def test_browse_and_read_webpage_fallback_uses_hardened_opener(self):
        """Verifies the HTTP fallback path uses build_safe_url_opener and blocks redirect bypasses."""
        from jarvis.core.security_boundaries import SecurityBoundaryViolation

        with patch("playwright.sync_api.sync_playwright", side_effect=ImportError("Playwright not installed")):
            mock_opener = MagicMock()
            mock_opener.open.side_effect = SecurityBoundaryViolation("Redirect to private IP blocked by SafeRedirectHandler")
            with patch("jarvis.core.security_boundaries.build_safe_url_opener", return_value=mock_opener):
                res = browser_agent.browse_and_read_webpage("https://example.com/redirect-to-private")
                self.assertIn("Security violation blocked fallback browsing operation", res)
                self.assertIn("SafeRedirectHandler", res)

    def test_browse_and_read_webpage_fallback_successful_extraction(self):
        """Verifies the HTTP fallback path correctly parses HTML and extracts text when safe."""
        mock_resp = MagicMock()
        mock_resp.headers.get_content_charset.return_value = "utf-8"
        mock_resp.read.return_value = b"<html><head><title>Architecture Docs</title></head><body><p>Core system invariants</p></body></html>"
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with patch("playwright.sync_api.sync_playwright", side_effect=ImportError("Playwright disabled")), \
             patch("jarvis.core.security_boundaries.build_safe_url_opener", return_value=mock_opener):
            res = browser_agent.browse_and_read_webpage("https://example.com/docs")
            self.assertIn("Page Title: Architecture Docs", res)
            self.assertIn("Core system invariants", res)


    def test_browse_and_read_webpage_and_screenshot_enforce_service_workers_block(self):
        """Verifies both browsing and screenshot operations explicitly configure service_workers='block'."""
        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_pw_context = MagicMock()
        mock_pw_context.__enter__.return_value = mock_playwright

        with patch("playwright.sync_api.sync_playwright", return_value=mock_pw_context):
            # 1. Test browse_and_read_webpage
            mock_page.title.return_value = "Test Title"
            mock_page.inner_text.return_value = "Test Content"
            browser_agent.browse_and_read_webpage("https://example.com")
            mock_browser.new_context.assert_called_with(
                service_workers="block",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

            mock_browser.new_context.reset_mock()

            # 2. Test capture_webpage_screenshot
            with patch("jarvis.core.security_boundaries.validate_confined_path", return_value="data/test_snap.png"):
                browser_agent.capture_webpage_screenshot("https://example.com", filename="test_snap.png")
                mock_browser.new_context.assert_called_with(service_workers="block")


class TestBrowserAgentLiveChromiumSSRF(unittest.TestCase):
    """Real-browser integration tests verifying SSRF guard against popups, service workers, and web workers in Chromium."""

    @classmethod
    def setUpClass(cls):
        """Spins up a lightweight ephemeral HTTP test server on loopback."""
        import http.server
        import socketserver
        import threading

        cls.requests_log = []

        class TestEgressServerHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests_log.append(self.path)
                if self.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    html_content = (
                        "<!DOCTYPE html><html><body>"
                        "<a id='popup-prohibited' href='http://10.0.0.1/admin' target='_blank'>Popup Bad</a>"
                        "<script>"
                        "window.workerStatus = 'none';"
                        "window.registerSW = async () => {"
                        "  try { return await navigator.serviceWorker.register('/sw.js'); }"
                        "  catch (e) { return e.name + ': ' + e.message; }"
                        "};"
                        "window.spawnWorker = () => {"
                        "  try {"
                        "    window.workerStatus = 'running';"
                        "    const w = new Worker('/worker.js');"
                        "    w.onmessage = (e) => { window.workerStatus = e.data; };"
                        "    w.onerror = (e) => { window.workerStatus = 'error:' + e.message; };"
                        "    return 'worker_created';"
                        "  } catch (e) { return e.name + ': ' + e.message; }"
                        "};"
                        "</script>"
                        "</body></html>"
                    )
                    self.wfile.write(html_content.encode("utf-8"))
                elif self.path == "/sw.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.end_headers()
                    self.wfile.write(b"console.log('sw loaded');")
                elif self.path == "/worker.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.end_headers()
                    self.wfile.write(
                        b"fetch('http://simulated-private-host.test/secret')"
                        b"  .then(r => postMessage('fetch_success:' + r.status))"
                        b"  .catch(e => postMessage('fetch_blocked:' + e.message));"
                    )
                elif self.path == "/secret":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"LEAKED")
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        cls.server = socketserver.TCPServer(("127.0.0.1", 0), TestEgressServerHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        """Shuts down ephemeral HTTP test server."""
        cls.server.shutdown()
        cls.server.server_close()

    def test_popup_initial_navigation_blocked_by_context_route_before_connection(self):
        """Verifies that a popup's initial navigation to a prohibited destination is intercepted and blocked."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            violation_ref = [None]

            # Install context-level SSRF guard before any page is created
            browser_agent._setup_playwright_ssrf_guard(context, violation_ref, allow_localhost=True)

            page = context.new_page()
            # Register popup auto-close handler like production code
            page.on("popup", lambda popup: popup.close())

            page.goto(self.base_url)

            # Trigger popup navigation to prohibited RFC 1918 destination (10.0.0.1)
            page.click("#popup-prohibited")

            # Allow event loop to process popup creation and routing
            import time
            time.sleep(0.5)

            # Assert SSRF guard caught the prohibited initial navigation
            self.assertIsNotNone(
                violation_ref[0],
                "Context route failed to intercept popup's initial navigation before connection!"
            )
            self.assertIn("10.0.0.1", violation_ref[0])
            self.assertIn("SSRF Protection", violation_ref[0])

            browser.close()

    def test_service_workers_block_prevents_registration_and_network_egress(self):
        """Verifies service_workers='block' prevents service worker script fetch and execution."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            violation_ref = [None]
            browser_agent._setup_playwright_ssrf_guard(context, violation_ref, allow_localhost=True)

            page = context.new_page()
            page.goto(self.base_url)

            # Attempt to register service worker
            page.evaluate("window.registerSW()")

            import time
            time.sleep(0.5)

            # Verify no service workers exist in the context
            self.assertEqual(
                len(context.service_workers), 0,
                "Service worker was registered despite service_workers='block'!"
            )
            # Verify the service worker script was never requested from the server
            self.assertNotIn(
                "/sw.js", self.requests_log,
                "Service worker script /sw.js was downloaded despite service_workers='block'!"
            )

            browser.close()

    def test_dedicated_web_worker_request_interception_investigation(self):
        """Proves dedicated Web Worker fetch requests to private destinations are intercepted and rejected by production SSRF guard."""
        import socket
        from unittest.mock import patch
        from playwright.sync_api import sync_playwright

        real_getaddrinfo = socket.getaddrinfo

        def controlled_getaddrinfo(host, port, *args, **kwargs):
            if host == "simulated-private-host.test":
                # Controlled simulation: host resolves to RFC 1918 private address (10.0.0.1)
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", port or 80))]
            return real_getaddrinfo(host, port, *args, **kwargs)

        with patch("socket.getaddrinfo", side_effect=controlled_getaddrinfo):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(service_workers="block")
                violation_ref = [None]
                intercepted_urls = []
                dispatched_fetch_urls = []

                orig_route = context.route

                def spy_route(pattern, handler):
                    def wrapped_handler(route):
                        intercepted_urls.append(route.request.url)
                        orig_fetch = route.fetch

                        def spy_fetch(*args, **kwargs):
                            dispatched_fetch_urls.append(route.request.url)
                            return orig_fetch(*args, **kwargs)

                        route.fetch = spy_fetch
                        return handler(route)

                    return orig_route(pattern, wrapped_handler)

                context.route = spy_route

                # Install production context-level SSRF guard (pure production decision path, no custom validator)
                browser_agent._setup_playwright_ssrf_guard(
                    context,
                    violation_ref,
                    allow_localhost=True,
                )

                start_req_count = len(self.requests_log)
                page = context.new_page()
                page.goto(self.base_url)

                # Spawn dedicated web worker that attempts fetch('http://simulated-private-host.test/secret')
                page.evaluate("window.spawnWorker()")

                # Deterministic sync: wait until worker executes and posts message back
                page.wait_for_function("() => window.workerStatus.startsWith('fetch_')", timeout=5000)
                worker_status = page.evaluate("window.workerStatus")

                # 1. Independent assertion: Worker script loaded and executed
                self.assertIn(
                    "/worker.js", self.requests_log[start_req_count:],
                    "Dedicated worker script /worker.js was not requested from the test server!"
                )
                self.assertTrue(
                    worker_status.startswith("fetch_blocked"),
                    f"Worker fetch was not rejected; got worker_status='{worker_status}'"
                )

                # 2. Independent assertion: Route handler received the worker's attempted request
                self.assertIn(
                    "http://simulated-private-host.test/secret", intercepted_urls,
                    "Route handler was never invoked for the worker's attempted request!"
                )

                # 3. Independent assertion: Production guard's decision path rejected it
                self.assertIsNotNone(
                    violation_ref[0],
                    "SSRF guard failed to record violation for prohibited worker request!"
                )
                self.assertIn(
                    "SSRF Protection: Host 'simulated-private-host.test' resolved to non-global address '10.0.0.1'",
                    violation_ref[0],
                )

                # 4. Independent assertion: Blocked request was never passed to route.fetch() or network
                self.assertNotIn(
                    "http://simulated-private-host.test/secret", dispatched_fetch_urls,
                    "Blocked request was dispatched to route.fetch() despite SSRF violation!"
                )
                self.assertTrue(
                    all(u.startswith(self.base_url) for u in dispatched_fetch_urls),
                    f"Non-harness dispatches occurred: {dispatched_fetch_urls}"
                )
                self.assertNotIn(
                    "/secret", self.requests_log[start_req_count:],
                    "Secret endpoint was reached on test server despite SSRF route guard!"
                )

                browser.close()


if __name__ == "__main__":
    unittest.main()
