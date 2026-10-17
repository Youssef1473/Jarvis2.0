"""Win32 Dynamic Hit-Testing and WDA_EXCLUDEFROMCAPTURE Transparent Overlay Engine.

Provides native Windows desktop compositing, cloaking the HUD from Jarvis's own
VLM visual cortex while enabling dynamic click-through hit testing.
"""
import ctypes
import os
import sys
from typing import List, Tuple

# Win32 Constants
GWL_EXSTYLE: int = -20
WS_EX_LAYERED: int = 0x00080000
WS_EX_TRANSPARENT: int = 0x00000020
WS_EX_TOPMOST: int = 0x00000008
WS_EX_TOOLWINDOW: int = 0x00000080
WS_EX_NOACTIVATE: int = 0x08000000

# Window Display Affinity
WDA_NONE: int = 0x00000000
WDA_MONITOR: int = 0x00000001
WDA_EXCLUDEFROMCAPTURE: int = 0x00000011

IS_WINDOWS: bool = sys.platform == "win32"

if IS_WINDOWS:
    try:
        user32 = ctypes.windll.user32
        # Use GetWindowLongPtr / SetWindowLongPtr for 64-bit safety
        GetWindowLong = getattr(user32, "GetWindowLongPtrW", getattr(user32, "GetWindowLongW", None))
        SetWindowLong = getattr(user32, "SetWindowLongPtrW", getattr(user32, "SetWindowLongW", None))
        SetWindowDisplayAffinity = getattr(user32, "SetWindowDisplayAffinity", None)
        GetCursorPos = getattr(user32, "GetCursorPos", None)
    except Exception:
        GetWindowLong = None
        SetWindowLong = None
        SetWindowDisplayAffinity = None
        GetCursorPos = None
else:
    GetWindowLong = None
    SetWindowLong = None
    SetWindowDisplayAffinity = None
    GetCursorPos = None


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def configure_stark_overlay_hwnd(hwnd: int) -> bool:
    """Configures Win32 window styles for transparent, click-through, always-on-top HUD display,

    and excludes the window from desktop screen captures (WDA_EXCLUDEFROMCAPTURE).
    """
    if not IS_WINDOWS or not hwnd or not SetWindowLong:
        return False

    try:
        # Retrieve current extended style
        current_style = GetWindowLong(hwnd, GWL_EXSTYLE) if GetWindowLong else 0

        # Apply HUD overlay extended styles
        overlay_style = (
            current_style
            | WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )

        SetWindowLong(hwnd, GWL_EXSTYLE, overlay_style)

        # Cloak from desktop screen capture to prevent recursive self-perception
        if SetWindowDisplayAffinity:
            try:
                SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            except Exception:
                pass

        return True
    except Exception:
        return False


def update_dynamic_click_through(hwnd: int, interactive_screen_rects: List[Tuple[int, int, int, int]]) -> bool:
    """Polls cursor position and dynamically toggles WS_EX_TRANSPARENT.

    If cursor is over any interactive bounding box (left, top, right, bottom),
    clears WS_EX_TRANSPARENT so mouse clicks hit the HUD controls.
    Otherwise restores WS_EX_TRANSPARENT for click-through to underlying apps.

    Returns:
        bool: True if cursor is hovering over an interactive widget, False otherwise.
    """
    if not IS_WINDOWS or not hwnd or not GetCursorPos or not GetWindowLong or not SetWindowLong:
        return False

    try:
        pt = POINT()
        if not GetCursorPos(ctypes.byref(pt)):
            return False

        cx, cy = pt.x, pt.y
        is_hovering_interactive = False

        for left, top, right, bottom in interactive_screen_rects:
            if left <= cx <= right and top <= cy <= bottom:
                is_hovering_interactive = True
                break

        current_style = GetWindowLong(hwnd, GWL_EXSTYLE)

        if is_hovering_interactive:
            # Clear WS_EX_TRANSPARENT so clicks are intercepted by HUD widgets
            if current_style & WS_EX_TRANSPARENT:
                new_style = current_style & ~WS_EX_TRANSPARENT
                SetWindowLong(hwnd, GWL_EXSTYLE, new_style)
        else:
            # Restore WS_EX_TRANSPARENT so clicks pass through to background desktop
            if not (current_style & WS_EX_TRANSPARENT):
                new_style = current_style | WS_EX_TRANSPARENT
                SetWindowLong(hwnd, GWL_EXSTYLE, new_style)

        return is_hovering_interactive
    except Exception:
        return False
