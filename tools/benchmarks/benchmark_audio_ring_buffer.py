"""Reproducible benchmark for AudioRingBuffer and zero-allocation DSP operations."""
from __future__ import annotations

import gc
import sys
import time
import tracemalloc
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np

from jarvis.audio.ring_buffer import (
    AudioRingBuffer,
    apply_cinematic_jarvis_dsp,
    apply_zero_crossing_cos2_rolloff,
)


def run_benchmark():
    print("=" * 70)
    print("BENCHMARK: AudioRingBuffer & DSP Latency and Heap Allocations")
    print("=" * 70)

    # 1. Zero Allocation Verification during read_into()
    ring = AudioRingBuffer(capacity_samples=24000 * 10, ramp_samples=60)
    chunk_size = 480  # 20ms at 24kHz (100Hz WASAPI audio period)
    input_pcm = np.random.uniform(-0.5, 0.5, chunk_size).astype(np.float32)
    output_pcm = np.zeros(chunk_size, dtype=np.float32)

    # Pre-warm
    for _ in range(50):
        ring.write(input_pcm)
        ring.read_into(output_pcm)

    gc.collect()
    tracemalloc.start()
    snapshot_before = tracemalloc.take_snapshot()

    iterations = 1000
    for _ in range(iterations):
        ring.write(input_pcm)
        ring.read_into(output_pcm)

    snapshot_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    stats = snapshot_after.compare_to(snapshot_before, "lineno")
    total_diff_kb = sum(stat.size_diff for stat in stats) / 1024.0

    print(f"[*] Heap Allocation over {iterations} read_into() loops: {total_diff_kb:.2f} KB")

    # 2. Barge-In Invalidation Latency
    barge_iterations = 10000
    t0 = time.perf_counter_ns()
    for _ in range(barge_iterations):
        ring.barge_in()
    t1 = time.perf_counter_ns()
    avg_barge_in_us = (t1 - t0) / barge_iterations / 1000.0

    print(f"[*] Barge-In Invalidation Latency: {avg_barge_in_us:.4f} µs ({avg_barge_in_us * 1000:.1f} ns)")

    # 3. Read/Write Throughput & Latency
    rw_iterations = 5000
    write_latencies = []
    read_latencies = []

    for _ in range(rw_iterations):
        s0 = time.perf_counter_ns()
        ring.write(input_pcm)
        s1 = time.perf_counter_ns()
        write_latencies.append((s1 - s0) / 1000.0)

        s2 = time.perf_counter_ns()
        ring.read_into(output_pcm)
        s3 = time.perf_counter_ns()
        read_latencies.append((s3 - s2) / 1000.0)

    p50_write = np.percentile(write_latencies, 50)
    p95_write = np.percentile(write_latencies, 95)
    p50_read = np.percentile(read_latencies, 50)
    p95_read = np.percentile(read_latencies, 95)

    print(f"[*] PCM Write (480 samples / 20ms): P50 = {p50_write:.2f} µs | P95 = {p95_write:.2f} µs")
    print(f"[*] PCM Read Into (480 samples):   P50 = {p50_read:.2f} µs | P95 = {p95_read:.2f} µs")

    # 4. Zero-Crossing cos^2 Anti-Click Roll-off Latency
    fade_buf = np.zeros(chunk_size, dtype=np.float32)
    fade_latencies = []
    for _ in range(1000):
        s0 = time.perf_counter_ns()
        apply_zero_crossing_cos2_rolloff(input_pcm, fade_len=60, max_zc_search=48, out=fade_buf)
        s1 = time.perf_counter_ns()
        fade_latencies.append((s1 - s0) / 1000.0)

    p50_fade = np.percentile(fade_latencies, 50)
    p95_fade = np.percentile(fade_latencies, 95)
    print(f"[*] Zero-Crossing cos^2 Roll-Off:   P50 = {p50_fade:.2f} µs | P95 = {p95_fade:.2f} µs")

    # 5. Cinematic MCU Post-Processing Chain Latency
    try:
        import scipy  # noqa: F401
        has_scipy = True
    except ImportError:
        has_scipy = False

    if has_scipy:
        dsp_latencies = []
        dsp_chunk = np.random.uniform(-0.5, 0.5, 24000).astype(np.float32)  # 1 second of audio
        # Warm up filter coefficients cache
        apply_cinematic_jarvis_dsp(dsp_chunk[:1000])

        for _ in range(200):
            s0 = time.perf_counter_ns()
            apply_cinematic_jarvis_dsp(dsp_chunk)
            s1 = time.perf_counter_ns()
            dsp_latencies.append((s1 - s0) / 1000.0)

        p50_dsp = np.percentile(dsp_latencies, 50)
        p95_dsp = np.percentile(dsp_latencies, 95)
        print(f"[*] Cinematic DSP (1.0s audio):     P50 = {p50_dsp:.2f} µs ({p50_dsp/1000:.2f} ms) | P95 = {p95_dsp:.2f} µs ({p95_dsp/1000:.2f} ms)")
    else:
        print("[*] Cinematic DSP:                  Skipped (scipy optional dependency not installed)")
    print("=" * 70)


if __name__ == "__main__":
    run_benchmark()
