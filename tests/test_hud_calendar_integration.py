"""Integration tests for HUD Calendar Matrix, Endpoints, and Saved Calendar Presentation."""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from starlette.requests import Request
from jarvis.ui.server import JarvisUIService
from jarvis.core.memory import MemoryManager
from jarvis.core.config_loader import load_config


class TestHUDCalendarIntegration(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.service = JarvisUIService()
        self.memory = MemoryManager(self.config)

    def test_api_calendar_endpoint_returns_events_saved_cals_and_grouped(self):
        """Verifies /api/calendar endpoint supplies structured events, saved calendar documents, and grouped days."""
        async def run_req():
            scope = {'type': 'http', 'method': 'GET', 'path': '/api/calendar', 'headers': []}
            req = Request(scope)
            res = await self.service._handle_calendar(req)
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.body)
            self.assertIn("events", data)
            self.assertIn("saved_calendars", data)
            self.assertIn("grouped", data)
            self.assertIsInstance(data["events"], list)
            self.assertIsInstance(data["saved_calendars"], list)
            self.assertIsInstance(data["grouped"], dict)

            # Check that registered classes exist
            if data["events"]:
                titles = [e["title"] for e in data["events"]]
                self.assertTrue(any("C++" in t or "MATH" in t for t in titles))

            # Check that saved calendar documents exist in memory
            if data["saved_calendars"]:
                cal_titles = [c["title"] for c in data["saved_calendars"]]
                self.assertTrue(any("Timetable" in t or "Syllabus" in t for t in cal_titles))

        asyncio.run(run_req())

    def test_hud_html_contains_calendar_elements(self):
        """Verifies hud.html contains the CALENDAR button, modal backdrop, week grid, and deletion elements."""
        hud_path = Path("jarvis/ui/templates/hud.html")
        if not hud_path.exists():
            hud_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        self.assertTrue(hud_path.exists())
        html = hud_path.read_text(encoding="utf-8")

        self.assertIn('id="btnCalendar"', html)
        self.assertIn('openCalendarModal()', html)
        self.assertIn('id="calendarModalBackdrop"', html)
        self.assertIn('id="calendarWeekGrid"', html)
        self.assertIn('id="savedCalendarsPills"', html)
        self.assertIn('id="activeCalTitle"', html)
        self.assertIn('id="activeCalSummary"', html)
        # Deletion UI elements
        self.assertIn('promptDeleteCalendar', html)
        self.assertIn('executeDeleteCalendar', html)
        self.assertIn('deleteCalendarEvent', html)
        self.assertIn('id="calConfirmBackdrop"', html)
        self.assertIn('id="btnDeleteCalendar"', html)
        self.assertIn('cal-pill-del-btn', html)
        self.assertIn('cal-card-del-btn', html)

    def test_memory_calendar_deletion_methods(self):
        """Verifies MemoryManager calendar and event deletion methods."""
        import tempfile
        import gc
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            tmp_config = {
                "memory": {"db_path": os.path.join(tmpdir, "test_mem.db")},
                "_base_dir": tmpdir
            }
            tmp_mem = MemoryManager(tmp_config)

            # 1. Test delete_saved_calendar
            doc_id = tmp_mem.save_document_knowledge(
                title="Test College Timetable",
                category="schedule",
                summary="summary",
                file_path="timetable.pdf"
            )

            cals = tmp_mem.get_saved_calendars()
            self.assertEqual(len(cals), 1)
            self.assertEqual(cals[0]["id"], doc_id)

            success = tmp_mem.delete_saved_calendar(doc_id)
            self.assertTrue(success)
            self.assertEqual(len(tmp_mem.get_saved_calendars()), 0)

            # Deleting non-existent returns False
            self.assertFalse(tmp_mem.delete_saved_calendar(99999))

            # 2. Test delete_calendar_event
            tmp_mem.add_calendar_event("PROJET C++", "2026-10-12T09:00:00", "2026-10-12T12:15:00", "G011")
            events = tmp_mem.get_calendar_events()
            self.assertEqual(len(events), 1)
            event_id = events[0]["id"]

            success = tmp_mem.delete_calendar_event(event_id)
            self.assertTrue(success)
            self.assertEqual(len(tmp_mem.get_calendar_events()), 0)
            self.assertFalse(tmp_mem.delete_calendar_event(99999))

            # 3. Test clear_calendar_events
            for i in range(3):
                tmp_mem.add_calendar_event(f"Class {i}", f"2026-10-1{i}T09:00:00", f"2026-10-1{i}T12:00:00", "H311")

            self.assertEqual(len(tmp_mem.get_calendar_events()), 3)
            cleared_count = tmp_mem.clear_calendar_events()
            self.assertEqual(cleared_count, 3)
            self.assertEqual(len(tmp_mem.get_calendar_events()), 0)

            del tmp_mem
            gc.collect()

    def test_api_calendar_delete_endpoints(self):
        """Verifies Starlette API routes for calendar and event deletion."""
        import tempfile
        import gc
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            tmp_config = {
                "memory": {"db_path": os.path.join(tmpdir, "test_ui_mem.db")},
                "_base_dir": tmpdir
            }
            tmp_mem = MemoryManager(tmp_config)

            # Insert sample document and events
            doc_id = tmp_mem.save_document_knowledge(
                title="Test Semester Timetable",
                category="schedule",
                summary="summary",
                file_path="timetable.pdf"
            )
            tmp_mem.add_calendar_event("MATH 3", "2026-10-12T13:30:00", "2026-10-12T16:45:00", "H311")
            events = tmp_mem.get_calendar_events()
            event_id = events[0]["id"]

            # Attach mock orchestrator with memory to service
            mock_orch = MagicMock()
            mock_orch.memory = tmp_mem
            self.service.orchestrator = mock_orch

            async def run_tests():
                # Helper to create JSON POST request
                def make_post_request(path: str, payload: dict):
                    body_bytes = json.dumps(payload).encode('utf-8')
                    scope = {
                        'type': 'http',
                        'method': 'POST',
                        'path': path,
                        'headers': [(b'content-type', b'application/json')],
                    }
                    async def receive():
                        return {'type': 'http.request', 'body': body_bytes, 'more_body': False}
                    return Request(scope, receive)

                # 1. Delete Event
                req_event = make_post_request('/api/calendar/event/delete', {'event_id': event_id})
                res_event = await self.service._handle_calendar_event_delete(req_event)
                self.assertEqual(res_event.status_code, 200)
                data_event = json.loads(res_event.body)
                self.assertEqual(data_event["status"], "deleted")
                self.assertEqual(data_event["event_id"], event_id)

                # 2. Delete Calendar Document with clear_events
                req_doc = make_post_request('/api/calendar/delete', {'doc_id': doc_id, 'clear_events': True})
                res_doc = await self.service._handle_calendar_delete(req_doc)
                self.assertEqual(res_doc.status_code, 200)
                data_doc = json.loads(res_doc.body)
                self.assertEqual(data_doc["status"], "deleted")
                self.assertEqual(data_doc["doc_id"], doc_id)

                # 3. Clear Calendar
                req_clear = make_post_request('/api/calendar/clear', {})
                res_clear = await self.service._handle_calendar_clear(req_clear)
                self.assertEqual(res_clear.status_code, 200)
                data_clear = json.loads(res_clear.body)
                self.assertEqual(data_clear["status"], "cleared")

            asyncio.run(run_tests())
            del tmp_mem
            gc.collect()

    def test_calendar_manager_skill_deletion(self):
        """Verifies jarvis.skills.calendar_manager delete_calendar function."""
        import tempfile
        import gc
        from unittest.mock import patch
        from jarvis.skills.calendar_manager import delete_calendar

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            tmp_config = {
                "memory": {"db_path": os.path.join(tmpdir, "test_skill_mem.db")},
                "_base_dir": tmpdir
            }
            tmp_mem = MemoryManager(tmp_config)

            doc_id = tmp_mem.save_document_knowledge(
                title="Timetable Fall 2026",
                category="schedule",
                summary="summary",
                file_path="fall.pdf"
            )

            with patch("jarvis.skills.calendar_manager._get_memory", return_value=tmp_mem):
                # Delete by name match
                result = delete_calendar(calendar_name="Fall 2026")
                self.assertIn("deleted", result.lower())
                self.assertIn(str(doc_id), result)

                # Verify deletion
                self.assertEqual(len(tmp_mem.get_saved_calendars()), 0)

                # Delete non-existent
                result_missing = delete_calendar(calendar_name="Nonexistent Calendar")
                self.assertIn("could not locate", result_missing.lower())

            del tmp_mem
            gc.collect()


if __name__ == "__main__":
    unittest.main()

