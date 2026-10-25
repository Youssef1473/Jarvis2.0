"""Local Egress Enforcement Proxy with Destination-IP Pinning for Jarvis 2.0 (CORE-NET-01B).

Binds strictly to 127.0.0.1 on an OS-allocated port.
Intercepts all outbound Chromium browser traffic, resolves hostnames, strictly verifies
all candidate IP addresses against SSRF, non-global IPs, loopback, private ranges, and
prohibited ports, and pins outbound TCP connections to the verified numeric IP literal
to eliminate TOCTOU DNS rebinding.

Supports:
- HTTPS / WSS: CONNECT tunnels with raw bidirectional TCP splicing (zero TLS interception / MITM).
- HTTP: GET, HEAD, POST, PUT, DELETE, OPTIONS, PATCH forwarding with body streaming and Connection: close.
- WebSocket: ws:// HTTP Upgrade transitions with bidirectional stream splicing.
"""
from __future__ import annotations

import collections.abc
import ipaddress
import logging
import select
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable

from jarvis.core.security_boundaries import (
    DISALLOWED_EGRESS_PORTS,
    SecurityBoundaryViolation,
    resolve_and_validate_host_port,
)

logger = logging.getLogger("Jarvis.EgressProxy")

# Approved outbound web ports (RFC 9110 compliant restriction)
DEFAULT_ALLOWED_WEB_PORTS: frozenset[int] = frozenset({80, 443, 8080, 8443})

# Resource limits
MAX_HEADER_BYTES = 32768  # 32 KB total header limit
MAX_REQUEST_LINE_BYTES = 8192
SOCKET_TIMEOUT = 15.0
BUFFER_SIZE = 32768


def _send_response(
    sock: socket.socket,
    status_code: int,
    reason: str,
    body: str = "",
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Sends a formatted HTTP error/status response and closes the socket fail-closed."""
    body_bytes = body.encode("utf-8") if body else b""
    headers = [
        f"HTTP/1.1 {status_code} {reason}",
        f"Content-Length: {len(body_bytes)}",
        "Content-Type: text/plain; charset=utf-8",
        "Connection: close",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append(f"{k}: {v}")
    payload = "\r\n".join(headers).encode("latin1") + b"\r\n\r\n" + body_bytes
    try:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _splice_sockets(sock1: socket.socket, sock2: socket.socket, timeout: float = 60.0) -> None:
    """Splices two sockets bidirectionally until one side closes or an error occurs."""
    sock1.setblocking(False)
    sock2.setblocking(False)
    socks = [sock1, sock2]
    while True:
        try:
            rlist, _, xlist = select.select(socks, [], socks, timeout)
        except (ValueError, OSError):
            break
        if xlist or not rlist:
            break
        for r in rlist:
            other = sock2 if r is sock1 else sock1
            try:
                data = r.recv(BUFFER_SIZE)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if not data:
                return
            total_sent = 0
            while total_sent < len(data):
                try:
                    sent = other.send(data[total_sent:])
                    if sent == 0:
                        return
                    total_sent += sent
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.005)
                except OSError:
                    return


class EgressProxyServer:
    """Local, non-decrypting HTTP forward and CONNECT proxy server with destination IP pinning."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        allowed_ports: collections.abc.Collection[int] | None = None,
        disallowed_ports: collections.abc.Collection[int] | None = None,
        destination_validator: Callable[[str, int], tuple[str, int, int]] | None = None,
        max_connections: int = 64,
        client_timeout: float = 30.0,
    ) -> None:
        if host not in ("127.0.0.1", "::1"):
            raise SecurityBoundaryViolation("EgressProxyServer must bind strictly to loopback (127.0.0.1).")
        self.host = host
        self.port = port
        self.allowed_ports = (
            frozenset(allowed_ports) if allowed_ports is not None else DEFAULT_ALLOWED_WEB_PORTS
        )
        self.disallowed_ports = (
            frozenset(disallowed_ports) if disallowed_ports is not None else DISALLOWED_EGRESS_PORTS
        )
        self._destination_validator = destination_validator or self._default_validator
        self.max_connections = max_connections
        self.client_timeout = client_timeout
        self.server_sock: socket.socket | None = None
        self.server_port: int | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_sockets: set[socket.socket] = set()
        self._lock = threading.RLock()
        self._semaphore = threading.Semaphore(max_connections)

    def _default_validator(self, host: str, port: int) -> tuple[str, int, int]:
        """Canonical destination validator enforcing SSRF, global IP, and port policies."""
        return resolve_and_validate_host_port(
            hostname=host,
            port=port,
            allow_local_mcp=False,
            allow_localhost=False,
            disallowed_ports=self.disallowed_ports,
            allowed_ports=self.allowed_ports,
        )

    def start(self) -> int:
        """Binds to loopback, starts listening worker thread, and returns dynamic server port."""
        with self._lock:
            if self.server_sock is not None:
                return self.server_port or 0

            self._stop_event.clear()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(128)
            sock.settimeout(1.0)
            self.server_sock = sock
            self.server_port = int(sock.getsockname()[1])

            self._thread = threading.Thread(
                target=self._accept_loop,
                name="JarvisEgressProxyAcceptor",
                daemon=True,
            )
            self._thread.start()
            logger.info("EgressProxyServer active on %s:%d", self.host, self.server_port)
            return self.server_port

    def is_alive(self) -> bool:
        """Returns True if the proxy is active and its listener socket is open."""
        with self._lock:
            if self._stop_event.is_set() or self.server_sock is None:
                return False
            return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Stops acceptor loop, terminates worker connections, and releases sockets."""
        with self._lock:
            self._stop_event.set()
            if self.server_sock:
                try:
                    self.server_sock.close()
                except OSError:
                    pass
                self.server_sock = None

            for s in list(self._active_sockets):
                try:
                    s.close()
                except OSError:
                    pass
            self._active_sockets.clear()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self.server_port = None
        logger.info("EgressProxyServer stopped.")

    def _accept_loop(self) -> None:
        """Accepts incoming loopback connections from the managed browser."""
        while not self._stop_event.is_set():
            try:
                if self.server_sock is None:
                    break
                client_sock, client_addr = self.server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            # Verify client is strictly loopback
            c_host = client_addr[0]
            if c_host not in ("127.0.0.1", "::1"):
                try:
                    client_sock.close()
                except OSError:
                    pass
                continue

            client_sock.settimeout(self.client_timeout)
            with self._lock:
                self._active_sockets.add(client_sock)

            t = threading.Thread(
                target=self._handle_client_wrapper,
                args=(client_sock,),
                daemon=True,
            )
            t.start()

    def _handle_client_wrapper(self, client_sock: socket.socket) -> None:
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            _send_response(client_sock, 503, "Service Unavailable", "Proxy concurrent connection limit reached.")
            self._close_socket(client_sock)
            return

        try:
            self._handle_client(client_sock)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("Proxy client handler notice: %s", exc)
        finally:
            self._semaphore.release()
            self._close_socket(client_sock)

    def _close_socket(self, sock: socket.socket) -> None:
        with self._lock:
            self._active_sockets.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def _create_outbound_connection(self, family: int, ip: str, port: int) -> socket.socket:
        """Creates outbound socket connected directly to pinned numeric IP literal."""
        out_sock = socket.socket(family, socket.SOCK_STREAM)
        out_sock.settimeout(SOCKET_TIMEOUT)
        with self._lock:
            self._active_sockets.add(out_sock)
        try:
            out_sock.connect((ip, port))
            return out_sock
        except Exception:
            self._close_socket(out_sock)
            raise

    def _read_headers(self, sock: socket.socket) -> tuple[bytes, bytes]:
        """Reads request headers up to MAX_HEADER_BYTES. Returns (header_bytes, leftover_body)."""
        buffer = bytearray()
        while not self._stop_event.is_set():
            if len(buffer) > MAX_HEADER_BYTES:
                raise ValueError("Request headers exceed 32KB limit.")
            try:
                chunk = sock.recv(4096)
            except (TimeoutError, OSError) as exc:
                raise ValueError(f"Socket error reading headers: {exc}") from exc
            if not chunk:
                break
            buffer.extend(chunk)
            idx = buffer.find(b"\r\n\r\n")
            if idx != -1:
                if (idx + 4) > MAX_HEADER_BYTES:
                    raise ValueError("Request headers exceed 32KB limit.")
                return bytes(buffer[:idx + 4]), bytes(buffer[idx + 4:])
            idx_lf = buffer.find(b"\n\n")
            if idx_lf != -1:
                if (idx_lf + 2) > MAX_HEADER_BYTES:
                    raise ValueError("Request headers exceed 32KB limit.")
                return bytes(buffer[:idx_lf + 2]), bytes(buffer[idx_lf + 2:])
            if len(buffer) > MAX_HEADER_BYTES:
                raise ValueError("Request headers exceed 32KB limit.")
        raise ValueError("Connection closed before complete headers received.")

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Parses and handles a single proxy request."""
        try:
            header_bytes, initial_body = self._read_headers(client_sock)
        except ValueError as exc:
            logger.debug("Malformed header read: %s", exc)
            status_code = 431 if "exceed" in str(exc) else 400
            reason = "Request Header Fields Too Large" if status_code == 431 else "Bad Request"
            _send_response(client_sock, status_code, reason, f"Header error: {exc}")
            return

        # Check for illegal control characters / null bytes in headers
        if b"\x00" in header_bytes:
            _send_response(client_sock, 400, "Bad Request", "Null bytes in request headers prohibited.")
            return

        try:
            header_str = header_bytes.decode("iso-8859-1")
        except UnicodeDecodeError:
            _send_response(client_sock, 400, "Bad Request", "Headers must be valid ASCII/ISO-8859-1.")
            return

        lines = header_str.splitlines()
        if not lines:
            _send_response(client_sock, 400, "Bad Request", "Empty request.")
            return

        request_line = lines[0].strip()
        parts = request_line.split()
        if len(parts) != 3:
            _send_response(client_sock, 400, "Bad Request", "Malformed request line.")
            return

        method, target, version = parts
        method = method.upper()

        if any(c in method for c in ("\r", "\n", "\x00")) or not method.isalpha():
            _send_response(client_sock, 400, "Bad Request", f"Invalid HTTP method: {method}")
            return

        # Parse headers dictionary
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line.strip():
                continue
            if ":" not in line:
                _send_response(client_sock, 400, "Bad Request", "Malformed header line.")
                return
            k, v = line.split(":", 1)
            k_clean = k.strip().lower()
            v_clean = v.strip()
            headers.setdefault(k_clean, []).append(v_clean)

        # Reject duplicate or conflicting Host headers
        host_headers = headers.get("host", [])
        if len(host_headers) > 1 and len(set(host_headers)) > 1:
            _send_response(client_sock, 400, "Bad Request", "Conflicting duplicate Host headers.")
            return

        if method == "CONNECT":
            self._handle_connect(client_sock, target, headers)
        else:
            self._handle_forward(client_sock, method, target, version, headers, initial_body)

    def _parse_authority(self, authority: str, default_port: int | None = None, require_port: bool = False) -> tuple[str, int]:
        """Strictly parses host and port from an authority string without stripping."""
        if not authority or "@" in authority or "/" in authority or "?" in authority:
            raise ValueError(f"Malformed or ambiguous authority: {authority}")

        # IPv6 literal [2001:db8::1]:port
        if authority.startswith("["):
            close_bracket = authority.find("]")
            if close_bracket == -1:
                raise ValueError("Unclosed IPv6 bracket in authority.")
            raw_ip = authority[1:close_bracket]
            try:
                ip_obj = ipaddress.ip_address(raw_ip)
                if ip_obj.version != 6:
                    raise ValueError("Not an IPv6 address.")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Malformed IPv6 literal in authority: {exc}") from exc

            remainder = authority[close_bracket + 1:]
            if remainder.startswith(":"):
                port_str = remainder[1:]
                if not port_str.isdigit():
                    raise ValueError(f"Non-digit port in authority: {port_str}")
                port = int(port_str)
            elif not remainder:
                if require_port or default_port is None:
                    raise ValueError("Explicit port required in authority.")
                port = default_port
            else:
                raise ValueError(f"Invalid characters after IPv6 literal: {remainder}")
            host = raw_ip
        else:
            if ":" in authority:
                h, p = authority.rsplit(":", 1)
                if not p.isdigit():
                    raise ValueError(f"Non-digit port in authority: {p}")
                port = int(p)
                host = h
            else:
                if require_port or default_port is None:
                    raise ValueError("Explicit port required in authority.")
                host = authority
                port = default_port

        if not (1 <= port <= 65535):
            raise ValueError(f"Port out of range: {port}")
        if not host:
            raise ValueError("Empty host in authority.")

        return host, port

    def _handle_connect(self, client_sock: socket.socket, target: str, headers: dict[str, list[str]]) -> None:
        """Handles HTTPS/WSS CONNECT tunnels with destination validation and socket pinning."""
        try:
            host, port = self._parse_authority(target, require_port=True)
        except ValueError as exc:
            _send_response(client_sock, 400, "Bad Request", str(exc))
            return

        # Check Host header consistency if present
        host_header = headers.get("host", [""])[0]
        if host_header:
            try:
                hh_host, hh_port = self._parse_authority(host_header, default_port=443)
                if hh_host.lower() != host.lower() or hh_port != port:
                    _send_response(client_sock, 400, "Bad Request", "CONNECT target does not match Host header.")
                    return
            except ValueError:
                _send_response(client_sock, 400, "Bad Request", "Malformed Host header in CONNECT.")
                return

        # Destination validation via canonical validator
        try:
            pinned_ip, address_family, validated_port = self._destination_validator(host, port)
        except SecurityBoundaryViolation as sec_err:
            if "DNS resolution failed" in str(sec_err):
                logger.debug("EgressProxy DNS resolution failed for %s: %s", host, sec_err)
                _send_response(client_sock, 502, "Bad Gateway", f"DNS resolution failed: {sec_err}")
                return
            logger.warning("EgressProxy blocked CONNECT to %s:%d: %s", host, port, sec_err)
            _send_response(
                client_sock,
                403,
                "Forbidden",
                f"Blocked by security policy (SSRF protection): {sec_err}",
            )
            return
        except socket.gaierror as dns_err:
            logger.debug("EgressProxy DNS resolution failed for %s: %s", host, dns_err)
            _send_response(client_sock, 502, "Bad Gateway", f"DNS resolution failed: {dns_err}")
            return
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Unexpected destination validation error: %s", exc)
            _send_response(client_sock, 502, "Bad Gateway", f"Validation error: {exc}")
            return

        # Connect directly to pinned numeric IP literal (no second DNS resolution)
        try:
            out_sock = self._create_outbound_connection(address_family, pinned_ip, validated_port)
        except TimeoutError as exc:
            logger.debug("Upstream connect timed out to %s (%s:%d)", host, pinned_ip, validated_port)
            _send_response(client_sock, 504, "Gateway Timeout", f"Upstream connection timed out: {exc}")
            return
        except (ConnectionRefusedError, OSError) as exc:
            logger.debug("Upstream connect failed to %s (%s:%d): %s", host, pinned_ip, validated_port, exc)
            _send_response(client_sock, 502, "Bad Gateway", f"Upstream connection failed: {exc}")
            return

        # Send 200 Connection Established and begin bidirectional splice
        try:
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except OSError:
            self._close_socket(out_sock)
            return

        try:
            _splice_sockets(client_sock, out_sock, timeout=self.client_timeout)
        finally:
            self._close_socket(out_sock)

    def _handle_forward(
        self,
        client_sock: socket.socket,
        method: str,
        target: str,
        version: str,
        headers: dict[str, list[str]],
        initial_body: bytes,
    ) -> None:
        """Handles plain HTTP requests (GET, POST, HEAD, etc.) and WebSocket ws:// upgrades."""
        # Parse target URI
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme:
            if parsed.scheme.lower() not in ("http", "ws"):
                _send_response(
                    client_sock,
                    400,
                    "Bad Request",
                    f"Unsupported proxy forwarding scheme '{parsed.scheme}'. Use CONNECT for HTTPS/WSS.",
                )
                return
            if not parsed.hostname:
                _send_response(client_sock, 400, "Bad Request", "Missing hostname in target URI.")
                return
            host = parsed.hostname
            port = parsed.port or 80
            path_and_query = parsed.path or "/"
            if parsed.query:
                path_and_query += f"?{parsed.query}"
        else:
            # Origin form target (e.g. /path) with Host header
            host_header = headers.get("host", [""])[0]
            if not host_header:
                _send_response(client_sock, 400, "Bad Request", "Missing Host header.")
                return
            try:
                host, port = self._parse_authority(host_header, default_port=80)
            except ValueError as exc:
                _send_response(client_sock, 400, "Bad Request", str(exc))
                return
            path_and_query = target

        # Check for userinfo credentials
        if "@" in host or "@" in (parsed.netloc or ""):
            _send_response(client_sock, 400, "Bad Request", "User-info credentials in URI are prohibited.")
            return

        # RFC 9112 Section 3.2.1 / Section 3.2.2: Validate Host header syntactically.
        # If the request target is in absolute-form, the proxy MUST use that authority even if
        # a Host header is present with a different value, and MUST replace the forwarded Host
        # header value with the target URI authority. Malformed Host headers must be rejected.
        host_header = headers.get("host", [""])[0]
        if host_header:
            try:
                self._parse_authority(host_header, default_port=80)
            except ValueError as exc:
                _send_response(client_sock, 400, "Bad Request", f"Malformed Host header: {exc}")
                return

        # Destination validation via canonical validator
        try:
            pinned_ip, address_family, validated_port = self._destination_validator(host, port)
        except SecurityBoundaryViolation as sec_err:
            if "DNS resolution failed" in str(sec_err):
                logger.debug("EgressProxy DNS resolution failed for %s: %s", host, sec_err)
                _send_response(client_sock, 502, "Bad Gateway", f"DNS resolution failed: {sec_err}")
                return
            logger.warning("EgressProxy blocked %s to %s:%d: %s", method, host, port, sec_err)
            _send_response(
                client_sock,
                403,
                "Forbidden",
                f"Blocked by security policy (SSRF protection): {sec_err}",
            )
            return
        except socket.gaierror as dns_err:
            logger.debug("EgressProxy DNS resolution failed for %s: %s", host, dns_err)
            _send_response(client_sock, 502, "Bad Gateway", f"DNS resolution failed: {dns_err}")
            return
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Unexpected destination validation error: %s", exc)
            _send_response(client_sock, 502, "Bad Gateway", f"Validation error: {exc}")
            return

        # Connect directly to pinned numeric IP literal
        try:
            out_sock = self._create_outbound_connection(address_family, pinned_ip, validated_port)
        except TimeoutError as exc:
            logger.debug("Upstream connect timed out to %s (%s:%d)", host, pinned_ip, validated_port)
            _send_response(client_sock, 504, "Gateway Timeout", f"Upstream connection timed out: {exc}")
            return
        except (ConnectionRefusedError, OSError) as exc:
            logger.debug("Upstream connect failed to %s (%s:%d): %s", host, pinned_ip, validated_port, exc)
            _send_response(client_sock, 502, "Bad Gateway", f"Upstream connection failed: {exc}")
            return

        # Determine if this is a WebSocket upgrade
        is_ws_upgrade = (
            "websocket" in [v.lower() for v in headers.get("upgrade", [])]
            and "upgrade" in [v.lower() for v in headers.get("connection", [])]
        )

        # Build rewritten request line and headers
        fwd_lines = [f"{method} {path_and_query} HTTP/1.1"]
        # RFC 9112 Section 3.2.1: Replace forwarded Host header with target URI authority
        host_header_val = host if port == 80 else f"{host}:{port}"
        fwd_lines.append(f"Host: {host_header_val}")

        for k, v_list in headers.items():
            if k in ("proxy-connection", "proxy-authorization", "host"):
                continue
            if not is_ws_upgrade and k == "connection":
                continue
            for val in v_list:
                fwd_lines.append(f"{k.title()}: {val}")

        if not is_ws_upgrade:
            fwd_lines.append("Connection: close")

        fwd_payload = "\r\n".join(fwd_lines).encode("latin1") + b"\r\n\r\n"

        try:
            out_sock.sendall(fwd_payload)
        except OSError:
            self._close_socket(out_sock)
            return

        # Forward request body if present
        content_length_headers = headers.get("content-length", [])
        transfer_encodings = headers.get("transfer-encoding", [])

        if content_length_headers:
            try:
                cl = int(content_length_headers[0])
            except ValueError:
                self._close_socket(out_sock)
                _send_response(client_sock, 400, "Bad Request", "Invalid Content-Length.")
                return

            sent_body = len(initial_body)
            if initial_body:
                to_send = initial_body[:cl]
                out_sock.sendall(to_send)

            remaining = cl - sent_body
            while remaining > 0:
                chunk = client_sock.recv(min(remaining, BUFFER_SIZE))
                if not chunk:
                    break
                out_sock.sendall(chunk)
                remaining -= len(chunk)
        elif "chunked" in [te.lower() for te in transfer_encodings]:
            # Forward initial body chunk and stream remaining until 0\r\n\r\n
            if initial_body:
                out_sock.sendall(initial_body)
            while True:
                chunk = client_sock.recv(BUFFER_SIZE)
                if not chunk:
                    break
                out_sock.sendall(chunk)
                if b"0\r\n\r\n" in chunk:
                    break
        elif initial_body:
            out_sock.sendall(initial_body)

        # Handle response
        if is_ws_upgrade:
            # Read upstream response headers for 101 Switching Protocols
            try:
                up_headers, up_leftover = self._read_headers(out_sock)
                client_sock.sendall(up_headers)
                if up_leftover:
                    client_sock.sendall(up_leftover)
                if b"101 " in up_headers[:32]:
                    # Transition to raw bidirectional splice
                    _splice_sockets(client_sock, out_sock, timeout=self.client_timeout)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.debug("WebSocket upgrade forwarding error: %s", exc)
            finally:
                self._close_socket(out_sock)
        else:
            # Stream response until upstream closes or completes
            try:
                while True:
                    data = out_sock.recv(BUFFER_SIZE)
                    if not data:
                        break
                    client_sock.sendall(data)
            except OSError:
                pass
            finally:
                self._close_socket(out_sock)
