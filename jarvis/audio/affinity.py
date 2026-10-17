"""Windows Win32 Thread Affinity and Real-Time Priority Scheduler for Jarvis 2.0.

Pins performance-critical voice threads (Kokoro-82M neural synthesis, sounddevice WASAPI
playback, faster-whisper STT, and wake-word VAD loops) to Intel Performance (P) cores,
preventing Windows 11 Thread Director from parking real-time audio threads onto Efficient (E) cores,
and registers threads with Windows Multimedia Class Scheduler Service (MMCSS) for Pro Audio guarantees.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("Jarvis.Audio.Affinity")

# Win32 Priority Constants
THREAD_PRIORITY_NORMAL = 0
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15

# MMCSS Priority Constants (AvSetMmThreadPriority)
AVRT_PRIORITY_NORMAL = 0
AVRT_PRIORITY_HIGH = 1
AVRT_PRIORITY_CRITICAL = 2

# Windows 11 EcoQoS Power Throttling Constants (SetThreadInformation)
ThreadPowerThrottling = 4
THREAD_POWER_THROTTLING_EXECUTION_SPEED = 0x1

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class THREAD_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.DWORD),
            ("ControlMask", wintypes.DWORD),
            ("StateMask", wintypes.DWORD),
        ]
else:
    class THREAD_POWER_THROTTLING_STATE:
        pass


def disable_thread_power_throttling(thread_handle: Optional[int] = None) -> bool:
    """Disables Windows 11 EcoQoS power throttling on the specified or current calling thread.

    Prevents Windows 11 22H2/24H2 Thread Director from dynamically throttling background
    synthesis and playback threads down to efficiency execution speeds when the console
    window is minimized or backgrounded.
    """
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        if not hasattr(k32, "SetThreadInformation"):
            return False

        k32.SetThreadInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetThreadInformation.restype = wintypes.BOOL

        target_handle = k32.GetCurrentThread() if thread_handle is None else ctypes.c_void_p(thread_handle)
        state = THREAD_POWER_THROTTLING_STATE()
        state.Version = 1
        state.ControlMask = THREAD_POWER_THROTTLING_EXECUTION_SPEED
        state.StateMask = 0

        # Try ThreadPowerThrottling class (3 in Win32 SDK header, 4 in specified enum)
        for info_class in (3, ThreadPowerThrottling):
            res = k32.SetThreadInformation(
                target_handle,
                info_class,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
            if res != 0:
                logger.debug("Disabled Windows 11 EcoQoS power throttling on thread (class %d)", info_class)
                return True

        return False
    except Exception as e:
        logger.debug("Failed to disable thread power throttling: %s", e)
        return False


def get_pcore_affinity_mask() -> int:
    """Calculates the processor affinity bitmask for Intel/AMD Performance (P) cores.

    On Intel 12th/13th/14th Gen hybrid laptop processors (e.g., i5-12500H with 12 physical cores,
    16 logical threads):
    - Cores 0-7: 4 P-Cores with Hyper-Threading (Mask: 0x00FF)
    - Cores 8-15: 8 E-Cores, single-threaded (Mask: 0xFF00)
    """
    logical_cpus = os.cpu_count() or 1
    if logical_cpus >= 16:
        # 16-thread hybrid CPU: Pin to logical threads 0-7 (P-cores)
        return 0x00FF
    elif logical_cpus >= 12:
        # 12-thread CPU: Pin to logical threads 0-7
        return 0x00FF
    elif logical_cpus > 4:
        # Standard multi-core CPU: use first min(logical_cpus, 8) cores
        return (1 << min(logical_cpus, 8)) - 1
    else:
        # Compact CPU (<=4 cores): allow all available cores
        return (1 << logical_cpus) - 1


def get_physical_pcore_mask() -> int:
    """Calculates affinity bitmask selecting non-SMT physical P-cores.

    On Intel hybrid architectures with Hyper-Threading (e.g. cores 0-7 = 4 P-cores * 2 threads):
    Bitmask 0x55 (01010101b) pins exclusively to physical cores 0, 2, 4, 6,
    preventing SMT hyperthread sibling cache thrashing and pipeline stalls.
    """
    logical_cpus = os.cpu_count() or 1
    if logical_cpus >= 12:
        return 0x55
    elif logical_cpus >= 8:
        return 0x55
    elif logical_cpus >= 4:
        return 0x05
    else:
        return (1 << logical_cpus) - 1


def elevate_to_pro_audio_mmcss(task_name: str = "Pro Audio") -> Optional[int]:
    """Elevates the current calling thread to Windows Multimedia Class Scheduler Service (MMCSS).

    Enters the real-time multimedia scheduling range (priorities 24-25) and guarantees
    dedicated CPU cycle reservation per 10ms quantum to prevent audio dropouts.

    Returns:
        Avrt task handle (int) if successful, None otherwise.
    """
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        avrt = ctypes.windll.avrt
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        avrt.AvSetMmThreadPriority.restype = wintypes.BOOL

        task_index = wintypes.DWORD(0)
        h_task = avrt.AvSetMmThreadCharacteristicsW(task_name, ctypes.byref(task_index))
        if h_task:
            avrt.AvSetMmThreadPriority(h_task, AVRT_PRIORITY_CRITICAL)
            logger.debug("Windows MMCSS '%s' task registered successfully (handle=%s)", task_name, h_task)
            return int(h_task)
        return None
    except Exception as e:
        logger.debug("Failed to set MMCSS thread characteristics: %s", e)
        return None


def revert_mmcss(h_task: int) -> bool:
    """Reverts MMCSS registration on thread termination."""
    if sys.platform != "win32" or not h_task:
        return False

    try:
        import ctypes
        from ctypes import wintypes
        avrt = ctypes.windll.avrt
        avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
        avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
        return bool(avrt.AvRevertMmThreadCharacteristics(ctypes.c_void_p(h_task)))
    except Exception:
        return False


def pin_thread_to_pcores(
    thread_handle: Optional[int] = None,
    mask: Optional[int] = None,
    physical_only: bool = False,
) -> bool:
    """Pins a thread to high-performance P-cores using SetThreadAffinityMask."""
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        target_handle = ctypes.c_void_p(-2) if thread_handle is None else ctypes.c_void_p(thread_handle)
        if mask is not None:
            target_mask = mask
        elif physical_only:
            target_mask = get_physical_pcore_mask()
        else:
            target_mask = get_pcore_affinity_mask()

        k32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k32.SetThreadAffinityMask.restype = ctypes.c_size_t

        prev_mask = k32.SetThreadAffinityMask(target_handle, ctypes.c_size_t(target_mask))
        if prev_mask != 0:
            logger.debug(
                "Thread affinity pinned to P-Cores: mask 0x%X (previous: 0x%X)",
                target_mask, prev_mask
            )
            return True
        return False
    except Exception as e:
        logger.debug("Failed to set thread affinity mask: %s", e)
        return False


def elevate_thread_priority(thread_handle: Optional[int] = None, priority: int = THREAD_PRIORITY_HIGHEST) -> bool:
    """Elevates thread priority on Windows (default THREAD_PRIORITY_HIGHEST = 2)."""
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        target_handle = ctypes.c_void_p(-2) if thread_handle is None else ctypes.c_void_p(thread_handle)

        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k32.SetThreadPriority.restype = ctypes.c_int

        res = k32.SetThreadPriority(target_handle, priority)
        if res != 0:
            logger.debug("Thread priority elevated to level %d", priority)
            return True
        return False
    except Exception as e:
        logger.debug("Failed to set thread priority: %s", e)
        return False


def pin_and_elevate_current_thread(
    tag: str = "AudioThread",
    time_critical: bool = False,
    physical_only: bool = True,
    mmcss: bool = True,
) -> bool:
    """Convenience helper to pin calling thread to physical P-cores, elevate priority, and register with MMCSS."""
    if sys.platform != "win32":
        return False

    priority = THREAD_PRIORITY_TIME_CRITICAL if time_critical else THREAD_PRIORITY_HIGHEST
    p_pinned = pin_thread_to_pcores(physical_only=physical_only)
    p_elevated = elevate_thread_priority(priority=priority)
    h_mmcss = elevate_to_pro_audio_mmcss() if mmcss else None
    eco_disabled = disable_thread_power_throttling()

    if p_pinned or p_elevated or h_mmcss or eco_disabled:
        mask_used = get_physical_pcore_mask() if physical_only else get_pcore_affinity_mask()
        logger.info(
            "[%s] High-Performance Audio active: P-Cores pinned (0x%X), priority=%d, MMCSS=%s, EcoQoSDisabled=%s",
            tag, mask_used, priority, bool(h_mmcss), eco_disabled
        )
        return True
    return False
