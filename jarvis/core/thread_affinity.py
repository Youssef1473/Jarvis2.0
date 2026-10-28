"""Backward-compatible re-export shim for thread affinity.
Canonical implementation resides in jarvis.core.win32_affinity.
"""
from jarvis.core.win32_affinity import (
    THREAD_PRIORITY_BELOW_NORMAL,
    THREAD_PRIORITY_IDLE,
    THREAD_PRIORITY_LOWEST,
    configure_ecore_worker_thread,
    demote_to_ecore_background,
    get_ecore_affinity_mask,
    init_speculative_worker_thread,
)

__all__ = [
    "THREAD_PRIORITY_BELOW_NORMAL",
    "THREAD_PRIORITY_IDLE",
    "THREAD_PRIORITY_LOWEST",
    "configure_ecore_worker_thread",
    "demote_to_ecore_background",
    "get_ecore_affinity_mask",
    "init_speculative_worker_thread",
]
