"""Workspace-scoped memory node inspection, document staging, and timer routes for Jarvis 2.0 HUD."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import anyio
from starlette.responses import JSONResponse

from jarvis.ui.routes import resolve_request_workspace

logger = logging.getLogger("Jarvis.UI.Memory")


class MemoryRoutesMixin:
    """Mixin providing workspace-isolated memory inspection, fact CRUD, and document intake endpoints."""

    orchestrator: Any

    async def _handle_memory_nodes(self, request: Any) -> JSONResponse:
        """Retrieves structured memory nodes across facts, preferences, documents, and episodic lessons."""
        try:
            ws_id = resolve_request_workspace(request, self.orchestrator)
        except PermissionError as pe:
            return JSONResponse({"error": str(pe)}, status_code=403)
        except ValueError as ve:
            return JSONResponse({"error": str(ve)}, status_code=400)

        mem = None
        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            mem = self.orchestrator.memory
        else:
            try:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()
            except (ImportError, AttributeError):
                mem = None

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
                def _fetch_nodes():
                    return mem.get_all_memory_nodes(category=category, search=search, limit=limit, workspace_id=ws_id)

                res = await anyio.to_thread.run_sync(_fetch_nodes)
                if isinstance(res, dict):
                    for k, default_val in default_nodes.items():
                        if k not in res:
                            res[k] = default_val
                    nodes = res
                else:
                    nodes = default_nodes
            except Exception as fetch_err:  # noqa: BLE001
                logger.debug("Memory nodes fetch notice: %s", fetch_err)
                nodes = default_nodes
        else:
            nodes = default_nodes

        def _json_safe(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {str(k): _json_safe(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [_json_safe(x) for x in obj]
            elif isinstance(obj, (int, float, str, bool)) or obj is None:
                return obj
            return str(obj)

        return JSONResponse(_json_safe(nodes))

    async def _handle_memory_delete(self, request: Any) -> JSONResponse:
        """Prunes/deletes a memory node from SQLite WAL storage."""
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        node_type = str(data.get("type", "")).strip().lower()
        node_id = data.get("id")
        if node_id is None:
            return JSONResponse({"error": "Missing node id"}, status_code=400)

        mem = None
        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            mem = self.orchestrator.memory
        else:
            try:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()
            except (ImportError, AttributeError):
                mem = None

        if hasattr(mem, "delete_memory_node"):
            def _delete_node():
                return mem.delete_memory_node(node_type, node_id)

            success = await anyio.to_thread.run_sync(_delete_node)
            if success:
                return JSONResponse({"status": "deleted", "type": node_type, "id": node_id})
            return JSONResponse({"error": "Record not found or failed to delete"}, status_code=404)
        return JSONResponse({"error": "Memory manager does not support node pruning"}, status_code=501)

    async def _handle_memory_update(self, request: Any) -> JSONResponse:
        """Updates or inscribes a memory node into SQLite WAL storage."""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}

        try:
            ws_id = resolve_request_workspace(data, self.orchestrator)
        except PermissionError as pe:
            return JSONResponse({"error": str(pe)}, status_code=403)
        except ValueError as ve:
            return JSONResponse({"error": str(ve)}, status_code=400)

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
            try:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()
            except (ImportError, AttributeError):
                mem = None

        if not mem:
            return JSONResponse({"error": "Memory manager not available"}, status_code=503)

        def _update_node():
            if node_type in ("preference", "user_preference"):
                return mem.update_user_preference(key, value)
            cat = category or "general"
            return mem.remember_fact(key, value, category=cat, workspace_id=ws_id)

        msg = await anyio.to_thread.run_sync(_update_node)

        return JSONResponse({"status": "success", "message": str(msg), "key": str(key), "value": str(value), "workspace_id": ws_id})

    async def _handle_documents_upload(self, request: Any) -> JSONResponse:
        """Receives a PDF document from the file explorer upload dialog and stages it."""
        try:
            ws_id = resolve_request_workspace(request, self.orchestrator)
        except PermissionError as pe:
            return JSONResponse({"error": str(pe)}, status_code=403)
        except ValueError as ve:
            return JSONResponse({"error": str(ve)}, status_code=400)

        try:
            form = await request.form()
        except Exception as form_err:  # noqa: BLE001
            return JSONResponse({"error": f"Failed to parse form: {form_err}"}, status_code=400)

        file_field = form.get("file")
        if not file_field:
            return JSONResponse({"error": "No file uploaded"}, status_code=400)

        filename = getattr(file_field, "filename", "uploaded_doc.pdf")
        if not filename.lower().endswith(".pdf"):
            return JSONResponse({"error": "Only PDF documents are supported currently"}, status_code=400)

        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        intake_dir = base_dir / "data" / "intake"
        intake_dir.mkdir(parents=True, exist_ok=True)

        target_path = intake_dir / filename
        content = await file_field.read()
        target_path.write_bytes(content)

        file_hash = hashlib.sha256(content).hexdigest()
        file_size = len(content)

        doc_info = {"id": 0, "filename": filename, "status": "PENDING_CONTEXT", "workspace_id": ws_id}
        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            doc_info = self.orchestrator.memory.stage_document(
                filename=filename,
                file_path=str(target_path),
                file_hash=file_hash,
                file_size=file_size,
                workspace_id=ws_id,
            )

        if hasattr(self, "broadcast"):
            self.broadcast({
                "type": "document_staged",
                "filename": filename,
                "id": doc_info.get("id"),
                "size_kb": round(file_size / 1024, 1),
                "workspace_id": ws_id,
                "message": f"Staged '{filename}'. Standing by for Sir's briefing.",
            })

        if self.orchestrator and hasattr(self.orchestrator, "sfx"):
            try:
                self.orchestrator.sfx.play_alert()
            except (AttributeError, RuntimeError, OSError):
                pass

        return JSONResponse({
            "status": "staged",
            "id": doc_info.get("id"),
            "filename": filename,
            "size_kb": round(file_size / 1024, 1),
            "workspace_id": ws_id,
            "message": f"Document '{filename}' staged successfully. Awaiting your briefing, Sir.",
        })

    async def _handle_staged_documents(self, request: Any) -> JSONResponse:
        """Returns all documents waiting in the staged queue."""
        try:
            ws_id = resolve_request_workspace(request, self.orchestrator)
        except PermissionError as pe:
            return JSONResponse({"error": str(pe)}, status_code=403)
        except ValueError as ve:
            return JSONResponse({"error": str(ve)}, status_code=400)

        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            staged = self.orchestrator.memory.get_staged_documents(status="PENDING_CONTEXT", workspace_id=ws_id)
            return JSONResponse({"staged": staged, "workspace_id": ws_id})
        return JSONResponse({"staged": [], "workspace_id": ws_id})

    async def _handle_notes(self, request: Any) -> JSONResponse:
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
        except OSError as err:
            logger.warning("Error reading notes for HUD: %s", err)
            return JSONResponse({"error": str(err), "notes": []})

    async def _handle_timers(self, request: Any) -> JSONResponse:
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
        except (ImportError, AttributeError, RuntimeError) as err:
            logger.warning("Error reading timers for HUD: %s", err)
            return JSONResponse({"timers": []})
