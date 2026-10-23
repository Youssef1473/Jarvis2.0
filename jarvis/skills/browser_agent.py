"""Autonomous web browsing agent powered by Playwright and live web intelligence."""
import os
import subprocess
import requests
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from jarvis.skills.base import SkillRegistry


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


@SkillRegistry.register(
    name="browse_and_read_webpage",
    description="Autonomously navigates to any URL using headless Chromium, parses the DOM, and extracts the core textual content.",
    external_ingestion=True,
)
def browse_and_read_webpage(url: str, max_chars: int = 2500) -> str:
    """Navigates to URL using Playwright and extracts body text."""
    # Ensure scheme
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            title = page.title()

            # Extract clean visible text
            body_text = page.inner_text("body")
            browser.close()

            # Clean and truncate text
            cleaned_lines = [line.strip() for line in body_text.splitlines() if line.strip()]
            full_text = " ".join(cleaned_lines)
            truncated = full_text[:max_chars]

            return f"Page Title: {title}\nURL: {url}\n\nExtracted Content:\n{truncated}..."
    except Exception as e:
        # Fallback to requests + simple extraction if playwright encounters issues
        try:
            res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            parser = _HTMLTextExtractor()
            parser.feed(res.text)
            title = parser.title or "Untitled"
            full_text = parser.get_text()
            truncated = full_text[:max_chars]
            return f"Page Title: {title}\nURL: {url}\n\nExtracted Content:\n{truncated}..."
        except Exception as e2:
            return f"Failed to browse webpage '{url}': {e} (Fallback also failed: {e2})"


@SkillRegistry.register(
    name="open_url_in_desktop_browser",
    description="Opens a website or link in Sir's visible desktop browser so they can see and interact with it."
)
def open_url_in_desktop_browser(url: str) -> str:
    """Opens URL in user's default desktop browser."""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return f"Opened '{url}' in your desktop browser, Sir."
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

    try:
        clean_url = url if url.startswith(("http://", "https://")) else "https://" + url
        safe_url = validate_egress_url(clean_url)
        data_dir = os.path.abspath("data")
        raw_out = os.path.join(data_dir, filename)
        safe_out_path = validate_confined_path(raw_out, data_dir)
        out_path = Path(safe_out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except SecurityBoundaryViolation as sec_err:
        return f"Security violation blocked screenshot operation: {sec_err}"

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            page.screenshot(path=str(out_path), full_page=False)
            browser.close()

        return f"Webpage screenshot captured and saved to: {out_path.resolve()}"
    except Exception as e:
        return f"Failed to capture webpage screenshot: {e}"
