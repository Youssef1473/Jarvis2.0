"""Reproducible benchmark for StreamingToolCallParser throughput and speculative dispatch latency."""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np

from jarvis.core.streaming_tool_parser import StreamingToolCallParser


def run_benchmark():
    print("=" * 70)
    print("BENCHMARK: StreamingToolCallParser Latency & Throughput")
    print("=" * 70)

    # 1. Per-chunk feed latency
    dispatched = []
    parser = StreamingToolCallParser(
        idempotent_read_tools={"get_battery_status", "get_weather", "get_system_vitals"},
        dispatch_callback=lambda name, args: dispatched.append((name, args)),
    )

    chunks = [
        '{"name": ',
        '"get_battery_status", ',
        '"arguments": ',
        '{"detailed": ',
        'true',
        '}',
        '}',
    ]

    iterations = 2000
    chunk_latencies = []
    closing_latencies = []

    for i in range(iterations):
        parser.reset()
        call_id = f"bench_call_{i}"
        for chunk in chunks[:-1]:
            t0 = time.perf_counter_ns()
            parser.feed_chunk(chunk, call_id=call_id)
            t1 = time.perf_counter_ns()
            chunk_latencies.append((t1 - t0) / 1000.0)

        # Measure speculative early dispatch latency on the closing brace chunk
        t2 = time.perf_counter_ns()
        parser.feed_chunk(chunks[-1], call_id=call_id)
        t3 = time.perf_counter_ns()
        closing_latencies.append((t3 - t2) / 1000.0)

    p50_chunk = np.percentile(chunk_latencies, 50)
    p95_chunk = np.percentile(chunk_latencies, 95)
    p99_chunk = np.percentile(chunk_latencies, 99)

    p50_close = np.percentile(closing_latencies, 50)
    p95_close = np.percentile(closing_latencies, 95)
    p99_close = np.percentile(closing_latencies, 99)

    print(f"[*] Intermediate Chunk Feed Latency: P50 = {p50_chunk:.2f} µs | P95 = {p95_chunk:.2f} µs | P99 = {p99_chunk:.2f} µs")
    print(f"[*] Speculative Tool Dispatch Lat:   P50 = {p50_close:.2f} µs | P95 = {p95_close:.2f} µs | P99 = {p99_close:.2f} µs")
    print(f"[*] Dispatch Verification:          {len(dispatched)} tools successfully triggered")

    # 2. Interleaved Call Stream Isolation & Throughput
    parser.reset()
    dispatched.clear()

    interleaved_calls = 50
    t_start = time.perf_counter_ns()
    for c_idx in range(interleaved_calls):
        c_id = f"interleaved_{c_idx}"
        parser.feed_chunk('{"name": ', call_id=c_id)
        parser.feed_chunk('"get_weather", ', call_id=c_id)
        parser.feed_chunk(f'"arguments": {{"city_id": {c_idx}}}', call_id=c_id)
        parser.feed_chunk('}', call_id=c_id)

    t_end = time.perf_counter_ns()
    total_us = (t_end - t_start) / 1000.0
    throughput = (interleaved_calls * 4) / (total_us / 1000000.0)

    print(f"[*] Interleaved 50-Call Stream:      Completed in {total_us:.2f} µs ({throughput:.0f} chunks/sec)")
    print(f"[*] Interleaved Verification:        {len(dispatched)} of {interleaved_calls} tool dispatches succeeded")
    print("=" * 70)


if __name__ == "__main__":
    run_benchmark()
