"""
Deterministic Nanosecond Stage-by-Stage Turn & TTFA Latency Profiler.
Tracks: Input -> Ingestion (STT) -> Routing -> Inference (TTFT) -> Execution -> Output (TTFA).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import time
from typing import Any


@dataclass(slots=True)
class TurnTrace:
    """Single-turn nanosecond timestamps across the voice-to-voice pipeline."""
    turn_id: str
    vad_onset_ns: int = 0
    vad_end_ns: int = 0
    stt_final_ns: int = 0
    route_complete_ns: int = 0
    llm_first_token_ns: int = 0
    tts_first_chunk_ns: int = 0
    audio_first_frame_ns: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def stage_durations_ms(self) -> dict[str, float]:
        """Computes per-stage and end-to-end TTFA durations in milliseconds."""
        base = self.vad_end_ns or self.vad_onset_ns
        if base <= 0:
            return {}

        def _delta_ms(start_ns: int, end_ns: int) -> float:
            if start_ns > 0 and end_ns >= start_ns:
                return round((end_ns - start_ns) / 1_000_000.0, 3)
            return 0.0

        stt_anchor = self.stt_final_ns or base
        token_anchor = self.llm_first_token_ns or stt_anchor
        tts_anchor = self.tts_first_chunk_ns or token_anchor

        return {
            "stt_ms": _delta_ms(base, self.stt_final_ns),
            "routing_ms": _delta_ms(stt_anchor, self.route_complete_ns),
            "ttft_ms": _delta_ms(stt_anchor, self.llm_first_token_ns),
            "tts_synth_ms": _delta_ms(token_anchor, self.tts_first_chunk_ns),
            "dsp_ipc_ms": _delta_ms(tts_anchor, self.audio_first_frame_ns),
            "total_ttfa_ms": _delta_ms(base, self.audio_first_frame_ns),
        }


class PipelineLatencyProfiler:
    """Bounded rolling histogram profiler for P50/P95/P99 real-time telemetry."""

    def __init__(self, window_size: int = 128) -> None:
        self._window_size = max(8, int(window_size))
        self._history: deque[dict[str, float]] = deque(maxlen=self._window_size)
        self._active_turns: dict[str, TurnTrace] = {}

    def begin_turn(self, turn_id: str) -> TurnTrace:
        now = time.perf_counter_ns()
        trace = TurnTrace(turn_id=turn_id, vad_onset_ns=now)
        self._active_turns[turn_id] = trace
        return trace

    def mark(self, turn_id: str, stage: str) -> None:
        trace = self._active_turns.get(turn_id)
        if trace is None:
            trace = TurnTrace(turn_id=turn_id, vad_onset_ns=time.perf_counter_ns())
            self._active_turns[turn_id] = trace

        now = time.perf_counter_ns()
        if hasattr(trace, stage):
            setattr(trace, stage, now)

    def complete_turn(self, turn_id: str) -> dict[str, float]:
        trace = self._active_turns.pop(turn_id, None)
        if trace is None:
            return {}
        if trace.audio_first_frame_ns == 0:
            trace.audio_first_frame_ns = time.perf_counter_ns()
        metrics = trace.stage_durations_ms()
        if metrics:
            self._history.append(metrics)
        return metrics

    def get_percentiles(self) -> dict[str, dict[str, float]]:
        """Returns P50, P95, and P99 in ms for each pipeline stage."""
        if not self._history:
            return {}

        keys = ("stt_ms", "routing_ms", "ttft_ms", "tts_synth_ms", "dsp_ipc_ms", "total_ttfa_ms")
        summary: dict[str, dict[str, float]] = {}

        for key in keys:
            samples = sorted(item[key] for item in self._history if item.get(key, 0.0) > 0.0)
            if not samples:
                continue
            n = len(samples)
            summary[key] = {
                "p50": samples[min(n - 1, int(n * 0.50))],
                "p95": samples[min(n - 1, int(n * 0.95))],
                "p99": samples[min(n - 1, int(n * 0.99))],
                "count": float(n),
            }
        return summary
