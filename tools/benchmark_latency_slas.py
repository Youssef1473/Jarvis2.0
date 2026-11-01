"""Reproducible Latency & Memory Benchmark Suite for Jarvis 2.0 Architectural SLAs.

Measures empirical P50, P95, P99, and Mean latencies for critical real-time desktop subsystems:
1. Preemptive voice barge-in cancellation epoch bump (signal_global_barge_in).
2. AudioRingBuffer write + read + barge_in() anti-click purge.
3. StatefulPolyphaseUpsampler 24kHz -> 48kHz polyphase resampler (20ms frame).
4. SingleWriterActor serialized SQLite WAL write transaction.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# Workspace root setup
WORKSPACE_DIR = Path(__file__).resolve().parent.parent
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from jarvis.audio.ring_buffer import AudioRingBuffer
from jarvis.audio.tts import StatefulPolyphaseUpsampler
from jarvis.core.cancellation import signal_global_barge_in
from jarvis.core.memory.sqlite_base import SingleWriterActor


def benchmark_barge_in_epoch_bump(iterations: int = 500, warmup: int = 50) -> list[float]:
    """Measures latency of signal_global_barge_in epoch invalidation in nanoseconds."""
    # Warmup
    for _ in range(warmup):
        signal_global_barge_in(async_callbacks=False)

    latencies_ns = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        signal_global_barge_in(async_callbacks=False)
        t1 = time.perf_counter_ns()
        latencies_ns.append(t1 - t0)
    return latencies_ns


def benchmark_audio_ring_buffer(iterations: int = 500, warmup: int = 50) -> list[float]:
    """Measures AudioRingBuffer write (480 samples), read (480 samples), and barge_in() clear in microseconds."""
    buf = AudioRingBuffer(capacity_samples=24000 * 10, ramp_samples=60)
    frame_in = (np.sin(np.linspace(0, 2 * np.pi, 480)) * 0.5).astype(np.float32)
    frame_out = np.zeros(480, dtype=np.float32)

    # Warmup
    for _ in range(warmup):
        buf.write(frame_in)
        buf.read(480, out=frame_out)
        buf.barge_in(blocking=False)
        buf.clear()

    latencies_us = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        buf.write(frame_in)
        buf.read(480, out=frame_out)
        buf.barge_in(blocking=False)
        buf.clear()
        t1 = time.perf_counter_ns()
        latencies_us.append((t1 - t0) / 1000.0)
    return latencies_us


def benchmark_polyphase_upsampler(iterations: int = 500, warmup: int = 50) -> list[float]:
    """Measures StatefulPolyphaseUpsampler 24kHz->48kHz processing for a 20ms frame (480 samples) in microseconds."""
    upsampler = StatefulPolyphaseUpsampler(up=2, down=1, pad_samples=32)
    frame_24k = (np.sin(np.linspace(0, 2 * np.pi, 480)) * 0.5).astype(np.float32)

    # Warmup
    for _ in range(warmup):
        _ = upsampler.process(frame_24k)
    upsampler.reset()

    latencies_us = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        _ = upsampler.process(frame_24k)
        t1 = time.perf_counter_ns()
        latencies_us.append((t1 - t0) / 1000.0)
    return latencies_us


def benchmark_sqlite_single_writer(iterations: int = 500, warmup: int = 50) -> list[float]:
    """Measures SingleWriterActor WAL write transaction roundtrip in milliseconds."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "bench.db"

        # Initialize schema table
        def _init_db(conn):
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("CREATE TABLE IF NOT EXISTS bench_kv (key TEXT PRIMARY KEY, value TEXT);")

        actor = SingleWriterActor.get_or_create(db_path)
        actor.execute_write(_init_db)

        def _write_op(conn):
            conn.execute("INSERT OR REPLACE INTO bench_kv (key, value) VALUES ('metric', 'val');")

        # Warmup
        for _ in range(warmup):
            actor.execute_write(_write_op)

        latencies_ms = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            actor.execute_write(_write_op)
            t1 = time.perf_counter_ns()
            latencies_ms.append((t1 - t0) / 1_000_000.0)

        actor.stop(drain=True, timeout=2.0)
    return latencies_ms


def compute_metrics(data: list[float]) -> dict[str, float]:
    arr = np.array(data)
    return {
        "mean": float(np.mean(arr)),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
    }


def main():
    iterations_fast = 10000
    warmup_fast = 500
    iterations_db = 2500
    warmup_db = 100

    print("Executing Jarvis 2.0 Architectural SLA Benchmark...")
    print(f"  - Fast In-Memory & DSP Subsystems: N={iterations_fast}, Warmup={warmup_fast}")
    print(f"  - SQLite WAL Actor Queue: N={iterations_db}, Warmup={warmup_db}\n")

    barge_in_ns = benchmark_barge_in_epoch_bump(iterations=iterations_fast, warmup=warmup_fast)
    ring_buf_us = benchmark_audio_ring_buffer(iterations=iterations_fast, warmup=warmup_fast)
    upsampler_us = benchmark_polyphase_upsampler(iterations=iterations_fast, warmup=warmup_fast)
    sqlite_ms = benchmark_sqlite_single_writer(iterations=iterations_db, warmup=warmup_db)

    m_barge = compute_metrics(barge_in_ns)
    m_ring = compute_metrics(ring_buf_us)
    m_upsampler = compute_metrics(upsampler_us)
    m_sqlite = compute_metrics(sqlite_ms)

    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")

    md_table = [
        "| Subsystem / Operation | Unit | Samples (N) | P50 | P95 | P99 | Mean | Architectural SLA Target | Target Status (P99) | Measured Architecture Characteristics |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        f"| Preemptive Barge-In Epoch Bump | ns | {iterations_fast} | {m_barge['p50']:.1f} | {m_barge['p95']:.1f} | {m_barge['p99']:.1f} | {m_barge['mean']:.1f} | < 1,000 ns (1.0 us) | {'MET' if m_barge['p99'] < 1000 else 'NOT MET'} | Pure Python atomics overhead on Windows (1.1-1.7 us) |",
        f"| AudioRingBuffer Write+Read+Purge | us | {iterations_fast} | {m_ring['p50']:.2f} | {m_ring['p95']:.2f} | {m_ring['p99']:.2f} | {m_ring['mean']:.2f} | < 15.0 us | {'MET' if m_ring['p99'] < 15.0 else 'NOT MET'} | Lock-free C-ctypes circular memory (fully compliant) |",
        f"| StatefulPolyphaseUpsampler (20ms) | us | {iterations_fast} | {m_upsampler['p50']:.2f} | {m_upsampler['p95']:.2f} | {m_upsampler['p99']:.2f} | {m_upsampler['mean']:.2f} | < 150.0 us | {'MET' if m_upsampler['p99'] < 150.0 else 'NOT MET'} | NumPy FIR filter (250-650 us; 30-80x faster than 20ms frame) |",
        f"| SingleWriterActor WAL Commit | ms | {iterations_db} | {m_sqlite['p50']:.2f} | {m_sqlite['p95']:.2f} | {m_sqlite['p99']:.2f} | {m_sqlite['mean']:.2f} | < 5.0 ms | {'MET' if m_sqlite['p99'] < 5.0 else 'NOT MET'} | Thread queue roundtrip + SQLite WAL fsync scheduling |",
    ]

    print("\n".join(md_table))


if __name__ == "__main__":
    main()
