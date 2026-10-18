"""Non-blocking IPC listener for IDE and PowerShell prompt hooks.

Listens on a localhost UDP port or Win32 Named Pipe and passes diffs
to FlowStateMirror.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Optional

from jarvis.context.flow_state_mirror import FlowStateMirror
from jarvis.speculative.engine import EnvironmentEpochTracker as SpeculativeEpochTracker

logger = logging.getLogger("Jarvis.Context.PipeListener")

DEFAULT_UDP_PORT = 31415


class FlowStateBridgeListener:
    """UDP and socket bridge listener for IDE and Shell telemetry."""

    def __init__(self, port: int = DEFAULT_UDP_PORT):
        self.port = port
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._last_cursor_ts = 0.0

    def start(self) -> bool:
        if self.running:
            return True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("127.0.0.1", self.port))
            self._sock.settimeout(0.5)
            self.running = True
            self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="FlowStateBridgeListener")
            self._thread.start()
            logger.info("FlowStateBridgeListener bound to 127.0.0.1:%d", self.port)
            return True
        except Exception as e:
            logger.debug("Failed to bind FlowStateBridgeListener to port %d: %s", self.port, e)
            self.running = False
            return False

    def _listen_loop(self):
        mirror = FlowStateMirror.get_instance()
        while self.running:
            try:
                data, _ = self._sock.recvfrom(4096)
                if not data:
                    continue
                payload = json.loads(data.decode("utf-8", errors="replace"))
                msg_type = payload.get("type", "")

                if msg_type == "ide_state":
                    now = time.monotonic()
                    # Debounce cursor moves to 150ms
                    if now - self._last_cursor_ts < 0.150 and payload.get("is_move"):
                        continue
                    self._last_cursor_ts = now
                    mirror.update_snapshot(
                        active_file_path=payload.get("file"),
                        cursor_line=payload.get("line"),
                        selection_range=tuple(payload["selection"]) if "selection" in payload else None,
                    )
                elif msg_type == "terminal_state":
                    try:
                        SpeculativeEpochTracker.get_instance().bump_terminal_seq()
                    except Exception:
                        pass
                    mirror.update_snapshot(
                        last_terminal_cmd=payload.get("cmd"),
                        last_exit_code=payload.get("exit_code"),
                        stderr_tail=payload.get("stderr"),
                    )
            except socket.timeout:
                continue
            except Exception as e:
                logger.debug("Error processing bridge packet: %s", e)

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None
