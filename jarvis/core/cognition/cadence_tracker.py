"""Passive Zero-Storage Win32 Motor Cadence Tracker for Jarvis 2.0.

Measures real-time Keystrokes Per Minute (KPM) and Backspace-to-Character Ratio (hesitation/error rate)
via a low-level Win32 keyboard hook (WH_KEYBOARD_LL) pinned to E-Cores (0xFF00) at THREAD_PRIORITY_HIGHEST.

STRICT PRIVACY GUARANTEE:
Zero key characters, letters, symbols, or passwords are ever captured, buffered, logged, or serialized.
Only timestamps and boolean backspace indicators (vkCode == 0x08) are recorded.
"""
from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
import logging
import sys
import threading
import time
from typing import Deque, Optional, Set, Tuple

from jarvis.core.win32_affinity import (
    MASK_ECORE_BACKGROUND,
    THREAD_PRIORITY_HIGHEST,
    pin_current_thread_to_mask,
)

logger = logging.getLogger("Jarvis.Cognition.Cadence")

# Win32 Hook & Message Constants
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

# Virtual Key Constants
VK_BACK = 0x08

# LLKHF_INJECTED: 0x00000010 (indicates whether keystroke was injected by SendInput/synthetic driver)
LLKHF_INJECTED = 0x00000010

if sys.platform == "win32":
    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(
        wintypes.LPARAM,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
else:
    class KBDLLHOOKSTRUCT:  # type: ignore[no-redef]
        pass

    HOOKPROC = None  # type: ignore[assignment,misc]


class MotorCadenceTracker:
    """Passively tracks typing cadence (KPM) and backspace error ratio with zero keystroke recording.

    Mandatory Architectural Invariants:
    1. Zero-Lag LowLevelKeyboardProc: Never acquire a threading.Lock inside the callback.
       Relies on CPython's atomic deque.append() and calls CallNextHookEx immediately.
    2. Real-Time E-Core Scheduling: Pinned to MASK_ECORE_BACKGROUND (0xFF00) with
       THREAD_PRIORITY_HIGHEST and eco_qos=False so Windows never hits LowLevelHooksTimeout.
    3. Synthetic Key Rejection: Filters out simulated/injected keys (flags & LLKHF_INJECTED).
    4. Auto-Repeat Filtering: Tracks held vkCodes until WM_KEYUP so held keys don't inflate KPM,
       while counting each repeated backspace (VK_BACK) as an active deletion.
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        # Rolling deque of (timestamp, is_backspace)
        self._events: Deque[Tuple[float, bool]] = deque(maxlen=4000)
        # Set of currently depressed virtual key codes (for auto-repeat filtering)
        self._held_keys: Set[int] = set()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook = None
        # Persistent reference to C callback preventing Python GC collection
        self._hook_proc: Optional[Any] = None

    def start(self) -> bool:
        """Starts the low-level keyboard hook thread."""
        if sys.platform != "win32":
            logger.info("MotorCadenceTracker: Non-Windows platform; running in mock/simulation mode.")
            self._running = True
            return True

        if self._running:
            return True

        self._running = True
        self._thread = threading.Thread(
            target=self._hook_message_loop,
            name="JarvisMotorCadenceHook",
            daemon=True,
        )
        self._thread.start()
        # Brief pause to allow hook initialization
        time.sleep(0.05)
        return self._hook is not None

    def stop(self) -> None:
        """Unhooks the low-level keyboard hook and cleanly terminates the message loop."""
        self._running = False
        if sys.platform == "win32":
            if self._hook:
                try:
                    ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
                except Exception as e:
                    logger.debug("UnhookWindowsHookEx error: %s", e)
                self._hook = None

            if self._thread_id:
                try:
                    ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                except Exception as e:
                    logger.debug("PostThreadMessageW WM_QUIT error: %s", e)
                self._thread_id = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

        self._hook_proc = None
        logger.info("MotorCadenceTracker cleanly terminated.")

    def record_keystroke(self, is_backspace: bool = False, timestamp: Optional[float] = None) -> None:
        """Directly registers a keystroke event (used for testing or synthetic simulation)."""
        ts = timestamp if timestamp is not None else time.time()
        self._events.append((ts, is_backspace))

    def get_cadence(self, window_seconds: Optional[float] = None) -> Tuple[float, float]:
        """Calculates current (KPM, backspace_ratio) over the specified rolling time window.

        Returns:
            Tuple of:
                kpm (float): Keystrokes per minute normalized over the active window.
                backspace_ratio (float): Fraction of keystrokes that were backspaces (0.0 to 1.0).
        """
        win = window_seconds if window_seconds is not None else self.window_seconds
        now = time.time()
        cutoff = now - win

        # Atomic copy of deque snapshot
        events_snapshot = list(self._events)
        recent = [ev for ev in events_snapshot if ev[0] >= cutoff]

        if not recent:
            return 0.0, 0.0

        total_keys = len(recent)
        backspace_count = sum(1 for _, is_bs in recent if is_bs)

        # Compute KPM based on actual elapsed time in the window
        oldest_ts = recent[0][0]
        elapsed_minutes = max(1.0 / 60.0, (now - oldest_ts) / 60.0)
        # Cap window span to requested window
        elapsed_minutes = min(win / 60.0, elapsed_minutes)
        kpm = total_keys / elapsed_minutes

        backspace_ratio = (backspace_count / total_keys) if total_keys > 0 else 0.0
        return round(kpm, 2), round(backspace_ratio, 4)

    def _low_level_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        """LowLevelKeyboardProc callback executed on Windows input thread.

        CRITICAL PERFORMANCE INVARIANTS:
        - NEVER acquire locks here.
        - NEVER log or inspect key characters.
        - Complete in <10 microseconds and immediately return CallNextHookEx.
        """
        user32 = ctypes.windll.user32
        if n_code >= 0 and l_param:
            try:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

                # 1. Ignore synthetic / injected keystrokes
                if not (kb.flags & LLKHF_INJECTED):
                    vk = kb.vkCode

                    # 2. Key Release: Remove from held keys set
                    if w_param in (WM_KEYUP, WM_SYSKEYUP):
                        self._held_keys.discard(vk)

                    # 3. Key Press
                    elif w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        now = time.time()
                        if vk == VK_BACK:
                            # Backspace auto-repeats ARE intentional erasures -> count each
                            self._events.append((now, True))
                        else:
                            # Filter auto-repeat on regular keys
                            if vk not in self._held_keys:
                                self._held_keys.add(vk)
                                self._events.append((now, False))
            except Exception:
                pass

        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _hook_message_loop(self) -> None:
        """Dedicated message pump thread running on E-Cores with THREAD_PRIORITY_HIGHEST."""
        pin_current_thread_to_mask(
            mask=MASK_ECORE_BACKGROUND,
            priority=THREAD_PRIORITY_HIGHEST,
            eco_qos=False,
        )

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook_proc = HOOKPROC(self._low_level_proc)

        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._hook_proc,
            kernel32.GetModuleHandleW(None),
            0,
        )

        if not self._hook:
            err = kernel32.GetLastError()
            logger.warning("Failed to install WH_KEYBOARD_LL hook (Error: %s).", err)
            self._running = False
            return

        logger.info("MotorCadenceTracker WH_KEYBOARD_LL hook online on E-Cores.")

        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
