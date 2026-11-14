---
name: windows-automation
description: >-
  Native Windows UI Automation (UIA) and accessibility tree control for Jarvis 2.0.
  Replaces fragile synthetic keystrokes with semantic element inspection, reliable focus,
  address bar navigation, and race-condition-free window management via pywinauto.
---

# Windows Desktop UI Automation (UIA) Skill

This skill governs reliable, race-condition-free automation of the Windows desktop
environment using the **UI Automation (UIA)** framework via `pywinauto` and native Win32 APIs.

---

## 1. Core Principles

1. **Semantic First, Keystrokes Last**: Never send blind `pyautogui.hotkey()` or coordinate clicks when an element can be addressed via its UIA ControlType (e.g. `EditControl`, `TabControl`, `ButtonControl`, `AutomationId`).
2. **Deterministic Focus Verification**: Always verify that a window is in the foreground and restored before triggering actions (`win32gui.SetForegroundWindow`, `win32gui.ShowWindow`).
3. **No Redundant Tabs**: Inspect the window title and active tab state. If an application is sitting on a blank or new tab (`New Tab`, `about:blank`), reuse that tab directly via address bar (`Ctrl+L`) rather than opening duplicate tabs (`Ctrl+T`).
4. **Message Pump Responsiveness Pre-probing**: Pre-probe window handles (`is_hwnd_responsive`) via `IsHungAppWindow` and `SendMessageTimeoutW(SMTO_ABORTIFHUNG)` before querying COM UIA trees to prevent deadlocks on hung/frozen applications.

---

## 2. pywinauto UIA Recipes

### A. Attaching to an Active Desktop Application
```python
from pywinauto import Application

# Connect to running browser or app via process title or exe name
app = Application(backend="uia").connect(title_re=".*Brave.*", timeout=2.0)
win = app.top_window()
win.set_focus()
```

### B. Finding and Navigating the Address Bar in Chromium Browsers
```python
# Locate address bar semantically across Brave, Chrome, or Edge
address_bar = win.descendants(control_type="Edit")[0]
address_bar.set_focus()
address_bar.set_edit_text("https://youtube.com")
address_bar.type_keys("{ENTER}")
```

### C. Safe Top-Level Window Enumeration
Avoid `EnumWindows` crashes on Windows 11 by guarding window handles:
```python
import win32gui

def get_visible_windows(app_name: str) -> list[tuple[int, str]]:
    matches = []
    def _enum(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                if title and app_name.lower() in title.lower():
                    matches.append((hwnd, title))
        except Exception:
            pass
        return True
    try:
        win32gui.EnumWindows(_enum, None)
    except Exception:
        pass
    return matches
```

---

## 3. Semantic Focus & Pattern Retrieval Recipes

### D. Semantic Element Focus with Closed-Loop Verification
Focus a specific target element semantically within an application window:
```python
from jarvis.skills.windows_uia import focus_element_uia

# Focus the Search or Address box within Brave Browser
result = focus_element_uia(
    window_title="Brave",
    control_type="Edit",
    name="Address and search bar",
    timeout_sec=1.5,
)
if result["success"] and result["has_keyboard_focus"]:
    print(f"Focused [{result['control_type']}] '{result['name']}' successfully.")
```

### E. Element Control Pattern Retrieval & State Inspection
Extract supported UIA control patterns (Value, Toggle, ExpandCollapse, RangeValue, Invoke, etc.) and their properties:
```python
from jarvis.skills.windows_uia import get_element_patterns, get_pattern_details

# Query supported patterns on a UIA wrapper or element
patterns = get_element_patterns(element)
details = get_pattern_details(element, patterns)

# Value pattern inspection
if "Value" in details:
    print(f"Current Value: {details['Value']['value']} (read-only: {details['Value']['is_read_only']})")

# Toggle pattern inspection
if "Toggle" in details:
    print(f"Toggle State: {details['Toggle']['toggle_state']}")  # 'on', 'off', 'indeterminate'

# ExpandCollapse pattern inspection
if "ExpandCollapse" in details:
    print(f"Expand State: {details['ExpandCollapse']['expand_collapse_state']}")
```

### F. Querying the Active Keyboard Focused Element
Inspect the active element currently receiving keyboard input across the desktop:
```python
from jarvis.skills.windows_uia import get_focused_element_uia

focused_info = get_focused_element_uia()
if focused_info:
    print(f"Focused Type: [{focused_info['control_type']}], Name: '{focused_info['name']}', HWND: {focused_info['handle']}")
    print(f"Patterns: {focused_info['supported_patterns']}")
```
