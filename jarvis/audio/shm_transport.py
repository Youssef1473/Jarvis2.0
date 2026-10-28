"""
Zero-Copy Multiprocessing Shared-Memory SPSC Float32 Audio Ring Buffer.

Header layout (64 bytes = 8 x little-endian int64, 8-byte aligned slots).
Every slot has exactly ONE writer, so no side ever performs a read-modify-write
of a word owned by another side (this removes the lost-update race of the
previous "pack all four fields" design):

  slot  offset  name         single writer            readers
  ----  ------  -----------  -----------------------  ----------------
    0      0    write_idx    producer (write_pcm)     consumer
    1      8    read_idx     consumer (read_pcm_into) producer (flow control)
    2     16    barge_epoch  trigger_barge_in_clear   consumer
    3     24    sample_rate  creator, init only       anyone
    4     32    cut_idx      trigger_barge_in_clear   consumer
    5     40    claim_idx    producer (write_pcm)     consumer
    6-7   48    reserved

Payload layout:
  [64 : 64 + capacity_samples * 4]  float32 PCM samples

Protocol
--------
* Producer: store claim_idx = write_idx + n  ->  copy payload  ->  store
  write_idx = write_idx + n (publish).  The producer never touches read_idx.
* Consumer: reads published samples [read_idx, write_idx), then re-reads
  claim_idx.  If claim_idx - read_idx > capacity the producer may have lapped
  (overwritten) the region mid-copy, so the copy is discarded and the consumer
  skips forward (seqlock-style validation).  Overflow handling is therefore
  consumer-side; the producer never needs to move read_idx.
* Barge-in: trigger_barge_in_clear() records cut_idx = write_idx, then bumps
  barge_epoch.  The consumer notices the epoch change on its next read and
  advances read_idx to cut_idx itself.  Audio written AFTER the trigger (the
  next turn) is preserved; only audio queued before it is dropped.

Every header word is read/written as one aligned 8-byte load/store through a
numpy int64 view, which is a single machine instruction on x86-64 and ARM64
(no torn reads).  The ordering arguments above (claim before payload before
publish; payload before claim re-check) rely on x86-64 TSO store/load ordering,
which is the target platform.  A weaker memory model (e.g. Windows-on-ARM)
would need explicit barriers.
"""
from __future__ import annotations

import sys
import threading
from multiprocessing import shared_memory

import numpy as np

DEFAULT_RING_NAME = "JarvisAudioShmRing"

HEADER_BYTES = 64
_HEADER_SLOTS = HEADER_BYTES // 8

_WRITE_IDX = 0
_READ_IDX = 1
_EPOCH = 2
_SAMPLE_RATE = 3
_CUT_IDX = 4
_CLAIM_IDX = 5

_MAX_READ_RETRIES = 4


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
        self._closed = False
        self._trigger_lock = threading.Lock()  # serialises in-process triggers only
        # Overflow resync keeps this many samples of headroom below capacity so
        # the consumer does not immediately get lapped again.
        self._guard = max(1, self.capacity // 8)
        # Consumer-side diagnostics (local to this instance).
        self._overruns = 0
        self._barge_in_dropped = 0
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
        buf = self._shm.buf
        self._hdr_mv = buf[:HEADER_BYTES]
        self._pcm_mv = buf[HEADER_BYTES : HEADER_BYTES + self.capacity * 4]
        self._hdr = np.ndarray((_HEADER_SLOTS,), dtype=np.int64, buffer=self._hdr_mv)
        self._audio_view = np.ndarray((self.capacity,), dtype=np.float32, buffer=self._pcm_mv)

        if create:
            self._hdr[:] = 0
            self._hdr[_SAMPLE_RATE] = self.sample_rate
            self._audio_view.fill(0.0)
            self._seen_epoch = 0
        else:
            # A late attacher ignores barge-ins that happened before it attached.
            self._seen_epoch = int(self._hdr[_EPOCH])

    @classmethod
    def open_or_create(
        cls,
        name: str = DEFAULT_RING_NAME,
        capacity_samples: int = 24000 * 10,
        sample_rate: int = 24000,
    ) -> ShmAudioRingTransport:
        """Creates the ring, or attaches if another party (TTS producer or DSP client) already did.

        Both ends call this, so startup order does not matter and neither side
        fails with FileExistsError.  Only the creator is the ``_is_owner``.
        """
        try:
            return cls(name, capacity_samples=capacity_samples, sample_rate=sample_rate, create=True)
        except FileExistsError:
            return cls(name, capacity_samples=capacity_samples, sample_rate=sample_rate, create=False)

    # ------------------------------------------------------------------ state
    @property
    def write_index(self) -> int:
        """Monotonic count of samples published by the producer."""
        return int(self._hdr[_WRITE_IDX])

    @property
    def read_index(self) -> int:
        """Monotonic count of samples consumed (or skipped) by the consumer."""
        return int(self._hdr[_READ_IDX])

    @property
    def free_samples(self) -> int:
        """Producer-side flow control: samples that can be written without overwriting unread audio."""
        return max(0, self.capacity - (self.write_index - self.read_index))

    @property
    def barge_in_epoch(self) -> int:
        return int(self._hdr[_EPOCH])

    @property
    def stats(self) -> dict[str, int]:
        """Consumer-side counters: overrun resyncs and samples dropped by barge-in."""
        return {"overruns": self._overruns, "barge_in_dropped": self._barge_in_dropped}

    # --------------------------------------------------------------- producer
    def write_pcm(self, samples: np.ndarray) -> int:
        """Writes float32 PCM into shared memory without pickle/pipe copies.

        Writes only write_idx / claim_idx.  If the consumer falls more than a
        full buffer behind, the oldest unread audio is overwritten and the
        consumer skips forward on its own side.
        """
        pcm = np.asarray(samples, dtype=np.float32).ravel()
        n = int(pcm.shape[0])
        if n == 0:
            return 0
        cap = self.capacity
        if n > cap:
            pcm = pcm[-cap:]
            n = cap

        hdr = self._hdr
        write_idx = int(hdr[_WRITE_IDX])
        hdr[_CLAIM_IDX] = write_idx + n  # 1) announce the region about to be overwritten

        pos = write_idx % cap
        first = min(n, cap - pos)
        view = self._audio_view
        view[pos : pos + first] = pcm[:first]  # 2) copy payload
        if first < n:
            view[0 : n - first] = pcm[first:n]

        hdr[_WRITE_IDX] = write_idx + n  # 3) publish
        return n

    # --------------------------------------------------------------- consumer
    def _apply_barge_in(self, read_idx: int) -> int:
        hdr = self._hdr
        epoch = int(hdr[_EPOCH])
        if epoch != self._seen_epoch:
            cut = int(hdr[_CUT_IDX])  # read after epoch; trigger writes cut before epoch
            self._seen_epoch = epoch
            if cut > read_idx:
                self._barge_in_dropped += cut - read_idx
                read_idx = cut
                hdr[_READ_IDX] = read_idx  # consumer owns read_idx
        return read_idx

    def read_pcm_into(self, out: np.ndarray) -> int:
        """Reads available float32 samples into pre-allocated `out`; writes only read_idx."""
        want = int(out.shape[0])
        if want == 0:
            return 0
        cap = self.capacity
        hdr = self._hdr
        view = self._audio_view

        read_idx = self._apply_barge_in(int(hdr[_READ_IDX]))

        for _ in range(_MAX_READ_RETRIES):
            write_idx = int(hdr[_WRITE_IDX])
            claim_idx = int(hdr[_CLAIM_IDX])

            if write_idx < read_idx:
                # Producer restarted / header reset underneath us: resynchronise.
                read_idx = write_idx
                hdr[_READ_IDX] = read_idx
                return 0

            if max(write_idx, claim_idx) - read_idx > cap:
                # Producer lapped the consumer: skip forward, keep some headroom.
                self._overruns += 1
                read_idx = max(write_idx, claim_idx) - cap + self._guard
                read_idx = min(read_idx, write_idx)

            unread = write_idx - read_idx
            if unread <= 0:
                if read_idx != int(hdr[_READ_IDX]):
                    hdr[_READ_IDX] = read_idx
                return 0

            n = min(want, unread)
            pos = read_idx % cap
            first = min(n, cap - pos)
            out[:first] = view[pos : pos + first]
            if first < n:
                out[first:n] = view[0 : n - first]

            # Seqlock validation: did the producer claim (start overwriting)
            # any slot we just copied while we were copying it?
            if int(hdr[_CLAIM_IDX]) - read_idx > cap:
                self._overruns += 1
                latest = max(int(hdr[_WRITE_IDX]), int(hdr[_CLAIM_IDX]))
                read_idx = min(latest - cap + self._guard, int(hdr[_WRITE_IDX]))
                continue

            hdr[_READ_IDX] = read_idx + n
            return n

        hdr[_READ_IDX] = read_idx
        return 0

    # ---------------------------------------------------------------- control
    def trigger_barge_in_clear(self) -> int:
        """Flush all audio queued before this call (callable from either side).

        Touches neither write_idx nor read_idx: it records the current write
        position as the cut point, then bumps the epoch.  The consumer applies
        the cut on its next read, so audio the producer writes after this call
        is NOT discarded.  Concurrent triggers from separate processes may
        coalesce into one epoch bump, which is harmless.
        """
        hdr = self._hdr
        with self._trigger_lock:
            hdr[_CUT_IDX] = int(hdr[_WRITE_IDX])  # cut first ...
            epoch = int(hdr[_EPOCH]) + 1
            hdr[_EPOCH] = epoch  # ... then epoch, so a consumer seeing the epoch sees the cut
        return epoch

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Drop numpy exports before releasing the underlying buffers.
        for attr in ("_audio_view", "_hdr"):
            if hasattr(self, attr):
                delattr(self, attr)
        for attr in ("_pcm_mv", "_hdr_mv"):
            mv = getattr(self, attr, None)
            if mv is not None:
                mv.release()
        self._shm.close()
        if self._is_owner:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass
