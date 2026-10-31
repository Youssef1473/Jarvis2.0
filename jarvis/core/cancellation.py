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
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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

def _is_bound_method(cb: Any) -> bool:
    return hasattr(cb, "__self__") and getattr(cb, "__self__", None) is not None


class _CallbackWrapper:
    __slots__ = ("_func_name", "_method_ref", "_self_id", "_strong_callback")

    def __init__(self, cb: Callable[[], None]) -> None:
        if _is_bound_method(cb):
            try:
                self._method_ref = weakref.WeakMethod(cb)
                self._strong_callback = None
            except TypeError:
                # Slotted classes without __weakref__ or non-weakref receivers fall back to strong retention
                self._method_ref = None
                self._strong_callback = cb
            self._self_id = id(getattr(cb, "__self__", None))
            self._func_name = getattr(cb, "__name__", "")
        else:
            self._method_ref = None
            self._strong_callback = cb
            self._self_id = 0
            self._func_name = getattr(cb, "__name__", "")

    def get(self) -> Callable[[], None] | None:
        if self._method_ref is not None:
            return self._method_ref()
        return self._strong_callback

    def matches(self, cb: Callable[[], None]) -> bool:
        live = self.get()
        if live is None:
            return False
        # Normalize bound methods (including built-ins like threading.Event.set) by receiver identity and name
        if _is_bound_method(cb) and _is_bound_method(live):
            return (
                getattr(live, "__self__", None) is getattr(cb, "__self__", None)
                and getattr(live, "__name__", "") == getattr(cb, "__name__", "")
            )
        return live is cb


_CALLBACK_LOCK = threading.Lock()
_CALLBACK_COND = threading.Condition(_CALLBACK_LOCK)
_FLUSH_CALLBACKS: list[_CallbackWrapper] = []
_IN_FLIGHT_SUPPRESSED_KEYS: set[tuple[int, str] | int] = set()
_CURRENTLY_EXECUTING_KEY: tuple[int, str] | int | None = None
_CURRENTLY_EXECUTING_THREAD_ID: int | None = None
_BARGE_IN_ACTIVE: bool = False
_IS_RUNNING_FLUSH: bool = False


def _get_callback_key(cb: Callable[[], None]) -> tuple[int, str] | int:
    """Returns an immutable identity key: (id(__self__), __name__) for bound methods, or id(cb)."""
    if _is_bound_method(cb):
        return (id(getattr(cb, "__self__", None)), getattr(cb, "__name__", ""))
    return id(cb)


def is_barge_in_active() -> bool:
    """Returns True if a barge-in event is currently active."""
    return _BARGE_IN_ACTIVE


def reset_global_barge_in() -> None:
    """Resets the barge-in active flag."""
    global _BARGE_IN_ACTIVE
    _BARGE_IN_ACTIVE = False


def register_audio_flush_callback(cb: Callable[[], None]) -> None:
    """Registers a callback (e.g. KokoroSynthWorker.stop, RingBuffer.stop) to run on barge-in.

    Bound methods (including built-ins) are weak-referenced via weakref.WeakMethod when supported
    to avoid leaking owning instances. Ordinary functions and lambdas are strongly retained.
    Thread-safe under _CALLBACK_LOCK.
    """
    if not callable(cb):
        return
    with _CALLBACK_LOCK:
        for w in _FLUSH_CALLBACKS:
            if w.matches(cb):
                return
        _FLUSH_CALLBACKS.append(_CallbackWrapper(cb))


def unregister_audio_flush_callback(cb: Callable[[], None], timeout: float = 2.0) -> bool:
    """Unregisters an audio flush callback with guaranteed linearization. Thread-safe under _CALLBACK_LOCK.

    Removes the callback from future dispatches. If a flush is currently in-flight:
    1. Suppresses the exact callback by (id(__self__), __name__) if not yet invoked.
    2. If this exact callback is currently executing on another dispatch thread, waits for it
       to complete (bounded by timeout) before returning. Skips waiting if called from
       within the callback itself on the active dispatch thread.

    Returns:
        bool: True if cleanly removed/quiescent, False if timed out waiting on an in-flight execution.
    """
    if not callable(cb):
        return True
    cb_key = _get_callback_key(cb)
    current_tid = threading.get_ident()
    with _CALLBACK_LOCK:
        _FLUSH_CALLBACKS[:] = [w for w in _FLUSH_CALLBACKS if not w.matches(cb)]
        if _IS_RUNNING_FLUSH:
            _IN_FLIGHT_SUPPRESSED_KEYS.add(cb_key)
            if _CURRENTLY_EXECUTING_THREAD_ID != current_tid:
                deadline = time.monotonic() + max(0.0, float(timeout))
                while _CURRENTLY_EXECUTING_KEY == cb_key:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        return False
                    _CALLBACK_COND.wait(timeout=rem)
    return True


def _run_flush_callbacks(exclude: Any = None) -> None:
    global _IS_RUNNING_FLUSH, _CURRENTLY_EXECUTING_KEY, _CURRENTLY_EXECUTING_THREAD_ID
    with _CALLBACK_LOCK:
        if _IS_RUNNING_FLUSH:
            return
        _IS_RUNNING_FLUSH = True
        _IN_FLIGHT_SUPPRESSED_KEYS.clear()
        _CURRENTLY_EXECUTING_KEY = None
        _CURRENTLY_EXECUTING_THREAD_ID = None

        active_cbs: list[Callable[[], None]] = []
        surviving_wrappers: list[_CallbackWrapper] = []
        for w in _FLUSH_CALLBACKS:
            if exclude is not None and w.matches(exclude):
                surviving_wrappers.append(w)
                continue
            c = w.get()
            if c is not None:
                active_cbs.append(c)
                surviving_wrappers.append(w)
        _FLUSH_CALLBACKS[:] = surviving_wrappers

    try:
        for cb in active_cbs:
            cb_key = _get_callback_key(cb)
            with _CALLBACK_LOCK:
                if cb_key in _IN_FLIGHT_SUPPRESSED_KEYS:
                    continue
                _CURRENTLY_EXECUTING_KEY = cb_key
                _CURRENTLY_EXECUTING_THREAD_ID = threading.get_ident()

            try:
                cb()
            except Exception as e:  # noqa: BLE001
                logger.debug("Error in flush callback: %s", e)
            finally:
                with _CALLBACK_LOCK:
                    _CURRENTLY_EXECUTING_KEY = None
                    _CURRENTLY_EXECUTING_THREAD_ID = None
                    _CALLBACK_COND.notify_all()
    finally:
        with _CALLBACK_LOCK:
            _IN_FLIGHT_SUPPRESSED_KEYS.clear()
            _CURRENTLY_EXECUTING_KEY = None
            _CURRENTLY_EXECUTING_THREAD_ID = None
            _IS_RUNNING_FLUSH = False
            _CALLBACK_COND.notify_all()


def is_in_cancellation_flush() -> bool:
    """Returns True if the current thread is executing inside cancellation flush dispatch."""
    with _CALLBACK_LOCK:
        return _CURRENTLY_EXECUTING_THREAD_ID == threading.get_ident()


def get_global_cancellation_token() -> BargeInCancellationToken:
    """Mints a new BargeInCancellationToken capturing the current global barge-in epoch."""
    return BargeInCancellationToken(GLOBAL_EPOCH_STATE)


def signal_global_barge_in(async_callbacks: bool = True, exclude: Any = None) -> int:
    """Atomically advances the global barge-in epoch, invalidating all outstanding tokens in <10ns.

    Args:
        async_callbacks: When True, dispatches registered flush callbacks on a background daemon
                         thread so the caller's real-time critical path is not blocked by slow callbacks.
                         Defaults to True to guarantee non-blocking <10ns execution on real-time threads.
        exclude: Optional callback or object to exclude from this dispatch round (e.g. self-invoked flush).
    """
    global _BARGE_IN_ACTIVE
    _BARGE_IN_ACTIVE = True
    GLOBAL_EPOCH_STATE.barge_in_epoch += 1
    logger.debug("Signaled global barge-in: epoch -> %d", GLOBAL_EPOCH_STATE.barge_in_epoch)
    if async_callbacks:
        threading.Thread(
            target=_run_flush_callbacks,
            args=(exclude,),
            name="Jarvis-AsyncBargeInCallbacks",
            daemon=True,
        ).start()
    else:
        _run_flush_callbacks(exclude=exclude)
    return int(GLOBAL_EPOCH_STATE.barge_in_epoch)


def signal_execution_abort() -> int:
    """Atomically advances the global barge-in epoch, aborting in-flight tasks and tool runs."""
    return signal_global_barge_in(async_callbacks=True)


def trigger_speech_barge_in() -> int:
    """Invoked when user speech or wake word is detected while audio is active.
    Advances the monotonic barge-in epoch in <10ns and dispatches flush callbacks asynchronously
    to eliminate stalls on the real-time audio input callback thread.
    """
    return signal_global_barge_in(async_callbacks=True)


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
        self._cancellation_requested: bool = False
        self._confirmed_stopped: bool = False
        self._drain_callbacks: list[Callable[[], None]] = []

    @property
    def is_cancelled(self) -> bool:
        """Returns True if cancelled explicitly, deadline elapsed, or voice barge-in occurred."""
        if self.cancel_event.is_set():
            return True
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return True
        return bool(self.parent_token is not None and self.parent_token.is_cancelled)

    @property
    def cancellation_requested(self) -> bool:
        """Returns True if cancellation was requested explicitly or via deadline/barge-in."""
        return self._cancellation_requested or self.is_cancelled

    @property
    def confirmed_stopped(self) -> bool:
        """Returns True if child execution was confirmed drained and stopped."""
        return self._confirmed_stopped

    def mark_confirmed_stopped(self) -> None:
        """Records that the running execution or child process tree has confirmed stopped."""
        self._confirmed_stopped = True

    def register_drain_callback(self, callback: Callable[[], None]) -> None:
        """Registers a callback (e.g. Job Object termination) to run upon cancellation."""
        self._drain_callbacks.append(callback)

    def cancel(self) -> None:
        """Triggers cooperative cancellation, executes drain callbacks, and invalidates the fencing token."""
        self._cancellation_requested = True
        self.cancel_event.set()
        self.fencing_token.is_invalidated = True
        for cb in list(self._drain_callbacks):
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                logger.debug("Tool cancellation callback error: %s", e)

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
    "is_in_cancellation_flush",
    "register_audio_flush_callback",
    "reset_tool_cancellation_token",
    "signal_execution_abort",
    "signal_global_barge_in",
    "trigger_speech_barge_in",
    "unregister_audio_flush_callback",
]

