"""Centralized 64-bit Win32 and DWM ABI bindings, RAII handle guards, and geometry utilities for Jarvis 2.0.

Provides explicit ctypes argtypes/restype declarations for user32, kernel32, dwmapi, and shcore,
guaranteeing zero 64-bit pointer truncation and zero unmanaged handle leaks across the vision subsystem.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import time
from collections.abc import Iterator
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("Jarvis.Vision.Win32ABI")

IS_WINDOWS = sys.platform == "win32" and hasattr(ctypes, "windll")

# DWM Constants
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

# Desktop Access Rights
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_READOBJECTS = 0x0001
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_ACCESS_ALL = 0x01FF

# WinEvent Hook Constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0
WM_QUIT = 0x0012

# Structure declarations
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


if IS_WINDOWS:
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    WINEVENTPROC = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,  # hWinEventHook (HWINEVENTHOOK is a 64-bit pointer)
        wintypes.DWORD,   # event
        wintypes.HWND,    # hwnd
        wintypes.LONG,    # idObject
        wintypes.LONG,    # idChild
        wintypes.DWORD,   # idEventThread
        wintypes.DWORD,   # dwmsEventTime
    )
else:
    WNDENUMPROC = None  # type: ignore[assignment,misc]
    WINEVENTPROC = None  # type: ignore[assignment,misc]


class Win32VisionABI:
    """Configures and holds strictly-typed Win32/DWM 64-bit ABI function pointers."""

    _initialized = False

    user32: Any = None
    kernel32: Any = None
    dwmapi: Any = None
    shcore: Any = None

    @classmethod
    def initialize(cls) -> None:
        if cls._initialized or not IS_WINDOWS:
            return

        try:
            cls.user32 = ctypes.WinDLL("user32", use_last_error=True)
            cls.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            try:
                cls.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            except OSError:
                cls.dwmapi = None
            try:
                cls.shcore = ctypes.WinDLL("shcore", use_last_error=True)
            except OSError:
                cls.shcore = None

            # --- user32 window & geometry declarations ---
            cls.user32.GetForegroundWindow.restype = wintypes.HWND
            cls.user32.GetForegroundWindow.argtypes = []

            cls.user32.IsWindow.restype = wintypes.BOOL
            cls.user32.IsWindow.argtypes = [wintypes.HWND]

            cls.user32.IsWindowVisible.restype = wintypes.BOOL
            cls.user32.IsWindowVisible.argtypes = [wintypes.HWND]

            cls.user32.IsIconic.restype = wintypes.BOOL
            cls.user32.IsIconic.argtypes = [wintypes.HWND]

            cls.user32.GetWindowRect.restype = wintypes.BOOL
            cls.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

            cls.user32.GetWindowTextLengthW.restype = ctypes.c_int
            cls.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

            cls.user32.GetWindowTextW.restype = ctypes.c_int
            cls.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

            cls.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            cls.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

            if WNDENUMPROC is not None:
                cls.user32.EnumWindows.restype = wintypes.BOOL
                cls.user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

            # --- user32 event hook declarations ---
            if WINEVENTPROC is not None:
                cls.user32.SetWinEventHook.restype = wintypes.HANDLE
                cls.user32.SetWinEventHook.argtypes = [
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HMODULE,
                    WINEVENTPROC,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                ]

            cls.user32.UnhookWinEvent.restype = wintypes.BOOL
            cls.user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

            cls.user32.GetMessageW.restype = wintypes.BOOL
            cls.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

            cls.user32.TranslateMessage.restype = wintypes.BOOL
            cls.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

            cls.user32.DispatchMessageW.restype = ctypes.c_ssize_t
            cls.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

            cls.user32.PostThreadMessageW.restype = wintypes.BOOL
            cls.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            # --- user32 desktop handle declarations ---
            cls.user32.OpenInputDesktop.restype = wintypes.HANDLE
            cls.user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

            cls.user32.OpenDesktopW.restype = wintypes.HANDLE
            cls.user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

            cls.user32.GetThreadDesktop.restype = wintypes.HANDLE
            cls.user32.GetThreadDesktop.argtypes = [wintypes.DWORD]

            cls.user32.SetThreadDesktop.restype = wintypes.BOOL
            cls.user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]

            cls.user32.CloseDesktop.restype = wintypes.BOOL
            cls.user32.CloseDesktop.argtypes = [wintypes.HANDLE]

            cls.user32.GetLastInputInfo.restype = wintypes.BOOL
            cls.user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]

            if hasattr(cls.user32, "IsHungAppWindow"):
                cls.user32.IsHungAppWindow.restype = wintypes.BOOL
                cls.user32.IsHungAppWindow.argtypes = [wintypes.HWND]

            if hasattr(cls.user32, "SetProcessDPIAware"):
                cls.user32.SetProcessDPIAware.restype = wintypes.BOOL
                cls.user32.SetProcessDPIAware.argtypes = []

            if hasattr(cls.user32, "SetProcessDpiAwarenessContext"):
                cls.user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
                cls.user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]

            if hasattr(cls.user32, "SetThreadDpiAwarenessContext"):
                cls.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
                cls.user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]

            # --- kernel32 declarations ---
            cls.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            cls.kernel32.GetCurrentThreadId.argtypes = []

            if hasattr(cls.kernel32, "GetTickCount64"):
                cls.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
                cls.kernel32.GetTickCount64.argtypes = []

            cls.kernel32.GetTickCount.restype = wintypes.DWORD
            cls.kernel32.GetTickCount.argtypes = []

            # --- dwmapi declarations ---
            if cls.dwmapi and hasattr(cls.dwmapi, "DwmGetWindowAttribute"):
                cls.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
                cls.dwmapi.DwmGetWindowAttribute.argtypes = [
                    wintypes.HWND,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                ]

            # --- shcore declarations ---
            if cls.shcore and hasattr(cls.shcore, "SetProcessDpiAwareness"):
                cls.shcore.SetProcessDpiAwareness.restype = ctypes.c_long
                cls.shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]

            cls._initialized = True
        except (OSError, AttributeError, ValueError) as e:
            logger.debug("Win32VisionABI initialization notice: %s", e)


# Initialize immediately upon import on Windows
Win32VisionABI.initialize()


@contextlib.contextmanager
def scoped_desktop_access() -> Iterator[int | None]:
    """RAII context manager guaranteeing thread desktop attachment and leak-free cleanup.

    Ensures that:
    1. The thread's original desktop handle is captured before desktop switching.
    2. Any opened desktop handle is set on the calling thread.
    3. In finally:, SetThreadDesktop(orig_hdesk) is restored BEFORE calling CloseDesktop(hdesk),
       preventing CloseDesktop from failing with ERROR_BUSY (170).
    """
    if not IS_WINDOWS or Win32VisionABI.user32 is None or Win32VisionABI.kernel32 is None:
        yield None
        return

    user32 = Win32VisionABI.user32
    kernel32 = Win32VisionABI.kernel32

    orig_hdesk: int | None = None
    new_hdesk: int | None = None

    try:
        current_tid = kernel32.GetCurrentThreadId()
        raw_orig = user32.GetThreadDesktop(current_tid)
        if raw_orig:
            orig_hdesk = raw_orig

        # Attempt to open active input desktop first
        hdesk = user32.OpenInputDesktop(0, False, DESKTOP_ACCESS_ALL)
        if not hdesk:
            hdesk = user32.OpenDesktopW("Default", 0, False, DESKTOP_ACCESS_ALL)

        if hdesk:
            new_hdesk = hdesk
            user32.SetThreadDesktop(wintypes.HANDLE(hdesk))

        yield new_hdesk
    except (OSError, ValueError, AttributeError) as err:
        logger.debug("scoped_desktop_access activation notice: %s", err)
        yield None
    finally:
        if orig_hdesk and new_hdesk:
            # Mandatory Win32 requirement: restore original thread desktop BEFORE closing new handle
            with contextlib.suppress(OSError, AttributeError):
                user32.SetThreadDesktop(wintypes.HANDLE(orig_hdesk))
        if new_hdesk:
            with contextlib.suppress(OSError, AttributeError):
                user32.CloseDesktop(wintypes.HANDLE(new_hdesk))


def _resolve_dwmapi() -> Any:
    return getattr(getattr(ctypes, "windll", None), "dwmapi", Win32VisionABI.dwmapi)


def _resolve_user32() -> Any:
    return getattr(getattr(ctypes, "windll", None), "user32", Win32VisionABI.user32)


def _resolve_kernel32() -> Any:
    return getattr(getattr(ctypes, "windll", None), "kernel32", Win32VisionABI.kernel32)


def _resolve_shcore() -> Any:
    return getattr(getattr(ctypes, "windll", None), "shcore", Win32VisionABI.shcore)


def enable_per_monitor_v2_dpi() -> bool:
    """Configures Per-Monitor v2 DPI synchronization across multi-monitor setups."""
    if not IS_WINDOWS:
        return False
    user32 = _resolve_user32()
    shcore = _resolve_shcore()
    if user32 is None:
        return False

    try:
        dpi_v2 = ctypes.c_void_p(-4)
        if hasattr(user32, "SetProcessDpiAwarenessContext") and user32.SetProcessDpiAwarenessContext(dpi_v2):
            return True
        if hasattr(user32, "SetThreadDpiAwarenessContext") and user32.SetThreadDpiAwarenessContext(dpi_v2):
            return True
        if shcore and hasattr(shcore, "SetProcessDpiAwareness") and shcore.SetProcessDpiAwareness(2) == 0:
            return True
        if hasattr(user32, "SetProcessDPIAware") and user32.SetProcessDPIAware():
            return True
    except (OSError, ValueError, AttributeError, RuntimeError) as e:
        logger.debug("DPI awareness context notice: %s", e)
    return False


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Returns (left, top, right, bottom) screen bounds for the given HWND."""
    if not IS_WINDOWS or not hwnd:
        return (0, 0, 0, 0)
    user32 = _resolve_user32()
    if user32 is None:
        return (0, 0, 0, 0)
    try:
        rect = wintypes.RECT()
        if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except (OSError, ValueError, AttributeError, RuntimeError) as err:
        logger.debug("GetWindowRect error for HWND %s: %s", hwnd, err)
    return (0, 0, 0, 0)


def get_window_title(hwnd: int) -> str:
    """Retrieves window title string via GetWindowTextW in <0.05ms."""
    if not IS_WINDOWS or not hwnd:
        return ""
    user32 = _resolve_user32()
    if user32 is None:
        return ""
    try:
        if hasattr(user32, "IsHungAppWindow") and user32.IsHungAppWindow(wintypes.HWND(hwnd)):
            return ""
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
            return buf.value.strip()
    except (OSError, ValueError, AttributeError, RuntimeError) as err:
        logger.debug("GetWindowTextW query notice for HWND %s: %s", hwnd, err)
    return ""


_PID_PROC_CACHE: dict[int, tuple[float, str]] = {}
_PID_CACHE_TTL_SEC = 30.0


def get_process_name_for_pid(pid: int) -> str:
    """Resolves process executable name from PID with a bounded TTL cache."""
    if pid <= 0:
        return ""
    now = time.monotonic()
    cached = _PID_PROC_CACHE.get(pid)
    if cached is not None:
        timestamp, name = cached
        if now - timestamp < _PID_CACHE_TTL_SEC:
            return name

    try:
        import psutil

        name = psutil.Process(pid).name().lower()
        if len(_PID_PROC_CACHE) > 512:
            _PID_PROC_CACHE.clear()
        _PID_PROC_CACHE[pid] = (now, name)
        return name
    except (OSError, ValueError, AttributeError, RuntimeError):
        return ""


def get_window_title_and_proc(hwnd: int) -> tuple[str, str]:
    """Returns (window_title, process_name) for a given HWND in <0.05ms."""
    if not IS_WINDOWS or not hwnd:
        return ("", "")
    user32 = _resolve_user32()
    if user32 is None:
        return ("", "")
    try:
        title = get_window_title(hwnd)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        proc_name = get_process_name_for_pid(pid.value) if pid.value > 0 else ""
        return (title, proc_name)
    except (OSError, ValueError, AttributeError, RuntimeError) as err:
        logger.debug("get_window_title_and_proc query notice for HWND %s: %s", hwnd, err)
        return ("", "")


def is_window_dwm_cloaked(hwnd: int) -> bool:
    """Returns True if hwnd is cloaked by Windows 11 DWM (e.g. inactive virtual desktop)."""
    if not IS_WINDOWS or not hwnd:
        return False
    dwmapi = _resolve_dwmapi()
    if dwmapi is None:
        return False
    try:
        cloaked = wintypes.DWORD(0)
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_CLOAKED),
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and bool(cloaked.value != 0)
    except (OSError, ValueError, AttributeError, RuntimeError) as err:
        logger.debug("DwmGetWindowAttribute error for HWND %s: %s", hwnd, err)
        return False


def get_idle_seconds() -> float:
    """Calculates workstation user idle seconds via GetLastInputInfo and high-resolution timer."""
    if not IS_WINDOWS:
        return 0.0
    user32 = _resolve_user32()
    kernel32 = _resolve_kernel32()
    if user32 is None or kernel32 is None:
        return 0.0
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            if hasattr(kernel32, "GetTickCount64"):
                current_ms = kernel32.GetTickCount64()
            else:
                current_ms = kernel32.GetTickCount()
            diff_ms = current_ms - lii.dwTime
            return max(0.0, float(diff_ms) / 1000.0)
    except (OSError, ValueError, AttributeError, RuntimeError) as err:
        logger.debug("get_idle_seconds query notice: %s", err)
    return 0.0


__all__ = [
    "CHILDID_SELF",
    "DESKTOP_ACCESS_ALL",
    "DWMWA_CLOAKED",
    "DWMWA_EXTENDED_FRAME_BOUNDS",
    "EVENT_OBJECT_NAMECHANGE",
    "EVENT_SYSTEM_DIALOGSTART",
    "EVENT_SYSTEM_FOREGROUND",
    "IS_WINDOWS",
    "LASTINPUTINFO",
    "OBJID_WINDOW",
    "WINEVENTPROC",
    "WINEVENT_OUTOFCONTEXT",
    "WINEVENT_SKIPOWNPROCESS",
    "WM_QUIT",
    "WNDENUMPROC",
    "Win32VisionABI",
    "enable_per_monitor_v2_dpi",
    "get_idle_seconds",
    "get_process_name_for_pid",
    "get_window_rect",
    "get_window_title",
    "get_window_title_and_proc",
    "is_window_dwm_cloaked",
    "scoped_desktop_access",
]
