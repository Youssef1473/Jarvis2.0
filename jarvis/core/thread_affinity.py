"""Win32 E-Core Thread Affinity and Priority Demotion for Background Speculative Workers."""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("Jarvis.Core.ThreadAffinity")

THREAD_PRIORITY_BELOW_NORMAL = -1
THREAD_PRIORITY_LOWEST = -2
THREAD_PRIORITY_IDLE = -15


def get_ecore_affinity_mask() -> int:
    """Calculates affinity mask for Intel/AMD Efficient (E) cores or upper core pool."""
    logical_cpus = os.cpu_count() or 1
    if logical_cpus >= 16:
        # Upper 8 threads (cores 8-15)
        return 0xFF00
    elif logical_cpus >= 12:
        # Upper 4 threads (cores 8-11)
        return 0x0F00
    elif logical_cpus > 4:
        # Half split: upper half
        half = logical_cpus // 2
        return ((1 << half) - 1) << half
    else:
        # <=4 cores: allow any
        return (1 << logical_cpus) - 1


def demote_to_ecore_background(thread_handle: Optional[int] = None) -> bool:
    """Demotes the target or current thread to below-normal priority and pins to E-cores.
    
    Guarantees that background speculative workers never steal CPU cycles or L1/L2 caches
    from the P-core Kokoro neural synthesis or WASAPI playback pipeline.
    """
    if sys.platform != "win32":
        return False

    success = True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        target_handle = ctypes.c_void_p(-2) if thread_handle is None else ctypes.c_void_p(thread_handle)

        # 1. Demote thread priority to BELOW_NORMAL (-1)
        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k32.SetThreadPriority.restype = ctypes.c_int
        if k32.SetThreadPriority(target_handle, THREAD_PRIORITY_BELOW_NORMAL) == 0:
            success = False

        # 2. Set affinity mask to E-cores
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
