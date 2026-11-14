"""Modular HTTP and WebSocket route mixins for Jarvis 2.0 HUD server."""
from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse

logger = logging.getLogger("Jarvis.UI.Routes")


def resolve_request_workspace(request_or_params: Any = None, orchestrator: Any = None) -> str:
    """Resolves, scopes, and validates workspace_id from query params, body dict, or orchestrator state.

    Fails closed with PermissionError if workspace is 'unscoped', and ValueError if the identifier
    violates strict format constraints.
    """
    raw_ws = None
    if hasattr(request_or_params, "query_params"):
        raw_ws = request_or_params.query_params.get("workspace_id")
    elif isinstance(request_or_params, dict):
        raw_ws = request_or_params.get("workspace_id")

    if not isinstance(raw_ws, str) or not raw_ws.strip():
        active_ws = getattr(orchestrator, "active_workspace_id", None)
        if isinstance(active_ws, str) and active_ws.strip():
            raw_ws = active_ws
        else:
            raw_ws = "ws_default"

    clean_ws = str(raw_ws).strip()
    if clean_ws.lower() == "unscoped":
        raise PermissionError("Access to unscoped partition is forbidden.")

    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", clean_ws):
        raise ValueError(f"Invalid workspace_id format: '{clean_ws}'")

    return clean_ws


def handle_route_errors(func: Callable) -> Callable:
    """Decorator mapping common domain exceptions to standardized Starlette JSON responses."""
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except PermissionError as pe:
            return JSONResponse({"error": str(pe)}, status_code=403)
        except (ValueError, json.JSONDecodeError) as ve:
            return JSONResponse({"error": str(ve)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - Top-level route error boundary protects HTTP worker
            logger.error("Unhandled route exception in %s: %s", getattr(func, "__name__", "endpoint"), exc)
            return JSONResponse({"error": str(exc)}, status_code=500)
    return wrapper
