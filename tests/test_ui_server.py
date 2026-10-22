"""Test suite for Jarvis 2.0 Holographic Web HUD Server."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
import urllib.request
import json
import unittest
from jarvis.ui.server import JarvisUIService, create_bootstrap_token, get_hud_session_token


class TestUIServer(unittest.TestCase):
    def test_ui_server_lifecycle(self):
        # Use ephemeral test port
        test_port = 20139
        service = JarvisUIService(orchestrator=None, host="127.0.0.1", port=test_port)

        try:
            service.start()
            time.sleep(0.8)

            # 1. Test index page: unauthenticated request renders lock screen without credentials
            req_index_unauth = urllib.request.Request(f"http://127.0.0.1:{test_port}/")
            with urllib.request.urlopen(req_index_unauth, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("Content-Security-Policy", resp.headers)
                self.assertIn("Content-Security-Policy-Report-Only", resp.headers)
                self.assertEqual(resp.headers.get("Reporting-Endpoints"), f'csp-endpoint="http://127.0.0.1:{test_port}/api/csp-report"')
                self.assertNotEqual(resp.headers.get("Reporting-Endpoints"), 'csp-endpoint="/api/csp-report"')
                self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
                self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
                content_unauth = resp.read().decode("utf-8")
                self.assertIn("Interface Locked", content_unauth)
                self.assertNotIn("window.__JARVIS_TOKEN__", content_unauth)

            # Authenticate via single-use bootstrap token exchange at /api/auth/unlock
            b_token = create_bootstrap_token(ttl_seconds=60.0)
            unlock_body = json.dumps({"token": b_token}).encode("utf-8")
            req_unlock = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/api/auth/unlock",
                data=unlock_body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req_unlock, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                set_cookie = resp.headers.get("Set-Cookie", "")
                self.assertIn("jarvis_session=", set_cookie)
                cookie_val = set_cookie.split(";")[0]

            # Fetch authenticated index page using the session cookie
            req_index_auth = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/",
                headers={"Cookie": cookie_val},
            )
            with urllib.request.urlopen(req_index_auth, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                content_auth = resp.read().decode("utf-8")
                self.assertIn("J.A.R.V.I.S.", content_auth)
                self.assertIn("reactorCanvas", content_auth)
                self.assertNotIn("window.__JARVIS_TOKEN__", content_auth)

            # 2. Test status endpoint fails closed without auth, succeeds with session token
            req_status_unauth = urllib.request.Request(f"http://127.0.0.1:{test_port}/api/status")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_status_unauth, timeout=3.0)
            self.assertEqual(ctx.exception.code, 401)

            token = get_hud_session_token()
            req_status = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/api/status",
                headers={"X-Jarvis-Token": token}
            )
            with urllib.request.urlopen(req_status, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("battery", data)
                self.assertIn("gpu", data)
                self.assertIn("model", data)

            # 3. Test direct action endpoint with explicit session token header
            token = get_hud_session_token()
            action_data = json.dumps({"action": "set_volume", "percent": 50}).encode("utf-8")
            req_action = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/api/action",
                data=action_data,
                headers={"Content-Type": "application/json", "X-Jarvis-Token": token}
            )
            with urllib.request.urlopen(req_action, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(res.get("status"), "success")
                self.assertEqual(res.get("percent"), 50)

            # 4. Test CSP violation report ingestion via live HTTP socket
            csp_report_data = json.dumps({
                "csp-report": {
                    "document-uri": f"http://127.0.0.1:{test_port}/",
                    "violated-directive": "script-src",
                    "effective-directive": "script-src",
                    "blocked-uri": "eval",
                }
            }).encode("utf-8")
            req_csp = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/api/csp-report",
                data=csp_report_data,
                headers={"Content-Type": "application/csp-report"},
            )
            with urllib.request.urlopen(req_csp, timeout=3.0) as resp:
                self.assertEqual(resp.status, 204)

        finally:
            service.stop()
            time.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
