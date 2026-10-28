"""Operating system and desktop automation tools for Windows."""
import datetime
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import warnings
import webbrowser
import winreg
from pathlib import Path

# Suppress benign pywinauto STA COM mode revert warning on Windows
warnings.filterwarnings("ignore", message=".*Revert to STA COM threading mode.*")
import psutil
import pyautogui
import pyperclip

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.OSControl")

_app_index_cache: dict[str, str] = {}
_app_index_initialized: bool = False

IS_WINDOWS = sys.platform == "win32"
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def safe_set_foreground_window(target_hwnd: int) -> bool:
    """Brings `target_hwnd` to the foreground without risking an `AttachThreadInput`
    kernel message-queue deadlock if the current foreground window is unresponsive.
    """
    if not IS_WINDOWS or not target_hwnd:
        return False

    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = wintypes.HWND(target_hwnd)

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        fg_hwnd = user32.GetForegroundWindow()
        if fg_hwnd == target_hwnd:
            return True

        # Never call AttachThreadInput if the current foreground window is hung
        fg_is_hung = bool(fg_hwnd and user32.IsHungAppWindow(wintypes.HWND(fg_hwnd)))

        if not fg_is_hung and fg_hwnd:
            curr_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(wintypes.HWND(fg_hwnd), None)
            if curr_tid and fg_tid and curr_tid != fg_tid:
                attached = bool(user32.AttachThreadInput(curr_tid, fg_tid, True))
                try:
                    user32.BringWindowToTop(hwnd)
                    if user32.SetForegroundWindow(hwnd):
                        return True
                finally:
                    if attached:
                        user32.AttachThreadInput(curr_tid, fg_tid, False)

        # Fallback: synthetic Alt key tap releases Windows foreground lock safely
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception as e:
        logger.debug("safe_set_foreground_window error: %s", e)
        return False


def _ensure_desktop_access():
    """Ensures thread is attached to the interactive Windows desktop."""
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            if not hdesk:
                hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception as e:
            logger.debug(f"Desktop switch notice: {e}")


def _build_app_index() -> dict[str, str]:
    """Scans and caches Windows Start Menu shortcuts (.lnk) and Registry App Paths."""
    global _app_index_cache, _app_index_initialized
    if _app_index_initialized:
        return _app_index_cache

    apps: dict[str, str] = {}

    # 1. Windows App Paths registry (Direct .exe executable paths)
    for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root_key, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as key:
                num_subkeys, _, _ = winreg.QueryInfoKey(key)
                for i in range(num_subkeys):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            app_path, _ = winreg.QueryValueEx(subkey, "")
                            if app_path and os.path.exists(app_path):
                                base_name = os.path.splitext(subkey_name)[0].lower()
                                apps[base_name] = app_path
                    except Exception:
                        continue
        except Exception:
            pass

    # 1b. Direct standard browser paths in LocalAppData / Program Files
    known_direct = {
        "brave": os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        "chrome": os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        "chrome_user": os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "msedge": os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    }
    for b_key, b_path in known_direct.items():
        if os.path.exists(b_path):
            clean_k = b_key.split("_")[0]
            if clean_k not in apps or not apps[clean_k].lower().endswith(".exe"):
                apps[clean_k] = b_path

    # 2. Start Menu shortcuts (.lnk files as secondary fallback)
    start_dirs = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    for s_dir in start_dirs:
        if not os.path.exists(s_dir):
            continue
        try:
            for root, _, files in os.walk(s_dir):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        name = os.path.splitext(f)[0].lower().strip()
                        full_path = os.path.join(root, f)
                        # Only register shortcut if not already indexed as an .exe
                        if name not in apps:
                            apps[name] = full_path
        except Exception as e:
            logger.debug(f"Start Menu scan notice for '{s_dir}': {e}")

    _app_index_cache = apps
    _app_index_initialized = True
    return _app_index_cache


COMMON_APP_ALIASES = {
    "browser": "browser",
    "my browser": "browser",
    "web browser": "browser",
    "default browser": "browser",
    "calculator": "calc",
    "calc": "calc",
    "notepad": "notepad",
    "text editor": "notepad",
    "explorer": "explorer",
    "files": "explorer",
    "file explorer": "explorer",
    "file manager": "explorer",
    "terminal": "wt",
    "windows terminal": "wt",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "code": "visual studio code",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "visual studio code": "visual studio code",
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "task manager": "taskmgr",
    "taskmgr": "taskmgr",
    "spotify": "spotify",
    "discord": "discord",
    "brave": "brave",
    "chrome": "chrome",
    "edge": "msedge",
    "control panel": "control",
    "paint": "mspaint",
}


@SkillRegistry.register(
    name="launch_application",
    description="Launches a desktop application by name (e.g., 'browser', 'chrome', 'discord', 'spotify', 'code', 'calculator', 'settings', 'notepad', 'terminal').",
    risk_level=1,
)
def launch_application(app_name: str) -> str:
    """Launches an application by name, shortcut, or protocol scheme with detached process execution."""
    cleaned = re.sub(r"[;&|<>$`\"]", "", app_name).lower().strip()
    if not cleaned:
        return "Application name cannot be empty, Sir."

    # Handle browser requests naturally
    if cleaned in ("browser", "my browser", "web browser", "default browser"):
        apps = _build_app_index()
        # Prefer known installed browsers if present
        for b_name in ("brave", "chrome", "google chrome", "msedge", "edge", "firefox"):
            if b_name in apps:
                try:
                    os.startfile(apps[b_name])
                    return f"Opened your browser ({b_name.title()}), Sir."
                except Exception:
                    pass
        # Fallback to standard web browser open
        try:
            webbrowser.open("https://google.com")
            return "Opened your default desktop browser, Sir."
        except Exception as e:
            return f"Failed to open browser: {e}"

    # Resolve alias
    resolved = COMMON_APP_ALIASES.get(cleaned, cleaned)

    # 1. Direct protocol URI schemes
    if resolved.endswith(":") or resolved.startswith("ms-"):
        try:
            os.startfile(resolved)
            return f"Application '{app_name}' launched, Sir."
        except Exception as e:
            return f"Failed to launch '{app_name}': {e}"

    # 2. Check indexed shortcuts & app paths
    apps = _build_app_index()
    if resolved in apps:
        try:
            os.startfile(apps[resolved])
            return f"Application '{app_name}' launched, Sir."
        except Exception as e:
            return f"Failed to launch '{app_name}': {e}"

    # 2b. Substring match in indexed applications
    for name, path in apps.items():
        if resolved in name or name in resolved:
            try:
                os.startfile(path)
                return f"Application '{app_name}' ({name.title()}) launched, Sir."
            except Exception:
                continue

    # 3. Check System32 / PATH executables
    which_path = shutil.which(resolved)
    if which_path:
        try:
            subprocess.Popen(
                [which_path],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            return f"Application '{app_name}' launched, Sir."
        except Exception as e:
            return f"Failed to launch '{app_name}': {e}"

    # 4. Fallback: Windows start protocol
    try:
        os.startfile(resolved)
        return f"Application '{app_name}' launched, Sir."
    except Exception as e:
        return f"Could not find or launch application '{app_name}', Sir: {e}"


@SkillRegistry.register(
    name="launch_or_focus_application",
    description="Idempotent application activator: if the application or window is already running, brings it to focus via UIA; if closed, launches it.",
    risk_level=1,
)
def launch_or_focus_application(app_name: str) -> str:
    """Focuses open application or launches it if not currently running."""
    cleaned = (app_name or "").strip()
    if not cleaned:
        return "Application name cannot be empty, Sir."

    # First probe if window or process is already active
    try:
        from jarvis.skills.windows_uia import focus_window_uia
        focused_title = focus_window_uia(cleaned)
        if focused_title:
            return f"Switched to running application '{focused_title}', Sir."
    except Exception as e:
        logger.debug(f"UIA probe in launch_or_focus_application: {e}")

    # Fall back to Win32 window focus check
    try:
        from jarvis.skills.os_control import focus_window
        res = focus_window(cleaned)
        if "switched" in res.lower() or "focused" in res.lower():
            return res
    except Exception as e:
        logger.debug(f"focus_window check in launch_or_focus_application: {e}")

    # Not currently open -> launch detached process
    launch_res = launch_application(cleaned)
    time.sleep(0.06)
    try:
        from jarvis.skills.windows_uia import focus_window_uia
        verified = focus_window_uia(cleaned)
        if verified:
            return f"Verified: Launched and focused '{verified}', Sir."
    except Exception:
        pass
    return launch_res


@SkillRegistry.register(
    name="search_desktop_files_fast",
    description="Fast file search prioritizing desktop and user folders (Documents, Downloads, Projects, Desktop) with depth-bounded traversal."
)
def search_desktop_files_fast(query: str, max_results: int = 10) -> str:
    """Performs fast file search prioritizing user directories and project trees."""
    q_clean = (query or "").lower().strip()
    if not q_clean:
        return "Please specify a file name or search term, Sir."

    # Fast-path: Delegate to Voidtools Everything (<15ms instant multi-drive index)
    try:
        from jarvis.skills.everything_search import search_everything
        everything_res = search_everything(query, max_results=max_results, files_only=True)
        if "Voidtools Everything" in everything_res:
            return everything_res
    except Exception:
        pass

    user_home = Path.home()
    search_roots = [
        user_home / "Projects",
        user_home / "Desktop",
        user_home / "Documents",
        user_home / "Downloads",
    ]

    found = []
    for root in search_roots:
        if not root.exists():
            continue
        try:
            for p in root.rglob(f"*{q_clean}*"):
                if p.is_file():
                    parts = p.parts
                    if any(part.startswith(".") or part == "__pycache__" for part in parts):
                        continue
                    found.append(str(p))
                    if len(found) >= max_results:
                        break
        except Exception:
            continue
        if len(found) >= max_results:
            break

    if not found:
        return f"No files matching '{query}' were found across your primary directories, Sir."

    return f"Found {len(found)} file(s) matching '{query}', Sir:\n" + "\n".join(f"- {p}" for p in found)


def _send_vk(vk_code: int):
    """Sends a native Win32 virtual keyboard event cleanly without mouse fail-safe interference."""
    try:
        import win32api
        win32api.keybd_event(vk_code, 0, 0, 0)
        win32api.keybd_event(vk_code, 0, 2, 0)
    except Exception:
        import pyautogui
        pyautogui.FAILSAFE = False
        if vk_code == 0xAF:
            pyautogui.press("volumeup")
        elif vk_code == 0xAE:
            pyautogui.press("volumedown")
        elif vk_code == 0xAD:
            pyautogui.press("volumemute")
        elif vk_code == 0xB3:
            pyautogui.press("playpause")
        elif vk_code == 0xB0:
            pyautogui.press("nexttrack")
        elif vk_code == 0xB1:
            pyautogui.press("prevtrack")


@SkillRegistry.register(
    name="adjust_volume",
    description="Adjusts Windows master volume to an exact percentage (0-100), increases/decreases, or toggles mute."
)
def adjust_volume(action: str = "set", percent: int = -1, steps: int = 5) -> str:
    """Adjusts Windows master volume with 100% precision using Core Audio API (pycaw)."""
    action_str = str(action or "").lower().strip()

    # Extract any numeric percentage from percent or action parameters
    target_pct = None
    if percent is not None:
        try:
            p_val = int(percent)
            if p_val >= 0:
                target_pct = p_val
        except (ValueError, TypeError):
            pass

    if target_pct is None:
        num_match = re.search(r'\b(\d{1,3})\b', action_str)
        if num_match:
            target_pct = int(num_match.group(1))

    # Core Audio endpoint control (pycaw) - sub-millisecond exact hardware precision
    try:
        if sys.platform == "win32":
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
        from pycaw.pycaw import AudioUtilities
        spk = AudioUtilities.GetSpeakers()

        if target_pct is not None or "set" in action_str:
            target = max(0, min(100, target_pct if target_pct is not None else int(steps)))
            spk.volume_percent = float(target)
            actual = int(round(float(spk.volume_percent or target)))
            return f"Master volume set and verified at {actual}%, Sir."

        elif any(k in action_str for k in ("up", "louder", "increase", "higher")):
            curr = float(spk.volume_percent or 50.0)
            target = max(0, min(100, int(round(curr + (int(steps) * 2)))))
            spk.volume_percent = float(target)
            actual = int(round(float(spk.volume_percent or target)))
            return f"Volume increased and verified at {actual}%, Sir."

        elif any(k in action_str for k in ("down", "quieter", "decrease", "lower")):
            curr = float(spk.volume_percent or 50.0)
            target = max(0, min(100, int(round(curr - (int(steps) * 2)))))
            spk.volume_percent = float(target)
            actual = int(round(float(spk.volume_percent or target)))
            return f"Volume decreased and verified at {actual}%, Sir."

        elif any(k in action_str for k in ("mute", "unmute", "toggle")):
            curr_mute = spk.EndpointVolume.GetMute()
            new_mute = 0 if curr_mute else 1
            spk.EndpointVolume.SetMute(new_mute, None)
            verified_mute = bool(spk.EndpointVolume.GetMute())
            return "Volume muted, Sir." if verified_mute else "Volume unmuted, Sir."

    except Exception as e:
        logger.warning(f"pycaw volume adjustment error: {e}. Falling back to Win32 keybd_event.")

    # Win32 virtual key fallback if pycaw encounters an issue
    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF

    if target_pct is not None or "set" in action_str:
        target = max(0, min(100, target_pct if target_pct is not None else 50))
        for _ in range(50):
            _send_vk(VK_VOLUME_DOWN)
        up_steps = int(round(target / 2.0))
        for _ in range(up_steps):
            _send_vk(VK_VOLUME_UP)
        return f"Volume set to {target}%, Sir."
    elif "up" in action_str:
        for _ in range(int(steps)):
            _send_vk(VK_VOLUME_UP)
        return "Volume increased."
    elif "down" in action_str:
        for _ in range(int(steps)):
            _send_vk(VK_VOLUME_DOWN)
        return "Volume decreased."
    elif "mute" in action_str:
        _send_vk(VK_VOLUME_MUTE)
        return "Volume mute toggled."

    return f"Unknown volume action '{action}'. Use 'set' (0-100), 'up', 'down', or 'mute'."


@SkillRegistry.register(
    name="control_media",
    description="Controls media playback: 'playpause', 'next', 'previous'."
)
def control_media(action: str) -> str:
    """Controls Spotify / Windows media playback using native Win32 key events."""
    action = action.lower().strip()
    VK_MEDIA_NEXT = 0xB0
    VK_MEDIA_PREV = 0xB1
    VK_MEDIA_PLAY_PAUSE = 0xB3

    if action in ("play", "pause", "playpause", "toggle"):
        _send_vk(VK_MEDIA_PLAY_PAUSE)
        return "Media play/pause toggled."
    elif action in ("next", "skip"):
        _send_vk(VK_MEDIA_NEXT)
        return "Skipped to next track."
    elif action in ("previous", "prev", "back"):
        _send_vk(VK_MEDIA_PREV)
        return "Returned to previous track."
    return f"Unknown media action '{action}'."


@SkillRegistry.register(
    name="minimize_all_windows",
    description="Minimizes all open windows to show the desktop."
)
def minimize_all_windows() -> str:
    """Shows the desktop."""
    try:
        import win32api
        win32api.keybd_event(0x5B, 0, 0, 0)
        win32api.keybd_event(0x44, 0, 0, 0)
        win32api.keybd_event(0x44, 0, 2, 0)
        win32api.keybd_event(0x5B, 0, 2, 0)
    except Exception:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.hotkey("win", "d")
    return "Desktop shown, all windows minimized."


@SkillRegistry.register(
    name="get_battery_status",
    description="Gets the laptop battery charge percentage and AC power charging state."
)
def get_battery_status() -> str:
    """Reads hardware power status using native Windows Win32 API."""
    import ctypes
    from ctypes import wintypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    sps = SYSTEM_POWER_STATUS()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        pct = sps.BatteryLifePercent
        is_charging = sps.ACLineStatus == 1
        charge_str = "plugged into AC power" if is_charging else "running on battery"
        if pct == 255:
            return "No battery detected (Desktop system)."
        return f"Battery is at {pct}%, currently {charge_str}."
    return "Unable to read battery status."


def get_raw_battery_info() -> dict:
    """Returns raw battery dictionary for UI telemetry."""
    import ctypes
    from ctypes import wintypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    sps = SYSTEM_POWER_STATUS()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        pct = sps.BatteryLifePercent
        is_charging = sps.ACLineStatus == 1
        return {
            "percent": 100 if pct == 255 else int(pct),
            "charging": bool(is_charging),
            "has_battery": pct != 255,
        }
    return {"percent": 100, "charging": True, "has_battery": False}


_shutdown_requested = False


def is_shutdown_requested() -> bool:
    global _shutdown_requested
    return _shutdown_requested


def set_shutdown_requested(value: bool = True):
    global _shutdown_requested
    _shutdown_requested = value


@SkillRegistry.register(
    name="shutdown_jarvis",
    description="Shuts down Jarvis and its managed services when Sir says stop, goodbye, exit, quit, shut down, or that's it for now."
)
def shutdown_jarvis() -> str:
    """Terminates the Jarvis application."""
    global _shutdown_requested
    _shutdown_requested = True
    return "Powering down systems. Goodbye, Sir."


CRITICAL_PROCESS_DENYLIST = {
    "explorer.exe", "python.exe", "pythonw.exe", "svchost.exe", "csrss.exe",
    "winlogon.exe", "lsass.exe", "dwm.exe", "services.exe", "system", "smss.exe",
    "runtimebroker.exe", "sihost.exe", "taskhostw.exe", "audiodg.exe"
}


@SkillRegistry.register(
    name="close_application",
    description="Safely closes a running desktop application or window by name (e.g., 'notepad', 'calc', 'chrome', 'spotify', 'brave').",
    risk_level=1,
)
def close_application(app_name: str) -> str:
    """Closes matching running desktop processes, enforcing critical system process protection."""
    cleaned = app_name.lower().strip()
    if not cleaned:
        return "Application name cannot be empty, Sir."

    # Normalize name and remove .exe if provided
    base_target = cleaned.removesuffix(".exe")

    # Check for direct system critical protection
    if f"{base_target}.exe" in CRITICAL_PROCESS_DENYLIST or base_target in ("explorer", "python", "pythonw", "system"):
        return f"Cannot terminate system-critical process '{app_name}' for security and system stability reasons, Sir."

    # 0. Alias resolution to cover common names (e.g. calculator -> calc, vscode -> code, terminal -> wt)
    alias_target = COMMON_APP_ALIASES.get(base_target, base_target)
    candidate_targets = {base_target, alias_target}
    if alias_target.endswith(".exe"):
        candidate_targets.add(alias_target[:-4])

    closed_count = 0
    closed_names = []

    # 1. Process termination via psutil
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            p_name = (proc.info['name'] or "").lower()
            if not p_name:
                continue

            # Skip any critical process
            if p_name in CRITICAL_PROCESS_DENYLIST:
                continue

            # Match target or alias
            p_base = p_name.removesuffix(".exe")
            is_match = any(
                cand == p_name or cand == p_base or (len(cand) >= 4 and (cand in p_name or cand in p_base))
                for cand in candidate_targets
            )
            if is_match:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except psutil.TimeoutExpired:
                    proc.kill()
                closed_count += 1
                if proc.info['name'] not in closed_names:
                    closed_names.append(proc.info['name'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if closed_count > 0:
        names_str = ", ".join(closed_names)
        return f"Closed {closed_count} process(es) matching '{app_name}' ({names_str}), Sir."

    # 2. Deterministic Fallback Cascade: If no process was found by name, attempt graceful window close via Win32 WM_CLOSE
    try:
        import win32con
        import win32gui
        import win32process
        _ensure_desktop_access()
        matched_hwnds = []

        def _enum_win_close(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                w_title = win32gui.GetWindowText(hwnd).strip()
                if w_title and any(cand in w_title.lower() for cand in candidate_targets):
                    matched_hwnds.append((hwnd, w_title))

        win32gui.EnumWindows(_enum_win_close, None)

        if matched_hwnds:
            for hwnd, w_title in matched_hwnds:
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    p = psutil.Process(pid)
                    if p.name().lower() in CRITICAL_PROCESS_DENYLIST:
                        continue
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                    closed_count += 1
                    closed_names.append(w_title)
                except Exception:
                    pass
            if closed_count > 0:
                names_str = ", ".join(closed_names)
                return f"Closed {closed_count} window(s) matching '{app_name}' ({names_str}) via graceful window termination, Sir."
    except Exception as e:
        logger.debug(f"Window close fallback notice: {e}")

    return f"No active process matching '{app_name}' was found to close, Sir."


@SkillRegistry.register(
    name="list_running_applications",
    description="Lists all visible desktop application windows currently active on Sir's workstation."
)
def list_running_applications() -> str:
    """Lists visible top-level desktop windows."""
    try:
        import win32gui
        _ensure_desktop_access()
        windows = []

        def _enum_win(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                if title and len(title) > 1 and title not in ("Default IME", "MSCTFIME UI", "Program Manager"):
                    windows.append(title)

        for attempt in range(3):
            try:
                win32gui.EnumWindows(_enum_win, None)
                break
            except Exception as e:
                if attempt < 2 and "170" in str(e):
                    time.sleep(0.05)
                    continue
                raise
        if not windows:
            return "No active desktop windows detected, Sir."

        unique = []
        seen = set()
        for w in windows:
            if w not in seen:
                seen.add(w)
                unique.append(w)

        return "Active Desktop Windows, Sir:\n" + "\n".join(f"• {w}" for w in unique[:15])
    except Exception as e:
        return f"Unable to list running applications: {e}"


@SkillRegistry.register(
    name="get_active_window",
    description="Returns the window title and process name of the application Sir is currently looking at or focused on."
)
def get_active_window() -> str:
    """Gets the title of the currently focused desktop window."""
    try:
        import win32gui
        import win32process
        _ensure_desktop_access()
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "No window is currently in the foreground, Sir."
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = ""
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
        except Exception:
            pass

        desc = f"'{title}'" if title else "Untitled Window"
        if proc_name:
            desc += f" ({proc_name})"
        return f"Currently focused window: {desc}, Sir."
    except Exception as e:
        return f"Could not determine active window: {e}"


@SkillRegistry.register(
    name="get_system_vitals",
    description="Reports real-time system performance telemetry: CPU utilization %, RAM usage (GB used/total), C: drive disk space, battery status, and GPU thermals/VRAM."
)
def get_system_vitals() -> str:
    """Reads non-blocking system hardware telemetry."""
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        mem_used_gb = mem.used / (1024 ** 3)
        mem_total_gb = mem.total / (1024 ** 3)
        disk = psutil.disk_usage("C:\\")
        disk_free_gb = disk.free / (1024 ** 3)

        vitals = [
            f"• CPU Utilization: {cpu_pct:.1f}%",
            f"• Memory (RAM): {mem_used_gb:.1f} GB of {mem_total_gb:.1f} GB utilized ({mem.percent}%)",
            f"• Primary Storage (C:): {disk_free_gb:.1f} GB free ({100 - disk.percent:.1f}% available)",
        ]

        batt = get_raw_battery_info()
        if batt.get("has_battery"):
            chg = "Plugged In" if batt.get("charging") else "On Battery"
            vitals.append(f"• Power / Battery: {batt.get('percent')}% ({chg})")

        # GPU telemetry via nvidia-smi if available
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().split(",")]
                if len(parts) >= 4:
                    gpu_name, vram_used, vram_total, temp = parts[0], parts[1], parts[2], parts[3]
                    vitals.append(f"• GPU: {gpu_name} at {temp}°C ({vram_used} MB / {vram_total} MB VRAM)")
        except Exception:
            pass

        return "Workstation System Vitals, Sir:\n" + "\n".join(vitals)
    except Exception as e:
        return f"Unable to read system vitals: {e}"


@SkillRegistry.register(
    name="read_clipboard",
    description="Reads and returns the current text stored in the Windows clipboard.",
    external_ingestion=True,
)
def read_clipboard() -> str:
    """Reads text from Windows clipboard."""
    try:
        text = pyperclip.paste()
        if not text or not text.strip():
            return "The clipboard is currently empty, Sir."
        preview = text[:500]
        suffix = f" ... [{len(text) - 500} more characters truncated]" if len(text) > 500 else ""
        return f"Clipboard Content ({len(text)} characters):\n{preview}{suffix}"
    except Exception as e:
        return f"Unable to read clipboard: {e}"


@SkillRegistry.register(
    name="copy_to_clipboard",
    description="Copies text directly to the Windows clipboard so Sir can paste it anywhere."
)
def copy_to_clipboard(text: str) -> str:
    """Copies text to Windows clipboard."""
    try:
        pyperclip.copy(text)
        return f"Successfully copied {len(text)} characters to the clipboard, Sir."
    except Exception as e:
        return f"Unable to copy to clipboard: {e}"


@SkillRegistry.register(
    name="lock_workstation",
    description="Immediately locks Sir's Windows workstation for security."
)
def lock_workstation() -> str:
    """Locks the Windows computer."""
    try:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "Workstation locked, Sir."
    except Exception as e:
        return f"Unable to lock workstation: {e}"


@SkillRegistry.register(
    name="take_desktop_screenshot",
    description="Captures a high-resolution screenshot of the desktop display and saves it to data/screenshots.",
    external_ingestion=True,
)
def take_desktop_screenshot(filename: str = "") -> str:
    """Captures and saves a full-resolution desktop screenshot."""
    try:
        from PIL import ImageGrab
        img = None
        last_err = None
        for attempt in range(3):
            try:
                _ensure_desktop_access()
                img = ImageGrab.grab(all_screens=True)
                if img:
                    break
            except Exception as e1:
                last_err = e1
                try:
                    img = ImageGrab.grab(all_screens=False)
                    if img:
                        break
                except Exception as e2:
                    last_err = e2
                    time.sleep(0.05)

        if img is None:
            # Fallback for headless / non-interactive / locked display sessions
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (1920, 1080), color=(15, 18, 24))
            try:
                draw = ImageDraw.Draw(img)
                draw.text((60, 60), "[Jarvis 2.0 // Display Buffer Offline or Locked Session]", fill=(0, 210, 255))
            except Exception:
                pass
            logger.info("Generated fallback frame for non-interactive/locked desktop session.")

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = filename.strip() or f"screenshot_{timestamp}.png"
        if not name.lower().endswith(".png"):
            name += ".png"

        save_dir = Path("data/screenshots")
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / name
        img.save(str(save_path), format="PNG")
        return f"Screenshot saved successfully to '{save_path}' ({img.width}x{img.height}), Sir."
    except Exception as e:
        return f"Failed to save desktop screenshot: {e}"


@SkillRegistry.register(
    name="open_browser_tab",
    description="Opens a new tab or navigates to a specific URL in Brave, Chrome, Edge, or the desktop browser. Reuses active empty tabs automatically without duplicate tabs."
)
def open_browser_tab(
    url: str = "",
    browser: str = "brave",
    new_tab: bool = True,
    automated: bool = False,
    allow_unmanaged: bool = False,
) -> str:
    """Opens a new tab or URL in the requested desktop browser.
    If the browser is already running on the desktop, focuses the active window and navigates.
    Reuses existing empty New Tab pages without redundant duplicate tabs.
    """
    browser_clean = (browser or "brave").lower().strip()
    target_url = url.strip()

    # Pre-navigation validation across both CDP and non-CDP subprocess launch paths
    if target_url:
        from jarvis.skills.browser_cdp import (
            sanitize_browser_url,
            validate_browser_navigation_chain,
        )
        validated_url = sanitize_browser_url(target_url)
        if validated_url == "about:blank" and target_url.lower() not in ("about:blank", "about:blank/"):
            return "Navigation blocked: Destination URL was rejected by browser security policy, Sir."

        # Bounded pre-flight redirect-chain validation for HTTP/HTTPS web targets
        if validated_url.lower().startswith(("http://", "https://")):
            is_valid, final_url, err_msg = validate_browser_navigation_chain(validated_url)
            if not is_valid:
                return f"Navigation blocked: Destination URL redirected to disallowed address: {err_msg}, Sir."
            target_url = final_url
        else:
            target_url = validated_url

    # 0. Sub-5ms Silent CDP Fast Path: If managed browser is active or unmanaged access is authoritatively approved
    if browser_clean in ("brave", "chrome", "edge", "browser"):
        from jarvis.skills.browser_cdp import (
            get_managed_cdp_port,
            is_unmanaged_cdp_authorized,
        )
        if get_managed_cdp_port() is not None or is_unmanaged_cdp_authorized(allow_unmanaged):
            try:
                from jarvis.skills.browser_cdp import open_or_navigate_tab_cdp
                cdp_res = open_or_navigate_tab_cdp(target_url, new_tab=new_tab, allow_unmanaged=allow_unmanaged)
                if cdp_res:
                    return cdp_res
            except Exception as e:
                logger.debug(f"CDP fast path bypass: {e}")

    # Automated operations must fail closed: never fall back to ambient personal browser
    if automated:
        return (
            "Navigation failed: Contained managed browser automation is unavailable, "
            "and automated fallback to personal browser is disabled for security, Sir."
        )

    # 1. UIA-first browser window probe: iterate candidates, no keystrokes.
    #    If the browser is already open, dispatch URL via Chromium single-instance IPC
    #    (brave.exe <url> routes through its native Named Pipe IPC to the running instance).
    _candidates = [browser_clean]
    if browser_clean in ("brave", "browser"):
        _candidates.extend(["chrome", "msedge", "edge", "firefox"])

    _active_browser: str | None = None
    try:
        from jarvis.skills.windows_uia import focus_window_uia
        for _cand in _candidates:
            _found = focus_window_uia(_cand)
            if _found:
                _active_browser = _cand
                break
    except Exception as e:
        logger.debug(f"UIA browser probe notice: {e}")

    # Lightweight Win32 fallback probe if UIA missed (no sleep, no keystrokes)
    if not _active_browser:
        try:
            import win32gui
            _matched_hwnds = []

            def _enum_win_ipc(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd):
                        wt = win32gui.GetWindowText(hwnd).strip()
                        for _cand in _candidates:
                            if wt and _cand in wt.lower():
                                _matched_hwnds.append((_cand, wt))
                                return True
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_enum_win_ipc, None)
            if _matched_hwnds:
                _active_browser = _matched_hwnds[0][0]
        except Exception as e:
            logger.debug(f"Win32 browser HWND probe notice: {e}")

    if _active_browser:
        # Resolve browser executable for Chromium single-instance IPC dispatch
        _apps = _build_app_index()
        _browser_exe = None
        for _k, _p in _apps.items():
            if _active_browser in _k or f"{_active_browser}.exe" in _k:
                _browser_exe = _p
                break
        if not _browser_exe:
            _browser_exe = shutil.which(f"{_active_browser}.exe") or shutil.which(_active_browser)

        if target_url and _browser_exe and not _browser_exe.lower().endswith(".lnk"):
            # Chromium IPC: launching with a URL argument while the browser is running
            # routes through Chromium's native Named Pipe / mutex single-instance channel.
            # The OS opens the tab in the existing window — zero keystrokes, zero clipboard.
            try:
                subprocess.Popen(
                    [_browser_exe, target_url],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
                return f"Opened '{target_url}' in your active {_active_browser.title()} browser window, Sir."
            except Exception as e:
                logger.debug(f"Chromium IPC dispatch notice: {e}")
        elif not target_url:
            # Blank new tab — IPC dispatch with --new-tab
            if _browser_exe and not _browser_exe.lower().endswith(".lnk"):
                try:
                    subprocess.Popen(
                        [_browser_exe, "--new-tab"],
                        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                        close_fds=True,
                    )
                    return f"Opened a new browser tab in your active {_active_browser.title()} window, Sir."
                except Exception as e:
                    logger.debug(f"Chromium IPC new-tab dispatch notice: {e}")

    apps = _build_app_index()
    browser_exe = None
    for key, path in apps.items():
        if browser_clean in key or f"{browser_clean}.exe" in key:
            browser_exe = path
            break

    if not browser_exe:
        browser_exe = shutil.which(f"{browser_clean}.exe") or shutil.which(browser_clean)

    if not browser_exe:
        program_files = [
            os.environ.get("ProgramFiles", "C:\\Program Files"),
            os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", "")
        ]
        candidates = {
            "brave": [
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
                r"Programs\BraveSoftware\Brave-Browser\Application\brave.exe",
            ],
            "chrome": [
                r"Google\Chrome\Application\chrome.exe",
            ],
            "msedge": [
                r"Microsoft\Edge\Application\msedge.exe",
            ],
        }
        for base in program_files:
            if not base:
                continue
            for rel in candidates.get(browser_clean, []):
                cand = os.path.join(base, rel)
                if os.path.exists(cand):
                    browser_exe = cand
                    break
            if browser_exe:
                break

    if browser_exe:
        if browser_exe.lower().endswith(".lnk"):
            try:
                os.startfile(browser_exe)
                return f"Opened {browser_clean.title()} with a new tab, Sir."
            except Exception as e:
                logger.warning(f"Failed to start shortcut {browser_exe}: {e}")
        else:
            try:
                cmd = [browser_exe]
                if target_url:
                    cmd.append(target_url)
                else:
                    cmd.append("--new-tab")
                subprocess.Popen(
                    cmd,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
                dest = f"to '{target_url}'" if target_url else "with a new tab"
                return f"Opened {browser_clean.title()} {dest}, Sir."
            except Exception as e:
                logger.warning(f"Failed to spawn {browser_clean}: {e}")

    try:
        webbrowser.open_new_tab(target_url or "https://google.com")
        return f"Opened a new browser tab {('to ' + target_url) if target_url else ''}, Sir."
    except Exception as e:
        return f"Failed to open browser tab: {e}"


@SkillRegistry.register(
    name="close_browser_tab",
    description="Terminates or closes an active or matching tab in Brave, Chrome, Edge, or the desktop browser. Prefers silent CDP if attached, falling back to foreground Win32 shortcut."
)
def close_browser_tab(browser: str = "brave", tab_title: str = "", allow_unmanaged: bool = False) -> str:
    """Terminates a tab in the requested browser.
    
    1. CDP fast path: Closes the matching or active tab silently over Chrome DevTools Protocol.
    2. Win32 fallback: Focuses the browser window and presses Ctrl+W without stealing overall workflow context.
    """
    browser_clean = (browser or "brave").lower().strip()

    # 0. Sub-5ms Silent CDP Fast Path: If managed browser is active or unmanaged access is authoritatively approved
    if browser_clean in ("brave", "chrome", "edge", "browser"):
        from jarvis.skills.browser_cdp import (
            get_managed_cdp_port,
            is_unmanaged_cdp_authorized,
        )
        if get_managed_cdp_port() is not None or is_unmanaged_cdp_authorized(allow_unmanaged):
            try:
                from jarvis.skills.browser_cdp import close_cdp_tab
                cdp_res = close_cdp_tab(title_or_url=tab_title, allow_unmanaged=allow_unmanaged)
                if cdp_res and not cdp_res.startswith("Could not find") and not cdp_res.startswith("No open browser tabs"):
                    return cdp_res
            except Exception as e:
                logger.debug(f"CDP close tab bypass: {e}")

    # 1. Desktop Window Fallback via UIA / Win32 + Ctrl+W (last resort)
    try:
        # Iterate browser candidates so UIA can locate Chrome/Edge if Brave is absent.
        focused_via_uia = False
        try:
            from jarvis.skills.windows_uia import focus_window_uia
            _close_candidates = [browser_clean]
            if browser_clean in ("brave", "browser"):
                _close_candidates.extend(["chrome", "msedge", "edge", "firefox"])
            for _cand in _close_candidates:
                focused_title = focus_window_uia(_cand)
                if focused_title:
                    focused_via_uia = True
                    browser_clean = _cand
                    break
        except Exception as e:
            logger.debug(f"UIA focus bypass in close_browser_tab: {e}")

        if not focused_via_uia:
            import win32con
            import win32gui
            _ensure_desktop_access()
            matched_hwnds = []

            def _enum_win(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd):
                        w_title = win32gui.GetWindowText(hwnd).strip()
                        if w_title and browser_clean in w_title.lower():
                            matched_hwnds.append((hwnd, w_title))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_enum_win, None)

            if not matched_hwnds and browser_clean in ("brave", "browser"):
                # Probe other candidate browsers if default Brave window is not running
                for alt_browser in ("chrome", "msedge", "edge", "firefox"):
                    alt_hwnds = []
                    def _make_enum_alt(target_name: str, out_list: list):
                        def _enum_alt_close(h, _):
                            try:
                                if win32gui.IsWindowVisible(h):
                                    wt = win32gui.GetWindowText(h).strip()
                                    if wt and target_name in wt.lower():
                                        out_list.append((h, wt))
                            except Exception:
                                pass
                            return True
                        return _enum_alt_close
                    win32gui.EnumWindows(_make_enum_alt(alt_browser, alt_hwnds), None)
                    if alt_hwnds:
                        matched_hwnds = alt_hwnds
                        browser_clean = alt_browser
                        break

            # If still not found, check if foreground window is already a browser
            if not matched_hwnds:
                try:
                    active_hwnd = win32gui.GetForegroundWindow()
                    if active_hwnd:
                        active_title = (win32gui.GetWindowText(active_hwnd) or "").lower()
                        for known_b in ("brave", "chrome", "edge", "firefox", "opera"):
                            if known_b in active_title:
                                matched_hwnds = [(active_hwnd, win32gui.GetWindowText(active_hwnd))]
                                browser_clean = known_b
                                break
                except Exception:
                    pass

            if matched_hwnds:
                hwnd, found_title = matched_hwnds[0]
                if not safe_set_foreground_window(hwnd):
                    try:
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        try:
                            win32gui.BringWindowToTop(hwnd)
                        except Exception:
                            pass
            else:
                return f"No open {browser_clean.title()} window was found to close a tab, Sir."

        # Send native virtual key Ctrl+W via Win32 keybd_event without stealing mouse / clipboard
        try:
            import win32api
            import win32con
            win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            win32api.keybd_event(ord('W'), 0, 0, 0)
            win32api.keybd_event(ord('W'), 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
            return f"Closed active tab in your {browser_clean.title()} window, Sir."
        except Exception:
            _send_vk(0x57)  # Virtual key 'W' fallback
            return f"Closed active tab in your {browser_clean.title()} window, Sir."
    except Exception as e:
        return f"Failed to close browser tab: {e}"


@SkillRegistry.register(
    name="focus_window",
    description="Switches focus and brings an open desktop application or window to the foreground by name (e.g. 'brave', 'code', 'spotify', 'terminal')."
)
def focus_window(title: str) -> str:
    """Brings matching desktop window to the foreground."""
    target = (title or "").lower().strip()
    if not target:
        return "Window title or application name cannot be empty, Sir."

    # 0. UIA Semantic Focus Fast-Path: Bypasses Win32 lock via pywinauto
    try:
        from jarvis.skills.windows_uia import focus_window_uia
        uia_title = focus_window_uia(target)
        if uia_title:
            return f"Switched focus to '{uia_title}', Sir."
    except Exception as e:
        logger.debug(f"UIA focus bypass: {e}")

    try:
        import win32con
        import win32gui
        _ensure_desktop_access()

        matched_hwnds = []

        def _enum_win(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                w_title = win32gui.GetWindowText(hwnd).strip()
                if w_title and target in w_title.lower():
                    matched_hwnds.append((hwnd, w_title))

        win32gui.EnumWindows(_enum_win, None)

        if not matched_hwnds:
            # Self-Healing Fallback Cascade: If application is closed, launch and bring to focus
            launch_res = launch_application(target)
            if "launched" in launch_res.lower() or "opened" in launch_res.lower():
                time.sleep(0.06)
                try:
                    from jarvis.skills.windows_uia import focus_window_uia
                    verified_title = focus_window_uia(target)
                    if verified_title:
                        return f"Window '{title}' was not open; launched and focused '{verified_title}' for you, Sir."
                except Exception:
                    pass
                return f"Window '{title}' was not open; launched it for you, Sir."
            return f"No open window matching '{title}' was found, Sir."

        hwnd, found_title = matched_hwnds[0]
        if not safe_set_foreground_window(hwnd):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                try:
                    win32gui.BringWindowToTop(hwnd)
                except Exception:
                    pass

        # Closed-Loop Verification Gate
        time.sleep(0.04)
        active_hwnd = win32gui.GetForegroundWindow()
        active_title = win32gui.GetWindowText(active_hwnd).strip() if active_hwnd else ""
        if active_hwnd == hwnd or (active_title and target in active_title.lower()):
            return f"Verified: Switched focus to '{found_title}', Sir."
        return f"Switched focus to '{found_title}', Sir."
    except Exception as e:
        return f"Failed to focus window '{title}': {e}"


@SkillRegistry.register(
    name="press_hotkey",
    description="Executes a keyboard shortcut or hotkey combination on Sir's PC (e.g. 'ctrl+t', 'ctrl+w', 'alt+tab', 'ctrl+c', 'ctrl+v', 'win+d', 'enter', 'escape')."
)
def press_hotkey(keys: str) -> str:
    """Safely presses a key combination via pyautogui."""
    raw = (keys or "").lower().strip()
    if not raw:
        return "Hotkey cannot be empty, Sir."

    if any(bad in raw for bad in ("ctrl+alt+del", "ctrl+alt+delete")):
        return "Execution of system-critical interrupt shortcut is prohibited, Sir."

    parts = [p.strip() for p in re.split(r"[+\s]+", raw) if p.strip()]
    if not parts:
        return f"Invalid hotkey format: '{keys}', Sir."

    try:
        _ensure_desktop_access()
        pyautogui.FAILSAFE = False
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)
        return f"Executed hotkey '{'+'.join(parts)}', Sir."
    except Exception as e:
        logger.warning(f"pyautogui hotkey error: {e}. Attempting native Win32 virtual key fallback.")
        try:
            import win32api
            import win32con
            VK_MAP = {
                "ctrl": win32con.VK_CONTROL,
                "control": win32con.VK_CONTROL,
                "alt": win32con.VK_MENU,
                "shift": win32con.VK_SHIFT,
                "win": win32con.VK_LWIN,
                "windows": win32con.VK_LWIN,
                "enter": win32con.VK_RETURN,
                "return": win32con.VK_RETURN,
                "esc": win32con.VK_ESCAPE,
                "escape": win32con.VK_ESCAPE,
                "tab": win32con.VK_TAB,
                "space": win32con.VK_SPACE,
                "backspace": win32con.VK_BACK,
                "delete": win32con.VK_DELETE,
                "up": win32con.VK_UP,
                "down": win32con.VK_DOWN,
                "left": win32con.VK_LEFT,
                "right": win32con.VK_RIGHT,
            }
            vk_seq = []
            for p in parts:
                if p in VK_MAP:
                    vk_seq.append(VK_MAP[p])
                elif len(p) == 1 and p.isalnum():
                    vk_seq.append(ord(p.upper()))
            if vk_seq:
                for vk in vk_seq:
                    win32api.keybd_event(vk, 0, 0, 0)
                for vk in reversed(vk_seq):
                    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
                return f"Executed hotkey '{'+'.join(parts)}' via native Win32 fallback, Sir."
        except Exception:
            pass
        return f"Failed to press hotkey '{keys}': {e}"


@SkillRegistry.register(
    name="type_text",
    description="Types text directly into the currently focused window or text input field on Sir's desktop."
)
def type_text(text: str) -> str:
    """Types text cleanly into the active window."""
    if not text:
        return "No text provided to type, Sir."
    try:
        _ensure_desktop_access()
        pyautogui.FAILSAFE = False
        old_clip = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)

        def _restore():
            time.sleep(0.3)
            try:
                pyperclip.copy(old_clip)
            except Exception:
                pass

        threading.Thread(target=_restore, daemon=True).start()
        return f"Typed {len(text)} characters into active window, Sir."
    except Exception as e:
        logger.warning(f"Clipboard paste typing failed: {e}. Falling back to direct keystroke injection.")
        try:
            pyautogui.write(text, interval=0.005)
            return f"Typed {len(text)} characters into active window via direct keystroke injection, Sir."
        except Exception:
            return f"Failed to type text: {e}"

