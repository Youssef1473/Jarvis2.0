"""Master runner for all reproducible Jarvis 2.0 latency and throughput benchmarks."""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.benchmarks.benchmark_audio_ring_buffer import (
    run_benchmark as run_audio_benchmark,
)
from tools.benchmarks.benchmark_reflex_dispatch import (
    run_benchmark as run_reflex_benchmark,
)
from tools.benchmarks.benchmark_streaming_parser import (
    run_benchmark as run_parser_benchmark,
)


def main():
    print("# Jarvis 2.0 Empirical Performance & Latency Benchmark Suite\n")
    run_audio_benchmark()
    print("\n")
    run_reflex_benchmark()
    print("\n")
    run_parser_benchmark()
    print("\n[+] All benchmarks executed successfully.")


if __name__ == "__main__":
    main()
