"""Closed-Loop Security Boundaries, SSRF Protection, Path Confinement, and Taint Tracking for Jarvis 2.0."""
import hashlib
import hmac
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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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


def resolve_and_validate_destination(
    url: str,
    allowed_schemes: Tuple[str, ...] = ("http", "https"),
    allow_local_mcp: bool = False,
    allow_localhost: bool = False,
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

    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise SecurityBoundaryViolation(
            f"Blocked URL scheme '{scheme}'. Permitted schemes: {allowed_schemes}"
        )

    try:
        hostname = parsed.hostname
    except Exception as err:
        raise SecurityBoundaryViolation(f"Malformed hostname in URL: {err}") from err

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
        port = parsed.port or (443 if scheme == "https" else 80)
    except Exception as err:
        raise SecurityBoundaryViolation(f"Malformed port in URL: {err}") from err

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
    )
    return clean_url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom HTTP redirect handler that validates every redirect destination hop.

    Uses request-scoped hop accounting and destination pinning to prevent SSRF,
    hop limit exhaustion across concurrent requests, and DNS rebinding on redirect hops.
    """

    def __init__(self, max_hops: int = 4, allowed_schemes: Tuple[str, ...] = ("http", "https")):
        super().__init__()
        self.max_hops = max_hops
        self.allowed_schemes = allowed_schemes

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

    def __init__(self, allow_local_mcp: bool = False, allow_localhost: bool = False):
        super().__init__()
        self.allow_local_mcp = allow_local_mcp
        self.allow_localhost = allow_localhost

    def http_open(self, req):
        pinned_ip = getattr(req, "_pinned_ip", None)
        if not pinned_ip:
            clean_url, _, pinned_ip = resolve_and_validate_destination(
                req.full_url,
                allowed_schemes=("http", "https"),
                allow_local_mcp=self.allow_local_mcp,
                allow_localhost=self.allow_localhost,
            )
            req._pinned_ip = pinned_ip

        def conn_factory(host, timeout=req.timeout, **kwargs):
            return PinnedIPHTTPConnection(host, port=None, pinned_ip=pinned_ip, timeout=timeout, **kwargs)

        return self.do_open(conn_factory, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler that resolves/validates destination and pins the socket to the pre-validated IP."""

    def __init__(self, allow_local_mcp: bool = False, allow_localhost: bool = False, context=None):
        super().__init__(context=context)
        self.allow_local_mcp = allow_local_mcp
        self.allow_localhost = allow_localhost

    def https_open(self, req):
        pinned_ip = getattr(req, "_pinned_ip", None)
        if not pinned_ip:
            clean_url, _, pinned_ip = resolve_and_validate_destination(
                req.full_url,
                allowed_schemes=("http", "https"),
                allow_local_mcp=self.allow_local_mcp,
                allow_localhost=self.allow_localhost,
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
) -> urllib.request.OpenerDirector:
    """Builds a hardened urllib opener director equipped with SafeRedirectHandler,
    NoProxyHandler, and destination-pinned HTTP/HTTPS handlers to eliminate SSRF and DNS rebinding.

    Explicitly excludes default urllib protocol handlers (FileHandler, FTPHandler, DataHandler)
    to prevent local file disclosure and non-HTTP protocol smuggling.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        NoProxyHandler(),
        SafeRedirectHandler(max_hops=max_hops),
        PinnedHTTPHandler(allow_local_mcp=allow_local_mcp, allow_localhost=allow_localhost),
        PinnedHTTPSHandler(allow_local_mcp=allow_local_mcp, allow_localhost=allow_localhost),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


def validate_confined_path(target_path: str, allowed_root_dir: str) -> str:
    """Resolves symlinks, NTFS junctions, and path traversals, ensuring target stays inside root.

    Blocks Windows reserved device filenames (CON, PRN, AUX, NUL, COM1..9, LPT1..9).
    """
    if not target_path or not allowed_root_dir:
        raise SecurityBoundaryViolation("Target path and allowed root directory must be specified.")

    # 1. Check for Windows reserved device names
    target_p = Path(target_path)
    stem_upper = target_p.stem.upper()
    name_upper = target_p.name.upper()

    # Match exact stem or device prefix (e.g., NUL.txt, COM1.png)
    if stem_upper in WINDOWS_RESERVED_DEVICE_NAMES or name_upper in WINDOWS_RESERVED_DEVICE_NAMES:
        raise SecurityBoundaryViolation(
            f"Access to Windows reserved device filename '{target_p.name}' is prohibited."
        )

    # 2. Canonicalize real absolute paths resolving symlinks and junctions
    real_root = os.path.realpath(os.path.abspath(allowed_root_dir))
    real_target = os.path.realpath(os.path.abspath(target_path))

    # 3. Check commonpath confinement
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
        if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
    }
    raw = json.dumps({"tool": tool_name, "args": clean_args}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_action_summary(tool_name: str, arguments: dict) -> str:
    """Generates an intuitive, readable summary of a proposed tool execution for human review."""
    clean_args = {
        k: v for k, v in (arguments or {}).items()
        if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
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


@dataclass(frozen=True)
class DualKeyAuthorization:
    """A cryptographically bound, one-time human authorization grant."""
    tool_name: str
    action_hash: str
    token: str
    expires_at: float
    confirmed: bool = True
    summary: str = ""

    def is_valid_for(self, tool_name: str, payload_hash: str) -> bool:
        return (
            self.confirmed
            and self.tool_name == tool_name
            and hmac.compare_digest(self.action_hash, payload_hash)
            and time.time() <= self.expires_at
        )


class DualKeyApprovalManager:
    """Central authority for human out-of-band authorization grants for high-privilege operations."""
    _instance: Optional["DualKeyApprovalManager"] = None
    _lock = threading.Lock()
    _local = threading.local()

    def __init__(self) -> None:
        self._grants: Dict[str, DualKeyAuthorization] = {}
        self._approval_handler: Optional[Callable[[Dict[str, Any]], bool]] = None

    @classmethod
    def get_instance(cls) -> "DualKeyApprovalManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

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
            if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        token = secrets.token_hex(16)
        if not summary:
            summary = format_action_summary(tool_name, clean_args)
        auth = DualKeyAuthorization(
            tool_name=tool_name,
            action_hash=action_hash,
            token=token,
            expires_at=time.time() + ttl_seconds,
            confirmed=True,
            summary=summary,
        )
        with self._lock:
            self._grants[action_hash] = auth
        logger.info("Dual-Key authorization granted for tool '%s' (hash: %s, ttl: %.0fs)", tool_name, action_hash[:12], ttl_seconds)
        return auth

    def has_valid_grant(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Checks if a valid, unexpired grant exists without consuming it."""
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        with self._lock:
            auth = self._grants.get(action_hash)
            return bool(auth and auth.is_valid_for(tool_name, action_hash))

    def consume_grant(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Validates and consumes an existing pre-grant for the given tool and arguments (single-use)."""
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        with self._lock:
            auth = self._grants.get(action_hash)
            if auth and auth.is_valid_for(tool_name, action_hash):
                del self._grants[action_hash]
                logger.info("Dual-Key authorization consumed for tool '%s' (hash: %s)", tool_name, action_hash[:12])
                return True
            # Clean up stale/expired grant if present
            if auth and time.time() > auth.expires_at:
                del self._grants[action_hash]
        return False

    def request_human_approval(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        summary: str = "",
        risk_level: int = 2,
    ) -> bool:
        """Presents the proposed action and readable summary to the user via registered trusted local handler."""
        with self._lock:
            handler = self._approval_handler
        if not handler:
            return False

        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
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
        }

        try:
            approved = handler(request_details)
            if approved:
                logger.info("Human operator approved action '%s' (hash: %s)", tool_name, action_hash[:12])
                return True
            else:
                logger.warning("Human operator denied action '%s' (hash: %s)", tool_name, action_hash[:12])
                return False
        except Exception as e:
            logger.error("Error during human approval request for '%s': %s", tool_name, e)
            return False

    def set_thread_authorized(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """Binds a verified single-use authorization to the current thread for tool execution."""
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
        }
        action_hash = compute_action_hash(tool_name, clean_args)
        self._local.authorized_action = (tool_name, action_hash)

    def clear_thread_authorized(self) -> None:
        """Clears the active thread-local authorization context."""
        self._local.authorized_action = None

    def verify_and_consume_active_context(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Verifies if the current thread or persistent grants hold a verified human authorization.
        Consumes the context immediately (single-use).
        """
        clean_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in ("authorization", "user_approved", "dual_key_confirmed", "confirmed", "_dual_key_token")
        }
        action_hash = compute_action_hash(tool_name, clean_args)

        # 1. Check thread-local execution context established by ToolOrchestrator
        active = getattr(self._local, "authorized_action", None)
        if active and active == (tool_name, action_hash):
            self._local.authorized_action = None
            return True

        # 2. Fallback to pre-existing stored grant (for direct API/test invocations)
        return self.consume_grant(tool_name, clean_args)

    def check_and_consume(self, tool_name: str, arguments: Dict[str, Any], risk_level: int = 2) -> bool:
        """Single-entry check: consumes an existing grant if present, or requests human approval via handler."""
        if self.consume_grant(tool_name, arguments):
            return True
        summary = format_action_summary(tool_name, arguments)
        return self.request_human_approval(tool_name, arguments, summary=summary, risk_level=risk_level)

    def reset(self) -> None:
        """Clears all pending and active authorization grants."""
        with self._lock:
            self._grants.clear()


class TurnTaintTracker:
    """Tracks untrusted external context within a conversational turn and enforces Dual-Key safety."""

    _instance: Optional["TurnTaintTracker"] = None
    _lock = threading.Lock()

    UNTRUSTED_INGESTION_TOOLS: Set[str] = {
        "web_search",
        "deep_research",
        "browser_navigate",
        "browser_get_dom",
        "read_webpage_content_cdp",
        "get_tech_news_headlines",
        "mcp_call_tool",
        "fetch_url",
    }

    HIGH_PRIVILEGE_MUTATION_TOOLS: Set[str] = {
        "execute_powershell",
        "execute_sandboxed_powershell",
        "run_job_sandboxed_process",
        "release_file_lock",
        "delete_file",
        "kill_process",
        "write_file",
        "patch_file",
        "create_or_update_custom_skill",
        "delete_custom_skill",
    }

    CRITICAL_PRIVILEGE_TOOLS: Set[str] = {
        "create_or_update_custom_skill",
        "delete_custom_skill",
        "execute_powershell",
    }

    def __init__(self) -> None:
        self.is_tainted: bool = False
        self._tainting_sources: List[str] = []

    @classmethod
    def get_instance(cls) -> "TurnTaintTracker":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def mark_tainted(self, source: str) -> None:
        """Marks the current turn as tainted by untrusted external data."""
        self.is_tainted = True
        self._tainting_sources.append(source)
        logger.warning("Turn marked as TAINTED by untrusted source: %s", source)

    def reset_turn(self) -> None:
        """Resets the taint tracking state at the start of a new conversational turn."""
        self.is_tainted = False
        self._tainting_sources.clear()

    def check_tool_execution(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        risk_level: int = 0,
        dual_key_confirmed: bool = False,
    ) -> bool:
        """Enforces mandatory out-of-band authorization on critical risk level 2 operations
        regardless of taint state, and enforces Dual-Key safety on high-privilege mutations
        when the turn has been tainted by external untrusted data.

        Validates and consumes authorization at the execution boundary BEFORE tool execution.
        """
        if tool_name in self.UNTRUSTED_INGESTION_TOOLS:
            self.mark_tainted(tool_name)
            return True

        is_critical = (risk_level >= 2) or (tool_name in self.CRITICAL_PRIVILEGE_TOOLS)
        is_tainted_mutation = self.is_tainted and (
            risk_level >= 1 or tool_name in self.HIGH_PRIVILEGE_MUTATION_TOOLS
        )

        if not (is_critical or is_tainted_mutation):
            return True

        # Validate and consume authorization at the execution boundary
        approval_mgr = DualKeyApprovalManager.get_instance()
        is_human_authorized = approval_mgr.check_and_consume(tool_name, arguments, risk_level=risk_level)

        # 1. Unconditional authorization check for Critical Risk (Level 2) operations
        if is_critical and not is_human_authorized:
            raise SecurityBoundaryViolation(
                f"Critical Authorization Gate: Tool '{tool_name}' (risk_level={risk_level}) "
                "modifies the host environment or live Python runtime and requires explicit, "
                "out-of-band user confirmation regardless of turn taint state."
            )

        # 2. Tainted turn check for High Privilege (Level 1) operations
        if is_tainted_mutation and not is_human_authorized:
            raise SecurityBoundaryViolation(
                f"Dual-Key Safety Gate: blocked high-privilege tool '{tool_name}' "
                f"execution in a turn tainted by untrusted external data "
                f"(sources: {', '.join(self._tainting_sources)})."
            )

        return True
