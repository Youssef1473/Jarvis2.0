"""Gemini Intelligence & API Documentation skills for Jarvis 2.0.

Provides real-time documentation search and retrieval directly from the official
Google Gemini API documentation MCP service (https://gemini-api-docs-mcp.dev),
as well as architectural guidelines for Gemini Live API and Interactions API.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.GeminiIntelligence")

GEMINI_DOCS_ENDPOINT = "https://gemini-api-docs-mcp.dev"
REQUEST_TIMEOUT_SEC = 8.0

_DOCS_SEARCH_CACHE: dict[str, Any] = {}
_DOCS_CHUNK_CACHE: dict[str, str] = {}


def _call_gemini_docs_mcp(method_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Sends a JSON-RPC 2.0 call to the Gemini docs MCP endpoint."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": method_name,
            "arguments": arguments,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2024-11-05",
        "User-Agent": "Jarvis-Gemini-DocClient/2.0",
    }
    req = urllib.request.Request(
        GEMINI_DOCS_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    if not req.full_url.startswith("https://"):
        raise ValueError(f"Insecure scheme for Gemini Docs endpoint: {req.full_url}")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        resp_bytes = resp.read()
        raw_text = resp_bytes.decode("utf-8").strip()
        if raw_text.startswith("{") or raw_text.startswith("["):
            return json.loads(raw_text)
        for line in raw_text.splitlines():
            line_str = line.strip()
            if line_str.startswith("data:"):
                return json.loads(line_str[5:].strip())
        return json.loads(raw_text)


@SkillRegistry.register(
    name="search_gemini_api_documentation",
    description="Searches current upstream Google Gemini API, Interactions API, Live API, and SDK documentation for method signatures, parameters, and official examples.",
    read_only=True,
    external_ingestion=True,
)
def search_gemini_api_documentation(
    query: str,
    language: str = "python",
    scope: str = "sdk",
    limit: int = 5,
) -> str:
    """Searches official Gemini API and SDK documentation.

    Args:
        query: Method names, class names, or task description (e.g. 'generate_content', 'Interactions API', 'audio streaming').
        language: Programming language ('python', 'javascript', 'go', 'java', 'dotnet').
        scope: Documentation scope ('sdk', 'api-reference', 'guide', 'migration', 'type-reference').
        limit: Number of results to return (1-20).

    Returns:
        Formatted documentation search hits with headings, snippets, and chunk IDs.
    """
    clean_query = query.strip()
    if not clean_query:
        return "Please specify a search query for Gemini API documentation."

    cache_key = f"{clean_query}_{language}_{scope}_{limit}".lower()
    if cache_key in _DOCS_SEARCH_CACHE:
        parsed_data = _DOCS_SEARCH_CACHE[cache_key]
    else:
        try:
            rpc_resp = _call_gemini_docs_mcp(
                "gemini_search_docs",
                {
                    "query": clean_query,
                    "language": language.lower() if language else "python",
                    "scope": scope,
                    "limit": limit,
                },
            )
            content_list = rpc_resp.get("result", {}).get("content", [])
            if not content_list:
                return f"No documentation entries found for query '{clean_query}'."

            raw_json_str = content_list[0].get("text", "{}")
            parsed_data = json.loads(raw_json_str)
            _DOCS_SEARCH_CACHE[cache_key] = parsed_data
        except Exception as e:
            logger.error("Failed to query Gemini API documentation MCP: %s", e)
            return f"Failed to retrieve Gemini documentation: {e}"

    hits = parsed_data.get("hits", [])
    if not hits:
        return f"No documentation matches found for '{clean_query}' in Google Gemini docs."

    lines = [f"### Google Gemini Documentation Search Results for '{clean_query}' ({language}):\n"]
    for idx, hit in enumerate(hits[:limit], start=1):
        chunk_id = hit.get("chunk_id", "")
        heading = hit.get("heading", "Untitled")
        breadcrumb = hit.get("breadcrumb", "")
        snippet = hit.get("snippet", "").strip().replace("\n", " ")
        lines.append(f"**{idx}. {heading}**")
        if breadcrumb:
            lines.append(f"*Path:* `{breadcrumb}`")
        lines.append(f"*Chunk ID:* `{chunk_id}`")
        if snippet:
            lines.append(f"*Snippet:* {snippet[:200]}...")
        lines.append("")

    lines.append("To read full content for any result, call `get_gemini_documentation_chunk` with the `chunk_id`.")
    return "\n".join(lines)


@SkillRegistry.register(
    name="get_gemini_documentation_chunk",
    description="Retrieves the full content of an official Google Gemini documentation section using its chunk_id from search results.",
    read_only=True,
    external_ingestion=True,
)
def get_gemini_documentation_chunk(chunk_id: str, max_chars: int = 4000) -> str:
    """Retrieves full text of a specific Google Gemini documentation chunk.

    Args:
        chunk_id: Exact chunk ID returned by search_gemini_api_documentation.
        max_chars: Maximum characters to bound token budget.

    Returns:
        Full markdown content of the requested documentation section.
    """
    clean_id = chunk_id.strip().strip("'\"")
    if not clean_id:
        return "Please specify a chunk_id to retrieve."

    if clean_id in _DOCS_CHUNK_CACHE:
        text = _DOCS_CHUNK_CACHE[clean_id]
    else:
        try:
            rpc_resp = _call_gemini_docs_mcp(
                "gemini_get_doc",
                {"chunk_id": clean_id, "context": 0},
            )
            content_list = rpc_resp.get("result", {}).get("content", [])
            if not content_list:
                return f"No documentation content found for chunk '{clean_id}'."

            text = content_list[0].get("text", "")
            _DOCS_CHUNK_CACHE[clean_id] = text
        except Exception as e:
            logger.error("Failed to fetch chunk '%s': %s", clean_id, e)
            return f"Failed to retrieve documentation chunk '{clean_id}': {e}"

    if len(text) > max_chars:
        return f"{text[:max_chars]}\n\n... [Truncated at {max_chars} chars. Narrow scope if needed]"

    return text


@SkillRegistry.register(
    name="get_gemini_live_architecture_overview",
    description="Retrieves architectural specifications for real-time bidirectional voice/video streaming using Gemini 3.8 Live API, WebSockets, background thinking, and async tool calling.",
    read_only=True,
)
def get_gemini_live_architecture_overview() -> str:
    """Returns official reference patterns for Gemini 3.8 Live API bidirectional streaming."""
    return (
        "### Gemini 3.8 Live API Architecture Reference\n\n"
        "1. **Protocol & Connection**:\n"
        "   - Bidirectional WebSocket endpoint connecting to `gemini-3.8-live` or `gemini-2.0-flash-exp`.\n"
        "   - Audio format: PCM 16-bit 16kHz mono input; PCM 16-bit 24kHz output.\n"
        "   - Ephemeral token authentication for client-side / browser sessions without exposing master API keys.\n\n"
        "2. **Real-Time Voice & Interruptibility (Barge-in)**:\n"
        "   - Server-side Voice Activity Detection (VAD) automatically truncates queued audio chunks on user speech onset.\n"
        "   - Client must drop local playback buffer instantly upon receiving server `interrupted: true` event frame.\n\n"
        "3. **Background Extended Thinking**:\n"
        "   - Live API supports reasoning while speaking via background thinking budgets.\n"
        "   - Streamed thoughts precede spoken audio tokens, enabling complex tool determination during active conversation.\n\n"
        "4. **Non-Blocking Function Calling**:\n"
        "   - Asynchronous tool call schemas executed while intermediate audio stream keeps latency minimal.\n"
    )
