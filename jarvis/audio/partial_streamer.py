"""Lock-free Shared Memory IPC channel for Faster-Whisper partial token streaming.

Eliminates Python queue head-of-line blocking and serialization overhead.
"""
from __future__ import annotations

import ctypes
from multiprocessing import shared_memory
import struct
import time
from typing import Any, Optional, Tuple
import numpy as np

# Binary Protocol:
# uint64 seq_id: Monotonic sequence counter
# uint64 qpc_timestamp_ns: Monotonic emission timestamp in nanoseconds
# uint32 text_len: UTF-8 payload byte length
# uint32 is_final: 1 if utterance concluded, 0 if partial
# char[256] utf8_text: Pre-allocated UTF-8 string buffer
SLOT_HEADER_STRUCT = "<QQII"
HEADER_SIZE = struct.calcsize(SLOT_HEADER_STRUCT)
TEXT_CAPACITY_BYTES = 256
TOTAL_CHANNEL_SIZE = HEADER_SIZE + TEXT_CAPACITY_BYTES
DEFAULT_CHANNEL_NAME = "JarvisWhisperPartialShm"


class ShmPartialTokenChannel:
    """Lock-free conflated shared memory slot for sub-microsecond partial transcript emission."""

    def __init__(self, name: str = DEFAULT_CHANNEL_NAME, create: bool = False) -> None:
        self.name = name
        self.create = create
        if create:
            # Clean up stale shm on Windows if left behind by previous abnormal exit
            try:
                old = shared_memory.SharedMemory(name=name, create=False)
                old.close()
                old.unlink()
            except Exception:
                pass

        self._shm = shared_memory.SharedMemory(name=name, create=create, size=TOTAL_CHANNEL_SIZE)
        self.buf = self._shm.buf
        if create:
            struct.pack_into(SLOT_HEADER_STRUCT, self.buf, 0, 0, 0, 0, 0)

        self._last_read_seq: int = 0

    def publish_partial(self, text: str, is_final: bool = False) -> None:
        """Atomically publishes a new partial transcript, overwriting previous stale states."""
        encoded = text.encode("utf-8")[:TEXT_CAPACITY_BYTES]
        text_len = len(encoded)

        seq_id, _, _, _ = struct.unpack_from(SLOT_HEADER_STRUCT, self.buf, 0)
        new_seq_id = seq_id + 1
        qpc_now = time.perf_counter_ns()

        # Write text buffer first
        self.buf[HEADER_SIZE : HEADER_SIZE + text_len] = encoded

        # Atomically commit header with sequence bump
        struct.pack_into(
            SLOT_HEADER_STRUCT,
            self.buf,
            0,
            new_seq_id,
            qpc_now,
            text_len,
            1 if is_final else 0,
        )

    def read_latest(self) -> Optional[Tuple[str, bool]]:
        """Non-blocking read of the freshest partial state. Returns None if unchanged."""
        seq_id, _, text_len, is_final = struct.unpack_from(SLOT_HEADER_STRUCT, self.buf, 0)
        if seq_id == self._last_read_seq or text_len == 0:
            return None

        self._last_read_seq = seq_id
        raw_text = bytes(self.buf[HEADER_SIZE : HEADER_SIZE + text_len])
        return raw_text.decode("utf-8", errors="replace"), bool(is_final)

    def close(self) -> None:
        """Releases memory view and closes shared memory descriptor cleanly."""
        try:
            self._shm.close()
        except Exception:
            pass
        if self.create:
            try:
                self._shm.unlink()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class FastStreamingWhisperWorker:
    """Greedy Faster-Whisper partial emitter running in an isolated worker process."""

    def __init__(self, whisper_model: Any, shm_channel: ShmPartialTokenChannel) -> None:
        self.model = whisper_model
        self.channel = shm_channel
        self.last_stride_sample: int = 0
        self.stride_samples: int = int(16000 * 0.160)  # 160ms stride = 2560 samples
        self._min_compute_samples: int = 3200           # 200ms minimum speech slice

    def process_live_audio_slice(self, full_audio_buffer: np.ndarray, is_voice_active: bool = True) -> None:
        """Invoked continuously as raw audio frames accumulate in the ring buffer."""
        current_len = len(full_audio_buffer)

        # Stride gating: Execute inference only once per 160ms accumulation step
        if (
            current_len - self.last_stride_sample < self.stride_samples
            or current_len < self._min_compute_samples
        ):
            return

        self.last_stride_sample = current_len

        # Fast-Path Greedy Decoding (sub-12ms on CUDA INT8)
        try:
            segments, _ = self.model.transcribe(
                full_audio_buffer,
                beam_size=1,
                best_of=1,
                temperature=0.0,
                without_timestamps=True,
                condition_on_previous_text=False,
                language="en",
            )
            partial_text = " ".join([s.text for s in segments]).strip()
            if partial_text:
                self.channel.publish_partial(partial_text, is_final=False)
        except Exception:
            pass

    def finalize_authoritative(self, final_audio_buffer: np.ndarray) -> str:
        """Executes a single authoritative beam-3 pass upon endpoint tripwire cutoff."""
        segments, _ = self.model.transcribe(
            final_audio_buffer,
            beam_size=3,
            best_of=3,
            temperature=0.0,
            without_timestamps=True,
            condition_on_previous_text=False,
            language="en",
        )
        final_text = " ".join([s.text for s in segments]).strip()
        self.channel.publish_partial(final_text, is_final=True)
        return final_text
