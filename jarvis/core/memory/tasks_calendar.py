"""Tasks, reminders, calendar lifecycle, and archived schedule document management."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("Jarvis.Memory.TasksCalendar")


class TasksCalendarMixin:
    """Provides task list operations, calendar event scheduling, and schedule query methods."""

    # Tasks & Reminders
    def add_task(self, description: str, due_date: str | None = None) -> str:
        """Adds a task or reminder serialized through SingleWriterActor."""
        clean_desc = description.strip()

        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (description, due_date, is_completed)
                VALUES (?, ?, 0)
            """, (clean_desc, due_date))
            return cursor.lastrowid

        task_id = self.execute_write(_write)
        return f"Task #{task_id} added: {description}"

    def get_pending_tasks(self) -> list[dict[str, Any]]:
        """Retrieves all incomplete tasks."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, description, due_date, created_at FROM tasks WHERE is_completed = 0")
            return [dict(row) for row in cursor.fetchall()]

    def complete_task(self, task_id: int) -> str:
        """Marks a task as completed serialized through SingleWriterActor."""
        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("UPDATE tasks SET is_completed = 1 WHERE id = ?", (task_id,))
            return cursor.rowcount

        rowcount = self.execute_write(_write)
        if rowcount > 0:
            return f"Task #{task_id} marked as completed."
        return f"Task #{task_id} not found."

    # Calendar Events
    def add_calendar_event(self, title: str, start_time: str, end_time: str | None = None, location: str | None = None) -> str:
        """Adds a calendar event with deduplication for identical title and start time."""
        clean_title = title.strip()
        clean_start = start_time.strip()

        def _write(conn: sqlite3.Connection) -> tuple[int, bool]:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM calendar_events
                WHERE is_cancelled = 0 AND LOWER(title) = ? AND start_time = ?
            """, (clean_title.lower(), clean_start))
            existing = cursor.fetchone()
            if existing:
                event_id = existing["id"]
                cursor.execute("""
                    UPDATE calendar_events
                    SET end_time = COALESCE(?, end_time),
                        location = COALESCE(?, location)
                    WHERE id = ?
                """, (end_time, location, event_id))
                return event_id, True

            cursor.execute("""
                INSERT INTO calendar_events (title, start_time, end_time, location, is_cancelled)
                VALUES (?, ?, ?, ?, 0)
            """, (clean_title, clean_start, end_time, location))
            return cursor.lastrowid, False

        event_id, is_updated = self.execute_write(_write)
        action_verb = "updated" if is_updated else "scheduled"
        loc_str = f" ({location})" if location else ""
        return f"Event #{event_id} {action_verb}: '{clean_title}' at {clean_start}{loc_str}." 

    def get_calendar_events(self, include_cancelled: bool = False) -> list[dict[str, Any]]:
        """Retrieves active calendar events."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT id, title, start_time, end_time, location, created_at FROM calendar_events WHERE is_cancelled = 0 ORDER BY start_time"
            if include_cancelled:
                query = "SELECT id, title, start_time, end_time, location, created_at, is_cancelled FROM calendar_events ORDER BY start_time"
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def cancel_calendar_event(self, event_id: Any = None, title: str | None = None) -> str:
        """Marks a calendar event as cancelled by numerical ID or by matching title."""
        # Normalize arguments if event_id is passed as title string or digits
        if event_id is not None:
            if isinstance(event_id, str):
                s_id = event_id.strip()
                if s_id.isdigit():
                    event_id = int(s_id)
                elif not title:
                    title = s_id
                    event_id = None
                else:
                    event_id = None

        def _write(conn: sqlite3.Connection) -> str:
            cursor = conn.cursor()
            if event_id is not None:
                cursor.execute("UPDATE calendar_events SET is_cancelled = 1 WHERE id = ?", (int(event_id),))
                if cursor.rowcount > 0:
                    return f"Event #{event_id} has been cancelled, Sir."
                return f"Event #{event_id} was not found."

            if title and title.strip():
                clean_title = title.strip()
                cursor.execute("""
                    SELECT id, title FROM calendar_events
                    WHERE is_cancelled = 0 AND LOWER(title) LIKE ?
                """, (f"%{clean_title.lower()}%",))
                rows = cursor.fetchall()
                if not rows:
                    return f"No active calendar event matching '{clean_title}' was found, Sir."

                cancelled_ids = [r["id"] for r in rows]
                cancelled_titles = [r["title"] for r in rows]
                placeholders = ",".join("?" * len(cancelled_ids))
                cursor.execute(f"UPDATE calendar_events SET is_cancelled = 1 WHERE id IN ({placeholders})", cancelled_ids)
                return f"Cancelled calendar event(s): {', '.join(cancelled_titles)}, Sir."

            # If neither provided, cancel the most recent or upcoming active event
            cursor.execute("SELECT id, title FROM calendar_events WHERE is_cancelled = 0 ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE calendar_events SET is_cancelled = 1 WHERE id = ?", (row["id"],))
                return f"Cancelled event '{row['title']}' (#{row['id']}), Sir."
            return "No active calendar events to cancel, Sir."

        return self.execute_write(_write)

    def get_saved_calendars(self) -> list[dict[str, Any]]:
        """Retrieves all calendar and schedule documents archived in memory."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, file_path, created_at
                FROM documents
                WHERE LOWER(category) IN ('schedule', 'syllabus')
                   OR LOWER(title) LIKE '%timetable%'
                   OR LOWER(title) LIKE '%calendar%'
                   OR LOWER(title) LIKE '%schedule%'
                   OR LOWER(title) LIKE '%emploi%'
                ORDER BY id DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def delete_saved_calendar(self, doc_id: int) -> bool:
        """Deletes a saved calendar document from knowledge memory serialized through SingleWriterActor."""
        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            return cursor.rowcount > 0

        return bool(self.execute_write(_write))

    def delete_calendar_event(self, event_id: int) -> bool:
        """Permanently deletes a calendar event by numerical ID serialized through SingleWriterActor."""
        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
            return cursor.rowcount > 0

        return bool(self.execute_write(_write))

    def clear_calendar_events(self) -> int:
        """Deletes all calendar events from the active database serialized through SingleWriterActor."""
        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM calendar_events")
            return cursor.rowcount

        return int(self.execute_write(_write))
