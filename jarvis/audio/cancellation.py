"""Audio Subsystem Cancellation and Barge-In Controller for Jarvis 2.0.

Provides immediate barge-in interruption across Kokoro neural TTS, WASAPI playback,
and wake-word / VAD speech onset detectors.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from jarvis.core.cancellation import (
    GLOBAL_EPOCH_STATE,
    SharedEpochState,
    BargeInCancellationToken,
    CancellationToken,
    get_global_cancellation_token,
    signal_global_barge_in,
    signal_execution_abort,
    trigger_speech_barge_in,
    is_barge_in_active,
    reset_global_barge_in,
    register_audio_flush_callback,
    unregister_audio_flush_callback,
    advance_active_tool_epoch,
    _FLUSH_CALLBACKS,
)

logger = logging.getLogger("Jarvis.Audio.Cancellation")


__all__ = [
    "GLOBAL_EPOCH_STATE",
    "BargeInCancellationToken",
    "CancellationToken",
    "get_global_cancellation_token",
    "signal_global_barge_in",
    "signal_execution_abort",
    "is_barge_in_active",
    "reset_global_barge_in",
    "register_audio_flush_callback",
    "unregister_audio_flush_callback",
    "trigger_speech_barge_in",
]
