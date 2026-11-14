"""Calendar integration, event grouping, and schedule mutation routes for Jarvis 2.0 HUD."""
from __future__ import annotations

import datetime
import logging
import re
from collections import defaultdict
from typing import Any

from starlette.responses import JSONResponse

logger = logging.getLogger("Jarvis.UI.Calendar")


class CalendarRoutesMixin:
    """Mixin providing calendar query, delete, and clear endpoints."""

    orchestrator: Any

    async def _handle_calendar(self, request: Any) -> JSONResponse:
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
                    except (ValueError, OSError):
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
                "grouped": dict(grouped),
            })
        except Exception as e:  # noqa: BLE001 - Error boundary prevents HUD crash
            logger.error("Error handling calendar request: %s", e)
            return JSONResponse({"events": [], "saved_calendars": [], "grouped": {}, "error": str(e)})

    async def _handle_calendar_delete(self, request: Any) -> JSONResponse:
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

            if hasattr(self, "broadcast"):
                self.broadcast({
                    "type": "calendar_deleted",
                    "doc_id": doc_id,
                    "title": doc_title,
                    "events_cleared": events_cleared,
                    "message": f"Calendar '{doc_title}' deleted successfully, Sir.",
                })

            return JSONResponse({
                "status": "deleted" if success else "not_found",
                "doc_id": doc_id,
                "title": doc_title,
                "events_cleared": events_cleared,
                "message": f"Calendar '{doc_title}' has been deleted.",
            })
        except Exception as e:  # noqa: BLE001
            logger.error("Error deleting calendar: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_calendar_event_delete(self, request: Any) -> JSONResponse:
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

            if hasattr(self, "broadcast"):
                self.broadcast({
                    "type": "calendar_event_deleted",
                    "event_id": event_id,
                    "message": f"Calendar event #{event_id} deleted.",
                })

            return JSONResponse({
                "status": "deleted" if success else "not_found",
                "event_id": event_id,
            })
        except Exception as e:  # noqa: BLE001
            logger.error("Error deleting calendar event: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_calendar_clear(self, request: Any) -> JSONResponse:
        """Purges all active calendar events from memory."""
        try:
            mem = None
            if self.orchestrator and hasattr(self.orchestrator, "memory"):
                mem = self.orchestrator.memory
            else:
                from jarvis.skills.calendar_manager import _get_memory
                mem = _get_memory()

            count = mem.clear_calendar_events() if mem else 0

            if hasattr(self, "broadcast"):
                self.broadcast({
                    "type": "calendar_cleared",
                    "count": count,
                    "message": f"Active schedule cleared ({count} events purged), Sir.",
                })

            return JSONResponse({
                "status": "cleared",
                "count": count,
                "message": f"Cleared {count} calendar events.",
            })
        except Exception as e:  # noqa: BLE001
            logger.error("Error clearing calendar events: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
