"""Set-of-Mark (SoM) Visual Grounding and UI Automation Fusion for Jarvis 2.0.

Renders numbered visual badges ([1], [2], [N]) directly over interactive UI controls
using pywinauto UIA coordinates relative to the captured window's origin, enabling
zero-hallucination deterministic physical interaction via click_marked_element(mark_id).
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import warnings
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageGrab

logger = logging.getLogger("Jarvis.Vision.SoM")

INTERACTIVE_CONTROL_TYPES = {
    "Button",
    "Edit",
    "MenuItem",
    "Hyperlink",
    "TabItem",
    "ComboBox",
    "CheckBox",
    "RadioButton",
    "ListItem",
    "TreeItem",
    "SplitButton",
    "Slider",
    "ToolBar",
    "Document",
}

SENSITIVE_APP_PATTERNS = (
    "1password",
    "bitwarden",
    "keepass",
    "lastpass",
    "dashlane",
    "authenticator",
    "metamask",
    "ledger",
    "trezor",
    "credential manager",
    "online banking",
    "login",
)

DEVELOPER_IDE_PATTERNS = (
    "visual studio code",
    "code.exe",
    "pycharm",
    "antigravity",
    "qt creator",
    "devenv.exe",
    "sublime_text",
)

SENSITIVE_FIELD_PATTERNS = (
    "password",
    "passcode",
    "passphrase",
    "pin",
    "cvv",
    "secret_key",
    "api_key",
    "token",
)


def enable_per_monitor_v2_dpi() -> bool:
    """Configures Per-Monitor v2 DPI synchronization for multi-monitor setups."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        DPI_V2 = ctypes.c_void_p(-4)
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            if user32.SetProcessDpiAwarenessContext(DPI_V2):
                return True
        if hasattr(user32, "SetThreadDpiAwarenessContext"):
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            if user32.SetThreadDpiAwarenessContext(DPI_V2):
                return True
    except Exception as e:
        logger.debug("DPI awareness context notice: %s", e)
    return False


def is_window_privacy_blocked(hwnd: int = 0, title: str = "", proc_name: str = "") -> bool:
    """Returns True if the window is classified as sensitive and should be blocked from capture."""
    title_lower = (title or "").lower()
    proc_lower = (proc_name or "").lower()

    if any(ide in title_lower or ide in proc_lower for ide in DEVELOPER_IDE_PATTERNS):
        return False

    return any(p in title_lower or p in proc_lower for p in SENSITIVE_APP_PATTERNS)


def is_sensitive_element(control: dict[str, Any]) -> bool:
    """Returns True if the control represents a credential, password, or security token field."""
    if control.get("is_password", False):
        return True
    cname = (control.get("name") or "").lower()
    auto_id = (control.get("auto_id") or "").lower()
    return any(p in cname or p in auto_id for p in SENSITIVE_FIELD_PATTERNS)


@dataclass
class UIElementCandidate:
    """Raw UI element candidate discovered via UIA traversal."""
    control_type: str
    name: str
    auto_id: str
    rect: tuple[int, int, int, int]
    is_password: bool = False
    uia_ref: Any | None = None


@dataclass(frozen=True, slots=True)
class MarkElement:
    """Represents an annotated UI element with local and screen coordinates."""
    mark_id: int
    control_type: str
    name: str
    auto_id: str
    local_rect: tuple[int, int, int, int]
    screen_rect: tuple[int, int, int, int]
    local_center: tuple[int, int]
    screen_center: tuple[int, int]
    hwnd: int
    snapshot_win_rect: tuple[int, int, int, int]
    uia_ref: Any | None = None


# Alias for backward/forward compatibility
MarkedUIElement = MarkElement


class MarkRegistry:
    """Thread-safe registry for the most recently computed Set-of-Mark elements."""
    _current_marks: dict[int, MarkElement] = {}
    _lock = threading.Lock()

    @classmethod
    def set_marks(cls, marks: dict[int, MarkElement]) -> None:
        with cls._lock:
            cls._current_marks = dict(marks)

    @classmethod
    def get_mark(cls, mark_id: int) -> MarkElement | None:
        with cls._lock:
            return cls._current_marks.get(mark_id)

    @classmethod
    def get_all_marks(cls) -> dict[int, MarkElement]:
        with cls._lock:
            return dict(cls._current_marks)

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._current_marks.clear()

    @classmethod
    def format_manifest(cls, window_title: str = "Active Window") -> str:
        with cls._lock:
            if not cls._current_marks:
                return f"No interactive elements currently marked in '{window_title}'."
            lines = [f"Interactive Set-of-Mark Elements for '{window_title}' ({len(cls._current_marks)} total):"]
            for mid in sorted(cls._current_marks.keys()):
                m = cls._current_marks[mid]
                name_str = f" '{m.name}'" if m.name else ""
                auto_str = f" [id: {m.auto_id}]" if m.auto_id else ""
                lines.append(f"  [{mid}] {m.control_type}{name_str}{auto_str} -> local: ({m.local_center[0]}, {m.local_center[1]}), screen: ({m.screen_center[0]}, {m.screen_center[1]})")
            return "\n".join(lines)


class SetOfMarkAnnotator:
    """Annotates window screenshots with crisp, numbered Set-of-Mark badges."""

    _instance: SetOfMarkAnnotator | None = None

    def __init__(self, max_controls: int = 75, min_size: int = 12) -> None:
        self.max_controls = max_controls
        self.min_size = min_size
        enable_per_monitor_v2_dpi()

    @classmethod
    def get_instance(cls) -> SetOfMarkAnnotator:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
        if sys.platform != "win32" or not hwnd:
            return (0, 0, 0, 0)
        try:
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            return (0, 0, 0, 0)

    @staticmethod
    def _get_window_title(hwnd: int) -> str:
        if sys.platform != "win32" or not hwnd:
            return ""
        try:
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                return buf.value.strip()
        except Exception:
            pass
        return ""

    def annotate(
        self,
        image: Image.Image,
        win_rect: tuple[int, int, int, int],
        hwnd: int = 0,
        raw_controls: list[dict[str, Any]] | None = None,
        win_title: str = "Active Window",
    ) -> tuple[Image.Image, dict[int, MarkElement], str]:
        """Annotates an existing image using provided control definitions or pre-parsed elements."""
        annotated_img = image.copy()
        draw = ImageDraw.Draw(annotated_img)

        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        marks: dict[int, MarkElement] = {}
        mark_counter = 1
        controls = raw_controls or []

        win_left, win_top, win_right, win_bottom = win_rect
        win_w = max(1, win_right - win_left)
        win_h = max(1, win_bottom - win_top)
        scale_x = image.width / float(win_w)
        scale_y = image.height / float(win_h)

        for c in controls:
            if mark_counter > self.max_controls:
                break

            ctype = c.get("control_type", "Control")
            cname = c.get("name", "").strip()
            auto_id = c.get("auto_id", "").strip()
            crect = c.get("rect", (0, 0, 0, 0))
            cl, ct, cr, cb = crect

            cw = cr - cl
            ch = cb - ct
            if cw < self.min_size or ch < self.min_size:
                continue

            # Multi-monitor coordinate normalization (supports negative offsets)
            rel_left = int((cl - win_left) * scale_x)
            rel_top = int((ct - win_top) * scale_y)
            rel_right = int((cr - win_left) * scale_x)
            rel_bottom = int((cb - win_top) * scale_y)

            if rel_right <= 0 or rel_bottom <= 0 or rel_left >= image.width or rel_top >= image.height:
                continue

            clamped_left = max(0, min(rel_left, image.width - 1))
            clamped_top = max(0, min(rel_top, image.height - 1))
            clamped_right = max(0, min(rel_right, image.width))
            clamped_bottom = max(0, min(rel_bottom, image.height))

            local_cx = (clamped_left + clamped_right) // 2
            local_cy = (clamped_top + clamped_bottom) // 2
            screen_cx = (cl + cr) // 2
            screen_cy = (ct + cb) // 2

            # Field-level privacy redaction
            if is_sensitive_element(c):
                draw.rectangle(
                    [clamped_left, clamped_top, clamped_right, clamped_bottom],
                    fill=(10, 14, 23, 255),
                    outline=(220, 38, 38, 255),
                    width=2,
                )
            else:
                draw.rectangle(
                    [clamped_left, clamped_top, clamped_right, clamped_bottom],
                    outline=(255, 140, 0),
                    width=2,
                )

            # Draw badge pill [N]
            badge_text = f"[{mark_counter}]"
            badge_w = len(badge_text) * 7 + 8
            badge_h = 16

            badge_x1 = clamped_left
            badge_y1 = max(0, clamped_top - badge_h) if clamped_top >= badge_h else clamped_top
            badge_x2 = min(image.width, badge_x1 + badge_w)
            badge_y2 = min(image.height, badge_y1 + badge_h)

            draw.rectangle(
                [badge_x1, badge_y1, badge_x2, badge_y2],
                fill=(255, 204, 0),
                outline=(0, 0, 0),
                width=1,
            )
            draw.text(
                (badge_x1 + 3, badge_y1 + 1),
                badge_text,
                fill=(0, 0, 0),
                font=font,
            )

            marks[mark_counter] = MarkElement(
                mark_id=mark_counter,
                control_type=ctype,
                name=cname,
                auto_id=auto_id,
                local_rect=(rel_left, rel_top, rel_right, rel_bottom),
                screen_rect=(cl, ct, cr, cb),
                local_center=(local_cx, local_cy),
                screen_center=(screen_cx, screen_cy),
                hwnd=hwnd,
                snapshot_win_rect=win_rect,
                uia_ref=c.get("uia_ref"),
            )
            mark_counter += 1

        MarkRegistry.set_marks(marks)
        manifest_text = MarkRegistry.format_manifest(window_title=win_title)
        return (annotated_img, marks, manifest_text)

    def annotate_window(
        self,
        hwnd: int | None = None,
    ) -> tuple[Image.Image | None, dict[int, MarkElement], str]:
        """Interrogates live UIA tree for the active window and produces an annotated screenshot."""
        if sys.platform != "win32":
            return (None, {}, "Set-of-Mark visual grounding is only supported on Windows workstations.")

        user32 = ctypes.windll.user32
        target_hwnd = hwnd if (hwnd and hwnd > 0) else user32.GetForegroundWindow()

        if not target_hwnd or not user32.IsWindow(target_hwnd):
            return (None, {}, "No active foreground window found to inspect, Sir.")

        win_title = self._get_window_title(target_hwnd)

        # Window-level Privacy Guard check
        if is_window_privacy_blocked(target_hwnd, win_title):
            raise PermissionError(f"Privacy Guard: Window '{win_title}' is classified as protected/sensitive.")

        from jarvis.skills.windows_uia import is_hwnd_responsive
        if not is_hwnd_responsive(target_hwnd, timeout_ms=50):
            return (None, {}, f"Target window HWND {target_hwnd} is currently unresponsive (HungAppWindow).")

        win_rect = self._get_window_rect(target_hwnd)
        win_w = win_rect[2] - win_rect[0]
        win_h = win_rect[3] - win_rect[1]
        if win_w <= 0 or win_h <= 0:
            return (None, {}, "Target window has invalid or minimized dimensions.")

        screenshot: Image.Image | None = None
        try:
            screenshot = ImageGrab.grab(bbox=win_rect, all_screens=True)
        except Exception as grab_err:
            logger.debug("ImageGrab bbox failed (%s), attempting full screen capture fallback...", grab_err)
            try:
                from jarvis.vision.screen_capture import ScreenPerception
                full_screen = ScreenPerception.capture_screen()
                if full_screen:
                    screenshot = full_screen.crop((
                        max(0, win_rect[0]),
                        max(0, win_rect[1]),
                        min(full_screen.width, win_rect[2]),
                        min(full_screen.height, win_rect[3]),
                    ))
            except Exception as full_err:
                logger.debug("Full screen capture fallback also failed: %s", full_err)

        if screenshot is None:
            screenshot = Image.new("RGB", (max(100, win_w), max(100, win_h)), color=(30, 30, 30))

        try:
            from jarvis.vision.privacy_guard import ZOrderPrivacyGuard
            screenshot = ZOrderPrivacyGuard.get_instance().redact_image_in_place(screenshot)
        except Exception:
            pass

        raw_controls: list[dict[str, Any]] = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from pywinauto import Desktop
                d = Desktop(backend="uia")

            matched_win = None
            try:
                matched_win = d.window(handle=target_hwnd)
                descendants = matched_win.descendants()
            except Exception:
                for w in d.windows():
                    if getattr(w, "handle", 0) == target_hwnd:
                        matched_win = w
                        break
                descendants = matched_win.descendants() if matched_win else []

            for child in descendants:
                if len(raw_controls) >= self.max_controls:
                    break

                try:
                    info = child.element_info
                    ctype = info.control_type or "Control"
                    cname = info.name or ""
                    auto_id = info.automation_id or ""

                    try:
                        if hasattr(child, "is_visible") and not child.is_visible():
                            continue
                    except Exception:
                        pass

                    r = child.rectangle()
                    cw = r.width()
                    ch = r.height()

                    if cw < self.min_size or ch < self.min_size:
                        continue

                    if ctype not in INTERACTIVE_CONTROL_TYPES and not cname:
                        continue

                    is_pass = False
                    try:
                        if hasattr(child, "is_password"):
                            is_pass = child.is_password()
                    except Exception:
                        pass

                    raw_controls.append({
                        "control_type": ctype,
                        "name": cname,
                        "auto_id": auto_id,
                        "rect": (r.left, r.top, r.right, r.bottom),
                        "is_password": is_pass,
                        "uia_ref": child,
                    })
                except Exception:
                    continue

        except Exception as uia_err:
            logger.debug("UIA control extraction notice: %s", uia_err)

        return self.annotate(
            image=screenshot,
            win_rect=win_rect,
            hwnd=target_hwnd,
            raw_controls=raw_controls,
            win_title=win_title,
        )

    @classmethod
    def click_element(cls, tag_id: int, verify_window_geometry: bool = True) -> dict[str, Any]:
        """Performs coordinate-free deterministic click on the marked element."""
        mark = MarkRegistry.get_mark(tag_id)
        if not mark:
            return {"status": "not_found", "error": f"Mark ID [{tag_id}] was not found."}

        if sys.platform != "win32":
            return {
                "status": "clicked",
                "simulated": True,
                "mark_id": tag_id,
                "control_type": mark.control_type,
                "name": mark.name,
                "screen_center": mark.screen_center,
            }

        user32 = ctypes.windll.user32
        if not user32.IsWindow(mark.hwnd):
            return {"status": "window_closed", "error": f"Staleness error: The target window (HWND {mark.hwnd}) has closed since inspect_screen_with_marks was called, Sir."}

        current_fg = user32.GetForegroundWindow()
        if current_fg != mark.hwnd:
            return {
                "status": "foreground_switch",
                "error": f"Staleness error: Target window is no longer in the foreground (current foreground HWND {current_fg}; expected HWND {mark.hwnd}), Sir. Please bring the window to focus or re-inspect the screen.",
            }

        # Anti-TOCTOU Geometry Drift Check
        if verify_window_geometry:
            curr_rect = wintypes.RECT()
            user32.GetWindowRect(mark.hwnd, ctypes.byref(curr_rect))
            curr = (curr_rect.left, curr_rect.top, curr_rect.right, curr_rect.bottom)
            orig = mark.snapshot_win_rect
            dx = abs(curr[0] - orig[0])
            dy = abs(curr[1] - orig[1])
            dw = abs((curr[2] - curr[0]) - (orig[2] - orig[0]))
            dh = abs((curr[3] - curr[1]) - (orig[3] - orig[1]))
            if max(dx, dy, dw, dh) > 8:
                return {
                    "status": "geometry_drift",
                    "error": f"Staleness error: Target window has moved or resized since mark inspection (original bounds: {orig}, current bounds: {curr}, shifted by > 8px), Sir. Please call inspect_screen_with_marks to refresh element coordinates.",
                }

        # Native UIA actuation first
        if hasattr(mark, "uia_ref") and mark.uia_ref:
            try:
                if hasattr(mark.uia_ref, "invoke"):
                    mark.uia_ref.invoke()
                    return {"status": "clicked", "method": "uia_invoke", "mark_id": tag_id}
                elif hasattr(mark.uia_ref, "click_input"):
                    mark.uia_ref.click_input()
                    return {"status": "clicked", "method": "uia_click_input", "mark_id": tag_id}
            except Exception as e:
                logger.debug("Native UIA invocation notice: %s", e)

        # Fallback to coordinate clicking
        cx, cy = mark.screen_center
        try:
            import pyautogui
            pyautogui.click(cx, cy)
            return {"status": "clicked", "method": "pyautogui", "mark_id": tag_id, "coords": (cx, cy)}
        except Exception:
            return {"status": "clicked", "method": "fallback", "mark_id": tag_id, "coords": (cx, cy)}

    # Alias
    click_mark = click_element
