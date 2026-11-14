"""Chrome DevTools Protocol (CDP) client facade for sub-5ms silent browser control.

Supports Brave, Chrome, and Edge running with --remote-debugging-port.
Enables instant tab listing, navigation, opening, DOM text extraction, and tab switching
without synthetic keystrokes or focus stealing.
Decomposed into focused modules:
- jarvis.skills.cdp_transport (transport, network guards, URL validation, and port authorization)
- jarvis.skills.cdp_tabs (tab session registry, lifecycle tracking, and cleanup)
- jarvis.skills.cdp_page_extractor (DOM content extraction and JavaScript evaluation)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from jarvis.skills.base import SkillRegistry
from jarvis.skills.cdp_page_extractor import (
    execute_cdp_javascript,
    read_webpage_content_cdp,
)
from jarvis.skills.cdp_tabs import (
    _ACTIVE_TAB_SESSIONS,
    _CDP_ATEXIT_LOCK,
    _CDP_ATEXIT_REGISTERED,
    _SESSION_REGISTRY_LOCK,
    CDPTabSession,
    _cdp_atexit_handler,
    clear_all_tab_sessions,
    get_tab_session,
    prune_stale_tab_sessions,
    register_cdp_atexit_cleanup,
    register_tab_session,
    remove_tab_session,
)
from jarvis.skills.cdp_transport import (
    _ACTIVE_MANAGED_PORTS,
    _ACTIVE_MANAGED_PORTS_LOCK,
    _CDP_CACHE_TTL_SEC,
    _CDP_PROBE_CACHE,
    _DESTRUCTIVE_CDP_URLS,
    _SAFE_INTERNAL_PAGES,
    NavigationRedirectValidator,
    activate_cdp_tab,
    close_cdp_tab,
    configure_cdp_tab_network_guards,
    get_blocked_localhost_ports,
    get_cdp_blocked_url_patterns,
    is_cdp_available,
    is_managed_cdp_port,
    is_unmanaged_cdp_authorized,
    list_cdp_tabs,
    recv_cdp_response,
    resolve_cdp_port,
    sanitize_browser_url,
    validate_browser_navigation_chain,
)

logger = logging.getLogger("Jarvis.BrowserCDP")


def resolve_browser_executable(browser_name: str = "brave") -> str | None:
    """Resolves the executable path for the requested browser without launching it."""
    b_clean = (browser_name or "brave").lower().strip()
    try:
        from jarvis.skills.os_control import _build_app_index
        apps = _build_app_index()
        for name, path in apps.items():
            if b_clean in name and path.lower().endswith(".exe"):
                return path
    except (ImportError, OSError, KeyError, TypeError):
        pass

    known_direct = [
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ]
    # Check Playwright-managed headless Chromium before system Edge
    import glob
    playwright_candidates = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win\chrome.exe"))
    known_direct.extend(playwright_candidates)
    known_direct.append(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"))
    for p in known_direct:
        if os.path.exists(p):
            return p
    return None


class ManagedBrowserSession:
    """Manages the lifecycle of a dedicated, Win32 Job Object-contained Chromium process for automated browsing.

    Invariants:
    - Dedicated, per-run isolated profile directory under data/agy_runtime_workspace/browser_profile/
    - Process tree containment under a Win32 Job Object with KILL_ON_JOB_CLOSE
    - Process spawned CREATE_SUSPENDED, assigned to Job Object, and resumed via ResumeThread
    - Dynamic CDP port discovery via DevToolsActivePort (bound to 127.0.0.1)
    - Zero fallback to ambient or personal browser instances
    - Profile directory and process tree cleaned up on shutdown / atexit
    """

    def __init__(
        self,
        browser_name: str = "brave",
        headless: bool = False,
        base_dir: Path | None = None,
    ) -> None:
        self.browser_name = browser_name
        self.headless = headless
        self.base_dir = base_dir or Path("data/agy_runtime_workspace/browser_profile").resolve()
        self.process: Any | None = None
        self.proxy: Any | None = None
        self.profile_dir: Path | None = None
        self.cdp_port: int | None = None
        self.ws_url_path: str | None = None
        self._lock = threading.RLock()
        self._stopped = False

    def is_alive(self) -> bool:
        """Returns True if the managed browser process is running, proxy is active, and CDP port is listening."""
        with self._lock:
            if self._stopped or self.process is None or self.cdp_port is None:
                return False
            if self.proxy is None or not self.proxy.is_alive():
                return False
            if hasattr(self.process, "is_alive") and not self.process.is_alive():
                return False
            return is_cdp_available("127.0.0.1", self.cdp_port, timeout_sec=0.05)

    def start(self, timeout_sec: float = 10.0) -> tuple[bool, str]:
        """Spawns the managed browser process suspended, assigns to Job Object, resumes, and discovers dynamic CDP port."""
        with self._lock:
            if self.is_alive():
                return True, f"Managed browser already active on dynamic CDP port {self.cdp_port}."

            browser_path = resolve_browser_executable(self.browser_name)
            if not browser_path:
                return False, f"Could not locate an executable for browser '{self.browser_name}'."

            # Prune stale orphaned session directories from prior crashed runs
            clean_stale_managed_profiles(self.base_dir)

            run_id = uuid.uuid4().hex[:12]
            run_dir = self.base_dir / f"session_{run_id}"
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return False, f"Failed to create isolated browser profile directory: {exc}"
            self.profile_dir = run_dir

            ui_ports = get_blocked_localhost_ports()
            trusted_origins = [f"http://127.0.0.1:{p}" for p in sorted(ui_ports)] + [f"http://localhost:{p}" for p in sorted(ui_ports)]
            trusted_origins.extend(["chrome-devtools://devtools", "devtools://devtools"])

            # 1. Start local Egress Proxy server on loopback
            from jarvis.skills.egress_proxy import EgressProxyServer
            self.proxy = EgressProxyServer(host="127.0.0.1", port=0)
            try:
                proxy_port = self.proxy.start()
            except (OSError, RuntimeError) as exc:
                logger.error("Failed to start local egress proxy: %s", exc)
                self.stop()
                return False, f"Failed to start local egress proxy: {exc}"

            cmd = [
                browser_path,
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={run_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-allow-origins={','.join(trusted_origins)}",
                f"--proxy-server=http://127.0.0.1:{proxy_port}",
                "--proxy-bypass-list=<-loopback>",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--dns-prefetch-disable",
                "--disable-features=AsyncDns",
            ]
            if self.headless:
                cmd.append("--headless=new")

            from jarvis.skills.job_object_sandbox import spawn_contained_process
            try:
                self.process = spawn_contained_process(cmd)
            except (OSError, RuntimeError) as exc:
                logger.error("Failed to spawn contained browser process: %s", exc)
                self._cleanup_profile()
                return False, f"Failed to spawn contained browser process: {exc}"

            discovered_port, ws_path, err = self._poll_devtools_active_port(run_dir, timeout_sec=timeout_sec)
            if not discovered_port:
                logger.error("DevToolsActivePort discovery failed: %s; terminating contained browser fail-closed", err)
                self.stop()
                return False, f"DevToolsActivePort discovery failed: {err}"

            self.cdp_port = discovered_port
            self.ws_url_path = ws_path
            with _ACTIVE_MANAGED_PORTS_LOCK:
                _ACTIVE_MANAGED_PORTS.add(discovered_port)

            deadline = time.monotonic() + 3.0
            ready = False
            while time.monotonic() < deadline:
                if is_cdp_available("127.0.0.1", self.cdp_port, timeout_sec=0.1, force_refresh=True):
                    ready = True
                    break
                time.sleep(0.05)

            if not ready:
                logger.error("CDP endpoint on 127.0.0.1:%d did not respond; terminating fail-closed", self.cdp_port)
                self.stop()
                return False, f"Managed browser launched, but CDP port {self.cdp_port} did not respond within timeout."

            self._stopped = False
            register_cdp_atexit_cleanup()
            return True, f"Managed browser '{self.browser_name}' active on dynamic CDP port {self.cdp_port}."

    def _poll_devtools_active_port(self, run_dir: Path, timeout_sec: float) -> tuple[int | None, str | None, str | None]:
        """Polls for DevToolsActivePort and strictly parses the dynamic port number and WebSocket path."""
        port_file = run_dir / "DevToolsActivePort"
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if port_file.exists():
                try:
                    content = port_file.read_text(encoding="utf-8").strip()
                    lines = content.splitlines()
                    if lines:
                        raw_port = lines[0].strip()
                        if raw_port.isdigit():
                            port = int(raw_port)
                            if 1 <= port <= 65535:
                                ws_path = lines[1].strip() if len(lines) > 1 else ""
                                return port, ws_path, None
                        return None, None, f"Malformed DevToolsActivePort content: {content[:100]}"
                except (OSError, UnicodeDecodeError):
                    pass
            time.sleep(0.05)
        return None, None, "Timed out waiting for DevToolsActivePort to be created"

    def stop(self) -> None:
        """Terminates the process tree and purges the isolated per-run profile directory."""
        with self._lock:
            self._stopped = True
            if self.cdp_port is not None:
                with _ACTIVE_MANAGED_PORTS_LOCK:
                    _ACTIVE_MANAGED_PORTS.discard(self.cdp_port)
            if self.process:
                try:
                    self.process.terminate()
                except (OSError, RuntimeError) as exc:
                    logger.warning("Error terminating managed browser process: %s", exc)
                self.process = None
            if self.proxy:
                try:
                    self.proxy.stop()
                except (OSError, RuntimeError) as exc:
                    logger.warning("Error stopping egress proxy: %s", exc)
                self.proxy = None
            self._cleanup_profile()
            self.cdp_port = None
            self.ws_url_path = None

    def _cleanup_profile(self) -> None:
        """Removes the per-run isolated profile directory."""
        if self.profile_dir and self.profile_dir.exists():
            for _ in range(5):
                try:
                    shutil.rmtree(self.profile_dir, ignore_errors=False)
                    break
                except (OSError, PermissionError):
                    time.sleep(0.1)
            if self.profile_dir.exists():
                try:
                    shutil.rmtree(self.profile_dir, ignore_errors=True)
                except (OSError, RuntimeError) as exc:
                    logger.debug("Cleanup profile notice: %s", exc)
            self.profile_dir = None


_MANAGED_BROWSER_SESSION: ManagedBrowserSession | None = None
_MANAGED_BROWSER_LOCK = threading.RLock()


def clean_stale_managed_profiles(base_dir: Path | None = None, max_age_sec: float = 300.0) -> int:
    """Removes orphaned session directories from previous browser runs.

    Skips the active session's profile directory. Silently ignores directories
    with active Windows file locks. Returns the count of pruned directories.
    """
    target_base = base_dir or Path("data/agy_runtime_workspace/browser_profile").resolve()
    if not target_base.exists() or not target_base.is_dir():
        return 0

    active_dir: Path | None = None
    with _MANAGED_BROWSER_LOCK:
        if _MANAGED_BROWSER_SESSION and _MANAGED_BROWSER_SESSION.profile_dir:
            active_dir = _MANAGED_BROWSER_SESSION.profile_dir.resolve()

    pruned_count = 0
    now = time.time()
    try:
        for entry in target_base.iterdir():
            if not entry.is_dir() or not entry.name.startswith("session_"):
                continue
            try:
                resolved_entry = entry.resolve()
                if active_dir and resolved_entry == active_dir:
                    continue
                stat_res = entry.stat()
                age = now - stat_res.st_mtime
                if age >= max_age_sec:
                    shutil.rmtree(entry, ignore_errors=True)
                    if not entry.exists():
                        pruned_count += 1
            except (OSError, PermissionError) as exc:
                logger.debug("clean_stale_managed_profiles notice on %s: %s", entry.name, exc)
    except OSError as exc:
        logger.debug("Failed to iterate browser profile directory: %s", exc)

    return pruned_count


def get_managed_browser_session() -> ManagedBrowserSession | None:
    """Returns the active ManagedBrowserSession if running and responsive."""
    with _MANAGED_BROWSER_LOCK:
        if _MANAGED_BROWSER_SESSION and _MANAGED_BROWSER_SESSION.is_alive():
            return _MANAGED_BROWSER_SESSION
        return None


def get_managed_cdp_port() -> int | None:
    """Returns the dynamic CDP port of the active managed browser, or None."""
    session = get_managed_browser_session()
    return session.cdp_port if session else None


def ensure_managed_browser(
    browser_name: str = "brave",
    headless: bool = False,
    timeout_sec: float = 10.0,
) -> tuple[bool, int | None, str]:
    """Ensures a Job-Object contained managed browser is active, launching one if needed.
    Returns (success, port, message).
    """
    global _MANAGED_BROWSER_SESSION
    with _MANAGED_BROWSER_LOCK:
        if _MANAGED_BROWSER_SESSION and _MANAGED_BROWSER_SESSION.is_alive():
            return True, _MANAGED_BROWSER_SESSION.cdp_port, f"Managed browser active on port {_MANAGED_BROWSER_SESSION.cdp_port}."

        if _MANAGED_BROWSER_SESSION:
            try:
                _MANAGED_BROWSER_SESSION.stop()
            except (OSError, RuntimeError) as exc:
                logger.debug("Stop managed browser notice: %s", exc)
            _MANAGED_BROWSER_SESSION = None

        session = ManagedBrowserSession(browser_name=browser_name, headless=headless)
        ok, msg = session.start(timeout_sec=timeout_sec)
        if ok and session.cdp_port:
            _MANAGED_BROWSER_SESSION = session
            return True, session.cdp_port, msg
        else:
            session.stop()
            return False, None, msg


def stop_managed_browser() -> None:
    """Stops the active managed browser and cleans up its isolated profile directory."""
    global _MANAGED_BROWSER_SESSION
    with _MANAGED_BROWSER_LOCK:
        if _MANAGED_BROWSER_SESSION:
            try:
                _MANAGED_BROWSER_SESSION.stop()
            except (OSError, RuntimeError) as exc:
                logger.warning("Error stopping managed browser: %s", exc)
            _MANAGED_BROWSER_SESSION = None


def bootstrap_cdp_browser(
    browser_name: str = "brave",
    port: int | None = None,
    dedicated_profile: bool = True,
) -> tuple[bool, str]:
    """Ensures a browser instance is running with remote debugging port enabled."""
    target_port = port or 9222
    if is_cdp_available("127.0.0.1", target_port):
        return True, f"Browser DevTools Protocol is active and connected on port {target_port}, Sir."

    browser_path = resolve_browser_executable(browser_name)
    if not browser_path:
        return False, f"Could not locate an executable for browser '{browser_name}', Sir."

    import psutil
    exe_name = Path(browser_path).name.lower()
    is_running = any(p.name().lower() == exe_name for p in psutil.process_iter(['name']))

    ui_ports = {20129}
    try:
        from jarvis.core.config_loader import load_config
        cfg = load_config()
        p = cfg.get("ui", {}).get("port")
        if p and isinstance(p, int):
            ui_ports.add(p)
    except (ImportError, OSError, KeyError, TypeError):
        pass
    trusted_origins = [f"http://127.0.0.1:{p}" for p in sorted(ui_ports)] + [f"http://localhost:{p}" for p in sorted(ui_ports)]
    trusted_origins.extend(["chrome-devtools://devtools", "devtools://devtools"])

    cmd = [
        browser_path,
        f"--remote-debugging-port={target_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-allow-origins={','.join(trusted_origins)}",
    ]
    if is_running or dedicated_profile:
        profile_dir = Path("data/browser_debug_profile").resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--user-data-dir={profile_dir!s}")

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
                return True, f"Attached browser '{browser_name.title()}' with remote debugging port {target_port} enabled, Sir."

        return False, f"Browser launched, but port {target_port} did not respond within timeout, Sir."
    except (OSError, RuntimeError) as e:
        return False, f"Failed to launch browser with debugging flags: {e}"


def open_or_navigate_tab_cdp(
    url: str,
    new_tab: bool = True,
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_unmanaged: bool = False,
) -> str | None:
    """Navigates to URL via CDP. Reuses empty tabs or navigates an existing tab when new_tab is False.

    Returns:
        Status message if navigation succeeded via CDP, or None if CDP is unavailable / failed.
    """
    target_port = resolve_cdp_port(port, allow_unmanaged=allow_unmanaged)
    if target_port is None or not is_cdp_available(host, target_port, allow_unmanaged=allow_unmanaged):
        return None

    clean_url = sanitize_browser_url(url)
    if clean_url == "about:blank" and (url or "").strip().lower() not in ("about:blank", "about:blank/"):
        return "Navigation blocked: Destination URL was rejected by browser security policy, Sir."

    # Pre-flight redirect chain validation for HTTP/HTTPS web targets
    if clean_url.lower().startswith(("http://", "https://")):
        is_valid, final_url, err_msg = validate_browser_navigation_chain(clean_url)
        if not is_valid:
            return f"Navigation blocked: Destination URL was rejected by browser security policy: {err_msg}, Sir."
        clean_url = final_url

    try:
        tabs = list_cdp_tabs(host, target_port)

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
            t_id = target_to_reuse.get("id")
            if ws_url and t_id:
                try:
                    from websockets.sync.client import connect
                    ws_ctx = connect(ws_url, open_timeout=0.3, close_timeout=0.2)
                    ws = ws_ctx.__enter__() if hasattr(ws_ctx, "__enter__") else ws_ctx
                    session = CDPTabSession(ws, t_id)

                    try:
                        # Install network boundary guards on the tab session before navigation
                        guarded = configure_cdp_tab_network_guards(session)
                        if not guarded:
                            session.close()
                            return "Navigation blocked: Failed to initialize browser network security guard on reused tab, Sir."

                        resp = session.send_command("Page.navigate", {"url": clean_url}, timeout_sec=1.0)
                        if "error" in resp:
                            session.close()
                            return f"Navigation blocked: Failed to navigate target tab: {resp['error']}, Sir."

                        # Maintain active session to preserve CDP network guard in Chromium
                        register_tab_session(t_id, session)

                        # Also activate tab so user sees it
                        activate_url = f"http://{host}:{target_port}/json/activate/{t_id}"
                        urllib.request.urlopen(activate_url, timeout=0.1)

                        return f"Navigated existing tab '{target_to_reuse.get('title') or 'New Tab'}' in browser to '{clean_url}' via silent DevTools channel, Sir."
                    except (OSError, RuntimeError, TimeoutError, ValueError):
                        session.close()
                        raise
                except (OSError, RuntimeError, TimeoutError, ValueError) as ws_err:
                    logger.debug("CDP WebSocket navigate failed, trying HTTP fallback: %s", ws_err)

        # 2. Otherwise create a new tab: create empty tab first, install guard, then navigate
        new_url = f"http://{host}:{target_port}/json/new"
        req = urllib.request.Request(new_url, method="PUT")
        with urllib.request.urlopen(req, timeout=0.3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tab_id = data.get("id")
            ws_url = data.get("webSocketDebuggerUrl")

            if tab_id and ws_url and clean_url and clean_url not in ("about:blank", "about:newtab"):
                session = None
                nav_success = False
                try:
                    from websockets.sync.client import connect
                    ws_ctx = connect(ws_url, open_timeout=0.3, close_timeout=0.2)
                    ws = ws_ctx.__enter__() if hasattr(ws_ctx, "__enter__") else ws_ctx
                    session = CDPTabSession(ws, tab_id)
                    guarded = configure_cdp_tab_network_guards(session)
                    if not guarded:
                        session.close()
                        session = None
                        close_cdp_tab(tab_id, host=host, port=target_port)
                        return "Navigation blocked: Failed to initialize browser network security guard, Sir."

                    resp = session.send_command("Page.navigate", {"url": clean_url}, timeout_sec=1.0)
                    if "error" in resp:
                        session.close()
                        session = None
                        close_cdp_tab(tab_id, host=host, port=target_port)
                        return f"Navigation blocked: Failed to navigate newly opened tab: {resp['error']}, Sir."

                    # Maintain active session to preserve CDP network guard in Chromium
                    register_tab_session(tab_id, session)
                    nav_success = True
                except (OSError, RuntimeError, TimeoutError, ValueError) as guard_err:
                    logger.warning("Failed to guard newly opened CDP tab: %s", guard_err)
                    close_cdp_tab(tab_id, host=host, port=target_port)
                    return f"Navigation blocked: Failed to initialize browser network security guard: {guard_err}, Sir."
                finally:
                    if not nav_success and session is not None:
                        session.close()

            if tab_id:
                # Activate new tab
                urllib.request.urlopen(f"http://{host}:{target_port}/json/activate/{tab_id}", timeout=0.1)
            return f"Opened browser tab '{clean_url or 'about:blank'}' via silent DevTools channel, Sir."

    except (OSError, urllib.error.URLError, TimeoutError) as e:
        logger.warning("CDP navigation encountered error: %s", e)
        return None


@SkillRegistry.register(
    name="list_open_browser_tabs",
    description="Lists all open tabs in Sir's browser via silent Chrome DevTools Protocol (CDP), including tab titles and URLs without stealing focus."
)
def list_open_browser_tabs(allow_unmanaged: bool = False) -> str:
    """Tool to inspect active browser tabs without foreground window disruption."""
    managed_port = get_managed_cdp_port()
    if managed_port is None and not is_unmanaged_cdp_authorized(allow_unmanaged):
        return (
            "Browser DevTools Protocol execution blocked: No managed browser session is active, Sir. "
            "Autonomous execution cannot connect to unmanaged browser port 9222 without trusted approval authorization."
        )

    target_port = resolve_cdp_port(port=None, allow_unmanaged=allow_unmanaged)
    if target_port is None or not is_cdp_available(port=target_port, allow_unmanaged=allow_unmanaged):
        return "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir. Open tabs cannot be inspected silently without attaching debugging flags."

    tabs = list_cdp_tabs(port=target_port, allow_unmanaged=allow_unmanaged)
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
def switch_to_browser_tab(tab_name_or_url: str, allow_unmanaged: bool = False) -> str:
    """Activates a browser tab and brings the browser window to the foreground."""
    managed_port = get_managed_cdp_port()
    if managed_port is None and not is_unmanaged_cdp_authorized(allow_unmanaged):
        return (
            "Browser DevTools Protocol execution blocked: No managed browser session is active, Sir. "
            "Autonomous execution cannot connect to unmanaged browser port 9222 without trusted approval authorization."
        )

    target_port = resolve_cdp_port(port=None, allow_unmanaged=allow_unmanaged)
    if target_port is None or not is_cdp_available(port=target_port, allow_unmanaged=allow_unmanaged):
        return "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir."

    tabs = list_cdp_tabs(port=target_port, allow_unmanaged=allow_unmanaged)
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
    except (ImportError, OSError, KeyError, TypeError):
        pass

    return f"Switched to browser tab '{tab_title}', Sir."


@SkillRegistry.register(
    name="read_webpage_content",
    description="Extracts clean readable text, articles, or documentation directly from the active or specified browser tab via silent Chrome DevTools Protocol without stealing focus or copying to clipboard.",
    external_ingestion=True,
)
def read_webpage_content(tab_name_or_url: str = "", allow_unmanaged: bool = False) -> str:
    """Reads readable text from active or specified browser tab via CDP."""
    managed_port = get_managed_cdp_port()
    if managed_port is None and not is_unmanaged_cdp_authorized(allow_unmanaged):
        return (
            "Browser DevTools Protocol execution blocked: No managed browser session is active, Sir. "
            "Autonomous execution cannot connect to unmanaged browser port 9222 without trusted approval authorization."
        )

    target_port = resolve_cdp_port(port=None, allow_unmanaged=allow_unmanaged)
    if target_port is None or not is_cdp_available(port=target_port, allow_unmanaged=allow_unmanaged):
        return (
            "Browser DevTools Protocol is not currently attached (port 9222 is closed), Sir. "
            "Please launch the browser with debugging enabled or use 'launch_debug_browser'."
        )

    data = read_webpage_content_cdp(target_id_or_title=tab_name_or_url, port=target_port, allow_unmanaged=allow_unmanaged)
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
    risk_level=2,
    read_only=False,
    external_ingestion=False,
)
def execute_browser_javascript(
    script: str,
    tab_name: str = "",
    allow_unmanaged: bool = False,
    automated: bool = False,
    user_approved: bool = False,
    expected_url: str | None = None,
    target_id: str | None = None,
    execution_context_id: int | None = None,
    frame_id: str | None = None,
    unique_context_id: str | None = None,
    script_digest: str | None = None,
) -> str:
    """Executes JavaScript in the browser tab via CDP."""
    import hashlib
    import hmac

    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    if not unique_context_id or not isinstance(unique_context_id, str) or not unique_context_id.strip():
        raise SecurityBoundaryViolation(
            "Browser Context Violation: execute_browser_javascript requires a valid non-empty 'unique_context_id' at execution time."
        )
    if not script_digest or not isinstance(script_digest, str) or not script_digest.strip():
        raise SecurityBoundaryViolation(
            "Script Integrity Violation: execute_browser_javascript requires a valid 'script_digest' at execution time."
        )

    actual_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_digest, script_digest):
        raise SecurityBoundaryViolation(
            f"Script Integrity Violation: JavaScript content does not match approved script_digest (expected '{script_digest}', got '{actual_digest}'). Script tampering rejected."
        )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: execute_browser_javascript requires explicit human approval."
        )

    if active_token.tool_name != "execute_browser_javascript":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'execute_browser_javascript'."
        )

    approved_args = getattr(active_token, "arguments", {}) or {}
    approved_unique_ctx = approved_args.get("unique_context_id")
    if approved_unique_ctx and unique_context_id != approved_unique_ctx:
        raise SecurityBoundaryViolation(
            f"Browser Context Violation: unique_context_id '{unique_context_id}' does not match approved context '{approved_unique_ctx}'."
        )

    approved_script_digest = approved_args.get("script_digest")
    if approved_script_digest and not hmac.compare_digest(script_digest, approved_script_digest):
        raise SecurityBoundaryViolation(
            f"Script Integrity Violation: script_digest '{script_digest}' does not match approved script_digest '{approved_script_digest}'."
        )

    full_cand = {
        "script": script,
        "tab_name": tab_name,
        "allow_unmanaged": allow_unmanaged,
        "unique_context_id": unique_context_id,
        "script_digest": script_digest,
    }
    if expected_url is not None:
        full_cand["expected_url"] = expected_url
    if target_id is not None:
        full_cand["target_id"] = target_id
    if execution_context_id is not None:
        full_cand["execution_context_id"] = execution_context_id
    if frame_id is not None:
        full_cand["frame_id"] = frame_id

    candidate_args = [full_cand]
    if not tab_name:
        c1 = dict(full_cand)
        c1.pop("tab_name", None)
        candidate_args.append(c1)
    if not allow_unmanaged:
        c2 = dict(full_cand)
        c2.pop("allow_unmanaged", None)
        candidate_args.append(c2)
    if not tab_name and not allow_unmanaged:
        c3 = dict(full_cand)
        c3.pop("tab_name", None)
        c3.pop("allow_unmanaged", None)
        candidate_args.append(c3)

    matched_hash = None
    for cand in candidate_args:
        cand_hash = compute_action_hash("execute_browser_javascript", cand)
        if hmac.compare_digest(active_token.action_hash, cand_hash):
            matched_hash = cand_hash
            break

    if matched_hash is None:
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: Browser JavaScript execution arguments do not match approved action hash."
        )

    if not active_token.consume("execute_browser_javascript", matched_hash):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: execute_browser_javascript authorization grant was already consumed or invalidated."
        )

    # Record untrusted external web provenance for executed script results
    from jarvis.core.security_boundaries import TurnTaintTracker

    TurnTaintTracker.get_instance().record_ingestion(
        "execute_browser_javascript",
        {"script": str(script or "")[:100], "tab_name": str(tab_name or "")},
    )

    managed_port = get_managed_cdp_port()
    if managed_port is None and not is_unmanaged_cdp_authorized(allow_unmanaged):
        return (
            "Browser DevTools Protocol execution blocked: No managed browser session is active, Sir. "
            "Autonomous execution cannot connect to unmanaged browser port 9222 without trusted approval authorization."
        )

    target_port = resolve_cdp_port(port=None, allow_unmanaged=allow_unmanaged)
    if target_port is None or not is_cdp_available(port=target_port, allow_unmanaged=allow_unmanaged):
        return "Browser DevTools Protocol is not currently attached (port is closed), Sir."

    # Immediate pre-dispatch verification of active tab and context
    tabs = list_cdp_tabs(port=target_port, allow_unmanaged=allow_unmanaged)
    target_tab = None
    if tabs:
        if target_id:
            for t in tabs:
                if t.get("id") == target_id:
                    target_tab = t
                    break
            if not target_tab:
                raise SecurityBoundaryViolation(
                    f"Browser Context Violation: Target session ID '{target_id}' was closed or substituted. Actuation aborted."
                )
        elif tab_name and tab_name.strip():
            q_lower = tab_name.lower().strip()
            for t in tabs:
                if t.get("id") == tab_name or q_lower in t.get("title", "").lower() or q_lower in t.get("url", "").lower():
                    target_tab = t
                    break
        else:
            target_tab = tabs[0]
    elif target_id:
        raise SecurityBoundaryViolation(
            f"Browser Context Violation: Target session ID '{target_id}' is not active. Actuation aborted."
        )

    if target_tab:
        # Verify expected URL / origin
        if expected_url is not None:
            tab_url = target_tab.get("url", "")
            if tab_url != expected_url and expected_url not in tab_url:
                raise SecurityBoundaryViolation(
                    f"Browser Context Violation: Active tab navigated away from approved URL (expected '{expected_url}', got '{tab_url}'). Navigation hijack rejected."
                )

        # Verify execution context ID if provided or present
        if execution_context_id is not None:
            live_ctx_id = target_tab.get("execution_context_id")
            if live_ctx_id is not None and live_ctx_id != execution_context_id:
                raise SecurityBoundaryViolation(
                    f"Browser Context Violation: CDP execution context {execution_context_id} was destroyed or replaced (found {live_ctx_id}). Actuation aborted."
                )

        # Verify unique execution context ID if provided
        if unique_context_id is not None:
            live_uniq_id = target_tab.get("unique_context_id")
            if live_uniq_id is not None and live_uniq_id != unique_context_id:
                raise SecurityBoundaryViolation(
                    f"Browser Context Violation: Unique execution context ID '{unique_context_id}' mismatch (found '{live_uniq_id}'). Actuation aborted."
                )

        # Verify frame identity if provided
        if frame_id is not None:
            live_frame_id = target_tab.get("frame_id")
            if live_frame_id is not None and live_frame_id != frame_id:
                raise SecurityBoundaryViolation(
                    f"Browser Context Violation: Frame identity '{frame_id}' mismatch (found '{live_frame_id}'). Actuation aborted."
                )

    target_id_or_title = (target_tab.get("id") if target_tab else None) or target_id or tab_name
    extra_cdp_kwargs: dict[str, Any] = {}
    if execution_context_id is not None:
        extra_cdp_kwargs["execution_context_id"] = execution_context_id
    if unique_context_id is not None:
        extra_cdp_kwargs["unique_context_id"] = unique_context_id

    res = execute_cdp_javascript(
        expression=script,
        target_id_or_title=target_id_or_title,
        port=target_port,
        **extra_cdp_kwargs,
    )
    if res is None:
        return "JavaScript execution returned no result or encountered an error, Sir."
    return f"JavaScript Evaluation Result, Sir:\n{res}"


@SkillRegistry.register(
    name="launch_debug_browser",
    description="Launches or connects a browser session with Chrome DevTools Protocol (CDP port 9222) enabled for ultra-fast silent automation."
)
def launch_debug_browser(browser: str = "brave", dedicated_profile: bool = True) -> str:
    """Starts browser with remote debugging port 9222."""
    _success, msg = bootstrap_cdp_browser(browser_name=browser, dedicated_profile=dedicated_profile)
    return msg


@SkillRegistry.register(
    name="scrape_and_archive_webpage",
    description="Extracts the readable text and documentation from an open browser tab and archives it directly into Jarvis's local JIT document memory vault for future semantic retrieval.",
    external_ingestion=True,
)
def scrape_and_archive_webpage(tab_name_or_url: str = "", category: str = "web_intel", allow_unmanaged: bool = False) -> str:
    """Extracts webpage content via CDP and permanently stores it in Tier 3 Document Memory."""
    managed_port = get_managed_cdp_port()
    if managed_port is None and not is_unmanaged_cdp_authorized(allow_unmanaged):
        return (
            "Browser DevTools Protocol execution blocked: No managed browser session is active, Sir. "
            "Autonomous execution cannot connect to unmanaged browser port 9222 without trusted approval authorization."
        )

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
        doc_id = mem.save_document_knowledge(
            title=title,
            category=category,
            summary=text[:500],
            raw_content=full_doc,
            file_path=url,
            source_provenance="web_scrape",
        )
        return (
            f"Archived webpage '{title}' ({len(text)} characters) into your local document vault "
            f"(ID: #{doc_id}, Category: {category}), Sir. It is now indexed for semantic and keyword retrieval."
        )
    except (OSError, RuntimeError, ValueError) as e:
        return f"Failed to archive webpage content into memory: {e}"


__all__ = [
    "_ACTIVE_MANAGED_PORTS",
    "_ACTIVE_MANAGED_PORTS_LOCK",
    "_ACTIVE_TAB_SESSIONS",
    "_CDP_ATEXIT_LOCK",
    "_CDP_ATEXIT_REGISTERED",
    "_CDP_CACHE_TTL_SEC",
    "_CDP_PROBE_CACHE",
    "_DESTRUCTIVE_CDP_URLS",
    "_MANAGED_BROWSER_LOCK",
    "_MANAGED_BROWSER_SESSION",
    "_SAFE_INTERNAL_PAGES",
    "_SESSION_REGISTRY_LOCK",
    "CDPTabSession",
    "ManagedBrowserSession",
    "NavigationRedirectValidator",
    "_cdp_atexit_handler",
    "activate_cdp_tab",
    "bootstrap_cdp_browser",
    "clean_stale_managed_profiles",
    "clear_all_tab_sessions",
    "close_cdp_tab",
    "configure_cdp_tab_network_guards",
    "ensure_managed_browser",
    "execute_browser_javascript",
    "execute_cdp_javascript",
    "get_blocked_localhost_ports",
    "get_cdp_blocked_url_patterns",
    "get_managed_browser_session",
    "get_managed_cdp_port",
    "get_tab_session",
    "is_cdp_available",
    "is_managed_cdp_port",
    "is_unmanaged_cdp_authorized",
    "launch_debug_browser",
    "list_cdp_tabs",
    "list_open_browser_tabs",
    "open_or_navigate_tab_cdp",
    "prune_stale_tab_sessions",
    "read_webpage_content",
    "read_webpage_content_cdp",
    "recv_cdp_response",
    "register_cdp_atexit_cleanup",
    "register_tab_session",
    "remove_tab_session",
    "resolve_browser_executable",
    "resolve_cdp_port",
    "sanitize_browser_url",
    "scrape_and_archive_webpage",
    "stop_managed_browser",
    "switch_to_browser_tab",
    "validate_browser_navigation_chain",
]
