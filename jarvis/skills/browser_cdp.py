"""Chrome DevTools Protocol (CDP) client for sub-5ms silent browser control.
Supports Brave, Chrome, and Edge running with --remote-debugging-port=9222.
Enables instant tab listing, navigation, opening, DOM text extraction, and tab switching
without synthetic keystrokes or focus stealing.
"""
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Dict, Any, List, Optional, Tuple

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.BrowserCDP")
import re as _re

_CDP_PROBE_CACHE: Dict[Tuple[str, int], Tuple[bool, float]] = {}
_CDP_CACHE_TTL_SEC: float = 3.0

# Safe internal new-tab pages that may be navigated via CDP.
_SAFE_INTERNAL_PAGES: frozenset = frozenset({
    "about:blank", "about:newtab",
    "chrome://newtab", "chrome://newtab/",
    "brave://newtab", "brave://newtab/",
    "edge://newtab", "edge://newtab/",
})

# Destructive browser management URLs that must never be navigated via CDP.
_DESTRUCTIVE_CDP_URLS: tuple = (
    "chrome://restart", "chrome://quit", "chrome://kill", "chrome://crash",
    "chrome://settings/clearbrowserdata",
    "brave://restart", "brave://quit", "brave://kill", "brave://crash",
    "edge://restart", "edge://quit",
)


def sanitize_browser_url(url: str) -> str:
    """Sanitizes and strictly whitelists URL schemes for secure CDP browser navigation.

    Blocks CRLF injection, javascript: execution, file:// local disclosure,
    vbscript:, data: URIs, and destructive internal chrome:// management pages.
    Returns 'about:blank' for any rejected input.
    """
    if not url:
        return "about:blank"

    # Strip CRLF, null bytes, and surrounding whitespace to prevent header injection.
    # Truncate at first newline or null byte to reject split headers cleanly
    truncated = _re.split(r"[\r\n\x00]", url.strip())[0]
    cleaned = _re.sub(r"[\t]", "", truncated).strip()
    cleaned_lower = cleaned.lower()

    # Block dangerous execution and disclosure URI schemes outright.
    _BLOCKED_SCHEMES = (
        "javascript:", "data:", "file:", "vbscript:",
        "ms-msdt:", "search-ms:", "mhtml:", "mk:",
    )
    if any(cleaned_lower.startswith(s) for s in _BLOCKED_SCHEMES):
        logger.warning(f"Blocked dangerous URL scheme from CDP navigation: {cleaned[:60]}")
        return "about:blank"

    # Permit a curated set of safe internal new-tab pages.
    if cleaned_lower in _SAFE_INTERNAL_PAGES:
        return cleaned

    # Block destructive internal browser management URLs.
    if any(cleaned_lower.startswith(d) for d in _DESTRUCTIVE_CDP_URLS):
        logger.warning(f"Blocked destructive internal browser URL from CDP navigation: {cleaned[:60]}")
        return "about:blank"

    # Block all other non-permitted internal browser schemes.
    _INTERNAL_SCHEMES = ("chrome:", "brave:", "edge:", "opera:", "vivaldi:")
    if any(cleaned_lower.startswith(s) for s in _INTERNAL_SCHEMES):
        logger.warning(f"Blocked unpermitted internal browser URL: {cleaned[:60]}")
        return "about:blank"

    # Normalise bare hostnames to https://.
    if cleaned and not cleaned_lower.startswith(("http://", "https://", "about:")):
        cleaned = "https://" + cleaned
        cleaned_lower = cleaned.lower()

    # SSRF & Private Network Egress Validation for web targets
    if cleaned_lower.startswith(("http://", "https://")):
        try:
            from jarvis.core.security_boundaries import validate_egress_url, SecurityBoundaryViolation
            cleaned = validate_egress_url(cleaned, allow_localhost=True)
        except SecurityBoundaryViolation as ssrf_err:
            logger.warning(f"Blocked SSRF egress from CDP navigation: {ssrf_err}")
            return "about:blank"

    return cleaned


def is_cdp_available(host: str = "127.0.0.1", port: int = 9222, timeout_sec: float = 0.05, force_refresh: bool = False) -> bool:
    """Non-blocking probe checking if Chrome DevTools Protocol port is listening on localhost."""
    now = time.time()
    cache_key = (host, port)
    if not force_refresh and cache_key in _CDP_PROBE_CACHE:
        val, ts = _CDP_PROBE_CACHE[cache_key]
        if now - ts < _CDP_CACHE_TTL_SEC:
            return val

    # Restrict strictly to localhost for safety
    if host not in ("127.0.0.1", "localhost"):
        return False

    is_open = False
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            is_open = True
    except (OSError, socket.timeout):
        is_open = False

    _CDP_PROBE_CACHE[cache_key] = (is_open, now)
    return is_open


def list_cdp_tabs(host: str = "127.0.0.1", port: int = 9222) -> List[Dict[str, Any]]:
    """Retrieves all open page targets via CDP HTTP JSON interface."""
    if not is_cdp_available(host, port):
        return []

    url = f"http://{host}:{port}/json/list"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-CDP-Client"})
        with urllib.request.urlopen(req, timeout=0.15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [
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
    except Exception as e:
        logger.debug(f"Failed to query CDP tabs: {e}")
        return []


def open_or_navigate_tab_cdp(
    url: str,
    new_tab: bool = True,
    host: str = "127.0.0.1",
    port: int = 9222
) -> Optional[str]:
    """Navigates to URL via CDP. Reuses empty tabs or navigates an existing tab when new_tab is False.
    
    Returns:
        Status message if navigation succeeded via CDP, or None if CDP is unavailable / failed.
    """
    if not is_cdp_available(host, port):
        return None

    clean_url = sanitize_browser_url(url)

    try:
        tabs = list_cdp_tabs(host, port)
        
        # 1. Look for existing blank / new tab page
        blank_tab = None
        for t in tabs:
            t_url = t.get("url", "").lower()
            t_title = t.get("title", "").lower()
            if any(k in t_url or k in t_title for k in ("about:blank", "chrome://newtab", "brave://newtab", "new tab")):
                blank_tab = t
                break

        target_to_reuse = None
        if not new_tab and tabs:
            target_to_reuse = tabs[0]
        elif blank_tab:
            target_to_reuse = blank_tab

        # If we have a tab to navigate, use WebSocket Page.navigate or HTTP activate
        if target_to_reuse and clean_url:
            ws_url = target_to_reuse.get("ws_url")
            if ws_url:
                try:
                    from websockets.sync.client import connect
                    with connect(ws_url, open_timeout=0.3, close_timeout=0.2) as ws:
                        cmd = {
                            "id": 1,
                            "method": "Page.navigate",
                            "params": {"url": clean_url}
                        }
                        ws.send(json.dumps(cmd))
                        # Read response with 0.3s timeout
                        ws.recv(timeout=0.3)
                    
                    # Also activate tab so user sees it
                    t_id = target_to_reuse.get("id")
                    if t_id:
                        activate_url = f"http://{host}:{port}/json/activate/{t_id}"
                        urllib.request.urlopen(activate_url, timeout=0.1)

                    return f"Navigated existing tab '{target_to_reuse.get('title') or 'New Tab'}' in browser to '{clean_url}' via silent DevTools channel, Sir."
                except Exception as ws_err:
                    logger.debug(f"CDP WebSocket navigate failed, trying HTTP fallback: {ws_err}")

        # 2. Otherwise create a new tab via CDP HTTP PUT /json/new
        encoded = urllib.parse.quote(clean_url, safe=":/%?&=#") if clean_url else ""
        new_url = f"http://{host}:{port}/json/new?{encoded}" if encoded else f"http://{host}:{port}/json/new"
        req = urllib.request.Request(new_url, method="PUT")
        with urllib.request.urlopen(req, timeout=0.3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tab_id = data.get("id")
            if tab_id:
                # Activate new tab
                urllib.request.urlopen(f"http://{host}:{port}/json/activate/{tab_id}", timeout=0.1)
            return f"Opened browser tab '{clean_url or 'about:blank'}' via silent DevTools channel, Sir."

    except Exception as e:
        logger.warning(f"CDP navigation encountered error: {e}")
        return None


def activate_cdp_tab(target_id: str, host: str = "127.0.0.1", port: int = 9222) -> bool:
    """Activates/focuses a specific browser tab via CDP HTTP PUT /json/activate/{target_id}."""
    if not is_cdp_available(host, port) or not target_id:
        return False
    try:
        activate_url = f"http://{host}:{port}/json/activate/{target_id}"
        req = urllib.request.Request(activate_url, method="PUT")
        with urllib.request.urlopen(req, timeout=0.25) as resp:
            return resp.status == 200 or resp.read() == b"Target activated"
    except Exception as e:
        logger.debug(f"Failed to activate CDP tab {target_id}: {e}")
        return False


def read_webpage_content_cdp(
    target_id_or_title: Optional[str] = None,
    max_chars: int = 15000,
    host: str = "127.0.0.1",
    port: int = 9222
) -> Optional[Dict[str, Any]]:
    """Extracts title, URL, and readable body/article text from an open tab in <5ms using Runtime.evaluate."""
    if not is_cdp_available(host, port):
        return None

    try:
        tabs = list_cdp_tabs(host, port)
        if not tabs:
            return None

        target_tab = None
        if target_id_or_title and target_id_or_title.strip():
            query_lower = target_id_or_title.lower().strip()
            for t in tabs:
                if t.get("id") == target_id_or_title or query_lower in t.get("title", "").lower() or query_lower in t.get("url", "").lower():
                    target_tab = t
                    break
        else:
            target_tab = tabs[0]

        if not target_tab:
            return None

        ws_url = target_tab.get("ws_url")
        if not ws_url:
            return None

        from websockets.sync.client import connect
        with connect(ws_url, open_timeout=0.3, close_timeout=0.2) as ws:
            js_expr = (
                "(() => {"
                "  const title = document.title || '';"
                "  const url = window.location.href || '';"
                "  let text = '';"
                "  const mainEl = document.querySelector('article, main, #content, .content, [role=\"main\"]');"
                "  if (mainEl) { text = mainEl.innerText || mainEl.textContent || ''; }"
                "  if (!text || text.length < 200) { text = document.body ? (document.body.innerText || document.body.textContent || '') : ''; }"
                "  text = text.replace(/[ \\t]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n').trim();"
                f"  return {{ title, url, text: text.slice(0, {max_chars}) }};"
                "})()"
            )
            cmd = {
                "id": 201,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": js_expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                }
            }
            ws.send(json.dumps(cmd))
            resp_raw = ws.recv(timeout=0.8)
            resp = json.loads(resp_raw)

            val = resp.get("result", {}).get("result", {}).get("value")
            if isinstance(val, dict):
                return val
            return None

    except Exception as e:
        logger.warning(f"Error reading webpage content via CDP: {e}")
        return None


def execute_cdp_javascript(
    expression: str,
    target_id_or_title: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 9222
) -> Optional[Any]:
    """Evaluates JavaScript expression on the target tab via CDP Runtime.evaluate."""
    if not is_cdp_available(host, port) or not expression:
        return None

    try:
        tabs = list_cdp_tabs(host, port)
        if not tabs:
            return None

        target_tab = None
        if target_id_or_title and target_id_or_title.strip():
            query_lower = target_id_or_title.lower().strip()
            for t in tabs:
                if t.get("id") == target_id_or_title or query_lower in t.get("title", "").lower() or query_lower in t.get("url", "").lower():
                    target_tab = t
                    break
        else:
            target_tab = tabs[0]

        if not target_tab:
            return None

        ws_url = target_tab.get("ws_url")
        if not ws_url:
            return None

        from websockets.sync.client import connect
        with connect(ws_url, open_timeout=0.3, close_timeout=0.2) as ws:
            cmd = {
                "id": 202,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                }
            }
            ws.send(json.dumps(cmd))
            resp_raw = ws.recv(timeout=1.0)
            resp = json.loads(resp_raw)
            return resp.get("result", {}).get("result", {}).get("value")

    except Exception as e:
        logger.warning(f"Error executing JavaScript via CDP: {e}")
        return None


def bootstrap_cdp_browser(
    browser_name: str = "brave",
    port: int = 9222,
    dedicated_profile: bool = True
) -> Tuple[bool, str]:
    """Ensures a browser instance is running with remote debugging port enabled."""
    if is_cdp_available("127.0.0.1", port):
        return True, f"Browser DevTools Protocol is active and connected on port {port}, Sir."

    browser_path = None
    try:
        from jarvis.skills.os_control import _build_app_index
        apps = _build_app_index()
        b_clean = browser_name.lower().strip()
        for name, path in apps.items():
            if b_clean in name and path.lower().endswith(".exe"):
                browser_path = path
                break
    except Exception:
        pass

    if not browser_path:
        known_direct = [
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in known_direct:
            if os.path.exists(p):
                browser_path = p
                break

    if not browser_path:
        return False, f"Could not locate an executable for browser '{browser_name}', Sir."

    import psutil
    exe_name = Path(browser_path).name.lower()
    is_running = any(p.name().lower() == exe_name for p in psutil.process_iter(['name']))

    cmd = [
        browser_path,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
    ]
    if is_running or dedicated_profile:
        profile_dir = Path("data/browser_debug_profile").resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--user-data-dir={str(profile_dir)}")

    try:
        subprocess.Popen(
            cmd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )

        _CDP_PROBE_CACHE.clear()
        for _ in range(40):
            time.sleep(0.1)
            if is_cdp_available("127.0.0.1", port, force_refresh=True):
                return True, f"Attached browser '{browser_name.title()}' with remote debugging port {port} enabled, Sir."

        return False, f"Browser launched, but port {port} did not respond within timeout, Sir."
    except Exception as e:
        return False, f"Failed to launch browser with debugging flags: {e}"


def close_cdp_tab(
    title_or_url: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 9222
) -> Optional[str]:
    """Closes an open browser tab via Chrome DevTools Protocol (CDP) HTTP API.
    
    Returns status message if successfully closed via CDP, or None if CDP unavailable/failed.
    """
    if not is_cdp_available(host, port):
        return None

    try:
        tabs = list_cdp_tabs(host, port)
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
            # Default to active/first tab
            target_tab = tabs[0]

        if not target_tab:
            return f"Could not find a browser tab matching '{title_or_url}' via DevTools channel, Sir."

        tab_id = target_tab.get("id")
        tab_title = target_tab.get("title") or "Browser Tab"
        if not tab_id:
            return None

        close_url = f"http://{host}:{port}/json/close/{tab_id}"
        req = urllib.request.Request(close_url)
        with urllib.request.urlopen(req, timeout=0.25) as resp:
            resp.read()

        return f"Verified: Closed browser tab '{tab_title}' via silent DevTools channel, Sir."
    except Exception as e:
        logger.warning(f"CDP close tab encountered error: {e}")
        return None


@SkillRegistry.register(
    name="list_open_browser_tabs",
    description="Lists all open tabs in Sir's browser via silent Chrome DevTools Protocol (CDP), including tab titles and URLs without stealing focus."
)
def list_open_browser_tabs() -> str:
    """Tool to inspect active browser tabs without foreground window disruption."""
    if not is_cdp_available():
        return "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir. Open tabs cannot be inspected silently without attaching debugging flags."

    tabs = list_cdp_tabs()
    if not tabs:
        return "No open browser tabs detected via DevTools channel, Sir."

    lines = ["Active Browser Tabs, Sir:"]
    for i, t in enumerate(tabs, 1):
        title = t.get("title", "Untitled")
        url = t.get("url", "")
        lines.append(f"{i}. {title} ({url})")

    return "\n".join(lines)


@SkillRegistry.register(
    name="switch_to_browser_tab",
    description="Switches focus and activates an open browser tab by title or URL keyword via Chrome DevTools Protocol (CDP)."
)
def switch_to_browser_tab(tab_name_or_url: str) -> str:
    """Activates a browser tab and brings the browser window to the foreground."""
    if not is_cdp_available():
        return "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir."

    tabs = list_cdp_tabs()
    if not tabs:
        return "No open browser tabs found to switch to, Sir."

    query = (tab_name_or_url or "").lower().strip()
    target_tab = None
    for t in tabs:
        if query in t.get("title", "").lower() or query in t.get("url", "").lower():
            target_tab = t
            break

    if not target_tab:
        return f"Could not find an open tab matching '{tab_name_or_url}', Sir."

    tab_id = target_tab.get("id")
    tab_title = target_tab.get("title") or "Browser Tab"
    if not activate_cdp_tab(tab_id):
        return f"Failed to activate tab '{tab_title}' via DevTools channel, Sir."

    try:
        from jarvis.skills.windows_uia import focus_window_uia
        focus_window_uia("Brave") or focus_window_uia("Chrome") or focus_window_uia("Edge")
    except Exception:
        pass

    return f"Switched to browser tab '{tab_title}', Sir."


@SkillRegistry.register(
    name="read_webpage_content",
    description="Extracts clean readable text, articles, or documentation directly from the active or specified browser tab via silent Chrome DevTools Protocol without stealing focus or copying to clipboard.",
    external_ingestion=True,
)
def read_webpage_content(tab_name_or_url: str = "") -> str:
    """Reads readable text from active or specified browser tab via CDP."""
    if not is_cdp_available():
        return (
            "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir. "
            "Please launch the browser with debugging enabled or use 'launch_debug_browser'."
        )

    data = read_webpage_content_cdp(target_id_or_title=tab_name_or_url)
    if not data or not data.get("text"):
        return f"Unable to extract readable content from browser tab '{tab_name_or_url or 'Active'}', Sir."

    title = data.get("title") or "Untitled Document"
    url = data.get("url") or ""
    text = data.get("text", "").strip()

    preview = text[:2500]
    suffix = f"\n\n... [{len(text) - 2500} additional characters available; call scrape_and_archive_webpage to store permanently]" if len(text) > 2500 else ""
    return f"Webpage Intel for '{title}' ({url}), Sir:\n\n{preview}{suffix}"


@SkillRegistry.register(
    name="execute_browser_javascript",
    description="Executes a JavaScript expression in the active or target browser tab via DevTools Protocol and returns the evaluated result.",
    external_ingestion=True,
)
def execute_browser_javascript(script: str, tab_name: str = "") -> str:
    """Executes JavaScript in the browser tab via CDP."""
    if not is_cdp_available():
        return "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir."

    res = execute_cdp_javascript(expression=script, target_id_or_title=tab_name)
    if res is None:
        return "JavaScript execution returned no result or encountered an error, Sir."
    return f"JavaScript Evaluation Result, Sir:\n{res}"


@SkillRegistry.register(
    name="launch_debug_browser",
    description="Launches or connects a browser session with Chrome DevTools Protocol (CDP port 9222) enabled for ultra-fast silent automation."
)
def launch_debug_browser(browser: str = "brave", dedicated_profile: bool = True) -> str:
    """Starts browser with remote debugging port 9222."""
    success, msg = bootstrap_cdp_browser(browser_name=browser, dedicated_profile=dedicated_profile)
    return msg


@SkillRegistry.register(
    name="scrape_and_archive_webpage",
    description="Extracts the readable text and documentation from an open browser tab and archives it directly into Jarvis's local JIT document memory vault for future semantic retrieval.",
    external_ingestion=True,
)
def scrape_and_archive_webpage(tab_name_or_url: str = "", category: str = "web_intel") -> str:
    """Extracts webpage content via CDP and permanently stores it in Tier 3 Document Memory."""
    content_data = read_webpage_content_cdp(target_id_or_title=tab_name_or_url)
    if not content_data or not content_data.get("text"):
        return "Could not extract webpage content to archive. Please ensure the target tab is loaded, Sir."

    title = content_data.get("title") or "Webpage Document"
    url = content_data.get("url") or ""
    text = content_data.get("text", "")

    try:
        from jarvis.skills.calendar_manager import _get_memory
        mem = _get_memory()
        if not mem:
            return "Memory subsystem is unavailable to archive the document, Sir."

        doc_header = f"[Source URL: {url} | Extracted via Browser CDP]\n\n"
        full_doc = doc_header + text
        doc_id = mem.archive_document(title=title, content=full_doc, category=category)
        return (
            f"Archived webpage '{title}' ({len(text)} characters) into your local document vault "
            f"(ID: #{doc_id}, Category: {category}), Sir. It is now indexed for semantic and keyword retrieval."
        )
    except Exception as e:
        return f"Failed to archive webpage content into memory: {e}"
