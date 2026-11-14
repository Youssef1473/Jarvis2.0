"""Chrome DevTools Protocol (CDP) DOM content extraction and JavaScript evaluation."""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from jarvis.skills.cdp_tabs import get_tab_session
from jarvis.skills.cdp_transport import (
    configure_cdp_tab_network_guards,
    is_cdp_available,
    list_cdp_tabs,
    recv_cdp_response,
    resolve_cdp_port,
)

logger = logging.getLogger("Jarvis.BrowserCDP.PageExtractor")


def _get_browser_cdp_symbol(name: str, fallback: Any = None) -> Any:
    """Dynamically resolves a symbol from jarvis.skills.browser_cdp to support mock patching."""
    mod = sys.modules.get("jarvis.skills.browser_cdp")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def read_webpage_content_cdp(
    target_id_or_title: str | None = None,
    max_chars: int = 15000,
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_unmanaged: bool = False,
) -> dict[str, Any] | None:
    """Extracts title, URL, and readable body/article text from an open tab in <5ms using Runtime.evaluate."""
    resolve_fn = _get_browser_cdp_symbol("resolve_cdp_port", resolve_cdp_port)
    avail_fn = _get_browser_cdp_symbol("is_cdp_available", is_cdp_available)
    list_fn = _get_browser_cdp_symbol("list_cdp_tabs", list_cdp_tabs)
    get_sess_fn = _get_browser_cdp_symbol("get_tab_session", get_tab_session)
    recv_fn = _get_browser_cdp_symbol("recv_cdp_response", recv_cdp_response)

    target_port = resolve_fn(port, allow_unmanaged=allow_unmanaged)
    if target_port is None or not avail_fn(host, target_port, allow_unmanaged=allow_unmanaged):
        return None

    try:
        tabs = list_fn(host, target_port, allow_unmanaged=allow_unmanaged)
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

        tab_id = target_tab.get("id")
        active_session = get_sess_fn(tab_id) if tab_id else None
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
        if active_session:
            resp = active_session.send_command(
                "Runtime.evaluate",
                {
                    "expression": js_expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                timeout_sec=1.0,
            )
            val = resp.get("result", {}).get("result", {}).get("value")
            if isinstance(val, dict):
                return val
            return None

        ws_url = target_tab.get("ws_url")
        if not ws_url:
            return None

        from websockets.sync.client import connect
        with connect(ws_url, open_timeout=0.3, close_timeout=0.2) as ws:
            cmd = {
                "id": 201,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": js_expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }
            ws.send(json.dumps(cmd))
            resp = recv_fn(ws, 201, timeout_sec=1.0)
            val = resp.get("result", {}).get("result", {}).get("value")
            if isinstance(val, dict):
                return val
            return None

    except (OSError, RuntimeError, TimeoutError, ValueError) as e:
        logger.warning("Error reading webpage content via CDP: %s", e)
        return None


def execute_cdp_javascript(
    expression: str,
    target_id_or_title: str | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_unmanaged: bool = False,
    execution_context_id: int | None = None,
    unique_context_id: str | None = None,
) -> Any | None:
    """Evaluates JavaScript expression on the target tab via CDP Runtime.evaluate."""
    resolve_fn = _get_browser_cdp_symbol("resolve_cdp_port", resolve_cdp_port)
    avail_fn = _get_browser_cdp_symbol("is_cdp_available", is_cdp_available)
    list_fn = _get_browser_cdp_symbol("list_cdp_tabs", list_cdp_tabs)
    get_sess_fn = _get_browser_cdp_symbol("get_tab_session", get_tab_session)
    guard_fn = _get_browser_cdp_symbol("configure_cdp_tab_network_guards", configure_cdp_tab_network_guards)
    recv_fn = _get_browser_cdp_symbol("recv_cdp_response", recv_cdp_response)

    target_port = resolve_fn(port, allow_unmanaged=allow_unmanaged)
    if target_port is None or not avail_fn(host, target_port, allow_unmanaged=allow_unmanaged) or not expression:
        return None

    try:
        tabs = list_fn(host, target_port, allow_unmanaged=allow_unmanaged)
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

        eval_params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if unique_context_id:
            eval_params["uniqueContextId"] = unique_context_id
        elif execution_context_id is not None:
            eval_params["contextId"] = execution_context_id

        tab_id = target_tab.get("id")
        active_session = get_sess_fn(tab_id) if tab_id else None
        if active_session:
            resp = active_session.send_command(
                "Runtime.evaluate",
                eval_params,
                timeout_sec=5.0,
            )
            return resp.get("result", {}).get("result", {}).get("value")

        ws_url = target_tab.get("ws_url")
        if not ws_url:
            return None

        from websockets.sync.client import connect
        with connect(ws_url, open_timeout=0.3, close_timeout=0.2) as ws:
            # Enforce tab network boundary policy before evaluation
            guard_fn(ws)
            cmd = {
                "id": 202,
                "method": "Runtime.evaluate",
                "params": eval_params,
            }
            ws.send(json.dumps(cmd))
            resp = recv_fn(ws, 202, timeout_sec=5.0)
            return resp.get("result", {}).get("result", {}).get("value")

    except (OSError, RuntimeError, TimeoutError, ValueError) as e:
        logger.warning("Error executing JavaScript via CDP: %s", e)
        return None


__all__ = [
    "execute_cdp_javascript",
    "read_webpage_content_cdp",
]
