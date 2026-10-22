"""Closed-Loop Security Boundaries, SSRF Protection, Path Confinement, and Taint Tracking for Jarvis 2.0."""
import hashlib
import hmac
import ipaddress
import json
import logging
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

        # Check explicit internal confirmation bypass
        if dual_key_confirmed:
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
