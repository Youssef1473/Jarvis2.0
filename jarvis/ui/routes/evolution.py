"""Self-evolution candidate AST patch proposal review and promotion routes for Jarvis 2.0 HUD."""
from __future__ import annotations

import contextlib
import logging
import secrets
from typing import Any

from starlette.responses import JSONResponse

from jarvis.skills.ast_code_surgeon import get_proposal_registry
from jarvis.ui.routes import handle_route_errors
from jarvis.ui.security import (
    resolve_active_session_token,
    validate_request_host_and_origin,
)

logger = logging.getLogger("Jarvis.UI.Evolution")


class EvolutionRoutesMixin:
    """Mixin providing authenticated endpoints for reviewing, approving, and rejecting code patch proposals."""

    @handle_route_errors
    async def _handle_evolution_proposals_list(self, request: Any) -> JSONResponse:
        """Retrieves verified AST patch proposals with active pending counts."""
        if getattr(getattr(self, "service", self), "enforce_auth", True):
            token = (
                request.headers.get("x-jarvis-token")
                or request.cookies.get("jarvis_session")
            )
            active_token = resolve_active_session_token(request)
            if not (token and secrets.compare_digest(token, active_token)):
                return JSONResponse(
                    {"error": "Unauthorized: Invalid or missing session credentials"},
                    status_code=401,
                )

        include_resolved = str(request.query_params.get("include_resolved", "false")).lower() in (
            "true",
            "1",
            "yes",
        )
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (ValueError, TypeError):
            limit = 50
        limit = max(1, min(100, limit))

        registry = get_proposal_registry()
        proposals = registry.list_proposals(include_resolved=include_resolved, limit=limit)
        pending_count = registry.get_pending_count()

        return JSONResponse({
            "total": len(proposals),
            "pending_count": pending_count,
            "proposals": [p.to_dict() for p in proposals],
        })

    @handle_route_errors
    async def _handle_evolution_proposal_approve(self, request: Any) -> JSONResponse:
        """Promotes a verified AST patch proposal guarded by auth, Origin check, and optimistic concurrency."""
        if getattr(getattr(self, "service", self), "enforce_auth", True):
            token = (
                request.headers.get("x-jarvis-token")
                or request.cookies.get("jarvis_session")
            )
            active_token = resolve_active_session_token(request)
            if not (token and secrets.compare_digest(token, active_token)):
                return JSONResponse(
                    {"error": "Unauthorized: Invalid or missing session credentials"},
                    status_code=401,
                )

        is_valid, err_msg, status_code = validate_request_host_and_origin(
            headers=request.headers,
            service=getattr(self, "service", self),
            request_scheme=request.url.scheme,
        )
        if not is_valid:
            return JSONResponse({"error": err_msg}, status_code=status_code)

        proposal_id = str(request.path_params.get("proposal_id", "")).strip()
        if not proposal_id:
            return JSONResponse({"error": "proposal_id is required"}, status_code=400)

        registry = get_proposal_registry()
        res = registry.approve_proposal(proposal_id, user_approved=True)
        res_status = res.get("status")

        if res_status == "not_found":
            return JSONResponse(res, status_code=404)
        if res_status == "conflict":
            return JSONResponse(res, status_code=409)
        if res_status in ("invalid_status", "expired"):
            return JSONResponse(res, status_code=400)
        if res_status == "rejected_unauthorized":
            return JSONResponse(res, status_code=403)
        if res_status == "approved":
            proposal = registry.get_proposal(proposal_id)
            if hasattr(self, "broadcast"):
                self.broadcast({
                    "type": "patch_proposal_resolved",
                    "data": {
                        "proposal_id": proposal_id,
                        "status": "approved",
                        "proposal": proposal.to_dict() if proposal else None,
                    },
                })
            if hasattr(self, "broadcast_message"):
                self.broadcast_message(
                    "system",
                    f"✅ Promoted AST patch proposal '{proposal_id}' to live repository.",
                )
            return JSONResponse(res, status_code=200)

        return JSONResponse(res, status_code=500)

    @handle_route_errors
    async def _handle_evolution_proposal_reject(self, request: Any) -> JSONResponse:
        """Rejects a pending AST patch proposal with an optional operator rationale."""
        if getattr(getattr(self, "service", self), "enforce_auth", True):
            token = (
                request.headers.get("x-jarvis-token")
                or request.cookies.get("jarvis_session")
            )
            active_token = resolve_active_session_token(request)
            if not (token and secrets.compare_digest(token, active_token)):
                return JSONResponse(
                    {"error": "Unauthorized: Invalid or missing session credentials"},
                    status_code=401,
                )

        is_valid, err_msg, status_code = validate_request_host_and_origin(
            headers=request.headers,
            service=getattr(self, "service", self),
            request_scheme=request.url.scheme,
        )
        if not is_valid:
            return JSONResponse({"error": err_msg}, status_code=status_code)

        proposal_id = str(request.path_params.get("proposal_id", "")).strip()
        if not proposal_id:
            return JSONResponse({"error": "proposal_id is required"}, status_code=400)

        reason = ""
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                reason = str(body.get("reason", "")).strip()

        registry = get_proposal_registry()
        res = registry.reject_proposal(proposal_id, reason=reason)
        res_status = res.get("status")

        if res_status == "not_found":
            return JSONResponse(res, status_code=404)
        if res_status in ("invalid_status", "expired"):
            return JSONResponse(res, status_code=400)
        if res_status == "rejected":
            proposal = registry.get_proposal(proposal_id)
            if hasattr(self, "broadcast"):
                self.broadcast({
                    "type": "patch_proposal_resolved",
                    "data": {
                        "proposal_id": proposal_id,
                        "status": "rejected",
                        "proposal": proposal.to_dict() if proposal else None,
                    },
                })
            if hasattr(self, "broadcast_message"):
                self.broadcast_message(
                    "system",
                    f"❌ Rejected AST patch proposal '{proposal_id}'.",
                )
            return JSONResponse(res, status_code=200)

        return JSONResponse(res, status_code=500)
