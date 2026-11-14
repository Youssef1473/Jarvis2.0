"""Holographic Web HUD Server, Starlette lifecycle facade, and WebSocket telemetry router for Jarvis 2.0."""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import psutil
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from jarvis.ui.metrics import WorkstationMetricsCollector
from jarvis.ui.routes import resolve_request_workspace
from jarvis.ui.routes.approval import ApprovalRoutesMixin
from jarvis.ui.routes.calendar import CalendarRoutesMixin
from jarvis.ui.routes.command import CommandRoutesMixin
from jarvis.ui.routes.media import MediaRoutesMixin, resolve_safe_photo_path
from jarvis.ui.routes.memory import MemoryRoutesMixin
from jarvis.ui.security import (
    MAX_QUERY_STRING_BYTES,
    OVERSIZED_QUERY_PLACEHOLDER_BYTES,
    OVERSIZED_QUERY_PLACEHOLDER_TEXT,
    AccessLogRedactionMiddleware,
    SecurityHeadersMiddleware,
    SensitiveQueryRedactionFilter,
    attach_access_log_redaction_filters,
    consume_bootstrap_token,
    create_bootstrap_token,
    derive_absolute_reporting_endpoint,
    generate_csp_nonce,
    get_hud_session_token,
    is_browser_facing_https,
    load_unlock_html,
    redact_query_string_bytes,
    redact_query_string_text,
    render_template_with_nonce,
    resolve_active_session_token,
    set_hud_session_token,
    validate_request_host_and_origin,
)
from jarvis.ui.telemetry import TelemetryConnectionManager

# Backward-compatible module-level re-exports for external callers and tests
__all__ = [
    "MAX_QUERY_STRING_BYTES",
    "OVERSIZED_QUERY_PLACEHOLDER_BYTES",
    "OVERSIZED_QUERY_PLACEHOLDER_TEXT",
    "AccessLogRedactionMiddleware",
    "JarvisUIService",
    "SecurityHeadersMiddleware",
    "SensitiveQueryRedactionFilter",
    "TelemetryConnectionManager",
    "WorkstationMetricsCollector",
    "attach_access_log_redaction_filters",
    "broadcast_ambient_telemetry",
    "consume_bootstrap_token",
    "create_bootstrap_token",
    "derive_absolute_reporting_endpoint",
    "generate_csp_nonce",
    "get_hud_session_token",
    "is_browser_facing_https",
    "load_unlock_html",
    "redact_query_string_bytes",
    "redact_query_string_text",
    "render_template_with_nonce",
    "resolve_active_session_token",
    "resolve_safe_photo_path",
    "set_hud_session_token",
    "validate_request_host_and_origin",
]

logger = logging.getLogger("Jarvis.UI.Server")


class JarvisUIService(
    ApprovalRoutesMixin,
    CalendarRoutesMixin,
    MemoryRoutesMixin,
    MediaRoutesMixin,
    CommandRoutesMixin,
):
    """Manages the lifecycle of the Starlette HUD server, routing assembly, and WebSocket broadcasting."""

    _instance: JarvisUIService | None = None

    def __init__(
        self,
        orchestrator: Any = None,
        host: str = "127.0.0.1",
        port: int = 20129,
        enforce_auth: bool = True,
        access_log: bool = False,
    ):
        self.orchestrator = orchestrator
        self.host = host
        self.port = port
        self.enforce_auth = enforce_auth
        self.access_log = access_log
        self.telemetry_manager = TelemetryConnectionManager(max_clients=8)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready_event = threading.Event()
        self._stopped_event = threading.Event()
        self.template_path = Path(__file__).parent / "templates" / "hud.html"
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._approval_lock = threading.Lock()
        self._csp_violations_ring: collections.deque = collections.deque(maxlen=50)
        self._csp_report_timestamps: collections.deque = collections.deque(maxlen=100)
        self._csp_report_lock = threading.Lock()
        self._csp_dedup_cache: dict[str, float] = {}
        self._command_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="Jarvis-WSCommand"
        )
        self._command_semaphore = threading.Semaphore(4)

        try:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(self.handle_approval_request)
        except (ImportError, AttributeError, RuntimeError) as e:
            logger.debug("DualKeyApprovalManager binding notice: %s", e)

        JarvisUIService._instance = self

        try:
            psutil.cpu_percent(interval=None)
        except (AttributeError, OSError):
            pass

    @property
    def connected_clients(self) -> set[WebSocket]:
        return self.telemetry_manager.connected_clients

    def get_authenticated_url(self) -> str:
        """Returns the HUD URL formatted with a single-use bootstrap token fragment."""
        token = create_bootstrap_token(ttl_seconds=60.0)
        return f"http://{self.host}:{self.port}/#bootstrap={token}"

    @classmethod
    def get_instance(cls) -> JarvisUIService | None:
        return cls._instance

    def _resolve_request_workspace(self, request_or_params: Any = None) -> str:
        """Resolves and validates workspace_id from request, payload, or orchestrator state."""
        return resolve_request_workspace(request_or_params, self.orchestrator)

    def _get_unlock_html(self, nonce: str = "") -> str:
        """Renders minimal unauthenticated interface lock page with zero token disclosure."""
        return load_unlock_html(nonce=nonce)

    def _create_app(self) -> Starlette:
        routes = [
            Route("/", endpoint=self._handle_index, methods=["GET"]),
            Route("/api/auth/unlock", endpoint=self._handle_unlock, methods=["POST"]),
            Route("/api/csp-report", endpoint=self._handle_csp_report, methods=["POST"]),
            Route("/api/status", endpoint=self._handle_status, methods=["GET"]),
            Route("/api/command", endpoint=self._handle_command, methods=["POST"]),
            Route("/api/action", endpoint=self._handle_action, methods=["POST"]),
            Route("/api/mute", endpoint=self._handle_mute, methods=["POST"]),
            Route("/api/notes", endpoint=self._handle_notes, methods=["GET"]),
            Route("/api/timers", endpoint=self._handle_timers, methods=["GET"]),
            Route("/api/documents/upload", endpoint=self._handle_documents_upload, methods=["POST"]),
            Route("/api/documents/staged", endpoint=self._handle_staged_documents, methods=["GET"]),
            Route("/api/calendar", endpoint=self._handle_calendar, methods=["GET"]),
            Route("/api/calendar/delete", endpoint=self._handle_calendar_delete, methods=["POST"]),
            Route("/api/calendar/event/delete", endpoint=self._handle_calendar_event_delete, methods=["POST"]),
            Route("/api/calendar/clear", endpoint=self._handle_calendar_clear, methods=["POST"]),
            Route("/api/photos", endpoint=self._handle_photos_list, methods=["GET"]),
            Route("/api/photos/{photo_id}", endpoint=self._handle_photo_stream, methods=["GET"]),
            Route("/api/screenshot/latest", endpoint=self._handle_latest_screenshot, methods=["GET"]),
            Route("/api/tasks", endpoint=self._handle_tasks_list, methods=["GET"]),
            Route("/api/tasks/terminate", endpoint=self._handle_task_terminate, methods=["POST"]),
            Route("/api/memory/nodes", endpoint=self._handle_memory_nodes, methods=["GET"]),
            Route("/api/memory/delete", endpoint=self._handle_memory_delete, methods=["POST"]),
            Route("/api/memory/update", endpoint=self._handle_memory_update, methods=["POST"]),
            Route("/api/approval/pending", endpoint=self._handle_approval_pending, methods=["GET"]),
            Route("/api/approval/respond", endpoint=self._handle_approval_respond, methods=["POST"]),
            Route("/api/approval/grant", endpoint=self._handle_approval_grant, methods=["POST"]),
            WebSocketRoute("/ws", endpoint=self._handle_websocket),
            WebSocketRoute("/api/telemetry", endpoint=self._handle_websocket),
        ]
        middleware = [
            Middleware(AccessLogRedactionMiddleware),
            Middleware(SecurityHeadersMiddleware, service=self),
        ]
        app = Starlette(
            routes=routes,
            middleware=middleware,
        )
        app.state.enforce_auth = self.enforce_auth
        return app

    async def _handle_unlock(self, request: Any) -> JSONResponse:
        """Exchanges a single-use bootstrap token or session token for a session cookie."""
        try:
            body = await request.json()
            token = str(body.get("token", "")).strip()
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid request body"}, status_code=400)

        if not token:
            return JSONResponse({"error": "Token is required"}, status_code=400)

        active_token = resolve_active_session_token(request)
        is_valid = secrets.compare_digest(token, active_token) or consume_bootstrap_token(token)
        if not is_valid:
            return JSONResponse({"error": "Invalid token"}, status_code=401)

        resp = JSONResponse({"status": "authenticated"})
        is_secure = is_browser_facing_https(request, self)
        resp.set_cookie(
            key="jarvis_session",
            value=active_token,
            httponly=True,
            samesite="strict",
            secure=is_secure,
            path="/",
        )
        return resp

    async def _handle_index(self, request: Any) -> Response:
        """Renders the main HUD dashboard if authenticated, or the minimal unlock screen."""
        if not self.template_path.exists():
            resp = HTMLResponse("<h3>HUD template not found.</h3>", status_code=404)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

        cookie_token = request.cookies.get("jarvis_session")
        header_token = request.headers.get("x-jarvis-token")
        active_token = resolve_active_session_token(request)

        # Zero-trust: only valid session cookies or explicit header credentials authenticate
        is_auth = (
            bool(cookie_token and secrets.compare_digest(cookie_token, active_token))
            or bool(header_token and secrets.compare_digest(header_token, active_token))
        )

        nonce = getattr(getattr(request, "state", None), "csp_nonce", None)
        if not nonce:
            nonce = generate_csp_nonce()
            if hasattr(request, "state"):
                request.state.csp_nonce = nonce

        if not is_auth and self.enforce_auth:
            resp = HTMLResponse(self._get_unlock_html(nonce=nonce), status_code=200)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

        html_content = self.template_path.read_text(encoding="utf-8")
        rendered_html = render_template_with_nonce(html_content, nonce)
        response = HTMLResponse(rendered_html)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        is_secure = is_browser_facing_https(request, self)
        response.set_cookie(
            key="jarvis_session",
            value=active_token,
            httponly=True,
            samesite="strict",
            secure=is_secure,
            path="/",
        )
        return response

    async def _handle_status(self, request: Any) -> JSONResponse:
        """Returns live system telemetry and hardware vitals."""
        data = self._gather_telemetry()
        return JSONResponse(data)

    async def _handle_csp_report(self, request: Any) -> Response:
        """Ingests, bounds, deduplicates, and logs Content Security Policy violation reports."""
        if request.method != "POST":
            return JSONResponse({"error": "Method Not Allowed"}, status_code=405)

        # 1. Content-Type validation
        content_type = request.headers.get("content-type", "").lower().split(";")[0].strip()
        allowed_types = {"application/csp-report", "application/reports+json", "application/json"}
        if content_type not in allowed_types:
            return JSONResponse(
                {"error": "Unsupported Media Type: expected application/csp-report or application/reports+json"},
                status_code=415,
            )

        # 2. Strict body size bounding while consuming stream (anti-DoS, max 32KB)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                cl_val = int(content_length)
                if cl_val < 0:
                    return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
                if cl_val > 32768:
                    return JSONResponse({"error": "Payload Too Large: max 32KB"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)

        chunks = []
        total_bytes = 0
        max_bytes = 32768
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                return JSONResponse({"error": "Payload Too Large: max 32KB"}, status_code=413)
            chunks.append(chunk)

        body_bytes = b"".join(chunks)
        if not body_bytes:
            return JSONResponse({"error": "Empty report body"}, status_code=400)

        # 3. JSON parsing & primitive rejection
        try:
            raw_payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        if not isinstance(raw_payload, (dict, list)):
            return JSONResponse({"error": "Invalid JSON: expected object or array"}, status_code=400)

        # 4. Extract reports
        reports_to_process = []
        if isinstance(raw_payload, dict):
            if "csp-report" in raw_payload:
                if isinstance(raw_payload["csp-report"], dict):
                    reports_to_process.append(raw_payload["csp-report"])
                else:
                    return JSONResponse({"error": "Invalid CSP report structure"}, status_code=400)
            else:
                reports_to_process.append(raw_payload)
        elif isinstance(raw_payload, list):
            for item in raw_payload[:10]:
                if isinstance(item, dict):
                    if item.get("type") == "csp-violation" and isinstance(item.get("body"), dict):
                        reports_to_process.append(item["body"])
                    else:
                        reports_to_process.append(item)

        if not reports_to_process:
            return JSONResponse({"error": "No valid CSP reports found in payload"}, status_code=400)

        # 5. Rate limiting (max 60 reports per minute)
        num_reports = len(reports_to_process)
        now = time.monotonic()
        with self._csp_report_lock:
            while self._csp_report_timestamps and (now - self._csp_report_timestamps[0] > 60.0):
                self._csp_report_timestamps.popleft()
            if len(self._csp_report_timestamps) + num_reports > 60:
                return JSONResponse({"error": "Too Many Requests: CSP report rate limit exceeded"}, status_code=429)
            for _ in range(num_reports):
                self._csp_report_timestamps.append(now)

            # Purge expired deduplication keys (>30s)
            stale_keys = [k for k, exp in self._csp_dedup_cache.items() if now > exp]
            for k in stale_keys:
                del self._csp_dedup_cache[k]

        # 6. Sanitize, deduplicate, and record
        for report in reports_to_process:
            if not isinstance(report, dict):
                continue

            def _clean(val: Any, max_len: int = 256) -> str:
                if val is None:
                    return ""
                s = str(val).replace("\r", " ").replace("\n", " ").strip()
                s = "".join(c for c in s if 32 <= ord(c) <= 126)
                return s[:max_len]

            blocked_uri = _clean(report.get("blocked-uri") or report.get("blockedURL") or report.get("blocked_url"))
            violated_directive = _clean(report.get("violated-directive") or report.get("violatedDirective") or report.get("directive"), 128)
            effective_directive = _clean(report.get("effective-directive") or report.get("effectiveDirective"), 128)
            document_uri = _clean(report.get("document-uri") or report.get("documentURL") or report.get("url"))
            disposition = _clean(report.get("disposition"), 32) or "report"

            dedup_key = f"{violated_directive}:{effective_directive}:{blocked_uri}:{document_uri}"
            with self._csp_report_lock:
                if dedup_key in self._csp_dedup_cache:
                    continue
                self._csp_dedup_cache[dedup_key] = now + 10.0

                entry = {
                    "timestamp": time.time(),
                    "disposition": disposition,
                    "violated_directive": violated_directive,
                    "effective_directive": effective_directive,
                    "blocked_uri": blocked_uri,
                    "document_uri": document_uri,
                }
                self._csp_violations_ring.append(entry)

            logger.warning(
                "CSP Violation [%s]: directive '%s' (effective: '%s') blocked '%s' on '%s'",
                disposition,
                violated_directive or effective_directive,
                effective_directive,
                blocked_uri,
                document_uri,
            )

        return Response(status_code=204)

    async def _handle_websocket(self, websocket: WebSocket) -> None:
        """HUD WebSocket endpoint with host/origin validation, auth gating, and command dispatch."""
        # 1. Validate Host and Origin headers with exact scheme, host, and port matching
        req_scheme = "https" if websocket.url.scheme in ("https", "wss") else "http"
        is_valid, err_msg, _ = validate_request_host_and_origin(
            headers=websocket.headers,
            service=self,
            request_scheme=req_scheme,
        )
        if not is_valid:
            await websocket.close(code=1008, reason=err_msg)
            return

        # 2. Check token authentication
        token = (
            websocket.headers.get("x-jarvis-token")
            or websocket.cookies.get("jarvis_session")
        )
        active_token = resolve_active_session_token(websocket)
        is_authenticated = bool(token and secrets.compare_digest(token, active_token))
        enforce_auth = getattr(self, "enforce_auth", True)

        if not is_authenticated and enforce_auth:
            await websocket.close(code=1008, reason="Unauthorized: Missing or invalid token")
            return

        accepted = await self.telemetry_manager.connect(websocket)
        if not accepted:
            return

        # Send initial status asynchronously off-loop
        try:
            init_telemetry = await anyio.to_thread.run_sync(
                WorkstationMetricsCollector.sample_metrics_sync, self.orchestrator
            )
            await websocket.send_text(json.dumps({"type": "telemetry", **init_telemetry}, separators=(",", ":")))
        except (RuntimeError, OSError) as telem_err:
            logger.debug("Initial websocket telemetry send notice: %s", telem_err)

        try:
            while True:
                data_text = await websocket.receive_text()
                try:
                    payload = json.loads(data_text)
                    msg_type = payload.get("type", "")

                    if msg_type == "command":
                        cmd = payload.get("text", "").strip()
                        if cmd and self.orchestrator:
                            if not self._command_semaphore.acquire(blocking=False):
                                await websocket.send_text(
                                    json.dumps({
                                        "type": "busy",
                                        "message": "Jarvis is currently processing maximum concurrent commands. Please wait a moment, Sir.",
                                    })
                                )
                            else:
                                def _run_ws_command(c_text: str):
                                    try:
                                        self.orchestrator.handle_command(c_text)
                                    finally:
                                        self._command_semaphore.release()
                                self._command_executor.submit(_run_ws_command, cmd)

                    elif msg_type == "action":
                        action = payload.get("action", "")
                        if action == "toggle_mute" and self.orchestrator:
                            is_muted = self.orchestrator.toggle_mute()
                            self.broadcast_telemetry({"is_muted": is_muted, "m": 1 if is_muted else 0})
                        elif action == "set_volume":
                            from jarvis.skills.os_control import adjust_volume
                            try:
                                percent = int(payload.get("percent", 50))
                            except (ValueError, TypeError):
                                percent = 50
                            adjust_volume(action="set", percent=percent)
                            self.broadcast_message("jarvis", f"Master volume set to {percent}%, Sir.")

                    elif msg_type == "approval_response":
                        action_hash = str(payload.get("action_hash", "")).strip()
                        decision_raw = str(payload.get("decision", "deny")).strip().lower()
                        approved = decision_raw in ("approve", "approved", "true", "yes", "1")
                        self.submit_approval_decision(action_hash, approved)

                except (ValueError, TypeError) as parse_err:
                    logger.debug("Invalid HUD WebSocket message format: %s", parse_err)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Error handling HUD WebSocket message: %s", e)

        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await self.telemetry_manager.disconnect(websocket)

    def _gather_telemetry(self) -> dict[str, Any]:
        """Collects current hardware and model telemetry without blocking."""
        return WorkstationMetricsCollector.sample_metrics_sync(self.orchestrator)

    def broadcast(self, payload: dict[str, Any]):
        """Thread-safe broadcast of a JSON payload to all connected clients."""
        if not self.telemetry_manager.client_count or not self._loop:
            return

        message = json.dumps(payload, separators=(",", ":"))
        coro = self.telemetry_manager.broadcast_direct(message)
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as e:
            coro.close()
            logger.debug("Failed scheduling broadcast_direct on loop: %s", e)
        except Exception as e:  # noqa: BLE001 - Guard UI thread against scheduling anomalies
            coro.close()
            logger.debug("Unexpected error scheduling broadcast_direct: %s", e)

    def broadcast_state(self, state: str):
        """Broadcasts voice state change ('idle', 'listening', 'thinking', 'speaking')."""
        self.broadcast({"type": "state", "state": state, "st": state})

    def broadcast_message(self, sender: str, text: str):
        """Broadcasts a dialogue message to the live mission feed."""
        self.broadcast({
            "type": "chat",
            "sender": sender,
            "text": text,
            "timestamp": time.strftime("%H:%M:%S"),
        })

    def broadcast_telemetry(self, extra: dict[str, Any] | None = None):
        """Broadcasts telemetry update."""
        base = self._gather_telemetry()
        if extra:
            base.update(extra)
        if self._loop and self.telemetry_manager.client_count:
            try:
                self._loop.call_soon_threadsafe(self.telemetry_manager.broadcast_telemetry, base)
            except RuntimeError as e:
                logger.debug("Failed scheduling broadcast_telemetry on closed loop: %s", e)
            except Exception as e:  # noqa: BLE001 - Guard UI thread against scheduling anomalies
                logger.debug("Unexpected error scheduling broadcast_telemetry: %s", e)

    def broadcast_audio_level(self, level: float):
        """Broadcasts audio reactivity level (0.0 to 1.0)."""
        self.broadcast({"type": "audio_level", "level": max(0.0, min(1.0, level))})

    def broadcast_dsp_frame(self, dsp_frame: bytes):
        """Thread-safe broadcast of 68-byte DSP binary frame to connected HUD clients."""
        if not self.telemetry_manager.client_count or not self._loop:
            return
        coro = self.telemetry_manager.broadcast_binary(dsp_frame)
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as e:
            coro.close()
            logger.debug("Failed scheduling broadcast_binary on loop: %s", e)
        except Exception as e:  # noqa: BLE001 - Guard UI thread against scheduling anomalies
            coro.close()
            logger.debug("Unexpected error scheduling broadcast_binary: %s", e)

    def start(self, timeout: float = 10.0) -> bool:
        """Starts the Uvicorn server in a background daemon thread with continuous live telemetry streaming."""
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            allow_remote = os.environ.get("JARVIS_ALLOW_REMOTE") == "1"
            has_tunnel = os.environ.get("JARVIS_SECURE_TUNNEL") == "1"
            if not (allow_remote and has_tunnel):
                raise RuntimeError(
                    f"Refusing to bind Jarvis UI to non-loopback interface '{self.host}' "
                    "without an encrypted transport or secured tunnel (set JARVIS_ALLOW_REMOTE=1 and JARVIS_SECURE_TUNNEL=1)."
                )

        if self._thread and self._thread.is_alive():
            if self._ready_event.is_set():
                return True
            return self._ready_event.wait(timeout=timeout)

        async def _telemetry_broadcast_loop():
            # Prime CPU counter baseline
            try:
                psutil.cpu_percent(interval=None)
            except (AttributeError, OSError) as err:
                logger.debug("CPU baseline query notice: %s", err)

            while self._server and not self._server.should_exit:
                try:
                    await asyncio.sleep(1.0)
                    if self.telemetry_manager.client_count:
                        # Offload synchronous system queries to worker thread: ZERO event-loop blocking
                        metrics = await anyio.to_thread.run_sync(
                            WorkstationMetricsCollector.sample_metrics_sync, self.orchestrator
                        )
                        self.telemetry_manager.broadcast_telemetry(metrics)
                except asyncio.CancelledError:
                    break
                except Exception as e:  # noqa: BLE001
                    logger.debug("Telemetry broadcast loop notice: %s", e)

        async def _serve_all():
            telemetry_task = self._loop.create_task(_telemetry_broadcast_loop())

            async def _notify_ready():
                for _ in range(100):
                    if getattr(self._server, "started", False):
                        break
                    await asyncio.sleep(0.02)
                self._ready_event.set()

            ready_task = self._loop.create_task(_notify_ready())
            try:
                await self._server.serve()
            finally:
                self._ready_event.set()
                ready_task.cancel()
                telemetry_task.cancel()
                try:
                    await telemetry_task
                except asyncio.CancelledError:
                    pass

        def _run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            attach_access_log_redaction_filters()
            config = uvicorn.Config(
                app=self._create_app(),
                host=self.host,
                port=self.port,
                access_log=bool(getattr(self, "access_log", False) or os.environ.get("JARVIS_UI_ACCESS_LOG") == "1"),
                log_level="info" if (getattr(self, "access_log", False) or os.environ.get("JARVIS_UI_ACCESS_LOG") == "1") else "warning",
                loop="asyncio",
            )
            self._server = uvicorn.Server(config)
            logger.info("Starting Jarvis HUD Web Server at http://%s:%d...", self.host, self.port)
            try:
                self._loop.run_until_complete(_serve_all())
            finally:
                try:
                    pending = asyncio.all_tasks(self._loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except (asyncio.CancelledError, RuntimeError) as exc:
                    logger.debug("Loop task cleanup notice: %s", exc)
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except (asyncio.CancelledError, RuntimeError) as exc:
                    logger.debug("Asyncgen shutdown notice: %s", exc)
                if not self._loop.is_closed():
                    self._loop.close()

        self._ready_event.clear()
        self._stopped_event.clear()
        self._thread = threading.Thread(target=_run_server, daemon=True, name="Jarvis-HUD-Server")
        self._thread.start()
        is_ready = self._ready_event.wait(timeout=timeout)
        if not is_ready:
            logger.warning("Jarvis HUD Web Server did not signal readiness within %.1fs", timeout)
        return is_ready

    def stop(self, timeout: float = 5.0) -> bool:
        """Stops the Uvicorn server cleanly and shuts down native NVML session."""
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Jarvis HUD Web Server thread did not terminate within %.1fs", timeout)
                return False
            self._thread = None
        if hasattr(self, "_command_executor") and self._command_executor:
            try:
                self._command_executor.shutdown(wait=False)
            except (RuntimeError, OSError) as exc:
                logger.debug("Command executor shutdown notice: %s", exc)
        self._server = None
        self._stopped_event.set()
        try:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(None)
        except (ImportError, AttributeError, RuntimeError) as exc:
            logger.debug("Approval handler teardown notice: %s", exc)
        WorkstationMetricsCollector.shutdown_nvml()
        return True


async def broadcast_ambient_telemetry(type: str, data: dict[str, Any]):
    """Module-level helper to dispatch ambient telemetry to the active HUD server."""
    instance = JarvisUIService.get_instance()
    if instance:
        instance.broadcast({"type": type, "data": data, "timestamp": time.strftime("%H:%M:%S")})
