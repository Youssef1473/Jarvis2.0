"""Focused regression tests for Option B targeted exception hardening.

Verifies:
1. Safe exception handling in WorkstationMetricsCollector vitals probes under realistic failure injection (including sqlite3.Error).
2. Closed-loop coroutine cleanup (coro.close()) when loop scheduling fails in JarvisUIService for direct and binary broadcasts.
3. Unconditional state reset in WorkstationMetricsCollector.shutdown_nvml() even under unexpected vendor exceptions.
4. Dead WebSocket cleanup in TelemetryConnectionManager on disconnect/socket errors.
5. Subscriber callback failure isolation in WinEventHookDaemon.
6. Path safety and error containment in photo resolution.
7. Explicit string representation, value, and equality contracts for CognitiveState and DeliveryModality.
"""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
import unittest
import warnings
from typing import Any
from unittest.mock import MagicMock, patch

from jarvis.core.cognition.proactive_evi import CognitiveState, DeliveryModality
from jarvis.ui.server import (
    JarvisUIService,
    TelemetryConnectionManager,
    WorkstationMetricsCollector,
    resolve_safe_photo_path,
)
from jarvis.vision.window_hook_daemon import WinEventHookDaemon


class MockFailingWebSocket:
    """Mock WebSocket that simulates disconnection during send_text."""

    def __init__(self, should_fail: bool = True) -> None:
        self.should_fail = should_fail
        self.sent_messages: list[str] = []

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def send_text(self, data: str) -> None:
        if self.should_fail:
            raise OSError("Simulated broken pipe / socket disconnect")
        self.sent_messages.append(data)


class TestTargetedExceptionHardening(unittest.IsolatedAsyncioTestCase):
    """Verifies hardened exception paths across UI server, telemetry, and window hooks."""

    def test_sample_metrics_sync_resilient_to_probe_exceptions(self) -> None:
        """Verifies orchestrator probe errors are caught safely with observable logging."""
        mock_orch = MagicMock()
        mock_orch.is_muted.side_effect = OSError("Audio endpoint device not found")
        mock_orch.state = "thinking"

        mock_brain = MagicMock()
        mock_brain.specialist_status.side_effect = TypeError("Invalid specialist config")
        mock_brain.get_latencies.side_effect = RuntimeError("Provider timeout")
        mock_brain.memory.get_all_facts.side_effect = RuntimeError("Database locked")
        mock_brain.provider = "hybrid"
        mock_orch.brain = mock_brain

        # Must not raise despite multiple probe failures
        metrics = WorkstationMetricsCollector.sample_metrics_sync(mock_orch)
        self.assertIn("c", metrics)
        self.assertIn("rp", metrics)
        self.assertEqual(metrics["is_muted"], False)
        self.assertEqual(metrics["state"], "thinking")
        self.assertEqual(metrics["latencies"], {"ollama": 0.0, "groq": 0.0, "gemini": 0.0})

    def test_sample_metrics_sync_sqlite_database_failure_does_not_abort_telemetry(self) -> None:
        """Verifies database failures (sqlite3.OperationalError) in memory probe do not fail telemetry collection."""
        mock_orch = MagicMock()
        mock_orch.state = "idle"
        mock_brain = MagicMock()
        mock_brain.memory.get_all_memory_nodes.side_effect = sqlite3.OperationalError("database is locked")
        mock_brain.provider = "hybrid"
        mock_orch.brain = mock_brain

        metrics = WorkstationMetricsCollector.sample_metrics_sync(mock_orch)
        self.assertIn("c", metrics)
        self.assertIn("rp", metrics)
        self.assertIn("gp", metrics)
        self.assertEqual(metrics["state"], "idle")
        self.assertEqual(metrics["memory_label"], "TIER 2 (ACTIVE)")

    def test_shutdown_nvml_always_resets_state_even_on_vendor_exception(self) -> None:
        """Verifies shutdown_nvml resets all internal handles even if driver raises custom vendor exception."""
        class MockVendorNVMLError(Exception):
            pass

        mock_module = MagicMock()
        mock_module.nvmlShutdown.side_effect = MockVendorNVMLError("NVML communication broken")

        with WorkstationMetricsCollector._nvml_lock:
            WorkstationMetricsCollector._nvml_initialized = True
            WorkstationMetricsCollector._device_handle = MagicMock()
            WorkstationMetricsCollector._nvml_module = mock_module

        # Must catch exception, log it, and unconditionally reset fields in finally block
        WorkstationMetricsCollector.shutdown_nvml()

        with WorkstationMetricsCollector._nvml_lock:
            self.assertFalse(WorkstationMetricsCollector._nvml_initialized)
            self.assertIsNone(WorkstationMetricsCollector._device_handle)
            self.assertIsNone(WorkstationMetricsCollector._nvml_module)

    def test_broadcast_direct_coroutine_closed_on_loop_failure(self) -> None:
        """Verifies created coroutines are closed (coro.close()) if loop scheduling raises RuntimeError."""
        service = JarvisUIService(host="127.0.0.1", port=20199)
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = True
        service._loop = mock_loop

        # Register dummy client to ensure client_count > 0 and avoid early return
        dummy_ws = MagicMock()
        service.telemetry_manager._clients[dummy_ws] = asyncio.Queue()

        captured_coro: Any = None

        def fake_run_coroutine_threadsafe(coro: Any, loop: Any) -> Any:
            nonlocal captured_coro
            captured_coro = coro
            raise RuntimeError("Event loop is closed")

        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            with patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coroutine_threadsafe) as mock_run:
                service.broadcast({"type": "ping"})

            mock_run.assert_called_once()
            self.assertIsNotNone(captured_coro)
            # Verify the coroutine was closed by the exception handler
            self.assertEqual(inspect.getcoroutinestate(captured_coro), inspect.CORO_CLOSED)

            # Ensure zero unawaited coroutine runtime warnings
            unawaited_warnings = [
                w for w in recorded_warnings if issubclass(w.category, RuntimeWarning) and "was never awaited" in str(w.message)
            ]
            self.assertEqual(len(unawaited_warnings), 0)

    def test_broadcast_binary_coroutine_closed_on_loop_failure(self) -> None:
        """Verifies binary frame coroutines are closed if loop scheduling fails."""
        service = JarvisUIService(host="127.0.0.1", port=20199)
        mock_loop = MagicMock()
        service._loop = mock_loop
        dummy_ws = MagicMock()
        service.telemetry_manager._clients[dummy_ws] = asyncio.Queue()

        captured_coro: Any = None

        def fake_run_coroutine_threadsafe(coro: Any, loop: Any) -> Any:
            nonlocal captured_coro
            captured_coro = coro
            raise RuntimeError("Event loop is closed")

        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            with patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coroutine_threadsafe) as mock_run:
                service.broadcast_dsp_frame(b"\x4A" * 68)

            mock_run.assert_called_once()
            self.assertIsNotNone(captured_coro)
            self.assertEqual(inspect.getcoroutinestate(captured_coro), inspect.CORO_CLOSED)

            unawaited_warnings = [
                w for w in recorded_warnings if issubclass(w.category, RuntimeWarning) and "was never awaited" in str(w.message)
            ]
            self.assertEqual(len(unawaited_warnings), 0)

    async def test_broadcast_direct_cleans_dead_client_on_socket_error(self) -> None:
        """Verifies dead clients are removed from subscriber pool when send_text raises OSError."""
        mgr = TelemetryConnectionManager(max_clients=4)
        ws_failing = MockFailingWebSocket(should_fail=True)
        ws_healthy = MockFailingWebSocket(should_fail=False)

        await mgr.connect(ws_failing)
        await mgr.connect(ws_healthy)
        self.assertEqual(mgr.client_count, 2)

        # Broadcast direct message
        await mgr.broadcast_direct('{"type":"ping"}')

        # Failing client should have been disconnected; healthy retained
        self.assertEqual(mgr.client_count, 1)
        self.assertNotIn(ws_failing, mgr.connected_clients)
        self.assertIn(ws_healthy, mgr.connected_clients)
        self.assertEqual(len(ws_healthy.sent_messages), 1)

    def test_window_hook_event_callback_isolates_listener_exceptions(self) -> None:
        """Verifies a failing subscriber listener does not halt other listeners or crash the pump."""
        daemon = WinEventHookDaemon()
        executed_second = []

        def failing_listener(hwnd: int, event: int, title: str) -> None:
            raise ValueError("Subscriber crashed on unexpected window title")

        def healthy_listener(hwnd: int, event: int, title: str) -> None:
            executed_second.append(True)

        daemon._listeners = [failing_listener, healthy_listener]

        # Invoke native C callback directly (objid=0, childid=0) with dummy window
        with patch.object(daemon, "_get_window_title", return_value="Test App"):
            daemon._event_callback(
                h_hook=1,
                event=3,
                hwnd=1234,
                id_object=0,
                id_child=0,
                id_thread=5,
                dwms_event_time=100,
            )

        self.assertEqual(executed_second, [True])

    def test_resolve_safe_photo_path_path_traversal_defense(self) -> None:
        """Verifies path traversal attempts are safely rejected and return None."""
        self.assertIsNone(resolve_safe_photo_path("../../etc/passwd"))
        self.assertIsNone(resolve_safe_photo_path("..\\..\\Windows\\System32\\cmd.exe"))
        self.assertIsNone(resolve_safe_photo_path(""))

    def test_cognitive_state_and_delivery_modality_enum_contract(self) -> None:
        """Verifies exact string representation, .value, format, and equality contracts for CognitiveState and DeliveryModality."""
        self.assertEqual(str(CognitiveState.DEEP_FLOW), "CognitiveState.DEEP_FLOW")
        self.assertEqual(f"{CognitiveState.DEEP_FLOW}", "CognitiveState.DEEP_FLOW")
        self.assertEqual(CognitiveState.DEEP_FLOW.value, "DEEP_FLOW")
        self.assertEqual(CognitiveState.DEEP_FLOW, "DEEP_FLOW")

        self.assertEqual(str(DeliveryModality.VOICE), "DeliveryModality.VOICE")
        self.assertEqual(f"{DeliveryModality.VOICE}", "DeliveryModality.VOICE")
        self.assertEqual(DeliveryModality.VOICE.value, "VOICE")
        self.assertEqual(DeliveryModality.VOICE, "VOICE")


if __name__ == "__main__":
    unittest.main()
