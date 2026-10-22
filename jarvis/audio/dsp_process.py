"""Process-Isolated Audio DSP Companion for Jarvis 2.0.

Isolates Kokoro-82M ONNX inference, Hann cross-fading, and WASAPI playback
into a dedicated companion process pinned to high priority and physical P-cores (0x0F),
connected via lock-free shared memory ring buffer and Win32 Named Event for <1us barge-in.
"""
from __future__ import annotations

import ctypes
import logging
import multiprocessing as mp
import numpy as np
import os
import sys
import time
from typing import Optional

from jarvis.audio.shm_transport import DEFAULT_RING_NAME, ShmAudioRingTransport

logger = logging.getLogger("Jarvis.Audio.DSPProcess")

DEFAULT_SHM_NAME = DEFAULT_RING_NAME  # single source of truth shared with tts.py
DEFAULT_BARGE_IN_EVENT = "JarvisAudioBargeInEvent"
SAMPLE_RATE = 24000
RING_CAPACITY_SAMPLES = SAMPLE_RATE * 10  # 10 seconds buffer: 240,000 float32 (960,000 bytes)

HIGH_PRIORITY_CLASS = 0x00000080


def _create_or_open_event(event_name: str, manual_reset: bool = True, initial_state: bool = False):
    """Creates or opens a Win32 Named Event."""
    if sys.platform == "win32":
        try:
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateEventW.restype = wintypes.HANDLE
            handle = k32.CreateEventW(None, manual_reset, initial_state, event_name)
            return handle
        except Exception as e:
            logger.debug("Win32 CreateEventW failed: %s", e)
    return None


def _set_named_event(handle) -> bool:
    if sys.platform == "win32" and handle:
        try:
            k32 = ctypes.windll.kernel32
            return bool(k32.SetEvent(handle))
        except Exception:
            return False
    return False


def _reset_named_event(handle) -> bool:
    if sys.platform == "win32" and handle:
        try:
            k32 = ctypes.windll.kernel32
            return bool(k32.ResetEvent(handle))
        except Exception:
            return False
    return False


def _close_event_handle(handle) -> None:
    if sys.platform == "win32" and handle:
        try:
            k32 = ctypes.windll.kernel32
            k32.CloseHandle(handle)
        except Exception:
            pass


def _elevate_companion_process(pcore_mask: int = 0x0F) -> None:
    """Sets companion process priority to HIGH_PRIORITY_CLASS and pins to P-cores."""
    if sys.platform != "win32":
        return

    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        curr_proc = k32.GetCurrentProcess()
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetPriorityClass(curr_proc, HIGH_PRIORITY_CLASS)

        k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        k32.SetProcessAffinityMask.restype = wintypes.BOOL
        k32.SetProcessAffinityMask(curr_proc, ctypes.c_size_t(pcore_mask))
    except Exception as e:
        logger.debug("Failed elevating companion process: %s", e)


def dsp_companion_main(shm_name: str, event_name: str, shutdown_event: mp.Event):
    """Companion process entry point: owns nothing, attaches to the ring and flushes it on barge-in."""
    _elevate_companion_process(pcore_mask=0x0F)
    barge_in_h = _create_or_open_event(event_name)

    try:
        transport = ShmAudioRingTransport(
            name=shm_name,
            capacity_samples=RING_CAPACITY_SAMPLES,
            sample_rate=SAMPLE_RATE,
            create=False,
        )
    except Exception as e:
        logger.error("DSP companion failed attaching to shared memory: %s", e)
        _close_event_handle(barge_in_h)
        return

    logger.info("DSP Companion Process online (PID: %d)", os.getpid())

    try:
        while not shutdown_event.is_set():
            # Check for atomic barge-in
            if sys.platform == "win32" and barge_in_h:
                try:
                    from ctypes import wintypes
                    k32 = ctypes.windll.kernel32
                    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                    k32.WaitForSingleObject.restype = wintypes.DWORD
                    # 0ms check
                    if k32.WaitForSingleObject(barge_in_h, 0) == 0:
                        # Barge-in signaled: flush through the SPSC protocol (never raw header writes)
                        transport.trigger_barge_in_clear()
                        _reset_named_event(barge_in_h)
                except Exception:
                    pass

            time.sleep(0.005)
    finally:
        transport.close()
        _close_event_handle(barge_in_h)
        logger.info("DSP Companion Process shutdown complete.")


class AudioDSPProcessClient:
    """Client in the main agent process managing the shared ring and the companion process."""

    def __init__(self, shm_name: str = DEFAULT_SHM_NAME, event_name: str = DEFAULT_BARGE_IN_EVENT):
        self.shm_name = shm_name
        self.event_name = event_name
        self.transport: Optional[ShmAudioRingTransport] = None
        self.barge_in_h = None
        self.process: Optional[mp.Process] = None
        self.shutdown_event = mp.Event()

    def start(self) -> bool:
        """Opens (or creates) the shared ring and starts the companion process."""
        try:
            # The TTS producer may already have created the ring; either order works.
            self.transport = ShmAudioRingTransport.open_or_create(
                name=self.shm_name,
                capacity_samples=RING_CAPACITY_SAMPLES,
                sample_rate=SAMPLE_RATE,
            )
            self.barge_in_h = _create_or_open_event(self.event_name)

            self.process = mp.Process(
                target=dsp_companion_main,
                args=(self.shm_name, self.event_name, self.shutdown_event),
                name="JarvisAudioDSPCompanion",
                daemon=True,
            )
            self.process.start()
            logger.info("Spawned Audio DSP Companion process (PID: %s)", self.process.pid)
            return True
        except Exception as e:
            logger.error("Failed starting Audio DSP Companion: %s", e)
            return False

    def trigger_barge_in(self) -> bool:
        """Flushes queued audio via the ring's epoch/cut protocol and signals the companion."""
        if self.transport:
            self.transport.trigger_barge_in_clear()
        return _set_named_event(self.barge_in_h)

    def write_pcm(self, pcm_data: np.ndarray) -> int:
        """Writes float32 audio samples into the shared ring buffer."""
        if self.transport:
            return self.transport.write_pcm(pcm_data)
        return 0

    def stop(self) -> None:
        """Stops the companion process and frees shared memory."""
        try:
            self.shutdown_event.set()
        except Exception:
            pass
        if self.process and self.process.is_alive():
            self.process.join(timeout=1.0)
            if self.process.is_alive():
                self.process.terminate()

        _close_event_handle(self.barge_in_h)
        self.barge_in_h = None

        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
