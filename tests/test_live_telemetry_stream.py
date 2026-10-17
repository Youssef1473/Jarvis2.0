"""Verification of live continuous telemetry broadcast via WebSocket."""
import asyncio
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets
from jarvis.ui.server import JarvisUIService


async def verify_live_stream():
    service = JarvisUIService(host="127.0.0.1", port=20139)
    service.start()
    time.sleep(1.0)

    received_packets = []
    uri = "ws://127.0.0.1:20139/ws"
    async with websockets.connect(uri) as ws:
        # Collect for 3.5 seconds (expecting initial packet + 3 periodic 1-second broadcasts)
        t_end = time.time() + 3.5
        while time.time() < t_end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                data = json.loads(msg)
                if data.get("type") == "telemetry":
                    received_packets.append(data)
                    cpu = data.get("cpu")
                    ram_gb = data.get("ram_used_gb")
                    ram_pct = data.get("ram_pct")
                    ts = data.get("timestamp")
                    print(f"Received live telemetry packet: CPU={cpu}%, RAM={ram_gb}GB ({ram_pct}%) at {ts}")
            except asyncio.TimeoutError:
                break

    service.stop()
    print(f"\nTotal telemetry packets received dynamically: {len(received_packets)}")
    assert len(received_packets) >= 3, f"Expected at least 3 live packets, got {len(received_packets)}"
    print("LIVE DYNAMIC TELEMETRY VERIFIED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(verify_live_stream())
