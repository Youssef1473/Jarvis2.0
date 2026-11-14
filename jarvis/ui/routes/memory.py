"""Workspace-scoped memory node inspection, document staging, and timer routes for Jarvis 2.0 HUD."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from pathlib import Path
from typing import Any

import anyio
from starlette.responses import JSONResponse

from jarvis.ui.routes import handle_route_errors, resolve_request_workspace
from jarvis.ui.security import resolve_active_session_token

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

    @handle_route_errors
    async def _handle_workspaces_list(self, request: Any) -> JSONResponse:
        """Lists active multi-tenant workspaces, partition fact/vector volumes, and virtual table headroom."""
        # 1. Enforce session authentication if enabled
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

        # 2. If a specific workspace query param is provided, validate it fail-closed
        query_ws = request.query_params.get("workspace_id")
        if query_ws is not None:
            resolve_request_workspace({"workspace_id": query_ws})

        mem = None
        if self.orchestrator and hasattr(self.orchestrator, "memory"):
            mem = self.orchestrator.memory
        else:
            try:
                from jarvis.skills.productivity import _get_memory
                mem = _get_memory()
            except (ImportError, AttributeError):
                mem = None

        def _fetch_workspace_stats() -> dict[str, Any]:
            active_ws = getattr(self.orchestrator, "active_workspace_id", None)
            if not active_ws and mem and hasattr(mem, "get_active_workspace_id"):
                active_ws = mem.get_active_workspace_id()
            if not active_ws:
                active_ws = "ws_default"

            if not mem or not hasattr(mem, "_get_ro_connection"):
                return {
                    "status": "ok",
                    "active_workspace_id": active_ws,
                    "workspaces": [
                        {
                            "workspace_id": "ws_default",
                            "name": "Default Workspace",
                            "path": "",
                            "created_at": 0.0,
                            "facts_count": 0,
                            "vectors_count": 0,
                            "is_active": True,
                        }
                    ],
                    "total_vectors": 0,
                    "vector_ceiling": 2500,
                    "headroom_remaining": 2500,
                }

            with mem._get_ro_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT workspace_id, name, path, is_archived, created_at
                    FROM workspaces
                    WHERE workspace_id != 'unscoped' AND is_archived = 0
                    ORDER BY created_at ASC
                    """
                )
                ws_rows = cursor.fetchall()

                total_vectors = 0
                try:
                    cursor.execute("SELECT count(1) FROM facts_vec")
                    v_tot_row = cursor.fetchone()
                    if v_tot_row:
                        total_vectors = int(v_tot_row[0])
                except Exception:  # noqa: BLE001
                    total_vectors = 0

                workspaces_data = []
                for row in ws_rows:
                    wid = row["workspace_id"]
                    if not wid or wid.lower() == "unscoped":
                        continue

                    cursor.execute(
                        "SELECT count(1) FROM facts WHERE workspace_id = ? AND scope = 'workspace'",
                        (wid,),
                    )
                    f_row = cursor.fetchone()
                    facts_count = int(f_row[0]) if f_row else 0

                    vectors_count = 0
                    try:
                        cursor.execute(
                            """
                            SELECT count(1)
                            FROM facts f
                            JOIN facts_vec v ON f.id = v.id
                            WHERE f.workspace_id = ? AND f.scope = 'workspace'
                            """,
                            (wid,),
                        )
                        v_row = cursor.fetchone()
                        if v_row:
                            vectors_count = int(v_row[0])
                    except Exception:  # noqa: BLE001
                        vectors_count = 0

                    workspaces_data.append({
                        "workspace_id": wid,
                        "name": row["name"],
                        "path": row["path"],
                        "created_at": row["created_at"],
                        "facts_count": facts_count,
                        "vectors_count": vectors_count,
                        "is_active": (wid == active_ws),
                    })

                if not workspaces_data:
                    workspaces_data.append({
                        "workspace_id": "ws_default",
                        "name": "Default Workspace",
                        "path": "",
                        "created_at": 0.0,
                        "facts_count": 0,
                        "vectors_count": 0,
                        "is_active": True,
                    })

                return {
                    "status": "ok",
                    "active_workspace_id": active_ws,
                    "workspaces": workspaces_data,
                    "total_vectors": total_vectors,
                    "vector_ceiling": 2500,
                    "headroom_remaining": max(0, 2500 - total_vectors),
                }

        data = await asyncio.to_thread(_fetch_workspace_stats)
        return JSONResponse(data)

    _handle_workspace_list = _handle_workspaces_list

    @handle_route_errors
    async def _handle_workspaces_switch(self, request: Any) -> JSONResponse:
        """Transitions active tenant workspace, persisting active_context via SingleWriterActor."""
        # 1. Enforce session authentication if enabled
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

        # 2. Parse request body
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        target_ws = body.get("target_workspace_id") or body.get("workspace_id")
        if not target_ws or not str(target_ws).strip():
            return JSONResponse({"error": "target_workspace_id is required"}, status_code=400)

        # 3. Validate format and fail-closed rejection of 'unscoped'
        clean_ws = resolve_request_workspace({"workspace_id": target_ws})

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

        # 4. Verify workspace existence and non-archived status, then write active_context
        def _verify_and_switch():
            with mem._get_ro_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT workspace_id, is_archived FROM workspaces WHERE workspace_id = ?",
                    (clean_ws,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Workspace '{clean_ws}' does not exist")
                if row["is_archived"]:
                    raise PermissionError(f"Cannot switch to archived workspace '{clean_ws}'")

            mem.set_active_context(clean_ws, None, switch_reason="hud_user_switch")

        await asyncio.to_thread(_verify_and_switch)

        # 5. Update orchestrator active workspace state
        if self.orchestrator:
            self.orchestrator.active_workspace_id = clean_ws

        # 6. Broadcast telemetry event to connected HUD clients
        if hasattr(self, "broadcast"):
            self.broadcast({
                "type": "workspace_switched",
                "active_workspace_id": clean_ws,
            })

        return JSONResponse({"status": "ok", "active_workspace_id": clean_ws})

    _handle_workspace_switch = _handle_workspaces_switch
