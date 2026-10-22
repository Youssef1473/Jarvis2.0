"""Verification suite for Zero-Trust Security, WebGL 2.0 SDF Reactor, DSP Tap, and Win32 Overlay."""
import os
import sys
import unittest
from unittest.mock import MagicMock
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from starlette.testclient import TestClient

from jarvis.audio.dsp_tap import compute_68byte_dsp_frame, AudioDspTap, MAGIC_HEADER_BYTE
from jarvis.ui.server import (
    JarvisUIService,
    get_hud_session_token,
    set_hud_session_token,
    create_bootstrap_token,
    consume_bootstrap_token,
    resolve_safe_photo_path,
)
from jarvis.ui.win32_overlay import configure_stark_overlay_hwnd, update_dynamic_click_through


class TestHudSecurityAndDsp(unittest.TestCase):
    """Verifies security middleware, binary DSP frame packaging, path traversal guards, and overlay hooks."""

    def test_security_middleware_rejects_unauthenticated_requests(self):
        """Verifies unauthenticated POST requests to /api/command are rejected with HTTP 401/403 when authentication is enforced."""
        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        # 1. Unauthenticated request without token -> must be rejected with 401
        res_unauth = client.post("/api/command", json={"command": "status"})
        self.assertEqual(res_unauth.status_code, 401)
        self.assertIn("Unauthorized", res_unauth.json().get("error", ""))

        # 2. Invalid token -> rejected with 401
        res_invalid = client.post(
            "/api/command",
            headers={"X-Jarvis-Token": "invalid_bogus_token_xyz"},
            json={"command": "status"},
        )
        self.assertEqual(res_invalid.status_code, 401)

        # 3. Valid token in X-Jarvis-Token header -> authorized (returns 503 or 200, not 401)
        valid_token = get_hud_session_token()
        res_auth_header = client.post(
            "/api/command",
            headers={"X-Jarvis-Token": valid_token},
            json={"command": "status"},
        )
        self.assertIn(res_auth_header.status_code, (200, 503))

        # 4. Ordinary API queries with token in query string -> REJECTED (query-param tokens removed from API)
        res_auth_query = client.post(
            f"/api/command?token={valid_token}",
            json={"command": "status"},
        )
        self.assertEqual(res_auth_query.status_code, 401)

        # 5. Valid token in jarvis_session cookie -> authorized
        client.cookies.set("jarvis_session", valid_token)
        res_auth_cookie = client.post(
            "/api/command",
            json={"command": "status"},
        )
        client.cookies.delete("jarvis_session")
        self.assertIn(res_auth_cookie.status_code, (200, 503))

        # 6. Malicious external Origin header -> rejected with 403 Forbidden
        res_evil_origin = client.post(
            "/api/command",
            headers={
                "X-Jarvis-Token": valid_token,
                "Origin": "http://evil-attacker-website.com",
            },
            json={"command": "status"},
        )
        self.assertEqual(res_evil_origin.status_code, 403)

    def test_path_traversal_guard_blocks_malicious_photo_id(self):
        """Verifies resolve_safe_photo_path blocks directory traversal attempts like ..%2f or absolute paths."""
        tmp_photos_dir = Path("data/photos").resolve()

        # Malicious directory traversal vectors
        malicious_ids = [
            "../secrets.txt",
            "..%2fsecrets.txt",
            "..\\windows\\system32",
            "%2e%2e%2froot",
            "/etc/passwd",
            "C:\\Windows\\System32\\cmd.exe",
            "valid_id/../../evil",
            "photo_id\0nullbyte",
            "../../data/notes/notes.md",
            "",
            None,
        ]

        for bad_id in malicious_ids:
            resolved = resolve_safe_photo_path(bad_id, photos_dir=tmp_photos_dir)
            self.assertIsNone(
                resolved,
                f"Malicious photo_id '{bad_id}' was not blocked by path traversal guard!",
            )

        # Valid photo IDs must resolve safely within photos_dir
        valid_id = "a1b2c3d4e5f60718"
        resolved_valid = resolve_safe_photo_path(valid_id, photos_dir=tmp_photos_dir)
        self.assertIsNotNone(resolved_valid)
        self.assertTrue(resolved_valid.name.endswith(".jpg"))
        self.assertTrue(resolved_valid.is_relative_to(tmp_photos_dir))

    def test_dsp_frame_packaging_structure(self):
        """Verifies compute_68byte_dsp_frame returns exactly 68 bytes starting with magic byte 0x4A."""
        # 1. Test silent input
        silent_frame = compute_68byte_dsp_frame(np.zeros(512, dtype=np.float32))
        self.assertEqual(len(silent_frame), 68)
        self.assertEqual(silent_frame[0], MAGIC_HEADER_BYTE)
        self.assertEqual(silent_frame[1], 0)  # RMS = 0

        # 2. Test acoustic sinusoidal wave
        pcm_sine = np.sin(np.linspace(0, 40 * np.pi, 512, dtype=np.float32)) * 0.8
        sine_frame = compute_68byte_dsp_frame(pcm_sine)
        self.assertEqual(len(sine_frame), 68)
        self.assertEqual(sine_frame[0], MAGIC_HEADER_BYTE)
        self.assertGreater(sine_frame[1], 0)  # RMS > 0
        self.assertGreater(sine_frame[2], 0)  # ZCR > 0
        self.assertLess(sine_frame[3], 64)    # Dominant formant in 0..63

        bands = list(sine_frame[4:68])
        self.assertEqual(len(bands), 64)
        for b in bands:
            self.assertTrue(0 <= b <= 255)

        # 3. Test non-standard buffer lengths (pads or slices cleanly)
        short_frame = compute_68byte_dsp_frame(np.ones(128, dtype=np.float32))
        self.assertEqual(len(short_frame), 68)
        self.assertEqual(short_frame[0], MAGIC_HEADER_BYTE)

        long_frame = compute_68byte_dsp_frame(np.ones(1024, dtype=np.float32))
        self.assertEqual(len(long_frame), 68)
        self.assertEqual(long_frame[0], MAGIC_HEADER_BYTE)

    def test_csp_and_security_headers_present(self):
        """Verifies HTTP responses include Content-Security-Policy, X-Frame-Options: DENY, and X-Content-Type-Options: nosniff."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        # Test index route headers
        res_index = client.get("/")
        self.assertEqual(res_index.status_code, 200)
        self.assertIn("Content-Security-Policy", res_index.headers)
        self.assertEqual(res_index.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res_index.headers.get("X-Content-Type-Options"), "nosniff")

        # Test API status route headers: unauthenticated gets 401 with security headers
        res_status_unauth = client.get("/api/status")
        self.assertEqual(res_status_unauth.status_code, 401)
        self.assertIn("Content-Security-Policy", res_status_unauth.headers)
        self.assertEqual(res_status_unauth.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res_status_unauth.headers.get("X-Content-Type-Options"), "nosniff")

        # Authenticated gets 200 with security headers
        from jarvis.ui.server import get_hud_session_token
        token = get_hud_session_token()
        res_status_auth = client.get("/api/status", headers={"X-Jarvis-Token": token})
        self.assertEqual(res_status_auth.status_code, 200)
        self.assertIn("Content-Security-Policy", res_status_auth.headers)
        self.assertEqual(res_status_auth.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res_status_auth.headers.get("X-Content-Type-Options"), "nosniff")

    def test_hud_html_contains_docked_stark_pill_and_webgl_reactor(self):
        """Verifies hud.html contains StarkWebGLReactor, docked pill markup, and containment styles."""
        hud_path = Path("jarvis/ui/templates/hud.html")
        self.assertTrue(hud_path.exists())
        content = hud_path.read_text(encoding="utf-8")

        # WebGL 2.0 analytical reactor engine
        self.assertIn("StarkWebGLReactor", content)
        self.assertIn("canvas.getContext('webgl2'", content)
        self.assertIn("uFftBands", content)

        # Docked Stark Pill (UPGRADE 4)
        self.assertIn('id="starkDockedPill"', content)
        self.assertIn('id="dockedMiniCanvas"', content)
        self.assertIn('id="dockedFftCanvas"', content)
        self.assertIn('id="dockedTicker"', content)
        self.assertIn('toggleDockedMode', content)

        # CSS containment
        self.assertIn("contain: layout paint style;", content)

    def test_win32_overlay_graceful_invocation(self):
        """Verifies win32_overlay helper functions execute safely without crashing."""
        # Test invalid HWND=0 returns False safely
        res_config = configure_stark_overlay_hwnd(0)
        self.assertFalse(res_config)

        res_click = update_dynamic_click_through(0, [(0, 0, 100, 100)])
        self.assertFalse(res_click)



    def test_rejects_spoofed_user_agent_without_credentials(self):
        """Verifies client spoofing Python-urllib or testclient is strictly rejected with HTTP 401."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        res = client.post(
            "/api/command",
            headers={"User-Agent": "Python-urllib/3.12"},
            json={"command": "status"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertIn("Unauthorized", res.json().get("error", ""))

    def test_rejects_invalid_host_header_dns_rebinding(self):
        """Verifies requests with untrusted Host header are rejected with HTTP 403."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        res = client.post(
            "/api/command",
            headers={"Host": "attacker.com", "X-Jarvis-Token": valid_token},
            json={"command": "status"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Invalid Host header", res.json().get("error", ""))

    def test_unauthenticated_root_serves_unlock_page_without_token_leak(self):
        """Verifies unauthenticated GET / serves interface locked page without leaking session token."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Interface Locked", res.text)
        self.assertNotIn("window.__JARVIS_TOKEN__", res.text)
        self.assertNotIn("jarvis_session", client.cookies)

    def test_single_use_bootstrap_token_exchange_and_consumption(self):
        """Verifies single-use bootstrap token authenticates via POST /api/auth/unlock and cannot be replayed."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        b_token = create_bootstrap_token(ttl_seconds=60.0)

        # 1. Query parameter ?bootstrap= is eliminated: does NOT authenticate and does NOT consume token
        res_query = client.get(f"/?bootstrap={b_token}")
        self.assertEqual(res_query.status_code, 200)
        self.assertIn("Interface Locked", res_query.text)
        self.assertNotIn("window.__JARVIS_TOKEN__", res_query.text)
        self.assertNotIn("jarvis_session", client.cookies)

        # 2. Single-use exchange via POST /api/auth/unlock succeeds and sets HttpOnly cookie
        res_unlock = client.post("/api/auth/unlock", json={"token": b_token})
        self.assertEqual(res_unlock.status_code, 200)
        self.assertIn("jarvis_session", client.cookies)
        set_cookie_hdr = res_unlock.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", set_cookie_hdr)
        self.assertIn("samesite=strict", set_cookie_hdr.lower())

        # 3. Authenticated client can now access / and receives HUD without token leak
        res_hud = client.get("/")
        self.assertEqual(res_hud.status_code, 200)
        self.assertNotIn("Interface Locked", res_hud.text)
        self.assertNotIn("window.__JARVIS_TOKEN__", res_hud.text)

        # 4. Replaying the consumed bootstrap token fails with 401 Unauthorized
        client2 = TestClient(service._create_app())
        res_replay = client2.post("/api/auth/unlock", json={"token": b_token})
        self.assertEqual(res_replay.status_code, 401)

    def test_unlock_api_endpoint(self):
        """Verifies POST /api/auth/unlock validates tokens and issues session cookies."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        b_token = create_bootstrap_token(ttl_seconds=60.0)

        # Invalid token
        res_bad = client.post("/api/auth/unlock", json={"token": "bogus"})
        self.assertEqual(res_bad.status_code, 401)

        # Valid bootstrap token
        res_ok = client.post("/api/auth/unlock", json={"token": b_token})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json().get("status"), "authenticated")
        self.assertIn("jarvis_session", res_ok.cookies)

    def test_sensitive_get_routes_require_authentication(self):
        """Verifies sensitive read routes return 401 without authentication."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()

        sensitive_paths = [
            "/api/screenshot/latest",
            "/api/memory/nodes",
            "/api/approval/pending",
            "/api/tasks",
            "/api/notes",
            "/api/calendar",
        ]

        for p in sensitive_paths:
            # Unauthenticated -> 401
            res_unauth = client.get(p)
            self.assertEqual(res_unauth.status_code, 401, f"Expected 401 on unauthenticated {p}")

            # Authenticated with X-Jarvis-Token -> authorized (not 401)
            res_auth = client.get(p, headers={"X-Jarvis-Token": valid_token})
            self.assertNotEqual(res_auth.status_code, 401, f"Expected authorized on {p}")

    def test_websocket_rejects_unauthenticated_connection(self):
        """Verifies WebSocket connection is closed with 1008 if unauthenticated."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())

        with self.assertRaises(Exception):
            with client.websocket_connect("/ws") as ws:
                pass

        # Connecting with valid cookie succeeds
        valid_token = get_hud_session_token()
        client.cookies.set("jarvis_session", valid_token)
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            self.assertEqual(data.get("type"), "telemetry")

    def test_remote_host_binding_fails_closed_without_tunnel(self):
        """Verifies non-loopback host binding raises RuntimeError without JARVIS_ALLOW_REMOTE and tunnel."""
        service = JarvisUIService(host="0.0.0.0", enforce_auth=True)
        with self.assertRaises(RuntimeError) as ctx:
            service.start()
        self.assertIn("Refusing to bind Jarvis UI to non-loopback interface", str(ctx.exception))

    def test_missing_host_header_rejected(self):
        """Verifies requests with empty or missing Host header are rejected with HTTP 403."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        res = client.post(
            "/api/command",
            headers={"Host": "", "X-Jarvis-Token": valid_token},
            json={"command": "status"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Invalid Host header", res.json().get("error", ""))

    def test_websocket_rejects_untrusted_host(self):
        """Verifies WebSocket connection with spoofed Host header is rejected with 1008."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        client.cookies.set("jarvis_session", valid_token)

        with self.assertRaises(Exception):
            with client.websocket_connect("/ws", headers={"Host": "attacker.com"}) as ws:
                pass

    def test_websocket_rejects_bootstrap_query_token(self):
        """Verifies WebSocket rejects bootstrap tokens in query parameters (only session credentials allowed)."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        b_token = create_bootstrap_token(ttl_seconds=60.0)

        with self.assertRaises(Exception):
            with client.websocket_connect(f"/ws?bootstrap={b_token}") as ws:
                pass

    def test_unlock_html_contains_client_side_fragment_scrubber(self):
        """Verifies _get_unlock_html contains client-side fragment extractor and history scrubber."""
        service = JarvisUIService(enforce_auth=True)
        html = service._get_unlock_html()
        self.assertIn("#bootstrap=", html)
        self.assertIn("history.replaceState", html)
        self.assertIn("/api/auth/unlock", html)

    def test_approval_grant_ttl_is_bounded(self):
        """Verifies DualKeyApprovalManager caps authorization TTL to a safe maximum (300s)."""
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        mgr = DualKeyApprovalManager.get_instance()
        mgr.reset()
        auth = mgr.grant_approval("execute_powershell", {"command": "dir"}, ttl_seconds=999999.0)
        import time
        self.assertLessEqual(auth.expires_at, time.time() + 301.0)

    def test_approval_grant_endpoint_requires_authentication(self):
        """Verifies POST /api/approval/grant requires valid authentication and cannot be called unauthenticated."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        payload = {"tool_name": "execute_powershell", "arguments": {"command": "dir"}}

        # 1. Unauthenticated -> 401
        res_unauth = client.post("/api/approval/grant", json=payload)
        self.assertEqual(res_unauth.status_code, 401)

        # 2. Authenticated -> 200
        valid_token = get_hud_session_token()
        res_auth = client.post(
            "/api/approval/grant",
            headers={"X-Jarvis-Token": valid_token},
            json=payload,
        )
        self.assertEqual(res_auth.status_code, 200)
        self.assertEqual(res_auth.json().get("status"), "granted")

    def test_session_cookie_httponly_and_no_token_leak(self):
        """Verifies jarvis_session cookie has HttpOnly=True and tokens are not leaked in HTML."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        b_token = create_bootstrap_token(ttl_seconds=60.0)

        res_unlock = client.post("/api/auth/unlock", json={"token": b_token})
        self.assertEqual(res_unlock.status_code, 200)
        cookie_header = res_unlock.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("samesite=strict", cookie_header.lower())

        res_root = client.get("/")
        self.assertEqual(res_root.status_code, 200)
        self.assertNotIn("window.__JARVIS_TOKEN__", res_root.text)
        root_cookie = res_root.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", root_cookie)

    def test_exact_origin_and_host_validation_matrix(self):
        """Verifies exact host and origin validation against forged, malformed, wrong-port, and valid headers."""
        service = JarvisUIService(host="127.0.0.1", port=20129, enforce_auth=True)
        service.orchestrator = MagicMock()
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        auth_hdr = {"X-Jarvis-Token": valid_token}

        # 1. Missing / empty Host
        res = client.post("/api/command", headers={"Host": "", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 403)

        # 2. Malformed Host (invalid IPv6)
        res = client.post("/api/command", headers={"Host": "[::1:20129", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 403)

        # 3. Forged Host
        res = client.post("/api/command", headers={"Host": "attacker.com", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 403)

        # 4. Wrong Port in Host
        res = client.post("/api/command", headers={"Host": "127.0.0.1:9999", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 403)

        # 5. Valid IPv4 Host
        res = client.post("/api/command", headers={"Host": "127.0.0.1:20129", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 200)

        # 6. Valid IPv6 Host
        res = client.post("/api/command", headers={"Host": "[::1]:20129", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 200)

        # 7. Valid localhost Host
        res = client.post("/api/command", headers={"Host": "localhost:20129", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 200)

        # 8. Malformed / null Origin
        for bad_origin in ("null", "[::1:20129", "not_a_url"):
            res = client.post("/api/command", headers={"Origin": bad_origin, **auth_hdr}, json={"command": "status"})
            self.assertEqual(res.status_code, 403)

        # 9. Wrong scheme in Origin (HTTPS when server is HTTP, ftp, etc.)
        for bad_scheme in ("https://127.0.0.1:20129", "ftp://127.0.0.1:20129"):
            res = client.post("/api/command", headers={"Origin": bad_scheme, **auth_hdr}, json={"command": "status"})
            self.assertEqual(res.status_code, 403)

        # 10. Wrong port in Origin
        res = client.post("/api/command", headers={"Origin": "http://127.0.0.1:9999", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 403)

        # 11. Forged Origin
        res = client.post("/api/command", headers={"Origin": "http://attacker.com:20129", **auth_hdr}, json={"command": "status"})
        self.assertEqual(res.status_code, 403)

        # 12. Valid Origins (IPv4, IPv6, localhost, testserver)
        for valid_origin in (
            "http://127.0.0.1:20129",
            "http://localhost:20129",
            "http://[::1]:20129",
            "http://testserver",
        ):
            res = client.post("/api/command", headers={"Origin": valid_origin, **auth_hdr}, json={"command": "status"})
            self.assertEqual(res.status_code, 200, f"Expected 200 for origin {valid_origin}")

    def test_websocket_origin_validation_matrix(self):
        """Verifies WebSocket rejects invalid, forged, wrong-scheme, or wrong-port origins with 1008."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        client.cookies.set("jarvis_session", valid_token)

        # Forged origin -> fails
        with self.assertRaises(Exception):
            with client.websocket_connect("/ws", headers={"Origin": "http://attacker.com:20129"}) as ws:
                pass

        # Wrong port origin -> fails
        with self.assertRaises(Exception):
            with client.websocket_connect("/ws", headers={"Origin": "http://127.0.0.1:9999"}) as ws:
                pass

        # Wrong scheme origin -> fails
        with self.assertRaises(Exception):
            with client.websocket_connect("/ws", headers={"Origin": "https://127.0.0.1:20129"}) as ws:
                pass

        # Malformed origin -> fails
        with self.assertRaises(Exception):
            with client.websocket_connect("/ws", headers={"Origin": "null"}) as ws:
                pass

        # Valid origin -> connects successfully and receives telemetry
        with client.websocket_connect("/ws", headers={"Origin": "http://127.0.0.1:20129"}) as ws:
            data = ws.receive_json()
            self.assertEqual(data.get("type"), "telemetry")

    def test_tunnel_origin_and_host_configuration(self):
        """Verifies secure tunnel allows configured tunnel hosts and ports under JARVIS_SECURE_TUNNEL=1."""
        service = JarvisUIService(enforce_auth=True)
        service.orchestrator = MagicMock()
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        auth_hdr = {"X-Jarvis-Token": valid_token}

        old_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL")
        old_hosts = os.environ.get("JARVIS_ALLOWED_HOSTS")
        try:
            os.environ["JARVIS_SECURE_TUNNEL"] = "1"
            os.environ["JARVIS_ALLOWED_HOSTS"] = "hud.secure-tunnel.io"

            # Configured tunnel host and HTTPS origin accepted
            res = client.post(
                "/api/command",
                headers={
                    "Host": "hud.secure-tunnel.io:443",
                    "Origin": "https://hud.secure-tunnel.io",
                    **auth_hdr,
                },
                json={"command": "status"},
            )
            self.assertEqual(res.status_code, 200)

            # Untrusted host still rejected
            res_bad = client.post(
                "/api/command",
                headers={"Host": "untrusted.com", **auth_hdr},
                json={"command": "status"},
            )
            self.assertEqual(res_bad.status_code, 403)
        finally:
            if old_tunnel is not None:
                os.environ["JARVIS_SECURE_TUNNEL"] = old_tunnel
            else:
                os.environ.pop("JARVIS_SECURE_TUNNEL", None)
            if old_hosts is not None:
                os.environ["JARVIS_ALLOWED_HOSTS"] = old_hosts
            else:
                os.environ.pop("JARVIS_ALLOWED_HOSTS", None)

    def test_approval_grant_endpoint_rejects_non_finite_ttl(self):
        """Verifies POST /api/approval/grant rejects non-finite, negative, and invalid TTLs with 400."""
        service = JarvisUIService(enforce_auth=True)
        client = TestClient(service._create_app())
        valid_token = get_hud_session_token()
        auth_hdr = {"X-Jarvis-Token": valid_token}

        invalid_ttls = ["nan", "inf", "-inf", -10, 0, "invalid_str"]
        for bad_ttl in invalid_ttls:
            payload = {
                "tool_name": "execute_powershell",
                "arguments": {"command": "dir"},
                "ttl_seconds": bad_ttl,
            }
            res = client.post("/api/approval/grant", headers=auth_hdr, json=payload)
            self.assertEqual(res.status_code, 400, f"Expected 400 for bad TTL {bad_ttl}")

        # Valid TTL succeeds
        valid_payload = {
            "tool_name": "execute_powershell",
            "arguments": {"command": "dir"},
            "ttl_seconds": 60,
        }
        res_ok = client.post("/api/approval/grant", headers=auth_hdr, json=valid_payload)
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json().get("status"), "granted")

    def test_access_log_redaction_live_rejected_request(self):
        """Verifies synthetic tokens in rejected requests are absent from captured Uvicorn access logs."""
        import io
        import logging
        import urllib.request
        import secrets

        test_port = 20177
        service = JarvisUIService(host="127.0.0.1", port=test_port, enforce_auth=True, access_log=True)
        synthetic_secret = "SYNTHETIC_LEAK_PROBE_TOKEN_" + secrets.token_hex(16)

        log_capture = io.StringIO()
        capture_handler = logging.StreamHandler(log_capture)
        capture_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))

        access_logger = logging.getLogger("uvicorn.access")
        orig_access_level = access_logger.level
        access_logger.setLevel(logging.INFO)

        ui_logger = logging.getLogger("Jarvis.UI")

        try:
            service.start()
            time.sleep(0.8)
            # Attach after server startup so uvicorn dictConfig does not overwrite
            access_logger.addHandler(capture_handler)
            ui_logger.addHandler(capture_handler)

            # Send rejected request containing synthetic secret in query string
            req = urllib.request.Request(f"http://127.0.0.1:{test_port}/?bootstrap={synthetic_secret}")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode("utf-8")
                self.assertIn("Interface Locked", html)

            time.sleep(0.3)
            logs = log_capture.getvalue()

            # Crucial verification: token must be completely absent from all captured log output
            self.assertNotIn(
                synthetic_secret,
                logs,
                f"Confidentiality breach: synthetic token leaked in server logs: {logs}",
            )
            self.assertIn("[REDACTED]", logs, "Expected [REDACTED] in access log output")

        finally:
            service.stop()
            access_logger.removeHandler(capture_handler)
            access_logger.setLevel(orig_access_level)
            ui_logger.removeHandler(capture_handler)

    def test_access_log_suppressed_by_default(self):
        """Verifies access logs are completely suppressed when access_log=False (default production mode)."""
        import io
        import logging
        import urllib.request

        test_port = 20178
        service = JarvisUIService(host="127.0.0.1", port=test_port, enforce_auth=True, access_log=False)

        log_capture = io.StringIO()
        capture_handler = logging.StreamHandler(log_capture)
        capture_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))

        access_logger = logging.getLogger("uvicorn.access")
        orig_access_level = access_logger.level

        try:
            service.start()
            time.sleep(0.8)
            access_logger.addHandler(capture_handler)

            req = urllib.request.Request(f"http://127.0.0.1:{test_port}/")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)

            time.sleep(0.3)
            logs = log_capture.getvalue()
            # In default production mode, access_logger does not log INFO access lines
            self.assertEqual(logs.strip(), "", f"Expected zero access logs in default mode, got: {logs}")
        finally:
            service.stop()
            access_logger.removeHandler(capture_handler)
            access_logger.setLevel(orig_access_level)

    def test_cookie_secure_attribute_across_deployment_modes(self):
        """Verifies Secure cookie attribute behavior across local loopback, direct HTTPS, and trusted reverse proxies."""
        service = JarvisUIService(enforce_auth=True)
        valid_token = get_hud_session_token()

        # Mode 1: Local HTTP Loopback (Secure=False for local browser compatibility)
        client_http = TestClient(service._create_app(), base_url="http://127.0.0.1:20129")
        res_http = client_http.post("/api/auth/unlock", json={"token": valid_token})
        self.assertEqual(res_http.status_code, 200)
        cookie_http = res_http.headers.get("set-cookie", "")
        self.assertIn("httponly", cookie_http.lower())
        self.assertIn("samesite=strict", cookie_http.lower())
        self.assertNotIn("secure", cookie_http.lower())

        # Mode 1B: Local HTTP with spoofed X-Forwarded-Proto from untrusted proxy (ignored -> Secure=False)
        res_spoof = client_http.post(
            "/api/auth/unlock",
            headers={"X-Forwarded-Proto": "https"},
            json={"token": valid_token},
        )
        cookie_spoof = res_spoof.headers.get("set-cookie", "")
        self.assertNotIn("secure", cookie_spoof.lower())

        # Mode 2: Direct HTTPS (Secure=True)
        client_https = TestClient(service._create_app(), base_url="https://127.0.0.1:20129")
        res_https = client_https.post("/api/auth/unlock", json={"token": valid_token})
        self.assertEqual(res_https.status_code, 200)
        cookie_https = res_https.headers.get("set-cookie", "")
        self.assertIn("secure", cookie_https.lower())
        self.assertIn("httponly", cookie_https.lower())

        # Mode 3: Trusted Reverse Proxy forwarding HTTPS (Secure=True)
        old_proxies = os.environ.get("JARVIS_TRUSTED_PROXIES")
        try:
            os.environ["JARVIS_TRUSTED_PROXIES"] = "127.0.0.1,testclient,testserver"
            client_proxy = TestClient(service._create_app(), base_url="http://127.0.0.1:20129")
            res_proxy = client_proxy.post(
                "/api/auth/unlock",
                headers={"X-Forwarded-Proto": "https"},
                json={"token": valid_token},
            )
            cookie_proxy = res_proxy.headers.get("set-cookie", "")
            self.assertIn("secure", cookie_proxy.lower())
            self.assertIn("httponly", cookie_proxy.lower())

            # RFC 7239 Forwarded header
            res_fwd = client_proxy.post(
                "/api/auth/unlock",
                headers={"Forwarded": "proto=https;for=192.0.2.1"},
                json={"token": valid_token},
            )
            cookie_fwd = res_fwd.headers.get("set-cookie", "")
            self.assertIn("secure", cookie_fwd.lower())
        finally:
            if old_proxies is not None:
                os.environ["JARVIS_TRUSTED_PROXIES"] = old_proxies
            else:
                os.environ.pop("JARVIS_TRUSTED_PROXIES", None)

        # Mode 4: Secure Tunnel mode (JARVIS_SECURE_TUNNEL=1 -> Secure=True when tunnel telemetry indicates HTTPS)
        old_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL")
        old_tunnel_port = os.environ.get("JARVIS_TUNNEL_PORT")
        try:
            os.environ["JARVIS_SECURE_TUNNEL"] = "1"
            os.environ["JARVIS_TUNNEL_PORT"] = "443"
            client_tunnel = TestClient(service._create_app(), base_url="http://127.0.0.1:20129")
            res_tunnel = client_tunnel.post(
                "/api/auth/unlock",
                headers={"X-Forwarded-Proto": "https"},
                json={"token": valid_token},
            )
            cookie_tunnel = res_tunnel.headers.get("set-cookie", "")
            self.assertIn("secure", cookie_tunnel.lower())
            self.assertIn("httponly", cookie_tunnel.lower())
        finally:
            if old_tunnel is not None:
                os.environ["JARVIS_SECURE_TUNNEL"] = old_tunnel
            else:
                os.environ.pop("JARVIS_SECURE_TUNNEL", None)
            if old_tunnel_port is not None:
                os.environ["JARVIS_TUNNEL_PORT"] = old_tunnel_port
            else:
                os.environ.pop("JARVIS_TUNNEL_PORT", None)

if __name__ == "__main__":
    unittest.main()
