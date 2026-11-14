"""Photo management, screenshot streaming, and task management routes for Jarvis 2.0 HUD."""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import anyio
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

logger = logging.getLogger("Jarvis.UI.Media")


def resolve_safe_photo_path(photo_id: str, photos_dir: Path | None = None) -> Path | None:
    """Resolves a photo ID safely within the photos directory, preventing directory traversal.

    Rejects any paths containing traversal characters or absolute paths.
    """
    if not photo_id or not isinstance(photo_id, str):
        return None

    clean_id = photo_id.strip()

    # Reject path traversal patterns explicitly
    traversal_indicators = ("..", "/", "\\", "%2e", "%2f", "%5c", "\0")
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
        except (ImportError, AttributeError, OSError) as err:
            logger.debug("Photo base directory resolution fallback: %s", err)
            base_photos_dir = Path("data/photos").resolve()
    else:
        base_photos_dir = Path(photos_dir).resolve()

    target_name = clean_id if clean_id.endswith(".jpg") else f"{clean_id}.jpg"
    target_path = (base_photos_dir / target_name).resolve()

    try:
        if not target_path.is_relative_to(base_photos_dir):
            return None
    except (ValueError, TypeError) as err:
        logger.debug("Photo path boundary containment check notice: %s", err)
        return None

    return target_path


class MediaRoutesMixin:
    """Mixin providing photo catalog, streaming, screenshot, and task lifecycle endpoints."""

    orchestrator: Any

    async def _handle_photos_list(self, request: Any) -> JSONResponse:
        """Lists archived photos from SQLite memory for HUD photo matrix."""
        try:
            from jarvis.skills.photo_manager import DB_PATH
        except (ImportError, AttributeError):
            DB_PATH = Path("data/photos/photos.db")

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
        except Exception as e:  # noqa: BLE001
            logger.error("Error retrieving photos for HUD: %s", e)
            return JSONResponse({"photos": [], "error": str(e)})

    async def _handle_photo_stream(self, request: Any) -> Response:
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
            file_obj = await anyio.to_thread.run_sync(lambda: open(path, "rb"))  # noqa: SIM115
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

    async def _handle_latest_screenshot(self, request: Any) -> Response:
        """Serves the latest audited workstation screenshot."""
        screenshot_path = Path("data/screenshots/latest_screen.jpg").resolve()
        exists = await anyio.to_thread.run_sync(screenshot_path.is_file)
        if not exists:
            return JSONResponse({"error": "No screenshot available, Sir."}, status_code=404)
        return FileResponse(str(screenshot_path), media_type="image/jpeg")

    async def _handle_tasks_list(self, request: Any) -> JSONResponse:
        """Lists active and recent background terminal tasks."""
        try:
            from jarvis.core.task_manager import BackgroundTaskManager
            mgr = BackgroundTaskManager.get_instance(self.orchestrator)
            tasks = mgr.list_tasks()
            return JSONResponse({"tasks": tasks, "count": len(tasks)})
        except (ImportError, AttributeError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_task_terminate(self, request: Any) -> JSONResponse:
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
        except (ValueError, TypeError) as ve:
            return JSONResponse({"error": str(ve)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)
