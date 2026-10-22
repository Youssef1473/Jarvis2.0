"""Verification test suite for Jarvis UI Enhancements and Holographic HUD."""
import sys
from pathlib import Path
import unittest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.ui.server import JarvisUIService, get_hud_session_token


class DummyOrchestrator:
    def __init__(self):
        self._muted = False

    def is_muted(self):
        return self._muted

    def toggle_mute(self):
        self._muted = not self._muted
        return self._muted

    def handle_command(self, cmd):
        pass


class TestUIEnhancements(unittest.TestCase):
    def test_ui_telemetry_gathering(self):
        service = JarvisUIService(orchestrator=DummyOrchestrator())
        telemetry = service._gather_telemetry()

        self.assertIn("cpu", telemetry)
        self.assertIn("ram_pct", telemetry)
        self.assertIn("ram_used_gb", telemetry)
        self.assertIn("ram_total_gb", telemetry)
        self.assertIn("gpu", telemetry)
        self.assertIn("gpu_temp", telemetry)
        self.assertIn("battery", telemetry)
        self.assertIn("active_timers", telemetry)
        self.assertIn("tier", telemetry)

    def test_ui_endpoints(self):
        service = JarvisUIService(orchestrator=DummyOrchestrator())
        app = service._create_app()
        client = TestClient(app)
        token = get_hud_session_token()
        client.cookies.set("jarvis_session", token)
        client.headers["X-Jarvis-Token"] = token

        # 1. Test index HTML serving
        res_index = client.get("/")
        self.assertEqual(res_index.status_code, 200)
        html = res_index.text
        self.assertTrue("J.A.R.V.I.S. // 4.0" in html or "J.A.R.V.I.S. // 3.0" in html)
        self.assertTrue("TACTICAL CONTROL FLANK" in html or "TACTICAL ARCHIVE" in html)
        self.assertIn("hud-drawer", html)
        self.assertIn("cpuMeter", html)
        self.assertIn("ramMeter", html)
        self.assertIn("WORK", html)
        self.assertIn("BRIEFING", html)
        self.assertIn("LOCKDOWN", html)
        self.assertIn("OPTIMIZE", html)

        # 2. Test /api/status
        res_status = client.get("/api/status")
        self.assertEqual(res_status.status_code, 200)
        data_status = res_status.json()
        self.assertIn("cpu", data_status)
        self.assertIn("ram_pct", data_status)

        # 3. Test /api/notes
        res_notes = client.get("/api/notes")
        self.assertEqual(res_notes.status_code, 200)
        data_notes = res_notes.json()
        self.assertIn("notes", data_notes)

        # 4. Test /api/timers
        res_timers = client.get("/api/timers")
        self.assertEqual(res_timers.status_code, 200)
        data_timers = res_timers.json()
        self.assertIn("timers", data_timers)

        # 5. Test volume action
        res_act = client.post("/api/action", json={"action": "set_volume", "percent": 40})
        self.assertEqual(res_act.status_code, 200)
        self.assertEqual(res_act.json().get("percent"), 40)

        # 6. Test mute action
        res_mute = client.post("/api/action", json={"action": "toggle_mute"})
        self.assertEqual(res_mute.status_code, 200)
        self.assertIn("is_muted", res_mute.json())

        # 7. Test /api/memory/nodes
        res_mem = client.get("/api/memory/nodes")
        self.assertEqual(res_mem.status_code, 200)
        data_mem = res_mem.json()
        self.assertIn("total", data_mem)
        self.assertIn("counts", data_mem)
        self.assertIn("facts", data_mem)
        self.assertIn("preferences", data_mem)

        # 8. Test /api/memory/update & /api/memory/delete
        res_upd = client.post("/api/memory/update", json={
            "type": "preference",
            "key": "test_ui_theme",
            "value": "holographic_cyan"
        })
        self.assertEqual(res_upd.status_code, 200)
        self.assertEqual(res_upd.json().get("status"), "success")

        res_del = client.post("/api/memory/delete", json={
            "type": "preference",
            "id": "test_ui_theme"
        })
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.json().get("status"), "deleted")


if __name__ == "__main__":
    unittest.main()
