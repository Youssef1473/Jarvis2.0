"""
Zero-Copy Multiprocessing Shared-Memory SPSC Float32 Audio Ring Buffer.
Header Layout (32 bytes, little-endian int64):
  [0:8]   write_idx (monotonic sample counter)
  [8:16]  read_idx  (monotonic sample counter)
  [16:24] barge_in_epoch (incremented on speech interruption for <1us cross-process drain)
  [24:32] sample_rate
Payload Layout:
  [32 : 32 + capacity_samples * 4] float32 PCM samples
"""
from __future__ import annotations

from multiprocessing import shared_memory
import struct
import sys
import numpy as np

HEADER_BYTES = 32
HEADER_STRUCT = struct.Struct("<qqqq")


class ShmAudioRingTransport:
    """Lock-free Single-Producer Single-Consumer SharedMemory PCM transport."""

    def __init__(
        self,
        name: str,
        capacity_samples: int = 24000 * 10,
        sample_rate: int = 24000,
        create: bool = True,
    ) -> None:
        self.name = name
        self.capacity = int(capacity_samples)
        self.sample_rate = int(sample_rate)
        self._is_owner = bool(create)
        total_bytes = HEADER_BYTES + (self.capacity * 4)

        kwargs: dict[str, object] = {
            "name": name,
            "create": create,
            "size": total_bytes,
        }
        # Python 3.13+ supports track=False for non-owner attachment; Windows handles kernel refcounting automatically.
        if sys.version_info >= (3, 13) and not create:
            kwargs["track"] = False

        self._shm = shared_memory.SharedMemory(**kwargs)  # type: ignore[arg-type]
        self._header_buf = memoryview(self._shm.buf)[:HEADER_BYTES]
        self._audio_view = np.ndarray(
            (self.capacity,),
            dtype=np.float32,
            buffer=memoryview(self._shm.buf)[HEADER_BYTES : HEADER_BYTES + self.capacity * 4],
        )

        if create:
            HEADER_STRUCT.pack_into(self._header_buf, 0, 0, 0, 0, self.sample_rate)
            self._audio_view.fill(0.0)

    def _read_header(self) -> tuple[int, int, int, int]:
        return HEADER_STRUCT.unpack_from(self._header_buf, 0)  # type: ignore[return-value]

    def write_pcm(self, samples: np.ndarray) -> int:
        """Writes float32 PCM samples into shared memory without pickle/pipe copies."""
        pcm = np.asarray(samples, dtype=np.float32).ravel()
        n = pcm.shape[0]
        if n == 0:
            return 0
        if n > self.capacity:
            pcm = pcm[-self.capacity :]
            n = self.capacity

        write_idx, read_idx, epoch, sr = self._read_header()
        unread = write_idx - read_idx
        if unread + n > self.capacity:
            # Advance read_idx on overflow so consumer always reads freshest audio
            read_idx = (write_idx + n) - self.capacity

        pos = write_idx % self.capacity
        first = min(n, self.capacity - pos)
        self._audio_view[pos : pos + first] = pcm[:first]
        if first < n:
            self._audio_view[0 : n - first] = pcm[first:n]

        HEADER_STRUCT.pack_into(self._header_buf, 0, write_idx + n, read_idx, epoch, sr)
        return n

    def read_pcm_into(self, out: np.ndarray) -> int:
        """Reads available float32 samples into pre-allocated `out` array."""
        write_idx, read_idx, epoch, sr = self._read_header()
        avail = max(0, min(self.capacity, write_idx - read_idx))
        n = min(out.shape[0], avail)
        if n == 0:
            return 0

        pos = read_idx % self.capacity
        first = min(n, self.capacity - pos)
        out[:first] = self._audio_view[pos : pos + first]
        if first < n:
            out[first:n] = self._audio_view[0 : n - first]

        HEADER_STRUCT.pack_into(self._header_buf, 0, write_idx, read_idx + n, epoch, sr)
        return n

    def trigger_barge_in_clear(self) -> int:
        """Atomically equates read_idx to write_idx and increments barge_in_epoch (<1us)."""
        write_idx, _, epoch, sr = self._read_header()
        new_epoch = epoch + 1
        HEADER_STRUCT.pack_into(self._header_buf, 0, write_idx, write_idx, new_epoch, sr)
        return new_epoch

    @property
    def barge_in_epoch(self) -> int:
        return self._read_header()[2]

    def close(self) -> None:
        if hasattr(self, "_audio_view"):
            del self._audio_view
        if hasattr(self, "_header_buf"):
            self._header_buf.release()
        self._shm.close()
        if self._is_owner:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass
