"""Autonomous Multi-Source Deep Web Researcher for Jarvis 2.0."""
import concurrent.futures
import datetime
import ipaddress
import logging
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.DeepResearch")

RESEARCH_DIR = Path("data/research")


def _normalize_text(text: str) -> str:
    """Replaces non-standard unicode punctuation with standard ASCII equivalents."""
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2014": "--", "\u2013": "-", "\u2026": "...", "\xa0": " ",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


class _HTMLTextExtractor(HTMLParser):
    """Native standard-library HTML parser that strips scripts, styles, and extracts visible text."""

    def __init__(self):
        super().__init__()
        self.text_parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style", "noscript", "svg", "header", "footer", "nav", "aside"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript", "svg", "header", "footer", "nav", "aside"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s:
                self.text_parts.append(s)

    def get_text(self) -> str:
        return " ".join(self.text_parts)


def _is_safe_url(url: str) -> bool:
    """SSRF & security filter: ensures URL is a valid public web page, blocking localhost, intranet & binaries."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        host = parsed.hostname
        if not host:
            return False

        host_lower = host.lower()
        if host_lower in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return False

        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass  # It is a domain name

        path_lower = parsed.path.lower()
        disallowed_exts = (".pdf", ".exe", ".zip", ".tar", ".gz", ".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".apk")
        if any(path_lower.endswith(ext) for ext in disallowed_exts):
            return False

        return True
    except Exception:
        return False


def _fetch_page_content(url: str, timeout: float = 4.0) -> str | None:
    """Fetches and extracts clean visible text from a webpage using standard urllib."""
    if not _is_safe_url(url):
        return None

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return None
            raw_html = response.read(1024 * 512).decode("utf-8", errors="ignore")  # Max 512KB

        parser = _HTMLTextExtractor()
        parser.feed(raw_html)
        text = parser.get_text()
        return text if len(text) > 100 else None
    except Exception as e:
        logger.debug(f"Webpage fetch notice for '{url}': {e}")
        return None


def _slugify(text: str) -> str:
    """Converts a topic string into a filesystem-safe slug."""
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "_", s)[:50] or "research_brief"


@SkillRegistry.register(
    name="deep_research",
    description="Conducts autonomous multi-source deep web research on any technical topic or current event, synthesizing key insights into a structured report (e.g. 'deep research quantum computing breakthroughs').",
    external_ingestion=True,
)
def deep_research(topic: str, max_sources: int = 3) -> str:
    """Orchestrates multi-source search, concurrent webpage extraction, and structured intelligence briefing."""
    if not topic or not topic.strip():
        return "Please provide a specific research topic or query, Sir."

    clean_topic = topic.strip()
    logger.info(f"Initiating deep research on '{clean_topic}'...")

    # Step 1: DuckDuckGo search retrieval
    search_hits: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for hit in ddgs.text(clean_topic, max_results=max(3, max_sources * 2)):
                href = hit.get("href", "")
                if href and _is_safe_url(href):
                    search_hits.append({
                        "title": hit.get("title", ""),
                        "url": href,
                        "snippet": hit.get("body", ""),
                    })
    except Exception as e:
        logger.warning(f"DuckDuckGo search error in deep research: {e}")

    if not search_hits:
        return f"Could not locate external intelligence sources for '{clean_topic}', Sir."

    # Filter top candidate URLs
    candidate_sources = search_hits[:max_sources]

    # Step 2: Concurrent multi-source page scraping
    extracted_sources: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(candidate_sources))) as executor:
        future_to_source = {
            executor.submit(_fetch_page_content, s["url"], 4.5): s
            for s in candidate_sources
        }
        for future in concurrent.futures.as_completed(future_to_source):
            src_info = future_to_source[future]
            try:
                content = future.result()
                if content:
                    src_info["body"] = content[:2000]
                    extracted_sources.append(src_info)
                else:
                    # Fallback to search snippet if page blocked
                    src_info["body"] = src_info["snippet"]
                    extracted_sources.append(src_info)
            except Exception:
                src_info["body"] = src_info["snippet"]
                extracted_sources.append(src_info)

    # Step 3: Synthesis of Intelligence Briefing
    now_str = datetime.datetime.now().strftime("%B %d, %Y - %I:%M %p")
    report_lines = [
        f"# Deep Intelligence Briefing: {clean_topic.title()}",
        "**Compiled by:** Jarvis 2.0 Autonomous Research Sentinel  ",
        f"**Timestamp:** {now_str}  ",
        f"**Sources Analyzed:** {len(extracted_sources)}\n",
        "## Executive Summary",
    ]

    # Compile executive summary points from findings
    key_points = []
    for s in extracted_sources:
        title = s.get("title", "Reference")
        snippet = s.get("snippet", "")
        body = s.get("body", "")
        # Extract first 2 meaningful sentences
        sentences = [sen.strip() for sen in re.split(r"[.!?]\s+", body or snippet) if len(sen.strip()) > 25]
        summary_sentence = sentences[0] if sentences else snippet[:150]
        key_points.append(f"• **{title}**: {summary_sentence}.")

    report_lines.extend(key_points[:5])
    report_lines.append("\n## Verified Citations & References")
    for idx, s in enumerate(extracted_sources, start=1):
        report_lines.append(f"{idx}. [{s.get('title', 'Source')}]({s.get('url')})")

    full_report = "\n".join(report_lines)

    # Step 4: Save persistent report to data/research/
    try:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        slug = _slugify(clean_topic)
        report_path = RESEARCH_DIR / f"{slug}.md"
        report_path.write_text(full_report, encoding="utf-8")
        save_notice = f" Full dossier saved to '{report_path}'."
    except Exception as e:
        logger.debug(f"Report save notice: {e}")
        save_notice = ""

    # Spoken executive summary for Jarvis response
    spoken_points = key_points[:2]
    spoken_summary = " ".join(spoken_points).replace("**", "")

    output = (
        f"Deep research completed for '{clean_topic}', Sir.\n\n"
        f"Key Intelligence:\n{spoken_summary}\n\n"
        f"Sources Consulted ({len(extracted_sources)}):\n" +
        "\n".join(f"• {s['title']} ({s['url']})" for s in extracted_sources) +
        f"\n\n{save_notice}"
    )
    return _normalize_text(output)
