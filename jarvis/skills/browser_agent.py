"""Autonomous web browsing agent powered by Playwright and live web intelligence."""
import logging
import os
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, List, Optional

import requests
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.BrowserAgent")


class _HTMLTextExtractor(HTMLParser):
    """Lightweight streaming HTML text extractor for fallback browsing."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.text_chunks = []
        self._in_title = False
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "svg", "head"}

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "title":
            self._in_title = True
        elif t in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self._in_title = False
        elif t in self._skip_tags:
            self._skip = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif not self._skip:
            chunk = data.strip()
            if chunk:
                self.text_chunks.append(chunk)

    def get_text(self) -> str:
        return " ".join(self.text_chunks)


def _setup_playwright_ssrf_guard(
    context_or_page,
    violation_holder: List[Optional[str]],
    allow_localhost: bool = False,
    max_hops: int = 4,
    custom_validator: Optional[Callable[[str], None]] = None,
) -> None:
    """Installs route and WebSocket interception on a Playwright BrowserContext or Page to enforce
    fail-closed SSRF validation across all requests, subresources, popups, and redirect chains.

    - Blocks service workers via context configuration (`service_workers='block'`).
    - Intercepts all HTTP/HTTPS requests at the context level (`context.route('**/*')`).
    - Explicitly resolves and iterates multi-hop redirect chains (`max_redirects=0`), validating
      each hop against SSRF, relative Location resolution, loop detection, hop limits, and HTTPS->HTTP downgrades
      before issuing any outbound network request to that hop.
    - Intercepts and validates WebSockets (`context.route_web_socket('**/*')`), closing prohibited endpoints (code 1008).
    """
    from jarvis.core.security_boundaries import validate_egress_url, SecurityBoundaryViolation

    def _http_route_handler(route):
        req_url = route.request.url
        current_url = req_url
        hops = 0
        visited = {current_url}

        # Step 1: Pre-navigation validation for the initial requested URL
        try:
            validate_egress_url(current_url, allow_localhost=allow_localhost)
            if custom_validator:
                custom_validator(current_url)
        except SecurityBoundaryViolation as sec_err:
            violation_holder[0] = str(sec_err)
            logger.warning("SSRF blocked Playwright initial request to '%s': %s", current_url, sec_err)
            route.abort("blockedbyclient")
            return
        except Exception as err:
            violation_holder[0] = f"Invalid destination URL '{current_url}': {err}"
            route.abort("blockedbyclient")
            return

        # Step 2: Handle multi-hop redirects deterministically
        while hops <= max_hops:
            try:
                # Fetch single hop with max_redirects=0
                resp = route.fetch(url=current_url, max_redirects=0)
            except Exception:
                route.abort("failed")
                return

            # If response is not a redirect (e.g. 200, 404, 500), fulfill into the DOM
            if resp.status not in (301, 302, 303, 307, 308):
                try:
                    route.fulfill(response=resp)
                except Exception:
                    route.abort("failed")
                return

            # It's a redirect: inspect Location header
            loc = resp.headers.get("location")
            if not loc:
                try:
                    route.fulfill(response=resp)
                except Exception:
                    route.abort("failed")
                return

            hops += 1
            if hops > max_hops:
                violation_holder[0] = f"Redirect limit exceeded (maximum {max_hops} hops)."
                logger.warning("SSRF blocked Playwright redirect: exceeded max hops (%d)", max_hops)
                route.abort("blockedbyclient")
                return

            resolved_hop = urllib.parse.urljoin(current_url, loc)

            # Check HTTPS -> HTTP downgrade
            old_scheme = urllib.parse.urlsplit(current_url).scheme.lower()
            new_scheme = urllib.parse.urlsplit(resolved_hop).scheme.lower()
            if old_scheme == "https" and new_scheme == "http":
                err_msg = f"Insecure redirect downgrade from HTTPS to HTTP blocked: '{current_url}' -> '{resolved_hop}'"
                violation_holder[0] = err_msg
                logger.warning("SSRF blocked Playwright redirect downgrade: %s", err_msg)
                route.abort("blockedbyclient")
                return

            # Check redirect loop
            if resolved_hop in visited:
                err_msg = f"Redirect loop detected: '{resolved_hop}' already visited in chain."
                violation_holder[0] = err_msg
                logger.warning("SSRF blocked Playwright redirect loop: %s", err_msg)
                route.abort("blockedbyclient")
                return
            visited.add(resolved_hop)

            # SSRF validation on the resolved redirect destination BEFORE network request
            try:
                validate_egress_url(resolved_hop, allow_localhost=allow_localhost)
                if custom_validator:
                    custom_validator(resolved_hop)
            except SecurityBoundaryViolation as redir_sec_err:
                violation_holder[0] = str(redir_sec_err)
                logger.warning("SSRF blocked Playwright redirect hop to '%s': %s", resolved_hop, redir_sec_err)
                route.abort("blockedbyclient")
                return
            except Exception as err:
                violation_holder[0] = f"Invalid redirect destination URL '{resolved_hop}': {err}"
                route.abort("blockedbyclient")
                return

            # Safe to advance to next hop in the chain
            current_url = resolved_hop

        violation_holder[0] = f"Redirect limit exceeded (maximum {max_hops} hops)."
        route.abort("blockedbyclient")

    if hasattr(context_or_page, "route"):
        context_or_page.route("**/*", _http_route_handler)

    if hasattr(context_or_page, "route_web_socket"):
        def _ws_route_handler(ws_route):
            ws_url = ws_route.url
            try:
                validate_egress_url(
                    ws_url,
                    allowed_schemes=("ws", "wss", "http", "https"),
                    allow_localhost=allow_localhost,
                )
                ws_route.connect_to_server()
            except SecurityBoundaryViolation as ws_sec_err:
                violation_holder[0] = str(ws_sec_err)
                logger.warning("SSRF blocked Playwright WebSocket to '%s': %s", ws_url, ws_sec_err)
                ws_route.close(code=1008, reason="Blocked by SSRF policy")
            except Exception as ws_err:
                violation_holder[0] = f"Invalid WebSocket URL '{ws_url}': {ws_err}"
                ws_route.close(code=1008, reason="Invalid WebSocket URL")

        context_or_page.route_web_socket("**/*", _ws_route_handler)


@SkillRegistry.register(
    name="browse_and_read_webpage",
    description="Autonomously navigates to any URL using headless Chromium, parses the DOM, and extracts the core textual content.",
    external_ingestion=True,
)
def browse_and_read_webpage(url: str, max_chars: int = 2500) -> str:
    """Navigates to URL using Playwright and extracts body text with fail-closed SSRF egress validation."""
    from jarvis.core.security_boundaries import (
        validate_egress_url,
        build_safe_url_opener,
        SecurityBoundaryViolation,
    )

    clean_url = str(url or "").strip()
    if not clean_url:
        return "Failed to browse webpage: URL cannot be empty."

    # Ensure scheme
    parsed_initial = urllib.parse.urlsplit(clean_url)
    if parsed_initial.scheme:
        if parsed_initial.scheme.lower() not in ("http", "https"):
            return f"Security violation blocked browsing operation: Blocked URL scheme '{parsed_initial.scheme}'."
    else:
        clean_url = "https://" + clean_url

    try:
        safe_url = validate_egress_url(clean_url, allow_localhost=False)
    except SecurityBoundaryViolation as sec_err:
        return f"Security violation blocked browsing operation: {sec_err}"
    except Exception as err:
        return f"Invalid URL for browsing operation: {err}"

    ssrf_violation: List[Optional[str]] = [None]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                service_workers="block",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            _setup_playwright_ssrf_guard(context, ssrf_violation, allow_localhost=False)
            page = context.new_page()
            page.on("popup", lambda popup: popup.close())
            page.goto(safe_url, timeout=15000, wait_until="domcontentloaded")
            title = page.title()

            # Extract clean visible text
            body_text = page.inner_text("body")
            browser.close()

            # Clean and truncate text
            cleaned_lines = [line.strip() for line in body_text.splitlines() if line.strip()]
            full_text = " ".join(cleaned_lines)
            truncated = full_text[:max_chars]

            return f"Page Title: {title}\nURL: {safe_url}\n\nExtracted Content:\n{truncated}..."
    except Exception as e:
        if ssrf_violation[0]:
            return f"Security violation blocked browsing operation: {ssrf_violation[0]}"

        # Fallback to hardened urllib with SafeRedirectHandler and socket IP pinning
        try:
            opener = build_safe_url_opener(allow_localhost=False)
            req = urllib.request.Request(
                safe_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
            )
            with opener.open(req, timeout=10) as res:
                charset = res.headers.get_content_charset() or "utf-8"
                raw_html = res.read().decode(charset, errors="replace")

            parser = _HTMLTextExtractor()
            parser.feed(raw_html)
            title = parser.title or "Untitled"
            full_text = parser.get_text()
            truncated = full_text[:max_chars]
            return f"Page Title: {title}\nURL: {safe_url}\n\nExtracted Content:\n{truncated}..."
        except SecurityBoundaryViolation as sec_err:
            return f"Security violation blocked fallback browsing operation: {sec_err}"
        except Exception as e2:
            return f"Failed to browse webpage '{safe_url}': {e} (Fallback also failed: {e2})"


@SkillRegistry.register(
    name="open_url_in_desktop_browser",
    description="Opens a website or link in Sir's visible desktop browser so they can see and interact with it."
)
def open_url_in_desktop_browser(url: str) -> str:
    """Opens URL in user's default desktop browser after scheme validation."""
    clean_url = str(url or "").strip()
    if not clean_url:
        return "Failed to open browser: URL cannot be empty, Sir."

    parsed = urllib.parse.urlsplit(clean_url)
    if parsed.scheme:
        if parsed.scheme.lower() not in ("http", "https"):
            return f"Blocked opening URL with unsupported scheme '{parsed.scheme}'."
    else:
        clean_url = "https://" + clean_url

    try:
        webbrowser.open(clean_url)
        return f"Opened '{clean_url}' in your desktop browser, Sir."
    except Exception as e:
        return f"Failed to open browser: {e}"


@SkillRegistry.register(
    name="get_tech_news_headlines",
    description="Fetches live breaking tech news and top stories from Hacker News.",
    external_ingestion=True,
)
def get_tech_news_headlines(count: int = 5) -> str:
    """Retrieves top live headlines from Hacker News API."""
    try:
        top_ids_res = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=5)
        top_ids = top_ids_res.json()[:count]

        stories = []
        for rank, item_id in enumerate(top_ids, start=1):
            try:
                item_data = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", timeout=3).json()
                title = item_data.get("title", "")
                score = item_data.get("score", 0)
                url = item_data.get("url", "")
                stories.append(f"{rank}. {title} ({score} points)")
            except Exception:
                pass

        if not stories:
            return "No news stories retrieved at this time."

        return "Top Trending Tech Headlines:\n" + "\n".join(stories)
    except Exception as e:
        return f"Failed to fetch tech headlines: {e}"


@SkillRegistry.register(
    name="capture_webpage_screenshot",
    description="Navigates to a webpage using Playwright and saves a screenshot image file."
)
def capture_webpage_screenshot(url: str, filename: str = "web_snapshot.png") -> str:
    """Captures screenshot of a webpage with SSRF and path confinement safety gates."""
    from jarvis.core.security_boundaries import validate_egress_url, validate_confined_path, SecurityBoundaryViolation

    clean_url = str(url or "").strip()
    if not clean_url:
        return "Failed to capture webpage screenshot: URL cannot be empty."

    parsed_initial = urllib.parse.urlsplit(clean_url)
    if parsed_initial.scheme:
        if parsed_initial.scheme.lower() not in ("http", "https"):
            return f"Security violation blocked screenshot operation: Blocked URL scheme '{parsed_initial.scheme}'."
    else:
        clean_url = "https://" + clean_url

    try:
        safe_url = validate_egress_url(clean_url, allow_localhost=False)
        data_dir = os.path.abspath("data")
        raw_out = os.path.join(data_dir, filename)
        safe_out_path = validate_confined_path(raw_out, data_dir)
        out_path = Path(safe_out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except SecurityBoundaryViolation as sec_err:
        return f"Security violation blocked screenshot operation: {sec_err}"
    except Exception as err:
        return f"Invalid screenshot operation parameters: {err}"

    ssrf_violation: List[Optional[str]] = [None]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                service_workers="block",
            )
            _setup_playwright_ssrf_guard(context, ssrf_violation, allow_localhost=False)
            page = context.new_page()
            page.on("popup", lambda popup: popup.close())
            page.goto(safe_url, timeout=15000, wait_until="domcontentloaded")
            page.screenshot(path=str(out_path), full_page=False)
            browser.close()

        return f"Webpage screenshot captured and saved to: {out_path.resolve()}"
    except Exception as e:
        if ssrf_violation[0]:
            return f"Security violation blocked screenshot operation: {ssrf_violation[0]}"
        return f"Failed to capture webpage screenshot: {e}"
