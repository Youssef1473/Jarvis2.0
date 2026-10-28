"""Tests for Phase 5 Historical P99 Latency Telemetry.

Verifies:
1. Bounded historical sequence of rolling P99 snapshots in PipelineLatencyProfiler.
2. Chronological sample ordering and bounded FIFO memory retention.
3. Distinguishing rolling window values from historical trend sequence.
4. Missing stage data omitted / not misrepresented as zero-latency measurements.
5. Telemetry server and connection manager serialization and forwarding.
6. HUD template integration and empty/disconnected state elements.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
from jarvis.ui.server import TelemetryConnectionManager, WorkstationMetricsCollector


class MockWebSocket:
    """Mock WebSocket for unit testing TelemetryConnectionManager."""

    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.sent_messages: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code

    async def send_text(self, data: str) -> None:
        self.sent_messages.append(data)


class TestPhase5HistoricalP99Telemetry(unittest.TestCase):
    """Unit tests for PipelineLatencyProfiler historical P99 snapshots."""

    def test_p99_history_empty_initially(self) -> None:
        profiler = PipelineLatencyProfiler(window_size=16, history_limit=10)
        self.assertEqual(profiler.get_p99_history(), [])

    def test_p99_history_records_snapshot_on_complete_turn(self) -> None:
        profiler = PipelineLatencyProfiler(window_size=16, history_limit=10)
        base_ns = 1_000_000_000

        # Create a complete turn trace manually
        turn_id = "turn-001"
        trace = profiler.begin_turn(turn_id)
        trace.vad_onset_ns = base_ns
        trace.vad_end_ns = base_ns + 50_000_000
        trace.stt_final_ns = base_ns + 100_000_000
        trace.route_complete_ns = base_ns + 110_000_000
        trace.llm_first_token_ns = base_ns + 200_000_000
        trace.tts_first_chunk_ns = base_ns + 260_000_000
        trace.audio_first_frame_ns = base_ns + 280_000_000

        metrics = profiler.complete_turn(turn_id)
        self.assertIn("total_ttfa_ms", metrics)

        history = profiler.get_p99_history()
        self.assertEqual(len(history), 1)

        snapshot = history[0]
        self.assertIn("ts", snapshot)
        self.assertIn("turn_id", snapshot)
        self.assertEqual(snapshot["turn_id"], "turn-001")
        self.assertIn("stages", snapshot)
        stages = snapshot["stages"]
        self.assertIn("total_ttfa_ms", stages)
        self.assertAlmostEqual(stages["total_ttfa_ms"], 230.0, places=1)
        self.assertIn("ttft_ms", stages)
        self.assertAlmostEqual(stages["ttft_ms"], 100.0, places=1)

    def test_p99_history_enforces_bounded_capacity(self) -> None:
        limit = 5
        profiler = PipelineLatencyProfiler(window_size=16, history_limit=limit)
        base_ns = 1_000_000_000

        for i in range(12):
            turn_id = f"turn-{i:03d}"
            trace = profiler.begin_turn(turn_id)
            trace.vad_onset_ns = base_ns
            trace.vad_end_ns = base_ns + 10_000_000
            trace.stt_final_ns = base_ns + 30_000_000
            trace.llm_first_token_ns = base_ns + 60_000_000
            trace.audio_first_frame_ns = base_ns + 100_000_000
            profiler.complete_turn(turn_id)

        history = profiler.get_p99_history()
        self.assertEqual(len(history), limit)
        # Verify FIFO retention: should be the last 5 turns (turn-007 to turn-011)
        expected_turn_ids = [f"turn-{i:03d}" for i in range(7, 12)]
        actual_turn_ids = [s["turn_id"] for s in history]
        self.assertEqual(actual_turn_ids, expected_turn_ids)

    def test_p99_history_preserves_chronological_order(self) -> None:
        profiler = PipelineLatencyProfiler(window_size=16, history_limit=10)
        base_ns = 1_000_000_000

        for i in range(4):
            turn_id = f"turn-seq-{i}"
            trace = profiler.begin_turn(turn_id)
            trace.vad_onset_ns = base_ns
            trace.audio_first_frame_ns = base_ns + 50_000_000
            profiler.complete_turn(turn_id)

        history = profiler.get_p99_history()
        self.assertEqual(len(history), 4)
        for i in range(len(history) - 1):
            self.assertLessEqual(history[i]["ts"], history[i + 1]["ts"])

    def test_distinguish_rolling_window_from_historical_sequence(self) -> None:
        """Verifies historical sequence records progressive shifts in rolling P99."""
        window_size = 4
        profiler = PipelineLatencyProfiler(window_size=window_size, history_limit=20)
        base_ns = 1_000_000_000

        # Phase 1: 4 turns with fast 100ms latency
        for i in range(4):
            turn_id = f"fast-{i}"
            trace = profiler.begin_turn(turn_id)
            trace.vad_onset_ns = base_ns
            trace.vad_end_ns = base_ns + 10_000_000
            trace.stt_final_ns = base_ns + 30_000_000
            trace.audio_first_frame_ns = base_ns + 100_000_000  # TTFA = 90ms
            profiler.complete_turn(turn_id)

        # Snapshot at turn 4 should reflect fast latency
        history = profiler.get_p99_history()
        self.assertEqual(len(history), 4)
        fast_p99 = history[3]["stages"]["total_ttfa_ms"]
        self.assertAlmostEqual(fast_p99, 90.0, places=1)

        # Phase 2: 4 turns with slow 500ms latency
        for i in range(4):
            turn_id = f"slow-{i}"
            trace = profiler.begin_turn(turn_id)
            trace.vad_onset_ns = base_ns
            trace.vad_end_ns = base_ns + 10_000_000
            trace.stt_final_ns = base_ns + 30_000_000
            trace.audio_first_frame_ns = base_ns + 510_000_000  # TTFA = 500ms
            profiler.complete_turn(turn_id)

        history = profiler.get_p99_history()
        self.assertEqual(len(history), 8)
        slow_p99 = history[7]["stages"]["total_ttfa_ms"]
        self.assertAlmostEqual(slow_p99, 500.0, places=1)

        # The historical sequence distinguishes the low-latency baseline from the later spike
        self.assertLess(history[0]["stages"]["total_ttfa_ms"], history[7]["stages"]["total_ttfa_ms"])

    def test_missing_stage_data_not_represented_as_zero(self) -> None:
        """Verifies omitted stages are omitted from stages dict rather than plotted as 0.0ms."""
        profiler = PipelineLatencyProfiler(window_size=8, history_limit=5)
        base_ns = 1_000_000_000

        turn_id = "text-only-turn"
        trace = profiler.begin_turn(turn_id)
        trace.vad_onset_ns = base_ns
        trace.vad_end_ns = base_ns + 20_000_000
        trace.stt_final_ns = base_ns + 60_000_000
        trace.route_complete_ns = base_ns + 70_000_000
        trace.llm_first_token_ns = base_ns + 150_000_000
        # TTS and audio playback never triggered (e.g. reflex silence or non-vocal output)
        trace.tts_first_chunk_ns = 0
        trace.audio_first_frame_ns = 0

        profiler.complete_turn(turn_id)

        history = profiler.get_p99_history()
        self.assertEqual(len(history), 1)
        stages = history[0]["stages"]

        self.assertIn("stt_ms", stages)
        self.assertIn("routing_ms", stages)
        self.assertIn("ttft_ms", stages)
        # Missing audio stages MUST NOT be reported as 0.0ms
        self.assertNotIn("tts_synth_ms", stages)
        self.assertNotIn("dsp_ipc_ms", stages)


class TestPhase5TelemetryIntegration(unittest.IsolatedAsyncioTestCase):
    """Tests telemetry collector and WebSocket connection manager forwarding."""

    def test_workstation_metrics_collector_extracts_p99_history(self) -> None:
        profiler = PipelineLatencyProfiler(window_size=8, history_limit=5)
        base_ns = 1_000_000_000
        trace = profiler.begin_turn("turn-mock")
        trace.vad_onset_ns = base_ns
        trace.vad_end_ns = base_ns + 10_000_000
        trace.audio_first_frame_ns = base_ns + 110_000_000
        profiler.complete_turn("turn-mock")

        mock_brain = MagicMock()
        mock_brain.latency_profiler = profiler
        mock_brain.provider = "hybrid"
        mock_orchestrator = MagicMock()
        mock_orchestrator.brain = mock_brain
        mock_orchestrator.is_muted.return_value = False

        metrics = WorkstationMetricsCollector.sample_metrics_sync(mock_orchestrator)
        self.assertIn("p99_history", metrics)
        self.assertIn("p99_h", metrics)
        self.assertEqual(len(metrics["p99_history"]), 1)
        self.assertEqual(metrics["p99_history"][0]["turn_id"], "turn-mock")

    async def test_telemetry_connection_manager_broadcast_forwards_p99_history(self) -> None:
        mgr = TelemetryConnectionManager(max_clients=4)
        ws = MockWebSocket()
        await mgr.connect(ws)

        sample_history = [{"ts": 1729999990.0, "turn_id": "t1", "stages": {"total_ttfa_ms": 320.5}}]
        mgr.broadcast_telemetry({
            "cpu": 15.0,
            "ram_pct": 40.0,
            "ram_used_mb": 4096,
            "ram_total_mb": 16384,
            "gpu_load": 10,
            "vram_used_mb": 500,
            "vram_total_mb": 4096,
            "gpu_temp_c": 50,
            "state": "idle",
            "model": "qwen3.8-27b",
            "is_muted": False,
            "p99_history": sample_history,
        })

        await asyncio.sleep(0.05)
        self.assertGreater(len(ws.sent_messages), 0)
        last_packet = json.loads(ws.sent_messages[-1])
        self.assertIn("p99_history", last_packet)
        self.assertEqual(len(last_packet["p99_history"]), 1)
        self.assertEqual(last_packet["p99_history"][0]["stages"]["total_ttfa_ms"], 320.5)

        await mgr.disconnect(ws)

    def test_hud_template_contains_p99_elements_and_logic(self) -> None:
        hud_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        self.assertTrue(hud_path.is_file(), f"Missing HUD template at {hud_path}")
        content = hud_path.read_text(encoding="utf-8")

        # HTML canvas and state elements
        self.assertIn('id="p99LatencyCanvas"', content)
        self.assertIn('id="p99EmptyState"', content)
        self.assertIn('id="p99TtfaBadge"', content)

        # JavaScript telemetry handler keys
        self.assertTrue("p99_history" in content or "p99_h" in content)
        # Rendering function & disconnected/empty states
        self.assertIn("renderP99LatencyTrend", content)


if __name__ == "__main__":
    unittest.main()
