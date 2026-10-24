"""Closed-Loop Security Boundaries, SSRF Protection, Path Confinement, and Taint Tracking for Jarvis 2.0."""
import hashlib
import hmac
import contextvars
import http.client
import ipaddress
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Collection, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Jarvis.SecurityBoundaries")


class SecurityBoundaryViolation(PermissionError):
    """Raised when an operation attempts to breach security, SSRF, or path boundaries."""
    pass


WINDOWS_RESERVED_DEVICE_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

SENSITIVE_AUTH_HEADERS: Set[str] = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "cookie2",
    "x-api-key",
    "x-auth-token",
}

IGNORED_MODEL_AUTH_FIELDS: Set[str] = {
    "authorization",
    "user_approved",
    "dual_key_confirmed",
    "confirmed",
    "_dual_key_token",
    "allow_uncontained",
    "_allow_uncontained",
}


def resolve_and_validate_destination(
    url: str,
    allowed_schemes: Tuple[str, ...] = ("http", "https"),
    allow_local_mcp: bool = False,
    allow_localhost: bool = False,
    disallowed_ports: Optional[Collection[int]] = None,
) -> Tuple[str, urllib.parse.SplitResult, str]:
    """Parses, resolves, and validates destination URL against SSRF, non-global IPs, and DNS rebinding.

    Performs fail-closed DNS pre-resolution, validates all returned candidate IP addresses against
    loopback, link-local, private RFC 1918, CGNAT, multicast, reserved, and non-global ranges,
    and returns the validated clean URL, SplitResult, and selected pinned IP literal.

    Returns:
        (clean_url, parsed_result, pinned_ip)
    Raises:
        SecurityBoundaryViolation: If the destination is invalid, forbidden, or unreachable.
    """
    if not url or not isinstance(url, str):
        raise SecurityBoundaryViolation("Empty or invalid URL provided.")

    clean_url = url.strip()
    # Reject CRLF or null byte injection
    if any(c in clean_url for c in ("\r", "\n", "\x00")):
        raise SecurityBoundaryViolation("URL contains illegal CRLF or null byte characters.")

    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except Exception as err:
        raise SecurityBoundaryViolation(f"Malformed URL: {err}") from err

    # Reject URLs with embedded user-info credentials (RFC 3986 section 3.2.1)
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise SecurityBoundaryViolation("URLs with embedded user-info credentials are prohibited.")

    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise SecurityBoundaryViolation(
            f"Blocked URL scheme '{scheme}'. Permitted schemes: {allowed_schemes}"
        )

    try:
        hostname = parsed.hostname
    except Exception:
        raise SecurityBoundaryViolation("Malformed hostname in URL.")

    if not hostname:
        raise SecurityBoundaryViolation("Egress URL must contain a valid hostname.")

    hostname_lower = hostname.lower()

    # Determine whether destination hostname is an explicitly configured local destination
    # (exact 'localhost' or IPv4/IPv6 loopback literal)
    is_explicit_local = False
    try:
        ip_parsed = ipaddress.ip_address(hostname)
        if isinstance(ip_parsed, ipaddress.IPv6Address) and getattr(ip_parsed, "ipv4_mapped", None):
            ip_parsed = ip_parsed.ipv4_mapped
        if ip_parsed.is_loopback:
            is_explicit_local = True
    except ValueError:
        if hostname_lower == "localhost":
            is_explicit_local = True

    # Loopback permission is strictly restricted to explicitly configured local destinations.
    # Arbitrary remote hostnames resolving to loopback via DNS must never receive loopback permission.
    permit_loopback = (allow_local_mcp or allow_localhost) and is_explicit_local
    if not permit_loopback:
        if (
            hostname_lower == "localhost"
            or hostname_lower.endswith(".localhost")
            or hostname_lower in ("127.0.0.1", "::1", "0.0.0.0")
            or is_explicit_local
        ):
            raise SecurityBoundaryViolation(f"Egress to localhost '{hostname}' is blocked.")

    try:
        port = parsed.port or (443 if scheme in ("https", "wss") else 80)
    except Exception:
        raise SecurityBoundaryViolation("Malformed port in URL.")

    if disallowed_ports and port in disallowed_ports:
        raise SecurityBoundaryViolation(
            f"Egress to port {port} on host '{hostname}' is prohibited."
        )

    # DNS Resolution (Fail Closed)
    try:
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise SecurityBoundaryViolation(f"DNS resolution failed for host '{hostname}': {err}")
    except Exception as err:
        raise SecurityBoundaryViolation(f"Unexpected error resolving host '{hostname}': {err}")

    if not addr_info:
        raise SecurityBoundaryViolation(f"No IP addresses resolved for host '{hostname}'.")

    validated_ips: List[str] = []

    # Inspect all resolved IPs - every address answer must be valid and global (or permitted loopback)
    for entry in addr_info:
        raw_ip = entry[4][0]
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise SecurityBoundaryViolation(f"Resolved address '{raw_ip}' is not a valid IP.")

        # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
        if isinstance(ip_obj, ipaddress.IPv6Address) and getattr(ip_obj, "ipv4_mapped", None):
            ip_obj = ip_obj.ipv4_mapped

        # When an explicitly local destination is permitted (e.g. local MCP or dev CDP),
        # enforce destination integrity: every resolved address MUST be a loopback address.
        # Any non-loopback answer (whether private or public) is rejected and fails closed.
        if permit_loopback:
            if not ip_obj.is_loopback:
                raise SecurityBoundaryViolation(
                    f"Local destination '{hostname}' resolved to non-loopback address '{ip_obj}'."
                )
            validated_ips.append(str(ip_obj))
            continue

        if (
            not ip_obj.is_global
            or ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            raise SecurityBoundaryViolation(
                f"SSRF Protection: Host '{hostname}' resolved to non-global address '{ip_obj}'."
            )

        validated_ips.append(str(ip_obj))

    if not validated_ips:
        raise SecurityBoundaryViolation(f"No valid IP addresses available for host '{hostname}'.")

    # Select the first validated IP for socket-level connection pinning
    pinned_ip = validated_ips[0]

    return clean_url, parsed, pinned_ip


def validate_egress_url(
    url: str,
    allowed_schemes: Tuple[str, ...] = ("http", "https"),
    allow_local_mcp: bool = False,
    allow_localhost: bool = False,
    disallowed_ports: Optional[Collection[int]] = None,
) -> str:
    """Validates an egress URL against SSRF, DNS rebinding, and private network access.

    Performs fail-closed DNS pre-resolution and blocks all loopback, link-local,
    private RFC 1918, CGNAT, multicast, reserved, and non-global IP destinations.
    """
    clean_url, _, _ = resolve_and_validate_destination(
        url,
        allowed_schemes=allowed_schemes,
        allow_local_mcp=allow_local_mcp,
        allow_localhost=allow_localhost,
        disallowed_ports=disallowed_ports,
    )
    return clean_url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom HTTP redirect handler that validates every redirect destination hop.

    Uses request-scoped hop accounting and destination pinning to prevent SSRF,
    hop limit exhaustion across concurrent requests, and DNS rebinding on redirect hops.
    """

    def __init__(
        self,
        max_hops: int = 4,
        allowed_schemes: Tuple[str, ...] = ("http", "https"),
        disallowed_ports: Optional[Collection[int]] = None,
    ):
        super().__init__()
        self.max_hops = max_hops
        self.allowed_schemes = allowed_schemes
        self.disallowed_ports = disallowed_ports

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "_jarvis_hops", 0) + 1
        if hops > self.max_hops:
            raise SecurityBoundaryViolation(f"Redirect limit exceeded (maximum {self.max_hops} hops).")

        # Resolve relative redirects against request full_url
        resolved_url = urllib.parse.urljoin(req.full_url, newurl)

        # Disallow insecure redirect downgrade from HTTPS to HTTP
        old_parsed = urllib.parse.urlsplit(req.full_url)
        new_parsed = urllib.parse.urlsplit(resolved_url)
        if (old_parsed.scheme or "").lower() == "https" and (new_parsed.scheme or "").lower() == "http":
            raise SecurityBoundaryViolation(
                f"Insecure redirect downgrade from HTTPS to HTTP blocked: '{req.full_url}' -> '{resolved_url}'"
            )

        # Validate destination hop through SSRF filter (never authorize loopback/private for redirect hops)
        clean_url, parsed_dest, pinned_ip = resolve_and_validate_destination(
            resolved_url,
            allowed_schemes=self.allowed_schemes,
            allow_local_mcp=False,
            allow_localhost=False,
            disallowed_ports=self.disallowed_ports,
        )

        new_req = super().redirect_request(req, fp, code, msg, headers, clean_url)
        if new_req is None:
            return None

        # Cross-origin redirect: strip sensitive auth credentials
        origin_changed = (
            (old_parsed.hostname or "").lower() != (new_parsed.hostname or "").lower()
            or old_parsed.port != new_parsed.port
            or (old_parsed.scheme or "").lower() != (new_parsed.scheme or "").lower()
        )
        if origin_changed:
            if hasattr(new_req, "headers"):
                for header_key in list(new_req.headers.keys()):
                    if header_key.lower() in SENSITIVE_AUTH_HEADERS:
                        del new_req.headers[header_key]
            if hasattr(new_req, "unredirected_hdrs"):
                for header_key in list(new_req.unredirected_hdrs.keys()):
                    if header_key.lower() in SENSITIVE_AUTH_HEADERS:
                        del new_req.unredirected_hdrs[header_key]

        # Scope hops and pinned destination to the new request
        new_req._jarvis_hops = hops
        new_req._pinned_ip = pinned_ip

        return new_req


class NoProxyHandler(urllib.request.ProxyHandler):
    """Disables environment and system proxy auto-detection to prevent proxy SSRF bypass."""

    def __init__(self):
        super().__init__(proxies={})

    def default_open(self, req):
        return None


class PinnedIPHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects directly to a pre-validated IP to eliminate DNS rebinding."""

    def __init__(self, host, port=None, pinned_ip: Optional[str] = None, **kwargs):
        super().__init__(host, port=port, **kwargs)
        self.pinned_ip = pinned_ip

    def connect(self):
        dest_host = self.pinned_ip or self.host
        self.sock = self._create_connection(
            (dest_host, self.port), self.timeout, self.source_address
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        if self._tunnel_host:
            self._tunnel()


class PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pre-validated IP while preserving SNI and cert verification."""

    def __init__(self, host, port=None, pinned_ip: Optional[str] = None, **kwargs):
        super().__init__(host, port=port, **kwargs)
        self.pinned_ip = pinned_ip

    def connect(self):
        dest_host = self.pinned_ip or self.host
        self.sock = self._create_connection(
            (dest_host, self.port), self.timeout, self.source_address
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        if self._tunnel_host:
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host

        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTPHandler that resolves/validates destination and pins the socket to the pre-validated IP."""

    def __init__(
        self,
        allow_local_mcp: bool = False,
        allow_localhost: bool = False,
        disallowed_ports: Optional[Collection[int]] = None,
    ):
        super().__init__()
        self.allow_local_mcp = allow_local_mcp
        self.allow_localhost = allow_localhost
        self.disallowed_ports = disallowed_ports

    def http_open(self, req):
        pinned_ip = getattr(req, "_pinned_ip", None)
        if not pinned_ip:
            clean_url, _, pinned_ip = resolve_and_validate_destination(
                req.full_url,
                allowed_schemes=("http", "https"),
                allow_local_mcp=self.allow_local_mcp,
                allow_localhost=self.allow_localhost,
                disallowed_ports=self.disallowed_ports,
            )
            req._pinned_ip = pinned_ip

        def conn_factory(host, timeout=req.timeout, **kwargs):
            return PinnedIPHTTPConnection(host, port=None, pinned_ip=pinned_ip, timeout=timeout, **kwargs)

        return self.do_open(conn_factory, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler that resolves/validates destination and pins the socket to the pre-validated IP."""

    def __init__(
        self,
        allow_local_mcp: bool = False,
        allow_localhost: bool = False,
        context=None,
        disallowed_ports: Optional[Collection[int]] = None,
    ):
        super().__init__(context=context)
        self.allow_local_mcp = allow_local_mcp
        self.allow_localhost = allow_localhost
        self.disallowed_ports = disallowed_ports

    def https_open(self, req):
        pinned_ip = getattr(req, "_pinned_ip", None)
        if not pinned_ip:
            clean_url, _, pinned_ip = resolve_and_validate_destination(
                req.full_url,
                allowed_schemes=("http", "https"),
                allow_local_mcp=self.allow_local_mcp,
                allow_localhost=self.allow_localhost,
                disallowed_ports=self.disallowed_ports,
            )
            req._pinned_ip = pinned_ip

        ctx = self._context

        def conn_factory(host, timeout=req.timeout, **kwargs):
            conn_kwargs = dict(kwargs)
            if ctx is not None:
                conn_kwargs["context"] = ctx
            return PinnedIPHTTPSConnection(host, port=None, pinned_ip=pinned_ip, timeout=timeout, **conn_kwargs)

        return self.do_open(conn_factory, req)


def build_safe_url_opener(
    max_hops: int = 4,
    allow_local_mcp: bool = False,
    allow_localhost: bool = False,
    disallowed_ports: Optional[Collection[int]] = None,
) -> urllib.request.OpenerDirector:
    """Builds a hardened urllib opener director equipped with SafeRedirectHandler,
    NoProxyHandler, and destination-pinned HTTP/HTTPS handlers to eliminate SSRF and DNS rebinding.

    Explicitly excludes default urllib protocol handlers (FileHandler, FTPHandler, DataHandler)
    to prevent local file disclosure and non-HTTP protocol smuggling.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        NoProxyHandler(),
        SafeRedirectHandler(max_hops=max_hops, disallowed_ports=disallowed_ports),
        PinnedHTTPHandler(
            allow_local_mcp=allow_local_mcp,
            allow_localhost=allow_localhost,
            disallowed_ports=disallowed_ports,
        ),
        PinnedHTTPSHandler(
            allow_local_mcp=allow_local_mcp,
            allow_localhost=allow_localhost,
            disallowed_ports=disallowed_ports,
        ),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


def is_prohibited_network_or_device_path(path_str: str) -> bool:
    """Detects UNC network shares, device namespaces, and NT object paths before filesystem access.

    Guarantees zero outbound SMB or network activity by rejecting paths before calling
    os.path.realpath, Path.resolve, os.path.abspath, or os.stat.
    """
    if not path_str or not isinstance(path_str, str):
        return True
    s = path_str.strip().strip("\"'")
    if not s:
        return True
    # Two leading slashes (any mix of \ and /) indicate UNC network share or NT device namespace
    if len(s) >= 2 and s[0] in ("\\", "/") and s[1] in ("\\", "/"):
        return True
    # Check drive specification (e.g. \\server\share or //server/share)
    drive, _ = os.path.splitdrive(s)
    if drive.startswith(("\\\\", "//", "\\/", "/\\")):
        return True
    # Windows NT object manager namespace (e.g. \??\..., /??/...)
    if len(s) >= 3 and s[0] in ("\\", "/") and s[1:3] == "??" and (len(s) == 3 or s[3] in ("\\", "/")):
        return True
    # Explicit Windows NT namespaces / DOS devices / extended UNC prefixes
    s_upper = s.upper()
    if any(s_upper.startswith(prefix) for prefix in ("\\\\?\\", "//?/", "\\\\.\\", "//./", "\\??\\", "/??/")):
        return True
    # Prohibit URL schemes masquerading as paths (e.g. smb://, file://, cifs://)
    if any(s_upper.startswith(prefix) for prefix in ("SMB:", "FILE:", "HTTP:", "HTTPS:", "FTP:", "CIFS:", "NFS:", "DAV:")):
        return True
    if re.match(r"^[A-Z][A-Z0-9+.-]*://", s_upper):
        return True
    return False


def validate_confined_path(target_path: str, allowed_root_dir: str) -> str:
    """Resolves symlinks, NTFS junctions, and path traversals, ensuring target stays inside root.

    Blocks UNC network paths, Windows device namespaces, and Windows reserved device filenames (CON, PRN, AUX, NUL, COM1..9, LPT1..9).
    Fails closed before any filesystem call on network paths to prevent outbound SMB negotiation.
    """
    if not target_path or not allowed_root_dir:
        raise SecurityBoundaryViolation("Target path and allowed root directory must be specified.")

    clean_target = str(target_path).strip()
    clean_root = str(allowed_root_dir).strip()

    # 1. Reject UNC network shares, NT device namespaces, and URI schemes before any filesystem operations
    if is_prohibited_network_or_device_path(clean_target):
        raise SecurityBoundaryViolation(
            f"Access to UNC network paths or Windows device namespaces is prohibited: '{clean_target}'"
        )
    if is_prohibited_network_or_device_path(clean_root):
        raise SecurityBoundaryViolation(
            f"Root directory cannot be a UNC network path or device namespace: '{clean_root}'"
        )

    # 2. Check for Windows reserved device names (including DOS devices with trailing colons)
    target_p = Path(clean_target)
    stem_upper = target_p.stem.upper().rstrip(":")
    name_upper = target_p.name.upper().rstrip(":")

    # Match exact stem or device prefix (e.g., NUL.txt, COM1.png, CON:)
    if stem_upper in WINDOWS_RESERVED_DEVICE_NAMES or name_upper in WINDOWS_RESERVED_DEVICE_NAMES:
        raise SecurityBoundaryViolation(
            f"Access to Windows reserved device filename '{target_p.name}' is prohibited."
        )

    # 3. Canonicalize real absolute paths resolving symlinks and junctions
    real_root = os.path.realpath(os.path.abspath(clean_root))
    real_target = os.path.realpath(os.path.abspath(clean_target))

    # 4. Check commonpath confinement
    try:
        common = os.path.commonpath([real_root, real_target])
    except ValueError as err:
        # Cross-drive paths on Windows raise ValueError
        raise SecurityBoundaryViolation(f"Path traversal across drives detected: {err}")

    if common != real_root:
        raise SecurityBoundaryViolation(
            f"Path traversal detected: target '{real_target}' escapes confined root '{real_root}'."
        )

    return real_target


def compute_action_hash(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash of a tool call's payload, ignoring forged model auth fields."""
    clean_args = {
        k: v for k, v in (arguments or {}).items()
        if k not in IGNORED_MODEL_AUTH_FIELDS
    }
    raw = json.dumps({"tool": tool_name, "args": clean_args}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_action_summary(tool_name: str, arguments: dict) -> str:
    """Generates an intuitive, readable summary of a proposed tool execution for human review."""
    clean_args = {
        k: v for k, v in (arguments or {}).items()
        if k not in IGNORED_MODEL_AUTH_FIELDS
    }
    if tool_name == "create_or_update_custom_skill":
        name = clean_args.get("skill_name", "unnamed_skill")
        code = clean_args.get("python_code", "")
        lines = len(code.splitlines()) if code else 0
        desc = clean_args.get("description", "")
        desc_str = f" - '{desc}'" if desc else ""
        return f"Autonomous Self-Building: Register/update skill '{name}' ({lines} lines of Python code){desc_str}"
    elif tool_name == "delete_custom_skill":
        name = clean_args.get("skill_name", "unnamed_skill")
        return f"Delete custom skill '{name}' from filesystem and live runtime memory"
    elif tool_name in ("execute_powershell", "execute_sandboxed_powershell"):
        cmd = clean_args.get("command", "")
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        return f"Execute PowerShell shell command: {cmd[:120]}"
    elif tool_name == "kill_process":
        return f"Terminate process PID={clean_args.get('pid')} / Name={clean_args.get('process_name')}"
    elif tool_name == "delete_file":
        return f"Delete file from filesystem: {clean_args.get('path') or clean_args.get('file_path')}"
    else:
        import json
        arg_preview = json.dumps(clean_args, sort_keys=True)
        if len(arg_preview) > 120:
            arg_preview = arg_preview[:117] + "..."
        return f"Execute tool '{tool_name}' with arguments {arg_preview}"


@dataclass
class DualKeyAuthorization:
    """A cryptographically bound, one-time human authorization grant with authoritative manager lifecycle state."""
    tool_name: str
    action_hash: str
    token: str
    expires_at: float
    grant_id: str = ""
    confirmed: bool = True
    status: str = "pending"  # "pending", "gate_consumed", "active", "consumed", "invalidated", "expired"
    summary: str = ""

    def is_valid_for(self, tool_name: str, payload_hash: str) -> bool:
        return (
            self.confirmed
            and self.tool_name == tool_name
            and hmac.compare_digest(self.action_hash, payload_hash)
            and time.time() <= self.expires_at
            and self.status not in ("consumed", "invalidated", "expired")
        )


@dataclass(frozen=True)
class GateDecision:
    """Decision returned by TurnTaintTracker.evaluate_tool_execution."""
    allowed: bool
    grant: Optional[DualKeyAuthorization] = None

    def __bool__(self) -> bool:
        return bool(self.allowed)


class _ActiveAuthToken:
    """Thread-safe, single-use active authorization token propagating through ContextVar.
    Cryptographically derived from a validated, manager-issued DualKeyAuthorization grant.
    """
    __slots__ = ("grant_id", "tool_name", "action_hash", "grant_token", "expires_at", "_consumed", "_invalidated", "_disarmed", "_lock", "manager")

    def __init__(
        self,
        grant_id: str = "",
        tool_name: str = "",
        action_hash: str = "",
        grant_token: str = "",
        expires_at: float = 0.0,
        disarmed: bool = False,
        manager: Optional["DualKeyApprovalManager"] = None,
    ) -> None:
        self.grant_id = grant_id
        self.tool_name = tool_name
        self.action_hash = action_hash
        self.grant_token = grant_token
        self.expires_at = expires_at
        self._consumed = False
        self._invalidated = False
        self._disarmed = disarmed
        self._lock = threading.Lock()
        self.manager = manager

    def consume(self, tool_name: str, action_hash: str) -> bool:
        """Atomically checks and consumes the active authorization token.
        Returns True exactly once if tool_name and action_hash match, unexpired, and registered grant is active.
        """
        if self._disarmed:
            return False
        if self.tool_name != tool_name or not hmac.compare_digest(self.action_hash, action_hash):
            return False
        if time.time() > self.expires_at:
            return False
        with self._lock:
            if self._consumed or self._invalidated:
                return False
            if self.manager is not None:
                if not self.manager._mark_grant_consumed(self.grant_id, self.grant_token):
                    self._invalidated = True
                    return False
            self._consumed = True
            return True

    def invalidate(self) -> None:
        """Permanently disarms the token regardless of whether it was consumed."""
        with self._lock:
            self._invalidated = True
            if self.manager is not None and self.grant_id:
                self.manager._mark_grant_invalidated(self.grant_id, self.grant_token)

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    @property
    def is_valid(self) -> bool:
        with self._lock:
            return (not self._disarmed) and (not self._consumed) and (not self._invalidated) and (time.time() <= self.expires_at)


_DISARMED_TOKEN = _ActiveAuthToken(disarmed=True)

_active_auth_context: contextvars.ContextVar[Optional[_ActiveAuthToken]] = contextvars.ContextVar(
    "_active_auth_context", default=None
)


class DualKeyApprovalManager:
    """Central authority for human out-of-band authorization grants for high-privilege operations."""
    _instance: Optional["DualKeyApprovalManager"] = None
    _lock = threading.Lock()
    _local = threading.local()

    def __init__(self) -> None:
        self._grants: Dict[str, DualKeyAuthorization] = {}
        self._grants_by_id: Dict[str, DualKeyAuthorization] = {}
        self._approval_handler: Optional[Callable[[Dict[str, Any]], bool]] = None
        self._pending_requests: Set[threading.Event] = set()
        self._grants_by_event: Dict[threading.Event, DualKeyAuthorization] = {}
        self._cancelled_events: Set[threading.Event] = set()
        self._pre_grant_creation_hook: Optional[Callable[[], None]] = None
        self._post_grant_creation_hook: Optional[Callable[[DualKeyAuthorization], None]] = None

    @classmethod
    def get_instance(cls) -> "DualKeyApprovalManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _mark_grant_consumed(self, grant_id: str, grant_token: str) -> bool:
        """Authoritatively marks a grant as consumed. Returns True iff grant was active and token matches."""
        with self._lock:
            auth = self._grants_by_id.get(grant_id)
            if not auth:
                return False
            if not hmac.compare_digest(auth.token, grant_token):
                return False
            if auth.status != "active":
                return False
            auth.status = "consumed"
            return True

    def _mark_grant_invalidated(self, grant_id: str, grant_token: str) -> None:
        with self._lock:
            auth = self._grants_by_id.get(grant_id)
            if auth and hmac.compare_digest(auth.token, grant_token):
                if auth.status == "active":
                    auth.status = "invalidated"

    def set_approval_handler(self, handler: Optional[Callable[[Dict[str, Any]], bool]]) -> None:
        """Sets a trusted local UI or CLI approval handler to prompt human operators."""
        with self._lock:
            self._approval_handler = handler

    def grant_approval(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        ttl_seconds: float = 60.0,
        summary: str = "",
    ) -> DualKeyAuthorization:
        """Grants an out-of-band authorization bound to the exact tool and arguments."""
        try:
            ttl_val = float(ttl_seconds)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid TTL value: {ttl_seconds}. TTL must be a positive finite number.")

        if not math.isfinite(ttl_val) or ttl_val <= 0:
            raise ValueError(f"Invalid TTL value: {ttl_seconds}. TTL must be a positive finite number.")

        ttl_seconds = min(max(ttl_val, 0.05), 300.0)
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in IGNORED_MODEL_AUTH_FIELDS
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        grant_id = secrets.token_hex(16)
        token = secrets.token_hex(16)
        if not summary:
            summary = format_action_summary(tool_name, clean_args)
        auth = DualKeyAuthorization(
            grant_id=grant_id,
            tool_name=tool_name,
            action_hash=action_hash,
            token=token,
            expires_at=time.time() + ttl_seconds,
            confirmed=True,
            status="pending",
            summary=summary,
        )
        with self._lock:
            self._grants[action_hash] = auth
            self._grants_by_id[grant_id] = auth
        # Track pending grant in calling thread-local state for direct synchronous execution
        self._local.pending_grant = auth
        logger.info("Dual-Key authorization granted for tool '%s' (id: %s, hash: %s, ttl: %.0fs)", tool_name, grant_id[:8], action_hash[:12], ttl_seconds)
        return auth

    def has_valid_grant(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Checks if a valid, unexpired grant exists without consuming it."""
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in IGNORED_MODEL_AUTH_FIELDS
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        with self._lock:
            auth = self._grants.get(action_hash)
            return bool(auth and auth.is_valid_for(tool_name, action_hash) and auth.status == "pending")

    def consume_grant_object(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[DualKeyAuthorization]:
        """Validates and consumes an existing pre-grant for the given tool and arguments (single-use),
        returning the DualKeyAuthorization if valid.
        """
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in IGNORED_MODEL_AUTH_FIELDS
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        with self._lock:
            auth = self._grants.get(action_hash)
            if auth and auth.is_valid_for(tool_name, action_hash) and auth.status == "pending":
                auth.status = "gate_consumed"
                del self._grants[action_hash]
                logger.info("Dual-Key authorization consumed at gate for tool '%s' (id: %s, hash: %s)", tool_name, auth.grant_id[:8], action_hash[:12])
                return auth
            # Clean up stale/expired grant if present
            if auth and (time.time() > auth.expires_at or auth.status != "pending"):
                del self._grants[action_hash]
        return None

    def consume_grant(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Validates and consumes an existing pre-grant for the given tool and arguments (single-use)."""
        return self.consume_grant_object(tool_name, arguments) is not None

    def revoke_grant(self, grant: Optional[DualKeyAuthorization]) -> None:
        """Immediately revokes and permanently invalidates a grant so it cannot be bound or consumed."""
        if not grant or not isinstance(grant, DualKeyAuthorization):
            return
        with self._lock:
            auth = self._grants_by_id.get(grant.grant_id)
            if auth:
                auth.status = "invalidated"
                self._grants.pop(auth.action_hash, None)
                self._grants_by_id.pop(auth.grant_id, None)

    def cancel_approval_request(self, cancel_event: Optional[threading.Event]) -> None:
        """Atomically cancels an approval request and revokes any associated grant."""
        if cancel_event is None:
            return
        cancel_event.set()
        with self._lock:
            self._cancelled_events.add(cancel_event)
            auth = self._grants_by_event.pop(cancel_event, None)
            if auth:
                auth.status = "invalidated"
                self._grants_by_id.pop(auth.grant_id, None)
                self._grants.pop(auth.action_hash, None)
                logger.warning(
                    "Revoked grant '%s' for action '%s' due to request cancellation.",
                    auth.grant_id[:8],
                    auth.tool_name,
                )

    def cleanup_approval_request(self, cancel_event: Optional[threading.Event]) -> None:
        """Cleans up internal tracking references for a completed or cancelled approval request."""
        if cancel_event is None:
            return
        with self._lock:
            self._grants_by_event.pop(cancel_event, None)
            self._cancelled_events.discard(cancel_event)
            self._pending_requests.discard(cancel_event)

    def set_pre_grant_creation_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """Sets a synchronization hook executed immediately before registering an approved grant."""
        with self._lock:
            self._pre_grant_creation_hook = hook

    def set_post_grant_creation_hook(self, hook: Optional[Callable[[DualKeyAuthorization], None]]) -> None:
        """Sets a synchronization hook executed immediately after registering an approved grant."""
        with self._lock:
            self._post_grant_creation_hook = hook

    def get_pending_request_count(self) -> int:
        """Returns the number of currently pending human approval requests."""
        with self._lock:
            return len(self._pending_requests)

    def is_request_pending(self, cancel_event: threading.Event) -> bool:
        """Returns True if the specified cancel_event is currently tracking a pending approval request."""
        with self._lock:
            return cancel_event in self._pending_requests

    def request_human_approval(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        summary: str = "",
        risk_level: int = 2,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[DualKeyAuthorization]:
        """Presents the proposed action and readable summary to the user via registered trusted local handler.
        If approved, returns a validated DualKeyAuthorization grant.
        """
        with self._lock:
            handler = self._approval_handler
            if cancel_event is not None:
                if cancel_event.is_set() or cancel_event in self._cancelled_events:
                    logger.warning("Human approval request for '%s' aborted before prompt (cancelled).", tool_name)
                    return None
                self._pending_requests.add(cancel_event)

        if not handler:
            if cancel_event is not None:
                with self._lock:
                    self._pending_requests.discard(cancel_event)
            return None

        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in IGNORED_MODEL_AUTH_FIELDS
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        if not summary:
            summary = format_action_summary(tool_name, clean_args)

        request_details = {
            "tool_name": tool_name,
            "arguments": clean_args,
            "action_hash": action_hash,
            "summary": summary,
            "risk_level": risk_level,
            "timestamp": time.time(),
            "cancel_event": cancel_event,
        }

        try:
            approved = handler(request_details)

            with self._lock:
                if cancel_event is not None and (cancel_event.is_set() or cancel_event in self._cancelled_events):
                    logger.warning("Approval request for action '%s' was cancelled; discarding approval result.", tool_name)
                    return None
                pre_hook = self._pre_grant_creation_hook

            if pre_hook is not None:
                try:
                    pre_hook()
                except Exception as hook_err:
                    logger.debug("Pre-grant hook error: %s", hook_err)

            if approved:
                with self._lock:
                    if cancel_event is not None and (cancel_event.is_set() or cancel_event in self._cancelled_events):
                        logger.warning(
                            "Approval request for action '%s' was cancelled at grant registration boundary; discarding.",
                            tool_name,
                        )
                        return None
                    grant_id = secrets.token_hex(16)
                    token = secrets.token_hex(16)
                    auth = DualKeyAuthorization(
                        grant_id=grant_id,
                        tool_name=tool_name,
                        action_hash=action_hash,
                        token=token,
                        expires_at=time.time() + 60.0,
                        confirmed=True,
                        status="gate_consumed",
                        summary=summary,
                    )
                    self._grants_by_id[grant_id] = auth
                    if cancel_event is not None:
                        self._grants_by_event[cancel_event] = auth
                    post_hook = self._post_grant_creation_hook

                if post_hook is not None:
                    try:
                        post_hook(auth)
                    except Exception as hook_err:
                        logger.debug("Post-grant hook error: %s", hook_err)

                logger.info("Human operator approved action '%s' (id: %s, hash: %s, token: %s)", tool_name, grant_id[:8], action_hash[:12], token[:8])
                return auth
            else:
                logger.warning("Human operator denied action '%s' (hash: %s)", tool_name, action_hash[:12])
                return None
        except Exception as e:
            logger.error("Error during human approval request for '%s': %s", tool_name, e)
            return None
        finally:
            if cancel_event is not None:
                with self._lock:
                    self._pending_requests.discard(cancel_event)

    def bind_active_grant(self, grant: DualKeyAuthorization) -> contextvars.Token:
        """Binds a validated, manager-issued DualKeyAuthorization to the current context and thread.
        Fails closed with PermissionError if grant is fabricated, replayed, expired, or invalid.
        """
        if not isinstance(grant, DualKeyAuthorization):
            raise PermissionError("Provided grant must be a genuine DualKeyAuthorization instance.")

        with self._lock:
            registered = self._grants_by_id.get(grant.grant_id)
            if registered is None:
                raise PermissionError(f"Rejected unverified or fabricated grant (grant_id={grant.grant_id}).")
            if not hmac.compare_digest(registered.token, grant.token):
                raise PermissionError("Rejected grant: cryptographic token mismatch.")
            if registered.tool_name != grant.tool_name or not hmac.compare_digest(registered.action_hash, grant.action_hash):
                raise PermissionError("Rejected grant: tool name or action hash mismatch.")
            if time.time() > registered.expires_at:
                registered.status = "expired"
                raise PermissionError(f"Rejected grant (grant_id={grant.grant_id}): authorization has expired.")
            if registered.status not in ("gate_consumed", "pending"):
                raise PermissionError(
                    f"Grant (grant_id={grant.grant_id}) cannot be bound: status is '{registered.status}' "
                    "(must be 'gate_consumed' or 'pending'). Replay or re-binding of consumed/active grants is prohibited."
                )
            registered.status = "active"
            self._grants.pop(registered.action_hash, None)

        token = _ActiveAuthToken(
            grant_id=registered.grant_id,
            tool_name=registered.tool_name,
            action_hash=registered.action_hash,
            grant_token=registered.token,
            expires_at=registered.expires_at,
            manager=self,
        )
        ctx_token = _active_auth_context.set(token)
        self._local.authorized_token = token
        return ctx_token

    def bind_disarmed_context(self) -> contextvars.Token:
        """Binds an explicit disarmed authorization scope to prevent nested context inheritance or scavenging."""
        ctx_token = _active_auth_context.set(_DISARMED_TOKEN)
        self._local.authorized_token = _DISARMED_TOKEN
        return ctx_token

    def set_active_authorized(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        grant: Optional[DualKeyAuthorization] = None,
    ) -> contextvars.Token:
        """Binds an active authorization derived ONLY from a genuine, registered DualKeyAuthorization grant.
        If grant is not provided, consumes an existing matching pending grant from _grants.
        Fails closed with PermissionError if no genuine grant exists or if grant is forged/replayed.
        """
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in IGNORED_MODEL_AUTH_FIELDS
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        if grant is not None:
            if not grant.is_valid_for(tool_name, action_hash):
                raise PermissionError(f"Provided grant does not authorize tool '{tool_name}' with given arguments.")
            valid_grant = grant
        else:
            valid_grant = self.consume_grant_object(tool_name, clean_args)
            if valid_grant is None:
                raise PermissionError(
                    f"Cannot establish active authorization for tool '{tool_name}': "
                    "no valid human authorization grant found."
                )
        return self.bind_active_grant(valid_grant)

    def set_thread_authorized(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        grant: Optional[DualKeyAuthorization] = None,
    ) -> contextvars.Token:
        """Binds a verified single-use authorization to the current thread and async context for tool execution."""
        return self.set_active_authorized(tool_name, arguments, grant=grant)

    def clear_active_authorized(self, ctx_token: Optional[contextvars.Token] = None) -> None:
        """Clears and permanently invalidates the active authorization context.
        Restores previous ContextVar binding if ctx_token is provided.
        """
        token = _active_auth_context.get()
        if token is not None and token is not _DISARMED_TOKEN:
            token.invalidate()
        self._local.pending_grant = None
        if ctx_token is not None:
            try:
                _active_auth_context.reset(ctx_token)
            except (ValueError, RuntimeError):
                _active_auth_context.set(None)
        else:
            _active_auth_context.set(None)
        restored = _active_auth_context.get()
        self._local.authorized_token = restored

    def clear_thread_authorized(self, ctx_token: Optional[contextvars.Token] = None) -> None:
        """Clears the active authorization context (alias for clear_active_authorized)."""
        self.clear_active_authorized(ctx_token)

    def verify_and_consume_active_context(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Verifies if the current context holds an active, valid human authorization.
        Consumes the context immediately (single-use).
        Fails closed without searching or scavenging pending grants.
        """
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in IGNORED_MODEL_AUTH_FIELDS
        }
        action_hash = compute_action_hash(tool_name, clean_args)

        # 1. Check ContextVar execution context (propagated across asyncio tasks and threads)
        token = _active_auth_context.get()
        if token is not None:
            if token.consume(tool_name, action_hash):
                self._local.authorized_token = None
                return True
            # Token exists in context (or is disarmed); if it did not match or was already consumed, fail closed
            return False

        # 2. Check thread-local execution context (for legacy threads without ContextVar propagation)
        local_token = getattr(self._local, "authorized_token", None)
        if local_token is not None:
            if local_token.consume(tool_name, action_hash):
                self._local.authorized_token = None
                return True
            return False

        # 3. Direct synchronous execution check for thread that directly called grant_approval
        pending = getattr(self._local, "pending_grant", None)
        if pending is not None:
            if pending.is_valid_for(tool_name, action_hash) and pending.status == "pending":
                self._local.pending_grant = None
                with self._lock:
                    pending.status = "consumed"
                    self._grants.pop(action_hash, None)
                return True
            self._local.pending_grant = None
            return False

        # 4. Fail closed. No fallback to _grants or arbitrary thread-local action tuples.
        return False

    @staticmethod
    def run_with_context(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Helper to run a callable in a copied contextvars Context for raw ThreadPoolExecutor/thread propagation."""
        ctx = contextvars.copy_context()
        return ctx.run(func, *args, **kwargs)

    def check_and_consume(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        risk_level: int = 2,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[DualKeyAuthorization]:
        """Single-entry check: consumes an existing grant if present, or requests human approval via handler."""
        grant = self.consume_grant_object(tool_name, arguments)
        if grant is not None:
            if cancel_event is not None:
                with self._lock:
                    if cancel_event.is_set() or cancel_event in self._cancelled_events:
                        grant.status = "invalidated"
                        self._grants_by_id.pop(grant.grant_id, None)
                        return None
                    self._grants_by_event[cancel_event] = grant
            return grant
        summary = format_action_summary(tool_name, arguments)
        return self.request_human_approval(
            tool_name, arguments, summary=summary, risk_level=risk_level, cancel_event=cancel_event
        )

    def reset(self) -> None:
        """Clears all pending and active authorization grants."""
        with self._lock:
            self._grants.clear()
            self._grants_by_id.clear()
            self._pending_requests.clear()
            self._grants_by_event.clear()
            self._cancelled_events.clear()
            self._pre_grant_creation_hook = None
            self._post_grant_creation_hook = None
        self.clear_active_authorized()


@dataclass(frozen=True)
class IngestionRecord:
    """Immutable audit record documenting untrusted external-data ingestion provenance."""
    source: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class _TurnTaintState:
    """Thread-safe turn-scoped taint state holder enforcing monotonic taint within a logical turn."""
    __slots__ = ("_is_tainted", "_tainting_sources", "_provenance_records", "_lock", "turn_id")

    MAX_SOURCES_PER_TURN: int = 128
    MAX_SOURCE_LENGTH: int = 128

    def __init__(self, turn_id: Optional[str] = None) -> None:
        self.turn_id = turn_id or secrets.token_hex(8)
        self._is_tainted: bool = False
        self._tainting_sources: List[str] = []
        self._provenance_records: List[IngestionRecord] = []
        self._lock = threading.Lock()

    @property
    def is_tainted(self) -> bool:
        with self._lock:
            return self._is_tainted

    @property
    def tainting_sources(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._tainting_sources)

    @property
    def provenance_records(self) -> Tuple[IngestionRecord, ...]:
        with self._lock:
            # Return defensive copy of records with copied metadata dicts
            return tuple(
                IngestionRecord(
                    source=r.source,
                    timestamp=r.timestamp,
                    metadata=dict(r.metadata),
                )
                for r in self._provenance_records
            )

    @classmethod
    def _sanitize_source(cls, source: Any) -> str:
        if source is None:
            clean = ""
        elif isinstance(source, str):
            clean = source
        else:
            try:
                clean = str(source)
            except Exception:
                clean = ""
        clean = clean.strip()
        clean = re.sub(r"[\r\n\x00-\x1f]", "", clean)
        if len(clean) > cls.MAX_SOURCE_LENGTH:
            clean = clean[:cls.MAX_SOURCE_LENGTH]
        return clean or "unknown_external_source"

    @classmethod
    def _sanitize_metadata(cls, meta: Optional[Dict[str, Any]], max_depth: int = 3) -> Dict[str, Any]:
        if not meta or not isinstance(meta, dict) or max_depth < 0:
            return {}
        safe: Dict[str, Any] = {}
        # Sensitive field key patterns (normalized by stripping non-alphanumeric characters)
        sensitive_normalized_tokens = {
            "token", "password", "passwd", "secret", "auth", "authorization",
            "apikey", "privatekey", "session", "credential", "cookie",
            "bearer", "cert", "certificate", "signature",
        }
        for k, v in meta.items():
            k_str = str(k).lower().strip()
            k_norm = re.sub(r"[^a-z0-9]", "", k_str)
            if any(s in k_norm for s in sensitive_normalized_tokens):
                continue
            if isinstance(v, (int, float, bool)):
                safe[k_str] = v
            elif isinstance(v, str):
                safe[k_str] = v[:64]
            elif isinstance(v, dict) and max_depth > 0:
                nested_safe = cls._sanitize_metadata(v, max_depth=max_depth - 1)
                if nested_safe:
                    safe[k_str] = nested_safe
        return safe

    def mark_tainted(self, source: Any, metadata: Optional[Dict[str, Any]] = None) -> None:
        clean_source = self._sanitize_source(source)
        clean_meta = self._sanitize_metadata(metadata)
        with self._lock:
            self._is_tainted = True
            if clean_source not in self._tainting_sources:
                if len(self._tainting_sources) < self.MAX_SOURCES_PER_TURN:
                    self._tainting_sources.append(clean_source)

            # Record provenance for each distinct ingestion event up to MAX_SOURCES_PER_TURN
            if len(self._provenance_records) < self.MAX_SOURCES_PER_TURN:
                if (
                    not self._provenance_records
                    or self._provenance_records[-1].source != clean_source
                    or self._provenance_records[-1].metadata != clean_meta
                ):
                    self._provenance_records.append(
                        IngestionRecord(source=clean_source, timestamp=time.time(), metadata=clean_meta)
                    )


_turn_taint_context: contextvars.ContextVar[Optional[_TurnTaintState]] = contextvars.ContextVar(
    "turn_taint_context", default=None
)


class TurnTaintTracker:
    """Tracks untrusted external context within a conversational turn and enforces Dual-Key safety.

    Enforces logical-turn scoping, monotonic taint inheritance across child tasks and workers,
    fail-closed security gating on unseeded contexts, and isolation between independent turns.
    """

    _instance: Optional["TurnTaintTracker"] = None
    _lock = threading.Lock()

    UNTRUSTED_INGESTION_TOOLS: Set[str] = {
        "web_search",
        "deep_research",
        "browser_navigate",
        "browser_get_dom",
        "read_webpage_content",
        "read_webpage_content_cdp",
        "browse_and_read_webpage",
        "get_tech_news_headlines",
        "get_weather",
        "get_wikipedia_summary",
        "check_unread_emails",
        "search_emails",
        "read_email_details",
        "execute_browser_javascript",
        "audit_webpage_accessibility",
        "audit_webpage_performance",
        "capture_devtools_snapshot",
        "get_daily_briefing",
        "search_modern_web_guidance",
        "retrieve_modern_web_guide",
        "search_gemini_api_documentation",
        "get_gemini_documentation_chunk",
        "scrape_and_archive_webpage",
        "mcp_call_tool",
        "fetch_url",
        "inspect_screen",
        "see_screen",
        "inspect_screen_with_marks",
        "take_desktop_screenshot",
        "read_clipboard",
        "save_clipboard_photo",
        "process_staged_document",
        "capture_webpage_screenshot",
    }

    HIGH_PRIVILEGE_MUTATION_TOOLS: Set[str] = {
        "execute_powershell",
        "execute_sandboxed_powershell",
        "run_job_sandboxed_process",
        "run_background_task",
        "release_file_lock",
        "delete_file",
        "kill_process",
        "close_application",
        "launch_application",
        "launch_or_focus_application",
        "write_file",
        "patch_file",
        "create_or_update_custom_skill",
        "delete_custom_skill",
    }

    CRITICAL_PRIVILEGE_TOOLS: Set[str] = {
        "create_or_update_custom_skill",
        "delete_custom_skill",
        "execute_powershell",
        "run_background_task",
    }

    TRUSTED_DOCUMENT_PROVENANCES: Set[str] = {"trusted_internal", "system"}
    KNOWN_DOCUMENT_PROVENANCES: Set[str] = {
        "trusted_internal",
        "system",
        "web_scrape",
        "staged_upload",
        "external",
        "unknown",
    }

    def __init__(self) -> None:
        self._dynamic_ingestion_tools: Set[str] = set()

    @classmethod
    def normalize_document_provenance(cls, provenance: Any) -> str:
        """Validates and normalizes document provenance labels according to policy.
        Missing, empty, non-string, or unrecognized values fail closed to 'unknown'."""
        if not provenance or not isinstance(provenance, str):
            return "unknown"
        clean = provenance.strip().lower()
        if clean in cls.KNOWN_DOCUMENT_PROVENANCES:
            return clean
        return "unknown"

    @classmethod
    def is_untrusted_document_provenance(cls, provenance: Any) -> bool:
        """Determines if a document provenance classification is untrusted.
        Fails closed: Any provenance other than explicitly trusted internal/system labels is untrusted."""
        if not provenance or not isinstance(provenance, str):
            return True
        clean = provenance.strip().lower()
        return clean not in cls.TRUSTED_DOCUMENT_PROVENANCES

    @staticmethod
    def _get_context_state(create_if_missing: bool = True) -> Optional[_TurnTaintState]:
        state = _turn_taint_context.get()
        if state is None and create_if_missing:
            state = _TurnTaintState()
            _turn_taint_context.set(state)
        return state

    @property
    def is_tainted(self) -> bool:
        state = _turn_taint_context.get()
        return state.is_tainted if state is not None else False

    @is_tainted.setter
    def is_tainted(self, value: bool) -> None:
        state = self._get_context_state(create_if_missing=True)
        if not value and state.is_tainted:
            logger.warning("Attempt to clear established taint within active turn ignored (taint is monotonic).")
            return
        if value:
            state.mark_tainted("manual_override")

    @property
    def tainting_sources(self) -> Tuple[str, ...]:
        state = _turn_taint_context.get()
        return state.tainting_sources if state is not None else ()

    @property
    def _tainting_sources(self) -> List[str]:
        state = _turn_taint_context.get()
        return list(state.tainting_sources) if state is not None else []

    @_tainting_sources.setter
    def _tainting_sources(self, value: List[str]) -> None:
        state = self._get_context_state(create_if_missing=True)
        if state.is_tainted and not value:
            logger.warning("Attempt to clear tainting sources within active turn ignored (taint is monotonic).")
            return
        for src in value:
            state.mark_tainted(src)

    @property
    def provenance_records(self) -> Tuple[IngestionRecord, ...]:
        state = _turn_taint_context.get()
        return state.provenance_records if state is not None else ()

    def get_provenance_records(self) -> Tuple[IngestionRecord, ...]:
        return self.provenance_records

    @classmethod
    def get_instance(cls) -> "TurnTaintTracker":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def is_untrusted_ingestion(self, tool_name: str) -> bool:
        """Determines if a tool represents an untrusted external data ingestion boundary."""
        if not tool_name:
            return False
        clean_name = tool_name.strip()
        if clean_name.startswith("mcp_") or clean_name == "mcp_call_tool":
            return True
        if clean_name in self.UNTRUSTED_INGESTION_TOOLS:
            return True
        with self._lock:
            if clean_name in self._dynamic_ingestion_tools:
                return True
        try:
            from jarvis.skills.base import SkillRegistry
            if SkillRegistry.is_ingestion_tool(clean_name):
                return True
        except Exception:
            pass
        return False

    def register_ingestion_tool(self, tool_name: Any) -> None:
        """Registers a dynamic tool name as an untrusted external ingestion source."""
        clean = _TurnTaintState._sanitize_source(tool_name)
        if clean and clean != "unknown_external_source":
            with self._lock:
                self._dynamic_ingestion_tools.add(clean)

    def mark_tainted(self, source: Any, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Marks the current turn context as tainted by untrusted external data with provenance."""
        state = self._get_context_state(create_if_missing=True)
        state.mark_tainted(source, metadata=metadata)
        logger.warning("Turn marked as TAINTED by untrusted source: %s", source)

    def record_ingestion(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> None:
        """Records provenance of external untrusted data ingestion into the active turn."""
        meta: Dict[str, Any] = {"tool": tool_name}
        if arguments and isinstance(arguments, dict):
            url_val = arguments.get("url")
            if not url_val and "tab_name_or_url" in arguments:
                cand = arguments["tab_name_or_url"]
                if isinstance(cand, str) and (cand.startswith("http://") or cand.startswith("https://")):
                    url_val = cand
            if url_val and isinstance(url_val, str):
                try:
                    parsed = urllib.parse.urlsplit(url_val)
                    # Use parsed.hostname to strictly exclude username, password, and port
                    host = parsed.hostname
                    if not host and parsed.netloc:
                        netloc_clean = parsed.netloc.split("@")[-1]
                        host = netloc_clean.split(":")[0]
                    if host:
                        meta["host"] = host[:64]
                except Exception:
                    pass
            if "query" in arguments and isinstance(arguments["query"], str):
                meta["query_length"] = len(arguments["query"])
            if "topic" in arguments and isinstance(arguments["topic"], str):
                meta["topic_length"] = len(arguments["topic"])
            if "city" in arguments and isinstance(arguments["city"], str):
                meta["city"] = arguments["city"][:64]
            if "identifier" in arguments and isinstance(arguments["identifier"], str):
                meta["identifier"] = arguments["identifier"][:64]
            if "focus" in arguments and isinstance(arguments["focus"], str):
                meta["focus_length"] = len(arguments["focus"])
            if "filename" in arguments and isinstance(arguments["filename"], str):
                meta["filename"] = arguments["filename"][:64]
        self.mark_tainted(tool_name, metadata=meta)

    def reset_turn(self) -> None:
        """Resets the taint tracking state for the current conversational turn context."""
        _turn_taint_context.set(_TurnTaintState())

    def evaluate_tool_execution(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        risk_level: int = 0,
        dual_key_confirmed: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> GateDecision:
        """Evaluates whether tool execution is permitted and returns a GateDecision.
        If a genuine human authorization grant was consumed (or pre-granted), the grant is returned.
        Ordinary/untainted tool executions return GateDecision(allowed=True, grant=None).
        """
        # 1. Untrusted ingestion tools or dynamic MCP tools mark the turn tainted immediately
        if self.is_untrusted_ingestion(tool_name):
            self.record_ingestion(tool_name, arguments)
            return GateDecision(allowed=True, grant=None)

        is_critical = (risk_level >= 2) or (tool_name in self.CRITICAL_PRIVILEGE_TOOLS)
        is_privileged_mutation = (risk_level >= 1) or (tool_name in self.HIGH_PRIVILEGE_MUTATION_TOOLS)

        state = _turn_taint_context.get()
        # SEC-H2-02: An uninitialized context has no provenance; treat as tainted for privileged mutations
        is_tainted = (state.is_tainted) if state is not None else is_privileged_mutation
        is_tainted_mutation = is_tainted and is_privileged_mutation

        approval_mgr = DualKeyApprovalManager.get_instance()

        if not (is_critical or is_tainted_mutation):
            # Consume any pre-existing grant if one was explicitly granted for this action
            consumed_grant = approval_mgr.consume_grant_object(tool_name, arguments)
            return GateDecision(allowed=True, grant=consumed_grant)

        # Validate and consume authorization at the execution boundary
        human_grant = approval_mgr.check_and_consume(
            tool_name, arguments, risk_level=risk_level, cancel_event=cancel_event
        )

        # 1. Unconditional authorization check for Critical Risk (Level 2) operations
        if is_critical and not human_grant:
            raise SecurityBoundaryViolation(
                f"Critical Authorization Gate: Tool '{tool_name}' (risk_level={risk_level}) "
                "modifies the host environment or live Python runtime and requires explicit, "
                "out-of-band user confirmation regardless of turn taint state."
            )

        # 2. Tainted turn check for High Privilege (Level 1) operations
        if is_tainted_mutation and not human_grant:
            if state is None:
                raise SecurityBoundaryViolation(
                    f"Dual-Key Safety Gate: blocked high-privilege tool '{tool_name}' "
                    "execution in an uninitialized security context without turn provenance."
                )
            sources_str = ", ".join(state.tainting_sources) or "untrusted input"
            raise SecurityBoundaryViolation(
                f"Dual-Key Safety Gate: blocked high-privilege tool '{tool_name}' "
                f"execution in a turn tainted by untrusted external data "
                f"(sources: {sources_str})."
            )

        if human_grant is not None and getattr(human_grant, "status", None) != "gate_consumed":
            raise SecurityBoundaryViolation(
                f"Dual-Key Safety Gate: authorization grant for tool '{tool_name}' was invalidated."
            )

        return GateDecision(allowed=True, grant=human_grant)

    def check_tool_execution(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        risk_level: int = 0,
        dual_key_confirmed: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """Enforces mandatory out-of-band authorization on critical risk level 2 operations
        regardless of taint state, and enforces Dual-Key safety on high-privilege mutations
        when the turn has been tainted by external untrusted data or when security context is missing.

        Validates and consumes authorization at the execution boundary BEFORE tool execution.
        """
        return self.evaluate_tool_execution(
            tool_name,
            arguments,
            risk_level=risk_level,
            dual_key_confirmed=dual_key_confirmed,
            cancel_event=cancel_event,
        ).allowed
