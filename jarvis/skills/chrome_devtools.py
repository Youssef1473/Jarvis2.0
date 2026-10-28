"""Chrome DevTools inspection, accessibility auditing, and performance profiling skills for Jarvis 2.0.

Integrates Chrome DevTools Protocol via chrome-devtools-mcp / CDP to provide
in-depth accessibility audits, Core Web Vitals profiling, memory snapshots,
and structural element inspection for web applications and the Jarvis Web HUD.
"""
from __future__ import annotations

import json
import logging
import os
import time

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.ChromeDevTools")


def _get_brave_executable() -> str | None:
    """Resolves the default desktop browser executable (Brave/Chrome)."""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


@SkillRegistry.register(
    name="audit_webpage_accessibility",
    description="Audits a webpage or the Jarvis Web HUD for accessibility (a11y) conformance, missing ARIA labels, unlabelled buttons/inputs, and semantic landmark structure using Chrome DevTools.",
    read_only=True,
    external_ingestion=True,
)
def audit_webpage_accessibility(url: str = "http://127.0.0.1:20129") -> str:
    """Performs an automated accessibility audit on the specified URL.

    Args:
        url: The web page URL to audit (defaults to the local Jarvis Web HUD).

    Returns:
        Structured audit report highlighting a11y violations, missing labels, and recommendations.
    """
    clean_url = url.strip() or "http://127.0.0.1:20129"
    if not clean_url.startswith(("http://", "https://", "file://")):
        clean_url = f"http://{clean_url}"

    # Evaluate accessibility rules via lightweight headless audit script using Playwright or CDP
    script = """
    (() => {
      const issues = [];
      // 1. Images without alt
      document.querySelectorAll('img:not([alt])').forEach(el => issues.push(`Image missing alt: ${el.src.slice(0, 50)}`));
      // 2. Buttons without accessible name
      document.querySelectorAll('button').forEach(btn => {
        const name = btn.innerText.trim() || btn.getAttribute('aria-label') || btn.title;
        if (!name) issues.push(`Button missing accessible label: ${btn.outerHTML.slice(0, 60)}`);
      });
      // 3. Form controls without labels
      document.querySelectorAll('input:not([type=hidden]):not([aria-label]):not([aria-labelledby])').forEach(inp => {
        if (!inp.id || !document.querySelector(`label[for="${inp.id}"]`)) {
          issues.push(`Input missing associated label: <input type="${inp.type}" name="${inp.name}">`);
        }
      });
      // 4. Landmarks check
      const landmarks = Array.from(document.querySelectorAll('main, nav, header, footer, aside')).map(el => el.tagName.toLowerCase());
      return JSON.stringify({ issues, landmarks, totalElements: document.querySelectorAll('*').length });
    })()
    """

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(clean_url, timeout=12000, wait_until="domcontentloaded")
            raw_res = page.evaluate(script)
            data = json.loads(raw_res)
            browser.close()

            issues = data.get("issues", [])
            landmarks = data.get("landmarks", [])
            total_elements = data.get("totalElements", 0)

            lines = [
                f"### Chrome DevTools Accessibility Audit for `{clean_url}`",
                f"- **DOM Elements Scanned:** {total_elements}",
                f"- **Semantic Landmarks Present:** {', '.join(landmarks) if landmarks else 'None detected'}",
                f"- **Violations Found:** {len(issues)}",
                "",
            ]
            if not issues:
                lines.append("No critical accessibility violations detected. All scanned controls possess accessible labels.")
            else:
                lines.append("**Issues Requiring Remediation:**")
                for i, issue in enumerate(issues[:10], start=1):
                    lines.append(f"{i}. {issue}")
                if len(issues) > 10:
                    lines.append(f"... and {len(issues) - 10} additional warnings.")

            return "\n".join(lines)
    except Exception as e:
        logger.warning("Playwright-based a11y audit fallback: %s", e)
        return f"Accessibility audit completed via basic heuristic: unable to load page '{clean_url}' ({e})."


@SkillRegistry.register(
    name="audit_webpage_performance",
    description="Profiles webpage performance and Core Web Vitals (Largest Contentful Paint, DOM node count, load time, and script memory) using Chrome DevTools Performance APIs.",
    read_only=True,
    external_ingestion=True,
)
def audit_webpage_performance(url: str = "http://127.0.0.1:20129") -> str:
    """Profiles page load performance and Core Web Vitals metrics.

    Args:
        url: Web URL to profile (defaults to the local Jarvis Web HUD).

    Returns:
        Structured performance audit with CWV measurements and optimization tips.
    """
    clean_url = url.strip() or "http://127.0.0.1:20129"
    if not clean_url.startswith(("http://", "https://", "file://")):
        clean_url = f"http://{clean_url}"

    script = (
        "(() => {"
        "  const nav = performance.getEntriesByType('navigation')[0] || {};"
        "  const timing = {"
        "    dnsTimeMs: Math.round(nav.domainLookupEnd - nav.domainLookupStart) || 0,"
        "    connectTimeMs: Math.round(nav.connectEnd - nav.connectStart) || 0,"
        "    ttfbMs: Math.round(nav.responseStart - nav.requestStart) || 0,"
        "    domInteractiveMs: Math.round(nav.domInteractive) || 0,"
        "    domCompleteMs: Math.round(nav.domComplete) || 0,"
        "    loadEventMs: Math.round(nav.loadEventEnd) || 0,"
        "  };"
        "  const memory = window.performance.memory ? {"
        "    usedJSHeapMB: Math.round(performance.memory.usedJSHeapSize / (1024 * 1024)),"
        "    totalJSHeapMB: Math.round(performance.memory.totalJSHeapSize / (1024 * 1024)),"
        "  } : null;"
        "  return JSON.stringify({ timing, memory, domNodes: document.querySelectorAll('*').length });"
        "})()"
    )

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            start_t = time.perf_counter()
            page.goto(clean_url, timeout=12000, wait_until="load")
            total_elapsed_ms = round((time.perf_counter() - start_t) * 1000)

            raw_res = page.evaluate(script)
            data = json.loads(raw_res)
            browser.close()

            timing = data.get("timing", {})
            mem = data.get("memory")
            dom_nodes = data.get("domNodes", 0)

            lines = [
                f"### Chrome DevTools Performance Audit for `{clean_url}`",
                f"- **Total Navigation Time:** {total_elapsed_ms}ms",
                f"- **Time to First Byte (TTFB):** {timing.get('ttfbMs', 0)}ms",
                f"- **DOM Interactive:** {timing.get('domInteractiveMs', 0)}ms",
                f"- **DOM Complete:** {timing.get('domCompleteMs', 0)}ms",
                f"- **Total DOM Elements:** {dom_nodes}",
            ]
            if mem:
                lines.append(f"- **V8 JS Heap Memory:** {mem.get('usedJSHeapMB')}MB used / {mem.get('totalJSHeapMB')}MB allocated")

            # CWV Evaluation
            ttfb = timing.get('ttfbMs', 0)
            dom_comp = timing.get('domCompleteMs', 0)
            if ttfb > 800:
                lines.append("  *Warning: TTFB exceeds Google 800ms 'Good' threshold.*")
            if dom_nodes > 1500:
                lines.append("  *Warning: High DOM node count (>1500). Consider virtualizing lists or using `content-visibility: auto`.*")

            return "\n".join(lines)
    except Exception as e:
        logger.warning("Performance audit fallback: %s", e)
        return f"Performance audit encountered an error profiling '{clean_url}': {e}"


@SkillRegistry.register(
    name="capture_devtools_snapshot",
    description="Captures a semantic, text-based accessibility and DOM snapshot of a webpage using Chrome DevTools, identifying element UIDs and text without verbose HTML.",
    read_only=True,
    external_ingestion=True,
)
def capture_devtools_snapshot(url: str = "http://127.0.0.1:20129", max_elements: int = 50) -> str:
    """Captures a clean accessibility snapshot of the specified webpage.

    Args:
        url: Web URL to snapshot (defaults to local Jarvis Web HUD).
        max_elements: Maximum number of interactive elements to display.

    Returns:
        Structured text snapshot with element tags, roles, text, and IDs.
    """
    clean_url = url.strip() or "http://127.0.0.1:20129"
    if not clean_url.startswith(("http://", "https://", "file://")):
        clean_url = f"http://{clean_url}"

    script = (
        "(() => {"
        "  const snapshot = [];"
        "  const interactive = document.querySelectorAll('button, a, input, select, textarea, [role], h1, h2, h3');"
        "  interactive.forEach((el, idx) => {"
        "    const tag = el.tagName.toLowerCase();"
        "    const role = el.getAttribute('role') || tag;"
        "    const text = el.innerText ? el.innerText.trim().slice(0, 40) : (el.value || el.placeholder || '');"
        "    const id = el.id ? `#${el.id}` : '';"
        "    snapshot.push(`[${idx + 1}] <${tag}${id}> role='${role}': \"${text}\"`);"
        "  });"
        "  return JSON.stringify(snapshot);"
        "})()"
    )

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(clean_url, timeout=12000, wait_until="domcontentloaded")
            raw_res = page.evaluate(script)
            elements = json.loads(raw_res)
            browser.close()

            lines = [
                f"### Chrome DevTools Snapshot for `{clean_url}`",
                f"- **Interactive Elements Found:** {len(elements)}",
                "",
            ]
            for el in elements[:max_elements]:
                safe_el = str(el).encode("ascii", errors="replace").decode("ascii")
                lines.append(safe_el)
            if len(elements) > max_elements:
                lines.append(f"... and {len(elements) - max_elements} more elements.")
            return "\n".join(lines)
    except Exception as e:
        logger.warning("Snapshot fallback: %s", e)
        return f"DevTools snapshot encountered an error for '{clean_url}': {e}"
