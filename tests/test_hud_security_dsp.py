"""Verification suite for Zero-Trust Security, WebGL 2.0 SDF Reactor, DSP Tap, and Win32 Overlay."""
import os
import sys
import unittest
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

        # 4. Valid token in query param -> authorized
        res_auth_query = client.post(
            f"/api/command?token={valid_token}",
            json={"command": "status"},
        )
        self.assertIn(res_auth_query.status_code, (200, 503))

        # 5. Valid token in jarvis_session cookie -> authorized
        res_auth_cookie = client.post(
            "/api/command",
            cookies={"jarvis_session": valid_token},
            json={"command": "status"},
        )
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

        # Test API status route headers
        res_status = client.get("/api/status")
        self.assertEqual(res_status.status_code, 200)
        self.assertIn("Content-Security-Policy", res_status.headers)
        self.assertEqual(res_status.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res_status.headers.get("X-Content-Type-Options"), "nosniff")

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


if __name__ == "__main__":
    unittest.main()
