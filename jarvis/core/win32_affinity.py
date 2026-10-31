"""Alder Lake Hybrid Core Partitioner, MMCSS Audio Hardening & Win32 Affinity for Jarvis 2.0.

Unified hardware scheduling and thread affinity engine for Intel 12th/13th/14th Gen hybrid
architectures (e.g., Core i5-12500H with 4 P-Cores / 8 P-Threads and 8 E-Cores):
- MASK_PCORE_AUDIO_ISOLATED (0x0003): Logical CPUs 0-1 (P-Core 0) exclusive for WASAPI playback + WakeWord.
- MASK_PCORE_COMPUTE (0x00FC): Logical CPUs 2-7 (P-Cores 1-3) for Kokoro ONNX, Whisper, and Brain.
- MASK_ECORE_BACKGROUND (0xFF00): Logical CPUs 8-15 (All 8 E-Cores) for SQLite, UIA, FastAPI, and I/O.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading

logger = logging.getLogger("Jarvis.Hardware.Affinity")

# Thread Priority Constants (SetThreadPriority)
THREAD_PRIORITY_IDLE = -15
THREAD_PRIORITY_LOWEST = -2
THREAD_PRIORITY_BELOW_NORMAL = -1
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
    import ctypes
    from ctypes import wintypes

    class THREAD_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]
else:
    class THREAD_POWER_THROTTLING_STATE:  # type: ignore[no-redef]
        pass


def get_pcore_affinity_mask() -> int:
    """Calculates the processor affinity bitmask for Intel/AMD Performance (P) cores."""
    logical_cpus = os.cpu_count() or 1
    if logical_cpus >= 16 or logical_cpus >= 12:
        return 0x00FF
    elif logical_cpus > 4:
        return (1 << min(logical_cpus, 8)) - 1
    else:
        return (1 << logical_cpus) - 1


def get_physical_pcore_mask() -> int:
    """Calculates affinity bitmask selecting non-SMT physical P-cores (e.g. 0x55)."""
    logical_cpus = os.cpu_count() or 1
    if logical_cpus >= 12 or logical_cpus >= 8:
        return 0x55
    elif logical_cpus >= 4:
        return 0x05
    else:
        return (1 << logical_cpus) - 1


def get_ecore_affinity_mask() -> int:
    """Calculates affinity mask for Intel/AMD Efficient (E) cores or upper core pool."""
    logical_cpus = os.cpu_count() or 1
    if logical_cpus >= 16:
        return 0xFF00
    elif logical_cpus >= 12:
        return 0x0F00
    elif logical_cpus > 4:
        half = logical_cpus // 2
        return ((1 << half) - 1) << half
    else:
        return (1 << logical_cpus) - 1


def disable_thread_power_throttling(thread_handle: int | None = None) -> bool:
    """Disables Windows 11 EcoQoS power throttling on the specified or current calling thread."""
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
        state.Version = THREAD_POWER_THROTTLING_CURRENT_VERSION
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


def pin_current_thread_to_mask(
    mask: int,
    priority: int = THREAD_PRIORITY_NORMAL,
    eco_qos: bool = False,
) -> bool:
    """Pins the calling native OS thread to specified CPU affinity mask, priority, and power profile."""
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_thread = kernel32.GetCurrentThread()

        logical_cpus = os.cpu_count() or 1
        effective_mask = mask
        if effective_mask >= (1 << logical_cpus):
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
        elif not eco_qos:
            disable_thread_power_throttling()
        return True
    except Exception as e:
        logger.debug("pin_current_thread_to_mask error: %s", e)
        return False


def pin_thread_to_pcores(
    thread_handle: int | None = None,
    mask: int | None = None,
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
            logger.debug("Thread affinity pinned to P-Cores: mask 0x%X (previous: 0x%X)", target_mask, prev_mask)
            return True
        return False
    except Exception as e:
        logger.debug("Failed to set thread affinity mask: %s", e)
        return False


def elevate_thread_priority(thread_handle: int | None = None, priority: int = THREAD_PRIORITY_HIGHEST) -> bool:
    """Elevates thread priority on Windows."""
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


_THREAD_LOCAL_MMCSS = threading.local()


def get_current_thread_mmcss_handle() -> int | None:
    """Returns the MMCSS task handle registered on the current thread, or None."""
    return getattr(_THREAD_LOCAL_MMCSS, "handle", None)


def revert_current_thread_mmcss() -> bool:
    """Reverts MMCSS registration on the calling thread if active."""
    h = getattr(_THREAD_LOCAL_MMCSS, "handle", None)
    if h:
        res = revert_mmcss(h)
        _THREAD_LOCAL_MMCSS.handle = None
        return res
    return False


@contextlib.contextmanager
def mmcss_scope(task_name: str = "Pro Audio"):
    """Context manager guaranteeing MMCSS elevation is cleanly reverted on exit."""
    h_task = elevate_to_pro_audio_mmcss(task_name)
    try:
        yield h_task
    finally:
        if h_task:
            revert_mmcss(h_task)


def elevate_to_pro_audio_mmcss(task_name: str = "Pro Audio") -> int | None:
    """Elevates calling thread to Windows Multimedia Class Scheduler Service (MMCSS)."""
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
            h_int = int(h_task)
            _THREAD_LOCAL_MMCSS.handle = h_int
            return h_int
        return None
    except Exception as e:
        logger.debug("Failed to set MMCSS thread characteristics: %s", e)
        return None


def elevate_audio_thread_mmcss(task_name: str = "Pro Audio") -> int | None:
    """Alias for elevate_to_pro_audio_mmcss with Alder Lake P-Core 0 pinning."""
    if sys.platform != "win32":
        return None
    pin_current_thread_to_mask(MASK_PCORE_AUDIO_ISOLATED, THREAD_PRIORITY_TIME_CRITICAL)
    return elevate_to_pro_audio_mmcss(task_name)


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
        success = bool(avrt.AvRevertMmThreadCharacteristics(ctypes.c_void_p(h_task)))
        if getattr(_THREAD_LOCAL_MMCSS, "handle", None) == h_task:
            _THREAD_LOCAL_MMCSS.handle = None
        return success
    except Exception:
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


def demote_to_ecore_background(thread_handle: int | None = None) -> bool:
    """Demotes the target or current thread to below-normal priority and pins to E-cores."""
    if sys.platform != "win32":
        return False

    success = True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        target_handle = ctypes.c_void_p(-2) if thread_handle is None else ctypes.c_void_p(thread_handle)

        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k32.SetThreadPriority.restype = ctypes.c_int
        if k32.SetThreadPriority(target_handle, THREAD_PRIORITY_BELOW_NORMAL) == 0:
            success = False

        ecore_mask = get_ecore_affinity_mask()
        k32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k32.SetThreadAffinityMask.restype = ctypes.c_size_t
        prev = k32.SetThreadAffinityMask(target_handle, ctypes.c_size_t(ecore_mask))
        if prev == 0:
            success = False
        else:
            logger.debug("Speculative thread pinned to E-cores (mask 0x%X, priority %d)", ecore_mask, THREAD_PRIORITY_BELOW_NORMAL)

        return success
    except Exception as e:
        logger.debug("demote_to_ecore_background failed: %s", e)
        return False


def init_speculative_worker_thread() -> None:
    """Initializer hook for speculative ThreadPoolExecutor workers."""
    demote_to_ecore_background()


def configure_ecore_worker_thread(coinit_sta: bool = False, tag: str = "EcoreWorker") -> None:
    """Initializer for background ThreadPoolExecutor threads to pin them to E-Cores with EcoQoS."""
    pin_current_thread_to_mask(MASK_ECORE_BACKGROUND, THREAD_PRIORITY_NORMAL, eco_qos=True)
    if coinit_sta and sys.platform == "win32":
        try:
            import ctypes
            res = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            logger.debug("[%s] COM Single-Threaded Apartment (STA) initialized: res=0x%X", tag, res & 0xFFFFFFFF)
        except Exception as e:
            logger.debug("[%s] CoInitializeEx error: %s", tag, e)
