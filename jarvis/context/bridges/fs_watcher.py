"""Workspace filesystem modification watcher using ReadDirectoryChangesW.

Monitors active workspace root and bumps EnvironmentEpochTracker.bump(ResourceScope.FILESYSTEM)
immediately on file additions, modifications, or deletions to ensure TOCTOU invalidation.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path

from jarvis.context.flow_state_mirror import EnvironmentEpochTracker, ResourceScope
from jarvis.speculative.engine import EnvironmentEpochTracker as SpeculativeEpochTracker

logger = logging.getLogger("Jarvis.Context.FSWatcher")

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_SIZE = 0x00000008

OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
GENERIC_READ = 0x80000000


class WorkspaceFilesystemWatcher:
    """Watches the workspace directory and triggers EnvironmentEpochTracker bumps."""

    def __init__(self, workspace_root: Path | str = "."):
        self.workspace_root = Path(workspace_root).resolve()
        self.running = False
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self.running:
            return True
        self.running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True, name="FSWatcherThread")
        self._thread.start()
        return True

    def _watch_loop(self):
        if sys.platform == "win32":
            self._watch_win32()
        else:
            self._watch_fallback()

    def _watch_win32(self):
        try:
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32

            h_dir = k32.CreateFileW(
                str(self.workspace_root),
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None
            )
            if h_dir == -1 or h_dir == 0:
                self._watch_fallback()
                return

            buf = ctypes.create_string_buffer(4096)
            bytes_returned = wintypes.DWORD(0)
            flags = (
                FILE_NOTIFY_CHANGE_FILE_NAME |
                FILE_NOTIFY_CHANGE_DIR_NAME |
                FILE_NOTIFY_CHANGE_LAST_WRITE |
                FILE_NOTIFY_CHANGE_SIZE
            )

            k32.ReadDirectoryChangesW.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
                ctypes.c_void_p
            ]
            k32.ReadDirectoryChangesW.restype = wintypes.BOOL

            while self.running:
                success = k32.ReadDirectoryChangesW(
                    h_dir,
                    buf,
                    ctypes.sizeof(buf),
                    True,  # recursive
                    flags,
                    ctypes.byref(bytes_returned),
                    None,
                    None
                )
                if success and bytes_returned.value > 0:
                    EnvironmentEpochTracker.bump(ResourceScope.FILESYSTEM)
                    try:
                        SpeculativeEpochTracker.get_instance().bump_fs_epoch()
                    except Exception:
                        pass
                    logger.debug("Filesystem change detected in workspace. Epoch bumped.")
                time.sleep(0.05)

            k32.CloseHandle(h_dir)
        except Exception as e:
            logger.debug("Win32 ReadDirectoryChangesW failed: %s; using fallback", e)
            self._watch_fallback()

    def _watch_fallback(self):
        # Polling fallback if ReadDirectoryChangesW is unavailable
        last_mtime = 0.0
        while self.running:
            try:
                if self.workspace_root.exists():
                    current_mtime = os.stat(self.workspace_root).st_mtime
                    if last_mtime != 0.0 and current_mtime != last_mtime:
                        EnvironmentEpochTracker.bump(ResourceScope.FILESYSTEM)
                        try:
                            SpeculativeEpochTracker.get_instance().bump_fs_epoch()
                        except Exception:
                            pass
                    last_mtime = current_mtime
            except Exception:
                pass
            time.sleep(0.5)

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
            self._thread = None
