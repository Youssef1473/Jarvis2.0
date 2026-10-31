"""Non-blocking IPC listener for IDE and PowerShell prompt hooks.

Listens on a localhost UDP port or Win32 Named Pipe and passes diffs
to FlowStateMirror with mandatory HMAC-SHA256 session token verification.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from jarvis.context.flow_state_mirror import FlowStateMirror
from jarvis.speculative.engine import EnvironmentEpochTracker as SpeculativeEpochTracker

logger = logging.getLogger("Jarvis.Context.PipeListener")

DEFAULT_UDP_PORT = 31415


class FlowStateBridgeListener:
    """UDP and socket bridge listener for IDE and Shell telemetry with HMAC authentication."""

    TOKEN_PATH = Path.home() / ".jarvis_sec" / "flow_state.token"

    def __init__(self, port: int = DEFAULT_UDP_PORT, auth_token: str | None = None):
        self.port = port
        token = auth_token or os.environ.get("JARVIS_FLOW_STATE_TOKEN")
        if not token:
            try:
                if self.TOKEN_PATH.exists():
                    saved = self.TOKEN_PATH.read_text(encoding="utf-8").strip()
                    if len(saved) >= 32:
                        token = saved
            except Exception:
                pass
        if not token:
            token = secrets.token_hex(32)
        self.auth_token = token
        self._persist_token()
        self.running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._last_cursor_ts = 0.0

    def _persist_token(self) -> None:
        """Persists the session auth token with user-restricted permissions."""
        try:
            token_file = self.TOKEN_PATH
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(self.auth_token, encoding="utf-8")
            if sys.platform != "win32":
                try:
                    os.chmod(token_file, 0o600)
                except Exception:
                    pass
            else:
                try:
                    import subprocess
                    user_principal = os.environ.get("USERNAME")
                    try:
                        who = subprocess.run(
                            ["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True,
                            text=True,
                            timeout=1.0,
                            check=False,
                        )
                        if who.returncode == 0 and who.stdout.strip():
                            parts = [p.strip(' "\r\n') for p in who.stdout.strip().split(",")]
                            if len(parts) >= 2 and parts[1].startswith("S-1-"):
                                user_principal = f"*{parts[1]}"
                    except Exception as sid_err:
                        logger.debug("SID lookup note: %s", sid_err)
                    if user_principal:
                        subprocess.run(
                            ["icacls", str(token_file), "/inheritance:r", "/grant:r", f"{user_principal}:(R,W)"],
                            capture_output=True,
                            timeout=2.0,
                            check=False,
                        )
                except Exception as acl_err:
                    logger.debug("Could not restrict token file ACL: %s", acl_err)
        except Exception as e:
            logger.debug("Failed to persist flow-state token: %s", e)

    def get_auth_token(self) -> str:
        """Returns the active session authentication token."""
        return self.auth_token

    def sign_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Signs a telemetry payload dictionary with HMAC-SHA256 using the session token."""
        clean_dict = {k: v for k, v in payload.items() if k not in ("signature", "hmac")}
        raw_bytes = json.dumps(clean_dict, sort_keys=True).encode("utf-8")
        sig = hmac.new(self.auth_token.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
        signed = dict(payload)
        signed["signature"] = sig
        return signed

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
            logger.info("FlowStateBridgeListener bound to 127.0.0.1:%d (HMAC-SHA256 authenticated)", self.port)
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
                try:
                    payload = json.loads(data.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                if not isinstance(payload, dict):
                    continue

                # Enforce cryptographic session authentication
                token = payload.get("token") or payload.get("auth_token")
                sig = payload.get("signature") or payload.get("hmac")
                is_valid = False

                if token and isinstance(token, str):
                    if hmac.compare_digest(token, self.auth_token):
                        is_valid = True
                elif sig and isinstance(sig, str):
                    clean_dict = {k: v for k, v in payload.items() if k not in ("signature", "hmac")}
                    raw_bytes = json.dumps(clean_dict, sort_keys=True).encode("utf-8")
                    expected_sig = hmac.new(self.auth_token.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
                    if hmac.compare_digest(sig.lower(), expected_sig.lower()):
                        is_valid = True

                if not is_valid:
                    logger.warning("FlowStateBridgeListener rejected unauthenticated packet from localhost UDP")
                    continue

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
            except TimeoutError:
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
