"""Chrome DevTools Protocol (CDP) transport, network guards, and port authorization."""
from __future__ import annotations

import json
import logging
import re as _re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from jarvis.skills.cdp_tabs import (
    CDPTabSession,
    prune_stale_tab_sessions,
    remove_tab_session,
)

logger = logging.getLogger("Jarvis.BrowserCDP.Transport")


def _get_browser_cdp_symbol(name: str, fallback: Any = None) -> Any:
    """Dynamically resolves a symbol from jarvis.skills.browser_cdp to support mock patching."""
    mod = sys.modules.get("jarvis.skills.browser_cdp")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


_CDP_PROBE_CACHE: dict[tuple[str, int], tuple[bool, float]] = {}
_CDP_CACHE_TTL_SEC: float = 3.0

# Safe internal new-tab pages that may be navigated via CDP.
_SAFE_INTERNAL_PAGES: frozenset[str] = frozenset({
    "about:blank", "about:newtab",
    "chrome://newtab", "chrome://newtab/",
    "brave://newtab", "brave://newtab/",
    "edge://newtab", "edge://newtab/",
})

# Destructive browser management URLs that must never be navigated via CDP.
_DESTRUCTIVE_CDP_URLS: tuple[str, ...] = (
    "chrome://restart", "chrome://quit", "chrome://kill", "chrome://crash",
    "chrome://settings/clearbrowserdata",
    "brave://restart", "brave://quit", "brave://kill", "brave://crash",
    "edge://restart", "edge://quit",
)


def get_blocked_localhost_ports() -> set[int]:
    """Retrieves ports reserved for Jarvis internal services and sensitive daemons that must be shielded from browser navigation."""
    ports: set[int] = {
        20129,  # Jarvis HUD / UI default port
        9222,   # Chrome DevTools Protocol port
        11434,  # Local Ollama daemon
        2375,   # Docker daemon HTTP
        2376,   # Docker daemon HTTPS
        5432,   # PostgreSQL
        6379,   # Redis
        6443,   # Kubernetes API server
    }
    try:
        from jarvis.core.config_loader import load_config
        cfg = load_config()
        p = cfg.get("ui", {}).get("port")
        if p and isinstance(p, int):
            ports.add(p)
    except (ImportError, OSError, KeyError, TypeError):
        pass
    try:
        from jarvis.ui.server import JarvisUIService
        srv = JarvisUIService.get_instance()
        if srv and hasattr(srv, "port"):
            ports.add(int(srv.port))
    except (ImportError, OSError, KeyError, TypeError):
        pass
    return ports


class NavigationRedirectValidator(urllib.request.HTTPRedirectHandler):
    """Custom HTTP redirect handler that validates every redirect hop for browser navigation.

    Enforces:
    - Hop count limit (maximum 4 hops)
    - Redirect loop detection
    - HTTPS to HTTP downgrade rejection
    - Revalidation of each hop against SSRF, blocked localhost ports, private RFC 1918,
      and link-local cloud metadata (169.254.169.254)
    - Stripping sensitive auth headers on cross-origin redirects
    """

    def __init__(
        self,
        initial_url: str,
        max_hops: int = 4,
        disallowed_ports: set[int] | None = None,
    ):
        super().__init__()
        self.initial_url = initial_url
        self.max_hops = max_hops
        ports_fn = _get_browser_cdp_symbol("get_blocked_localhost_ports", get_blocked_localhost_ports)
        self.disallowed_ports = disallowed_ports if disallowed_ports is not None else ports_fn()
        parsed = urllib.parse.urlsplit(initial_url)
        h = (parsed.hostname or "").lower()
        self.initial_is_local = h in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or h.endswith(".localhost")
        self.visited: set[str] = {initial_url}
        self.hops: int = 0
        self.final_url: str = initial_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.max_hops:
            from jarvis.core.security_boundaries import SecurityBoundaryViolation
            raise SecurityBoundaryViolation(f"Redirect limit exceeded (maximum {self.max_hops} hops).")

        resolved = urllib.parse.urljoin(req.full_url, newurl)
        old_parsed = urllib.parse.urlsplit(req.full_url)
        new_parsed = urllib.parse.urlsplit(resolved)
        old_scheme = (old_parsed.scheme or "").lower()
        new_scheme = (new_parsed.scheme or "").lower()

        from jarvis.core.security_boundaries import (
            SENSITIVE_AUTH_HEADERS,
            SecurityBoundaryViolation,
            resolve_and_validate_destination,
        )

        if new_scheme not in ("http", "https"):
            raise SecurityBoundaryViolation(f"Unsupported redirect scheme '{new_scheme}'.")

        # Disallow insecure redirect downgrade from HTTPS to HTTP
        if old_scheme == "https" and new_scheme == "http":
            raise SecurityBoundaryViolation(
                f"Insecure redirect downgrade from HTTPS to HTTP blocked: '{req.full_url}' -> '{resolved}'"
            )

        # Detect redirect loops
        if resolved in self.visited:
            raise SecurityBoundaryViolation(f"Redirect loop detected: '{resolved}' already visited in chain.")
        self.visited.add(resolved)

        # Validate destination hop through SSRF filter
        clean_url, _, pinned_ip = resolve_and_validate_destination(
            resolved,
            allowed_schemes=("http", "https"),
            allow_localhost=self.initial_is_local,
            disallowed_ports=self.disallowed_ports,
        )
        self.final_url = clean_url

        new_req = super().redirect_request(req, fp, code, msg, headers, clean_url)
        if new_req is None:
            return None

        # Cross-origin redirect: strip sensitive auth credentials
        origin_changed = (
            (old_parsed.hostname or "").lower() != (new_parsed.hostname or "").lower()
            or old_parsed.port != new_parsed.port
            or old_scheme != new_scheme
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

        new_req._pinned_ip = pinned_ip
        return new_req


def validate_browser_navigation_chain(
    url: str,
    timeout_sec: float = 2.0,
    max_hops: int = 4,
) -> tuple[bool, str, str | None]:
    """Pre-flight validates HTTP/HTTPS navigation destinations across their redirect chain.

    Traces server-side redirect hops (301, 302, 303, 307, 308) in Python up to max_hops.
    Verifies that no intermediate or terminal destination targets:
    - Blocked localhost ports (HUD, CDP, Ollama, Docker, DB, K8s)
    - Loopback addresses (when the initial URL was an external destination)
    - Private network addresses (RFC 1918)
    - Link-local cloud metadata (169.254.169.254)
    - HTTPS-to-HTTP downgrade
    - Redirect loops or hop limits

    Returns:
        (is_valid: bool, final_clean_url: str, error_message: Optional[str])
    """
    if not url or not isinstance(url, str):
        return False, "about:blank", "Empty or invalid URL."

    sanitize_fn = _get_browser_cdp_symbol("sanitize_browser_url", sanitize_browser_url)
    clean = sanitize_fn(url)
    if clean == "about:blank" and url.strip().lower() not in ("about:blank", "about:blank/"):
        return False, "about:blank", "Destination URL was rejected by browser security policy."

    parsed = urllib.parse.urlsplit(clean)
    scheme = (parsed.scheme or "").lower()

    # Safe internal browser pages
    if scheme in ("about", ""):
        return True, clean, None

    if scheme not in ("http", "https"):
        return False, "about:blank", f"Unsupported scheme for browser navigation: '{scheme}'"

    from jarvis.core.security_boundaries import (
        NoProxyHandler,
        PinnedHTTPHandler,
        PinnedHTTPSHandler,
        SecurityBoundaryViolation,
    )

    ports_fn = _get_browser_cdp_symbol("get_blocked_localhost_ports", get_blocked_localhost_ports)
    blocked_ports = ports_fn()
    validator = NavigationRedirectValidator(clean, max_hops=max_hops, disallowed_ports=blocked_ports)

    handlers = [
        NoProxyHandler(),
        validator,
        PinnedHTTPHandler(allow_localhost=validator.initial_is_local, disallowed_ports=blocked_ports),
        PinnedHTTPSHandler(allow_localhost=validator.initial_is_local, disallowed_ports=blocked_ports),
    ]
    opener = urllib.request.build_opener(*handlers)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 JarvisBrowserBoundaryGuard/2.0",
        "Accept": "*/*",
    }

    try:
        # First attempt lightweight HEAD request to avoid body download
        req = urllib.request.Request(clean, headers=headers, method="HEAD")
        try:
            with opener.open(req, timeout=timeout_sec):
                pass
        except urllib.error.HTTPError as http_err:
            # If server rejects HEAD (e.g. 405 Method Not Allowed, 501 Not Implemented), retry with GET
            if http_err.code in (405, 501):
                get_req = urllib.request.Request(clean, headers=headers, method="GET")
                with opener.open(get_req, timeout=timeout_sec):
                    pass
            elif http_err.code in (301, 302, 303, 307, 308):
                pass
            else:
                pass

        return True, validator.final_url, None

    except SecurityBoundaryViolation as sec_err:
        logger.warning("Browser navigation pre-flight blocked destination: %s", sec_err)
        return False, clean, str(sec_err)
    except TimeoutError as t_err:
        logger.warning("Browser navigation pre-flight timed out for '%s': %s", clean, t_err)
        return False, clean, f"Pre-navigation validation timed out: {t_err}"
    except urllib.error.URLError as url_err:
        if isinstance(url_err.reason, TimeoutError):
            return False, clean, f"Pre-navigation validation timed out: {url_err.reason}"
        logger.debug("Browser navigation pre-flight network notice for '%s': %s", clean, url_err)
        return False, clean, f"Pre-navigation connection check failed: {url_err.reason}"
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Unexpected error in browser navigation pre-flight: %s", exc)
        return False, clean, f"Pre-navigation validation error: {exc}"


def get_cdp_blocked_url_patterns() -> list[str]:
    """Generates defense-in-depth URL wildcard patterns for CDP Network.setBlockedURLs."""
    ports_fn = _get_browser_cdp_symbol("get_blocked_localhost_ports", get_blocked_localhost_ports)
    blocked_ports = ports_fn()
    patterns: list[str] = []

    loopback_hosts = ["127.0.0.1", "localhost", "[::1]", "0.0.0.0"]
    for p in sorted(blocked_ports):
        for host in loopback_hosts:
            patterns.append(f"*://{host}:{p}/*")
            patterns.append(f"*://{host}:{p}")

    # Cloud metadata link-local endpoints
    patterns.extend([
        "*://169.254.169.254/*",
        "*://169.254.169.254:*/*",
        "*://169.254.*/*",
        "*://[fe80:*",
        "*://[fe80:*/*",
    ])

    # Dangerous URI schemes for subresources
    patterns.extend([
        "file://*",
        "javascript:*",
    ])

    return patterns


def configure_cdp_tab_network_guards(session_or_ws: Any, timeout_sec: float = 0.5) -> bool:
    """Installs Network.enable and Network.setBlockedURLs on an active CDP session or WebSocket."""
    try:
        patterns_fn = _get_browser_cdp_symbol("get_cdp_blocked_url_patterns", get_cdp_blocked_url_patterns)
        patterns = patterns_fn()
        if isinstance(session_or_ws, CDPTabSession):
            resp1 = session_or_ws.send_command("Network.enable", {}, timeout_sec=timeout_sec)
            if "error" in resp1:
                logger.warning("CDP Network.enable returned error: %s", resp1["error"])
                return False
            resp2 = session_or_ws.send_command("Network.setBlockedURLs", {"urls": patterns}, timeout_sec=timeout_sec)
            if "error" in resp2:
                logger.warning("CDP Network.setBlockedURLs returned error: %s", resp2["error"])
                return False
            return True

        # Fallback for raw WebSocket or test mocks
        cmd_enable = {"id": 1001, "method": "Network.enable", "params": {}}
        session_or_ws.send(json.dumps(cmd_enable))
        recv_fn = _get_browser_cdp_symbol("recv_cdp_response", recv_cdp_response)
        resp1 = recv_fn(session_or_ws, 1001, timeout_sec=timeout_sec)
        if "error" in resp1:
            logger.warning("CDP Network.enable returned error: %s", resp1["error"])
            return False

        cmd_block = {
            "id": 1002,
            "method": "Network.setBlockedURLs",
            "params": {"urls": patterns},
        }
        session_or_ws.send(json.dumps(cmd_block))
        resp2 = recv_fn(session_or_ws, 1002, timeout_sec=timeout_sec)
        if "error" in resp2:
            logger.warning("CDP Network.setBlockedURLs returned error: %s", resp2["error"])
            return False

        return True
    except (OSError, RuntimeError, AttributeError, ValueError) as e:
        logger.warning("Failed to configure CDP tab network guards: %s", e)
        return False


def recv_cdp_response(ws: Any, expected_id: int, timeout_sec: float = 1.0) -> dict[str, Any]:
    """Reads WebSocket frames until the response matching expected_id is received, filtering asynchronous events."""
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            raw = ws.recv(timeout=timeout_sec)
        except (TimeoutError, OSError):
            break
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                data = json.loads(raw)
                if data.get("id") == expected_id:
                    return data
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                continue
        else:
            return {"id": expected_id, "result": {}}
    return {"id": expected_id, "result": {}}


def sanitize_browser_url(url: str) -> str:
    """Sanitizes and strictly whitelists URL schemes for secure CDP browser navigation.

    Blocks CRLF injection, javascript: execution, file:// local disclosure,
    vbscript:, data: URIs, and destructive internal chrome:// management pages.
    Returns 'about:blank' for any rejected input.
    """
    if not url:
        return "about:blank"

    truncated = _re.split(r"[\r\n\x00]", url.strip())[0]
    cleaned = _re.sub(r"[\t]", "", truncated).strip()
    cleaned_lower = cleaned.lower()

    _BLOCKED_SCHEMES = (
        "javascript:", "data:", "file:", "vbscript:",
        "ms-msdt:", "search-ms:", "mhtml:", "mk:",
    )
    if any(cleaned_lower.startswith(s) for s in _BLOCKED_SCHEMES):
        logger.warning("Blocked dangerous URL scheme from CDP navigation: %s", cleaned[:60])
        return "about:blank"

    if cleaned_lower in _SAFE_INTERNAL_PAGES:
        return cleaned

    try:
        parsed_candidate = urllib.parse.urlsplit(cleaned)
        if parsed_candidate.username is not None or parsed_candidate.password is not None or "@" in (parsed_candidate.netloc or ""):
            logger.warning("Blocked URL containing embedded user-info credentials from CDP navigation.")
            return "about:blank"
    except (ValueError, AttributeError) as parse_err:
        logger.warning("Malformed URL in browser sanitization: %s", parse_err)
        return "about:blank"

    if any(cleaned_lower.startswith(d) for d in _DESTRUCTIVE_CDP_URLS):
        logger.warning("Blocked destructive internal browser URL from CDP navigation: %s", cleaned[:60])
        return "about:blank"

    _INTERNAL_SCHEMES = ("chrome:", "brave:", "edge:", "opera:", "vivaldi:")
    if any(cleaned_lower.startswith(s) for s in _INTERNAL_SCHEMES):
        logger.warning("Blocked unpermitted internal browser URL: %s", cleaned[:60])
        return "about:blank"

    if cleaned and not cleaned_lower.startswith(("http://", "https://", "about:")):
        cleaned = "https://" + cleaned
        cleaned_lower = cleaned.lower()

    if cleaned_lower.startswith(("http://", "https://")):
        try:
            from jarvis.core.security_boundaries import (
                SecurityBoundaryViolation,
                validate_egress_url,
            )
            ports_fn = _get_browser_cdp_symbol("get_blocked_localhost_ports", get_blocked_localhost_ports)
            blocked_ports = ports_fn()
            cleaned = validate_egress_url(
                cleaned,
                allow_localhost=True,
                disallowed_ports=blocked_ports,
            )
        except SecurityBoundaryViolation as ssrf_err:
            logger.warning("Blocked SSRF egress from CDP navigation: %s", ssrf_err)
            return "about:blank"

    return cleaned


def is_unmanaged_cdp_authorized(allow_unmanaged: bool = False) -> bool:
    """Verifies that unmanaged CDP port access is authoritatively permitted by a trusted approval decision."""
    if not allow_unmanaged:
        return False
    try:
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        mgr = DualKeyApprovalManager.get_instance()
        if mgr.is_unmanaged_browser_approved():
            return True
        return mgr.request_unmanaged_browser_approval(ttl_seconds=60.0)
    except (ImportError, AttributeError):
        return False


_ACTIVE_MANAGED_PORTS: set[int] = set()
_ACTIVE_MANAGED_PORTS_LOCK = threading.Lock()


def is_managed_cdp_port(port: int | None) -> bool:
    """Returns True if the port belongs to an active Job Object-contained managed browser session."""
    if port is None:
        return False
    with _ACTIVE_MANAGED_PORTS_LOCK:
        if port in _ACTIVE_MANAGED_PORTS:
            return True
    try:
        get_sess_fn = _get_browser_cdp_symbol("get_managed_browser_session", None)
        session = get_sess_fn() if callable(get_sess_fn) else None
        if session and getattr(session, "cdp_port", None) == port:
            return True
    except (OSError, AttributeError, RuntimeError) as exc:
        logger.debug("Failed to verify managed browser session port %s: %s", port, exc)
    return False


def resolve_cdp_port(
    port: int | None = None,
    allow_unmanaged: bool = False,
) -> int | None:
    """Resolves target CDP port prioritizing managed browser sessions. Fails closed by default."""
    get_port_fn = _get_browser_cdp_symbol("get_managed_cdp_port", None)
    managed_port = get_port_fn() if callable(get_port_fn) else None

    is_managed_fn = _get_browser_cdp_symbol("is_managed_cdp_port", is_managed_cdp_port)

    if port is None:
        if managed_port is not None:
            return managed_port
    else:
        if (managed_port is not None and port == managed_port) or is_managed_fn(port):
            return port

    target = port if port is not None else 9222
    auth_fn = _get_browser_cdp_symbol("is_unmanaged_cdp_authorized", is_unmanaged_cdp_authorized)
    if auth_fn(allow_unmanaged):
        return target

    return None


def is_cdp_available(
    host: str = "127.0.0.1",
    port: int | None = None,
    timeout_sec: float = 0.05,
    force_refresh: bool = False,
    allow_unmanaged: bool = False,
) -> bool:
    """Non-blocking probe checking if Chrome DevTools Protocol port is listening on localhost."""
    resolve_fn = _get_browser_cdp_symbol("resolve_cdp_port", resolve_cdp_port)
    effective_port = resolve_fn(port, allow_unmanaged=allow_unmanaged)
    if effective_port is None:
        return False
    now = time.time()
    cache_key = (host, effective_port)
    if not force_refresh and cache_key in _CDP_PROBE_CACHE:
        val, ts = _CDP_PROBE_CACHE[cache_key]
        if now - ts < _CDP_CACHE_TTL_SEC:
            return val

    if host not in ("127.0.0.1", "localhost"):
        return False

    is_open = False
    try:
        with socket.create_connection((host, effective_port), timeout=timeout_sec):
            is_open = True
    except (TimeoutError, OSError):
        is_open = False

    _CDP_PROBE_CACHE[cache_key] = (is_open, now)
    return is_open


def list_cdp_tabs(
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_unmanaged: bool = False,
) -> list[dict[str, Any]]:
    """Retrieves all open page targets via CDP HTTP JSON interface."""
    resolve_fn = _get_browser_cdp_symbol("resolve_cdp_port", resolve_cdp_port)
    avail_fn = _get_browser_cdp_symbol("is_cdp_available", is_cdp_available)
    prune_fn = _get_browser_cdp_symbol("prune_stale_tab_sessions", prune_stale_tab_sessions)

    target_port = resolve_fn(port, allow_unmanaged=allow_unmanaged)
    if target_port is None or not avail_fn(host, target_port, allow_unmanaged=allow_unmanaged):
        prune_fn(set())
        return []

    url = f"http://{host}:{target_port}/json/list"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-CDP-Client"})
        with urllib.request.urlopen(req, timeout=0.15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tabs = [
                {
                    "id": item.get("id"),
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "ws_url": item.get("webSocketDebuggerUrl"),
                    "type": item.get("type", "page"),
                }
                for item in data
                if item.get("type") == "page"
            ]
            active_ids = {t["id"] for t in tabs if t.get("id")}
            prune_fn(active_ids)
            return tabs
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError, TimeoutError) as e:
        logger.debug("Failed to query CDP tabs: %s", e)
        prune_fn(set())
        return []


def activate_cdp_tab(
    target_id: str,
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_unmanaged: bool = False,
) -> bool:
    """Activates/focuses a specific browser tab via CDP HTTP PUT /json/activate/{target_id}."""
    resolve_fn = _get_browser_cdp_symbol("resolve_cdp_port", resolve_cdp_port)
    avail_fn = _get_browser_cdp_symbol("is_cdp_available", is_cdp_available)

    target_port = resolve_fn(port, allow_unmanaged=allow_unmanaged)
    if target_port is None or not avail_fn(host, target_port, allow_unmanaged=allow_unmanaged) or not target_id:
        return False
    try:
        activate_url = f"http://{host}:{target_port}/json/activate/{target_id}"
        req = urllib.request.Request(activate_url, method="PUT")
        with urllib.request.urlopen(req, timeout=0.25) as resp:
            return resp.status == 200 or resp.read() == b"Target activated"
    except (OSError, urllib.error.URLError, TimeoutError) as e:
        logger.debug("Failed to activate CDP tab %s: %s", target_id, e)
        return False


def close_cdp_tab(
    title_or_url: str | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_unmanaged: bool = False,
) -> str | None:
    """Closes an open browser tab via Chrome DevTools Protocol (CDP) HTTP API."""
    resolve_fn = _get_browser_cdp_symbol("resolve_cdp_port", resolve_cdp_port)
    avail_fn = _get_browser_cdp_symbol("is_cdp_available", is_cdp_available)
    list_fn = _get_browser_cdp_symbol("list_cdp_tabs", list_cdp_tabs)
    remove_fn = _get_browser_cdp_symbol("remove_tab_session", remove_tab_session)

    target_port = resolve_fn(port, allow_unmanaged=allow_unmanaged)
    if target_port is None or not avail_fn(host, target_port, allow_unmanaged=allow_unmanaged):
        return None

    try:
        tabs = list_fn(host, target_port, allow_unmanaged=allow_unmanaged)
        if not tabs:
            return "No open browser tabs detected via DevTools channel, Sir."

        target_tab = None
        if title_or_url and title_or_url.strip():
            query_lower = title_or_url.lower().strip()
            for t in tabs:
                if query_lower in t.get("title", "").lower() or query_lower in t.get("url", "").lower():
                    target_tab = t
                    break
        else:
            target_tab = tabs[0]

        if not target_tab:
            return f"Could not find a browser tab matching '{title_or_url}' via DevTools channel, Sir."

        tab_id = target_tab.get("id")
        tab_title = target_tab.get("title") or "Browser Tab"
        if not tab_id:
            return None

        remove_fn(tab_id)

        close_url = f"http://{host}:{target_port}/json/close/{tab_id}"
        req = urllib.request.Request(close_url)
        with urllib.request.urlopen(req, timeout=0.25) as resp:
            resp.read()

        return f"Verified: Closed browser tab '{tab_title}' via silent DevTools channel, Sir."
    except (OSError, urllib.error.URLError, TimeoutError) as e:
        logger.warning("CDP close tab encountered error: %s", e)
        return None


__all__ = [
    "_ACTIVE_MANAGED_PORTS",
    "_ACTIVE_MANAGED_PORTS_LOCK",
    "_CDP_CACHE_TTL_SEC",
    "_CDP_PROBE_CACHE",
    "_DESTRUCTIVE_CDP_URLS",
    "_SAFE_INTERNAL_PAGES",
    "NavigationRedirectValidator",
    "activate_cdp_tab",
    "close_cdp_tab",
    "configure_cdp_tab_network_guards",
    "get_blocked_localhost_ports",
    "get_cdp_blocked_url_patterns",
    "is_cdp_available",
    "is_managed_cdp_port",
    "is_unmanaged_cdp_authorized",
    "list_cdp_tabs",
    "recv_cdp_response",
    "resolve_cdp_port",
    "sanitize_browser_url",
    "validate_browser_navigation_chain",
]
