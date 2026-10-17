"""Unit tests for Z-Order HWND Scanning & In-RAM Pixel Redaction Privacy Guard."""
import unittest
from unittest.mock import MagicMock, patch
from PIL import Image

from jarvis.vision.privacy_guard import BoundingRect, ZOrderPrivacyGuard
from jarvis.vision.screen_capture import ScreenPerception


class TestZOrderPrivacyGuard(unittest.TestCase):
    """Verifies Z-Order HWND privacy scanning, bounding box mathematics, and in-RAM image redaction."""

    def test_bounding_rect_properties(self):
        """Verifies BoundingRect width, height, validity, and intersection logic."""
        rect1 = BoundingRect(left=100, top=100, right=400, bottom=300)
        self.assertEqual(rect1.width, 300)
        self.assertEqual(rect1.height, 200)
        self.assertTrue(rect1.is_valid())

        # Invalid rect (right <= left)
        invalid_rect = BoundingRect(left=200, top=100, right=100, bottom=300)
        self.assertFalse(invalid_rect.is_valid())

        # Intersecting rect
        rect2 = BoundingRect(left=200, top=200, right=500, bottom=400)
        self.assertTrue(rect1.intersects(rect2))

        # Disjoint rect
        rect3 = BoundingRect(left=600, top=600, right=800, bottom=800)
        self.assertFalse(rect1.intersects(rect3))

    def test_redact_image_in_place_zeroes_pixels(self):
        """Verifies that sensitive bounding rectangles are zeroed out (black) in RAM, leaving other pixels intact."""
        # Create a 1000x800 pure white test image
        img = Image.new("RGB", (1000, 800), color=(255, 255, 255))

        guard = ZOrderPrivacyGuard.get_instance()
        redaction_items = [
            (BoundingRect(left=100, top=100, right=400, bottom=300), "Bitwarden - Vault", "bitwarden.exe")
        ]

        redacted_img = guard.redact_image_in_place(img, redaction_items=redaction_items)

        # Pixels inside the redacted area must be pure black (0, 0, 0)
        for x, y in [(150, 150), (200, 200), (399, 299)]:
            pixel = redacted_img.getpixel((x, y))
            self.assertEqual(pixel, (0, 0, 0), f"Pixel at ({x}, {y}) should be redacted to black")

        # Pixels outside the redacted area must remain pure white (255, 255, 255)
        for x, y in [(50, 50), (500, 150), (150, 500), (900, 700)]:
            pixel = redacted_img.getpixel((x, y))
            self.assertEqual(pixel, (255, 255, 255), f"Pixel at ({x}, {y}) should remain unchanged")

    def test_redact_image_clamping(self):
        """Verifies that bounding boxes extending beyond screen or with negative coords clamp gracefully."""
        img = Image.new("RGB", (500, 500), color=(255, 255, 255))
        guard = ZOrderPrivacyGuard.get_instance()

        # Rect extending into negative space and beyond image bounds
        redaction_items = [
            (BoundingRect(left=-100, top=-50, right=200, bottom=600), "KeePassXC", "keepassxc.exe")
        ]

        redacted_img = guard.redact_image_in_place(img, redaction_items=redaction_items)

        # Clamped area (0 to 200, 0 to 500) must be black
        self.assertEqual(redacted_img.getpixel((50, 50)), (0, 0, 0))
        self.assertEqual(redacted_img.getpixel((199, 499)), (0, 0, 0))

        # Outside clamped area (x >= 200) remains white
        self.assertEqual(redacted_img.getpixel((250, 250)), (255, 255, 255))

    def test_empty_redaction_leaves_image_unmodified(self):
        """When no sensitive windows are present, image pixels must be 100% unaltered."""
        img = Image.new("RGB", (200, 200), color=(128, 128, 128))
        guard = ZOrderPrivacyGuard.get_instance()
        redacted_img = guard.redact_image_in_place(img, redaction_items=[])
        self.assertEqual(redacted_img.getpixel((100, 100)), (128, 128, 128))

    @patch("jarvis.vision.privacy_guard.ZOrderPrivacyGuard.get_redaction_rectangles")
    @patch("PIL.ImageGrab.grab")
    def test_screen_perception_automatically_redacts_captured_frame(self, mock_grab, mock_get_rects):
        """Verifies that ScreenPerception.capture_screen applies ZOrderPrivacyGuard automatically."""
        # Simulated raw screen capture (pure green)
        raw_screen = Image.new("RGB", (800, 600), color=(0, 255, 0))
        mock_grab.return_value = raw_screen

        # Simulate a background Bitwarden window visible at (100, 100, 300, 300)
        mock_get_rects.return_value = [
            (BoundingRect(left=100, top=100, right=300, bottom=300), "Bitwarden", "bitwarden.exe")
        ]

        captured = ScreenPerception.capture_screen(max_width=1600)
        self.assertIsNotNone(captured)

        # Inside sensitive window region -> must be redacted to black
        self.assertEqual(captured.getpixel((150, 150)), (0, 0, 0))

        # Outside sensitive region -> remains original green
        self.assertEqual(captured.getpixel((500, 500)), (0, 255, 0))


if __name__ == "__main__":
    unittest.main()
