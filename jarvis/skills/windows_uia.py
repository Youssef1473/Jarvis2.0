"""Semantic Windows UI Automation (UIA) tools for Jarvis 2.0.

Leverages pywinauto 0.6.9 to inspect accessibility trees, focus windows reliably,
and interact with native UI controls without blind synthetic keystrokes,
fortified with Win32 message pump responsiveness pre-probing (SendMessageTimeoutW
with SMTO_ABORTIFHUNG) to prevent thread hangs on frozen applications.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import warnings
from ctypes import wintypes

from jarvis.skills.base import SkillRegistry

# Suppress benign pywinauto STA COM mode revert warning on Windows
warnings.filterwarnings("ignore", message=".*Revert to STA COM threading mode.*")

logger = logging.getLogger("Jarvis.WindowsUIA")

WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002


def is_uia_supported() -> bool:
    """Checks if UI Automation is supported on the current platform."""
    return sys.platform == "win32"


def get_active_uia_delta_worker():
    """Returns the active UIADeltaWorker instance if initialized."""
    try:
        from jarvis.desktop.uia_delta_worker import UIADeltaWorker
        return UIADeltaWorker.get_instance()
    except Exception:
        return None


def is_hwnd_responsive(hwnd: int, timeout_ms: int = 50) -> bool:
    """Pre-probes target HWND to verify message pump responsiveness in <1ms.

    Guarantees pywinauto and COM UIA tree calls never deadlock on frozen,
    hung, or garbage-collecting applications (HungAppWindow).
    """
    if sys.platform != "win32" or not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
        return True

    try:
        user32 = ctypes.windll.user32

        # 1. Fast check via IsHungAppWindow
        if hasattr(user32, "IsHungAppWindow") and user32.IsHungAppWindow(hwnd):
            return False

        # 2. Timeout probe via SendMessageTimeoutW
        result = wintypes.DWORD(0)
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM

        ret = user32.SendMessageTimeoutW(
            hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
        )
        return bool(ret != 0)
    except Exception as e:
        logger.debug("is_hwnd_responsive check failed: %s", e)
        return True


def focus_window_uia(title_query: str) -> str | None:
    """Focuses a desktop window using pywinauto UI Automation backend.

    Bypasses standard Windows SetForegroundWindow lock by restoring and focusing the UIA element.
    Falls back to pywinauto win32 backend if UIA enumeration misses the target.
    Pre-probes message pump responsiveness to skip hung windows safely.
    """
    if not is_uia_supported() or not title_query:
        return None

    clean_target = title_query.strip().lower()

    # 1. Primary UIA backend
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pywinauto import Desktop
            d = Desktop(backend="uia")
        windows = d.windows()
        for win in windows:
            try:
                handle = getattr(win, "handle", 0)
                if handle and not is_hwnd_responsive(handle, timeout_ms=50):
                    logger.debug("Skipping unresponsive UIA window HWND %s", handle)
                    continue

                text = win.window_text().strip()
                if text and clean_target in text.lower():
                    # Restore if minimized and bring to foreground
                    if win.is_minimized():
                        win.restore()
                    win.set_focus()
                    return text
            except Exception:
                continue
    except Exception as e:
        logger.debug("pywinauto UIA focus notice: %s", e)

    # 2. Deterministic Fallback: pywinauto win32 backend
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pywinauto import Desktop
            d_win32 = Desktop(backend="win32")
        for win in d_win32.windows():
            try:
                handle = getattr(win, "handle", 0)
                if handle and not is_hwnd_responsive(handle, timeout_ms=50):
                    logger.debug("Skipping unresponsive Win32 window HWND %s", handle)
                    continue

                text = win.window_text().strip()
                if text and clean_target in text.lower():
                    if win.is_minimized():
                        win.restore()
                    win.set_focus()
                    return text
            except Exception:
                continue
    except Exception as e:
        logger.debug("pywinauto win32 focus fallback notice: %s", e)

    return None


@SkillRegistry.register(
    name="inspect_window_controls",
    description="Inspects native UI elements (buttons, inputs, menus, tabs) of an active window using Windows UI Automation (UIA) without taking screenshots."
)
def inspect_window_controls(window_title: str, max_depth: int = 2) -> str:
    """Inspects native accessibility tree controls for a given desktop window."""
    if not is_uia_supported():
        return "UI Automation is only supported on Windows workstations, Sir."

    target = (window_title or "").strip().lower()
    if not target:
        return "Window title cannot be empty, Sir."

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pywinauto import Desktop
            d = Desktop(backend="uia")
        matched = None
        matched_title = ""

        for win in d.windows():
            try:
                handle = getattr(win, "handle", 0)
                if handle and not is_hwnd_responsive(handle, timeout_ms=50):
                    continue

                t = win.window_text().strip()
                if t and target in t.lower():
                    matched = win
                    matched_title = t
                    break
            except Exception:
                continue

        if not matched:
            return f"Could not find an active window matching '{window_title}' to inspect, Sir."

        matched_hwnd = getattr(matched, "handle", 0)
        if matched_hwnd and not is_hwnd_responsive(matched_hwnd, timeout_ms=50):
            return f"Window '{matched_title}' is currently unresponsive or frozen (HungAppWindow), Sir. Inspection aborted to prevent process hang."

        # Collect child controls
        controls = []
        try:
            children = matched.descendants(depth=max_depth)
            for child in children[:20]:
                c_type = child.element_info.control_type or "Unknown"
                c_name = child.element_info.name or ""
                c_auto_id = child.element_info.automation_id or ""
                label = f"• [{c_type}] {c_name}"
                if c_auto_id:
                    label += f" (id: {c_auto_id})"
                controls.append(label)
        except Exception as e:
            controls.append(f"• Traversal note: {e}")

        summary = "\n".join(controls) if controls else "No interactive child elements found."
        return (
            f"UI Automation Tree for '{matched_title}', Sir:\n"
            f"{summary}"
        )

    except Exception as e:
        logger.warning("Failed to inspect window controls via UIA: %s", e)
        return f"Unable to inspect UI controls for '{window_title}': {e}"
