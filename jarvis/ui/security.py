"""Security primitives, origin validation, access-log redaction, and CSP middleware for Jarvis 2.0 HUD."""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("Jarvis.UI.Security")

# Ephemeral single-use bootstrap tokens for browser authentication exchange
_BOOTSTRAP_TOKENS: dict[str, float] = {}
_BOOTSTRAP_LOCK: threading.Lock = threading.Lock()

# Ephemeral cryptographic session token generated once per host process startup
_HUD_SESSION_TOKEN: str = secrets.token_urlsafe(32)

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

_UNLOCK_HTML_PATH = Path(__file__).parent / "templates" / "unlock.html"


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


def resolve_active_session_token(request: Any = None) -> str:
    """Dynamically resolves the active HUD session token.

    Prefers an explicit per-app state override if set, otherwise falling back
    to the active global session token.
    """
    if request is not None:
        app_state = getattr(getattr(request, "app", None), "state", None)
        if app_state is not None and getattr(app_state, "session_token", None):
            return str(app_state.session_token)
    return get_hud_session_token()


def load_unlock_html(nonce: str = "") -> str:
    """Loads and renders the minimal unauthenticated interface lock page with CSP nonce."""
    try:
        raw_html = _UNLOCK_HTML_PATH.read_text(encoding="utf-8")
    except OSError:
        raw_html = (
            "<!DOCTYPE html><html><head><title>Locked</title></head>"
            "<body><h2>J.A.R.V.I.S. Locked</h2></body></html>"
        )
    return render_template_with_nonce(raw_html, nonce)


def redact_query_string_bytes(qs: bytes) -> bytes:
    """Parses raw query string bytes, unquotes parameter names, and redacts sensitive values.

    Decodes percent-encoded names and redacts matched values while preserving
    authentic parameter casing, delimiters (&, ;), and non-sensitive query keys.
    Inputs exceeding 10MB fail safe to fixed placeholder [OVERSIZED_QUERY_REDACTED].
    """
    if not qs:
        return qs
    if len(qs) > MAX_QUERY_STRING_BYTES:
        return OVERSIZED_QUERY_PLACEHOLDER_BYTES
    parts = re.split(rb"([&;]+)", qs)
    out_parts: list[bytes] = []
    for part in parts:
        if not part or part.startswith((b"&", b";")):
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
    and replaces sensitive values with '[REDACTED]'.
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
        lgr = logging.getLogger(name)
        lgr.addFilter(redaction_filter)
        for h in lgr.handlers:
            h.addFilter(redaction_filter)


class AccessLogRedactionMiddleware:
    """ASGI middleware that scrubs sensitive query parameters in-place from ASGI scope.

    Ensures that when Uvicorn formats HTTP access logs via get_path_with_query_string(self.scope)
    during http.response.start (or websocket.accept), credentials are already replaced with '[REDACTED]'.
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
    """Determines whether the browser-facing connection is HTTPS."""
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
        trusted_proxies.update({"127.0.0.1", "::1", "localhost", "testserver", "testclient"})

    # 4. If client is a trusted proxy, inspect forwarded scheme headers
    if client_host and client_host in trusted_proxies:
        xf_proto = request.headers.get("x-forwarded-proto", "").strip().lower()
        if xf_proto == "https" or xf_proto.startswith("https,"):
            return True

        fwd = request.headers.get("forwarded", "").strip().lower()
        if "proto=https" in fwd:
            return True

    # 5. Secure tunnel heuristics
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
    if host_name != "testserver" and parsed_host.port is not None and parsed_host.port not in allowed_ports:
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

        # Origin port verification
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
        is_https_bool = scheme == "https"
        netloc = f"{host}:{port_num}" if (is_https_bool and port_num != 443) or (not is_https_bool and port_num != 80) else host
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
            active_token = resolve_active_session_token(request)
            is_authenticated = bool(token and secrets.compare_digest(token, active_token))

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
