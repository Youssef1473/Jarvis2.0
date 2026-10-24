"""Automated tests for Chrome DevTools Protocol (CDP) silent browser control."""
import json
import unittest
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
import jarvis.skills.browser_cdp as b_cdp
import jarvis.skills.os_control as os_ctrl


class TestBrowserCDPSuite(unittest.TestCase):
    """Verifies CDP probe latency, silent tab navigation, and graceful Win32 fallbacks."""

    def test_cdp_probe_safety_and_speed(self):
        """Verifies CDP probe rejects non-localhost addresses and responds in <60ms."""
        # Non-localhost must be blocked immediately
        self.assertFalse(b_cdp.is_cdp_available("192.168.1.5", 9222))
        self.assertFalse(b_cdp.is_cdp_available("google.com", 9222))

        # Localhost probe test
        available = b_cdp.is_cdp_available("127.0.0.1", 9222, timeout_sec=0.02)
        self.assertIsInstance(available, bool)

    def test_list_open_browser_tabs_fallback(self):
        """Verifies tool returns courteous message when CDP port 9222 is not active."""
        with patch.object(b_cdp, "is_cdp_available", return_value=False):
            res = SkillRegistry.execute("list_open_browser_tabs")
            self.assertIn("port 9222 is closed", res)

    def test_list_cdp_tabs_parsing(self):
        """Verifies JSON response from /json/list is parsed into page targets."""
        fake_response = json.dumps([
            {"id": "tab1", "type": "page", "title": "YouTube", "url": "https://youtube.com"},
            {"id": "tab2", "type": "page", "title": "New Tab", "url": "brave://newtab"},
            {"id": "sw1", "type": "service_worker", "title": "Worker", "url": "sw.js"},
        ]).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            tabs = b_cdp.list_cdp_tabs()
            self.assertEqual(len(tabs), 2, "Only 'page' targets should be returned")
            self.assertEqual(tabs[0]["title"], "YouTube")
            self.assertEqual(tabs[1]["url"], "brave://newtab")

    def test_open_or_navigate_tab_cdp_reuses_blank_tab(self):
        """Verifies open_or_navigate_tab_cdp reuses an open empty tab via WebSocket or activation."""
        fake_tabs = [
            {"id": "tab_blank", "type": "page", "title": "New Tab", "url": "about:blank", "ws_url": "ws://127.0.0.1:9222/devtools/page/tab_blank"}
        ]

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs):
            
            mock_ws = MagicMock()
            mock_ws_enter = MagicMock()
            mock_ws_enter.__enter__.return_value = mock_ws

            with patch("websockets.sync.client.connect", return_value=mock_ws_enter), \
                 patch("urllib.request.urlopen") as mock_url:
                res = b_cdp.open_or_navigate_tab_cdp("https://github.com", new_tab=True)
                self.assertIsNotNone(res)
                self.assertIn("Navigated existing tab", res)
                self.assertIn("https://github.com", res)
                mock_ws.send.assert_called_once()
                sent_data = json.loads(mock_ws.send.call_args[0][0])
                self.assertEqual(sent_data["method"], "Page.navigate")
                self.assertEqual(sent_data["params"]["url"], "https://github.com")

    def test_os_control_open_browser_tab_cdp_fallback_to_win32(self):
        """Verifies open_browser_tab falls back gracefully to Win32 when CDP is closed."""
        with patch.object(b_cdp, "is_cdp_available", return_value=False), \
             patch("subprocess.Popen"), \
             patch("webbrowser.open_new_tab"):
            # Should proceed into win32gui without throwing exception
            res = os_ctrl.open_browser_tab(url="https://example.com", browser="brave")
            self.assertIsInstance(res, str)
            self.assertTrue(
                "active brave window" in res.lower()
                or "launched" in res.lower()
                or "opened" in res.lower()
                or "default" in res.lower()
            )

    def test_close_cdp_tab_success(self):
        """Verifies close_cdp_tab sends HTTP close request to DevTools port."""
        fake_tabs = [
            {"id": "tab123", "type": "page", "title": "Documentation", "url": "https://docs.python.org"}
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Target closed"
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs), \
             patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            res = b_cdp.close_cdp_tab(title_or_url="Documentation")
            self.assertIsNotNone(res)
            self.assertIn("Closed browser tab 'Documentation'", res)
            mock_open.assert_called_once()
            req = mock_open.call_args[0][0]
            self.assertIn("/json/close/tab123", req.full_url)

    def test_close_browser_tab_skill_cdp_path(self):
        """Verifies close_browser_tab skill executes CDP fast path when available."""
        with patch("jarvis.skills.browser_cdp.close_cdp_tab", return_value="Closed browser tab 'Docs' via silent DevTools channel, Sir."):
            res = SkillRegistry.execute("close_browser_tab", browser="brave", tab_title="Docs")
            self.assertIn("Closed browser tab", res)

    def test_close_browser_tab_skill_fallback_win32(self):
        """Verifies close_browser_tab falls back to Win32 hotkey when CDP is unavailable."""
        with patch("jarvis.skills.browser_cdp.close_cdp_tab", return_value=None), \
             patch("win32gui.EnumWindows") as mock_enum, \
             patch("pyautogui.hotkey") as mock_hotkey:
            res = SkillRegistry.execute("close_browser_tab", browser="brave")
            self.assertIsInstance(res, str)

    def test_activate_cdp_tab(self):
        """Verifies activate_cdp_tab sends PUT request to /json/activate."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"Target activated"
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch("urllib.request.urlopen", return_value=mock_resp) as mock_url:
            ok = b_cdp.activate_cdp_tab("tab_target_42")
            self.assertTrue(ok)
            mock_url.assert_called_once()
            req = mock_url.call_args[0][0]
            self.assertEqual(req.get_method(), "PUT")
            self.assertIn("/json/activate/tab_target_42", req.full_url)

    def test_read_webpage_content_cdp_and_skill(self):
        """Verifies read_webpage_content_cdp evaluates DOM and returns structured payload."""
        fake_tabs = [
            {"id": "doc_tab", "title": "Architecture Guide", "url": "https://example.org/arch", "ws_url": "ws://127.0.0.1:9222/devtools/page/doc_tab"}
        ]
        eval_payload = {
            "title": "Architecture Guide",
            "url": "https://example.org/arch",
            "text": "Core architecture overview: P-Cores, WASAPI, SQLite WAL, and CDP WebSocket."
        }
        ws_mock = MagicMock()
        ws_mock.recv.return_value = json.dumps({
            "id": 201,
            "result": {"result": {"value": eval_payload}}
        })
        ws_ctx = MagicMock()
        ws_ctx.__enter__.return_value = ws_mock

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs), \
             patch("websockets.sync.client.connect", return_value=ws_ctx):
            
            data = b_cdp.read_webpage_content_cdp("Architecture")
            self.assertIsNotNone(data)
            self.assertEqual(data["title"], "Architecture Guide")
            self.assertIn("Core architecture overview", data["text"])

            # Verify SkillRegistry invocation
            skill_res = SkillRegistry.execute("read_webpage_content", tab_name_or_url="Architecture")
            self.assertIn("Webpage Intel for 'Architecture Guide'", skill_res)
            self.assertIn("https://example.org/arch", skill_res)
            self.assertIn("P-Cores", skill_res)

    def test_execute_cdp_javascript_and_skill(self):
        """Verifies execute_cdp_javascript evaluates arbitrary script in target tab."""
        fake_tabs = [
            {"id": "math_tab", "title": "Calculator", "url": "https://calc.org", "ws_url": "ws://127.0.0.1:9222/devtools/page/math_tab"}
        ]
        ws_mock = MagicMock()
        ws_mock.recv.return_value = json.dumps({
            "id": 202,
            "result": {"result": {"value": 42}}
        })
        ws_ctx = MagicMock()
        ws_ctx.__enter__.return_value = ws_mock

        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs), \
             patch("websockets.sync.client.connect", return_value=ws_ctx):
            
            val = b_cdp.execute_cdp_javascript("6 * 7", target_id_or_title="Calculator")
            self.assertEqual(val, 42)

            skill_res = SkillRegistry.execute("execute_browser_javascript", script="6 * 7", tab_name="Calculator")
            self.assertIn("JavaScript Evaluation Result", skill_res)
            self.assertIn("42", skill_res)

    def test_switch_to_browser_tab_skill(self):
        """Verifies switch_to_browser_tab matches title and activates target tab."""
        fake_tabs = [
            {"id": "tab_gh", "title": "Jarvis 2.0 Repository", "url": "https://github.com/repo"}
        ]
        with patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs), \
             patch.object(b_cdp, "activate_cdp_tab", return_value=True) as mock_act, \
             patch("jarvis.skills.windows_uia.focus_window_uia", return_value=True):
            
            res = SkillRegistry.execute("switch_to_browser_tab", tab_name_or_url="Repository")
            self.assertIn("Switched to browser tab 'Jarvis 2.0 Repository'", res)
            mock_act.assert_called_once_with("tab_gh")

    def test_scrape_and_archive_webpage_skill(self):
        """Verifies scrape_and_archive_webpage archives DOM content into MemoryManager document vault."""
        fake_content = {
            "title": "Quantum Computing Documentation",
            "url": "https://quantum.org/docs",
            "text": "Detailed quantum supremacy benchmarks and QPU circuit depth specifications."
        }
        mock_mem = MagicMock()
        mock_mem.save_document_knowledge.return_value = 101

        with patch.object(b_cdp, "read_webpage_content_cdp", return_value=fake_content), \
             patch("jarvis.skills.calendar_manager._get_memory", return_value=mock_mem):
            
            res = SkillRegistry.execute("scrape_and_archive_webpage", tab_name_or_url="Quantum", category="quantum_intel")
            self.assertIn("Archived webpage 'Quantum Computing Documentation'", res)
            self.assertIn("ID: #101", res)
            self.assertIn("quantum_intel", res)
            mock_mem.save_document_knowledge.assert_called_once()
            call_kw = mock_mem.save_document_knowledge.call_args[1]
            self.assertEqual(call_kw["title"], "Quantum Computing Documentation")
            self.assertEqual(call_kw["category"], "quantum_intel")
            self.assertIn("https://quantum.org/docs", call_kw["raw_content"])
            self.assertIn("Detailed quantum supremacy benchmarks", call_kw["raw_content"])
            self.assertEqual(call_kw["file_path"], "https://quantum.org/docs")
            self.assertEqual(call_kw["source_provenance"], "web_scrape")

    def test_bootstrap_cdp_browser(self):
        """Verifies bootstrap_cdp_browser spawns detached browser process with dedicated profile."""
        with patch.object(b_cdp, "is_cdp_available", side_effect=[False, True]), \
             patch("jarvis.skills.os_control._build_app_index", return_value={"brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"}), \
             patch("subprocess.Popen") as mock_popen, \
             patch("time.sleep"):
            
            success, msg = b_cdp.bootstrap_cdp_browser(browser_name="brave", port=9222, dedicated_profile=True)
            self.assertTrue(success)
            self.assertIn("Attached browser 'Brave' with remote debugging port 9222 enabled", msg)
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertIn("--remote-debugging-port=9222", args)
            self.assertIn("--remote-debugging-address=127.0.0.1", args)
            self.assertTrue(any("--user-data-dir=" in a for a in args))

    def test_url_scheme_security_validation(self):
        """Verifies dangerous schemes like javascript: and chrome:// management pages are blocked by sanitize_browser_url."""
        # Test sanitize_browser_url directly — it is the authoritative URL security boundary.
        from jarvis.skills.browser_cdp import sanitize_browser_url

        # Dangerous execution schemes must be sanitized to about:blank.
        self.assertEqual(sanitize_browser_url("javascript:alert(1)"), "about:blank")
        self.assertEqual(sanitize_browser_url("data:text/html,<h1>xss</h1>"), "about:blank")
        self.assertEqual(sanitize_browser_url("vbscript:msgbox(1)"), "about:blank")
        self.assertEqual(sanitize_browser_url("file:///C:/sensitive.txt"), "about:blank")

        # Destructive internal browser management URLs must be blocked.
        self.assertEqual(sanitize_browser_url("chrome://restart"), "about:blank")
        self.assertEqual(sanitize_browser_url("chrome://crash"), "about:blank")
        self.assertEqual(sanitize_browser_url("brave://quit"), "about:blank")
        self.assertEqual(sanitize_browser_url("chrome://settings/clearBrowserData"), "about:blank")

        # Unpermitted internal schemes must be blocked.
        self.assertEqual(sanitize_browser_url("chrome://flags"), "about:blank")
        self.assertEqual(sanitize_browser_url("brave://flags"), "about:blank")

        # Safe navigation targets must pass through unmolested.
        self.assertEqual(sanitize_browser_url("https://github.com"), "https://github.com")
        self.assertEqual(sanitize_browser_url("http://localhost:8080"), "http://localhost:8080")
        self.assertEqual(sanitize_browser_url("about:blank"), "about:blank")
        self.assertEqual(sanitize_browser_url("youtube.com"), "https://youtube.com")

        # CRLF injection must be stripped.
        self.assertEqual(sanitize_browser_url("https://safe.com\r\nX-Injected: header"), "https://safe.com")

        # Shield internal Jarvis services (HUD and CDP debugging ports) on loopback.
        self.assertEqual(sanitize_browser_url("http://localhost:20129"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://127.0.0.1:20129/api/status"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://localhost:9222"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://127.0.0.1:9222/json"), "about:blank")

        # Legitimate local developer workflows remain allowed on non-Jarvis ports.
        self.assertEqual(sanitize_browser_url("http://localhost:3000"), "http://localhost:3000")
        self.assertEqual(sanitize_browser_url("http://127.0.0.1:5173/dev"), "http://127.0.0.1:5173/dev")

        # Non-global destinations (RFC 1918 private and link-local cloud metadata) are blocked.
        self.assertEqual(sanitize_browser_url("http://169.254.169.254/latest/meta-data/"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://192.168.1.1/admin"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://10.0.0.1:8080/intranet"), "about:blank")

        # URLs with embedded user-info credentials must be rejected outright.
        self.assertEqual(sanitize_browser_url("http://admin:secret123@example.com"), "about:blank")
        self.assertEqual(sanitize_browser_url("https://user@example.com/api"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://:pass@example.com"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://admin%40corp.com:secret@example.com"), "about:blank")

        # Shield sensitive background services on localhost (Ollama, Docker, PostgreSQL, Redis, K8s).
        self.assertEqual(sanitize_browser_url("http://localhost:11434"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://127.0.0.1:11434/api/tags"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://localhost:2375"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://localhost:5432"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://localhost:6379"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://localhost:6443"), "about:blank")

        # IPv6 loopback and link-local destinations
        self.assertEqual(sanitize_browser_url("http://[::1]:9222"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://[::1]:11434"), "about:blank")
        self.assertEqual(sanitize_browser_url("http://[fe80::1]/"), "about:blank")

    def test_url_sanitizer_credential_privacy_in_logs(self):
        """Verifies embedded credentials are never recorded in logger warnings during sanitization."""
        super_secret = "UltraConfidentialToken99!"
        with self.assertLogs("Jarvis.BrowserCDP", level="WARNING") as captured_logs:
            res = b_cdp.sanitize_browser_url(f"https://admin:{super_secret}@sensitive-corp.internal/v1")
            self.assertEqual(res, "about:blank")
        all_logs = " ".join(captured_logs.output)
        self.assertNotIn(super_secret, all_logs)
        self.assertIn("embedded user-info credentials", all_logs)

    def test_open_or_navigate_tab_cdp_explicit_rejection(self):
        """Verifies open_or_navigate_tab_cdp returns explicit blocked status instead of navigating to about:blank."""
        with patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True):
            res = b_cdp.open_or_navigate_tab_cdp("http://admin:secret@example.com")
            self.assertIn("Navigation blocked", res)
            self.assertNotIn("secret", res)

            res_priv = b_cdp.open_or_navigate_tab_cdp("http://192.168.1.1/admin")
            self.assertIn("Navigation blocked", res_priv)

    def test_open_browser_tab_validates_before_cdp_and_subprocess(self):
        """Verifies open_browser_tab validates target_url before invoking either CDP or subprocess."""
        # Scenario A: Dangerous scheme with CDP available
        with patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=True):
            res = os_ctrl.open_browser_tab("file:///C:/Windows/System32/cmd.exe")
            self.assertIn("Navigation blocked", res)

        # Scenario B: Dangerous scheme with CDP unavailable (must never reach subprocess.Popen)
        with patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=False), \
             patch("subprocess.Popen") as mock_popen:
            res = os_ctrl.open_browser_tab("file:///C:/Windows/System32/cmd.exe")
            self.assertIn("Navigation blocked", res)
            mock_popen.assert_not_called()

        # Scenario C: Embedded credentials with CDP unavailable
        with patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=False), \
             patch("subprocess.Popen") as mock_popen:
            res = os_ctrl.open_browser_tab("http://admin:secret123@example.com")
            self.assertIn("Navigation blocked", res)
            mock_popen.assert_not_called()

        # Scenario D: Private RFC 1918 destination with CDP unavailable
        with patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=False), \
             patch("subprocess.Popen") as mock_popen:
            res = os_ctrl.open_browser_tab("http://192.168.1.1/router")
            self.assertIn("Navigation blocked", res)
            mock_popen.assert_not_called()

        # Scenario E: Safe public HTTPS URL with CDP unavailable reaches subprocess with validated URL
        with patch("jarvis.skills.browser_cdp.is_cdp_available", return_value=False), \
             patch("jarvis.skills.os_control._build_app_index", return_value={"brave.exe": "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"}), \
             patch("subprocess.Popen") as mock_popen:
            res = os_ctrl.open_browser_tab("https://github.com/microsoft")
            self.assertIn("Opened", res)
            self.assertTrue(mock_popen.called)
            called_args = mock_popen.call_args[0][0]
            self.assertIn("https://github.com/microsoft", called_args)


if __name__ == "__main__":
    unittest.main()


