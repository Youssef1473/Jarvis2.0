"""Z-Order HWND Scanning & Pixel Redaction Privacy Guard for Jarvis 2.0.

Provides sub-1ms Live Z-Order HWND scanning (EnumWindows + DwmGetWindowAttribute)
and in-RAM frame redaction to prevent TOCTOU and background / split-screen
credential and financial data leakage in screen capture pipelines.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import logging
import re
import sys
from typing import Dict, List, Optional, Set, Tuple
from PIL import Image, ImageDraw

logger = logging.getLogger("Jarvis.Vision.PrivacyGuard")

# DWM Window Attributes
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
IS_WINDOWS = sys.platform == "win32"


def is_window_dwm_cloaked(hwnd: int) -> bool:
    """Returns True if `hwnd` is cloaked by Windows 11 DWM (e.g., on an inactive
    Virtual Desktop or suspended UWP background frame) and therefore invisible
    on the physical monitor.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
        cloaked = wintypes.DWORD(0)
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_CLOAKED),
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and bool(cloaked.value != 0)
    except Exception:
        return False

SENSITIVE_PROCESSES: Set[str] = {
    "1password.exe",
    "bitwarden.exe",
    "keepass.exe",
    "keepassxc.exe",
    "lastpass.exe",
    "dashlane.exe",
    "authenticator.exe",
    "metamask.exe",
    "ledger live.exe",
    "trezor suite.exe",
    "windowsecurity.exe",
    "credentialuibroker.exe",
    "authy.exe",
}

SENSITIVE_TITLE_KEYWORDS: Tuple[str, ...] = (
    "1password",
    "bitwarden",
    "keepass",
    "lastpass",
    "dashlane",
    "kdbx",
    "authenticator",
    "online banking",
    "netbanking",
    "bank of america",
    "chase online",
    "wells fargo",
    "citibank",
    "hsbc",
    "barclays",
    "schwab",
    "fidelity",
    "metamask",
    "phantom wallet",
    "trust wallet",
    "ledger live",
    "trezor",
    "private key",
    "seed phrase",
    "master password",
    "credential manager",
    "sign in - google accounts",
)

SAFE_IDE_PROCESSES: Set[str] = {
    "code.exe",
    "devenv.exe",
    "pycharm64.exe",
    "idea64.exe",
    "sublime_text.exe",
    "notepad++.exe",
    "cursor.exe",
}

LOGIN_PATTERN = re.compile(r"\b(log\s*in|sign\s*in|sign\s*on|enter\s*password)\b", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class BoundingRect:
    """Represents a screen bounding rectangle [left, top, right, bottom]."""
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def is_valid(self) -> bool:
        return self.right > self.left and self.bottom > self.top

    def intersects(self, other: BoundingRect) -> bool:
        return not (
            self.right <= other.left
            or self.left >= other.right
            or self.bottom <= other.top
            or self.top >= other.bottom
        )


class ZOrderPrivacyGuard:
    """Sub-1ms Z-Order HWND Scanner & In-RAM Pixel Redactor.

    Enumerates visible, uncloaked windows across the desktop Z-order,
    identifies sensitive processes and credential dialogs (even when unfocused
    or split-screen), and zeroes out their bounding boxes directly in memory.
    """

    _instance: Optional[ZOrderPrivacyGuard] = None

    def __init__(self) -> None:
        self._pid_name_cache: Dict[int, str] = {}

    @classmethod
    def get_instance(cls) -> ZOrderPrivacyGuard:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _get_process_name(self, pid: int) -> str:
        if pid in self._pid_name_cache:
            return self._pid_name_cache[pid]
        try:
            import psutil
            name = psutil.Process(pid).name().lower()
            self._pid_name_cache[pid] = name
            return name
        except Exception:
            return ""

    def get_redaction_rectangles(self) -> List[Tuple[BoundingRect, str, str]]:
        """Enumerates visible, uncloaked HWNDs in <1ms and returns sensitive bounding rects.

        Returns:
            List of tuples: (BoundingRect, window_title, process_name)
        """
        if sys.platform != "win32":
            return []

        try:
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
        except Exception as e:
            logger.debug("Win32 API unavailable for privacy scan: %s", e)
            return []

        redacted_items: List[Tuple[BoundingRect, str, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_windows_callback(hwnd: int, lparam: int) -> bool:
            # 1. Skip non-visible or minimized windows
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True

            # 2. Skip cloaked windows (e.g. inactive virtual desktops, shell hidden)
            if is_window_dwm_cloaked(hwnd):
                return True

            # 3. Retrieve window title
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value

            # 4. Retrieve process name
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = self._get_process_name(pid.value)

            # 5. Evaluate sensitivity
            title_lower = title.lower()
            proc_lower = proc_name.lower()

            is_sensitive = False
            if proc_lower in SENSITIVE_PROCESSES:
                is_sensitive = True
            elif any(kw in title_lower for kw in SENSITIVE_TITLE_KEYWORDS):
                is_sensitive = True
            elif LOGIN_PATTERN.search(title_lower) and proc_lower not in SAFE_IDE_PROCESSES:
                is_sensitive = True

            if is_sensitive:
                # 6. Resolve high-DPI frame bounding box
                rect = wintypes.RECT()
                res = dwmapi.DwmGetWindowAttribute(
                    hwnd,
                    DWMWA_EXTENDED_FRAME_BOUNDS,
                    ctypes.byref(rect),
                    ctypes.sizeof(rect),
                )
                if res != 0:
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))

                bounding_rect = BoundingRect(
                    left=rect.left,
                    top=rect.top,
                    right=rect.right,
                    bottom=rect.bottom,
                )
                if bounding_rect.is_valid():
                    redacted_items.append((bounding_rect, title, proc_name))

            return True

        try:
            user32.EnumWindows(enum_windows_callback, 0)
        except Exception as err:
            logger.debug("EnumWindows failed during privacy inspection: %s", err)

        return redacted_items

    def redact_image_in_place(
        self,
        image: Image.Image,
        redaction_items: Optional[List[Tuple[BoundingRect, str, str]]] = None,
    ) -> Image.Image:
        """Draws solid black rectangles over sensitive window areas on the given image.

        Args:
            image: PIL Image captured from the screen.
            redaction_items: Optional pre-computed redaction list. If None, scans live Z-order.

        Returns:
            The redacted PIL Image (modified in-place).
        """
        if redaction_items is None:
            redaction_items = self.get_redaction_rectangles()

        if not redaction_items:
            return image

        img_w, img_h = image.size
        draw = ImageDraw.Draw(image)

        for rect, title, proc in redaction_items:
            x1 = max(0, min(rect.left, img_w))
            y1 = max(0, min(rect.top, img_h))
            x2 = max(0, min(rect.right, img_w))
            y2 = max(0, min(rect.bottom, img_h))

            if x2 > x1 and y2 > y1:
                draw.rectangle([x1, y1, x2, y2], fill=(0, 0, 0))
                logger.info(
                    "Visual Privacy Guard redacted sensitive area [%d, %d, %d, %d] for window '%s' (%s).",
                    x1, y1, x2, y2, title, proc
                )

        return image
