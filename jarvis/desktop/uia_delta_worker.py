"""UIA Delta Worker consuming the 64KB SPSC shared memory ring buffer.

Updates live in-memory active window / control state and bumps ResourceScope.UIA,
tripping VisualPrivacyGuard atomically if sensitive privacy flags are raised.
"""
from __future__ import annotations

import logging
import mmap
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass

from jarvis.context.flow_state_mirror import (
    EnvironmentEpochTracker,
    ResourceScope,
    VisualPrivacyGuard,
)

logger = logging.getLogger("Jarvis.Desktop.UIADelta")

SHM_NAME = "Local\\JarvisWinEventRing"
RING_CAPACITY = 1024
FRAME_SIZE = 64
HEADER_SIZE = 64
TOTAL_SHM_SIZE = HEADER_SIZE + (RING_CAPACITY * FRAME_SIZE)


@dataclass
class WinEventFramePython:
    qpc_timestamp: int
    hwnd: int
    pid: int
    event_id: int
    obj_id: int
    privacy_flags: int
    utf8_title: str


class UIADeltaWorker:
    """Background SPSC consumer reading WinEvent frames and updating live UIA state."""

    def __init__(self, shm_name: str = SHM_NAME):
        self.shm_name = shm_name
        self.running = False
        self._thread: threading.Thread | None = None
        self._shm_buf: bytearray | mmap.mmap | None = None
        self.active_hwnd: int = 0
        self.active_title: str = ""
        self.events_processed: int = 0
        self._lock = threading.Lock()

    def attach_buffer(self, buffer: bytearray | mmap.mmap):
        """Attaches a raw memory buffer (for testing or direct shared memory)."""
        self._shm_buf = buffer

    def start(self) -> bool:
        if self.running:
            return True

        if self._shm_buf is None:
            if sys.platform == "win32":
                try:
                    # Try to open existing file mapping
                    self._shm_buf = mmap.mmap(0, TOTAL_SHM_SIZE, tagname=self.shm_name, access=mmap.ACCESS_WRITE)
                except Exception as e:
                    logger.debug("Could not attach to native WinEvent ring: %s; using in-memory buffer", e)
                    self._shm_buf = bytearray(TOTAL_SHM_SIZE)
                    # Initialize header
                    struct.pack_into("<QQII", self._shm_buf, 0, 0, 0, 0, RING_CAPACITY)
            else:
                self._shm_buf = bytearray(TOTAL_SHM_SIZE)
                struct.pack_into("<QQII", self._shm_buf, 0, 0, 0, 0, RING_CAPACITY)

        self.running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="UIADeltaWorker")
        self._thread.start()
        logger.info("UIADeltaWorker started.")
        return True

    def _worker_loop(self):
        while self.running:
            self.poll_once()
            time.sleep(0.005)

    def poll_once(self) -> int:
        """Reads all pending frames from the ring buffer. Returns number of frames processed."""
        if self._shm_buf is None:
            return 0

        buf = self._shm_buf
        write_idx, read_idx, privacy_lock, capacity = struct.unpack_from("<QQII", buf, 0)

        # Check atomic privacy lock
        if privacy_lock == 1:
            VisualPrivacyGuard.set_lock(True)

        if read_idx >= write_idx:
            return 0

        # Safely drop frames if queue backlog exceeds 1,024 events without allocating memory
        if write_idx - read_idx > RING_CAPACITY:
            read_idx = write_idx - RING_CAPACITY

        count = 0
        while read_idx < write_idx:
            slot = read_idx % RING_CAPACITY
            offset = HEADER_SIZE + (slot * FRAME_SIZE)

            qpc, hwnd, pid, event_id, obj_id, privacy_flags = struct.unpack_from("<QQIIII", buf, offset)
            raw_title = bytes(buf[offset + 32 : offset + 64])
            title = raw_title.split(b"\x00")[0].decode("utf-8", errors="replace")

            if privacy_flags == 1:
                VisualPrivacyGuard.set_lock(True)

            with self._lock:
                self.active_hwnd = hwnd
                self.active_title = title
                self.events_processed += 1

            read_idx += 1
            count += 1

        # Advance read_idx in shared memory header
        struct.pack_into("<Q", buf, 8, read_idx)
        EnvironmentEpochTracker.bump(ResourceScope.UIA)
        return count

    def push_frame_for_test(self, hwnd: int, event_id: int, title: str, privacy: int = 0):
        """Helper to simulate unmanaged C++ event push into the SPSC buffer."""
        if self._shm_buf is None:
            self._shm_buf = bytearray(TOTAL_SHM_SIZE)
            struct.pack_into("<QQII", self._shm_buf, 0, 0, 0, 0, RING_CAPACITY)

        buf = self._shm_buf
        write_idx = struct.unpack_from("<Q", buf, 0)[0]
        slot = write_idx % RING_CAPACITY
        offset = HEADER_SIZE + (slot * FRAME_SIZE)

        qpc = time.monotonic_ns()
        struct.pack_into("<QQIIII", buf, offset, qpc, hwnd, os.getpid(), event_id, 0, privacy)

        title_bytes = title.encode("utf-8")[:31]
        buf[offset + 32 : offset + 32 + len(title_bytes)] = title_bytes
        buf[offset + 32 + len(title_bytes) : offset + 64] = b"\x00" * (32 - len(title_bytes))

        if privacy == 1:
            struct.pack_into("<I", buf, 16, 1)

        struct.pack_into("<Q", buf, 0, write_idx + 1)

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._shm_buf is not None and isinstance(self._shm_buf, mmap.mmap):
            try:
                self._shm_buf.close()
            except Exception:
                pass
            self._shm_buf = None
