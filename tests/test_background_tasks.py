"""Unit tests for asynchronous BackgroundTaskManager, shell skills, and HUD endpoints."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from jarvis.core.task_manager import BackgroundTaskManager, BackgroundTask
from jarvis.skills.base import SkillRegistry
import jarvis.skills.dev_shell as dev_shell


class TestBackgroundTasksSuite(unittest.TestCase):
    """Verifies non-blocking task execution, pipe streaming, cancellation, and API endpoints."""

    def setUp(self):
        self.orchestrator = MagicMock()
        self.mgr = BackgroundTaskManager(self.orchestrator)
        # Point singleton instance to test manager
        BackgroundTaskManager._instance = self.mgr

    def tearDown(self):
        if hasattr(self, "mgr"):
            self.mgr.stop_all(timeout=0.5)

    def test_skills_registered(self):
        """Verifies background task skills are registered in SkillRegistry."""
        tools = SkillRegistry.get_all_tools()
        self.assertIn("run_background_task", tools)
        self.assertIn("list_background_tasks", tools)
        self.assertIn("get_background_task_output", tools)
        self.assertIn("cancel_background_task", tools)

    def test_run_background_task_success(self):
        """Verifies a command runs asynchronously and records stdout and exit code 0."""
        success, msg, task_id = self.mgr.run_task(
            command="Write-Output 'Alpha-Bravo-Charlie'",
            task_name="Echo Test",
            notify_voice=False,
        )
        self.assertTrue(success)
        self.assertIn("Echo Test", msg)
        self.assertIsNotNone(task_id)

        # Wait up to 6.0s for completion under high test runner CPU contention
        start_wait = time.time()
        while time.time() - start_wait < 6.0:
            task = self.mgr._tasks.get(task_id)
            if task and task.status in ("completed", "failed"):
                break
            time.sleep(0.05)

        task = self.mgr._tasks.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.exit_code, 0)
        self.assertTrue(any("Alpha-Bravo-Charlie" in line for line in task.stdout_lines))

    def test_run_background_task_failure_exit_code(self):
        """Verifies failed commands record non-zero exit code and failed status."""
        success, msg, task_id = self.mgr.run_task(
            command="exit 17",
            task_name="Exit 17 Test",
            notify_voice=False,
        )
        self.assertTrue(success)

        start_wait = time.time()
        while time.time() - start_wait < 6.0:
            task = self.mgr._tasks.get(task_id)
            if task and task.status in ("completed", "failed"):
                break
            time.sleep(0.05)

        task = self.mgr._tasks.get(task_id)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.exit_code, 17)

    def test_safety_gate_blocks_destructive_command(self):
        """Verifies SafetyGate blocks dangerous commands before execution."""
        success, msg, task_id = self.mgr.run_task(
            command="Remove-Item -Recurse -Force C:\\Windows",
            task_name="Dangerous Command",
        )
        self.assertFalse(success)
        self.assertIn("blocked by SafetyGate", msg)
        self.assertIsNone(task_id)

    def test_cancel_running_task(self):
        """Verifies active tasks can be terminated cleanly on demand."""
        success, msg, task_id = self.mgr.run_task(
            command="Start-Sleep -Seconds 10",
            task_name="Long Sleep",
            notify_voice=False,
        )
        self.assertTrue(success)
        time.sleep(0.1)

        cancelled = self.mgr.cancel_task(task_id)
        self.assertTrue(cancelled)

        task = self.mgr._tasks.get(task_id)
        self.assertEqual(task.status, "cancelled")

    def test_skill_wrappers_execution(self):
        """Verifies dev_shell skill wrappers interact with BackgroundTaskManager."""
        res = dev_shell.run_background_task("Write-Output 'Skill Test'", task_name="Wrapper Test", notify_voice=False)
        self.assertIn("Launched background task 'Wrapper Test'", res)

        list_out = dev_shell.list_background_tasks()
        self.assertIn("Wrapper Test", list_out)

        # Get task ID from list
        tasks = self.mgr.list_tasks()
        self.assertTrue(len(tasks) > 0)
        t_id = tasks[-1]["id"]

        time.sleep(0.5)
        out = dev_shell.get_background_task_output(t_id)
        self.assertIn("Output for Task", out)

        cancel_out = dev_shell.cancel_background_task("invalid_task_999")
        self.assertIn("Unable to cancel task", cancel_out)

    def test_server_screenshot_and_tasks_endpoints(self):
        """Verifies Starlette server endpoints /api/screenshot/latest and /api/tasks."""
        from jarvis.ui.server import JarvisUIService

        ui_service = JarvisUIService(orchestrator=self.orchestrator, host="127.0.0.1", port=20129)
        app = ui_service._create_app()
        client = TestClient(app)

        # 1. Test /api/tasks
        resp = client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("tasks", data)

        # 2. Test /api/screenshot/latest
        screenshots_dir = Path("data/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        test_screenshot = screenshots_dir / "latest_screen.jpg"
        test_screenshot.write_bytes(b"\xff\xd8\xff\xe0fakejpgbytes")

        try:
            resp_screen = client.get("/api/screenshot/latest")
            self.assertEqual(resp_screen.status_code, 200)
            self.assertEqual(resp_screen.headers.get("content-type"), "image/jpeg")
            self.assertEqual(resp_screen.content, b"\xff\xd8\xff\xe0fakejpgbytes")
        finally:
            if test_screenshot.exists():
                test_screenshot.unlink()


if __name__ == "__main__":
    unittest.main()
