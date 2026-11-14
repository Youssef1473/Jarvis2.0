"""WebSocket connection pool and lossy telemetry fanout for Jarvis 2.0 HUD."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("Jarvis.UI.Telemetry")


class TelemetryConnectionManager:
    """Manages active HUD WebSocket connections with an 8-subscriber ceiling and lossy Queue(maxsize=2)."""

    def __init__(self, max_clients: int = 8):
        self.max_clients = max_clients
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._sender_tasks: dict[WebSocket, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def connected_clients(self) -> set[WebSocket]:
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
        except (RuntimeError, OSError) as e:
            logger.debug("Client sender worker socket closed: %s", e)
        except Exception as e:  # noqa: BLE001 - Worker loop teardown must not leak uncaught exceptions
            logger.debug("Client sender worker terminated: %s", e)
        finally:
            asyncio.create_task(self.disconnect(websocket))

    def broadcast_telemetry(self, raw_data: dict[str, Any]):
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
            "p99_history": raw_data.get("p99_history", raw_data.get("p99_h", [])),
            "p99_h": raw_data.get("p99_h", raw_data.get("p99_history", [])),
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
            except (WebSocketDisconnect, RuntimeError, OSError) as err:
                logger.debug("WebSocket client disconnected during direct broadcast: %s", err)
                dead.append(ws)
            except Exception as e:  # noqa: BLE001 - Keep connection pool robust against client socket faults
                logger.debug("Unexpected error during direct broadcast to client: %s", e)
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
