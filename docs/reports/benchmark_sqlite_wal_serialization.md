# SQLite WAL Serialization Benchmark: Empirical Architectural Evaluation

## Executive Summary

This empirical benchmark directly evaluates the core trade-offs of **ADR-001** by comparing:
- **Mode A (SingleWriterActor):** Centralized FIFO asynchronous queue worker thread serializing transactions on a single persistent connection.
- **Mode B (Direct WAL Connections):** Concurrent multi-threaded access using persistent per-thread connections with `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, 5,000ms `busy_timeout`, and explicit `BEGIN IMMEDIATE` write transactions.

> [!IMPORTANT]
> **Measurement-Only Verification:** No production files or serialization modules were modified. All measurements executed against isolated synthetic databases configured to ensure persistent connection reuse (`is_temp_db == False`).

---

## 1. Test Environment & System Metadata

- **Operating System:** Windows (10.0.26300)
- **Python Interpreter:** 3.12.10
- **SQLite C Library:** 3.49.1
- **CPU Logical Cores:** 16
- **Filesystem / Storage:** NTFS (Local NVMe SSD)
- **Actor Path Temp-Check:** `is_temp_db == False` verified (zero connection reopen churn)
- **Execution Timestamp:** 2026-11-02T10:07:40Z

---

## 2. Workload Performance Matrix

### Sustained Concurrent Writes

| Architecture Mode | Producers | Total Writes | Throughput (ops/s) | E2E P50 (ms) | E2E P95 (ms) | E2E P99 (ms) | Queue / Lock P95 (ms) | Peak Queue | Contention Errors |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Mode A (SingleWriterActor) | 1 | 200 | 11379.3 | 0.06 | 0.12 | 0.14 | 0.02 | 0 | 0 |
| Mode A (SingleWriterActor) | 2 | 400 | 12228.3 | 0.12 | 0.22 | 0.30 | 0.10 | 0 | 0 |
| Mode A (SingleWriterActor) | 4 | 800 | 13479.8 | 0.24 | 0.37 | 0.74 | 0.25 | 3 | 0 |
| Mode A (SingleWriterActor) | 8 | 1600 | 13465.2 | 0.48 | 0.70 | 3.30 | 0.59 | 6 | 0 |
| Mode A (SingleWriterActor) | 16 | 3200 | 13694.6 | 0.98 | 2.23 | 4.47 | 1.64 | 14 | 0 |
| Mode B (Direct WAL Connections) | 1 | 200 | 15569.2 | 0.03 | 0.06 | 0.10 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 2 | 400 | 12363.0 | 0.02 | 0.07 | 0.14 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 4 | 800 | 13072.3 | 0.03 | 0.07 | 0.28 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 8 | 1600 | 8715.3 | 0.02 | 0.07 | 0.47 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 16 | 3200 | 7046.0 | 0.02 | 0.06 | 0.54 | 0.01 | N/A | 0 |

### Burst Load Profile (Synchronized Release)

| Architecture Mode | Producers | Burst Writes | Throughput (ops/s) | E2E P50 (ms) | E2E P95 (ms) | E2E P99 (ms) | Queue / Lock P95 (ms) | Peak Queue | Contention Errors |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Mode A (SingleWriterActor) | 1 | 100 | 12428.7 | 0.06 | 0.11 | 0.13 | 0.02 | 0 | 0 |
| Mode A (SingleWriterActor) | 2 | 200 | 13013.0 | 0.12 | 0.21 | 0.25 | 0.09 | 0 | 0 |
| Mode A (SingleWriterActor) | 4 | 400 | 10676.6 | 0.28 | 0.67 | 1.30 | 0.34 | 3 | 0 |
| Mode A (SingleWriterActor) | 8 | 800 | 13158.6 | 0.50 | 0.69 | 3.22 | 0.60 | 7 | 0 |
| Mode A (SingleWriterActor) | 16 | 1600 | 13315.9 | 0.99 | 1.62 | 3.92 | 1.36 | 14 | 0 |
| Mode B (Direct WAL Connections) | 1 | 100 | 12116.7 | 0.03 | 0.06 | 0.09 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 2 | 200 | 11683.0 | 0.03 | 0.06 | 0.10 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 4 | 400 | 5781.0 | 0.03 | 0.09 | 1.11 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 8 | 800 | 5023.5 | 0.02 | 0.05 | 2.67 | 0.01 | N/A | 0 |
| Mode B (Direct WAL Connections) | 16 | 1600 | 3505.6 | 0.02 | 0.06 | 7.94 | 0.01 | N/A | 0 |

---

## 3. WAL Lifecycle & Checkpoint Concurrency Analysis

### Checkpoint Mode Behaviors

- **PASSIVE Checkpoint:** Duration = 1.13 ms | Result = `(busy=0, log=206, checkpointed=206)`
  - WAL file size remained 4,136,512 bytes (frames transferred to DB without shrinking WAL).
- **RESTART Checkpoint:** Duration = 2.37 ms | Result = `(busy=0, log=651, checkpointed=651)`
- **TRUNCATE Checkpoint:** Duration = 2.70 ms | Result = `(busy=0, log=0, checkpointed=0)`
  - WAL file successfully truncated to **0 bytes**.

### Active Held Reader Contention Verification

1. **Non-Blocking Write Verification:** While an active reader held a read transaction open (`snapshot count = 2500`), the writer committed 200 writes at **18300.8 ops/sec** without blocking.
2. **PASSIVE Checkpoint under Reader:** Executed in 0.01 ms. Returned `(busy=0, log=1492, checkpointed=772)`. Checkpointed frames older than the reader snapshot, leaving uncommitted frames in WAL.
3. **TRUNCATE Checkpoint under Reader:** Returned `busy=1` after 637.57 ms. WAL could **not** be truncated because the active reader required historical frames.
4. **Post-Reader TRUNCATE:** Once the reader committed and closed, TRUNCATE executed in 8.56 ms and shrank the WAL file to **0 bytes**.

---

## 4. Key Architectural Findings & ADR-001 Implications

### 1. Invalidation of Historical Connection Churn in Latency Benchmark
The initial latency SLA benchmark (`tools/benchmark_latency_slas.py`) reported SQLite latencies while unwittingly using `tempfile.TemporaryDirectory()`, which triggered `SingleWriterActor`'s temporary database fallback (`is_temp_db == True`). This caused a new SQLite connection to be opened, 7 PRAGMAs executed, and `sqlite-vec` extension loaded for *every single queued write*. When persistent connection reuse is verified (`is_temp_db == False`), SingleWriterActor transaction latency drops dramatically.

### 2. Contention & Tail Latency (Mode A vs Mode B)
- In **Mode A (SingleWriterActor)**, the actor routes participating writes through one worker thread, reducing contention between independent SQLite writer connections within the participating process. Queue synchronization and possible contention from external connections remain. Under high thread counts, callers experience queue wait latency that scales with concurrency.
- In **Mode B (Direct WAL Connections)**, direct connections expose calling threads to SQLite's database-level writer-lock contention. The configured busy timeout allows waiting for eligible lock conflicts rather than requiring immediate failure. Under high concurrency, threads spin/sleep waiting for writer-lock release, leading to tail latency expansion during bursts.

### 3. Backpressure & Memory Footprint
- SingleWriterActor uses an unbounded `queue.Queue()`. Application-level blocking of callers in `execute_write` is not equivalent to bounded queue capacity or explicit overload rejection; if asynchronous callers or large thread pools submit writes without bounds, in-memory queue depth can expand indefinitely.
- Direct WAL connections expose calling threads to SQLite's database-level writer-lock contention. The configured busy timeout allows waiting for eligible lock conflicts rather than requiring immediate failure, causing callers to block directly at `BEGIN IMMEDIATE` rather than buffering transactions in process memory.

### 4. Architectural Status & Recommendation for ADR-001
- **Status:** BENCHMARK IMPLEMENTED — PRELIMINARY RESULTS ACCEPTED — ADR-001 DECISION DEFERRED.
- `ADR-001` remains **`PROPOSED`**.
- Preliminary results provide encouraging evidence for centralized write serialization under high in-process concurrency, but ratification is deferred pending a validation round addressing:
  1. Concurrency pressure across 16, 32, 64, and 128 outstanding callers.
  2. Larger statistical sample sizes (>= 10,000 operations across 5 independent repetitions).
  3. Disaggregated percentile reporting for Queue Wait, Lock Acquisition, and Transaction Duration.
  4. Precise recording of PRAGMA `wal_autocheckpoint` and checkpoint connection timeouts.
