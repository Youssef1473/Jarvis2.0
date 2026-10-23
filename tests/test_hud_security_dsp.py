"""Verification suite for Zero-Trust Security, WebGL 2.0 SDF Reactor, DSP Tap, and Win32 Overlay."""
import json
import os
import sys
import unittest
from unittest import mock
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
    generate_csp_nonce,
    render_template_with_nonce,
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
        self.assertIn("Content-Security-Policy-Report-Only", res_status_auth.headers)
        self.assertIn("Reporting-Endpoints", res_status_auth.headers)
        reporting_endpoints = res_status_auth.headers.get("Reporting-Endpoints", "")
        self.assertIn('csp-endpoint="http://', reporting_endpoints)
        self.assertIn("/api/csp-report", reporting_endpoints)
        # Absolute URL requirement: must NOT be a relative URL mapping
        self.assertNotEqual(reporting_endpoints, 'csp-endpoint="/api/csp-report"')
        self.assertIn("report-uri /api/csp-report", res_status_auth.headers.get("Content-Security-Policy"))
        self.assertIn("report-to csp-endpoint", res_status_auth.headers.get("Content-Security-Policy"))
        self.assertIn("report-uri /api/csp-report", res_status_auth.headers.get("Content-Security-Policy-Report-Only"))
        self.assertEqual(res_status_auth.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res_status_auth.headers.get("X-Content-Type-Options"), "nosniff")

    def test_reporting_endpoints_absolute_url_derivation(self):
        """Verifies Reporting-Endpoints header uses an absolute URL and rejects relative paths across local and tunnel configs."""
        from jarvis.ui.server import derive_absolute_reporting_endpoint

        service = JarvisUIService(host="127.0.0.1", port=20129)

        # 1. Local loopback default derivation
        local_endpoint = derive_absolute_reporting_endpoint(request=None, service=service)
        self.assertTrue(local_endpoint.startswith("http://"), "Local endpoint must be an absolute http:// URL")
        self.assertFalse(local_endpoint.startswith("/"), "Relative endpoint URL mapping is invalid under W3C Reporting API")
        self.assertEqual(local_endpoint, "http://127.0.0.1:20129/api/csp-report")

        # 2. Local loopback response header verification via client
        client = TestClient(service._create_app())
        res_local = client.get("/")
        rep_hdr = res_local.headers.get("Reporting-Endpoints", "")
        self.assertTrue(rep_hdr.startswith('csp-endpoint="http://'), "Reporting-Endpoints must emit absolute URL")
        self.assertNotIn('csp-endpoint="/api/csp-report"', rep_hdr, "Relative path mapping must not be emitted")

        # 3. Secure-tunnel configuration derivation (standard 443 HTTPS)
        with mock.patch.dict(os.environ, {"JARVIS_SECURE_TUNNEL": "1", "JARVIS_TUNNEL_HOST": "hud.stark.internal"}):
            tunnel_endpoint = derive_absolute_reporting_endpoint(request=None, service=service)
            self.assertEqual(tunnel_endpoint, "https://hud.stark.internal/api/csp-report")

            res_tunnel = client.get("/")
            tunnel_rep_hdr = res_tunnel.headers.get("Reporting-Endpoints", "")
            self.assertEqual(tunnel_rep_hdr, 'csp-endpoint="https://hud.stark.internal/api/csp-report"')

        # 4. Secure-tunnel configuration derivation with custom port
        with mock.patch.dict(os.environ, {
            "JARVIS_SECURE_TUNNEL": "1",
            "JARVIS_TUNNEL_HOST": "hud.stark.internal",
            "JARVIS_TUNNEL_PORT": "8443"
        }):
            tunnel_port_endpoint = derive_absolute_reporting_endpoint(request=None, service=service)
            self.assertEqual(tunnel_port_endpoint, "https://hud.stark.internal:8443/api/csp-report")

    def test_csp_report_endpoint_valid_legacy_payload(self):
        """Verifies /api/csp-report ingests application/csp-report format and records telemetry."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        report_payload = {
            "csp-report": {
                "document-uri": "http://127.0.0.1:20129/",
                "referrer": "",
                "violated-directive": "script-src 'self'",
                "effective-directive": "script-src",
                "original-policy": "default-src 'self'",
                "disposition": "report",
                "blocked-uri": "eval",
                "status-code": 200,
            }
        }
        res = client.post(
            "/api/csp-report",
            content=json.dumps(report_payload),
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res.status_code, 204)
        self.assertGreaterEqual(len(service._csp_violations_ring), 1)
        last_report = service._csp_violations_ring[-1]
        self.assertEqual(last_report["blocked_uri"], "eval")
        self.assertEqual(last_report["effective_directive"], "script-src")
        self.assertEqual(last_report["disposition"], "report")

    def test_csp_report_endpoint_valid_reporting_api_payload(self):
        """Verifies /api/csp-report ingests application/reports+json modern format."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        reports_payload = [
            {
                "type": "csp-violation",
                "age": 10,
                "url": "http://127.0.0.1:20129/hud",
                "body": {
                    "documentURL": "http://127.0.0.1:20129/hud",
                    "blockedURL": "http://untrusted-analytics.local/track",
                    "effectiveDirective": "connect-src",
                    "disposition": "report",
                },
            }
        ]
        res = client.post(
            "/api/csp-report",
            content=json.dumps(reports_payload),
            headers={"Content-Type": "application/reports+json"},
        )
        self.assertEqual(res.status_code, 204)
        self.assertGreaterEqual(len(service._csp_violations_ring), 1)
        last_report = service._csp_violations_ring[-1]
        self.assertEqual(last_report["blocked_uri"], "http://untrusted-analytics.local/track")
        self.assertEqual(last_report["effective_directive"], "connect-src")

    def test_csp_report_endpoint_unsupported_media_type(self):
        """Verifies /api/csp-report rejects non-reporting content types with HTTP 415."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res = client.post(
            "/api/csp-report",
            content="document-uri: http://localhost",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(res.status_code, 415)
        self.assertIn("Unsupported Media Type", res.json().get("error", ""))

    def test_csp_report_endpoint_oversized_payload_rejected(self):
        """Verifies /api/csp-report rejects oversized payloads (>32KB) with HTTP 413."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        huge_payload = json.dumps({"csp-report": {"blocked-uri": "x" * 35000}})
        res = client.post(
            "/api/csp-report",
            content=huge_payload,
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res.status_code, 413)
        self.assertIn("Payload Too Large", res.json().get("error", ""))

    def test_csp_report_endpoint_malformed_json_rejected(self):
        """Verifies /api/csp-report rejects malformed JSON with HTTP 400."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res = client.post(
            "/api/csp-report",
            content="{bad-json: not valid}",
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid JSON", res.json().get("error", ""))

    def test_csp_report_endpoint_invalid_content_length_rejected(self):
        """Verifies /api/csp-report rejects negative or non-integer Content-Length headers with HTTP 400."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res_neg = client.post(
            "/api/csp-report",
            content=json.dumps({"csp-report": {}}),
            headers={"Content-Type": "application/csp-report", "Content-Length": "-5"},
        )
        self.assertEqual(res_neg.status_code, 400)

        res_invalid = client.post(
            "/api/csp-report",
            content=json.dumps({"csp-report": {}}),
            headers={"Content-Type": "application/csp-report", "Content-Length": "not-a-number"},
        )
        self.assertEqual(res_invalid.status_code, 400)

    def test_csp_report_endpoint_primitive_json_rejected(self):
        """Verifies /api/csp-report rejects primitive JSON (integers, strings, booleans) with HTTP 400."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res_int = client.post(
            "/api/csp-report",
            content="12345",
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res_int.status_code, 400)
        self.assertIn("expected object or array", res_int.json().get("error", ""))

        res_str = client.post(
            "/api/csp-report",
            content='"just-a-string"',
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res_str.status_code, 400)

    def test_csp_report_endpoint_nested_structure_validation(self):
        """Verifies /api/csp-report validates csp-report structure is a dictionary."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res = client.post(
            "/api/csp-report",
            content=json.dumps({"csp-report": "not-a-dict"}),
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid CSP report structure", res.json().get("error", ""))

    def test_csp_report_log_injection_sanitization(self):
        """Verifies CRLF and ANSI control characters in report fields are stripped to prevent log poisoning."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        malicious_payload = {
            "csp-report": {
                "document-uri": "http://127.0.0.1:20129/\r\nADMIN_OVERRIDE: True\x1b[31m",
                "violated-directive": "script-src\r\nPOISONED_HEADER: 1",
                "effective-directive": "script-src",
                "blocked-uri": "eval\x00\x08",
            }
        }
        res = client.post(
            "/api/csp-report",
            content=json.dumps(malicious_payload),
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res.status_code, 204)
        last_report = service._csp_violations_ring[-1]
        self.assertNotIn("\r", last_report["document_uri"])
        self.assertNotIn("\n", last_report["document_uri"])
        self.assertNotIn("\x1b", last_report["document_uri"])
        self.assertNotIn("\x00", last_report["blocked_uri"])
        self.assertEqual(last_report["blocked_uri"], "eval")

    def test_csp_report_endpoint_empty_body_rejected(self):
        """Verifies /api/csp-report rejects empty bodies with HTTP 400."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res = client.post(
            "/api/csp-report",
            content="",
            headers={"Content-Type": "application/csp-report"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Empty report body", res.json().get("error", ""))

    def test_csp_report_origin_validation_fail_closed(self):
        """Verifies /api/csp-report enforces host and origin validation against unauthorized external origins."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        res = client.post(
            "/api/csp-report",
            content=json.dumps({"csp-report": {"blocked-uri": "eval"}}),
            headers={
                "Content-Type": "application/csp-report",
                "Origin": "http://malicious-external-site.com",
            },
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Content-Security-Policy", res.headers)

    def test_csp_report_rate_limiting(self):
        """Verifies /api/csp-report rejects excessive submissions with HTTP 429 when rate limit is exceeded."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        payload = json.dumps({"csp-report": {"blocked-uri": "eval"}})
        headers = {"Content-Type": "application/csp-report"}

        # Simulate filling rate limiter bucket (60 requests)
        with service._csp_report_lock:
            now = time.monotonic()
            for _ in range(60):
                service._csp_report_timestamps.append(now)

        res = client.post("/api/csp-report", content=payload, headers=headers)
        self.assertEqual(res.status_code, 429)
        self.assertIn("rate limit exceeded", res.json().get("error", ""))

    def test_csp_report_batch_rate_limiting_effective_maximum(self):
        """Verifies rate limiter accounts for individual reports in application/reports+json batches (60 reports/min limit)."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        def _make_batch(start_idx: int, count: int = 10):
            return [
                {
                    "type": "csp-violation",
                    "body": {
                        "documentURL": f"http://127.0.0.1:20129/page_{i}",
                        "blockedURL": f"http://blocked.local/{i}",
                        "effectiveDirective": "connect-src",
                        "disposition": "report",
                    },
                }
                for i in range(start_idx, start_idx + count)
            ]

        # 6 batches of 10 reports = 60 reports total (consumes full budget)
        for b_idx in range(6):
            batch = _make_batch(b_idx * 10, 10)
            res = client.post(
                "/api/csp-report",
                content=json.dumps(batch),
                headers={"Content-Type": "application/reports+json"},
            )
            self.assertEqual(res.status_code, 204, f"Batch {b_idx} must succeed within 60 report budget")

        # 7th batch: even a single report exceeds the effective maximum and is rejected with 429
        overflow_batch = _make_batch(60, 1)
        res_overflow = client.post(
            "/api/csp-report",
            content=json.dumps(overflow_batch),
            headers={"Content-Type": "application/reports+json"},
        )
        self.assertEqual(res_overflow.status_code, 429)
        self.assertIn("rate limit exceeded", res_overflow.json().get("error", ""))

    def test_csp_report_deduplication(self):
        """Verifies repeated identical reports within 10s are deduplicated without inflating ring buffer."""
        service = JarvisUIService()
        client = TestClient(service._create_app())

        payload = json.dumps({
            "csp-report": {
                "document-uri": "http://127.0.0.1:20129/",
                "violated-directive": "script-src",
                "effective-directive": "script-src",
                "blocked-uri": "eval",
            }
        })
        headers = {"Content-Type": "application/csp-report"}

        res1 = client.post("/api/csp-report", content=payload, headers=headers)
        self.assertEqual(res1.status_code, 204)
        count_after_first = len(service._csp_violations_ring)

        res2 = client.post("/api/csp-report", content=payload, headers=headers)
        self.assertEqual(res2.status_code, 204)
        # Count should remain unchanged due to deduplication cache
        self.assertEqual(len(service._csp_violations_ring), count_after_first)

    def test_csp_headers_tunnel_configuration(self):
        """Verifies connect-src includes trusted tunnel origin when JARVIS_SECURE_TUNNEL=1 is set."""
        service = JarvisUIService()
        with mock.patch.dict(os.environ, {"JARVIS_SECURE_TUNNEL": "1", "JARVIS_TUNNEL_HOST": "hud.stark.internal"}):
            client = TestClient(service._create_app())
            res = client.get("/")
            self.assertEqual(res.status_code, 200)

            csp = res.headers.get("Content-Security-Policy", "")
            csp_report = res.headers.get("Content-Security-Policy-Report-Only", "")
            self.assertIn("https://hud.stark.internal", csp)
            self.assertIn("wss://hud.stark.internal", csp)
            self.assertIn("https://hud.stark.internal", csp_report)
            self.assertIn("wss://hud.stark.internal", csp_report)

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

    def test_hud_template_contains_zero_inline_event_handlers(self):
        """Verifies Phase 3 CSP requirement: zero inline DOM event handlers in HUD template."""
        from pathlib import Path
        import re

        template_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        self.assertTrue(template_path.exists(), f"HUD template missing at {template_path}")

        template_text = template_path.read_text(encoding="utf-8")
        handler_pattern = re.compile(r"""(?:\s|\b)(on[a-zA-Z]+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
        matches = [m.group(0).strip() for m in handler_pattern.finditer(template_text)]

        self.assertEqual(
            len(matches),
            0,
            f"Expected zero inline event handlers in hud.html, but found {len(matches)}: {matches[:10]}",
        )

        # Confirm centralized event delegation architecture is wired
        self.assertIn("function initDomEvents()", template_text)
        self.assertIn('data-action="toggle-mute"', template_text)
        self.assertIn('data-action="quick-command"', template_text)

    def test_hud_event_architecture_and_attribute_escaping(self):
        """Verifies idempotence, nested propagation isolation, and HTML attribute context escaping."""
        from pathlib import Path
        import subprocess

        template_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        template_text = template_path.read_text(encoding="utf-8")

        # 1. Idempotence guard verification
        self.assertIn("let domEventsInitialized = false;", template_text)
        self.assertIn("if (domEventsInitialized) return;", template_text)
        self.assertIn("domEventsInitialized = true;", template_text)

        # 2. Priority nested event propagation isolation checks
        del_cal_idx = template_text.find('data-action="prompt-delete-calendar"')
        del_event_idx = template_text.find('data-action="delete-calendar-event"')
        general_action_idx = template_text.find("const actionEl = e.target.closest('[data-action]');")

        self.assertGreater(del_cal_idx, 0)
        self.assertGreater(del_event_idx, 0)
        self.assertGreater(general_action_idx, 0)
        self.assertLess(del_cal_idx, general_action_idx, "Inner calendar delete must precede general action dispatch")
        self.assertLess(del_event_idx, general_action_idx, "Inner event delete must precede general action dispatch")

        # 3. HTML attribute escaping on user-controlled fields
        self.assertIn('data-key="${escapeHtml(key)}"', template_text)
        self.assertIn('data-id="${escapeHtml(p.key)}"', template_text)
        self.assertIn('data-command="Jarvis, cancel timer ${escapeHtml(t.label)}"', template_text)
        self.assertIn('data-filename="${escapeHtml(doc.filename)}"', template_text)
        self.assertIn('data-title="${escapeHtml(ev.title)}"', template_text)
        self.assertIn('data-title="${escapeHtml(doc.title)}"', template_text)
        self.assertIn('data-url="${escapeHtml(url)}"', template_text)

        # 4. Syntactic validity of embedded script via Node.js (when available)
        import shutil

        s_open = template_text.find("<script")
        self.assertGreater(s_open, 0)
        s_idx = template_text.find(">", s_open)
        end_tag = "</script>"
        e_idx = template_text.rfind(end_tag)
        self.assertGreater(s_idx, s_open)
        self.assertGreater(e_idx, s_idx)
        js_code = template_text[s_idx + 1:e_idx]

        node_bin = shutil.which("node")
        if node_bin:
            node_proc = subprocess.run(
                [node_bin, "--check"],
                input=js_code,
                text=True,
                encoding="utf-8",
                capture_output=True,
                shell=False,
            )
            self.assertEqual(node_proc.returncode, 0, f"Node syntax check failed: {node_proc.stderr}")

    def test_csp_nonce_generation_uniqueness_and_format(self):
        """Verifies cryptographically secure 32-byte Base64 nonce generation, entropy, and templating."""
        import math
        import re
        from collections import Counter

        # 1. Format check: 32 bytes Base64-encoded is exactly 44 characters ending with '='
        nonce_re = re.compile(r"^[A-Za-z0-9+/]{43}=$")
        nonces = [generate_csp_nonce() for _ in range(100)]

        for n in nonces:
            self.assertEqual(len(n), 44)
            self.assertTrue(nonce_re.match(n), f"Nonce {n} does not match Base64 format")

        # 2. Uniqueness: 100 independently generated nonces must be completely unique
        self.assertEqual(len(set(nonces)), 100, "Collision detected in generated CSP nonces")

        # 3. Entropy: verify character distribution over generated nonces has high entropy
        all_chars = "".join(nonces)
        counts = Counter(all_chars)
        total = len(all_chars)
        entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
        self.assertGreater(entropy, 5.0, f"Shannon entropy {entropy:.2f} is suspiciously low for Base64 randomness")

        # 4. Template substitution helper verification
        mock_template = (
            '<!DOCTYPE html><html><head>'
            '<style nonce="{{ csp_nonce }}">body { margin: 0; }</style>'
            '<link rel="stylesheet" href="style.css">'
            '</head><body>'
            '<div style="color: blue;">Text</div>'
            '<script nonce="{{ csp_nonce }}">console.log("hello");</script>'
            '</body></html>'
        )
        sample_nonce = nonces[0]
        rendered = render_template_with_nonce(mock_template, sample_nonce)
        self.assertIn(f'<style nonce="{sample_nonce}">', rendered)
        self.assertIn(f'<script nonce="{sample_nonce}">', rendered)
        self.assertIn('<div style="color: blue;">', rendered, "Inline style attributes must be preserved untouched")
        self.assertNotIn("{{ csp_nonce }}", rendered)

        # Empty nonce must cleanly remove placeholder without leaving stray attributes
        unnonced = render_template_with_nonce(mock_template, "")
        self.assertIn("<style>body", unnonced)
        self.assertIn("<script>console", unnonced)
        self.assertNotIn("nonce=", unnonced)

    def test_csp_nonce_authenticated_hud_body_and_headers_matching(self):
        """Verifies authenticated GET / injects matching nonces into HTML body tags and CSP headers."""
        import re

        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        client.cookies.set("jarvis_session", get_hud_session_token())
        response = client.get("/")
        self.assertEqual(response.status_code, 200)

        # Verify anti-caching headers on HTML responses with nonces
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")

        csp_enforced = response.headers.get("Content-Security-Policy", "")
        csp_report_only = response.headers.get("Content-Security-Policy-Report-Only", "")

        # Extract nonces from headers
        m_enforced = re.search(r"script-src-elem [^;]*'nonce-([A-Za-z0-9+/=]+)'", csp_enforced)
        m_ro = re.search(r"script-src-elem [^;]*'nonce-([A-Za-z0-9+/=]+)'", csp_report_only)

        self.assertIsNotNone(m_enforced, "Enforced CSP missing nonce in script-src-elem")
        self.assertIsNotNone(m_ro, "Report-only CSP missing nonce in script-src-elem")

        nonce_header = m_enforced.group(1)
        self.assertEqual(nonce_header, m_ro.group(1), "Enforced and report-only nonces must match for the same response")

        # Verify HTML body script and style tags have the exact same nonce
        body = response.text
        self.assertIn(f'<style nonce="{nonce_header}">', body, "Top-level <style> must contain matching nonce")
        self.assertIn(f'<script nonce="{nonce_header}">', body, "Top-level <script> must contain matching nonce")
        self.assertNotIn("{{ csp_nonce }}", body, "Unreplaced template placeholder in HUD body")
        self.assertNotIn("csp_nonce", body, "Unreplaced csp_nonce keyword in HUD body")

    def test_csp_nonce_unauthenticated_unlock_page_and_headers_matching(self):
        """Verifies unauthenticated GET / injects matching nonces and renders lock form without inline handlers."""
        import re

        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        response = client.get("/")
        self.assertEqual(response.status_code, 200)

        # Verify anti-caching headers
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")

        csp_enforced = response.headers.get("Content-Security-Policy", "")
        m_enforced = re.search(r"script-src-elem [^;]*'nonce-([A-Za-z0-9+/=]+)'", csp_enforced)
        self.assertIsNotNone(m_enforced, "Enforced CSP missing nonce on unlock page")
        nonce = m_enforced.group(1)

        body = response.text
        self.assertIn(f'<style nonce="{nonce}">', body, "Unlock page <style> must contain matching nonce")
        self.assertIn(f'<script nonce="{nonce}">', body, "Unlock page <script> must contain matching nonce")
        self.assertNotIn("{{ csp_nonce }}", body)

        # Confirm form has no inline onsubmit and uses JavaScript event listener
        self.assertNotIn("onsubmit", body.lower(), "Unlock page form must not contain inline onsubmit")
        self.assertIn("unlockForm.addEventListener('submit', unlock)", body)

    def test_unlock_flow_and_session_cookie_exchange(self):
        """Verifies bootstrap and session credentials exchange via /api/auth/unlock without errors."""
        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        # 1. Invalid credential exchange fails
        res_fail = client.post("/api/auth/unlock", json={"token": "invalid_bogus_token"})
        self.assertEqual(res_fail.status_code, 401)
        self.assertNotIn("jarvis_session", client.cookies)

        # 2. Single-use bootstrap token exchange succeeds and sets session cookie
        boot_token = create_bootstrap_token(ttl_seconds=30.0)
        res_ok = client.post("/api/auth/unlock", json={"token": boot_token})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json().get("status"), "authenticated")
        self.assertIn("jarvis_session", client.cookies)

        # 3. Bootstrap token is single-use; subsequent replay attempt fails
        res_replay = client.post("/api/auth/unlock", json={"token": boot_token})
        self.assertEqual(res_replay.status_code, 401)

        # 4. Authenticated client now loads full HUD on GET /
        res_hud = client.get("/")
        self.assertEqual(res_hud.status_code, 200)
        self.assertIn("Sovereign Command Bridge", res_hud.text)

    def test_csp_nonce_present_across_error_and_api_responses(self):
        """Verifies well-formed CSP headers with valid nonces on 401, 403, and 200 API responses."""
        import re

        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        nonce_pat = re.compile(r"'nonce-([A-Za-z0-9+/]{43}=)'")

        # 1. 401 Unauthorized API error
        res_401 = client.post("/api/command", json={"command": "ping"})
        self.assertEqual(res_401.status_code, 401)
        self.assertIn("Content-Security-Policy", res_401.headers)
        self.assertTrue(nonce_pat.search(res_401.headers["Content-Security-Policy"]))

        # 2. 403 Forbidden Origin validation error
        res_403 = client.post(
            "/api/command",
            headers={"X-Jarvis-Token": get_hud_session_token(), "Origin": "http://evil-attacker.com"},
            json={"command": "ping"},
        )
        self.assertEqual(res_403.status_code, 403)
        self.assertIn("Content-Security-Policy", res_403.headers)
        self.assertTrue(nonce_pat.search(res_403.headers["Content-Security-Policy"]))

        # 3. 200 OK JSON status endpoint
        res_200 = client.get("/api/status", headers={"X-Jarvis-Token": get_hud_session_token()})
        self.assertEqual(res_200.status_code, 200)
        self.assertIn("Content-Security-Policy", res_200.headers)
        self.assertTrue(nonce_pat.search(res_200.headers["Content-Security-Policy"]))

    def test_csp_enforced_vs_report_only_policy_directives(self):
        """Verifies policy directive distinctions: unsafe-eval, Level 3 script/style-src-*, and reporting."""
        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        res = client.get("/api/status", headers={"X-Jarvis-Token": get_hud_session_token()})
        self.assertEqual(res.status_code, 200)

        csp = res.headers["Content-Security-Policy"]
        csp_ro = res.headers["Content-Security-Policy-Report-Only"]

        # Enforced retains unsafe-eval; Report-Only strictly excludes it
        self.assertIn("'unsafe-eval'", csp, "Enforced policy must retain unsafe-eval during transitional phase")
        self.assertNotIn("'unsafe-eval'", csp_ro, "Report-only policy must not contain unsafe-eval")

        # Intentional difference in inline event handler allowances:
        # Enforced policy permits script-src-attr 'unsafe-inline' during transition
        # Report-Only policy strictly enforces script-src-attr 'none' to report regressions
        self.assertIn("script-src-attr 'unsafe-inline'", csp)
        self.assertIn("script-src-attr 'none'", csp_ro)
        self.assertNotIn("script-src-attr 'unsafe-inline'", csp_ro)

        # Both policies must feature explicit Level 3 element directives and style attribute allowances
        for policy in (csp, csp_ro):
            self.assertIn("script-src-elem", policy)
            self.assertIn("style-src-elem", policy)
            self.assertIn("style-src-attr 'unsafe-inline'", policy)
            # Level 2 fallback: style-src must include 'unsafe-inline' and NOT include 'nonce-'
            # to prevent Level 2 browsers from blocking HUD inline style="..." attributes
            style_src_part = [part.strip() for part in policy.split(";") if part.strip().startswith("style-src ")][0]
            self.assertIn("'unsafe-inline'", style_src_part)
            self.assertNotIn("'nonce-", style_src_part, "style-src fallback must not have nonce to protect style attributes")

            # Reporting directives present
            self.assertIn("report-uri /api/csp-report", policy)
            self.assertIn("report-to csp-endpoint", policy)

        # Reporting-Endpoints header present and absolute
        rep_ep = res.headers.get("Reporting-Endpoints", "")
        self.assertIn('csp-endpoint="http://testserver/api/csp-report"', rep_ep)

    def test_csp_missing_template_404_has_anti_cache_headers(self):
        """Verifies missing template 404 HTML response also carries strict anti-caching headers."""
        service = JarvisUIService(enforce_auth=False)
        service.template_path = Path("nonexistent_hud_template.html")
        app = service._create_app()
        client = TestClient(app)

        res = client.get("/")
        self.assertEqual(res.status_code, 404)
        self.assertIn("HUD template not found", res.text)
        self.assertEqual(res.headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")
        self.assertEqual(res.headers.get("Pragma"), "no-cache")
        self.assertEqual(res.headers.get("Expires"), "0")
        self.assertIn("Content-Security-Policy", res.headers)

    def test_csp_nonce_concurrency_isolation(self):
        """Verifies concurrent requests receive independent, isolated nonces without cross-contamination."""
        import concurrent.futures
        import re

        service = JarvisUIService(enforce_auth=True)
        app = service._create_app()
        client = TestClient(app)

        def make_request(idx: int):
            # Mix authenticated and unauthenticated calls
            is_auth = (idx % 2 == 0)
            headers = {"X-Jarvis-Token": get_hud_session_token()} if is_auth else {}
            resp = client.get("/", headers=headers)
            csp = resp.headers.get("Content-Security-Policy", "")
            m = re.search(r"script-src-elem [^;]*'nonce-([A-Za-z0-9+/=]+)'", csp)
            h_nonce = m.group(1) if m else None

            # Extract body nonce from <style nonce="...">
            m_body = re.search(r'<style nonce="([A-Za-z0-9+/=]+)">', resp.text)
            b_nonce = m_body.group(1) if m_body else None
            return (idx, h_nonce, b_nonce)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(make_request, i) for i in range(24)]
            results = [f.result() for f in futures]

        header_nonces = [r[1] for r in results]
        body_nonces = [r[2] for r in results]

        # All 24 requests must receive non-None nonces
        self.assertTrue(all(n is not None for n in header_nonces))
        self.assertTrue(all(n is not None for n in body_nonces))

        # In every request, the body tag nonce must match the header nonce
        for idx, h_n, b_n in results:
            self.assertEqual(h_n, b_n, f"Request {idx} had mismatched header and body nonces")

        # Every concurrent request must receive a distinct nonce (no reuse or cross-contamination)
        self.assertEqual(len(set(header_nonces)), len(header_nonces), "Nonce collision detected across concurrent requests")

    def test_zero_inline_event_handlers_in_hud_and_unlock_templates(self):
        """Verifies zero inline on* event handler attributes exist in hud.html and the unlock page."""
        import re
        from pathlib import Path

        hud_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        hud_text = hud_path.read_text(encoding="utf-8")

        service = JarvisUIService()
        unlock_text = service._get_unlock_html(nonce="test_nonce")

        # Match inline event handlers in HTML markup: whitespace + on<event> =
        # Excludes JS occurrences like .on("click") or onmessage = in script blocks
        html_only_hud = hud_text[:hud_text.find("<script")]
        handler_re = re.compile(r"""\son[a-zA-Z]+\s*=""", re.IGNORECASE)

        hud_matches = handler_re.findall(html_only_hud)
        self.assertEqual(len(hud_matches), 0, f"Found inline event handlers in HUD template HTML: {hud_matches}")

        unlock_html_only = unlock_text[:unlock_text.find("<script")]
        unlock_matches = handler_re.findall(unlock_html_only)
        self.assertEqual(len(unlock_matches), 0, f"Found inline event handlers in unlock page HTML: {unlock_matches}")

if __name__ == "__main__":
    unittest.main()
