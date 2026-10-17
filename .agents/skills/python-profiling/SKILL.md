---
name: python-profiling
description: >-
  Zero-overhead sampling profiler, latency telemetry, and thread deadlock inspection
  for Jarvis 2.0. Uses py-spy, cProfile, and tracemalloc to diagnose hot paths,
  TTS speech latency, GIL contention, and memory leaks.
---

# Python Performance Profiling & Latency Diagnostics

This skill provides protocols and automated procedures for diagnosing latency,
lock contention, thread deadlocks, and memory allocations in Jarvis 2.0.

---

## 1. Quick Diagnostic Recipes

### A. Live Call Stack Dump of All Running Threads (Deadlock Finder)
When Jarvis hangs or a worker appears stalled:
```powershell
# 1. Identify Jarvis PID
Get-Process python | Select-Object Id, CPU, WS, MainWindowTitle

# 2. Dump call stacks of all threads instantly via py-spy (reads memory non-intrusively)
.\.venv\Scripts\py-spy.exe dump --pid <PID>
```

### B. Live Console Top-View (Active Function Profiling)
See which functions are currently consuming CPU cycles:
```powershell
.\.venv\Scripts\py-spy.exe top --pid <PID>
```

### C. High-Resolution Flame Graph Recording
Record a 10-second sampling profile to an interactive SVG flame graph:
```powershell
.\.venv\Scripts\py-spy.exe record -o latency_flamegraph.svg --duration 10 --rate 200 --pid <PID>
```

---

## 2. In-Process Latency & Microbenchmarking

### Audio Pipeline Benchmark (Time-to-First-Phoneme)
Measure end-to-end synthesis latency from text token to WASAPI playback buffer:
```python
import time
from jarvis.audio.tts import get_tts_engine

tts = get_tts_engine()
t0 = time.perf_counter()
chunks = list(tts.synthesize_stream("Right away, Sir. All systems operational."))
elapsed_ms = (time.perf_counter() - t0) * 1000.0
print(f"Time-to-First-Chunk: {elapsed_ms:.2f}ms across {len(chunks)} audio blocks.")
```

### Memory Allocation Tracing
Detect memory leaks or excessive buffer allocations:
```python
import tracemalloc

tracemalloc.start()
# Execute candidate code
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')
for stat in top_stats[:10]:
    print(stat)
```
