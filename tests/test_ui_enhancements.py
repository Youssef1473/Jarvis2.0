"""Verification test suite for Jarvis UI Enhancements and Holographic HUD."""
import sys
from pathlib import Path
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.ui.server import JarvisUIService


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


def test_ui_telemetry_gathering():
    print("\n--- 1. Testing UI Telemetry Gathering ---")
    service = JarvisUIService(orchestrator=DummyOrchestrator())
    telemetry = service._gather_telemetry()
    print("Telemetry gathered keys:", list(telemetry.keys()))

    assert "cpu" in telemetry
    assert "ram_pct" in telemetry
    assert "ram_used_gb" in telemetry
    assert "ram_total_gb" in telemetry
    assert "gpu" in telemetry
    assert "gpu_temp" in telemetry
    assert "battery" in telemetry
    assert "active_timers" in telemetry
    assert "tier" in telemetry

    print(f"CPU: {telemetry['cpu']}%, RAM: {telemetry['ram_used_gb']}/{telemetry['ram_total_gb']} GB ({telemetry['ram_pct']}%)")
    print(f"GPU: {telemetry['gpu']} at {telemetry['gpu_temp']}°C, Battery: {telemetry['battery']}%")
    print(f"Tier: {telemetry['tier']}, Active Timers: {telemetry['active_timers']}")


def test_ui_endpoints():
    print("\n--- 2. Testing Starlette HTTP & REST Endpoints ---")
    service = JarvisUIService(orchestrator=DummyOrchestrator())
    app = service._create_app()
    client = TestClient(app)

    # 1. Test index HTML serving
    res_index = client.get("/")
    assert res_index.status_code == 200
    html = res_index.text
    assert ("J.A.R.V.I.S. // 4.0" in html or "J.A.R.V.I.S. // 3.0" in html)
    assert ("TACTICAL CONTROL FLANK" in html or "TACTICAL ARCHIVE" in html)
    assert "hud-drawer" in html
    assert "cpuMeter" in html
    assert "ramMeter" in html
    assert "WORK" in html
    assert "BRIEFING" in html
    assert "LOCKDOWN" in html
    assert "OPTIMIZE" in html
    print("Index HUD template rendered with all new components!")

    # 2. Test /api/status
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert "cpu" in data_status
    assert "ram_pct" in data_status
    print("GET /api/status returned valid JSON telemetry!")

    # 3. Test /api/notes
    res_notes = client.get("/api/notes")
    assert res_notes.status_code == 200
    data_notes = res_notes.json()
    assert "notes" in data_notes
    print(f"GET /api/notes returned {len(data_notes['notes'])} notes!")

    # 4. Test /api/timers
    res_timers = client.get("/api/timers")
    assert res_timers.status_code == 200
    data_timers = res_timers.json()
    assert "timers" in data_timers
    print(f"GET /api/timers returned {len(data_timers['timers'])} timers!")

    # 5. Test volume action
    res_act = client.post("/api/action", json={"action": "set_volume", "percent": 40})
    assert res_act.status_code == 200
    assert res_act.json().get("percent") == 40
    print("POST /api/action volume control verified!")

    # 6. Test mute action
    res_mute = client.post("/api/action", json={"action": "toggle_mute"})
    assert res_mute.status_code == 200
    assert "is_muted" in res_mute.json()
    print("POST /api/action toggle_mute verified!")

    # 7. Test /api/memory/nodes
    res_mem = client.get("/api/memory/nodes")
    assert res_mem.status_code == 200
    data_mem = res_mem.json()
    assert "total" in data_mem
    assert "counts" in data_mem
    assert "facts" in data_mem
    assert "preferences" in data_mem
    print(f"GET /api/memory/nodes returned {data_mem['total']} total nodes!")

    # 8. Test /api/memory/update & /api/memory/delete
    res_upd = client.post("/api/memory/update", json={
        "type": "preference",
        "key": "test_ui_theme",
        "value": "holographic_cyan"
    })
    assert res_upd.status_code == 200
    assert res_upd.json().get("status") == "success"
    print("POST /api/memory/update preference verified!")

    res_del = client.post("/api/memory/delete", json={
        "type": "preference",
        "id": "test_ui_theme"
    })
    assert res_del.status_code == 200
    assert res_del.json().get("status") == "deleted"
    print("POST /api/memory/delete preference verified!")

    print("\nALL UI ENHANCEMENT TESTS PASSED 100%!")


if __name__ == "__main__":
    test_ui_telemetry_gathering()
    test_ui_endpoints()
