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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

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
MASK_PCORE_AUDIO_ISOLATED = 0x0003  # Logical CPUs 0-1 (P-Core 0 exclusive: WASAPI playback)
MASK_PCORE_WAKEWORD = 0x0054        # Logical CPUs 2, 4, 6 (P-Cores 1-3 non-SMT: avoids collision on Logical CPU 0 with P-Core 0 / 0x0003)
MASK_PCORE_COMPUTE = 0x00FC         # Logical CPUs 2-7 (P-Cores 1-3: Kokoro ONNX, Whisper, Brain)
MASK_ECORE_BACKGROUND = 0xFF00     # Logical CPUs 8-15 (All 8 E-Cores: SQLite, UIA, FastAPI, Sync)

# Windows 11 EcoQoS Power Throttling Constants
ThreadPowerThrottling = 4
THREAD_POWER_THROTTLING_CURRENT_VERSION = 1
THREAD_POWER_THROTTLING_EXECUTION_SPEED = 0x1


@dataclass(frozen=True, slots=True)
class CpuTopologyProfile:
    """Hardware CPU topology discovery profile for thread affinity scheduling."""

    is_hybrid: bool
    logical_cpu_count: int
    playback_mask: int
    wakeword_mask: int
    compute_mask: int
    background_mask: int
    source: str


def compute_topology_from_cores(
    cores: Sequence[tuple[int, int]],
    source: str = "win32_topology",
) -> CpuTopologyProfile:
    """Computes pure affinity bitmasks from a sequence of (core_mask, efficiency_class) tuples."""
    if not cores:
        return _fallback_topology_from_count(os.cpu_count() or 1, source=source)

    logical_cpu_count = sum(m.bit_count() for m, _ in cores)
    if logical_cpu_count <= 0:
        logical_cpu_count = os.cpu_count() or 1

    distinct_eff = sorted({eff for _, eff in cores})
    is_hybrid = len(distinct_eff) > 1

    if is_hybrid:
        p_cores = [m for m, eff in cores if eff == max(distinct_eff)]
        e_cores = [m for m, eff in cores if eff < max(distinct_eff)]

        playback_mask = p_cores[0] if p_cores else 1
        rem_p = p_cores[1:]
        if rem_p:
            wakeword_mask = 0
            for m in rem_p:
                wakeword_mask |= (m & -m)
            compute_mask = 0
            for m in rem_p:
                compute_mask |= m
        else:
            wakeword_mask = playback_mask
            compute_mask = playback_mask

        background_mask = 0
        for m in e_cores:
            background_mask |= m
        if background_mask == 0:
            background_mask = compute_mask or playback_mask

        return CpuTopologyProfile(
            is_hybrid=True,
            logical_cpu_count=logical_cpu_count,
            playback_mask=playback_mask,
            wakeword_mask=wakeword_mask,
            compute_mask=compute_mask,
            background_mask=background_mask,
            source=source,
        )

    # Homogeneous CPU topology
    core_masks = [m for m, _ in cores]
    n_cores = len(core_masks)
    playback_mask = core_masks[0] if core_masks else 1

    if n_cores >= 4:
        lower_half = core_masks[: n_cores // 2]
        upper_half = core_masks[n_cores // 2 :]
        rem_lower = lower_half[1:]

        wakeword_mask = 0
        for m in rem_lower:
            wakeword_mask |= (m & -m)
        if wakeword_mask == 0:
            wakeword_mask = playback_mask

        compute_mask = 0
        for m in core_masks[1:]:
            compute_mask |= m
        if compute_mask == 0:
            compute_mask = playback_mask

        background_mask = 0
        for m in upper_half:
            background_mask |= m
        if background_mask == 0:
            background_mask = compute_mask
    elif n_cores >= 2:
        rem_cores = core_masks[1:]
        wakeword_mask = 0
        for m in rem_cores:
            wakeword_mask |= (m & -m)
        if wakeword_mask == 0:
            wakeword_mask = playback_mask

        compute_mask = 0
        for m in rem_cores:
            compute_mask |= m
        if compute_mask == 0:
            compute_mask = playback_mask

        background_mask = compute_mask
    else:
        wakeword_mask = playback_mask
        compute_mask = playback_mask
        background_mask = playback_mask

    return CpuTopologyProfile(
        is_hybrid=False,
        logical_cpu_count=logical_cpu_count,
        playback_mask=playback_mask,
        wakeword_mask=wakeword_mask,
        compute_mask=compute_mask,
        background_mask=background_mask,
        source=source,
    )


def _fallback_topology_from_count(logical_cpu_count: int, source: str = "fallback_count") -> CpuTopologyProfile:
    """Provides safe, deterministic fallback affinity partitions derived from logical core count."""
    count = max(1, logical_cpu_count)
    if count >= 16:
        return CpuTopologyProfile(
            is_hybrid=True,
            logical_cpu_count=count,
            playback_mask=0x0003,
            wakeword_mask=0x0054,
            compute_mask=0x00FC,
            background_mask=0xFF00,
            source=source,
        )
    if count >= 12:
        return CpuTopologyProfile(
            is_hybrid=True,
            logical_cpu_count=count,
            playback_mask=0x0003,
            wakeword_mask=0x0054,
            compute_mask=0x00FC,
            background_mask=0x0F00,
            source=source,
        )
    if count >= 8:
        half = count // 2
        bg = ((1 << half) - 1) << half
        return CpuTopologyProfile(
            is_hybrid=False,
            logical_cpu_count=count,
            playback_mask=0x0003,
            wakeword_mask=0x0054,
            compute_mask=0x00FC,
            background_mask=bg if bg != 0 else 0xFF00,
            source=source,
        )
    if count >= 4:
        return CpuTopologyProfile(
            is_hybrid=False,
            logical_cpu_count=count,
            playback_mask=0x0003,
            wakeword_mask=0x0004,
            compute_mask=0x000C,
            background_mask=0x000C,
            source=source,
        )
    if count >= 2:
        return CpuTopologyProfile(
            is_hybrid=False,
            logical_cpu_count=count,
            playback_mask=0x0001,
            wakeword_mask=0x0002,
            compute_mask=0x0002,
            background_mask=0x0002,
            source=source,
        )
    return CpuTopologyProfile(
        is_hybrid=False,
        logical_cpu_count=count,
        playback_mask=0x0001,
        wakeword_mask=0x0001,
        compute_mask=0x0001,
        background_mask=0x0001,
        source=source,
    )


def _discover_cpu_topology_impl() -> CpuTopologyProfile:
    """Discovers CPU topology using Win32 kernel32.GetLogicalProcessorInformationEx."""
    if sys.platform != "win32":
        return _fallback_topology_from_count(os.cpu_count() or 1, source="non_windows")

    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not hasattr(k32, "GetLogicalProcessorInformationEx"):
            return _fallback_topology_from_count(os.cpu_count() or 1, source="fallback_count")

        k32.GetLogicalProcessorInformationEx.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.GetLogicalProcessorInformationEx.restype = wintypes.BOOL

        relation_processor_core = 0
        buf_len = wintypes.DWORD(0)
        k32.GetLogicalProcessorInformationEx(relation_processor_core, None, ctypes.byref(buf_len))
        if buf_len.value == 0:
            return _fallback_topology_from_count(os.cpu_count() or 1, source="fallback_count")

        buf = ctypes.create_string_buffer(buf_len.value)
        ret = k32.GetLogicalProcessorInformationEx(relation_processor_core, buf, ctypes.byref(buf_len))
        if not ret:
            return _fallback_topology_from_count(os.cpu_count() or 1, source="fallback_count")

        offset = 0
        raw_cores: list[tuple[int, int]] = []
        while offset < buf_len.value:
            rel = int.from_bytes(buf[offset : offset + 4], "little")
            size = int.from_bytes(buf[offset + 4 : offset + 8], "little")
            if size <= 0:
                break
            if rel == relation_processor_core:
                eff = buf[offset + 9]
                mask = int.from_bytes(buf[offset + 32 : offset + 40], "little")
                raw_cores.append((mask, eff))
            offset += size

        if not raw_cores:
            return _fallback_topology_from_count(os.cpu_count() or 1, source="fallback_count")

        return compute_topology_from_cores(raw_cores, source="win32_topology")
    except (OSError, AttributeError, RuntimeError) as e:
        logger.debug("Failed dynamic Win32 CPU topology discovery, using fallback: %s", e)
        return _fallback_topology_from_count(os.cpu_count() or 1, source="fallback_count")


_TOPOLOGY_LOCK = threading.Lock()
_CACHED_TOPOLOGY: CpuTopologyProfile | None = None


def discover_cpu_topology(force_refresh: bool = False) -> CpuTopologyProfile:
    """Discovers CPU topology via Win32 GetLogicalProcessorInformationEx or fallback, cached safely."""
    global _CACHED_TOPOLOGY
    if not force_refresh and _CACHED_TOPOLOGY is not None:
        return _CACHED_TOPOLOGY
    with _TOPOLOGY_LOCK:
        if not force_refresh and _CACHED_TOPOLOGY is not None:
            return _CACHED_TOPOLOGY
        profile = _discover_cpu_topology_impl()
        _CACHED_TOPOLOGY = profile
        return profile


def get_cpu_topology_profile() -> CpuTopologyProfile:
    """Returns the cached or newly discovered system CPU topology profile."""
    return discover_cpu_topology()


def get_playback_affinity_mask() -> int:
    """Returns the exclusive Physical Core 0 SMT playback affinity mask."""
    curr = os.cpu_count() or 1
    prof = discover_cpu_topology()
    if curr != prof.logical_cpu_count:
        return _fallback_topology_from_count(curr, source="fallback_count").playback_mask
    return prof.playback_mask


def get_compute_affinity_mask() -> int:
    """Returns affinity mask for high-performance compute P-Cores excluding Core 0."""
    curr = os.cpu_count() or 1
    prof = discover_cpu_topology()
    if curr != prof.logical_cpu_count:
        return _fallback_topology_from_count(curr, source="fallback_count").compute_mask
    return prof.compute_mask


def get_background_affinity_mask() -> int:
    """Returns affinity mask for background / E-Cores."""
    curr = os.cpu_count() or 1
    prof = discover_cpu_topology()
    if curr != prof.logical_cpu_count:
        return _fallback_topology_from_count(curr, source="fallback_count").background_mask
    return prof.background_mask

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


def get_wakeword_affinity_mask() -> int:
    """Calculates affinity bitmask selecting physical P-cores excluding P-Core 0 (0x0003)."""
    curr = os.cpu_count() or 1
    prof = discover_cpu_topology()
    if curr != prof.logical_cpu_count:
        return _fallback_topology_from_count(curr, source="fallback_count").wakeword_mask
    return prof.wakeword_mask


def get_ecore_affinity_mask() -> int:
    """Calculates affinity mask for Intel/AMD Efficient (E) cores or upper core pool."""
    return get_background_affinity_mask()


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
    except (OSError, AttributeError, RuntimeError) as e:
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
    except (OSError, AttributeError, RuntimeError, ValueError) as e:
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
    except (OSError, AttributeError, RuntimeError) as e:
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
    except (OSError, AttributeError, RuntimeError) as e:
        logger.debug("Failed to set thread priority: %s", e)
        return False


_THREAD_LOCAL_MMCSS = threading.local()


def get_current_thread_mmcss_handle() -> int | None:
    """Returns the MMCSS task handle registered on the current thread, or None."""
    return getattr(_THREAD_LOCAL_MMCSS, "handle", None)


def revert_current_thread_mmcss() -> bool:
    """Reverts MMCSS registration on the calling thread if active."""
    h = getattr(_THREAD_LOCAL_MMCSS, "handle", None)
    if h and not isinstance(h, bool):
        _THREAD_LOCAL_MMCSS.handle = None
        return revert_mmcss(h)
    return False


@contextlib.contextmanager
def scoped_mmcss(task_name: str = "Pro Audio"):
    """Context manager guaranteeing MMCSS elevation is cleanly reverted on exit."""
    h_task = elevate_to_pro_audio_mmcss(task_name)
    try:
        yield h_task
    finally:
        if h_task and not isinstance(h_task, bool):
            revert_mmcss(h_task)


mmcss_scope = scoped_mmcss


def elevate_to_pro_audio_mmcss(task_name: str = "Pro Audio") -> int | None:
    """Elevates calling thread to Windows Multimedia Class Scheduler Service (MMCSS)."""
    if sys.platform != "win32":
        return None

    existing_h = getattr(_THREAD_LOCAL_MMCSS, "handle", None)
    if existing_h and not isinstance(existing_h, bool):
        return existing_h

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
    except (OSError, AttributeError, RuntimeError) as e:
        logger.debug("Failed to set MMCSS thread characteristics: %s", e)
        return None


def elevate_audio_thread_mmcss(task_name: str = "Pro Audio") -> int | None:
    """Alias for elevate_to_pro_audio_mmcss with dynamic Physical Core 0 pinning."""
    if sys.platform != "win32":
        return None
    pin_current_thread_to_mask(get_playback_affinity_mask(), THREAD_PRIORITY_TIME_CRITICAL)
    return elevate_to_pro_audio_mmcss(task_name)


def revert_mmcss(h_task: int | Any) -> bool:
    """Reverts MMCSS registration on thread termination."""
    if sys.platform != "win32" or not h_task or isinstance(h_task, bool):
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
    except (OSError, AttributeError, RuntimeError) as e:
        logger.debug("revert_mmcss notice: %s", e)
        return False


def pin_and_elevate_current_thread(
    tag: str = "AudioThread",
    time_critical: bool = False,
    physical_only: bool = True,
    mmcss: bool = True,
    mask: int | None = None,
) -> bool:
    """Convenience helper to pin calling thread to physical P-cores, elevate priority, and register with MMCSS."""
    if sys.platform != "win32":
        return False

    priority = THREAD_PRIORITY_TIME_CRITICAL if time_critical else THREAD_PRIORITY_HIGHEST
    p_pinned = pin_thread_to_pcores(thread_handle=None, mask=mask, physical_only=physical_only)
    p_elevated = elevate_thread_priority(priority=priority)
    h_mmcss = elevate_to_pro_audio_mmcss() if mmcss else None
    eco_disabled = disable_thread_power_throttling()

    if p_pinned or p_elevated or h_mmcss or eco_disabled:
        if mask is not None:
            mask_used = mask
        elif physical_only:
            mask_used = get_physical_pcore_mask()
        else:
            mask_used = get_pcore_affinity_mask()
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
    except (OSError, AttributeError, RuntimeError) as e:
        logger.debug("demote_to_ecore_background failed: %s", e)
        return False


def init_speculative_worker_thread() -> None:
    """Initializer hook for speculative ThreadPoolExecutor workers."""
    demote_to_ecore_background()


def configure_ecore_worker_thread(coinit_sta: bool = False, tag: str = "EcoreWorker") -> None:
    """Initializer for background ThreadPoolExecutor threads to pin them to E-Cores with EcoQoS."""
    pin_current_thread_to_mask(get_background_affinity_mask(), THREAD_PRIORITY_NORMAL, eco_qos=True)
    if coinit_sta and sys.platform == "win32":
        try:
            import ctypes
            res = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            logger.debug("[%s] COM Single-Threaded Apartment (STA) initialized: res=0x%X", tag, res & 0xFFFFFFFF)
        except (OSError, AttributeError, RuntimeError) as e:
            logger.debug("[%s] CoInitializeEx error: %s", tag, e)
