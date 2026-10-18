"""Test suite for Jarvis 2.0 Holographic Web HUD Server."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
import urllib.request
import json
import unittest
from jarvis.ui.server import JarvisUIService


class TestUIServer(unittest.TestCase):
    def test_ui_server_lifecycle(self):
        # Use ephemeral test port
        test_port = 20139
        service = JarvisUIService(orchestrator=None, host="127.0.0.1", port=test_port)

        try:
            service.start()
            time.sleep(0.8)

            # 1. Test index page
            req_index = urllib.request.Request(f"http://127.0.0.1:{test_port}/")
            with urllib.request.urlopen(req_index, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                content = resp.read().decode("utf-8")
                self.assertIn("J.A.R.V.I.S.", content)
                self.assertIn("reactorCanvas", content)

            # 2. Test status endpoint
            req_status = urllib.request.Request(f"http://127.0.0.1:{test_port}/api/status")
            with urllib.request.urlopen(req_status, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("battery", data)
                self.assertIn("gpu", data)
                self.assertIn("model", data)

            # 3. Test direct action endpoint (instant hardware volume execution)
            action_data = json.dumps({"action": "set_volume", "percent": 50}).encode("utf-8")
            req_action = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/api/action",
                data=action_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req_action, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(res.get("status"), "success")
                self.assertEqual(res.get("percent"), 50)

        finally:
            service.stop()
            time.sleep(0.3)


if __name__ == "__main__":
    test_ui_server_lifecycle()
    print("ALL UI SERVER TESTS PASSED CLEANLY!")
