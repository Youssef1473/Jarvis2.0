"""DualKey human-in-the-loop authorization routes and state management for Jarvis 2.0 HUD."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from starlette.responses import JSONResponse

logger = logging.getLogger("Jarvis.UI.Approval")


class ApprovalRoutesMixin:
    """Mixin providing out-of-band authorization endpoints and decision dispatch."""

    _pending_approvals: dict[str, dict[str, Any]]
    _approval_lock: threading.Lock

    def handle_approval_request(self, request_details: dict[str, Any], timeout: float = 30.0) -> bool:
        """Presents an out-of-band authorization request to the user via WebSocket and HUD mission feed."""
        action_hash = request_details.get("action_hash", "")
        tool_name = request_details.get("tool_name", "")
        summary = request_details.get("summary", "")
        arguments = request_details.get("arguments", {})
        risk_level = request_details.get("risk_level", 2)
        expires_at = time.time() + timeout

        event = threading.Event()
        with self._approval_lock:
            self._pending_approvals[action_hash] = {
                "action_hash": action_hash,
                "tool_name": tool_name,
                "summary": summary,
                "arguments": arguments,
                "risk_level": risk_level,
                "expires_at": expires_at,
                "event": event,
                "decision": False,
            }

        # Broadcast approval request to connected HUD clients
        if hasattr(self, "broadcast"):
            self.broadcast({
                "type": "approval_request",
                "data": {
                    "action_hash": action_hash,
                    "tool_name": tool_name,
                    "summary": summary,
                    "arguments": arguments,
                    "risk_level": risk_level,
                    "expires_at": expires_at,
                },
            })
        if hasattr(self, "broadcast_message"):
            self.broadcast_message(
                "system",
                f"⚠️ Authorization Required: {summary}"
            )

        logger.info(
            "Awaiting user approval for action '%s' (hash: %s, timeout: %.1fs)...",
            tool_name,
            action_hash[:12] if action_hash else "unknown",
            timeout,
        )
        event.wait(timeout=timeout)

        with self._approval_lock:
            rec = self._pending_approvals.pop(action_hash, None)
            if rec and rec["event"].is_set():
                decision = bool(rec["decision"])
                logger.info("User approval outcome for '%s': %s", tool_name, "APPROVED" if decision else "DENIED")
                return decision
            logger.warning("Approval request for '%s' timed out after %.1fs (fail closed).", tool_name, timeout)
            return False

    def submit_approval_decision(self, action_hash: str, approved: bool) -> bool:
        """Submits human decision (approve or deny) for an in-flight approval request."""
        with self._approval_lock:
            rec = self._pending_approvals.get(action_hash)
            if rec and not rec["event"].is_set():
                rec["decision"] = bool(approved)
                rec["event"].set()
                return True
            return False

    async def _handle_approval_pending(self, request: Any) -> JSONResponse:
        """Lists active pending approval requests for HUD prompt presentation."""
        now = time.time()
        with self._approval_lock:
            pending = [
                {
                    "action_hash": h,
                    "tool_name": r["tool_name"],
                    "summary": r["summary"],
                    "arguments": r["arguments"],
                    "risk_level": r["risk_level"],
                    "expires_in": max(0.0, r["expires_at"] - now),
                }
                for h, r in self._pending_approvals.items()
                if r["expires_at"] > now and not r["event"].is_set()
            ]
        return JSONResponse({"pending": pending})

    async def _handle_approval_respond(self, request: Any) -> JSONResponse:
        """Submits human authorization decision for a pending action hash."""
        try:
            body = await request.json()
            action_hash = str(body.get("action_hash", "")).strip()
            decision_raw = str(body.get("decision", "deny")).strip().lower()
            approved = decision_raw in ("approve", "approved", "true", "yes", "1")
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not action_hash:
            return JSONResponse({"error": "action_hash is required"}, status_code=400)

        success = self.submit_approval_decision(action_hash, approved)
        if success:
            decision_text = "APPROVED" if approved else "DENIED"
            if hasattr(self, "broadcast_message"):
                self.broadcast_message(
                    "system",
                    f"Action {decision_text} by user for hash {action_hash[:8]}..."
                )
            return JSONResponse({"status": "recorded", "action_hash": action_hash, "approved": approved})
        return JSONResponse({"error": "Pending approval request not found or expired"}, status_code=404)

    async def _handle_approval_grant(self, request: Any) -> JSONResponse:
        """Allows authenticated human operators to issue an explicit out-of-band grant in advance."""
        try:
            body = await request.json()
            tool_name = str(body.get("tool_name", "")).strip()
            args = body.get("arguments", {})
            ttl = float(body.get("ttl_seconds", 60.0))
            summary = str(body.get("summary", "")).strip()
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not tool_name:
            return JSONResponse({"error": "tool_name is required"}, status_code=400)

        from jarvis.core.security_boundaries import DualKeyApprovalManager
        mgr = DualKeyApprovalManager.get_instance()
        try:
            auth = mgr.grant_approval(tool_name, args, ttl_seconds=ttl, summary=summary)
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
        return JSONResponse({
            "status": "granted",
            "tool_name": auth.tool_name,
            "action_hash": auth.action_hash,
            "expires_at": auth.expires_at,
            "summary": auth.summary,
        })
