"""Screen capture and visual perception for Jarvis 2.0."""
import io
import sys
import logging
from typing import Optional
from PIL import Image, ImageGrab

logger = logging.getLogger("Jarvis.Vision")


def _ensure_desktop_access():
    """Ensures thread is attached to the interactive Windows desktop."""
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # Enable DPI awareness (Per Monitor v2)
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass

            # Ensure thread desktop is attached to active interactive input desktop
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            if not hdesk:
                hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception as e:
            logger.debug(f"Desktop switch notice: {e}")


class ScreenPerception:
    """Captures desktop screenshots and prepares images for Gemini Vision analysis."""

    @staticmethod
    def capture_screen(max_width: int = 1600) -> Optional[Image.Image]:
        """Captures active screen and scales down proportionally for fast LLM inference."""
        _ensure_desktop_access()
        try:
            from jarvis.vision.privacy_guard import ZOrderPrivacyGuard

            screenshot = ImageGrab.grab(all_screens=False)
            # In-RAM redaction of sensitive Z-order windows (passwords, banks, wallets)
            screenshot = ZOrderPrivacyGuard.get_instance().redact_image_in_place(screenshot)

            # Scale down if width exceeds max_width to keep payload small and fast
            if screenshot.width > max_width:
                scale = max_width / float(screenshot.width)
                new_size = (max_width, int(screenshot.height * scale))
                screenshot = screenshot.resize(new_size, Image.Resampling.LANCZOS)
            return screenshot
        except Exception as e:
            logger.error(f"Failed to capture screen: {e}")
            return None

    @staticmethod
    def capture_screen_bytes(max_width: int = 1600, quality: int = 80) -> Optional[bytes]:
        """Captures screen and returns compressed JPEG bytes."""
        img = ScreenPerception.capture_screen(max_width=max_width)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
