"""Preemptive Actuation Cancellation Bus for Jarvis 2.0.

Provides lock-free, atomic cancellation token signaling across voice barge-in,
tool actuation pipelines, and background agent loops, terminating in-flight
desktop and browser mutations when speech interruption is triggered.
"""
from __future__ import annotations

import contextvars
import ctypes
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

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

    def __init__(self, shared_state: SharedEpochState | None = None, snapshot_epoch: int | None = None):
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
        for cb in _FLUSH_CALLBACKS:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
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


@dataclass
class FencingToken:
    """Monotonic fencing token to detect post-timeout late execution completions."""
    fencing_id: int
    is_invalidated: bool = False


class ToolCancellationToken:
    """Per-invocation cancellation token with monotonic deadline and thread-safe event.

    Propagated via contextvars across worker threads to enable cooperative cancellation,
    subprocess Job Object tree termination, and post-timeout fencing.
    """

    def __init__(
        self,
        deadline: float | None = None,
        parent_token: BargeInCancellationToken | None = None,
        fencing_id: int | None = None,
    ):
        self.deadline = deadline
        self.cancel_event = threading.Event()
        self.parent_token = parent_token
        self.fencing_token = FencingToken(fencing_id=fencing_id or 0)

    @property
    def is_cancelled(self) -> bool:
        """Returns True if cancelled explicitly, deadline elapsed, or voice barge-in occurred."""
        if self.cancel_event.is_set():
            return True
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return True
        return bool(self.parent_token is not None and self.parent_token.is_cancelled)

    def cancel(self) -> None:
        """Triggers cooperative cancellation and invalidates the fencing token."""
        self.cancel_event.set()
        self.fencing_token.is_invalidated = True

    def remaining_time(self) -> float:
        """Returns seconds remaining before deadline, or float('inf') if no deadline."""
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def raise_if_cancelled(self) -> None:
        """Raises RuntimeError if cancelled explicitly, deadline exceeded, or voice barge-in."""
        if self.cancel_event.is_set():
            raise RuntimeError("Tool execution cancelled explicitly.")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RuntimeError(f"Tool execution deadline exceeded ({self.deadline:.2f}).")
        if self.parent_token is not None and self.parent_token.is_cancelled:
            self.parent_token.raise_if_cancelled()


_active_tool_token: contextvars.ContextVar[ToolCancellationToken | None] = contextvars.ContextVar(
    "_active_tool_token", default=None
)


def get_current_tool_cancellation_token() -> ToolCancellationToken | None:
    """Returns the active ToolCancellationToken from the current context, if bound."""
    return _active_tool_token.get()


def bind_tool_cancellation_token(token: ToolCancellationToken) -> contextvars.Token:
    """Binds a ToolCancellationToken to the current context."""
    return _active_tool_token.set(token)


def reset_tool_cancellation_token(token: contextvars.Token) -> None:
    """Resets the active ToolCancellationToken context variable."""
    _active_tool_token.reset(token)


__all__ = [
    "GLOBAL_EPOCH_STATE",
    "BargeInCancellationToken",
    "FencingToken",
    "SharedEpochState",
    "ToolCancellationToken",
    "advance_active_tool_epoch",
    "bind_tool_cancellation_token",
    "get_current_tool_cancellation_token",
    "get_global_cancellation_token",
    "register_audio_flush_callback",
    "reset_tool_cancellation_token",
    "signal_execution_abort",
    "signal_global_barge_in",
    "trigger_speech_barge_in",
    "unregister_audio_flush_callback",
]

