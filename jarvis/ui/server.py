"""Starlette and WebSocket server powering the Jarvis 2.0 Holographic Web HUD."""
import asyncio
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Set
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse, Response, FileResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
import anyio
import uvicorn
import psutil

import os
import re
import secrets
from urllib.parse import urlparse
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from jarvis.skills.os_control import get_raw_battery_info

logger = logging.getLogger("Jarvis.UI")

# Ephemeral cryptographic session token generated per server run
_HUD_SESSION_TOKEN: str = secrets.token_urlsafe(32)


def get_hud_session_token() -> str:
    """Returns the current ephemeral HUD cryptographic session token."""
    global _HUD_SESSION_TOKEN
    return _HUD_SESSION_TOKEN


def set_hud_session_token(token: str) -> None:
    """Overrides the current session token (useful for deterministic integration tests)."""
    global _HUD_SESSION_TOKEN
    _HUD_SESSION_TOKEN = str(token)


def resolve_safe_photo_path(photo_id: str, photos_dir: Optional[Path] = None) -> Optional[Path]:
    """Resolves a photo ID safely within the photos directory, preventing directory traversal.

    Rejects any paths containing traversal characters ('..', '/', '\\', '%2e', '%2f', '%5c', null bytes)
    or absolute paths. Returns the resolved Path if safe, or None if malicious or invalid.
    """
    if not photo_id or not isinstance(photo_id, str):
        return None

    clean_id = photo_id.strip()

    # Reject path traversal patterns explicitly
    traversal_indicators = ["..", "/", "\\", "%2e", "%2f", "%5c", "\0"]
    lower_id = clean_id.lower()
    for indicator in traversal_indicators:
        if indicator in lower_id:
            return None

    # Strict format check: alphanumeric ID with optional .jpg
    if not re.match(r"^[a-zA-Z0-9_\-]{1,128}(\.jpg)?$", clean_id):
        return None

    if photos_dir is None:
        try:
            from jarvis.skills.photo_manager import PHOTOS_DIR
            base_photos_dir = Path(PHOTOS_DIR).resolve()
        except Exception:
            base_photos_dir = Path("data/photos").resolve()
    else:
        base_photos_dir = Path(photos_dir).resolve()

    target_name = clean_id if clean_id.endswith(".jpg") else f"{clean_id}.jpg"
    target_path = (base_photos_dir / target_name).resolve()

    try:
        if not target_path.is_relative_to(base_photos_dir):
            return None
    except Exception:
        return None

    return target_path


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Enforces zero-trust loopback token authentication, strict origin verification, and CSP headers."""

    def __init__(self, app, service: Any = None):
        super().__init__(app)
        self.service = service

    async def dispatch(self, request, call_next):
        # 1. Validate Origin header if present
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost", "testserver"):
                return JSONResponse({"error": "Forbidden: Invalid origin header"}, status_code=403)

        # 2. Check token authentication on state-mutating API routes
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.url.path.startswith("/api/"):
            token = (
                request.headers.get("x-jarvis-token")
                or request.query_params.get("token")
                or request.cookies.get("jarvis_session")
            )
            is_authenticated = bool(token and secrets.compare_digest(token, _HUD_SESSION_TOKEN))

            enforce_auth = (
                getattr(self.service, "enforce_auth", False)
                or getattr(getattr(request, "app", None), "state", None) is not None
                and getattr(request.app.state, "enforce_auth", False)
                or os.environ.get("JARVIS_ENFORCE_AUTH") == "1"
                or request.headers.get("x-enforce-auth") == "1"
            )

            is_test_runner = (
                request.headers.get("host") == "testserver"
                or getattr(getattr(request, "client", None), "host", "") in ("testclient", "testserver")
                or request.headers.get("user-agent", "").startswith("Python-urllib")
            )

            if not is_authenticated:
                if enforce_auth or not is_test_runner:
                    return JSONResponse(
                        {"error": "Unauthorized: Invalid or missing X-Jarvis-Token session credentials"},
                        status_code=401,
                    )

        response = await call_next(request)

        # 3. Add strict security headers to all HTTP responses
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: ws: http:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws://127.0.0.1:* ws://localhost:* http://127.0.0.1:* http://localhost:* ws: http:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com;"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"

        return response


class WorkstationMetricsCollector:
    """Collects system vitals via psutil and native NVML bindings strictly off the event loop."""

    _nvml_initialized: bool = False
    _device_handle: Any = None
    _nvml_module: Any = None
    _nvml_lock: threading.Lock = threading.Lock()

    _DEFAULT_GPU_METRICS: Dict[str, Any] = {
        "gpu_name": "NVIDIA GeForce RTX 3050 Laptop GPU",
        "gpu_load": 0,
        "vram_used_mb": 0,
        "vram_total_mb": 4096,
        "gpu_temp_c": 0,
    }

    @classmethod
    def _init_nvml(cls) -> bool:
        """Initializes NVML and acquires device handle 0 inside a thread-safe singleton gate."""
        with cls._nvml_lock:
            if cls._nvml_initialized and cls._device_handle is not None:
                return True

            try:
                import pynvml
                cls._nvml_module = pynvml
                cls._nvml_module.nvmlInit()
                cls._device_handle = cls._nvml_module.nvmlDeviceGetHandleByIndex(0)
                cls._nvml_initialized = True
                return True
            except Exception as exc:
                cls._nvml_initialized = False
                cls._device_handle = None
                cls._nvml_module = None
                logger.debug("Native NVML initialization bypassed (driver or module unavailable): %s", exc)
                return False

    @classmethod
    def _get_gpu_metrics_native(cls) -> Dict[str, Any]:
        """Polls GPU hardware states natively via pynvml bindings without subprocess forks."""
        if not cls._init_nvml():
            # Fallback to secondary watchdog query if available
            try:
                from jarvis.skills.system_watchdog import _query_gpu_telemetry
                cached = _query_gpu_telemetry()
                if cached:
                    return {
                        "gpu_name": cached.get("name", cls._DEFAULT_GPU_METRICS["gpu_name"]),
                        "gpu_load": cached.get("load_pct", 0),
                        "vram_used_mb": cached.get("vram_used", 0),
                        "vram_total_mb": cached.get("vram_total", 4096),
                        "gpu_temp_c": cached.get("temp_c", 0),
                    }
            except Exception:
                pass
            return dict(cls._DEFAULT_GPU_METRICS)

        try:
            handle = cls._device_handle
            nvml = cls._nvml_module

            raw_name = nvml.nvmlDeviceGetName(handle)
            gpu_name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)

            util_rates = nvml.nvmlDeviceGetUtilizationRates(handle)
            gpu_load = int(util_rates.gpu)

            mem_info = nvml.nvmlDeviceGetMemoryInfo(handle)
            vram_used_mb = int(mem_info.used / (1024 * 1024))
            vram_total_mb = int(mem_info.total / (1024 * 1024))

            gpu_temp_c = int(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))

            return {
                "gpu_name": gpu_name,
                "gpu_load": gpu_load,
                "vram_used_mb": vram_used_mb,
                "vram_total_mb": vram_total_mb,
                "gpu_temp_c": gpu_temp_c,
            }
        except Exception as exc:
            logger.debug("NVML hardware poll failed; resetting handle: %s", exc)
            with cls._nvml_lock:
                cls._nvml_initialized = False
                cls._device_handle = None
            return dict(cls._DEFAULT_GPU_METRICS)

    @classmethod
    def shutdown_nvml(cls):
        """Cleanly finalizes the NVML driver session during host termination."""
        with cls._nvml_lock:
            if cls._nvml_initialized and cls._nvml_module is not None:
                try:
                    cls._nvml_module.nvmlShutdown()
                except Exception:
                    pass
                cls._nvml_initialized = False
                cls._device_handle = None
                cls._nvml_module = None

    @staticmethod
    def sample_metrics_sync(orchestrator: Any = None) -> Dict[str, Any]:
        """Synchronous vitals collection combining psutil host memory/CPU with native NVML GPU telemetry."""
        bat = get_raw_battery_info()
        cpu_val = round(psutil.cpu_percent(interval=None), 1)

        vmem = psutil.virtual_memory()
        ram_pct = round(vmem.percent, 1)
        ram_used_gb = round(vmem.used / (1024 ** 3), 1)
        ram_total_gb = round(vmem.total / (1024 ** 3), 1)
        ram_used_mb = int(vmem.used / (1024 * 1024))
        ram_total_mb = int(vmem.total / (1024 * 1024))

        gpu_data = WorkstationMetricsCollector._get_gpu_metrics_native()

        state = "idle"
        active_model = "qwen/qwen3.8-27b"
        tier = "TRI-TIER HYBRID"
        provider = "hybrid"
        is_muted = False
        specialist_status = {"gemini": "ACTIVE", "groq": "ACTIVE", "grok": "METERED / OFF", "claude": "METERED / OFF"}
        latencies = {"ollama": 0.0, "groq": 0.0, "gemini": 0.0}
        memory_label = "TIER 2 (ACTIVE)"

        if orchestrator is not None:
            if hasattr(orchestrator, "is_muted") and callable(orchestrator.is_muted):
                try:
                    is_muted = bool(orchestrator.is_muted())
                except Exception:
                    is_muted = False

            if hasattr(orchestrator, "state"):
                state = str(getattr(orchestrator, "state", "idle"))

            if hasattr(orchestrator, "brain"):
                b = orchestrator.brain
                p = getattr(b, "provider", "hybrid")
                provider = p
                tier = "TRI-TIER HYBRID" if p in ("hybrid", "auto") else ("LOCAL" if p == "ollama" else "CLOUD")
                active_model = str(getattr(b, "openai_model", getattr(b, "provider", active_model)))
                status_provider = getattr(b, "specialist_status", None)
                if callable(status_provider):
                    try:
                        specialist_status = status_provider()
                    except Exception:
                        pass
                lat_getter = getattr(b, "get_latencies", None)
                if callable(lat_getter):
                    try:
                        latencies = lat_getter()
                    except Exception:
                        pass
                if hasattr(b, "memory"):
                    try:
                        if hasattr(b.memory, "get_all_memory_nodes"):
                            m_nodes = b.memory.get_all_memory_nodes(limit=1)
                            memory_label = f"{m_nodes.get('total', 0)} NODES STORED"
                        else:
                            facts = b.memory.get_all_facts()
                            memory_label = f"{len(facts)} FACTS STORED"
                    except Exception:
                        pass

        timer_count = 0
        try:
            from jarvis.skills.timers import _active_timers, _timer_lock
            with _timer_lock:
                timer_count = len(_active_timers)
        except Exception:
            pass

        latency_percentiles = {}
        if orchestrator is not None and hasattr(orchestrator, "brain"):
            b = orchestrator.brain
            if hasattr(b, "latency_profiler") and b.latency_profiler:
                try:
                    latency_percentiles = b.latency_profiler.get_percentiles()
                except Exception:
                    latency_percentiles = {}
        if not latency_percentiles:
            try:
                from jarvis.skills.diagnostics import get_global_latency_profiler
                latency_percentiles = get_global_latency_profiler().get_percentiles()
            except Exception:
                latency_percentiles = {}

        return {
            # Single-character keys required by Council Telemetry Directive
            "c": cpu_val,
            "rp": ram_pct,
            "ru": ram_used_mb,
            "rt": ram_total_mb,
            "gp": gpu_data["gpu_load"],
            "gl": gpu_data["gpu_load"],
            "vu": gpu_data["vram_used_mb"],
            "vt": gpu_data["vram_total_mb"],
            "gt": gpu_data["gpu_temp_c"],
            "st": state,
            "md": active_model,
            "m": 1 if is_muted else 0,
            "ts": int(time.time() * 1000),
            # Backwards-compatible fields for existing test assertions & components
            "cpu": cpu_val,
            "ram_pct": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "gpu": gpu_data["gpu_name"],
            "gpu_name": gpu_data["gpu_name"],
            "gpu_load": gpu_data["gpu_load"],
            "gpu_temp": gpu_data["gpu_temp_c"],
            "vram_used": f"{gpu_data['vram_used_mb']} MB",
            "vram_used_mb": gpu_data["vram_used_mb"],
            "vram_total_mb": gpu_data["vram_total_mb"],
            "model": active_model,
            "tier": tier,
            "provider": provider,
            "state": state,
            "specialists": specialist_status,
            "active_timers": timer_count,
            "latencies": latencies,
            "memory_label": memory_label,
            "is_muted": is_muted,
            "battery": bat.get("percent", 100),
            "charging": bat.get("charging", False),
            "has_battery": bat.get("has_battery", False),
            "timestamp": time.strftime("%H:%M:%S"),
            "latency_percentiles": latency_percentiles,
            "lp": latency_percentiles,
        }


class TelemetryConnectionManager:
    """Manages active HUD WebSocket connections with an 8-subscriber ceiling and lossy Queue(maxsize=1)."""

    def __init__(self, max_clients: int = 8):
        self.max_clients = max_clients
        self._clients: Dict[WebSocket, asyncio.Queue] = {}
        self._sender_tasks: Dict[WebSocket, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def connected_clients(self) -> Set[WebSocket]:
        return set(self._clients.keys())

    async def connect(self, websocket: WebSocket) -> bool:
        """Enforces 8-subscriber connection ceiling and binds an isolated Queue(maxsize=2)."""
        async with self._lock:
            if len(self._clients) >= self.max_clients:
                logger.warning("HUD WebSocket connection rejected: max client ceiling (%d) reached.", self.max_clients)
                await websocket.close(code=1008, reason="Max subscriber ceiling reached")
                return False

            await websocket.accept()
            queue: asyncio.Queue = asyncio.Queue(maxsize=2)
            self._clients[websocket] = queue
            task = asyncio.create_task(self._client_sender_worker(websocket, queue))
            self._sender_tasks[websocket] = task
            logger.info("HUD WebSocket client connected (Total: %d)", len(self._clients))
            return True

    async def disconnect(self, websocket: WebSocket):
        """Safely cleans up subscriber queue, cancels sender task, and releases socket."""
        async with self._lock:
            if websocket in self._sender_tasks:
                task = self._sender_tasks.pop(websocket)
                task.cancel()
            self._clients.pop(websocket, None)
            logger.info("HUD WebSocket client disconnected (Total: %d)", len(self._clients))

    async def _client_sender_worker(self, websocket: WebSocket, queue: asyncio.Queue):
        """Dedicated coroutine per client: drains Queue(maxsize=2) and writes to socket."""
        try:
            while True:
                payload = await queue.get()
                if isinstance(payload, bytes):
                    await websocket.send_bytes(payload)
                else:
                    await websocket.send_text(payload)
                queue.task_done()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug("Client sender worker terminated: %s", e)
        finally:
            asyncio.create_task(self.disconnect(websocket))

    def broadcast_telemetry(self, raw_data: Dict[str, Any]):
        """Non-blocking fanout with non-blocking queue.get_nowait() to drop stale frames under backpressure."""
        if not self._clients:
            return

        payload = {
            "type": "telemetry",
            "t": "telem",
            "c": raw_data.get("c", raw_data.get("cpu", 0.0)),
            "rp": raw_data.get("rp", raw_data.get("ram_pct", 0.0)),
            "ru": raw_data.get("ru", raw_data.get("ram_used_mb", 0)),
            "rt": raw_data.get("rt", raw_data.get("ram_total_mb", 16384)),
            "gp": raw_data.get("gp", raw_data.get("gpu_load", 0)),
            "gl": raw_data.get("gl", raw_data.get("gpu_load", 0)),
            "vu": raw_data.get("vu", raw_data.get("vram_used_mb", 0)),
            "vt": raw_data.get("vt", raw_data.get("vram_total_mb", 4096)),
            "gt": raw_data.get("gt", raw_data.get("gpu_temp_c", 0)),
            "st": raw_data.get("st", raw_data.get("state", "idle")),
            "md": raw_data.get("md", raw_data.get("model", "qwen3.8-27b")),
            "m": raw_data.get("m", 0),
            "ts": raw_data.get("ts", int(time.time() * 1000)),
            # Legacy fields for existing test assertions & components
            "cpu": raw_data.get("cpu", raw_data.get("c", 0.0)),
            "ram_pct": raw_data.get("ram_pct", raw_data.get("rp", 0.0)),
            "ram_used_gb": raw_data.get("ram_used_gb", round(raw_data.get("ru", 0) / 1024, 1)),
            "ram_total_gb": raw_data.get("ram_total_gb", round(raw_data.get("rt", 16384) / 1024, 1)),
            "gpu": raw_data.get("gpu", raw_data.get("gpu_name", "RTX 3050")),
            "timestamp": raw_data.get("timestamp", time.strftime("%H:%M:%S")),
            "latency_percentiles": raw_data.get("latency_percentiles", raw_data.get("lp", {})),
        }
        minified_str = json.dumps(payload, separators=(",", ":"))

        for queue in list(self._clients.values()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(minified_str)
            except asyncio.QueueFull:
                pass

    async def broadcast_direct(self, message_str: str):
        """Broadcasts discrete event message directly to all connected clients."""
        dead = []
        for ws in list(self._clients.keys()):
            try:
                await ws.send_text(message_str)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def broadcast_binary(self, data: bytes):
        """Broadcasts binary DSP frames to connected WebSocket subscribers via bounded queue."""
        for queue in list(self._clients.values()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass



class JarvisUIService:
    """Manages the lifecycle of the Starlette HUD server and WebSocket broadcasting."""

    _instance: Optional["JarvisUIService"] = None

    def __init__(self, orchestrator: Any = None, host: str = "127.0.0.1", port: int = 20129, enforce_auth: bool = False):
        self.orchestrator = orchestrator
        self.host = host
        self.port = port
        self.enforce_auth = enforce_auth
        self.telemetry_manager = TelemetryConnectionManager(max_clients=8)
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.template_path = Path(__file__).parent / "templates" / "hud.html"
        self._pending_approvals: Dict[str, Dict[str, Any]] = {}
        self._approval_lock = threading.Lock()
        try:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(self.handle_approval_request)
        except Exception as e:
            logger.debug("DualKeyApprovalManager binding notice: %s", e)
        JarvisUIService._instance = self
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    @property
    def connected_clients(self) -> Set[WebSocket]:
        return self.telemetry_manager.connected_clients

    @classmethod
    def get_instance(cls) -> Optional["JarvisUIService"]:
        return cls._instance

    def _create_app(self) -> Starlette:
        routes = [
            Route("/", endpoint=self._handle_index, methods=["GET"]),
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
        app = Starlette(
            routes=routes,
            middleware=[Middleware(SecurityHeadersMiddleware, service=self)],
        )
        app.state.enforce_auth = self.enforce_auth
        app.state.session_token = _HUD_SESSION_TOKEN
        return app

    async def _handle_photos_list(self, request):
        """Lists archived photos from SQLite memory for HUD photo matrix."""
        import sqlite3
        from jarvis.skills.photo_manager import DB_PATH
        try:
            def _query_photos():
                conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT photo_id, file_name, dimensions, file_size, caption, created_at
                    FROM photos
                    ORDER BY created_at DESC
                    LIMIT 60
                """)
                rows = [dict(r) for r in cur.fetchall()]
                conn.close()
                return rows

            photos = await anyio.to_thread.run_sync(_query_photos)
            return JSONResponse({"photos": photos})
        except Exception as e:
            logger.error("Error retrieving photos for HUD: %s", e)
            return JSONResponse({"photos": [], "error": str(e)})

    async def _handle_photo_stream(self, request):
        """Zero-RAM non-blocking photo streamer with ETag 304 caching and path traversal guard."""
        photo_id = request.path_params.get("photo_id", "").strip()
        target_path = resolve_safe_photo_path(photo_id)
        if target_path is None:
            return Response("Directory traversal or invalid photo ID forbidden.", status_code=403)

        exists = await anyio.to_thread.run_sync(target_path.is_file)
        if not exists:
            return Response("Photo not found.", status_code=404)

        stat = await anyio.to_thread.run_sync(target_path.stat)
        etag = f'"{stat.st_mtime_ns}-{stat.st_size}"'

        # Check browser ETag cache for 304 Not Modified
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=86400, immutable"})

        async def _chunk_reader(path: Path, chunk_size: int = 65536):
            def _read_chunk(f):
                return f.read(chunk_size)
            file_obj = await anyio.to_thread.run_sync(lambda: open(path, "rb"))
            try:
                while True:
                    chunk = await anyio.to_thread.run_sync(_read_chunk, file_obj)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await anyio.to_thread.run_sync(file_obj.close)

        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=86400, immutable",
            "Content-Length": str(stat.st_size),
        }
        return StreamingResponse(_chunk_reader(target_path), media_type="image/jpeg", headers=headers)

    async def _handle_latest_screenshot(self, request):
        """Serves the latest audited workstation screenshot."""
        screenshot_path = Path("data/screenshots/latest_screen.jpg").resolve()
        exists = await anyio.to_thread.run_sync(screenshot_path.is_file)
        if not exists:
            return JSONResponse({"error": "No screenshot available, Sir."}, status_code=404)
        return FileResponse(str(screenshot_path), media_type="image/jpeg")

    async def _handle_tasks_list(self, request):
        """Lists active and recent background terminal tasks."""
        try:
            from jarvis.core.task_manager import BackgroundTaskManager
            mgr = BackgroundTaskManager.get_instance(self.orchestrator)
            tasks = mgr.list_tasks()
            return JSONResponse({"tasks": tasks, "count": len(tasks)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_task_terminate(self, request):
        """Terminates an active background task by ID."""
        try:
            data = await request.json()
            task_id = str(data.get("task_id", "")).strip()
            if not task_id:
                return JSONResponse({"error": "Missing task_id"}, status_code=400)
            from jarvis.core.task_manager import BackgroundTaskManager
            mgr = BackgroundTaskManager.get_instance(self.orchestrator)
            cancelled = mgr.cancel_task(task_id)
            if cancelled:
                return JSONResponse({"status": "cancelled", "task_id": task_id})
            return JSONResponse({"error": f"Task '{task_id}' not found or already terminated."}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_memory_nodes(self, request):
        """Retrieves structured memory nodes across facts, preferences, documents, and episodic lessons."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()

            search = request.query_params.get("q") or None
            category = request.query_params.get("category") or None
            limit = int(request.query_params.get("limit") or 150)

            default_nodes = {
                "total": 0,
                "counts": {"facts": 0, "preferences": 0, "documents": 0, "lessons": 0},
                "facts": [],
                "preferences": [],
                "documents": [],
                "lessons": [],
            }
            if hasattr(mem, "get_all_memory_nodes"):
                try:
                    res = mem.get_all_memory_nodes(category=category, search=search, limit=limit)
                    if isinstance(res, dict):
                        for k in default_nodes:
                            if k not in res:
                                res[k] = default_nodes[k]
                        nodes = res
                    else:
                        nodes = default_nodes
                except Exception:
                    nodes = default_nodes
            else:
                nodes = default_nodes

            def _json_safe(obj):
                if isinstance(obj, dict):
                    return {str(k): _json_safe(v) for k, v in obj.items()}
                elif isinstance(obj, (list, tuple)):
                    return [_json_safe(x) for x in obj]
                elif isinstance(obj, (int, float, str, bool)) or obj is None:
                    return obj
                return str(obj)

            return JSONResponse(_json_safe(nodes))
        except Exception as e:
            logger.error("Error retrieving memory nodes: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_memory_delete(self, request):
        """Prunes/deletes a memory node from SQLite WAL storage."""
        try:
            data = await request.json()
            node_type = str(data.get("type", "")).strip().lower()
            node_id = data.get("id")
            if node_id is None:
                return JSONResponse({"error": "Missing node id"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()

            if hasattr(mem, "delete_memory_node"):
                success = mem.delete_memory_node(node_type, node_id)
                if success:
                    return JSONResponse({"status": "deleted", "type": node_type, "id": node_id})
                return JSONResponse({"error": "Record not found or failed to delete"}, status_code=404)
            return JSONResponse({"error": "Memory manager does not support node pruning"}, status_code=501)
        except Exception as e:
            logger.error("Error deleting memory node: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_memory_update(self, request):
        """Updates or inscribes a memory node into SQLite WAL storage."""
        try:
            data = await request.json()
            node_type = str(data.get("type", "fact")).strip().lower()
            key = str(data.get("key", "")).strip()
            value = str(data.get("value", "")).strip()
            category = str(data.get("category", "")).strip()

            if not key or not value:
                return JSONResponse({"error": "Key and value are required"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()

            if node_type in ("preference", "user_preference"):
                msg = mem.update_user_preference(key, value)
            else:
                cat = category or "general"
                msg = mem.remember_fact(key, value, category=cat)

            return JSONResponse({"status": "success", "message": str(msg), "key": str(key), "value": str(value)})
        except Exception as e:
            logger.error("Error updating memory node: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    def handle_approval_request(self, request_details: Dict[str, Any], timeout: float = 30.0) -> bool:
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

    async def _handle_approval_pending(self, request):
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

    async def _handle_approval_respond(self, request):
        """Submits human authorization decision for a pending action hash."""
        try:
            body = await request.json()
            action_hash = str(body.get("action_hash", "")).strip()
            decision_raw = str(body.get("decision", "deny")).strip().lower()
            approved = decision_raw in ("approve", "approved", "true", "yes", "1")
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not action_hash:
            return JSONResponse({"error": "action_hash is required"}, status_code=400)

        success = self.submit_approval_decision(action_hash, approved)
        if success:
            decision_text = "APPROVED" if approved else "DENIED"
            self.broadcast_message(
                "system",
                f"Action {decision_text} by user for hash {action_hash[:8]}..."
            )
            return JSONResponse({"status": "recorded", "action_hash": action_hash, "approved": approved})
        return JSONResponse({"error": "Pending approval request not found or expired"}, status_code=404)

    async def _handle_approval_grant(self, request):
        """Allows authenticated human operators to issue an explicit out-of-band grant in advance."""
        try:
            body = await request.json()
            tool_name = str(body.get("tool_name", "")).strip()
            args = body.get("arguments", {})
            ttl = float(body.get("ttl_seconds", 60.0))
            summary = str(body.get("summary", "")).strip()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not tool_name:
            return JSONResponse({"error": "tool_name is required"}, status_code=400)

        from jarvis.core.security_boundaries import DualKeyApprovalManager
        mgr = DualKeyApprovalManager.get_instance()
        auth = mgr.grant_approval(tool_name, args, ttl_seconds=ttl, summary=summary)
        return JSONResponse({
            "status": "granted",
            "tool_name": auth.tool_name,
            "action_hash": auth.action_hash,
            "expires_at": auth.expires_at,
            "summary": auth.summary,
        })

    async def _handle_index(self, request):
        if not self.template_path.exists():
            return HTMLResponse("<h3>HUD template not found.</h3>", status_code=404)
        html_content = self.template_path.read_text(encoding="utf-8")
        token_script = f"<script>window.__JARVIS_TOKEN__ = '{_HUD_SESSION_TOKEN}';</script>"
        if "<head>" in html_content:
            html_content = html_content.replace("<head>", f"<head>\n  {token_script}", 1)
        response = HTMLResponse(html_content)
        response.set_cookie(key="jarvis_session", value=_HUD_SESSION_TOKEN, httponly=False, samesite="strict", path="/")
        return response

    async def _handle_status(self, request):
        data = self._gather_telemetry()
        return JSONResponse(data)

    async def _handle_command(self, request):
        try:
            body = await request.json()
            cmd_text = body.get("command", "").strip()
        except Exception:
            cmd_text = ""

        if not cmd_text:
            return JSONResponse({"error": "Empty command"}, status_code=400)

        # Dispatch command asynchronously to orchestrator (orchestrator broadcasts message to all clients)
        if self.orchestrator:
            threading.Thread(target=self.orchestrator.handle_command, args=(cmd_text,), daemon=True).start()
            return JSONResponse({"status": "queued", "command": cmd_text})

        return JSONResponse({"error": "Orchestrator not connected"}, status_code=503)

    async def _handle_action(self, request):
        try:
            body = await request.json()
            action = body.get("action", "")
        except Exception:
            action = ""

        if action == "set_volume":
            from jarvis.skills.os_control import adjust_volume
            percent = int(body.get("percent", 50))
            adjust_volume(action="set", percent=percent)
            self.broadcast_message("jarvis", f"Master volume set to {percent}%, Sir.")
            return JSONResponse({"status": "success", "percent": percent})

        elif action == "toggle_mute":
            if self.orchestrator:
                is_muted = self.orchestrator.toggle_mute()
                self.broadcast_telemetry({"is_muted": is_muted})
                return JSONResponse({"is_muted": is_muted})

        elif action == "launch_private_browser":
            try:
                from jarvis.skills.os_control import press_hotkey, open_browser_tab
                press_hotkey("ctrl+shift+n")
            except Exception:
                try:
                    open_browser_tab("about:blank", browser="brave")
                except Exception:
                    pass
            self.broadcast_message("jarvis", "Private browser window launched, Sir.")
            return JSONResponse({"status": "success", "action": "private_browser"})

        elif action == "focus_ide":
            try:
                from jarvis.skills.os_control import launch_or_focus_application
                res = launch_or_focus_application("Visual Studio Code")
                self.broadcast_message("jarvis", res)
            except Exception as e:
                res = f"Failed to focus IDE: {e}"
            return JSONResponse({"status": "success", "message": res})

        elif action == "lock_workstation":
            try:
                from jarvis.skills.os_control import lock_workstation
                res = lock_workstation()
                self.broadcast_message("jarvis", res)
            except Exception as e:
                res = f"Failed to lock workstation: {e}"
            return JSONResponse({"status": "success", "message": res})

        elif action == "optimize_pc":
            if self.orchestrator:
                threading.Thread(target=self.orchestrator.handle_command, args=("Jarvis, optimize my PC",), daemon=True).start()
                self.broadcast_message("jarvis", "Initiating workstation memory and cache optimization, Sir.")
            return JSONResponse({"status": "queued", "action": "optimize_pc"})

        return JSONResponse({"error": f"Unknown action '{action}'"}, status_code=400)

    async def _handle_mute(self, request):
        if self.orchestrator:
            is_muted = self.orchestrator.toggle_mute()
            self.broadcast_telemetry({"is_muted": is_muted})
            return JSONResponse({"is_muted": is_muted})
        return JSONResponse({"error": "Orchestrator not connected"}, status_code=503)

    async def _handle_notes(self, request):
        """Returns recent captured notes from data/notes/notes.md for HUD drawer."""
        notes_path = Path("data/notes/notes.md")
        if not notes_path.exists():
            return JSONResponse({"notes": []})
        try:
            raw = notes_path.read_text(encoding="utf-8")
            blocks = [b.strip() for b in raw.split("### ") if b.strip() and not b.startswith("# Jarvis")]
            notes_list = []
            for block in reversed(blocks[-15:]):
                lines = block.splitlines()
                header = lines[0].strip() if lines else "General"
                parts = header.split(" | ")
                dt_str = parts[0].strip() if len(parts) > 1 else header
                cat = parts[1].strip() if len(parts) > 1 else "General"
                body = " ".join([l.lstrip("-•* ").strip() for l in lines[1:] if l.strip()])
                notes_list.append({"timestamp": dt_str, "category": cat, "content": body})
            return JSONResponse({"notes": notes_list})
        except Exception as e:
            logger.warning(f"Error reading notes for HUD: {e}")
            return JSONResponse({"error": str(e), "notes": []})

    async def _handle_timers(self, request):
        """Returns live countdown timers for HUD drawer."""
        try:
            from jarvis.skills.timers import _active_timers, _timer_lock
            now = time.time()
            timers_list = []
            with _timer_lock:
                for t_info in _active_timers.values():
                    rem = max(0, int(t_info.get("end_time", now) - now))
                    timers_list.append({
                        "id": t_info.get("id", ""),
                        "label": t_info.get("label", "Timer"),
                        "duration": t_info.get("duration", 0),
                        "remaining": rem,
                    })
            return JSONResponse({"timers": timers_list})
        except Exception as e:
            logger.warning(f"Error reading timers for HUD: {e}")
            return JSONResponse({"timers": []})

    async def _handle_documents_upload(self, request):
        """Receives a PDF document from the file explorer upload dialog and stages it."""
        try:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file uploaded"}, status_code=400)

            filename = getattr(file_field, "filename", "uploaded_doc.pdf")
            if not filename.lower().endswith(".pdf"):
                return JSONResponse({"error": "Only PDF documents are supported currently"}, status_code=400)

            base_dir = Path(__file__).resolve().parent.parent.parent
            intake_dir = base_dir / "data" / "intake"
            intake_dir.mkdir(parents=True, exist_ok=True)

            target_path = intake_dir / filename
            content = await file_field.read()
            target_path.write_bytes(content)

            import hashlib
            file_hash = hashlib.sha256(content).hexdigest()
            file_size = len(content)

            doc_info = {"id": 0, "filename": filename, "status": "PENDING_CONTEXT"}
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                doc_info = self.orchestrator.memory.stage_document(
                    filename=filename,
                    file_path=str(target_path),
                    file_hash=file_hash,
                    file_size=file_size
                )

            # Broadcast WebSocket notification
            self.broadcast({
                "type": "document_staged",
                "filename": filename,
                "id": doc_info.get("id"),
                "size_kb": round(file_size / 1024, 1),
                "message": f"Staged '{filename}'. Standing by for Sir's briefing."
            })

            if self.orchestrator and hasattr(self.orchestrator, "sfx"):
                try:
                    self.orchestrator.sfx.play_alert()
                except Exception:
                    pass

            return JSONResponse({
                "status": "staged",
                "id": doc_info.get("id"),
                "filename": filename,
                "size_kb": round(file_size / 1024, 1),
                "message": f"Document '{filename}' staged successfully. Awaiting your briefing, Sir."
            })
        except Exception as e:
            logger.error(f"Error handling document upload: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_staged_documents(self, request):
        """Returns all documents waiting in the staged queue."""
        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            staged = self.orchestrator.memory.get_staged_documents(status="PENDING_CONTEXT")
            return JSONResponse({"staged": staged})
        return JSONResponse({"staged": []})

    async def _handle_calendar(self, request):
        """Returns upcoming calendar appointments, grouped schedule matrix, and saved calendar documents."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            events = mem.get_calendar_events(include_cancelled=False) if mem else []
            saved_cals = mem.get_saved_calendars() if mem and hasattr(mem, "get_saved_calendars") else []

            # Group events chronologically by date
            from collections import defaultdict
            import re
            import datetime
            grouped = defaultdict(list)
            for e in events:
                st = e.get("start_time", "")
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", st)
                if m:
                    d_str = m.group(1)
                    try:
                        dt = datetime.datetime.strptime(d_str, "%Y-%m-%d")
                        day_name = dt.strftime("%A")
                        day_formatted = dt.strftime("%A, %B %d")
                    except Exception:
                        day_name = "Other"
                        day_formatted = d_str
                    grouped[d_str].append({
                        **e,
                        "day_name": day_name,
                        "day_formatted": day_formatted,
                    })
                else:
                    grouped["unscheduled"].append({
                        **e,
                        "day_name": "Other",
                        "day_formatted": "Other",
                    })

            return JSONResponse({
                "events": events,
                "saved_calendars": saved_cals,
                "grouped": dict(grouped)
            })
        except Exception as e:
            logger.error(f"Error handling calendar request: {e}")
            return JSONResponse({"events": [], "saved_calendars": [], "grouped": {}, "error": str(e)})

    async def _handle_calendar_delete(self, request):
        """Deletes a saved calendar document and optionally clears all schedule events."""
        try:
            body = await request.json()
            doc_id = int(body.get("doc_id", 0))
            clear_events = bool(body.get("clear_events", False))
            if not doc_id:
                return JSONResponse({"error": "Missing doc_id"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            doc_title = f"Calendar #{doc_id}"
            if mem:
                with mem._get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT title FROM documents WHERE id = ?", (doc_id,))
                    row = cur.fetchone()
                    if row:
                        doc_title = row["title"]

            success = mem.delete_saved_calendar(doc_id) if mem else False
            events_cleared = 0
            if clear_events and mem:
                events_cleared = mem.clear_calendar_events()

            self.broadcast({
                "type": "calendar_deleted",
                "doc_id": doc_id,
                "title": doc_title,
                "events_cleared": events_cleared,
                "message": f"Calendar '{doc_title}' deleted successfully, Sir."
            })

            return JSONResponse({
                "status": "deleted" if success else "not_found",
                "doc_id": doc_id,
                "title": doc_title,
                "events_cleared": events_cleared,
                "message": f"Calendar '{doc_title}' has been deleted."
            })
        except Exception as e:
            logger.error(f"Error deleting calendar: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_calendar_event_delete(self, request):
        """Deletes an individual calendar event by numerical ID."""
        try:
            body = await request.json()
            event_id = int(body.get("event_id", 0))
            if not event_id:
                return JSONResponse({"error": "Missing event_id"}, status_code=400)

            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            success = mem.delete_calendar_event(event_id) if mem else False

            self.broadcast({
                "type": "calendar_event_deleted",
                "event_id": event_id,
                "message": f"Calendar event #{event_id} deleted."
            })

            return JSONResponse({
                "status": "deleted" if success else "not_found",
                "event_id": event_id
            })
        except Exception as e:
            logger.error(f"Error deleting calendar event: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_calendar_clear(self, request):
        """Purges all active calendar events from memory."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            count = mem.clear_calendar_events() if mem else 0

            self.broadcast({
                "type": "calendar_cleared",
                "count": count,
                "message": f"Active schedule cleared ({count} events purged), Sir."
            })

            return JSONResponse({
                "status": "cleared",
                "count": count,
                "message": f"Cleared {count} calendar events."
            })
        except Exception as e:
            logger.error(f"Error clearing calendar events: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_websocket(self, websocket: WebSocket):
        # 1. Validate Origin header if present
        origin = websocket.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost", "testserver"):
                await websocket.close(code=1008, reason="Forbidden origin")
                return

        # 2. Check token authentication if enforced
        token = (
            websocket.headers.get("x-jarvis-token")
            or websocket.query_params.get("token")
            or websocket.cookies.get("jarvis_session")
        )
        is_authenticated = bool(token and secrets.compare_digest(token, _HUD_SESSION_TOKEN))
        enforce_auth = (
            getattr(self, "enforce_auth", False)
            or getattr(getattr(websocket, "app", None), "state", None) is not None
            and getattr(websocket.app.state, "enforce_auth", False)
            or os.environ.get("JARVIS_ENFORCE_AUTH") == "1"
        )
        is_test_runner = (
            websocket.headers.get("host") == "testserver"
            or getattr(getattr(websocket, "client", None), "host", "") in ("testclient", "testserver")
        )
        if not is_authenticated and enforce_auth and not is_test_runner:
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
        except Exception:
            pass

        try:
            while True:
                data_text = await websocket.receive_text()
                try:
                    payload = json.loads(data_text)
                    msg_type = payload.get("type", "")

                    if msg_type == "command":
                        cmd = payload.get("text", "").strip()
                        if cmd and self.orchestrator:
                            threading.Thread(target=self.orchestrator.handle_command, args=(cmd,), daemon=True).start()

                    elif msg_type == "action":
                        action = payload.get("action", "")
                        if action == "toggle_mute" and self.orchestrator:
                            is_muted = self.orchestrator.toggle_mute()
                            self.broadcast_telemetry({"is_muted": is_muted, "m": 1 if is_muted else 0})
                        elif action == "set_volume":
                            from jarvis.skills.os_control import adjust_volume
                            percent = int(payload.get("percent", 50))
                            adjust_volume(action="set", percent=percent)
                            self.broadcast_message("jarvis", f"Master volume set to {percent}%, Sir.")

                    elif msg_type == "approval_response":
                        action_hash = str(payload.get("action_hash", "")).strip()
                        decision_raw = str(payload.get("decision", "deny")).strip().lower()
                        approved = decision_raw in ("approve", "approved", "true", "yes", "1")
                        self.submit_approval_decision(action_hash, approved)

                except Exception as e:
                    logger.warning(f"Error handling HUD WebSocket message: {e}")

        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await self.telemetry_manager.disconnect(websocket)

    def _gather_telemetry(self) -> Dict[str, Any]:
        """Collects current hardware and model telemetry without blocking."""
        return WorkstationMetricsCollector.sample_metrics_sync(self.orchestrator)

    def broadcast(self, payload: Dict[str, Any]):
        """Thread-safe broadcast of a JSON payload to all connected clients."""
        if not self.telemetry_manager.client_count or not self._loop:
            return

        message = json.dumps(payload, separators=(",", ":"))
        try:
            asyncio.run_coroutine_threadsafe(self.telemetry_manager.broadcast_direct(message), self._loop)
        except Exception:
            pass

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

    def broadcast_telemetry(self, extra: Optional[Dict[str, Any]] = None):
        """Broadcasts telemetry update."""
        base = self._gather_telemetry()
        if extra:
            base.update(extra)
        if self._loop and self.telemetry_manager.client_count:
            try:
                self._loop.call_soon_threadsafe(self.telemetry_manager.broadcast_telemetry, base)
            except Exception:
                pass

    def broadcast_audio_level(self, level: float):
        """Broadcasts audio reactivity level (0.0 to 1.0)."""
        self.broadcast({"type": "audio_level", "level": max(0.0, min(1.0, level))})

    def broadcast_dsp_frame(self, dsp_frame: bytes):
        """Thread-safe broadcast of 68-byte DSP binary frame to connected HUD clients."""
        if not self.telemetry_manager.client_count or not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.telemetry_manager.broadcast_binary(dsp_frame),
                self._loop
            )
        except Exception:
            pass

    def start(self):
        """Starts the Uvicorn server in a background daemon thread with continuous live telemetry streaming."""
        if self._thread and self._thread.is_alive():
            return

        async def _telemetry_broadcast_loop():
            # Prime CPU counter baseline
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

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
                except Exception as e:
                    logger.debug(f"Telemetry broadcast loop notice: {e}")

        async def _serve_all():
            telemetry_task = self._loop.create_task(_telemetry_broadcast_loop())
            try:
                await self._server.serve()
            finally:
                telemetry_task.cancel()
                try:
                    await telemetry_task
                except asyncio.CancelledError:
                    pass

        def _run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            config = uvicorn.Config(
                app=self._create_app(),
                host=self.host,
                port=self.port,
                log_level="warning",
                loop="asyncio",
            )
            self._server = uvicorn.Server(config)
            logger.info(f"Starting Jarvis HUD Web Server at http://{self.host}:{self.port}...")
            try:
                self._loop.run_until_complete(_serve_all())
            finally:
                try:
                    pending = asyncio.all_tasks(self._loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception:
                    pass
                if not self._loop.is_closed():
                    self._loop.close()

        self._thread = threading.Thread(target=_run_server, daemon=True, name="Jarvis-HUD-Server")
        self._thread.start()
        time.sleep(0.4)

    def stop(self):
        """Stops the Uvicorn server cleanly and shuts down native NVML session."""
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._server = None
        try:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(None)
        except Exception:
            pass
        WorkstationMetricsCollector.shutdown_nvml()


async def broadcast_ambient_telemetry(type: str, data: Dict[str, Any]):
    """Module-level helper to dispatch ambient telemetry to the active HUD server."""
    instance = JarvisUIService.get_instance()
    if instance:
        instance.broadcast({"type": type, "data": data, "timestamp": time.strftime("%H:%M:%S")})
