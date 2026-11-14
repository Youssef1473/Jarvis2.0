"""Performance and Latency Benchmark for Failure Collector (ENG-OS-02 Phase 3B2).

Measures:
1. In-process internal logic latencies:
   - JSON parsing
   - Event normalization
   - Evidence validation
   - Redaction processing
   - Storage deduplication
   - Total internal collector logic
2. Complete out-of-process hook execution overhead:
   - Cold process startup (first python subprocess invocation)
   - Warm repeated process invocations (successful command payload - fast exit)
   - Warm repeated process invocations (failing command payload - with persistence)

Reports min, median, p95, and max across iterations.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add repo root to path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.failure_collector import (
    EventEnvelope,
    parse_post_tool_use,
    persist_event_envelope,
    process_hook_payload,
    read_step_evidence,
    redact_sensitive_text,
)

NUM_INTERNAL_ITERATIONS = 500
NUM_SUBPROCESS_ITERATIONS = 30


def run_internal_benchmark() -> dict[str, dict[str, float]]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_dir = Path(tmp_dir) / "events"
        mock_root = Path(tmp_dir) / "mock_conv"
        mock_root.mkdir(parents=True, exist_ok=True)

        step_dir = mock_root / ".system_generated" / "steps" / "42"
        step_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = step_dir / "output.txt"
        evidence_file.write_text(
            "Traceback (most recent call last):\n"
            "  File 'test.py', line 12, in <module>\n"
            "    raise AssertionError('Simulated failure')\n"
            "AssertionError: Simulated failure\n"
            "Command exited with code 1.\n",
            encoding="utf-8",
        )

        sample_payload_dict = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "pytest tests/failing.py --key=sk-dummy1234567890abcdef1234",
                    "toolSummary": "Run test suite",
                },
            },
            "stepIdx": 42,
            "error": "exit status 1",
            "conversationId": "bench-uuid-1234-5678-90ab",
            "artifactDirectoryPath": str(mock_root),
        }
        sample_payload_str = json.dumps(sample_payload_dict)

        parse_latencies_ms: list[float] = []
        norm_latencies_ms: list[float] = []
        evidence_latencies_ms: list[float] = []
        redact_latencies_ms: list[float] = []
        storage_latencies_ms: list[float] = []
        total_latencies_ms: list[float] = []

        raw_text_to_redact = (
            "Error on C:\\Users\\DummyUser\\project: token = ghp_dummytoken12345678901234567890, "
            "bearer dummy-token-xyz123456, key: AIzaSyDummyGoogleApiKey123456789012345"
        )

        for i in range(NUM_INTERNAL_ITERATIONS):
            # 1. JSON parse
            t0 = time.perf_counter()
            parsed_dict = json.loads(sample_payload_str)
            t1 = time.perf_counter()
            parse_latencies_ms.append((t1 - t0) * 1000.0)

            # 2. Event normalization
            t0 = time.perf_counter()
            _ = parse_post_tool_use(parsed_dict, allowed_root=mock_root)
            t1 = time.perf_counter()
            norm_latencies_ms.append((t1 - t0) * 1000.0)

            # 3. Evidence validation & read
            t0 = time.perf_counter()
            read_step_evidence(evidence_file, mock_root)
            t1 = time.perf_counter()
            evidence_latencies_ms.append((t1 - t0) * 1000.0)

            # 4. Redaction processing
            t0 = time.perf_counter()
            redact_sensitive_text(raw_text_to_redact)
            t1 = time.perf_counter()
            redact_latencies_ms.append((t1 - t0) * 1000.0)

            # 5. Storage & Deduplication
            envelope_var = EventEnvelope(
                event_id=f"bench_event_{i % 10}",
                event_type="POST_TOOL_USE",
                collection_status="OBSERVED_FAILURE",
                failure_signal="exit status 1",
            )
            t0 = time.perf_counter()
            persist_event_envelope(envelope_var, storage_dir)
            t1 = time.perf_counter()
            storage_latencies_ms.append((t1 - t0) * 1000.0)

            # 6. Total end-to-end internal
            t0 = time.perf_counter()
            process_hook_payload(sample_payload_str, custom_storage_dir=storage_dir, allowed_root=mock_root)
            t1 = time.perf_counter()
            total_latencies_ms.append((t1 - t0) * 1000.0)

        def summarize(latencies: list[float]) -> dict[str, float]:
            sorted_lat = sorted(latencies)
            p95_idx = int(len(sorted_lat) * 0.95)
            return {
                "min_ms": round(sorted_lat[0], 4),
                "median_ms": round(statistics.median(sorted_lat), 4),
                "p95_ms": round(sorted_lat[p95_idx], 4),
                "max_ms": round(sorted_lat[-1], 4),
            }

        return {
            "json_parsing": summarize(parse_latencies_ms),
            "normalization": summarize(norm_latencies_ms),
            "evidence_validation": summarize(evidence_latencies_ms),
            "redaction": summarize(redact_latencies_ms),
            "storage_dedup": summarize(storage_latencies_ms),
            "total_internal": summarize(total_latencies_ms),
        }


def run_subprocess_benchmark() -> dict[str, dict[str, float]]:
    """Measures complete out-of-process hook execution overhead (process spawn + stdio)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_dir = Path(tmp_dir) / "events"
        collector_script = str(WORKSPACE_ROOT / "tools" / "failure_collector.py")

        success_payload = json.dumps({
            "toolCall": {"name": "run_command", "args": {"CommandLine": "python --version"}},
            "stepIdx": 10,
            "error": "",
            "conversationId": "bench-subproc-success",
        })

        failure_payload = json.dumps({
            "toolCall": {"name": "run_command", "args": {"CommandLine": "python failing_test.py"}},
            "stepIdx": 11,
            "error": "exit status 1",
            "conversationId": "bench-subproc-failure",
        })

        env = os.environ.copy()
        env["JARVIS_LEARNING_EVENTS_DIR"] = str(storage_dir)

        # 1. Cold process startup
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, collector_script],
            input=success_payload,
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        t1 = time.perf_counter()
        cold_startup_ms = (t1 - t0) * 1000.0

        # 2. Warm success executions (fast exit without disk write)
        warm_success_ms: list[float] = []
        for _ in range(NUM_SUBPROCESS_ITERATIONS):
            t0 = time.perf_counter()
            subprocess.run(
                [sys.executable, collector_script],
                input=success_payload,
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            t1 = time.perf_counter()
            warm_success_ms.append((t1 - t0) * 1000.0)

        # 3. Warm failure executions (with disk persistence)
        warm_failure_ms: list[float] = []
        for _ in range(NUM_SUBPROCESS_ITERATIONS):
            t0 = time.perf_counter()
            subprocess.run(
                [sys.executable, collector_script],
                input=failure_payload,
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            t1 = time.perf_counter()
            warm_failure_ms.append((t1 - t0) * 1000.0)

        def summarize(latencies: list[float]) -> dict[str, float]:
            sorted_lat = sorted(latencies)
            p95_idx = int(len(sorted_lat) * 0.95)
            return {
                "min_ms": round(sorted_lat[0], 2),
                "median_ms": round(statistics.median(sorted_lat), 2),
                "p95_ms": round(sorted_lat[p95_idx], 2),
                "max_ms": round(sorted_lat[-1], 2),
            }

        return {
            "cold_process_startup": {
                "min_ms": round(cold_startup_ms, 2),
                "median_ms": round(cold_startup_ms, 2),
                "p95_ms": round(cold_startup_ms, 2),
                "max_ms": round(cold_startup_ms, 2),
            },
            "subproc_success_warm": summarize(warm_success_ms),
            "subproc_failure_warm": summarize(warm_failure_ms),
        }


def main_bench() -> None:
    print(f"Executing Failure Collector In-Process Benchmark ({NUM_INTERNAL_ITERATIONS} iterations)...")
    internal_results = run_internal_benchmark()
    print("\n" + "=" * 65)
    print(f"{'Metric (In-Process)':<25} | {'Min (ms)':<9} | {'Median':<9} | {'P95 (ms)':<9} | {'Max (ms)':<9}")
    print("-" * 65)
    for metric, stats in internal_results.items():
        print(
            f"{metric:<25} | {stats['min_ms']:<9.4f} | {stats['median_ms']:<9.4f} | "
            f"{stats['p95_ms']:<9.4f} | {stats['max_ms']:<9.4f}"
        )
    print("=" * 65)

    print(f"\nExecuting Subprocess Hook Overhead Benchmark ({NUM_SUBPROCESS_ITERATIONS} iterations)...")
    subproc_results = run_subprocess_benchmark()
    print("\n" + "=" * 65)
    print(f"{'Metric (Out-of-Process)':<25} | {'Min (ms)':<9} | {'Median':<9} | {'P95 (ms)':<9} | {'Max (ms)':<9}")
    print("-" * 65)
    for metric, stats in subproc_results.items():
        print(
            f"{metric:<25} | {stats['min_ms']:<9.2f} | {stats['median_ms']:<9.2f} | "
            f"{stats['p95_ms']:<9.2f} | {stats['max_ms']:<9.2f}"
        )
    print("=" * 65)


if __name__ == "__main__":
    main_bench()
