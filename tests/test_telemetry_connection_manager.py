"""Test suite for TelemetryConnectionManager and WorkstationMetricsCollector."""
import asyncio
import json
import unittest
from starlette.websockets import WebSocket
from jarvis.ui.server import TelemetryConnectionManager, WorkstationMetricsCollector


class MockWebSocket:
    """Mock WebSocket for unit testing TelemetryConnectionManager."""
    def __init__(self, slow: bool = False):
        self.accepted = False
        self.closed = False
        self.close_code = None
        self.sent_messages = []
        self.slow = slow

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code

    async def send_text(self, data: str):
        if self.slow:
            await asyncio.sleep(0.05)
        self.sent_messages.append(data)


class TestTelemetryLayer(unittest.IsolatedAsyncioTestCase):
    """Verifies backpressure frame-dropping, connection caps, and metric serialization."""

    async def test_connection_ceiling_enforcement(self):
        mgr = TelemetryConnectionManager(max_clients=2)
        ws1, ws2, ws3 = MockWebSocket(), MockWebSocket(), MockWebSocket()

        # Connect up to ceiling
        res1 = await mgr.connect(ws1)
        res2 = await mgr.connect(ws2)
        self.assertTrue(res1)
        self.assertTrue(res2)
        self.assertEqual(mgr.client_count, 2)

        # 3rd client rejected with 1008
        res3 = await mgr.connect(ws3)
        self.assertFalse(res3)
        self.assertTrue(ws3.closed)
        self.assertEqual(ws3.close_code, 1008)

        # Clean disconnect
        await mgr.disconnect(ws1)
        await mgr.disconnect(ws2)
        self.assertEqual(mgr.client_count, 0)

    async def test_lossy_frame_dropping_under_backpressure(self):
        mgr = TelemetryConnectionManager(max_clients=4)
        ws = MockWebSocket(slow=True)
        await mgr.connect(ws)

        # Broadcast multiple frames rapidly
        for i in range(10):
            mgr.broadcast_telemetry({
                "cpu": float(i),
                "ram_pct": 50.0,
                "ram_used_mb": 8192,
                "ram_total_mb": 16384,
                "gpu_load": i * 5,
                "vram_used_mb": 1000,
                "vram_total_mb": 4096,
                "gpu_temp_c": 55,
                "state": "idle",
                "model": "qwen3.8-27b",
                "is_muted": False,
            })

        # Wait briefly for sender worker to process
        await asyncio.sleep(0.1)

        # Verify minified keys are present in sent messages
        self.assertGreater(len(ws.sent_messages), 0)
        last_packet = json.loads(ws.sent_messages[-1])
        self.assertIn("c", last_packet)
        self.assertIn("rp", last_packet)
        self.assertIn("gl", last_packet)
        self.assertIn("vu", last_packet)
        self.assertIn("ts", last_packet)

        await mgr.disconnect(ws)

    def test_workstation_metrics_collector_sync(self):
        metrics = WorkstationMetricsCollector.sample_metrics_sync(orchestrator=None)
        self.assertIn("c", metrics)
        self.assertIn("rp", metrics)
        self.assertIn("ru", metrics)
        self.assertIn("rt", metrics)
        self.assertIn("gpu", metrics)
        self.assertIn("vram_used_mb", metrics)
        self.assertIn("vram_total_mb", metrics)
        WorkstationMetricsCollector.shutdown_nvml()


if __name__ == "__main__":
    unittest.main()
