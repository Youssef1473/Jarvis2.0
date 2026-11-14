"""Screen capture and visual perception for Jarvis 2.0.

Provides leak-free, high-DPI desktop capture and in-RAM privacy redaction
for multimodal visual perception and LLM reasoning.
"""
from __future__ import annotations

import io
import logging

from PIL import Image, ImageGrab

from jarvis.vision.win32_vision_abi import (
    enable_per_monitor_v2_dpi,
    scoped_desktop_access,
)

logger = logging.getLogger("Jarvis.Vision.ScreenCapture")


def _ensure_desktop_access() -> None:
    """Ensures thread is attached to the interactive Windows desktop and DPI v2 is configured."""
    enable_per_monitor_v2_dpi()


class ScreenPerception:
    """Captures desktop screenshots and prepares images for visual perception analysis."""

    @staticmethod
    def capture_screen(max_width: int = 1600) -> Image.Image | None:
        """Captures active screen with RAII desktop attachment and scales proportionally."""
        _ensure_desktop_access()
        try:
            from jarvis.vision.privacy_guard import ZOrderPrivacyGuard

            with scoped_desktop_access():
                screenshot = ImageGrab.grab(all_screens=False)

            # In-RAM redaction of sensitive Z-order windows (passwords, banks, wallets)
            screenshot = ZOrderPrivacyGuard.get_instance().redact_image_in_place(screenshot)

            # Scale down if width exceeds max_width to keep payload small and fast
            if screenshot.width > max_width:
                scale = max_width / float(screenshot.width)
                new_size = (max_width, int(screenshot.height * scale))
                screenshot = screenshot.resize(new_size, Image.Resampling.BILINEAR)
            return screenshot
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("Failed to capture screen: %s", e)
            return None

    @staticmethod
    def capture_screen_bytes(max_width: int = 1600, quality: int = 80) -> bytes | None:
        """Captures screen and returns compressed JPEG bytes."""
        img = ScreenPerception.capture_screen(max_width=max_width)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


__all__ = [
    "ScreenPerception",
    "_ensure_desktop_access",
]
