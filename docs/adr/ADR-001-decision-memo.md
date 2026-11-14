# ADR-001 Decision Memo: SQLite Single-Writer Serialization vs. Direct WAL Connections

- **Subject:** Formal Architectural Evaluation of `SingleWriterActor` for Scoped Persistent Stores
- **Target Decision Record:** [`ADR-001-sqlite-single-writer-wal.md`](ADR-001-sqlite-single-writer-wal.md)
- **Current Status:** **`DECISION DEFERRED (ADR-001 REMAINS PROPOSED)`**
- **Date:** 2026-11-02
- **Author:** Software Engineering Collaborator (Antigravity)
- **Reference Evidence:**
  - Preliminary Baseline: [`docs/reports/benchmark_sqlite_wal_serialization.md`](../reports/benchmark_sqlite_wal_serialization.md) ([JSON Dataset](../reports/benchmark_sqlite_wal_results.json))
  - Full-Spectrum Validation Round (Superseding): [`docs/reports/benchmark_sqlite_wal_validation_round.md`](../reports/benchmark_sqlite_wal_validation_round.md) ([JSON Dataset](../reports/benchmark_sqlite_wal_validation_round.json))

---

## 1. Executive Summary & Core Trade-Off

Across two rigorous benchmark rounds on Windows 11 (NTFS on local NVMe SSD, Python 3.12, SQLite 3.49.1), `SingleWriterActor` (Mode A) and Direct WAL Connections (Mode B) were empirically evaluated across 1 to 128 concurrent callers with 10,000 measured operations per configuration across 5 independent repetitions.

> [!NOTE]
> **Synthetic Microbenchmark Classification:** All measurements reflect a synthetic microbenchmark (single-table insert with an indexed key and ~350-byte payload) under controlled conditions. This establishes a clean comparative baseline between centralized queue serialization and multi-connection locking, but does not establish that complex production Jarvis operations (with FTS5 full-text indexing, sqlite-vec dense embeddings, cascading foreign keys, or multi-statement transactions) will achieve identical throughput. Representative-workload benchmarking is required before making production throughput claims for full memory operations.

### The Fundamental Trade-Off:

| Architectural Dimension | Mode A (`SingleWriterActor`) | Mode B (Direct WAL Connections) | Architectural Takeaway |
| :--- | :--- | :--- | :--- |
| **Low Concurrency (1 Caller)** | 13,112.0 ops/s (P50 = 0.05 ms) | **20,745.4 ops/s** (P50 = 0.02 ms) | Direct WAL is **1.58× faster** at 1 caller by avoiding thread queue dispatch and event signaling. |
| **High Concurrency (128 Callers)** | **12,237.7 ops/s** (P50 = 8.91 ms) | 583.7 ops/s (P50 = 0.03 ms) | Actor is **20.96× faster** at 128 callers; direct throughput collapses by 97.2% under lock contention. |
| **P99 Tail Latency (128 Callers)** | **12.49 ms** (P99 Queue Wait = 12.39 ms) | **2,201.06 ms** (P99 Lock Acquisition = 2,200.86 ms) | Empirical observation: in tested workload, Actor P99 remained < 12.5 ms (not an absolute bound under arbitrary load). Direct lock contention explodes to > 2.2 seconds. |
| **Underlying SQL Execution Time** | 0.03 – 0.05 ms (P50 across all tiers) | 0.02 – 0.05 ms (P50 across all tiers) | The actual statement execution + commit time is identical; difference is entirely access arbitration. |
| **Multi-Repetition Stability** | Rel StdDev $\le 4.0\%$ across all 8 tiers | Rel StdDev 4.1%–24.5% overall; 13.8%–24.5% under contention (8..128) | Actor throughput is highly repeatable; Direct lock contention is non-deterministic under load. |
| **Process Boundary** | Strictly process-local Python registry | Handled by OS/SQLite shared memory | Actor cannot coordinate with external processes or auxiliary tools writing to the same file. |
| **Queue & Backpressure** | Unbounded Python `queue.Queue()` | SQLite C-level `busy_timeout` waiting | Application-level blocking limits queue in synchronous calls, but detached async writes risk unbounded growth. |

---

## 2. Invalidation of Historical SLA Benchmark

The preliminary inspection revealed that the historical SLA benchmark in `tools/benchmark_latency_slas.py` executed inside `tempfile.TemporaryDirectory()`. Because the Windows temp path contained `"temp"`/`"tmp"`, it inadvertently triggered `SingleWriterActor`'s temporary database fallback heuristic (`is_temp_db == True`). This forced a complete connection reopen, 7 PRAGMA executions, and dynamic `sqlite-vec` extension loading on *every single queued write*.

When verified with persistent connection reuse (`is_temp_db == False`):
- Historical reported latency: ~1.0 – 2.0 ms.
- Actual persistent connection P50 latency: **0.05 – 0.11 ms**.

The historical SLA measurements are classified as invalid for evaluating persistent actor performance.

---

## 3. Scoped Persistence Reality: Unified Database & Writer Ownership

The forensic investigation documented in [`docs/reports/sqlite_writer_ownership_audit.md`](../reports/sqlite_writer_ownership_audit.md) established that the five database files listed in earlier drafts (`data/memory.db`, `data/facts.db`, `data/habits.db`, `data/reflex.db`, `data/obsidian_sync.db`) do **not exist as separate physical files on disk** in production.

All core episodic memories, atomic facts, learned habits, temporal transitions, reflex macros, and photo metadata reside within a **single canonical database file**:
👉 [`data/jarvis_memory.db`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/data/jarvis_memory.db) (resolved via `jarvis.core.config_loader.get_database_path()`).

### Three Concurrency Arrangements Targeting the Canonical Database:
1. **Pure `SingleWriterActor` (Category A):** Core memory mixins (`facts_manager`, `interaction_memory`, `document_vault`, `tasks_calendar`, `temporal_graph`), `DynamicReflexEngine`, and `TranscriptDistiller` serialize strictly through the actor's FIFO queue and single persistent worker connection.
2. **Domain Queues Dispatching to Actor (Category B):** `TemporalHabitGraph` (routine transitions) and `ObsidianContextSyncEngine` buffer events in domain queues (`_write_queue` and `_db_queue`) to isolate callers from disk latency, then dispatch write callables to `SingleWriterActor`.
3. **Independent Direct Writers Bypassing Actor (Category C):**
   - `PhotoManager`: Dedicated thread `PhotoDBWorker` opens a direct SQLite connection to `data/jarvis_memory.db` (`synchronous=FULL`, `busy_timeout=5000`).
   - `ReflexMacroCompiler`: Directly connects to `data/jarvis_memory.db` on caller threads (`synchronous=FULL`, `busy_timeout=5000`).
   - `TemporalHabitGraph.apply_exponential_decay()`: Directly connects to `data/jarvis_memory.db` during maintenance passes.
   - `SleepPhaseConsolidator`: Directly connects to `data/jarvis_memory.db` using interruptible transactions registered with VDBE progress handler abort hooks.

### Architectural Takeaway:
Because all writers share the exact same physical database file (`data/jarvis_memory.db`), `SingleWriterActor` currently has **partial, not exclusive, write ownership** over the database. Direct writers contend for the SQLite OS write lock, where Python's connection `timeout=5.0` manages `SQLITE_BUSY` backoff. Addressing this requires a clear three-tier writer policy rather than assuming universal in-memory serialization.

---

## 4. Five Unresolved Architectural Requirements Before Ratification

Ratification of ADR-001 is deferred because throughput advantage alone does not establish complete operational fitness. Five architectural policies must be formally decided:

### 1. Writer Ownership & Staged Migration Sequence
- **Current Reality:** Four direct-write paths bypass `SingleWriterActor` on `data/jarvis_memory.db`.
- **Recommended Migration Sequence:**
  1. **`ReflexMacroCompiler`:** Candidate for actor migration; formally resolve durability policy first (`FULL` vs `NORMAL`).
  2. **`PhotoManager`:** Candidate for actor migration; formally resolve durability policy first (`FULL` vs `NORMAL`).
  3. **`TemporalHabitGraph.decay`:** Strong candidate for actor migration; wrap batch decay updates in a single actor callable.
  4. **`SleepPhaseConsolidator`:** Design an interruptible chunked implementation with per-chunk progress handlers, verifying idempotency and recoverability across partially completed runs.
  5. **`HabitGraph` & `Obsidian` Queues:** Retain upstream domain queues to preserve producer isolation from Win32 hook and file-watcher pumps.
- **Three Prerequisite Design Questions Before Production Changes:**
  1. *Durability Policy Selection:* What synchronization policy (`synchronous=NORMAL` vs `synchronous=FULL`) should apply to `PhotoManager` and `ReflexMacroCompiler`? Migrating them to `SingleWriterActor` would alter their effective durability guarantees; the project must explicitly decide whether their workloads accept `NORMAL` synchronization before designing connection or actor configurations.
  2. *Recoverability & Idempotency:* How will `SleepPhaseConsolidator` preserve recoverability and idempotency if a multi-chunk maintenance pass is aborted halfway through by user activity?
  3. *Queue Admission & Starvation Gating:* What admission control or interleaving policy will prevent a burst of maintenance chunks from delaying high-priority interactive dialogue writes in `SingleWriterActor._queue`?

### 2. Admission & Overload Backpressure Policy
- **Current Reality:** `SingleWriterActor` initializes `queue.Queue()` without a `maxsize`.
- **Constraint:** In synchronous `execute_write()`, caller threads block on `done_evt.wait()`, which naturally bounds queue depth to the number of active calling threads (peak queue depth reached 126 with 128 callers).
- **Risk:** If asynchronous tasks (`execute_write_async`) or detached task pools submit writes without caller-side gating, queue depth can expand indefinitely during heavy disk I/O, risking unbounded memory growth.
- **Requirement:** Define whether `SingleWriterActor` should enforce an explicit queue capacity (e.g., `queue.Queue(maxsize=1024)`) with fast-fail `ActorOverloadedError`, or whether admission backpressure remains an application-level responsibility.

### 3. Process Boundary & External Writer Policy
- **Current Reality:** The singleton registry (`_instances`) is confined to a single Python OS process.
- **Constraint:** SQLite WAL mode coordinates multiple connections via operating system shared memory (`.db-shm`) and file locks (`.db-wal`). However, `SingleWriterActor` is entirely unaware of external OS processes.
- **Risk:** If an external CLI, backup agent, or separate microservice attempts to write to `jarvis_memory.db` directly, it will compete with the actor's worker thread at the SQLite file level, potentially encountering `SQLITE_BUSY` or stalling the actor worker.
- **Requirement:** Formally document that scoped core databases are strictly **single-process private stores**. External tools must route mutations via IPC/daemon RPC, or be restricted to read-only WAL snapshots.

### 4. Checkpoint Ownership & Autocheckpoint Policy
- **Current Reality:** All worker connections operate with default `PRAGMA wal_autocheckpoint=1000;`.
- **Observation:** Automatic checkpoint settings are connection-specific. In production, each writer connection inherits or configures its own autocheckpoint threshold (default 1,000 pages). Inspecting a single probe connection does not establish the configuration of all active connections. When a committing thread crosses this threshold, it performs checkpoint work on its own thread, contributing to occasional commit tail latency (P99).
- **Diagnostic Finding:** Under held reader transactions, `wal_checkpoint(TRUNCATE)` correctly returns `busy=1` after timeout without damaging the active reader snapshot, while `wal_checkpoint(PASSIVE)` checkpoints older frames without truncating.
- **Requirement:** Avoid disabling automatic checkpointing (`wal_autocheckpoint=0`) until an explicit background checkpoint ownership and recovery policy (e.g. in `SleepPhaseConsolidator`) has been formally designed and verified.

### 5. Durability & Connection PRAGMA Policy
- **Current Reality:** `SingleWriterActor` uses WAL with `PRAGMA synchronous=NORMAL;`. However, independent unconfigured connections (`PhotoManager`, `ReflexMacroCompiler`) execute with connection-local compile-time default `synchronous=FULL` (2).
- **Durability Consequence:** `NORMAL` prioritizes performance while accepting that recently committed transactions may be lost following a power failure or operating-system crash before checkpoint synchronization. Database consistency is maintained, but individual transaction durability is not guaranteed in the same way as with `FULL`. These measurements do not establish equivalent performance or durability under `synchronous=FULL`.
- **Requirement:** Unify connection pragmas across all writers targeting `data/jarvis_memory.db` by routing them through `SingleWriterActor` so that `synchronous=NORMAL` is consistently applied.
- **Requirement:** Formally confirm that `synchronous=NORMAL` remains project policy for core memory stores, accepting power-loss durability deferred to checkpoint boundaries in exchange for 12,000+ ops/sec write performance.

---

## 5. Dataset Accounting & Test Verification Status

- **Dataset Supersession:** The 8-configuration full-spectrum validation dataset ([`benchmark_sqlite_wal_validation_round.md`](../reports/benchmark_sqlite_wal_validation_round.md) and [`.json`](../reports/benchmark_sqlite_wal_validation_round.json)) supersedes the preliminary 4-configuration validation run. The Phase 1 preliminary baseline ([`benchmark_sqlite_wal_serialization.md`](../reports/benchmark_sqlite_wal_serialization.md) and [`.json`](../reports/benchmark_sqlite_wal_results.json)) remains preserved for historical comparison.
- **Representative Tiers Clarification:** Summary tables in executive communications and Section 1 display representative concurrency tiers ($C=1, C=16, C=128$) to illustrate low-concurrency baseline, mid-tier crossover, and extreme load; the complete eight-tier breakdown across all 80 runs ($C \in \{1, 2, 4, 8, 16, 32, 64, 128\}$) is recorded in full within `benchmark_sqlite_wal_validation_round.md` and `.json`.
- **Test Baseline Status:** The full Tier 1 fast test suite completed earlier (`1,758 passed, 22 skipped, 0 failed, exit code 0`). Targeted memory hardening verification passed 10/10 on the latest working tree revision (`tests/test_milestone2_memory_hardening.py`: 10 passed in 1.35s, exit code 0). AST invariants and Ruff lint diagnostics passed with exit code 0.

---

## 6. Architectural Recommendation

1. **Retain `SingleWriterActor` as the Leading Architectural Proposal:**
   The empirical evidence is definitive: within a single Python process on Windows, centralized serialization achieves **12,000–13,500 ops/s** consistently across 1 to 128 callers, completely avoiding the severe lock-acquisition collapse (97% decline) experienced by direct concurrent WAL connections.
2. **Keep ADR-001 in `PROPOSED` Status:**
   Ratification should occur only after the project team aligns on the five operational policies above (specifically bounded queue admission and external process confinement).
3. **Preserve Validation Tooling:**
   The validation harness [`tools/benchmark_sqlite_wal_validation.py`](../../tools/benchmark_sqlite_wal_validation.py) and baseline [`tools/benchmark_sqlite_wal_serialization.py`](../../tools/benchmark_sqlite_wal_serialization.py) provide automated, regression-safe empirical verification for future storage reviews.
