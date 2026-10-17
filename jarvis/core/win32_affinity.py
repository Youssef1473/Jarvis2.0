"""Alder Lake Hybrid Core Partitioner & MMCSS Audio Hardening for Jarvis 2.0.

Provides deterministic thread affinity and priority scheduling for Intel 12th/13th/14th Gen
hybrid architectures (e.g., Core i5-12500H with 4 Golden Cove P-Cores and 8 Gracemont E-Cores):
- MASK_PCORE_AUDIO_ISOLATED (0x0003): Logical CPUs 0-1 (P-Core 0) exclusive for WASAPI playback + WakeWord.
- MASK_PCORE_COMPUTE (0x00FC): Logical CPUs 2-7 (P-Cores 1-3) for Kokoro ONNX, Whisper, and Brain.
- MASK_ECORE_BACKGROUND (0xFF00): Logical CPUs 8-15 (All 8 E-Cores) for SQLite, UIA, FastAPI, and I/O.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("Jarvis.Hardware.Affinity")

# Thread Priority Constants (SetThreadPriority)
THREAD_PRIORITY_NORMAL = 0
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15

# MMCSS AvSetMmThreadPriority Constants
AVRT_PRIORITY_NORMAL = 0
AVRT_PRIORITY_HIGH = 1
AVRT_PRIORITY_CRITICAL = 2

# Alder Lake i5-12500H Core Topology Constants (4 P-Cores = 0..7, 8 E-Cores = 8..15)
MASK_PCORE_AUDIO_ISOLATED = 0x0003  # Logical CPUs 0-1 (P-Core 0: Exclusive WASAPI + WakeWord)
MASK_PCORE_COMPUTE = 0x00FC         # Logical CPUs 2-7 (P-Cores 1-3: Kokoro ONNX, Whisper, Brain)
MASK_ECORE_BACKGROUND = 0xFF00     # Logical CPUs 8-15 (All 8 E-Cores: SQLite, UIA, FastAPI, Sync)

# Windows 11 EcoQoS Power Throttling Constants
ThreadPowerThrottling = 4
THREAD_POWER_THROTTLING_CURRENT_VERSION = 1
THREAD_POWER_THROTTLING_EXECUTION_SPEED = 0x1


if sys.platform == "win32":
    class THREAD_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]
else:
    class THREAD_POWER_THROTTLING_STATE:  # type: ignore[no-redef]
        pass


def pin_current_thread_to_mask(
    mask: int,
    priority: int = THREAD_PRIORITY_NORMAL,
    eco_qos: bool = False,
) -> bool:
    """Pins the calling native OS thread to specified CPU affinity mask, priority, and power profile."""
    if sys.platform != "win32":
        return False

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_thread = kernel32.GetCurrentThread()

        # Check total logical CPUs to prevent invalid bitmasks on smaller systems
        logical_cpus = os.cpu_count() or 1
        effective_mask = mask
        if (effective_mask >= (1 << logical_cpus)):
            effective_mask = (1 << logical_cpus) - 1

        kernel32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t
        res = kernel32.SetThreadAffinityMask(h_thread, ctypes.c_size_t(effective_mask))
        if res == 0:
            logger.debug("SetThreadAffinityMask(0x%04X) failed: %s", effective_mask, ctypes.get_last_error())

        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        kernel32.SetThreadPriority(h_thread, priority)

        if eco_qos and hasattr(kernel32, "SetThreadInformation"):
            state = THREAD_POWER_THROTTLING_STATE()
            state.Version = THREAD_POWER_THROTTLING_CURRENT_VERSION
            state.ControlMask = THREAD_POWER_THROTTLING_EXECUTION_SPEED
            state.StateMask = THREAD_POWER_THROTTLING_EXECUTION_SPEED
            kernel32.SetThreadInformation(
                h_thread,
                ThreadPowerThrottling,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        return True
    except Exception as e:
        logger.debug("pin_current_thread_to_mask error: %s", e)
        return False


def elevate_audio_thread_mmcss(task_name: str = "Pro Audio") -> Optional[int]:
    """Pins calling thread to P-Core 0 and registers with Windows Multimedia Class Scheduler (MMCSS)."""
    if sys.platform != "win32":
        return None

    pin_current_thread_to_mask(MASK_PCORE_AUDIO_ISOLATED, THREAD_PRIORITY_TIME_CRITICAL)

    try:
        avrt = ctypes.WinDLL("Avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        avrt.AvSetMmThreadPriority.restype = wintypes.BOOL

        task_index = wintypes.DWORD(0)
        h_mmcss = avrt.AvSetMmThreadCharacteristicsW(task_name, ctypes.byref(task_index))
        if h_mmcss:
            avrt.AvSetMmThreadPriority(h_mmcss, AVRT_PRIORITY_CRITICAL)
            logger.info("Elevated audio thread to MMCSS '%s' on P-Core 0 (mask 0x%04X)", task_name, MASK_PCORE_AUDIO_ISOLATED)
            return int(h_mmcss)
        return None
    except Exception as e:
        logger.debug("elevate_audio_thread_mmcss error: %s", e)
        return None


def configure_ecore_worker_thread(coinit_sta: bool = False, tag: str = "EcoreWorker") -> None:
    """Initializer for background ThreadPoolExecutor threads to pin them to E-Cores with EcoQoS."""
    pin_current_thread_to_mask(MASK_ECORE_BACKGROUND, THREAD_PRIORITY_NORMAL, eco_qos=True)
    if coinit_sta and sys.platform == "win32":
        try:
            # 0x2 = COINIT_APARTMENTTHREADED (STA) for pywinauto / Shell COM isolation
            res = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            logger.debug("[%s] COM Single-Threaded Apartment (STA) initialized: res=0x%X", tag, res & 0xFFFFFFFF)
        except Exception as e:
            logger.debug("[%s] CoInitializeEx error: %s", tag, e)
