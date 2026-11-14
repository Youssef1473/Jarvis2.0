"""Benchmark for Context Router Latency and Token Efficiency (ENG-OS-02 Phase 2B).

Measures:
- Classification time
- Context resolution time
- Serialization time
- End-to-end hook processing time
Reports median and p95 across repeated runs on varied prompts.
Estimates token overhead across prompt types.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.context_router import (
    classify_task,
    process_hook_input,
    resolve_context,
    serialize_hook_response,
)

SAMPLE_PROMPTS = [
    # Short
    "Fix WASAPI audio",
    # Single domain
    "Configure Win32 Job Object containment with CREATE_SUSPENDED and kill_on_job_close limit",
    # SQLite
    "Optimize SQLite SingleWriterActor transaction throughput with WAL pragma and reader pool",
    # Multi-subsystem
    "Refactor WASAPI audio playback thread and log events to SQLite database while checking DualKeyApprovalManager",
    # Architecture
    "Review subsystem boundaries according to master architectural blueprint and ADR index in docs/architecture/INDEX.md",
    # Fallback / Unknown
    "Explain the history of coffee cultivation in South America and its trade routes",
    # Long prompt
    "Investigate WASAPI playback worker underrun during Kokoro speech synthesis. " * 50,
]


def estimate_tokens(text: str) -> int:
    """Rough estimation of token count (~4 characters per token)."""
    return max(1, len(text) // 4)


def run_benchmark(iterations_per_prompt: int = 50) -> dict[str, Any]:
    classify_times = []
    resolve_times = []
    serialize_times = []
    e2e_times = []

    for prompt in SAMPLE_PROMPTS:
        for _ in range(iterations_per_prompt):
            # 1. E2E timing via process_hook_input
            stdin_json = json.dumps({"lastUserInput": prompt})
            t0 = time.perf_counter_ns()
            _ = process_hook_input(stdin_json)
            t1 = time.perf_counter_ns()
            e2e_times.append((t1 - t0) / 1_000_000.0)  # ms

            # 2. Stage-by-stage timing
            t_c0 = time.perf_counter_ns()
            classification = classify_task(prompt)
            t_c1 = time.perf_counter_ns()
            classify_times.append((t_c1 - t_c0) / 1_000_000.0)

            t_r0 = time.perf_counter_ns()
            packet = resolve_context(classification)
            t_r1 = time.perf_counter_ns()
            resolve_times.append((t_r1 - t_r0) / 1_000_000.0)

            t_s0 = time.perf_counter_ns()
            _ = serialize_hook_response(packet)
            t_s1 = time.perf_counter_ns()
            serialize_times.append((t_s1 - t_s0) / 1_000_000.0)

    def stats(data: list[float]) -> dict[str, float]:
        sorted_data = sorted(data)
        p50 = statistics.median(sorted_data)
        p95 = sorted_data[int(len(sorted_data) * 0.95)]
        mean = statistics.mean(sorted_data)
        return {"p50_ms": round(p50, 4), "p95_ms": round(p95, 4), "mean_ms": round(mean, 4)}

    # Token profiling
    token_profiles = {}
    categories_to_test = [
        ("Low Risk / Docs", "Update session log in docs/agent/SESSION_LOG.md"),
        ("Medium Risk / UIA", "Inspect accessibility element tree using pywinauto and UI Automation"),
        ("High Risk / Audio", "Optimize WASAPI playback worker loop and handle Kokoro neural TTS synthesis"),
        ("Critical / Security", "Validate egress url destination to prevent SSRF and check dual-key approval manager"),
        ("Multi-Subsystem", "Refactor WASAPI audio playback thread and log events to SQLite database with DualKeyApprovalManager"),
        ("Fallback / Unknown", "Tell me about ancient Roman architecture and aqueducts"),
    ]

    for label, prompt in categories_to_test:
        raw_output = process_hook_input(json.dumps({"lastUserInput": prompt}))
        char_count = len(raw_output)
        tok_count = estimate_tokens(raw_output)
        token_profiles[label] = {
            "characters": char_count,
            "estimated_tokens": tok_count,
        }

    return {
        "iterations": len(e2e_times),
        "e2e_latency": stats(e2e_times),
        "classification_latency": stats(classify_times),
        "resolution_latency": stats(resolve_times),
        "serialization_latency": stats(serialize_times),
        "token_profiles": token_profiles,
    }


if __name__ == "__main__":
    results = run_benchmark(iterations_per_prompt=50)
    print(json.dumps(results, indent=2))
