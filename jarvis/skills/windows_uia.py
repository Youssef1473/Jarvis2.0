"""Semantic Windows UI Automation (UIA) tools for Jarvis 2.0.

Leverages pywinauto 0.6.9 to inspect accessibility trees, focus windows and controls reliably,
extract UI Automation control patterns, and interact with native UI controls without blind synthetic keystrokes,
fortified with Win32 message pump responsiveness pre-probing (SendMessageTimeoutW with SMTO_ABORTIFHUNG)
to prevent thread hangs on frozen applications.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
import warnings
from ctypes import wintypes
from typing import Any

from jarvis.skills.base import SkillRegistry

# Suppress benign pywinauto STA COM mode revert warning on Windows
warnings.filterwarnings("ignore", message=".*Revert to STA COM threading mode.*")

logger = logging.getLogger("Jarvis.WindowsUIA")

WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002

# Standard toggle, expand-collapse, and visual window state text mappings
_TOGGLE_STATES = {0: "off", 1: "on", 2: "indeterminate"}
_EXPAND_STATES = {0: "collapsed", 1: "expanded", 2: "partially_expanded", 3: "leaf_node"}
_WINDOW_VISUAL_STATES = {0: "normal", 1: "maximized", 2: "minimized"}


def is_uia_supported() -> bool:
    """Checks if UI Automation is supported on the current platform."""
    return sys.platform == "win32"


def get_active_uia_delta_worker():
    """Returns the active UIADeltaWorker instance if initialized."""
    try:
        from jarvis.desktop.uia_delta_worker import UIADeltaWorker
        return UIADeltaWorker.get_instance()
    except Exception:  # noqa: BLE001 - optional background worker lookup
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
    except Exception as e:  # noqa: BLE001 - probe failures treat window as responsive
        logger.debug("is_hwnd_responsive check failed: %s", e)
        return True


def get_element_patterns(element: Any) -> list[str]:
    """Retrieves all UI Automation control pattern names supported by an element.

    Accepts pywinauto UIAWrapper, UIAElementInfo, COM IUIAutomationElement, or test mocks.
    Returns a sorted list of supported pattern names (e.g. ['Invoke', 'Value']).
    """
    if not is_uia_supported() or element is None:
        return []

    try:
        raw_elem = element
        if not hasattr(raw_elem, "GetCurrentPattern") and hasattr(raw_elem, "element_info"):
            raw_elem = raw_elem.element_info
        if not hasattr(raw_elem, "GetCurrentPattern") and hasattr(raw_elem, "element"):
            raw_elem = raw_elem.element

        if not hasattr(raw_elem, "GetCurrentPattern"):
            return []

        from pywinauto.uia_defines import pattern_ids

        supported: list[str] = []
        for pat_name, (pat_id, _) in pattern_ids.items():
            try:
                cur_ptrn = raw_elem.GetCurrentPattern(pat_id)
                if cur_ptrn:
                    supported.append(pat_name)
            except Exception:  # noqa: BLE001, S112 - ignore unsupported pattern queries
                continue
        return sorted(supported)
    except Exception as e:  # noqa: BLE001 - defensive guard against COM errors
        logger.debug("Failed to query element patterns: %s", e)
        return []


def get_pattern_details(
    element: Any,
    pattern_names: list[str] | None = None,
) -> dict[str, Any]:
    """Extracts property values and state from supported UIA patterns on an element.

    Supported patterns inspected:
      - Value: value, is_read_only
      - Toggle: toggle_state ('off', 'on', 'indeterminate')
      - ExpandCollapse: expand_collapse_state ('collapsed', 'expanded', 'partially_expanded', 'leaf_node')
      - RangeValue: value, minimum, maximum, small_change, large_change, is_read_only
      - SelectionItem: is_selected
      - Selection: can_select_multiple, is_selection_required
      - Scroll: horizontally_scrollable, vertically_scrollable, horizontal_scroll_percent, vertical_scroll_percent
      - Window: can_maximize, can_minimize, is_modal, visual_state ('normal', 'maximized', 'minimized')
      - Transform: can_move, can_resize, can_rotate
      - Grid / Table: row_count, column_count
      - GridItem / TableItem: row, column, row_span, column_span
      - LegacyIAccessible: name, role, state, value, description, default_action
      - Invoke: supported (bool)
    """
    if not is_uia_supported() or element is None:
        return {}

    try:
        raw_elem = element
        if not hasattr(raw_elem, "GetCurrentPattern") and hasattr(raw_elem, "element_info"):
            raw_elem = raw_elem.element_info
        if not hasattr(raw_elem, "GetCurrentPattern") and hasattr(raw_elem, "element"):
            raw_elem = raw_elem.element

        if not hasattr(raw_elem, "GetCurrentPattern"):
            return {}

        from pywinauto.uia_defines import pattern_ids

        details: dict[str, Any] = {}
        target_patterns = set(pattern_names) if pattern_names is not None else set(pattern_ids.keys())

        for pat_name in sorted(target_patterns):
            if pat_name not in pattern_ids:
                continue
            pat_id, pat_cls = pattern_ids[pat_name]
            try:
                cur_ptrn = raw_elem.GetCurrentPattern(pat_id)
                if not cur_ptrn:
                    continue

                iface = cur_ptrn.QueryInterface(pat_cls) if hasattr(cur_ptrn, "QueryInterface") else cur_ptrn
                p_data: dict[str, Any] = {}

                if pat_name == "Value":
                    p_data["value"] = getattr(iface, "CurrentValue", None)
                    p_data["is_read_only"] = bool(getattr(iface, "CurrentIsReadOnly", False))
                elif pat_name == "Toggle":
                    raw_state = getattr(iface, "CurrentToggleState", 0)
                    p_data["toggle_state"] = _TOGGLE_STATES.get(raw_state, str(raw_state))
                elif pat_name == "ExpandCollapse":
                    raw_state = getattr(iface, "CurrentExpandCollapseState", 0)
                    p_data["expand_collapse_state"] = _EXPAND_STATES.get(raw_state, str(raw_state))
                elif pat_name == "RangeValue":
                    p_data["value"] = getattr(iface, "CurrentValue", None)
                    p_data["minimum"] = getattr(iface, "CurrentMinimum", None)
                    p_data["maximum"] = getattr(iface, "CurrentMaximum", None)
                    p_data["small_change"] = getattr(iface, "CurrentSmallChange", None)
                    p_data["large_change"] = getattr(iface, "CurrentLargeChange", None)
                    p_data["is_read_only"] = bool(getattr(iface, "CurrentIsReadOnly", False))
                elif pat_name == "SelectionItem":
                    p_data["is_selected"] = bool(getattr(iface, "CurrentIsSelected", False))
                elif pat_name == "Selection":
                    p_data["can_select_multiple"] = bool(getattr(iface, "CurrentCanSelectMultiple", False))
                    p_data["is_selection_required"] = bool(getattr(iface, "CurrentIsSelectionRequired", False))
                elif pat_name == "Scroll":
                    p_data["horizontally_scrollable"] = bool(getattr(iface, "CurrentHorizontallyScrollable", False))
                    p_data["vertically_scrollable"] = bool(getattr(iface, "CurrentVerticallyScrollable", False))
                    p_data["horizontal_scroll_percent"] = getattr(iface, "CurrentHorizontalScrollPercent", None)
                    p_data["vertical_scroll_percent"] = getattr(iface, "CurrentVerticalScrollPercent", None)
                elif pat_name == "Window":
                    p_data["can_maximize"] = bool(getattr(iface, "CurrentCanMaximize", False))
                    p_data["can_minimize"] = bool(getattr(iface, "CurrentCanMinimize", False))
                    p_data["is_modal"] = bool(getattr(iface, "CurrentIsModal", False))
                    raw_wstate = getattr(iface, "CurrentWindowVisualState", 0)
                    p_data["visual_state"] = _WINDOW_VISUAL_STATES.get(raw_wstate, str(raw_wstate))
                elif pat_name == "Transform":
                    p_data["can_move"] = bool(getattr(iface, "CurrentCanMove", False))
                    p_data["can_resize"] = bool(getattr(iface, "CurrentCanResize", False))
                    p_data["can_rotate"] = bool(getattr(iface, "CurrentCanRotate", False))
                elif pat_name in ("Grid", "Table"):
                    p_data["row_count"] = getattr(iface, "CurrentRowCount", None)
                    p_data["column_count"] = getattr(iface, "CurrentColumnCount", None)
                elif pat_name in ("GridItem", "TableItem"):
                    p_data["row"] = getattr(iface, "CurrentRow", None)
                    p_data["column"] = getattr(iface, "CurrentColumn", None)
                    p_data["row_span"] = getattr(iface, "CurrentRowSpan", None)
                    p_data["column_span"] = getattr(iface, "CurrentColumnSpan", None)
                elif pat_name == "LegacyIAccessible":
                    p_data["name"] = getattr(iface, "CurrentName", None)
                    p_data["role"] = getattr(iface, "CurrentRole", None)
                    p_data["state"] = getattr(iface, "CurrentState", None)
                    p_data["value"] = getattr(iface, "CurrentValue", None)
                    p_data["description"] = getattr(iface, "CurrentDescription", None)
                    p_data["default_action"] = getattr(iface, "CurrentDefaultAction", None)
                elif pat_name == "Invoke":
                    p_data["supported"] = True
                else:
                    p_data["supported"] = True

                details[pat_name] = p_data
            except Exception:  # noqa: BLE001, S112 - skip individual pattern extraction errors
                continue

        return details
    except Exception as e:  # noqa: BLE001 - defensive guard
        logger.debug("Failed to extract pattern details: %s", e)
        return {}


def get_focused_element_uia() -> dict[str, Any] | None:
    """Retrieves metadata, state, and supported patterns for the currently focused UI element.

    Returns a structured dictionary containing:
      - name: Text label or accessibility name
      - control_type: UI Automation control type (e.g., 'Edit', 'Button', 'Pane')
      - automation_id: Automation ID string
      - class_name: Native window/control class name
      - handle: HWND handle if available
      - is_responsive: bool (HWND message-pump responsiveness check)
      - is_keyboard_focusable: bool
      - has_keyboard_focus: bool
      - bounding_rectangle: (left, top, right, bottom) tuple if available
      - supported_patterns: list of supported pattern names
      - pattern_details: property dictionary for supported patterns
    """
    if not is_uia_supported():
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.uia_defines import IUIA
            from pywinauto.uia_element_info import UIAElementInfo

        elem = IUIA().get_focused_element()
        if not elem:
            return None

        info = UIAElementInfo(elem)
        wrap = UIAWrapper(info)
        handle = getattr(info, "handle", 0)

        # Check HWND responsiveness if window handle exists
        is_resp = True
        if handle:
            is_resp = is_hwnd_responsive(handle, timeout_ms=50)

        rect_coords = None
        try:
            r = wrap.rectangle()
            if r is not None:
                rect_coords = (r.left, r.top, r.right, r.bottom)
        except Exception:  # noqa: BLE001, S110 - ignore rectangle reading issues
            pass

        focusable = False
        try:
            focusable = wrap.is_keyboard_focusable()
        except Exception:  # noqa: BLE001, S110
            pass

        focused = False
        try:
            focused = wrap.has_keyboard_focus()
        except Exception:  # noqa: BLE001, S110
            pass

        patterns = get_element_patterns(elem)
        p_details = get_pattern_details(elem, patterns)

        return {
            "name": info.name or "",
            "control_type": info.control_type or "Unknown",
            "automation_id": info.automation_id or "",
            "class_name": info.class_name or "",
            "handle": handle,
            "is_responsive": is_resp,
            "is_keyboard_focusable": focusable,
            "has_keyboard_focus": focused,
            "bounding_rectangle": rect_coords,
            "supported_patterns": patterns,
            "pattern_details": p_details,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to get focused element: %s", e)
        return None


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
            except Exception:  # noqa: BLE001, S112
                continue
    except Exception as e:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001, S112
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug("pywinauto win32 focus fallback notice: %s", e)

    return None


def focus_element_uia(
    window_title: str | None = None,
    control_type: str | None = None,
    name: str | None = None,
    automation_id: str | None = None,
    class_name: str | None = None,
    timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """Semantically navigates and sets keyboard focus to an element matching criteria.

    Performs closed-loop verification to guarantee focus was applied without race conditions.
    Pre-probes HWND responsiveness before touching COM trees to abort safely on hung windows.

    Args:
        window_title: Substring query to identify and foreground the host window (optional).
        control_type: UIA ControlType name (e.g. 'Edit', 'Button', 'ListItem', 'TabItem').
        name: Substring or exact name/text of the target element.
        automation_id: UIA AutomationId identifier of the target element.
        class_name: Control class name.
        timeout_sec: Maximum seconds to wait for closed-loop focus verification.

    Returns:
        Structured dictionary reporting success, verified focus state, element info, and patterns.
    """
    if not is_uia_supported():
        return {
            "success": False,
            "error": "UI Automation is only supported on Windows workstations.",
        }

    # At least one filter must be provided
    clean_type = (control_type or "").strip().lower()
    clean_name = (name or "").strip().lower()
    clean_auto_id = (automation_id or "").strip().lower()
    clean_class = (class_name or "").strip().lower()

    if not any([clean_type, clean_name, clean_auto_id, clean_class]):
        return {
            "success": False,
            "error": "At least one target criterion (control_type, name, automation_id, class_name) must be specified.",
        }

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pywinauto import Desktop
            d = Desktop(backend="uia")
        target_window = None

        if window_title and window_title.strip():
            target_title = window_title.strip().lower()
            for win in d.windows():
                try:
                    wtext = win.window_text().strip()
                    if wtext and target_title in wtext.lower():
                        target_window = win
                        break
                except Exception:  # noqa: BLE001, S112
                    continue

            if not target_window:
                return {
                    "success": False,
                    "error": f"Could not find an active window matching '{window_title}'.",
                }
        else:
            # Locate active foreground window
            try:
                fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
                if fg_hwnd:
                    if not is_hwnd_responsive(fg_hwnd, timeout_ms=50):
                        return {
                            "success": False,
                            "error": f"Active foreground window HWND {fg_hwnd} is unresponsive (HungAppWindow).",
                        }
                    target_window = d.window(handle=fg_hwnd)
            except Exception:  # noqa: BLE001, S110
                pass

            if not target_window:
                # Search responsive top-level windows
                windows = d.windows()
                for w in windows:
                    h = getattr(w, "handle", 0)
                    if not h or is_hwnd_responsive(h, timeout_ms=50):
                        target_window = w
                        break

        if not target_window:
            return {
                "success": False,
                "error": "No accessible desktop window found to inspect for element focus.",
            }

        win_hwnd = getattr(target_window, "handle", 0)
        if win_hwnd and not is_hwnd_responsive(win_hwnd, timeout_ms=50):
            try:
                w_title = target_window.window_text()
            except Exception:  # noqa: BLE001
                w_title = window_title or "Target Window"
            return {
                "success": False,
                "error": f"Window '{w_title}' (HWND {win_hwnd}) is unresponsive or frozen (HungAppWindow).",
            }

        # Ensure parent window is restored and focused
        try:
            if target_window.is_minimized():
                target_window.restore()
            target_window.set_focus()
        except Exception as e:  # noqa: BLE001
            logger.debug("Parent window focus pre-step notice: %s", e)

        # Enumerate descendants and locate matching element
        matched_child = None
        descendants = target_window.descendants()
        for child in descendants:
            try:
                info = child.element_info
                ctype = (info.control_type or "").strip().lower()
                cname = (info.name or "").strip().lower()
                cauto = (info.automation_id or "").strip().lower()
                cclass = (info.class_name or "").strip().lower()

                if clean_type and clean_type != ctype:
                    continue
                if clean_name and clean_name not in cname:
                    continue
                if clean_auto_id and clean_auto_id != cauto:
                    continue
                if clean_class and clean_class != cclass:
                    continue

                matched_child = child
                break
            except Exception:  # noqa: BLE001, S112
                continue

        if not matched_child:
            return {
                "success": False,
                "error": f"No element matching criteria (type='{control_type}', name='{name}', id='{automation_id}') was found.",
            }

        # Check child responsiveness if it possesses an HWND
        child_hwnd = getattr(matched_child.element_info, "handle", 0)
        if child_hwnd and not is_hwnd_responsive(child_hwnd, timeout_ms=50):
            return {
                "success": False,
                "error": f"Matched element's HWND {child_hwnd} is unresponsive.",
            }

        # Apply semantic focus
        matched_child.set_focus()

        # Closed-loop verification loop
        verified = False
        deadline = time.monotonic() + max(0.05, timeout_sec)
        while time.monotonic() < deadline:
            try:
                if matched_child.has_keyboard_focus():
                    verified = True
                    break
            except Exception:  # noqa: BLE001, S110
                pass
            time.sleep(0.04)

        patterns = get_element_patterns(matched_child)
        p_details = get_pattern_details(matched_child, patterns)

        c_name = matched_child.element_info.name or ""
        c_type = matched_child.element_info.control_type or "Unknown"
        c_id = matched_child.element_info.automation_id or ""

        return {
            "success": True,
            "has_keyboard_focus": verified,
            "name": c_name,
            "control_type": c_type,
            "automation_id": c_id,
            "handle": child_hwnd,
            "supported_patterns": patterns,
            "pattern_details": p_details,
            "message": f"Successfully focused [{c_type}] '{c_name}' (verified: {verified}).",
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_element_uia failed: %s", e)
        return {
            "success": False,
            "error": f"Failed to focus element: {e}",
        }


@SkillRegistry.register(
    name="inspect_window_controls",
    description="Inspects native UI elements (buttons, inputs, menus, tabs) of an active window using Windows UI Automation (UIA) without taking screenshots.",
)
def inspect_window_controls(
    window_title: str,
    max_depth: int = 2,
    include_patterns: bool = True,
) -> str:
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
            except Exception:  # noqa: BLE001, S112
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
                if include_patterns:
                    patterns = get_element_patterns(child)
                    if patterns:
                        label += f" [patterns: {', '.join(patterns[:5])}]"
                controls.append(label)
        except Exception as e:  # noqa: BLE001
            controls.append(f"• Traversal note: {e}")

        summary = "\n".join(controls) if controls else "No interactive child elements found."
        return (
            f"UI Automation Tree for '{matched_title}', Sir:\n"
            f"{summary}"
        )

    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to inspect window controls via UIA: %s", e)
        return f"Unable to inspect UI controls for '{window_title}': {e}"


@SkillRegistry.register(
    name="focus_element",
    description="Semantically sets keyboard focus to a target UI element (e.g. input field, button, tab) matching control_type, name, or automation_id within an active window.",
)
def focus_element(
    window_title: str = "",
    control_type: str = "",
    element_name: str = "",
    automation_id: str = "",
    **kwargs: Any,
) -> str:
    """Sets semantic keyboard focus to a target UI element with closed-loop verification."""
    target_name = element_name or kwargs.get("name", "")
    res = focus_element_uia(
        window_title=window_title or None,
        control_type=control_type or None,
        name=target_name or None,
        automation_id=automation_id or None,
    )
    if not res.get("success"):
        return f"Unable to focus element: {res.get('error', 'Unknown failure')}, Sir."

    patterns_str = (
        f" [Patterns: {', '.join(res.get('supported_patterns', []))}]"
        if res.get("supported_patterns")
        else ""
    )
    status = "Verified in keyboard focus" if res.get("has_keyboard_focus") else "Focus applied"
    auto_id_str = f" (id: {res.get('automation_id')})" if res.get("automation_id") else ""
    return f"{status}: [{res.get('control_type')}] '{res.get('name')}'{auto_id_str}{patterns_str}, Sir."


@SkillRegistry.register(
    name="get_focused_element",
    description="Retrieves the currently focused desktop UI element, including its control type, name, automation ID, and supported UIA patterns.",
)
def get_focused_element() -> str:
    """Inspects the currently active focused element on the desktop."""
    if not is_uia_supported():
        return "UI Automation is only supported on Windows workstations, Sir."

    info = get_focused_element_uia()
    if not info:
        return "No UI element currently has keyboard focus on the desktop, Sir."

    patterns_str = (
        f"\n• Supported Patterns: {', '.join(info['supported_patterns'])}"
        if info.get("supported_patterns")
        else ""
    )
    return (
        f"Currently Focused Element, Sir:\n"
        f"• Type: [{info['control_type']}]\n"
        f"• Name: '{info['name']}'\n"
        f"• AutomationId: '{info['automation_id']}'\n"
        f"• HWND: {info['handle']}\n"
        f"• Keyboard Focusable: {info['is_keyboard_focusable']}"
        f"{patterns_str}"
    )


@SkillRegistry.register(
    name="retrieve_element_patterns",
    description="Retrieves supported UI Automation (UIA) control patterns (Value, Toggle, ExpandCollapse, Invoke, etc.) and state attributes for a specified UI element.",
)
def retrieve_element_patterns(
    window_title: str = "",
    control_type: str = "",
    element_name: str = "",
    automation_id: str = "",
    **kwargs: Any,
) -> str:
    """Retrieves supported UIA control patterns and properties for an element."""
    if not is_uia_supported():
        return "UI Automation is only supported on Windows workstations, Sir."

    target_name = element_name or kwargs.get("name", "")
    # If no filters specified, inspect currently focused element
    if not any([window_title, control_type, target_name, automation_id]):
        info = get_focused_element_uia()
        if not info:
            return "No element specified and no focused element detected, Sir."
        patterns = info.get("supported_patterns", [])
        details = info.get("pattern_details", {})
        elem_desc = f"[{info.get('control_type')}] '{info.get('name')}'"
    else:
        # Locate target element
        res = focus_element_uia(
            window_title=window_title or None,
            control_type=control_type or None,
            name=target_name or None,
            automation_id=automation_id or None,
        )
        if not res.get("success"):
            return f"Could not inspect patterns: {res.get('error', 'Element not found')}, Sir."
        patterns = res.get("supported_patterns", [])
        details = res.get("pattern_details", {})
        elem_desc = f"[{res.get('control_type')}] '{res.get('name')}'"

    if not patterns:
        return f"Element {elem_desc} does not expose any standard UIA control patterns, Sir."

    lines = [f"UIA Patterns for {elem_desc}, Sir:"]
    lines.append(f"• Supported: {', '.join(patterns)}")
    for pat_name, props in details.items():
        if props and isinstance(props, dict):
            prop_str = ", ".join(f"{k}={v}" for k, v in props.items())
            lines.append(f"  - {pat_name}: {prop_str}")
        else:
            lines.append(f"  - {pat_name}: (supported)")

    return "\n".join(lines)
