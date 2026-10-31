"""tests/test_ui_concurrency_lifecycle.py — UI Service Deterministic Lifecycle & Non-Blocking Async Offloading Tests."""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

from jarvis.ui.server import JarvisUIService


class TestUIServiceLifecycle(unittest.TestCase):
    """Verifies deterministic ready/stopped event synchronization and non-competing thread lifecycles."""

    def test_ready_and_stopped_events_initialized(self) -> None:
        service = JarvisUIService(host="127.0.0.1", port=20129)
        self.assertIsInstance(service._ready_event, threading.Event)
        self.assertIsInstance(service._stopped_event, threading.Event)
        self.assertFalse(service._ready_event.is_set())
        self.assertFalse(service._stopped_event.is_set())

    @patch("uvicorn.Server.serve", new_callable=AsyncMock)
    def test_start_signals_ready_event(self, mock_serve: AsyncMock) -> None:
        service = JarvisUIService(host="127.0.0.1", port=20199)

        # Mock server started and running until should_exit is signaled
        async def fake_serve() -> None:
            service._server.started = True
            while not getattr(service._server, "should_exit", False):
                await asyncio.sleep(0.01)

        mock_serve.side_effect = fake_serve

        # Start service
        ready = service.start(timeout=2.0)
        self.assertTrue(ready)
        self.assertTrue(service._ready_event.is_set())

        # Calling start again while running returns True immediately
        ready_second = service.start(timeout=1.0)
        self.assertTrue(ready_second)

        # Stop service
        stopped = service.stop(timeout=2.0)
        self.assertTrue(stopped)
        self.assertTrue(service._stopped_event.is_set())
        self.assertIsNone(service._thread)
        self.assertIsNone(service._server)


class TestUIMemoryAsyncOffloading(unittest.IsolatedAsyncioTestCase):
    """Verifies that SQLite WAL memory nodes, deletion, and updates are offloaded via anyio.to_thread without blocking."""

    async def test_handle_memory_nodes_offloaded_via_anyio(self) -> None:
        service = JarvisUIService(host="127.0.0.1", port=20129)
        mock_mem = MagicMock()
        mock_mem.get_all_memory_nodes.return_value = {
            "total": 1,
            "counts": {"facts": 1, "preferences": 0, "documents": 0, "lessons": 0},
            "facts": [{"id": 1, "key": "favorite_editor", "value": "VS Code"}],
            "preferences": [],
            "documents": [],
            "lessons": [],
        }

        mock_orch = MagicMock()
        mock_orch.memory = mock_mem
        service.orchestrator = mock_orch

        # Build dummy Starlette Request
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/memory/nodes",
            "headers": [],
            "query_string": b"q=test&limit=10",
        }
        req = Request(scope)

        with patch("anyio.to_thread.run_sync", wraps=asyncio.to_thread) as mock_run_sync:
            resp = await service._handle_memory_nodes(req)
            self.assertEqual(resp.status_code, 200)
            # Verify anyio.to_thread.run_sync was called
            self.assertTrue(mock_run_sync.called)

    async def test_handle_memory_delete_offloaded_via_anyio(self) -> None:
        service = JarvisUIService(host="127.0.0.1", port=20129)
        mock_mem = MagicMock()
        mock_mem.delete_memory_node.return_value = True

        mock_orch = MagicMock()
        mock_orch.memory = mock_mem
        service.orchestrator = mock_orch

        async def fake_receive() -> dict:
            import json
            return {
                "type": "http.request",
                "body": json.dumps({"type": "fact", "id": 42}).encode("utf-8"),
                "more_body": False,
            }

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/memory/delete",
            "headers": [(b"content-type", b"application/json")],
        }
        req = Request(scope, receive=fake_receive)

        with patch("anyio.to_thread.run_sync", wraps=asyncio.to_thread) as mock_run_sync:
            resp = await service._handle_memory_delete(req)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(mock_run_sync.called)

    async def test_handle_memory_update_offloaded_via_anyio(self) -> None:
        service = JarvisUIService(host="127.0.0.1", port=20129)
        mock_mem = MagicMock()
        mock_mem.remember_fact.return_value = "Remembered fact"

        mock_orch = MagicMock()
        mock_orch.memory = mock_mem
        service.orchestrator = mock_orch

        async def fake_receive() -> dict:
            import json
            return {
                "type": "http.request",
                "body": json.dumps({"type": "fact", "key": "city", "value": "Cairo"}).encode("utf-8"),
                "more_body": False,
            }

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/memory/update",
            "headers": [(b"content-type", b"application/json")],
        }
        req = Request(scope, receive=fake_receive)

        with patch("anyio.to_thread.run_sync", wraps=asyncio.to_thread) as mock_run_sync:
            resp = await service._handle_memory_update(req)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(mock_run_sync.called)


if __name__ == "__main__":
    unittest.main()
