"""Audio Subsystem Cancellation and Barge-In Controller for Jarvis 2.0.

Provides immediate barge-in interruption across Kokoro neural TTS, WASAPI playback,
and wake-word / VAD speech onset detectors.
"""
from __future__ import annotations

import logging

from jarvis.core.cancellation import (
    GLOBAL_EPOCH_STATE,
    BargeInCancellationToken,
    CancellationToken,
    get_global_cancellation_token,
    is_barge_in_active,
    register_audio_flush_callback,
    reset_global_barge_in,
    signal_execution_abort,
    signal_global_barge_in,
    trigger_speech_barge_in,
    unregister_audio_flush_callback,
)

logger = logging.getLogger("Jarvis.Audio.Cancellation")


__all__ = [
    "GLOBAL_EPOCH_STATE",
    "BargeInCancellationToken",
    "CancellationToken",
    "get_global_cancellation_token",
    "is_barge_in_active",
    "register_audio_flush_callback",
    "reset_global_barge_in",
    "signal_execution_abort",
    "signal_global_barge_in",
    "trigger_speech_barge_in",
    "unregister_audio_flush_callback",
]
