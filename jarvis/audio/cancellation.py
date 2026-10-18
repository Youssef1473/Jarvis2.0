"""Audio Subsystem Cancellation and Barge-In Controller for Jarvis 2.0.

Provides immediate barge-in interruption across Kokoro neural TTS, WASAPI playback,
and wake-word / VAD speech onset detectors.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from jarvis.core.cancellation import (
    GLOBAL_EPOCH_STATE,
    BargeInCancellationToken,
    get_global_cancellation_token,
    signal_global_barge_in,
    signal_execution_abort,
)

logger = logging.getLogger("Jarvis.Audio.Cancellation")

_FLUSH_CALLBACKS: list[Callable[[], None]] = []


def register_audio_flush_callback(cb: Callable[[], None]) -> None:
    """Registers a callback (e.g. KokoroSynthWorker.stop, RingBuffer.stop) to run on barge-in."""
    if cb not in _FLUSH_CALLBACKS:
        _FLUSH_CALLBACKS.append(cb)


def unregister_audio_flush_callback(cb: Callable[[], None]) -> None:
    """Unregisters an audio flush callback."""
    if cb in _FLUSH_CALLBACKS:
        _FLUSH_CALLBACKS.remove(cb)


def trigger_speech_barge_in() -> int:
    """Invoked when user speech or wake word is detected while audio is active.
    Advances the monotonic barge-in epoch and immediately triggers all audio flush callbacks.
    """
    epoch = signal_global_barge_in()
    for cb in list(_FLUSH_CALLBACKS):
        try:
            cb()
        except Exception as e:
            logger.debug("Error in audio flush callback: %s", e)
    return epoch


__all__ = [
    "GLOBAL_EPOCH_STATE",
    "BargeInCancellationToken",
    "get_global_cancellation_token",
    "signal_global_barge_in",
    "signal_execution_abort",
    "register_audio_flush_callback",
    "unregister_audio_flush_callback",
    "trigger_speech_barge_in",
]
