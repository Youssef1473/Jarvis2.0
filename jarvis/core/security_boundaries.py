"""Closed-Loop Security Boundaries, SSRF Protection, Path Confinement, and Taint Tracking for Jarvis 2.0."""
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import threading
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Jarvis.SecurityBoundaries")


class SecurityBoundaryViolation(PermissionError):
    """Raised when an operation attempts to breach security, SSRF, or path boundaries."""
    pass


WINDOWS_RESERVED_DEVICE_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def validate_egress_url(
    url: str,
    allowed_schemes: Tuple[str, ...] = ("http", "https"),
    allow_local_mcp: bool = False,
    allow_localhost: bool = False,
) -> str:
    """Validates an egress URL against SSRF, DNS rebinding, and private network access.

    Performs fail-closed DNS pre-resolution and blocks all loopback, link-local,
    private RFC 1918, and multicast IP destinations.
    """
    if not url or not isinstance(url, str):
        raise SecurityBoundaryViolation("Empty or invalid URL provided.")

    clean_url = url.strip()
    # Reject CRLF injection
    if any(c in clean_url for c in ("\r", "\n", "\x00")):
        raise SecurityBoundaryViolation("URL contains illegal CRLF or null byte characters.")

    parsed = urllib.parse.urlsplit(clean_url)
    scheme = (parsed.scheme or "").lower()

    if scheme not in allowed_schemes:
        raise SecurityBoundaryViolation(
            f"Blocked URL scheme '{scheme}'. Permitted schemes: {allowed_schemes}"
        )

    hostname = parsed.hostname
    if not hostname:
        raise SecurityBoundaryViolation("Egress URL must contain a valid hostname.")

    hostname_lower = hostname.lower()

    # Reject localhost aliases outright unless allow_local_mcp or allow_localhost is explicitly granted
    permit_loopback = allow_local_mcp or allow_localhost
    if not permit_loopback:
        if (
            hostname_lower == "localhost"
            or hostname_lower.endswith(".localhost")
            or hostname_lower in ("127.0.0.1", "::1", "0.0.0.0")
        ):
            raise SecurityBoundaryViolation(f"Egress to localhost '{hostname}' is blocked.")

    port = parsed.port or (443 if scheme == "https" else 80)

    # DNS Resolution (Fail Closed)
    try:
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise SecurityBoundaryViolation(f"DNS resolution failed for host '{hostname}': {err}")
    except Exception as err:
        raise SecurityBoundaryViolation(f"Unexpected error resolving host '{hostname}': {err}")

    if not addr_info:
        raise SecurityBoundaryViolation(f"No IP addresses resolved for host '{hostname}'.")

    # Inspect all resolved IPs
    for entry in addr_info:
        raw_ip = entry[4][0]
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise SecurityBoundaryViolation(f"Resolved address '{raw_ip}' is not a valid IP.")

        # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
        if isinstance(ip_obj, ipaddress.IPv6Address) and getattr(ip_obj, "ipv4_mapped", None):
            ip_obj = ip_obj.ipv4_mapped

        # If permit_loopback is set, permit loopback for user-configured MCP servers or local dev browser tabs
        if permit_loopback and ip_obj.is_loopback:
            continue

        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            raise SecurityBoundaryViolation(
                f"SSRF Protection: Host '{hostname}' resolved to non-global address '{ip_obj}'."
            )

    return clean_url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom HTTP redirect handler that validates every redirect destination hop."""

    def __init__(self, max_hops: int = 4, allowed_schemes: Tuple[str, ...] = ("http", "https")):
        super().__init__()
        self.max_hops = max_hops
        self.allowed_schemes = allowed_schemes
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.max_hops:
            raise SecurityBoundaryViolation(f"Redirect limit exceeded (maximum {self.max_hops} hops).")

        # Validate destination hop through SSRF filter
        validated_url = validate_egress_url(newurl, allowed_schemes=self.allowed_schemes)
        return super().redirect_request(req, fp, code, msg, headers, validated_url)


def build_safe_url_opener(max_hops: int = 4) -> urllib.request.OpenerDirector:
    """Builds a urllib opener director equipped with SafeRedirectHandler."""
    return urllib.request.build_opener(SafeRedirectHandler(max_hops=max_hops))


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
    ) -> None:
        """Enforces Dual-Key security on high-privilege mutations when turn is tainted."""
        if tool_name in self.UNTRUSTED_INGESTION_TOOLS:
            self.mark_tainted(tool_name)
            return

        if self.is_tainted:
            if tool_name in self.HIGH_PRIVILEGE_MUTATION_TOOLS or risk_level >= 1:
                if not dual_key_confirmed:
                    raise SecurityBoundaryViolation(
                        f"Dual-Key Safety Gate: blocked high-privilege tool '{tool_name}' "
                        f"execution in a turn tainted by untrusted external data "
                        f"(sources: {', '.join(self._tainting_sources)})."
                    )
