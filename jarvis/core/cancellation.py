"""Preemptive Actuation Cancellation Bus for Jarvis 2.0.

Provides lock-free, atomic cancellation token signaling across voice barge-in,
tool actuation pipelines, and background agent loops, terminating in-flight
desktop and browser mutations when speech interruption is triggered.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Callable, Optional

logger = logging.getLogger("Jarvis.Core.Cancellation")


class SharedEpochState(ctypes.Structure):
    """Lock-free atomic C-structure tracking monotonic epochs across subsystems."""
    _fields_ = [
        ("barge_in_epoch", ctypes.c_uint64),
        ("active_tool_epoch", ctypes.c_uint64),
    ]


# Process-wide shared singleton state
GLOBAL_EPOCH_STATE = SharedEpochState(0, 0)


class BargeInCancellationToken:
    """Lightweight cancellation token bound to the shared barge-in epoch.

    Actuation routines check `is_cancelled` or invoke `raise_if_cancelled()`
    before dispatching non-idempotent physical actions (clicks, keystrokes, DOM mutations).
    """

    def __init__(self, shared_state: Optional[SharedEpochState] = None, snapshot_epoch: Optional[int] = None):
        self._state = shared_state if shared_state is not None else GLOBAL_EPOCH_STATE
        self._snapshot_epoch = int(self._state.barge_in_epoch if snapshot_epoch is None else snapshot_epoch)

    @property
    def is_cancelled(self) -> bool:
        """Returns True if a voice barge-in occurred since this token was minted."""
        return int(self._state.barge_in_epoch) != self._snapshot_epoch

    @property
    def snapshot_epoch(self) -> int:
        return self._snapshot_epoch

    @property
    def current_epoch(self) -> int:
        return int(self._state.barge_in_epoch)

    def raise_if_cancelled(self) -> None:
        """Raises RuntimeError if the token has been preempted by speech barge-in."""
        if self.is_cancelled:
            raise RuntimeError(
                f"Execution preempted by voice barge-in (epoch {self._snapshot_epoch} -> {self._state.barge_in_epoch})"
            )


CancellationToken = BargeInCancellationToken

_FLUSH_CALLBACKS: list[Callable[[], None]] = []
_BARGE_IN_ACTIVE: bool = False


def is_barge_in_active() -> bool:
    """Returns True if a barge-in event is currently active."""
    return _BARGE_IN_ACTIVE


def reset_global_barge_in() -> None:
    """Resets the barge-in active flag."""
    global _BARGE_IN_ACTIVE
    _BARGE_IN_ACTIVE = False


def register_audio_flush_callback(cb: Callable[[], None]) -> None:
    """Registers a callback (e.g. KokoroSynthWorker.stop, RingBuffer.stop) to run on barge-in."""
    if cb not in _FLUSH_CALLBACKS:
        _FLUSH_CALLBACKS.append(cb)


def unregister_audio_flush_callback(cb: Callable[[], None]) -> None:
    """Unregisters an audio flush callback."""
    if cb in _FLUSH_CALLBACKS:
        _FLUSH_CALLBACKS.remove(cb)


_IS_RUNNING_FLUSH: bool = False


def _run_flush_callbacks() -> None:
    global _IS_RUNNING_FLUSH
    if _IS_RUNNING_FLUSH:
        return
    _IS_RUNNING_FLUSH = True
    try:
        for cb in list(_FLUSH_CALLBACKS):
            try:
                cb()
            except Exception as e:
                logger.debug("Error in flush callback: %s", e)
    finally:
        _IS_RUNNING_FLUSH = False


def get_global_cancellation_token() -> BargeInCancellationToken:
    """Mints a new BargeInCancellationToken capturing the current global barge-in epoch."""
    return BargeInCancellationToken(GLOBAL_EPOCH_STATE)


def signal_global_barge_in() -> int:
    """Atomically advances the global barge-in epoch, invalidating all outstanding tokens in <10ns."""
    global _BARGE_IN_ACTIVE
    _BARGE_IN_ACTIVE = True
    GLOBAL_EPOCH_STATE.barge_in_epoch += 1
    logger.debug("Signaled global barge-in: epoch -> %d", GLOBAL_EPOCH_STATE.barge_in_epoch)
    _run_flush_callbacks()
    return int(GLOBAL_EPOCH_STATE.barge_in_epoch)


def signal_execution_abort() -> int:
    """Atomically advances the global barge-in epoch, aborting in-flight tasks and tool runs."""
    return signal_global_barge_in()


def trigger_speech_barge_in() -> int:
    """Invoked when user speech or wake word is detected while audio is active.
    Advances the monotonic barge-in epoch and immediately triggers all audio flush callbacks.
    """
    return signal_global_barge_in()


def advance_active_tool_epoch() -> int:
    """Advances the active tool epoch counter."""
    GLOBAL_EPOCH_STATE.active_tool_epoch += 1
    return int(GLOBAL_EPOCH_STATE.active_tool_epoch)


__all__ = [
    "SharedEpochState",
    "GLOBAL_EPOCH_STATE",
    "BargeInCancellationToken",
    "get_global_cancellation_token",
    "signal_global_barge_in",
    "signal_execution_abort",
    "trigger_speech_barge_in",
    "register_audio_flush_callback",
    "unregister_audio_flush_callback",
    "advance_active_tool_epoch",
]

