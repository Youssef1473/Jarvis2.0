"""Modern Web Guidance skills for Jarvis 2.0.

Integrates the official Google Chrome / Modern Web Guidance repository to provide
instant search and retrieval of up-to-date modern web platform capabilities,
CSS architectures, HTML semantics, accessibility, and Core Web Vitals optimization.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.ModernWeb")

# In-memory caches to guarantee sub-10ms response on repeated queries
_SEARCH_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_GUIDE_CACHE: Dict[str, str] = {}


def _get_npx_executable() -> str:
    """Resolves the platform-specific npx executable."""
    executable = shutil.which("npx")
    if not executable and os.name == "nt":
        executable = shutil.which("npx.cmd")
    return executable or "npx"


@SkillRegistry.register(
    name="search_modern_web_guidance",
    description="Searches modern web platform best practices and architectural guidance (CSS layout, container queries, popover API, HTML semantics, CWV performance, accessibility, Chrome extensions) using the Modern Web Guidance knowledge base.",
    read_only=True,
)
def search_modern_web_guidance(query: str, limit: int = 5) -> str:
    """Searches modern web best practice guides via modern-web-guidance CLI.

    Args:
        query: 2-5 word action-oriented query for a web platform capability (e.g., 'responsive dialog', 'css layout', 'drag').
        limit: Maximum number of candidate guides to return.

    Returns:
        Structured summary of relevant modern web best practice guides.
    """
    clean_query = query.strip()
    if not clean_query:
        return "Please specify a query for modern web guidance."

    cache_key = clean_query.lower()
    if cache_key in _SEARCH_CACHE:
        results = _SEARCH_CACHE[cache_key][:limit]
    else:
        npx_cmd = _get_npx_executable()
        try:
            proc = subprocess.run(
                [npx_cmd, "-y", "modern-web-guidance@latest", "search", clean_query],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if proc.returncode != 0:
                logger.warning("modern-web-guidance search failed (code %d): %s", proc.returncode, proc.stderr)
                return f"Modern Web Guidance search failed: {proc.stderr.strip() or 'Unknown error'}"

            raw_out = proc.stdout.strip()
            if not raw_out or raw_out == "[]":
                return f"No modern web guidelines directly matched '{clean_query}'. Consider running with broader keywords (e.g., 'css', 'html', 'performance')."

            results = json.loads(raw_out)
            _SEARCH_CACHE[cache_key] = results
            results = results[:limit]
        except subprocess.TimeoutExpired:
            return f"Modern Web Guidance search timed out for query '{clean_query}'."
        except Exception as e:
            logger.error("Error invoking modern-web-guidance: %s", e)
            return f"Error querying modern web guidance: {e}"

    lines = [f"Found {len(results)} modern web guidance result(s) for '{clean_query}':\n"]
    for idx, guide in enumerate(results, start=1):
        g_id = guide.get("id", "unknown")
        desc = guide.get("description", "No description provided.")
        cat = guide.get("category", "general")
        features = guide.get("featuresUsed", [])
        feat_str = f" | Features: {', '.join(features[:4])}" if features else ""
        lines.append(f"{idx}. [{g_id}] (Category: {cat}{feat_str})\n   {desc}\n")

    lines.append("To inspect the full implementation guide, invoke 'retrieve_modern_web_guide' with the guide ID.")
    return "\n".join(lines)


@SkillRegistry.register(
    name="retrieve_modern_web_guide",
    description="Retrieves the authoritative implementation steps and best practice markdown guide for a specific Modern Web Guidance topic ID (e.g., 'css-layout', 'declarative-dialog-popover-control', 'drag', 'html', 'accessibility').",
    read_only=True,
)
def retrieve_modern_web_guide(guide_id: str, max_chars: int = 4000) -> str:
    """Retrieves full documentation for a given Modern Web Guidance guide ID.

    Args:
        guide_id: The exact guide ID (e.g., 'css', 'html', 'css-layout', 'drag').
        max_chars: Maximum characters to return to bound LLM context window.

    Returns:
        Authoritative guidelines, code patterns, and modern platform gotchas.
    """
    clean_id = guide_id.strip().strip("'\"")
    if not clean_id:
        return "Please specify a guide ID to retrieve."

    if clean_id in _GUIDE_CACHE:
        content = _GUIDE_CACHE[clean_id]
    else:
        npx_cmd = _get_npx_executable()
        try:
            proc = subprocess.run(
                [npx_cmd, "-y", "modern-web-guidance@latest", "retrieve", clean_id],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if proc.returncode != 0:
                logger.warning("modern-web-guidance retrieve failed (code %d): %s", proc.returncode, proc.stderr)
                return f"Could not retrieve guide '{clean_id}': {proc.stderr.strip() or 'Unknown error'}"

            content = proc.stdout.strip()
            if not content:
                return f"Guide '{clean_id}' returned empty content."
            _GUIDE_CACHE[clean_id] = content
        except subprocess.TimeoutExpired:
            return f"Timeout retrieving guide '{clean_id}'."
        except Exception as e:
            logger.error("Error retrieving modern web guide: %s", e)
            return f"Error retrieving modern web guide '{clean_id}': {e}"

    if len(content) > max_chars:
        truncated = content[:max_chars]
        return f"# Modern Web Guidance: {clean_id}\n\n{truncated}\n\n... [Content truncated at {max_chars} chars. Consult specific sub-sections if needed]"

    return f"# Modern Web Guidance: {clean_id}\n\n{content}"
