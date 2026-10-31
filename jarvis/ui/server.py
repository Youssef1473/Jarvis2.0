"""Starlette and WebSocket server powering the Jarvis 2.0 Holographic Web HUD."""
import asyncio
import base64
import collections
import concurrent.futures
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, unquote_to_bytes, urlsplit

import anyio
import psutil
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from jarvis.skills.os_control import get_raw_battery_info

logger = logging.getLogger("Jarvis.UI")

# Ephemeral cryptographic session token generated per server run
_HUD_SESSION_TOKEN: str = secrets.token_urlsafe(32)

# Single-use, short-lived bootstrap token registry for zero-trust browser onboarding
_BOOTSTRAP_TOKENS: dict[str, float] = {}
_BOOTSTRAP_LOCK = threading.Lock()


def create_bootstrap_token(ttl_seconds: float = 60.0) -> str:
    """Creates a short-lived, single-use bootstrap token to exchange for a session cookie."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _BOOTSTRAP_LOCK:
        expired = [t for t, exp in _BOOTSTRAP_TOKENS.items() if exp < now]
        for t in expired:
            del _BOOTSTRAP_TOKENS[t]
        _BOOTSTRAP_TOKENS[token] = now + max(1.0, float(ttl_seconds))
    return token


def consume_bootstrap_token(token: str) -> bool:
    """Validates and immediately consumes a single-use bootstrap token."""
    if not token or not isinstance(token, str):
        return False
    now = time.time()
    with _BOOTSTRAP_LOCK:
        expires_at = _BOOTSTRAP_TOKENS.pop(token.strip(), None)
        if expires_at is not None and expires_at >= now:
            return True
    return False


def generate_csp_nonce() -> str:
    """Generates a cryptographically secure, unpredictable 32-byte Base64-encoded CSP nonce."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def render_template_with_nonce(template_text: str, nonce: str) -> str:
    """Injects a cryptographically verified CSP nonce into designated template blocks.

    Replaces explicit, known placeholders only (e.g. nonce="{{ csp_nonce }}")
    without altering other script or style elements.
    """
    if nonce:
        return re.sub(r'nonce="{{\s*csp_nonce\s*}}"', f'nonce="{nonce}"', template_text)
    return re.sub(r'\s*nonce="{{\s*csp_nonce\s*}}"', '', template_text)


def get_hud_session_token() -> str:
    """Returns the current ephemeral HUD cryptographic session token."""
    return _HUD_SESSION_TOKEN


def set_hud_session_token(token: str) -> None:
    """Overrides the current session token (useful for deterministic integration tests)."""
    global _HUD_SESSION_TOKEN
    _HUD_SESSION_TOKEN = str(token)


def resolve_safe_photo_path(photo_id: str, photos_dir: Path | None = None) -> Path | None:
    """Resolves a photo ID safely within the photos directory, preventing directory traversal.

    Rejects any paths containing traversal characters ('..', '/', '\\', '%2e', '%2f', '%5c', null bytes)
    or absolute paths. Returns the resolved Path if safe, or None if malicious or invalid.
    """
    if not photo_id or not isinstance(photo_id, str):
        return None

    clean_id = photo_id.strip()

    # Reject path traversal patterns explicitly
    traversal_indicators = ["..", "/", "\\", "%2e", "%2f", "%5c", "\0"]
    lower_id = clean_id.lower()
    for indicator in traversal_indicators:
        if indicator in lower_id:
            return None

    # Strict format check: alphanumeric ID with optional .jpg
    if not re.match(r"^[a-zA-Z0-9_\-]{1,128}(\.jpg)?$", clean_id):
        return None

    if photos_dir is None:
        try:
            from jarvis.skills.photo_manager import PHOTOS_DIR
            base_photos_dir = Path(PHOTOS_DIR).resolve()
        except (ImportError, AttributeError, OSError) as err:
            logger.debug("Photo base directory resolution fallback: %s", err)
            base_photos_dir = Path("data/photos").resolve()
    else:
        base_photos_dir = Path(photos_dir).resolve()

    target_name = clean_id if clean_id.endswith(".jpg") else f"{clean_id}.jpg"
    target_path = (base_photos_dir / target_name).resolve()

    try:
        if not target_path.is_relative_to(base_photos_dir):
            return None
    except (ValueError, TypeError) as err:
        logger.debug("Photo path boundary containment check notice: %s", err)
        return None

    return target_path


SENSITIVE_QUERY_PARAM_NAMES: frozenset[str] = frozenset({
    "bootstrap",
    "token",
    "key",
    "secret",
    "auth",
    "access_token",
    "api_key",
    "session",
    "session_token",
    "password",
    "passwd",
})


MAX_QUERY_STRING_BYTES: int = 10 * 1024 * 1024  # 10 MB limit
OVERSIZED_QUERY_PLACEHOLDER_BYTES: bytes = b"[OVERSIZED_QUERY_REDACTED]"
OVERSIZED_QUERY_PLACEHOLDER_TEXT: str = "[OVERSIZED_QUERY_REDACTED]"


def redact_query_string_bytes(qs: bytes) -> bytes:
    """Parses raw query string bytes, unquotes parameter names, and redacts sensitive values.

    Decodes percent-encoded names (e.g. '%74oken', '%62ootstrap', 't%6fken') and redacts matched values
    while preserving authentic parameter casing, delimiters (&, ;), and non-sensitive query keys.
    Processes queries up to 10MB fully so sensitive parameters near both the beginning
    and end are guaranteed to be redacted before access logging.
    Inputs exceeding 10MB fail safe to fixed placeholder [OVERSIZED_QUERY_REDACTED] to prevent
    unredacted credential leaks or partial redaction bypasses.
    """
    if not qs:
        return qs
    if len(qs) > MAX_QUERY_STRING_BYTES:
        return OVERSIZED_QUERY_PLACEHOLDER_BYTES
    parts = re.split(rb"([&;]+)", qs)
    out_parts: list[bytes] = []
    for part in parts:
        if not part or part.startswith(b"&") or part.startswith(b";"):
            out_parts.append(part)
            continue
        eq_pos = part.find(b"=")
        if eq_pos == -1:
            out_parts.append(part)
            continue
        raw_key = part[:eq_pos]
        try:
            decoded_key = unquote_to_bytes(raw_key).decode("utf-8", errors="replace").lower().strip()
        except (UnicodeError, ValueError) as err:
            logger.debug("Query string key decoding notice: %s", err)
            decoded_key = raw_key.decode("latin1", errors="replace").lower().strip()

        if decoded_key in SENSITIVE_QUERY_PARAM_NAMES:
            out_parts.append(raw_key + b"=[REDACTED]")
        else:
            out_parts.append(part)
    return b"".join(out_parts)


def redact_query_string_text(text: str) -> str:
    """Bounded, decoded-parameter redaction for log records and formatted URLs.

    Matches key=value pairs, unquotes parameter names (including percent-encoded variants),
    and replaces sensitive values with '[REDACTED]'. Fully processes log lines up to 10MB
    so credentials near the beginning and end are scrubbed. Inputs exceeding 10MB fail safe
    to fixed placeholder [OVERSIZED_QUERY_REDACTED].
    """
    if not text:
        return text
    if len(text) > MAX_QUERY_STRING_BYTES:
        return OVERSIZED_QUERY_PLACEHOLDER_TEXT

    def _repl(m: re.Match) -> str:
        k = m.group(1)
        try:
            decoded_k = unquote(k).lower().strip()
        except (UnicodeError, ValueError) as err:
            logger.debug("Query string key text decode notice: %s", err)
            decoded_k = k.lower().strip()
        if decoded_k in SENSITIVE_QUERY_PARAM_NAMES:
            return f"{k}=[REDACTED]"
        return m.group(0)

    # Match key=val where key is preceded by start, &, ;, ?, quote, space, or brackets
    return re.sub(r"(?:^|(?<=[\s\?&;\x22\x27<({\[]))([^&=;?\s\x22\x27\\]+)=([^&;\s\x22\x27\\]*)", _repl, text)


class SensitiveQueryRedactionFilter(logging.Filter):
    """Logging filter that redacts sensitive credentials from access log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_query_string_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_query_string_text(a) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (redact_query_string_text(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True


def attach_access_log_redaction_filters() -> None:
    """Attaches SensitiveQueryRedactionFilter to all Uvicorn and UI loggers."""
    redaction_filter = SensitiveQueryRedactionFilter()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn", "Jarvis.UI"):
        l = logging.getLogger(name)
        l.addFilter(redaction_filter)
        for h in l.handlers:
            h.addFilter(redaction_filter)


class AccessLogRedactionMiddleware:
    """ASGI middleware that scrubs sensitive query parameters in-place from ASGI scope.

    Ensures that when Uvicorn formats HTTP access logs via get_path_with_query_string(self.scope)
    during http.response.start (or websocket.accept), credentials are already replaced with '[REDACTED]'.
    Legitimate downstream application handlers read original unredacted query parameters before response start.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        redacted = False

        def _redact_query_string() -> None:
            nonlocal redacted
            if not redacted:
                qs = scope.get("query_string", b"")
                if qs:
                    scope["query_string"] = redact_query_string_bytes(qs)
                redacted = True

        async def send_wrapper(message: Any) -> None:
            if message.get("type") in ("http.response.start", "websocket.accept", "websocket.close"):
                _redact_query_string()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _redact_query_string()



def is_browser_facing_https(request: Any, service: Any = None) -> bool:
    """Determines whether the browser-facing connection is HTTPS.

    Guarantees Secure=True whenever the browser-facing connection is HTTPS:
    1. Direct connection is HTTPS (request.url.scheme == "https").
    2. Request arrives from an explicitly configured trusted reverse proxy (JARVIS_TRUSTED_PROXIES)
       with verified X-Forwarded-Proto: https or Forwarded: proto=https headers.
    3. Secure tunnel mode (JARVIS_SECURE_TUNNEL=1) is active and tunnel telemetry indicates HTTPS.

    Strictly preserves local HTTP loopback (Secure=False) when no trusted HTTPS proxy is configured,
    and ignores forwarded scheme headers from unconfigured or untrusted clients.
    """
    # 1. Direct TLS connection
    if getattr(getattr(request, "url", None), "scheme", "").lower() == "https":
        return True

    # 2. Extract client peer address
    client_host = ""
    if getattr(request, "client", None) and getattr(request.client, "host", None):
        client_host = str(request.client.host).strip().lower()

    # 3. Determine trusted proxy sources
    trusted_proxies: set[str] = set()
    raw_trusted = os.environ.get("JARVIS_TRUSTED_PROXIES", "").strip()
    if raw_trusted:
        for p in raw_trusted.split(","):
            p_clean = p.strip().lower()
            if p_clean:
                trusted_proxies.add(p_clean)

    has_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL") == "1"
    if has_tunnel:
        # Secure tunnel daemons (cloudflared, ngrok, local tunnel) connect via loopback
        trusted_proxies.update({"127.0.0.1", "::1", "localhost", "testserver", "testclient"})

    # 4. If client is a trusted proxy, inspect forwarded scheme headers
    if client_host and client_host in trusted_proxies:
        xf_proto = request.headers.get("x-forwarded-proto", "").strip().lower()
        if xf_proto == "https" or xf_proto.startswith("https,"):
            return True

        fwd = request.headers.get("forwarded", "").strip().lower()
        if "proto=https" in fwd:
            return True

    # 5. Secure tunnel heuristics if client is loopback/trusted and tunnel host/port configured
    if has_tunnel and (not client_host or client_host in trusted_proxies or client_host in ("127.0.0.1", "::1", "testserver", "testclient")):
        tunnel_port = os.environ.get("JARVIS_TUNNEL_PORT", "").strip()
        if tunnel_port == "443":
            return True
        origin = getattr(request, "headers", {}).get("origin", "").strip().lower() if request else ""
        if origin.startswith("https://"):
            return True
        tunnel_host = os.environ.get("JARVIS_TUNNEL_HOST", "").strip().lower()
        if tunnel_host:
            host_hdr = getattr(request, "headers", {}).get("host", "").strip().lower() if request else ""
            if tunnel_host in host_hdr or request is None:
                return True

    return False


def validate_request_host_and_origin(
    headers: Any,
    service: Any,
    request_scheme: str = "http",
) -> tuple[bool, str, int]:
    """Validates Host and Origin headers with exact scheme, hostname, and port verification.

    Protects against DNS rebinding, cross-origin WebSocket hijacking, and CSRF attacks.
    Supports IPv4, IPv6 ([::1]), testserver, and configured secure tunnels.
    """
    raw_host = headers.get("host", "").strip()
    if not raw_host:
        return False, "Forbidden: Invalid Host header", 403

    try:
        parsed_host = urlsplit(f"//{raw_host}")
    except ValueError:
        return False, "Forbidden: Malformed Host header", 403

    host_name = parsed_host.hostname
    if not host_name:
        return False, "Forbidden: Invalid Host header", 403

    host_name = host_name.lower()

    # Allowed hostnames
    allowed_hostnames = {"127.0.0.1", "localhost", "::1", "testserver"}
    service_host = getattr(service, "host", None)
    if service_host:
        allowed_hostnames.add(str(service_host).lower())

    has_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL") == "1"
    if has_tunnel:
        for env_var in ("JARVIS_ALLOWED_HOSTS", "JARVIS_TUNNEL_HOST"):
            val = os.environ.get(env_var, "").strip()
            if val:
                for h in val.split(","):
                    h_clean = h.strip().lower()
                    if h_clean:
                        allowed_hostnames.add(h_clean)

    if host_name not in allowed_hostnames:
        return False, f"Forbidden: Invalid Host header: '{host_name}' not allowed", 403

    # Allowed ports
    service_port = getattr(service, "port", 20129)
    try:
        service_port = int(service_port)
    except (TypeError, ValueError):
        service_port = 20129
    allowed_ports = {service_port}

    if has_tunnel:
        allowed_ports.update({80, 443})
        tunnel_port = os.environ.get("JARVIS_TUNNEL_PORT", "").strip()
        if tunnel_port.isdigit():
            allowed_ports.add(int(tunnel_port))

    # Host port verification (testserver is exempt)
    if host_name != "testserver" and parsed_host.port is not None:
        if parsed_host.port not in allowed_ports:
            return False, f"Forbidden: Host port {parsed_host.port} not allowed", 403

    # Origin header verification if present
    origin = headers.get("origin")
    if origin is not None:
        origin_str = origin.strip()
        if not origin_str or origin_str.lower() == "null":
            return False, "Forbidden: Invalid origin header", 403

        try:
            parsed_origin = urlsplit(origin_str)
        except ValueError:
            return False, "Forbidden: Malformed origin header", 403

        if not parsed_origin.scheme or not parsed_origin.netloc or not parsed_origin.hostname:
            return False, "Forbidden: Malformed origin header", 403

        origin_scheme = parsed_origin.scheme.lower()
        allowed_schemes = {"http"}
        if request_scheme in ("https", "wss") or has_tunnel:
            allowed_schemes.add("https")
        if request_scheme in ("https", "wss"):
            allowed_schemes.discard("http")

        if origin_scheme not in allowed_schemes:
            return False, f"Forbidden: Origin scheme '{origin_scheme}' not allowed", 403

        origin_host = parsed_origin.hostname.lower()
        if origin_host not in allowed_hostnames:
            return False, f"Forbidden: Origin host '{origin_host}' not allowed", 403

        if origin_host != "testserver":
            origin_port = parsed_origin.port
            if origin_port is None:
                implicit_port = 443 if origin_scheme == "https" else 80
                if implicit_port not in allowed_ports:
                    return False, f"Forbidden: Origin port {implicit_port} not allowed", 403
            elif origin_port not in allowed_ports:
                return False, f"Forbidden: Origin port {origin_port} not allowed", 403

    return True, "", 200


def derive_absolute_reporting_endpoint(request: Any = None, service: Any = None) -> str:
    """Derives an absolute same-origin reporting endpoint URL from the validated connection context.

    Mandated by the W3C Reporting API specification (RFC 8941 / Reporting-Endpoints dictionary),
    which requires absolute URLs with scheme, host, and port. Supports local loopback and
    configured secure-tunnel deployments.
    """
    has_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL") == "1"
    tunnel_host = os.environ.get("JARVIS_TUNNEL_HOST", "").strip().lower()

    # Determine scheme: secure tunnel edge is always browser-facing HTTPS
    if has_tunnel and tunnel_host:
        scheme = "https"
    else:
        is_https = is_browser_facing_https(request, service)
        scheme = "https" if is_https else "http"

    # Determine host and port
    if has_tunnel and tunnel_host:
        tunnel_port = os.environ.get("JARVIS_TUNNEL_PORT", "").strip()
        if tunnel_port and tunnel_port not in ("80", "443"):
            netloc = f"{tunnel_host}:{tunnel_port}"
        else:
            netloc = tunnel_host
    elif request is not None and getattr(request, "headers", None) and request.headers.get("host"):
        netloc = request.headers.get("host").strip()
    elif service is not None:
        host = getattr(service, "host", "127.0.0.1")
        port = getattr(service, "port", 20129)
        try:
            port_num = int(port)
        except (TypeError, ValueError):
            port_num = 20129
        netloc = f"{host}:{port_num}" if (is_https and port_num != 443) or (not is_https and port_num != 80) else host
    else:
        netloc = "127.0.0.1:20129"

    return f"{scheme}://{netloc}/api/csp-report"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Enforces zero-trust loopback token authentication, strict origin verification, and CSP headers."""

    def __init__(self, app, service: Any = None):
        super().__init__(app)
        self.service = service

    SENSITIVE_GET_PREFIXES = (
        "/api/status",
        "/api/screenshot",
        "/api/memory",
        "/api/approval",
        "/api/tasks",
        "/api/notes",
        "/api/calendar",
        "/api/documents",
        "/api/photos",
        "/api/timers",
    )

    def _add_security_headers(self, response, request: Any = None, nonce: str | None = None):
        if nonce is None and request is not None:
            nonce = getattr(getattr(request, "state", None), "csp_nonce", None)
        if not nonce:
            nonce = generate_csp_nonce()

        has_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL") == "1"
        tunnel_host = os.environ.get("JARVIS_TUNNEL_HOST", "").strip().lower()
        tunnel_connect = f" https://{tunnel_host} wss://{tunnel_host}" if (has_tunnel and tunnel_host) else ""

        connect_src_enforced = f"'self' ws://127.0.0.1:* ws://localhost:* http://127.0.0.1:* http://localhost:* ws: http:{tunnel_connect}"
        connect_src_report = f"'self' ws://127.0.0.1:* ws://localhost:* http://127.0.0.1:* http://localhost:*{tunnel_connect}"

        reporting_endpoint_url = derive_absolute_reporting_endpoint(request, self.service)
        response.headers["Reporting-Endpoints"] = f'csp-endpoint="{reporting_endpoint_url}"'
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: ws: http:; "
            "img-src 'self' data: blob:; "
            f"connect-src {connect_src_enforced}; "
            f"script-src 'self' 'unsafe-inline' 'unsafe-eval' 'nonce-{nonce}'; "
            f"script-src-elem 'self' 'unsafe-inline' 'unsafe-eval' 'nonce-{nonce}'; "
            "script-src-attr 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            f"style-src-elem 'self' https://fonts.googleapis.com 'nonce-{nonce}'; "
            "style-src-attr 'unsafe-inline'; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "report-uri /api/csp-report; "
            "report-to csp-endpoint;"
        )
        response.headers["Content-Security-Policy-Report-Only"] = (
            "default-src 'self' 'unsafe-inline' data:; "
            "img-src 'self' data:; "
            f"connect-src {connect_src_report}; "
            f"script-src 'self' 'unsafe-inline' 'nonce-{nonce}'; "
            f"script-src-elem 'self' 'nonce-{nonce}'; "
            "script-src-attr 'none'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            f"style-src-elem 'self' https://fonts.googleapis.com 'nonce-{nonce}'; "
            "style-src-attr 'unsafe-inline'; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "report-uri /api/csp-report; "
            "report-to csp-endpoint;"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def dispatch(self, request, call_next):
        nonce = getattr(getattr(request, "state", None), "csp_nonce", None)
        if not nonce:
            nonce = generate_csp_nonce()
            if hasattr(request, "state"):
                request.state.csp_nonce = nonce

        # 1. Validate Host and Origin headers with exact scheme, host, and port matching
        is_valid, err_msg, status_code = validate_request_host_and_origin(
            headers=request.headers,
            service=self.service,
            request_scheme=request.url.scheme,
        )
        if not is_valid:
            return self._add_security_headers(
                JSONResponse({"error": err_msg}, status_code=status_code),
                request=request,
                nonce=nonce,
            )

        # 2. Check token authentication on state-mutating and sensitive read routes
        path = request.url.path
        is_mutating = request.method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api/")
        is_sensitive_get = request.method == "GET" and any(path.startswith(p) for p in self.SENSITIVE_GET_PREFIXES)

        # Allow unauthenticated access to the authentication exchange endpoint and CSP telemetry reporting
        if path in ("/api/auth/unlock", "/api/csp-report"):
            response = await call_next(request)
            return self._add_security_headers(response, request=request, nonce=nonce)

        enforce_auth = (
            getattr(self.service, "enforce_auth", True)
            and (
                getattr(getattr(request, "app", None), "state", None) is None
                or getattr(request.app.state, "enforce_auth", True)
            )
        )

        if (is_mutating or is_sensitive_get) and enforce_auth:
            token = (
                request.headers.get("x-jarvis-token")
                or request.cookies.get("jarvis_session")
            )
            is_authenticated = bool(token and secrets.compare_digest(token, _HUD_SESSION_TOKEN))

            if not is_authenticated:
                return self._add_security_headers(
                    JSONResponse(
                        {"error": "Unauthorized: Invalid or missing X-Jarvis-Token session credentials"},
                        status_code=401,
                    ),
                    request=request,
                    nonce=nonce,
                )

        response = await call_next(request)
        return self._add_security_headers(response, request=request, nonce=nonce)


class WorkstationMetricsCollector:
    """Collects system vitals via psutil and native NVML bindings strictly off the event loop."""

    _nvml_initialized: bool = False
    _device_handle: Any = None
    _nvml_module: Any = None
    _nvml_lock: threading.Lock = threading.Lock()

    _DEFAULT_GPU_METRICS: dict[str, Any] = {
        "gpu_name": "NVIDIA GeForce RTX 3050 Laptop GPU",
        "gpu_load": 0,
        "vram_used_mb": 0,
        "vram_total_mb": 4096,
        "gpu_temp_c": 0,
    }

    @classmethod
    def _init_nvml(cls) -> bool:
        """Initializes NVML and acquires device handle 0 inside a thread-safe singleton gate."""
        with cls._nvml_lock:
            if cls._nvml_initialized and cls._device_handle is not None:
                return True

            try:
                import pynvml
                cls._nvml_module = pynvml
                cls._nvml_module.nvmlInit()
                cls._device_handle = cls._nvml_module.nvmlDeviceGetHandleByIndex(0)
                cls._nvml_initialized = True
                return True
            except Exception as exc:
                cls._nvml_initialized = False
                cls._device_handle = None
                cls._nvml_module = None
                logger.debug("Native NVML initialization bypassed (driver or module unavailable): %s", exc)
                return False

    @classmethod
    def _get_gpu_metrics_native(cls) -> dict[str, Any]:
        """Polls GPU hardware states natively via pynvml bindings without subprocess forks."""
        if not cls._init_nvml():
            # Fallback to secondary watchdog query if available
            try:
                from jarvis.skills.system_watchdog import _query_gpu_telemetry
                cached = _query_gpu_telemetry()
                if cached:
                    return {
                        "gpu_name": cached.get("name", cls._DEFAULT_GPU_METRICS["gpu_name"]),
                        "gpu_load": cached.get("load_pct", 0),
                        "vram_used_mb": cached.get("vram_used", 0),
                        "vram_total_mb": cached.get("vram_total", 4096),
                        "gpu_temp_c": cached.get("temp_c", 0),
                    }
            except (AttributeError, KeyError, TypeError) as err:
                logger.debug("GPU cache inspection notice: %s", err)
            return dict(cls._DEFAULT_GPU_METRICS)

        try:
            handle = cls._device_handle
            nvml = cls._nvml_module

            raw_name = nvml.nvmlDeviceGetName(handle)
            gpu_name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)

            util_rates = nvml.nvmlDeviceGetUtilizationRates(handle)
            gpu_load = int(util_rates.gpu)

            mem_info = nvml.nvmlDeviceGetMemoryInfo(handle)
            vram_used_mb = int(mem_info.used / (1024 * 1024))
            vram_total_mb = int(mem_info.total / (1024 * 1024))

            gpu_temp_c = int(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))

            return {
                "gpu_name": gpu_name,
                "gpu_load": gpu_load,
                "vram_used_mb": vram_used_mb,
                "vram_total_mb": vram_total_mb,
                "gpu_temp_c": gpu_temp_c,
            }
        except Exception as exc:
            logger.debug("NVML hardware poll failed; resetting handle: %s", exc)
            with cls._nvml_lock:
                cls._nvml_initialized = False
                cls._device_handle = None
            return dict(cls._DEFAULT_GPU_METRICS)

    @classmethod
    def shutdown_nvml(cls):
        """Cleanly finalizes the NVML driver session during host termination."""
        with cls._nvml_lock:
            if cls._nvml_initialized and cls._nvml_module is not None:
                try:
                    cls._nvml_module.nvmlShutdown()
                except Exception as err:  # noqa: BLE001 - Driver-level cleanup boundary must not block host teardown
                    logger.debug("NVML driver shutdown notice: %s", err)
                finally:
                    cls._nvml_initialized = False
                    cls._device_handle = None
                    cls._nvml_module = None

    @staticmethod
    def sample_metrics_sync(orchestrator: Any = None) -> dict[str, Any]:
        """Synchronous vitals collection combining psutil host memory/CPU with native NVML GPU telemetry."""
        bat = get_raw_battery_info()
        cpu_val = round(psutil.cpu_percent(interval=None), 1)

        vmem = psutil.virtual_memory()
        ram_pct = round(vmem.percent, 1)
        ram_used_gb = round(vmem.used / (1024 ** 3), 1)
        ram_total_gb = round(vmem.total / (1024 ** 3), 1)
        ram_used_mb = int(vmem.used / (1024 * 1024))
        ram_total_mb = int(vmem.total / (1024 * 1024))

        gpu_data = WorkstationMetricsCollector._get_gpu_metrics_native()

        state = "idle"
        active_model = "qwen/qwen3.8-27b"
        tier = "TRI-TIER HYBRID"
        provider = "hybrid"
        is_muted = False
        specialist_status = {"gemini": "ACTIVE", "groq": "ACTIVE", "grok": "METERED / OFF", "claude": "METERED / OFF"}
        latencies = {"ollama": 0.0, "groq": 0.0, "gemini": 0.0}
        memory_label = "TIER 2 (ACTIVE)"

        if orchestrator is not None:
            if hasattr(orchestrator, "is_muted") and callable(orchestrator.is_muted):
                try:
                    is_muted = bool(orchestrator.is_muted())
                except (AttributeError, TypeError, RuntimeError, OSError) as err:
                    logger.debug("Orchestrator is_muted probe notice: %s", err)
                    is_muted = False

            if hasattr(orchestrator, "state"):
                state = str(getattr(orchestrator, "state", "idle"))

            if hasattr(orchestrator, "brain"):
                b = orchestrator.brain
                p = getattr(b, "provider", "hybrid")
                provider = p
                tier = "TRI-TIER HYBRID" if p in ("hybrid", "auto") else ("LOCAL" if p == "ollama" else "CLOUD")
                active_model = str(getattr(b, "openai_model", getattr(b, "provider", active_model)))
                status_provider = getattr(b, "specialist_status", None)
                if callable(status_provider):
                    try:
                        specialist_status = status_provider()
                    except (AttributeError, TypeError, RuntimeError) as err:
                        logger.debug("Specialist status probe notice: %s", err)
                lat_getter = getattr(b, "get_latencies", None)
                if callable(lat_getter):
                    try:
                        latencies = lat_getter()
                    except (AttributeError, TypeError, RuntimeError) as err:
                        logger.debug("Provider latencies probe notice: %s", err)
                if hasattr(b, "memory"):
                    try:
                        if hasattr(b.memory, "get_all_memory_nodes"):
                            m_nodes = b.memory.get_all_memory_nodes(limit=1)
                            memory_label = f"{m_nodes.get('total', 0)} NODES STORED"
                        else:
                            facts = b.memory.get_all_facts()
                            memory_label = f"{len(facts)} FACTS STORED"
                    except (AttributeError, TypeError, RuntimeError, OSError, sqlite3.Error) as err:
                        logger.debug("Memory nodes probe notice: %s", err)

        timer_count = 0
        try:
            from jarvis.skills.timers import _active_timers, _timer_lock
            with _timer_lock:
                timer_count = len(_active_timers)
        except (ImportError, AttributeError, RuntimeError) as err:
            logger.debug("Active timers registry probe notice: %s", err)

        latency_percentiles = {}
        p99_history = []
        profiler = None
        if orchestrator is not None and hasattr(orchestrator, "brain"):
            b = orchestrator.brain
            if hasattr(b, "latency_profiler") and b.latency_profiler:
                profiler = b.latency_profiler
        if profiler is None and orchestrator is not None and hasattr(orchestrator, "latency_profiler") and orchestrator.latency_profiler:
            profiler = orchestrator.latency_profiler
        if profiler is None:
            try:
                from jarvis.skills.diagnostics import get_global_latency_profiler
                profiler = get_global_latency_profiler()
            except Exception:
                profiler = None

        if profiler is not None:
            try:
                latency_percentiles = profiler.get_percentiles()
            except Exception:
                latency_percentiles = {}
            try:
                if hasattr(profiler, "get_p99_history"):
                    p99_history = profiler.get_p99_history()
            except Exception:
                p99_history = []

        return {
            # Single-character keys required by Council Telemetry Directive
            "c": cpu_val,
            "rp": ram_pct,
            "ru": ram_used_mb,
            "rt": ram_total_mb,
            "gp": gpu_data["gpu_load"],
            "gl": gpu_data["gpu_load"],
            "vu": gpu_data["vram_used_mb"],
            "vt": gpu_data["vram_total_mb"],
            "gt": gpu_data["gpu_temp_c"],
            "st": state,
            "md": active_model,
            "m": 1 if is_muted else 0,
            "ts": int(time.time() * 1000),
            # Backwards-compatible fields for existing test assertions & components
            "cpu": cpu_val,
            "ram_pct": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "gpu": gpu_data["gpu_name"],
            "gpu_name": gpu_data["gpu_name"],
            "gpu_load": gpu_data["gpu_load"],
            "gpu_temp": gpu_data["gpu_temp_c"],
            "vram_used": f"{gpu_data['vram_used_mb']} MB",
            "vram_used_mb": gpu_data["vram_used_mb"],
            "vram_total_mb": gpu_data["vram_total_mb"],
            "model": active_model,
            "tier": tier,
            "provider": provider,
            "state": state,
            "specialists": specialist_status,
            "active_timers": timer_count,
            "latencies": latencies,
            "memory_label": memory_label,
            "is_muted": is_muted,
            "battery": bat.get("percent", 100),
            "charging": bat.get("charging", False),
            "has_battery": bat.get("has_battery", False),
            "timestamp": time.strftime("%H:%M:%S"),
            "latency_percentiles": latency_percentiles,
            "lp": latency_percentiles,
            "p99_history": p99_history,
            "p99_h": p99_history,
        }


class TelemetryConnectionManager:
    """Manages active HUD WebSocket connections with an 8-subscriber ceiling and lossy Queue(maxsize=1)."""

    def __init__(self, max_clients: int = 8):
        self.max_clients = max_clients
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._sender_tasks: dict[WebSocket, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def connected_clients(self) -> set[WebSocket]:
        return set(self._clients.keys())

    async def connect(self, websocket: WebSocket) -> bool:
        """Enforces 8-subscriber connection ceiling and binds an isolated Queue(maxsize=2)."""
        async with self._lock:
            if len(self._clients) >= self.max_clients:
                logger.warning("HUD WebSocket connection rejected: max client ceiling (%d) reached.", self.max_clients)
                await websocket.close(code=1008, reason="Max subscriber ceiling reached")
                return False

            await websocket.accept()
            queue: asyncio.Queue = asyncio.Queue(maxsize=2)
            self._clients[websocket] = queue
            task = asyncio.create_task(self._client_sender_worker(websocket, queue))
            self._sender_tasks[websocket] = task
            logger.info("HUD WebSocket client connected (Total: %d)", len(self._clients))
            return True

    async def disconnect(self, websocket: WebSocket):
        """Safely cleans up subscriber queue, cancels sender task, and releases socket."""
        async with self._lock:
            if websocket in self._sender_tasks:
                task = self._sender_tasks.pop(websocket)
                task.cancel()
            self._clients.pop(websocket, None)
            logger.info("HUD WebSocket client disconnected (Total: %d)", len(self._clients))

    async def _client_sender_worker(self, websocket: WebSocket, queue: asyncio.Queue):
        """Dedicated coroutine per client: drains Queue(maxsize=2) and writes to socket."""
        try:
            while True:
                payload = await queue.get()
                if isinstance(payload, bytes):
                    await websocket.send_bytes(payload)
                else:
                    await websocket.send_text(payload)
                queue.task_done()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug("Client sender worker terminated: %s", e)
        finally:
            asyncio.create_task(self.disconnect(websocket))

    def broadcast_telemetry(self, raw_data: dict[str, Any]):
        """Non-blocking fanout with non-blocking queue.get_nowait() to drop stale frames under backpressure."""
        if not self._clients:
            return

        payload = {
            "type": "telemetry",
            "t": "telem",
            "c": raw_data.get("c", raw_data.get("cpu", 0.0)),
            "rp": raw_data.get("rp", raw_data.get("ram_pct", 0.0)),
            "ru": raw_data.get("ru", raw_data.get("ram_used_mb", 0)),
            "rt": raw_data.get("rt", raw_data.get("ram_total_mb", 16384)),
            "gp": raw_data.get("gp", raw_data.get("gpu_load", 0)),
            "gl": raw_data.get("gl", raw_data.get("gpu_load", 0)),
            "vu": raw_data.get("vu", raw_data.get("vram_used_mb", 0)),
            "vt": raw_data.get("vt", raw_data.get("vram_total_mb", 4096)),
            "gt": raw_data.get("gt", raw_data.get("gpu_temp_c", 0)),
            "st": raw_data.get("st", raw_data.get("state", "idle")),
            "md": raw_data.get("md", raw_data.get("model", "qwen3.8-27b")),
            "m": raw_data.get("m", 0),
            "ts": raw_data.get("ts", int(time.time() * 1000)),
            # Legacy fields for existing test assertions & components
            "cpu": raw_data.get("cpu", raw_data.get("c", 0.0)),
            "ram_pct": raw_data.get("ram_pct", raw_data.get("rp", 0.0)),
            "ram_used_gb": raw_data.get("ram_used_gb", round(raw_data.get("ru", 0) / 1024, 1)),
            "ram_total_gb": raw_data.get("ram_total_gb", round(raw_data.get("rt", 16384) / 1024, 1)),
            "gpu": raw_data.get("gpu", raw_data.get("gpu_name", "RTX 3050")),
            "timestamp": raw_data.get("timestamp", time.strftime("%H:%M:%S")),
            "latency_percentiles": raw_data.get("latency_percentiles", raw_data.get("lp", {})),
            "p99_history": raw_data.get("p99_history", raw_data.get("p99_h", [])),
            "p99_h": raw_data.get("p99_h", raw_data.get("p99_history", [])),
        }
        minified_str = json.dumps(payload, separators=(",", ":"))

        for queue in list(self._clients.values()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(minified_str)
            except asyncio.QueueFull:
                pass

    async def broadcast_direct(self, message_str: str):
        """Broadcasts discrete event message directly to all connected clients."""
        dead = []
        for ws in list(self._clients.keys()):
            try:
                await ws.send_text(message_str)
            except (WebSocketDisconnect, RuntimeError, OSError) as err:
                logger.debug("WebSocket client disconnected during direct broadcast: %s", err)
                dead.append(ws)
            except Exception as e:  # noqa: BLE001 - Keep connection pool robust against client socket faults
                logger.debug("Unexpected error during direct broadcast to client: %s", e)
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def broadcast_binary(self, data: bytes):
        """Broadcasts binary DSP frames to connected WebSocket subscribers via bounded queue."""
        for queue in list(self._clients.values()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass



class JarvisUIService:
    """Manages the lifecycle of the Starlette HUD server and WebSocket broadcasting."""

    _instance: Optional["JarvisUIService"] = None

    def __init__(self, orchestrator: Any = None, host: str = "127.0.0.1", port: int = 20129, enforce_auth: bool = True, access_log: bool = False):
        self.orchestrator = orchestrator
        self.host = host
        self.port = port
        self.enforce_auth = enforce_auth
        self.access_log = access_log
        self.telemetry_manager = TelemetryConnectionManager(max_clients=8)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready_event = threading.Event()
        self._stopped_event = threading.Event()
        self.template_path = Path(__file__).parent / "templates" / "hud.html"
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._approval_lock = threading.Lock()
        self._csp_violations_ring: collections.deque = collections.deque(maxlen=50)
        self._csp_report_timestamps: collections.deque = collections.deque(maxlen=100)
        self._csp_report_lock = threading.Lock()
        self._csp_dedup_cache: dict[str, float] = {}
        self._command_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="Jarvis-WSCommand"
        )
        try:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(self.handle_approval_request)
        except Exception as e:
            logger.debug("DualKeyApprovalManager binding notice: %s", e)
        JarvisUIService._instance = self
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    @property
    def connected_clients(self) -> set[WebSocket]:
        return self.telemetry_manager.connected_clients

    def get_authenticated_url(self) -> str:
        """Returns the HUD URL formatted with a single-use bootstrap token fragment."""
        token = create_bootstrap_token(ttl_seconds=60.0)
        return f"http://{self.host}:{self.port}/#bootstrap={token}"

    @classmethod
    def get_instance(cls) -> Optional["JarvisUIService"]:
        return cls._instance

    def _create_app(self) -> Starlette:
        routes = [
            Route("/", endpoint=self._handle_index, methods=["GET"]),
            Route("/api/auth/unlock", endpoint=self._handle_unlock, methods=["POST"]),
            Route("/api/csp-report", endpoint=self._handle_csp_report, methods=["POST"]),
            Route("/api/status", endpoint=self._handle_status, methods=["GET"]),
            Route("/api/command", endpoint=self._handle_command, methods=["POST"]),
            Route("/api/action", endpoint=self._handle_action, methods=["POST"]),
            Route("/api/mute", endpoint=self._handle_mute, methods=["POST"]),
            Route("/api/notes", endpoint=self._handle_notes, methods=["GET"]),
            Route("/api/timers", endpoint=self._handle_timers, methods=["GET"]),
            Route("/api/documents/upload", endpoint=self._handle_documents_upload, methods=["POST"]),
            Route("/api/documents/staged", endpoint=self._handle_staged_documents, methods=["GET"]),
            Route("/api/calendar", endpoint=self._handle_calendar, methods=["GET"]),
            Route("/api/calendar/delete", endpoint=self._handle_calendar_delete, methods=["POST"]),
            Route("/api/calendar/event/delete", endpoint=self._handle_calendar_event_delete, methods=["POST"]),
            Route("/api/calendar/clear", endpoint=self._handle_calendar_clear, methods=["POST"]),
            Route("/api/photos", endpoint=self._handle_photos_list, methods=["GET"]),
            Route("/api/photos/{photo_id}", endpoint=self._handle_photo_stream, methods=["GET"]),
            Route("/api/screenshot/latest", endpoint=self._handle_latest_screenshot, methods=["GET"]),
            Route("/api/tasks", endpoint=self._handle_tasks_list, methods=["GET"]),
            Route("/api/tasks/terminate", endpoint=self._handle_task_terminate, methods=["POST"]),
            Route("/api/memory/nodes", endpoint=self._handle_memory_nodes, methods=["GET"]),
            Route("/api/memory/delete", endpoint=self._handle_memory_delete, methods=["POST"]),
            Route("/api/memory/update", endpoint=self._handle_memory_update, methods=["POST"]),
            Route("/api/approval/pending", endpoint=self._handle_approval_pending, methods=["GET"]),
            Route("/api/approval/respond", endpoint=self._handle_approval_respond, methods=["POST"]),
            Route("/api/approval/grant", endpoint=self._handle_approval_grant, methods=["POST"]),
            WebSocketRoute("/ws", endpoint=self._handle_websocket),
            WebSocketRoute("/api/telemetry", endpoint=self._handle_websocket),
        ]
        middleware = [
            Middleware(AccessLogRedactionMiddleware),
            Middleware(SecurityHeadersMiddleware, service=self),
        ]
        app = Starlette(
            routes=routes,
            middleware=middleware,
        )
        app.state.enforce_auth = self.enforce_auth
        app.state.session_token = _HUD_SESSION_TOKEN
        return app

    async def _handle_photos_list(self, request):
        """Lists archived photos from SQLite memory for HUD photo matrix."""
        import sqlite3

        from jarvis.skills.photo_manager import DB_PATH
        try:
            def _query_photos():
                conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT photo_id, file_name, dimensions, file_size, caption, created_at
                    FROM photos
                    ORDER BY created_at DESC
                    LIMIT 60
                """)
                rows = [dict(r) for r in cur.fetchall()]
                conn.close()
                return rows

            photos = await anyio.to_thread.run_sync(_query_photos)
            return JSONResponse({"photos": photos})
        except Exception as e:
            logger.error("Error retrieving photos for HUD: %s", e)
            return JSONResponse({"photos": [], "error": str(e)})

    async def _handle_photo_stream(self, request):
        """Zero-RAM non-blocking photo streamer with ETag 304 caching and path traversal guard."""
        photo_id = request.path_params.get("photo_id", "").strip()
        target_path = resolve_safe_photo_path(photo_id)
        if target_path is None:
            return Response("Directory traversal or invalid photo ID forbidden.", status_code=403)

        exists = await anyio.to_thread.run_sync(target_path.is_file)
        if not exists:
            return Response("Photo not found.", status_code=404)

        stat = await anyio.to_thread.run_sync(target_path.stat)
        etag = f'"{stat.st_mtime_ns}-{stat.st_size}"'

        # Check browser ETag cache for 304 Not Modified
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=86400, immutable"})

        async def _chunk_reader(path: Path, chunk_size: int = 65536):
            def _read_chunk(f):
                return f.read(chunk_size)
            file_obj = await anyio.to_thread.run_sync(lambda: open(path, "rb"))
            try:
                while True:
                    chunk = await anyio.to_thread.run_sync(_read_chunk, file_obj)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await anyio.to_thread.run_sync(file_obj.close)

        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=86400, immutable",
            "Content-Length": str(stat.st_size),
        }
        return StreamingResponse(_chunk_reader(target_path), media_type="image/jpeg", headers=headers)

    async def _handle_latest_screenshot(self, request):
        """Serves the latest audited workstation screenshot."""
        screenshot_path = Path("data/screenshots/latest_screen.jpg").resolve()
        exists = await anyio.to_thread.run_sync(screenshot_path.is_file)
        if not exists:
            return JSONResponse({"error": "No screenshot available, Sir."}, status_code=404)
        return FileResponse(str(screenshot_path), media_type="image/jpeg")

    async def _handle_tasks_list(self, request):
        """Lists active and recent background terminal tasks."""
        try:
            from jarvis.core.task_manager import BackgroundTaskManager
            mgr = BackgroundTaskManager.get_instance(self.orchestrator)
            tasks = mgr.list_tasks()
            return JSONResponse({"tasks": tasks, "count": len(tasks)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_task_terminate(self, request):
        """Terminates an active background task by ID."""
        try:
            data = await request.json()
            task_id = str(data.get("task_id", "")).strip()
            if not task_id:
                return JSONResponse({"error": "Missing task_id"}, status_code=400)
            from jarvis.core.task_manager import BackgroundTaskManager
            mgr = BackgroundTaskManager.get_instance(self.orchestrator)
            cancelled = mgr.cancel_task(task_id)
            if cancelled:
                return JSONResponse({"status": "cancelled", "task_id": task_id})
            return JSONResponse({"error": f"Task '{task_id}' not found or already terminated."}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_memory_nodes(self, request):
        """Retrieves structured memory nodes across facts, preferences, documents, and episodic lessons."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()

            search = request.query_params.get("q") or None
            category = request.query_params.get("category") or None
            limit = int(request.query_params.get("limit") or 150)

            default_nodes = {
                "total": 0,
                "counts": {"facts": 0, "preferences": 0, "documents": 0, "lessons": 0},
                "facts": [],
                "preferences": [],
                "documents": [],
                "lessons": [],
            }
            if hasattr(mem, "get_all_memory_nodes"):
                try:
                    def _fetch_nodes():
                        return mem.get_all_memory_nodes(category=category, search=search, limit=limit)

                    res = await anyio.to_thread.run_sync(_fetch_nodes)
                    if isinstance(res, dict):
                        for k in default_nodes:
                            if k not in res:
                                res[k] = default_nodes[k]
                        nodes = res
                    else:
                        nodes = default_nodes
                except Exception:
                    nodes = default_nodes
            else:
                nodes = default_nodes

            def _json_safe(obj):
                if isinstance(obj, dict):
                    return {str(k): _json_safe(v) for k, v in obj.items()}
                elif isinstance(obj, (list, tuple)):
                    return [_json_safe(x) for x in obj]
                elif isinstance(obj, (int, float, str, bool)) or obj is None:
                    return obj
                return str(obj)

            return JSONResponse(_json_safe(nodes))
        except Exception as e:
            logger.error("Error retrieving memory nodes: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_memory_delete(self, request):
        """Prunes/deletes a memory node from SQLite WAL storage."""
        try:
            data = await request.json()
            node_type = str(data.get("type", "")).strip().lower()
            node_id = data.get("id")
            if node_id is None:
                return JSONResponse({"error": "Missing node id"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()

            if hasattr(mem, "delete_memory_node"):
                def _delete_node():
                    return mem.delete_memory_node(node_type, node_id)

                success = await anyio.to_thread.run_sync(_delete_node)
                if success:
                    return JSONResponse({"status": "deleted", "type": node_type, "id": node_id})
                return JSONResponse({"error": "Record not found or failed to delete"}, status_code=404)
            return JSONResponse({"error": "Memory manager does not support node pruning"}, status_code=501)
        except Exception as e:
            logger.error("Error deleting memory node: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_memory_update(self, request):
        """Updates or inscribes a memory node into SQLite WAL storage."""
        try:
            data = await request.json()
            node_type = str(data.get("type", "fact")).strip().lower()
            key = str(data.get("key", "")).strip()
            value = str(data.get("value", "")).strip()
            category = str(data.get("category", "")).strip()

            if not key or not value:
                return JSONResponse({"error": "Key and value are required"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()

            def _update_node():
                if node_type in ("preference", "user_preference"):
                    return mem.update_user_preference(key, value)
                cat = category or "general"
                return mem.remember_fact(key, value, category=cat)

            msg = await anyio.to_thread.run_sync(_update_node)

            return JSONResponse({"status": "success", "message": str(msg), "key": str(key), "value": str(value)})
        except Exception as e:
            logger.error("Error updating memory node: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    def handle_approval_request(self, request_details: dict[str, Any], timeout: float = 30.0) -> bool:
        """Presents an out-of-band authorization request to the user via WebSocket and HUD mission feed."""
        action_hash = request_details.get("action_hash", "")
        tool_name = request_details.get("tool_name", "")
        summary = request_details.get("summary", "")
        arguments = request_details.get("arguments", {})
        risk_level = request_details.get("risk_level", 2)
        expires_at = time.time() + timeout

        event = threading.Event()
        with self._approval_lock:
            self._pending_approvals[action_hash] = {
                "action_hash": action_hash,
                "tool_name": tool_name,
                "summary": summary,
                "arguments": arguments,
                "risk_level": risk_level,
                "expires_at": expires_at,
                "event": event,
                "decision": False,
            }

        # Broadcast approval request to connected HUD clients
        self.broadcast({
            "type": "approval_request",
            "data": {
                "action_hash": action_hash,
                "tool_name": tool_name,
                "summary": summary,
                "arguments": arguments,
                "risk_level": risk_level,
                "expires_at": expires_at,
            },
        })
        self.broadcast_message(
            "system",
            f"⚠️ Authorization Required: {summary}"
        )

        logger.info(
            "Awaiting user approval for action '%s' (hash: %s, timeout: %.1fs)...",
            tool_name,
            action_hash[:12] if action_hash else "unknown",
            timeout,
        )
        event.wait(timeout=timeout)

        with self._approval_lock:
            rec = self._pending_approvals.pop(action_hash, None)
            if rec and rec["event"].is_set():
                decision = bool(rec["decision"])
                logger.info("User approval outcome for '%s': %s", tool_name, "APPROVED" if decision else "DENIED")
                return decision
            logger.warning("Approval request for '%s' timed out after %.1fs (fail closed).", tool_name, timeout)
            return False

    def submit_approval_decision(self, action_hash: str, approved: bool) -> bool:
        """Submits human decision (approve or deny) for an in-flight approval request."""
        with self._approval_lock:
            rec = self._pending_approvals.get(action_hash)
            if rec and not rec["event"].is_set():
                rec["decision"] = bool(approved)
                rec["event"].set()
                return True
            return False

    async def _handle_approval_pending(self, request):
        """Lists active pending approval requests for HUD prompt presentation."""
        now = time.time()
        with self._approval_lock:
            pending = [
                {
                    "action_hash": h,
                    "tool_name": r["tool_name"],
                    "summary": r["summary"],
                    "arguments": r["arguments"],
                    "risk_level": r["risk_level"],
                    "expires_in": max(0.0, r["expires_at"] - now),
                }
                for h, r in self._pending_approvals.items()
                if r["expires_at"] > now and not r["event"].is_set()
            ]
        return JSONResponse({"pending": pending})

    async def _handle_approval_respond(self, request):
        """Submits human authorization decision for a pending action hash."""
        try:
            body = await request.json()
            action_hash = str(body.get("action_hash", "")).strip()
            decision_raw = str(body.get("decision", "deny")).strip().lower()
            approved = decision_raw in ("approve", "approved", "true", "yes", "1")
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not action_hash:
            return JSONResponse({"error": "action_hash is required"}, status_code=400)

        success = self.submit_approval_decision(action_hash, approved)
        if success:
            decision_text = "APPROVED" if approved else "DENIED"
            self.broadcast_message(
                "system",
                f"Action {decision_text} by user for hash {action_hash[:8]}..."
            )
            return JSONResponse({"status": "recorded", "action_hash": action_hash, "approved": approved})
        return JSONResponse({"error": "Pending approval request not found or expired"}, status_code=404)

    async def _handle_approval_grant(self, request):
        """Allows authenticated human operators to issue an explicit out-of-band grant in advance."""
        try:
            body = await request.json()
            tool_name = str(body.get("tool_name", "")).strip()
            args = body.get("arguments", {})
            ttl = float(body.get("ttl_seconds", 60.0))
            summary = str(body.get("summary", "")).strip()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not tool_name:
            return JSONResponse({"error": "tool_name is required"}, status_code=400)

        from jarvis.core.security_boundaries import DualKeyApprovalManager
        mgr = DualKeyApprovalManager.get_instance()
        try:
            auth = mgr.grant_approval(tool_name, args, ttl_seconds=ttl, summary=summary)
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
        return JSONResponse({
            "status": "granted",
            "tool_name": auth.tool_name,
            "action_hash": auth.action_hash,
            "expires_at": auth.expires_at,
            "summary": auth.summary,
        })

    def _get_unlock_html(self, nonce: str = "") -> str:
        """Minimal unauthenticated interface lock page with zero token disclosure."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>JARVIS 2.0 &mdash; Interface Locked</title>
  <style nonce="{{ csp_nonce }}">
    body { background: #070b12; color: #00d2ff; font-family: 'Segoe UI', system-ui, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: rgba(13, 22, 38, 0.95); border: 1px solid rgba(0, 210, 255, 0.3); border-radius: 8px; padding: 32px; width: 380px; text-align: center; box-shadow: 0 0 30px rgba(0, 210, 255, 0.15); }
    h2 { margin: 0 0 8px; letter-spacing: 2px; font-weight: 600; text-shadow: 0 0 10px #00d2ff; }
    p { color: #8ba2b9; font-size: 13px; margin: 0 0 24px; }
    input { width: 100%; box-sizing: border-box; background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(0, 210, 255, 0.4); color: #fff; padding: 10px 12px; border-radius: 4px; font-size: 14px; margin-bottom: 16px; outline: none; }
    input:focus { border-color: #00d2ff; box-shadow: 0 0 8px rgba(0, 210, 255, 0.4); }
    button { width: 100%; background: #00d2ff; color: #070b12; border: none; padding: 10px; border-radius: 4px; font-weight: 700; cursor: pointer; font-size: 14px; letter-spacing: 1px; transition: background 0.2s; }
    button:hover { background: #5ce1e6; }
    .error { color: #ff5252; font-size: 12px; margin-top: 12px; display: none; }
  </style>
</head>
<body>
  <div class="card">
    <h2>J.A.R.V.I.S.</h2>
    <p>Sovereign Interface Locked. Provide session credentials or unlock token to authenticate.</p>
    <form id="unlockForm">
      <input type="password" id="tokenInput" placeholder="Session or Bootstrap Token" autocomplete="off" required autofocus>
      <button type="submit">AUTHENTICATE</button>
      <div id="errMsg" class="error">Invalid credentials</div>
    </form>
  </div>
  <script nonce="{{ csp_nonce }}">
    window.addEventListener('DOMContentLoaded', async () => {
      const unlockForm = document.getElementById('unlockForm');
      if (unlockForm) {
        unlockForm.addEventListener('submit', unlock);
      }

      let token = '';
      if (window.location.hash.startsWith('#bootstrap=')) {
        token = window.location.hash.substring('#bootstrap='.length).trim();
      } else if (window.location.hash.startsWith('#token=')) {
        token = window.location.hash.substring('#token='.length).trim();
      }
      if (token) {
        if (window.history.replaceState) {
          window.history.replaceState(null, '', window.location.pathname);
        }
        try {
          const res = await fetch('/api/auth/unlock', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({token})
          });
          if (res.ok) {
            window.location.replace('/');
            return;
          }
        } catch (ex) {}
        const errEl = document.getElementById('errMsg');
        if (errEl) errEl.style.display = 'block';
      }
    });

    async function unlock(e) {
      e.preventDefault();
      const tokenInput = document.getElementById('tokenInput');
      const token = tokenInput ? tokenInput.value.trim() : '';
      const err = document.getElementById('errMsg');
      if (err) err.style.display = 'none';
      try {
        const res = await fetch('/api/auth/unlock', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({token})
        });
        if (res.ok) {
          window.location.reload();
        } else {
          if (err) err.style.display = 'block';
        }
      } catch (ex) {
        if (err) err.style.display = 'block';
      }
    }
  </script>
</body>
</html>"""
        return render_template_with_nonce(html, nonce)

    async def _handle_unlock(self, request):
        """Exchanges a single-use bootstrap token or session token for a session cookie."""
        try:
            body = await request.json()
            token = str(body.get("token", "")).strip()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        if not token:
            return JSONResponse({"error": "Token required"}, status_code=400)

        is_valid = secrets.compare_digest(token, _HUD_SESSION_TOKEN) or consume_bootstrap_token(token)
        if is_valid:
            resp = JSONResponse({"status": "authenticated"})
            is_secure = is_browser_facing_https(request, self)
            resp.set_cookie(key="jarvis_session", value=_HUD_SESSION_TOKEN, httponly=True, samesite="strict", secure=is_secure, path="/")
            return resp
        return JSONResponse({"error": "Unauthorized: Invalid token"}, status_code=401)

    async def _handle_csp_report(self, request):
        """Ingests, bounds, deduplicates, and logs Content Security Policy violation reports."""
        if request.method != "POST":
            return JSONResponse({"error": "Method Not Allowed"}, status_code=405)

        # 1. Content-Type validation
        content_type = request.headers.get("content-type", "").lower().split(";")[0].strip()
        allowed_types = {"application/csp-report", "application/reports+json", "application/json"}
        if content_type not in allowed_types:
            return JSONResponse(
                {"error": "Unsupported Media Type: expected application/csp-report or application/reports+json"},
                status_code=415,
            )

        # 2. Strict body size bounding while consuming stream (anti-DoS, max 32KB)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                cl_val = int(content_length)
                if cl_val < 0:
                    return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
                if cl_val > 32768:
                    return JSONResponse({"error": "Payload Too Large: max 32KB"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)

        chunks = []
        total_bytes = 0
        max_bytes = 32768
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                return JSONResponse({"error": "Payload Too Large: max 32KB"}, status_code=413)
            chunks.append(chunk)

        body_bytes = b"".join(chunks)
        if not body_bytes:
            return JSONResponse({"error": "Empty report body"}, status_code=400)

        # 3. JSON parsing & primitive rejection
        try:
            raw_payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        if not isinstance(raw_payload, (dict, list)):
            return JSONResponse({"error": "Invalid JSON: expected object or array"}, status_code=400)

        # 4. Extract reports (CSP Level 2 and Reporting API Level 3 formats)
        reports_to_process = []
        if isinstance(raw_payload, dict):
            if "csp-report" in raw_payload:
                if isinstance(raw_payload["csp-report"], dict):
                    reports_to_process.append(raw_payload["csp-report"])
                else:
                    return JSONResponse({"error": "Invalid CSP report structure"}, status_code=400)
            else:
                reports_to_process.append(raw_payload)
        elif isinstance(raw_payload, list):
            for item in raw_payload[:10]:
                if isinstance(item, dict):
                    if item.get("type") == "csp-violation" and isinstance(item.get("body"), dict):
                        reports_to_process.append(item["body"])
                    else:
                        reports_to_process.append(item)

        if not reports_to_process:
            return JSONResponse({"error": "No valid CSP reports found in payload"}, status_code=400)

        # 5. Rate limiting (max 60 reports per minute across individual reports in batches)
        num_reports = len(reports_to_process)
        now = time.monotonic()
        with self._csp_report_lock:
            while self._csp_report_timestamps and (now - self._csp_report_timestamps[0] > 60.0):
                self._csp_report_timestamps.popleft()
            if len(self._csp_report_timestamps) + num_reports > 60:
                return JSONResponse({"error": "Too Many Requests: CSP report rate limit exceeded"}, status_code=429)
            for _ in range(num_reports):
                self._csp_report_timestamps.append(now)

            # Purge expired deduplication keys (>30s)
            stale_keys = [k for k, exp in self._csp_dedup_cache.items() if now > exp]
            for k in stale_keys:
                del self._csp_dedup_cache[k]

        # 6. Sanitize, deduplicate, and record
        for report in reports_to_process:
            if not isinstance(report, dict):
                continue

            def _clean(val: Any, max_len: int = 256) -> str:
                if val is None:
                    return ""
                s = str(val).replace("\r", " ").replace("\n", " ").strip()
                # Strict printable ASCII (32-126) eliminates CRLF log injection and escape sequences
                s = "".join(c for c in s if 32 <= ord(c) <= 126)
                return s[:max_len]

            blocked_uri = _clean(report.get("blocked-uri") or report.get("blockedURL") or report.get("blocked_url"))
            violated_directive = _clean(report.get("violated-directive") or report.get("violatedDirective") or report.get("directive"), 128)
            effective_directive = _clean(report.get("effective-directive") or report.get("effectiveDirective"), 128)
            document_uri = _clean(report.get("document-uri") or report.get("documentURL") or report.get("url"))
            disposition = _clean(report.get("disposition"), 32) or "report"

            dedup_key = f"{violated_directive}:{effective_directive}:{blocked_uri}:{document_uri}"
            with self._csp_report_lock:
                if dedup_key in self._csp_dedup_cache:
                    continue
                self._csp_dedup_cache[dedup_key] = now + 10.0

                entry = {
                    "timestamp": time.time(),
                    "disposition": disposition,
                    "violated_directive": violated_directive,
                    "effective_directive": effective_directive,
                    "blocked_uri": blocked_uri,
                    "document_uri": document_uri,
                }
                self._csp_violations_ring.append(entry)

            logger.warning(
                "CSP Violation [%s]: directive '%s' (effective: '%s') blocked '%s' on '%s'",
                disposition,
                violated_directive or effective_directive,
                effective_directive,
                blocked_uri,
                document_uri,
            )

        return Response(status_code=204)

    async def _handle_index(self, request):
        if not self.template_path.exists():
            resp = HTMLResponse("<h3>HUD template not found.</h3>", status_code=404)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

        cookie_token = request.cookies.get("jarvis_session")
        header_token = request.headers.get("x-jarvis-token")

        # Zero-trust: only valid session cookies or explicit header credentials authenticate
        # Query-string bootstrap tokens are NEVER accepted or consumed here
        is_auth = (
            bool(cookie_token and secrets.compare_digest(cookie_token, _HUD_SESSION_TOKEN))
            or bool(header_token and secrets.compare_digest(header_token, _HUD_SESSION_TOKEN))
        )

        nonce = getattr(getattr(request, "state", None), "csp_nonce", None)
        if not nonce:
            nonce = generate_csp_nonce()
            if hasattr(request, "state"):
                request.state.csp_nonce = nonce

        if not is_auth and self.enforce_auth:
            resp = HTMLResponse(self._get_unlock_html(nonce=nonce), status_code=200)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

        html_content = self.template_path.read_text(encoding="utf-8")
        rendered_html = render_template_with_nonce(html_content, nonce)
        response = HTMLResponse(rendered_html)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        is_secure = is_browser_facing_https(request, self)
        response.set_cookie(
            key="jarvis_session",
            value=_HUD_SESSION_TOKEN,
            httponly=True,
            samesite="strict",
            secure=is_secure,
            path="/",
        )
        return response

    async def _handle_status(self, request):
        data = self._gather_telemetry()
        return JSONResponse(data)

    async def _handle_command(self, request):
        try:
            body = await request.json()
            cmd_text = body.get("command", "").strip()
        except Exception:
            cmd_text = ""

        if not cmd_text:
            return JSONResponse({"error": "Empty command"}, status_code=400)

        # Dispatch command asynchronously to orchestrator (orchestrator broadcasts message to all clients)
        if self.orchestrator:
            threading.Thread(target=self.orchestrator.handle_command, args=(cmd_text,), daemon=True).start()
            return JSONResponse({"status": "queued", "command": cmd_text})

        return JSONResponse({"error": "Orchestrator not connected"}, status_code=503)

    async def _handle_action(self, request):
        try:
            body = await request.json()
            action = body.get("action", "")
        except Exception:
            action = ""

        if action == "set_volume":
            from jarvis.skills.os_control import adjust_volume
            percent = int(body.get("percent", 50))
            adjust_volume(action="set", percent=percent)
            self.broadcast_message("jarvis", f"Master volume set to {percent}%, Sir.")
            return JSONResponse({"status": "success", "percent": percent})

        elif action == "toggle_mute":
            if self.orchestrator:
                is_muted = self.orchestrator.toggle_mute()
                self.broadcast_telemetry({"is_muted": is_muted})
                return JSONResponse({"is_muted": is_muted})

        elif action == "launch_private_browser":
            try:
                from jarvis.skills.os_control import open_browser_tab, press_hotkey
                press_hotkey("ctrl+shift+n")
            except Exception:
                try:
                    open_browser_tab("about:blank", browser="brave")
                except Exception:
                    pass
            self.broadcast_message("jarvis", "Private browser window launched, Sir.")
            return JSONResponse({"status": "success", "action": "private_browser"})

        elif action == "focus_ide":
            try:
                from jarvis.skills.os_control import launch_or_focus_application
                res = launch_or_focus_application("Visual Studio Code")
                self.broadcast_message("jarvis", res)
            except Exception as e:
                res = f"Failed to focus IDE: {e}"
            return JSONResponse({"status": "success", "message": res})

        elif action == "lock_workstation":
            try:
                from jarvis.skills.os_control import lock_workstation
                res = lock_workstation()
                self.broadcast_message("jarvis", res)
            except Exception as e:
                res = f"Failed to lock workstation: {e}"
            return JSONResponse({"status": "success", "message": res})

        elif action == "optimize_pc":
            if self.orchestrator:
                threading.Thread(target=self.orchestrator.handle_command, args=("Jarvis, optimize my PC",), daemon=True).start()
                self.broadcast_message("jarvis", "Initiating workstation memory and cache optimization, Sir.")
            return JSONResponse({"status": "queued", "action": "optimize_pc"})

        return JSONResponse({"error": f"Unknown action '{action}'"}, status_code=400)

    async def _handle_mute(self, request):
        if self.orchestrator:
            is_muted = self.orchestrator.toggle_mute()
            self.broadcast_telemetry({"is_muted": is_muted})
            return JSONResponse({"is_muted": is_muted})
        return JSONResponse({"error": "Orchestrator not connected"}, status_code=503)

    async def _handle_notes(self, request):
        """Returns recent captured notes from data/notes/notes.md for HUD drawer."""
        notes_path = Path("data/notes/notes.md")
        if not notes_path.exists():
            return JSONResponse({"notes": []})
        try:
            raw = notes_path.read_text(encoding="utf-8")
            blocks = [b.strip() for b in raw.split("### ") if b.strip() and not b.startswith("# Jarvis")]
            notes_list = []
            for block in reversed(blocks[-15:]):
                lines = block.splitlines()
                header = lines[0].strip() if lines else "General"
                parts = header.split(" | ")
                dt_str = parts[0].strip() if len(parts) > 1 else header
                cat = parts[1].strip() if len(parts) > 1 else "General"
                body = " ".join([l.lstrip("-•* ").strip() for l in lines[1:] if l.strip()])
                notes_list.append({"timestamp": dt_str, "category": cat, "content": body})
            return JSONResponse({"notes": notes_list})
        except Exception as e:
            logger.warning(f"Error reading notes for HUD: {e}")
            return JSONResponse({"error": str(e), "notes": []})

    async def _handle_timers(self, request):
        """Returns live countdown timers for HUD drawer."""
        try:
            from jarvis.skills.timers import _active_timers, _timer_lock
            now = time.time()
            timers_list = []
            with _timer_lock:
                for t_info in _active_timers.values():
                    rem = max(0, int(t_info.get("end_time", now) - now))
                    timers_list.append({
                        "id": t_info.get("id", ""),
                        "label": t_info.get("label", "Timer"),
                        "duration": t_info.get("duration", 0),
                        "remaining": rem,
                    })
            return JSONResponse({"timers": timers_list})
        except Exception as e:
            logger.warning(f"Error reading timers for HUD: {e}")
            return JSONResponse({"timers": []})

    async def _handle_documents_upload(self, request):
        """Receives a PDF document from the file explorer upload dialog and stages it."""
        try:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file uploaded"}, status_code=400)

            filename = getattr(file_field, "filename", "uploaded_doc.pdf")
            if not filename.lower().endswith(".pdf"):
                return JSONResponse({"error": "Only PDF documents are supported currently"}, status_code=400)

            base_dir = Path(__file__).resolve().parent.parent.parent
            intake_dir = base_dir / "data" / "intake"
            intake_dir.mkdir(parents=True, exist_ok=True)

            target_path = intake_dir / filename
            content = await file_field.read()
            target_path.write_bytes(content)

            import hashlib
            file_hash = hashlib.sha256(content).hexdigest()
            file_size = len(content)

            doc_info = {"id": 0, "filename": filename, "status": "PENDING_CONTEXT"}
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                doc_info = self.orchestrator.memory.stage_document(
                    filename=filename,
                    file_path=str(target_path),
                    file_hash=file_hash,
                    file_size=file_size
                )

            # Broadcast WebSocket notification
            self.broadcast({
                "type": "document_staged",
                "filename": filename,
                "id": doc_info.get("id"),
                "size_kb": round(file_size / 1024, 1),
                "message": f"Staged '{filename}'. Standing by for Sir's briefing."
            })

            if self.orchestrator and hasattr(self.orchestrator, "sfx"):
                try:
                    self.orchestrator.sfx.play_alert()
                except Exception:
                    pass

            return JSONResponse({
                "status": "staged",
                "id": doc_info.get("id"),
                "filename": filename,
                "size_kb": round(file_size / 1024, 1),
                "message": f"Document '{filename}' staged successfully. Awaiting your briefing, Sir."
            })
        except Exception as e:
            logger.error(f"Error handling document upload: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_staged_documents(self, request):
        """Returns all documents waiting in the staged queue."""
        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            staged = self.orchestrator.memory.get_staged_documents(status="PENDING_CONTEXT")
            return JSONResponse({"staged": staged})
        return JSONResponse({"staged": []})

    async def _handle_calendar(self, request):
        """Returns upcoming calendar appointments, grouped schedule matrix, and saved calendar documents."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            events = mem.get_calendar_events(include_cancelled=False) if mem else []
            saved_cals = mem.get_saved_calendars() if mem and hasattr(mem, "get_saved_calendars") else []

            # Group events chronologically by date
            import datetime
            import re
            from collections import defaultdict
            grouped = defaultdict(list)
            for e in events:
                st = e.get("start_time", "")
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", st)
                if m:
                    d_str = m.group(1)
                    try:
                        dt = datetime.datetime.strptime(d_str, "%Y-%m-%d")
                        day_name = dt.strftime("%A")
                        day_formatted = dt.strftime("%A, %B %d")
                    except Exception:
                        day_name = "Other"
                        day_formatted = d_str
                    grouped[d_str].append({
                        **e,
                        "day_name": day_name,
                        "day_formatted": day_formatted,
                    })
                else:
                    grouped["unscheduled"].append({
                        **e,
                        "day_name": "Other",
                        "day_formatted": "Other",
                    })

            return JSONResponse({
                "events": events,
                "saved_calendars": saved_cals,
                "grouped": dict(grouped)
            })
        except Exception as e:
            logger.error(f"Error handling calendar request: {e}")
            return JSONResponse({"events": [], "saved_calendars": [], "grouped": {}, "error": str(e)})

    async def _handle_calendar_delete(self, request):
        """Deletes a saved calendar document and optionally clears all schedule events."""
        try:
            body = await request.json()
            doc_id = int(body.get("doc_id", 0))
            clear_events = bool(body.get("clear_events", False))
            if not doc_id:
                return JSONResponse({"error": "Missing doc_id"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            doc_title = f"Calendar #{doc_id}"
            if mem:
                with mem._get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT title FROM documents WHERE id = ?", (doc_id,))
                    row = cur.fetchone()
                    if row:
                        doc_title = row["title"]

            success = mem.delete_saved_calendar(doc_id) if mem else False
            events_cleared = 0
            if clear_events and mem:
                events_cleared = mem.clear_calendar_events()

            self.broadcast({
                "type": "calendar_deleted",
                "doc_id": doc_id,
                "title": doc_title,
                "events_cleared": events_cleared,
                "message": f"Calendar '{doc_title}' deleted successfully, Sir."
            })

            return JSONResponse({
                "status": "deleted" if success else "not_found",
                "doc_id": doc_id,
                "title": doc_title,
                "events_cleared": events_cleared,
                "message": f"Calendar '{doc_title}' has been deleted."
            })
        except Exception as e:
            logger.error(f"Error deleting calendar: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_calendar_event_delete(self, request):
        """Deletes an individual calendar event by numerical ID."""
        try:
            body = await request.json()
            event_id = int(body.get("event_id", 0))
            if not event_id:
                return JSONResponse({"error": "Missing event_id"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            success = mem.delete_calendar_event(event_id) if mem else False

            self.broadcast({
                "type": "calendar_event_deleted",
                "event_id": event_id,
                "message": f"Calendar event #{event_id} deleted."
            })

            return JSONResponse({
                "status": "deleted" if success else "not_found",
                "event_id": event_id
            })
        except Exception as e:
            logger.error(f"Error deleting calendar event: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_calendar_clear(self, request):
        """Purges all active calendar events from memory."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            count = mem.clear_calendar_events() if mem else 0

            self.broadcast({
                "type": "calendar_cleared",
                "count": count,
                "message": f"Active schedule cleared ({count} events purged), Sir."
            })

            return JSONResponse({
                "status": "cleared",
                "count": count,
                "message": f"Cleared {count} calendar events."
            })
        except Exception as e:
            logger.error(f"Error clearing calendar events: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_websocket(self, websocket: WebSocket):
        # 1. Validate Host and Origin headers with exact scheme, host, and port matching
        req_scheme = "https" if websocket.url.scheme in ("https", "wss") else "http"
        is_valid, err_msg, _ = validate_request_host_and_origin(
            headers=websocket.headers,
            service=self,
            request_scheme=req_scheme,
        )
        if not is_valid:
            await websocket.close(code=1008, reason=err_msg)
            return

        # 2. Check token authentication (cookie or x-jarvis-token header)
        token = (
            websocket.headers.get("x-jarvis-token")
            or websocket.cookies.get("jarvis_session")
        )
        is_authenticated = bool(token and secrets.compare_digest(token, _HUD_SESSION_TOKEN))
        enforce_auth = getattr(self, "enforce_auth", True)

        if not is_authenticated and enforce_auth:
            await websocket.close(code=1008, reason="Unauthorized: Missing or invalid token")
            return

        accepted = await self.telemetry_manager.connect(websocket)
        if not accepted:
            return

        # Send initial status asynchronously off-loop
        try:
            init_telemetry = await anyio.to_thread.run_sync(
                WorkstationMetricsCollector.sample_metrics_sync, self.orchestrator
            )
            await websocket.send_text(json.dumps({"type": "telemetry", **init_telemetry}, separators=(",", ":")))
        except Exception:
            pass

        try:
            while True:
                data_text = await websocket.receive_text()
                try:
                    payload = json.loads(data_text)
                    msg_type = payload.get("type", "")

                    if msg_type == "command":
                        cmd = payload.get("text", "").strip()
                        if cmd and self.orchestrator:
                            self._command_executor.submit(self.orchestrator.handle_command, cmd)

                    elif msg_type == "action":
                        action = payload.get("action", "")
                        if action == "toggle_mute" and self.orchestrator:
                            is_muted = self.orchestrator.toggle_mute()
                            self.broadcast_telemetry({"is_muted": is_muted, "m": 1 if is_muted else 0})
                        elif action == "set_volume":
                            from jarvis.skills.os_control import adjust_volume
                            percent = int(payload.get("percent", 50))
                            adjust_volume(action="set", percent=percent)
                            self.broadcast_message("jarvis", f"Master volume set to {percent}%, Sir.")

                    elif msg_type == "approval_response":
                        action_hash = str(payload.get("action_hash", "")).strip()
                        decision_raw = str(payload.get("decision", "deny")).strip().lower()
                        approved = decision_raw in ("approve", "approved", "true", "yes", "1")
                        self.submit_approval_decision(action_hash, approved)

                except Exception as e:
                    logger.warning(f"Error handling HUD WebSocket message: {e}")

        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await self.telemetry_manager.disconnect(websocket)

    def _gather_telemetry(self) -> dict[str, Any]:
        """Collects current hardware and model telemetry without blocking."""
        return WorkstationMetricsCollector.sample_metrics_sync(self.orchestrator)

    def broadcast(self, payload: dict[str, Any]):
        """Thread-safe broadcast of a JSON payload to all connected clients."""
        if not self.telemetry_manager.client_count or not self._loop:
            return

        message = json.dumps(payload, separators=(",", ":"))
        coro = self.telemetry_manager.broadcast_direct(message)
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as e:
            coro.close()
            logger.debug("Failed scheduling broadcast_direct on loop: %s", e)
        except Exception as e:  # noqa: BLE001 - Guard UI thread against scheduling anomalies
            coro.close()
            logger.debug("Unexpected error scheduling broadcast_direct: %s", e)

    def broadcast_state(self, state: str):
        """Broadcasts voice state change ('idle', 'listening', 'thinking', 'speaking')."""
        self.broadcast({"type": "state", "state": state, "st": state})

    def broadcast_message(self, sender: str, text: str):
        """Broadcasts a dialogue message to the live mission feed."""
        self.broadcast({
            "type": "chat",
            "sender": sender,
            "text": text,
            "timestamp": time.strftime("%H:%M:%S"),
        })

    def broadcast_telemetry(self, extra: dict[str, Any] | None = None):
        """Broadcasts telemetry update."""
        base = self._gather_telemetry()
        if extra:
            base.update(extra)
        if self._loop and self.telemetry_manager.client_count:
            try:
                self._loop.call_soon_threadsafe(self.telemetry_manager.broadcast_telemetry, base)
            except RuntimeError as e:
                logger.debug("Failed scheduling broadcast_telemetry on closed loop: %s", e)
            except Exception as e:  # noqa: BLE001 - Guard UI thread against scheduling anomalies
                logger.debug("Unexpected error scheduling broadcast_telemetry: %s", e)

    def broadcast_audio_level(self, level: float):
        """Broadcasts audio reactivity level (0.0 to 1.0)."""
        self.broadcast({"type": "audio_level", "level": max(0.0, min(1.0, level))})

    def broadcast_dsp_frame(self, dsp_frame: bytes):
        """Thread-safe broadcast of 68-byte DSP binary frame to connected HUD clients."""
        if not self.telemetry_manager.client_count or not self._loop:
            return
        coro = self.telemetry_manager.broadcast_binary(dsp_frame)
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as e:
            coro.close()
            logger.debug("Failed scheduling broadcast_binary on loop: %s", e)
        except Exception as e:  # noqa: BLE001 - Guard UI thread against scheduling anomalies
            coro.close()
            logger.debug("Unexpected error scheduling broadcast_binary: %s", e)

    def start(self, timeout: float = 10.0) -> bool:
        """Starts the Uvicorn server in a background daemon thread with continuous live telemetry streaming."""
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            allow_remote = os.environ.get("JARVIS_ALLOW_REMOTE") == "1"
            has_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL") == "1"
            if not (allow_remote and has_tunnel):
                raise RuntimeError(
                    f"Refusing to bind Jarvis UI to non-loopback interface '{self.host}' "
                    "without an encrypted transport or secured tunnel (set JARVIS_ALLOW_REMOTE=1 and JARVIS_SECURE_TUNNEL=1)."
                )

        if self._thread and self._thread.is_alive():
            if self._ready_event.is_set():
                return True
            return self._ready_event.wait(timeout=timeout)

        async def _telemetry_broadcast_loop():
            # Prime CPU counter baseline
            try:
                psutil.cpu_percent(interval=None)
            except (AttributeError, OSError) as err:
                logger.debug("CPU baseline query notice: %s", err)

            while self._server and not self._server.should_exit:
                try:
                    await asyncio.sleep(1.0)
                    if self.telemetry_manager.client_count:
                        # Offload synchronous system queries to worker thread: ZERO event-loop blocking
                        metrics = await anyio.to_thread.run_sync(
                            WorkstationMetricsCollector.sample_metrics_sync, self.orchestrator
                        )
                        self.telemetry_manager.broadcast_telemetry(metrics)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Telemetry broadcast loop notice: {e}")

        async def _serve_all():
            telemetry_task = self._loop.create_task(_telemetry_broadcast_loop())

            async def _notify_ready():
                for _ in range(100):
                    if getattr(self._server, "started", False):
                        break
                    await asyncio.sleep(0.02)
                self._ready_event.set()

            ready_task = self._loop.create_task(_notify_ready())
            try:
                await self._server.serve()
            finally:
                self._ready_event.set()
                ready_task.cancel()
                telemetry_task.cancel()
                try:
                    await telemetry_task
                except asyncio.CancelledError:
                    pass

        def _run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            attach_access_log_redaction_filters()
            config = uvicorn.Config(
                app=self._create_app(),
                host=self.host,
                port=self.port,
                access_log=bool(getattr(self, "access_log", False) or os.environ.get("JARVIS_UI_ACCESS_LOG") == "1"),
                log_level="info" if (getattr(self, "access_log", False) or os.environ.get("JARVIS_UI_ACCESS_LOG") == "1") else "warning",
                loop="asyncio",
            )
            self._server = uvicorn.Server(config)
            logger.info(f"Starting Jarvis HUD Web Server at http://{self.host}:{self.port}...")
            try:
                self._loop.run_until_complete(_serve_all())
            finally:
                try:
                    pending = asyncio.all_tasks(self._loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except (asyncio.CancelledError, RuntimeError) as exc:
                    logger.debug("Loop task cleanup notice: %s", exc)
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except (asyncio.CancelledError, RuntimeError) as exc:
                    logger.debug("Asyncgen shutdown notice: %s", exc)
                if not self._loop.is_closed():
                    self._loop.close()

        self._ready_event.clear()
        self._stopped_event.clear()
        self._thread = threading.Thread(target=_run_server, daemon=True, name="Jarvis-HUD-Server")
        self._thread.start()
        is_ready = self._ready_event.wait(timeout=timeout)
        if not is_ready:
            logger.warning("Jarvis HUD Web Server did not signal readiness within %.1fs", timeout)
        return is_ready

    def stop(self, timeout: float = 5.0) -> bool:
        """Stops the Uvicorn server cleanly and shuts down native NVML session."""
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Jarvis HUD Web Server thread did not terminate within %.1fs", timeout)
                return False
            self._thread = None
        if hasattr(self, "_command_executor") and self._command_executor:
            try:
                self._command_executor.shutdown(wait=False)
            except Exception:
                pass
        self._server = None
        self._stopped_event.set()
        try:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(None)
        except (ImportError, AttributeError) as exc:
            logger.debug("Approval handler teardown notice: %s", exc)
        WorkstationMetricsCollector.shutdown_nvml()
        return True


async def broadcast_ambient_telemetry(type: str, data: dict[str, Any]):
    """Module-level helper to dispatch ambient telemetry to the active HUD server."""
    instance = JarvisUIService.get_instance()
    if instance:
        instance.broadcast({"type": type, "data": data, "timestamp": time.strftime("%H:%M:%S")})
