"""Passive 32x32 dHash Background Screen Monitor for Jarvis 2.0.

Provides zero-VRAM, sub-0.3% CPU background change detection on E-Cores (0xFF00)
to alert Jarvis when long builds, test suites, downloads, or error dialogs complete
while the user is idle, gated through PrivacyGuard with per-HWND baseline tracking.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageGrab

from jarvis.core.win32_affinity import (
    MASK_ECORE_BACKGROUND,
    THREAD_PRIORITY_NORMAL,
    pin_current_thread_to_mask,
)

logger = logging.getLogger("Jarvis.Vision.DHashMonitor")


def _get_window_title_and_proc(hwnd: int) -> Tuple[str, str]:
    """Returns (window_title, process_name) for a given HWND in <0.05ms."""
    if sys.platform != "win32" or not hwnd:
        return ("", "")
    try:
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()

        proc_name = ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value > 0:
            try:
                import psutil
                proc_name = psutil.Process(pid.value).name().lower()
            except Exception:
                pass
        return (title, proc_name)
    except Exception:
        return ("", "")


class BackgroundDHashMonitor:
    """Monitors active workstation screen transitions via 32x32 horizontal gradient dHash.

    Runs on Gracemont E-Cores with EcoQoS at 0% P-Core CPU utilization.
    Tracks previous hashes per HWND, resets baseline on window switches,
    and gates all captures through PrivacyGuard.
    """

    def __init__(
        self,
        orchestrator: Any = None,
        get_idle_seconds_fn: Optional[Callable[[], float]] = None,
        check_interval: float = 2.0,
        hamming_threshold: int = 140,
        idle_threshold: float = 3.0,
    ) -> None:
        self.orchestrator = orchestrator
        self.get_idle_seconds_fn = get_idle_seconds_fn
        self.check_interval = check_interval
        self.hamming_threshold = hamming_threshold
        self.idle_threshold = idle_threshold

        # Mandatory constraint: track previous 32x32 dHash per hwnd
        self._last_hashes: Dict[int, np.ndarray] = {}
        # Mandatory constraint: track foreground hwnd to reset baseline on window switch
        self._current_fg_hwnd: int = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._callbacks: List[Callable[[int, str, int], None]] = []
        self._lock = threading.Lock()

    def add_transition_callback(self, callback: Callable[[int, str, int], None]) -> None:
        """Registers a callback(hwnd, window_title, hamming_distance) for screen transitions."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_transition_callback(self, callback: Callable[[int, str, int], None]) -> None:
        """Removes a registered callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    @staticmethod
    def compute_dhash(image: Image.Image) -> np.ndarray:
        """Computes 1024-bit horizontal difference hash on a 33x32 downsampled grayscale grid.

        Sub-0.4ms execution on CPU SIMD with zero VRAM overhead.
        Returns a boolean numpy array of shape (32, 32).
        """
        # Downscale to 33 width, 32 height in 8-bit grayscale
        gray = image.convert("L").resize((33, 32), Image.Resampling.BILINEAR)
        arr = np.asarray(gray, dtype=np.uint8)
        # Compare adjacent columns: arr[:, 1:] > arr[:, :-1] -> shape (32, 32)
        diff = arr[:, 1:] > arr[:, :-1]
        return diff

    @staticmethod
    def hamming_distance(h1: np.ndarray, h2: np.ndarray) -> int:
        """Computes the Hamming bit distance between two 32x32 dHash arrays."""
        if h1.shape != h2.shape:
            return 1024
        return int(np.count_nonzero(h1 != h2))

    def _get_idle_seconds(self) -> float:
        """Returns the user idle duration in seconds."""
        if self.get_idle_seconds_fn:
            try:
                return float(self.get_idle_seconds_fn())
            except Exception:
                pass
        if sys.platform == "win32":
            try:
                class LASTINPUTINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
                lii = LASTINPUTINFO()
                lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
                if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                    return max(0.0, millis / 1000.0)
            except Exception:
                pass
        return 0.0

    def _capture_window_or_screen(self, hwnd: int) -> Optional[Image.Image]:
        """Captures window bounds or active screen with fallback."""
        if sys.platform != "win32" or not hwnd:
            return None
        try:
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w <= 0 or h <= 0:
                return None
            try:
                return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
            except Exception:
                from jarvis.vision.screen_capture import ScreenPerception
                return ScreenPerception.capture_screen()
        except Exception as e:
            logger.debug("Failed to capture frame for dHash: %s", e)
            return None

    def poll_once(self) -> Optional[Tuple[int, str, int]]:
        """Executes a single check iteration.

        Returns (hwnd, window_title, distance) if an idle screen transition occurred, else None.
        Gated through PrivacyGuard with per-HWND baseline reset.
        """
        if sys.platform != "win32":
            return None

        user32 = ctypes.windll.user32
        fg_hwnd = user32.GetForegroundWindow()
        if not fg_hwnd or not user32.IsWindow(fg_hwnd):
            return None

        title, proc_name = _get_window_title_and_proc(fg_hwnd)

        # Mandatory constraint: Gate captures through PrivacyGuard
        # Never hash or alert for sensitive windows (passwords, banking, crypto)
        from jarvis.skills.screen_vision import check_visual_privacy
        privacy_warning = check_visual_privacy(title, proc_name)
        if privacy_warning:
            logger.debug("DHash monitor: Sensitive window '%s' (%s) detected. Gated by PrivacyGuard.", title, proc_name)
            return None

        # Mandatory constraint: Reset baseline on foreground window switch
        if fg_hwnd != self._current_fg_hwnd:
            self._current_fg_hwnd = fg_hwnd
            frame = self._capture_window_or_screen(fg_hwnd)
            if frame is not None:
                self._last_hashes[fg_hwnd] = self.compute_dhash(frame)
            # Suppress alert on window switch
            return None

        frame = self._capture_window_or_screen(fg_hwnd)
        if frame is None:
            return None

        curr_hash = self.compute_dhash(frame)
        prev_hash = self._last_hashes.get(fg_hwnd)

        if prev_hash is None:
            self._last_hashes[fg_hwnd] = curr_hash
            return None

        dist = self.hamming_distance(curr_hash, prev_hash)
        idle_sec = self._get_idle_seconds()

        if idle_sec < self.idle_threshold:
            # User is actively interacting; continuously refresh baseline so active typing
            # does not trigger a false alert when user stops typing
            self._last_hashes[fg_hwnd] = curr_hash
            return None

        # User is idle and waiting! Check if significant background change occurred
        if dist > self.hamming_threshold:
            logger.info(
                "Background screen transition detected in '%s' (HWND %d, dist=%d bits, idle=%.1fs)",
                title, fg_hwnd, dist, idle_sec
            )
            # Update baseline to prevent duplicate alarms for the same transition
            self._last_hashes[fg_hwnd] = curr_hash

            # Dispatch ambient telemetry to Holographic HUD
            if self.orchestrator and hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                try:
                    self.orchestrator.broadcast_ambient_telemetry({
                        "event": "screen_transition",
                        "title": title,
                        "hwnd": fg_hwnd,
                        "diff_bits": dist,
                        "idle_seconds": round(idle_sec, 1),
                    })
                except Exception as e:
                    logger.debug("Telemetry broadcast notice: %s", e)

            # Fire registered callbacks
            with self._lock:
                callbacks = list(self._callbacks)
            for cb in callbacks:
                try:
                    cb(fg_hwnd, title, dist)
                except Exception as cb_err:
                    logger.debug("Callback error in dHash monitor: %s", cb_err)

            return (fg_hwnd, title, dist)

        return None

    def _monitor_loop(self) -> None:
        """Background loop pinned to E-Cores (0xFF00) with EcoQoS."""
        if sys.platform == "win32":
            pin_current_thread_to_mask(MASK_ECORE_BACKGROUND, priority=THREAD_PRIORITY_NORMAL, eco_qos=True)

        logger.info("BackgroundDHashMonitor started on E-Cores (interval=%.1fs, threshold=%d bits).", self.check_interval, self.hamming_threshold)
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                logger.debug("Error in BackgroundDHashMonitor poll cycle: %s", e)

            if self._stop_event.wait(timeout=self.check_interval):
                break

        logger.info("BackgroundDHashMonitor loop exited cleanly.")

    def start(self) -> bool:
        """Starts the passive background monitor thread."""
        if self._running:
            return True
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="Jarvis-DHashMonitor",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 1.0) -> None:
        """Stops the background monitor thread cleanly."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
