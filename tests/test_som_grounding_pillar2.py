"""Unit test suite for Pillar 2: Set-of-Marks (SoM) Semantic UI Grounding in Jarvis 2.0."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

from jarvis.vision.som_grounding import (
    SetOfMarkAnnotator,
    MarkRegistry,
    MarkElement,
    is_window_privacy_blocked,
    is_sensitive_element,
    enable_per_monitor_v2_dpi,
)


class TestSoMGroundingPillar2(unittest.TestCase):
    """Verifies DPI v2, privacy blocking, password field masking, negative coords, and anti-TOCTOU click_element."""

    def setUp(self):
        MarkRegistry.clear()

    def tearDown(self):
        MarkRegistry.clear()

    def test_dpi_v2_enablement(self):
        """Verifies enable_per_monitor_v2_dpi returns boolean without crashing."""
        res = enable_per_monitor_v2_dpi()
        self.assertIsInstance(res, bool)

    def test_window_privacy_guard_blocks_sensitive_and_exempts_ides(self):
        """Window-level privacy guard must block password managers and banking while exempting IDEs."""
        self.assertTrue(is_window_privacy_blocked(title="Bitwarden - Vault", proc_name="Bitwarden.exe"))
        self.assertTrue(is_window_privacy_blocked(title="1Password - Sign In", proc_name="1password.exe"))
        self.assertTrue(is_window_privacy_blocked(title="Chase Online Banking - Chrome", proc_name="chrome.exe"))

        # Developer files in IDE must be exempt
        self.assertFalse(is_window_privacy_blocked(title="login.py - Visual Studio Code", proc_name="Code.exe"))
        self.assertFalse(is_window_privacy_blocked(title="banking_service.ts - Antigravity", proc_name="antigravity.exe"))

    def test_annotate_window_raises_permission_error_on_blocked_window(self):
        """annotate_window must raise PermissionError if foreground window is classified as sensitive."""
        annotator = SetOfMarkAnnotator()
        with patch.object(annotator, "_get_window_title", return_value="Bitwarden - Master Password"):
            with patch("ctypes.windll.user32.GetForegroundWindow", return_value=1234):
                with patch("ctypes.windll.user32.IsWindow", return_value=1):
                    with self.assertRaises(PermissionError):
                        annotator.annotate_window(hwnd=1234)

    def test_negative_secondary_monitor_coordinate_mapping_and_password_redaction(self):
        """Must correctly map secondary monitor with negative coords and redact password fields with (10, 14, 23)."""
        annotator = SetOfMarkAnnotator(min_size=12)
        # Secondary monitor window at negative screen offset (-1920, 0, 0, 1080)
        win_rect = (-1920, 0, 0, 1080)
        img = Image.new("RGB", (1920, 1080), color=(50, 50, 50))

        controls = [
            # Normal button at (-1820, 100, -1720, 140)
            {"control_type": "Button", "name": "Submit", "rect": (-1820, 100, -1720, 140)},
            # Sensitive password field at (-1820, 200, -1620, 240)
            {"control_type": "Edit", "name": "User Password", "auto_id": "txt_password", "rect": (-1820, 200, -1620, 240), "is_password": True},
        ]

        t0 = time_start = sys.modules["time"].perf_counter()
        annotated_img, marks, manifest = annotator.annotate(
            image=img,
            win_rect=win_rect,
            hwnd=777,
            raw_controls=controls,
        )
        elapsed_ms = (sys.modules["time"].perf_counter() - time_start) * 1000.0

        # Badge rendering must take < 250ms under heavy test runner load
        self.assertLess(elapsed_ms, 250.0)

        # Relative coordinates must be normalized cleanly into local image frame
        self.assertEqual(marks[1].local_rect, (100, 100, 200, 140))
        self.assertEqual(marks[2].local_rect, (100, 200, 300, 240))

        # Check pixel redaction on password field center (local coords: 200, 220)
        pixel = annotated_img.getpixel((200, 220))
        self.assertEqual(pixel, (10, 14, 23), "Password field must be masked with solid #0A0E17 (10, 14, 23)")

    def test_click_element_native_uia_invoke(self):
        """click_element must invoke uia_ref.invoke() without touching mouse when available."""
        mock_uia = MagicMock()
        mock_mark = MarkElement(
            mark_id=1,
            control_type="Button",
            name="Save",
            auto_id="btn_save",
            local_rect=(10, 10, 50, 50),
            screen_rect=(10, 10, 50, 50),
            local_center=(30, 30),
            screen_center=(30, 30),
            hwnd=999,
            snapshot_win_rect=(0, 0, 500, 500),
            uia_ref=mock_uia,
        )
        MarkRegistry.set_marks({1: mock_mark})

        with patch("ctypes.windll.user32.IsWindow", return_value=1), \
             patch("ctypes.windll.user32.GetForegroundWindow", return_value=999):
            with patch("ctypes.windll.user32.GetWindowRect") as mock_rect:
                # Same geometry (no drift)
                mock_rect.side_effect = lambda h, r: setattr(r._obj, 'left', 0) or setattr(r._obj, 'top', 0) or setattr(r._obj, 'right', 500) or setattr(r._obj, 'bottom', 500)
                res = SetOfMarkAnnotator.click_element(1, verify_window_geometry=True)

        mock_uia.invoke.assert_called_once()
        self.assertEqual(res["status"], "clicked")
        self.assertEqual(res["method"], "uia_invoke")

    def test_anti_toctou_geometry_drift_detection(self):
        """click_element must reject clicks if window shifted by > 8px."""
        mock_mark = MarkElement(
            mark_id=2,
            control_type="Button",
            name="Delete",
            auto_id="btn_del",
            local_rect=(10, 10, 50, 50),
            screen_rect=(10, 10, 50, 50),
            local_center=(30, 30),
            screen_center=(30, 30),
            hwnd=999,
            snapshot_win_rect=(100, 100, 600, 600),
            uia_ref=None,
        )
        MarkRegistry.set_marks({2: mock_mark})

        with patch("ctypes.windll.user32.IsWindow", return_value=1), \
             patch("ctypes.windll.user32.GetForegroundWindow", return_value=999):
            with patch("ctypes.windll.user32.GetWindowRect") as mock_rect:
                # Window shifted by 20px (100 -> 120)
                mock_rect.side_effect = lambda h, r: setattr(r._obj, 'left', 120) or setattr(r._obj, 'top', 100) or setattr(r._obj, 'right', 620) or setattr(r._obj, 'bottom', 600)
                res = SetOfMarkAnnotator.click_element(2, verify_window_geometry=True)

        self.assertEqual(res["status"], "geometry_drift")
        self.assertIn("shifted by > 8px", res["error"])


if __name__ == "__main__":
    unittest.main()
