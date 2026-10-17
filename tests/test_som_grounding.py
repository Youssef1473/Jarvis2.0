"""Unit tests for Set-of-Mark (SoM) Visual Grounding and UI Automation Fusion."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from PIL import Image

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.vision.som_grounding import (
    SetOfMarkAnnotator,
    MarkRegistry,
    MarkElement,
)
from jarvis.skills.screen_vision import click_marked_element


class TestSoMGrounding(unittest.TestCase):
    """Verifies SetOfMarkAnnotator, relative coordinate calculation, size filtering, and staleness gating."""

    def setUp(self):
        MarkRegistry.clear()

    def tearDown(self):
        MarkRegistry.clear()

    def test_annotate_copies_image_and_does_not_mutate_original(self):
        """Verifies annotating an image returns a fresh copy without modifying source image pixels."""
        orig_image = Image.new("RGB", (400, 300), color=(50, 50, 50))
        orig_bytes = orig_image.tobytes()

        annotator = SetOfMarkAnnotator()
        win_rect = (100, 100, 500, 400)
        controls = [
            {"control_type": "Button", "name": "OK", "auto_id": "btn_ok", "rect": (150, 150, 200, 180)}
        ]

        annotated_img, marks, manifest = annotator.annotate(
            image=orig_image,
            win_rect=win_rect,
            hwnd=999,
            raw_controls=controls,
            win_title="Test App",
        )

        self.assertEqual(orig_image.tobytes(), orig_bytes, "Source image must not be mutated in-place")
        self.assertNotEqual(annotated_img.tobytes(), orig_bytes, "Annotated image must have rendered badges")
        self.assertEqual(len(marks), 1)
        self.assertIn("[1]", manifest)

    def test_relative_coordinate_computation_with_virtual_desktop_offset(self):
        """Mandatory Constraint 2: Compute badge coordinates relative to captured window origin."""
        annotator = SetOfMarkAnnotator()
        # Simulated secondary monitor window placed at physical screen offset (1920, 100)
        win_rect = (1920, 100, 2920, 800)
        controls = [
            # Button placed at physical screen coords (2020, 200, 2120, 240)
            {"control_type": "Button", "name": "Save File", "auto_id": "btn_save", "rect": (2020, 200, 2120, 240)}
        ]

        img = Image.new("RGB", (1000, 700), color=(100, 100, 100))
        _, marks, _ = annotator.annotate(
            image=img,
            win_rect=win_rect,
            hwnd=1234,
            raw_controls=controls,
        )

        mark = marks[1]
        # Relative coordinates must be:
        # rel_left = 2020 - 1920 = 100
        # rel_top = 200 - 100 = 100
        # rel_right = 2120 - 1920 = 200
        # rel_bottom = 240 - 100 = 140
        self.assertEqual(mark.local_rect, (100, 100, 200, 140))
        self.assertEqual(mark.local_center, (150, 120))
        # Absolute screen coordinates preserved for physical clicking
        self.assertEqual(mark.screen_rect, (2020, 200, 2120, 240))
        self.assertEqual(mark.screen_center, (2070, 220))
        self.assertEqual(mark.hwnd, 1234)
        self.assertEqual(mark.snapshot_win_rect, win_rect)

    def test_minimum_size_filtering_skips_sub_12px_elements(self):
        """Mandatory Constraint 2: Cap UIA traversal with minimum size 12x12px."""
        annotator = SetOfMarkAnnotator(min_size=12)
        win_rect = (0, 0, 500, 500)
        controls = [
            # Width = 8px (< 12) -> must be skipped
            {"control_type": "Button", "name": "Tiny W", "rect": (10, 10, 18, 30)},
            # Height = 10px (< 12) -> must be skipped
            {"control_type": "Button", "name": "Tiny H", "rect": (20, 20, 40, 30)},
            # Width = 15, Height = 15 (>= 12) -> must be included
            {"control_type": "Button", "name": "Valid Button", "rect": (50, 50, 65, 65)},
        ]

        img = Image.new("RGB", (500, 500))
        _, marks, _ = annotator.annotate(image=img, win_rect=win_rect, raw_controls=controls)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[1].name, "Valid Button")

    def test_traversal_cap_at_75_controls(self):
        """Mandatory Constraint 2: Cap UIA traversal to top 75 visible interactive controls."""
        annotator = SetOfMarkAnnotator(max_controls=75)
        win_rect = (0, 0, 1000, 1000)
        # Create 100 valid controls
        controls = [
            {"control_type": "Button", "name": f"Btn_{i}", "rect": (10, 10 + i * 5, 50, 25 + i * 5)}
            for i in range(100)
        ]

        img = Image.new("RGB", (1000, 1000))
        _, marks, _ = annotator.annotate(image=img, win_rect=win_rect, raw_controls=controls)
        self.assertEqual(len(marks), 75, "Traversal must strictly cap at 75 marks")

    def test_click_marked_element_missing_id_error(self):
        """Verifies nonexistent mark_id returns a clear error."""
        res = click_marked_element(999)
        self.assertIn("was not found", res)

    def test_click_marked_element_staleness_on_window_closed(self):
        """Mandatory Constraint 3: Verify target hwnd is still valid (not closed)."""
        MarkRegistry.set_marks({
            1: MarkElement(
                mark_id=1,
                control_type="Button",
                name="Submit",
                auto_id="btn_submit",
                local_rect=(10, 10, 50, 30),
                screen_rect=(10, 10, 50, 30),
                local_center=(30, 20),
                screen_center=(30, 20),
                hwnd=8888,
                snapshot_win_rect=(0, 0, 200, 200),
            )
        })

        if sys.platform == "win32":
            with patch("ctypes.windll.user32.IsWindow", return_value=0):
                res = click_marked_element(1)
                self.assertIn("Staleness error", res)
                self.assertIn("has closed", res)

    def test_click_marked_element_staleness_on_foreground_switch(self):
        """Mandatory Constraint 3: Verify target hwnd is still in foreground."""
        MarkRegistry.set_marks({
            1: MarkElement(
                mark_id=1,
                control_type="Button",
                name="Submit",
                auto_id="btn_submit",
                local_rect=(10, 10, 50, 30),
                screen_rect=(10, 10, 50, 30),
                local_center=(30, 20),
                screen_center=(30, 20),
                hwnd=8888,
                snapshot_win_rect=(0, 0, 200, 200),
            )
        })

        if sys.platform == "win32":
            with patch("ctypes.windll.user32.IsWindow", return_value=1), \
                 patch("ctypes.windll.user32.GetForegroundWindow", return_value=9999), \
                 patch("ctypes.windll.user32.GetWindowTextLengthW", return_value=0):
                res = click_marked_element(1)
                self.assertIn("Staleness error", res)
                self.assertIn("no longer in the foreground", res)

    def test_click_marked_element_staleness_on_window_moved_or_resized(self):
        """Mandatory Constraint 3: Verify GetWindowRect matches snapshot before clicking."""
        MarkRegistry.set_marks({
            1: MarkElement(
                mark_id=1,
                control_type="Button",
                name="Submit",
                auto_id="btn_submit",
                local_rect=(10, 10, 50, 30),
                screen_rect=(10, 10, 50, 30),
                local_center=(30, 20),
                screen_center=(30, 20),
                hwnd=8888,
                snapshot_win_rect=(0, 0, 200, 200),
            )
        })

        if sys.platform == "win32":
            # Simulate window moved to (50, 50, 250, 250)
            def mock_get_window_rect(hwnd, rect_ptr):
                obj = getattr(rect_ptr, "_obj", getattr(rect_ptr, "contents", None))
                if obj is not None:
                    obj.left = 50
                    obj.top = 50
                    obj.right = 250
                    obj.bottom = 250
                return 1

            with patch("ctypes.windll.user32.IsWindow", return_value=1), \
                 patch("ctypes.windll.user32.GetForegroundWindow", return_value=8888), \
                 patch("ctypes.windll.user32.GetWindowRect", side_effect=mock_get_window_rect):
                res = click_marked_element(1)
                self.assertIn("Staleness error", res)
                self.assertIn("has moved or resized", res)


if __name__ == "__main__":
    unittest.main()
