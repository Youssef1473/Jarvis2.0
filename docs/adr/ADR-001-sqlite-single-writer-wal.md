# ADR-001: SQLite Single-Writer Serialization and WAL Mode

## Status
PROPOSED

## Date
2026-11-02

## Scope
Write operations on the unified persistent database [`data/jarvis_memory.db`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/data/jarvis_memory.db), encompassing episodic and semantic memory, atomic facts, learned reflexes, temporal habit graphs, and Obsidian vault synchronization.

> [!IMPORTANT]
> **Scope Boundary & Writer Ownership Invariant:** As established in the [SQLite Writer Ownership Audit](../reports/sqlite_writer_ownership_audit.md), Jarvis 2.0 maintains a single unified physical database on disk (`data/jarvis_memory.db`). This ADR governs write serialization for that database. However, writer ownership is currently divided across three arrangements:
> - **Category A (Actor Serialized):** Core memory mixins (`FactsManagerMixin`, `InteractionMemoryMixin`, etc.), `DynamicReflexEngine`, and `TranscriptDistiller`.
> - **Category B (Domain-Queued):** `TemporalHabitGraph` transitions and `ObsidianContextSyncEngine` buffer events in domain queues before serialized dispatch to `SingleWriterActor`.
> - **Category C (Direct Writers / Bypasses):** `photo_manager.py`, `reflex_macro_compiler.py`, habit decay, and `SleepPhaseConsolidator` write directly to `data/jarvis_memory.db` without routing through the actor.
> Complete writer exclusivity requires migrating Priority 1 direct writers to the actor and adopting an interruptible chunked maintenance policy for `SleepPhaseConsolidator`.

---

## Context and Problem Statement
Jarvis 2.0 operates multiple concurrent background threads—including cognitive routing, audio streaming, reflex dispatch, habit tracking, and semantic memory indexing. In standard SQLite configurations on Windows, concurrent writes from multiple OS threads encounter mandatory file-locking contention, leading to `sqlite3.OperationalError: database is locked` (`SQLITE_BUSY`) exceptions and potential database corruption during abrupt process termination.

---

## Decision
Serialize all write operations (`INSERT`, `UPDATE`, `DELETE`, schema migrations) for core memory stores through a dedicated per-database background actor thread using `SingleWriterActor`.

1. **Per-Database Actor Singleton:** Maintain exactly one `SingleWriterActor` instance per normalized database path (`os.path.normcase(str(Path(db_path).resolve()))`), guarded by `_registry_lock` and a per-database `threading.RLock`.
2. **Dedicated Serialization Worker:** Write callables are submitted to a thread-safe FIFO `queue.Queue` and executed sequentially by a single background worker thread (`JarvisSingleWriter`).
3. **Write-Ahead Logging (WAL) Configuration:** Every worker connection initializes with high-performance concurrency pragmas:
   - `PRAGMA journal_mode=WAL;` (Enables concurrent readers alongside the single writer).
   - `PRAGMA synchronous=NORMAL;` (Reduces disk flush overhead while preserving WAL crash consistency).
   - `PRAGMA busy_timeout=5000;` (5-second lock acquisition backoff).
   - `PRAGMA mmap_size=268435456;` (256 MB memory-mapped I/O).
   - `PRAGMA cache_size=-64000;` (64 MB page cache).
   - `PRAGMA temp_store=MEMORY;`
   - `PRAGMA recursive_triggers=ON;`
4. **Execution Phase Tracking:** Each queued item tracks its execution phase (`pending`, `executing`, `completed`, `cancelled_before_execution`, `timed_out_in_flight`) under an item mutex, allowing callers to determine whether an expired timeout occurred before execution began or mid-transaction.
5. **Fail-Closed Fallback Discipline:** Fallback to a direct connection is authorized via `execute_or_fallback()` **only** if the actor was completely unavailable before queuing. Direct fallback is strictly prohibited (`ActorStoppingError`) if the actor worker thread is currently stopping or active, preventing dual-writer race conditions.

---

## Alternatives Considered
- **Direct Multi-Threaded Writes with Busy Retries:** Rejected due to persistent Windows file-lock contention and non-deterministic transaction latency under high thread counts.
- **Global In-Process Mutex:** Rejected because a coarse lock blocks concurrent readers and across distinct database files.
- *(Historical alternatives from initial pre-M-02 architecture: Historical rationale unavailable).*

---

## Consequences and Trade-Offs
- **Positive:**
  - Complete elimination of `SQLITE_BUSY` write conflicts across core memory stores.
  - Concurrent readers access the WAL database without blocking or being blocked by the single writer.
  - Predictable FIFO transaction serialization.
- **Negative / Costs:**
  - Queued writes incur thread dispatch context-switch latency.
  - A slow or stalling write transaction blocks all subsequent writes queued for that database.
  - Caller threads must block on `done_evt.wait()` or utilize async offloading (`asyncio.to_thread`).

---

## Binding Invariants
- All mutating writes to core memory stores must route through `SingleWriterActor.execute_write`.
- No two live writer threads may ever hold an open write connection to the same resolved database file path simultaneously.
- Direct connection fallbacks must fail closed if an actor worker thread is alive or stopping.

---

## Implementation References
- [`sqlite_base.py:41`](../../jarvis/core/memory/sqlite_base.py#L41) (`SingleWriterActor`)
- [`sqlite_base.py:204`](../../jarvis/core/memory/sqlite_base.py#L204) (`_create_conn` and WAL pragmas)
- [`sqlite_base.py:441`](../../jarvis/core/memory/sqlite_base.py#L441) (`execute_or_fallback`)

---

## Production Call Sites
- [`jarvis/core/memory/__init__.py`](../../jarvis/core/memory/__init__.py) (`MemoryManager` decomposing episodic, semantic, facts, documents, tasks, and interaction memory)
- [`jarvis/core/reflex_engine.py`](../../jarvis/core/reflex_engine.py) (`DynamicReflexEngine` habit caching)
- [`jarvis/core/cognition/habit_graph.py`](../../jarvis/core/cognition/habit_graph.py) (`TemporalHabitGraph` routine action transitions)
- [`jarvis/core/memory/obsidian_sync.py`](../../jarvis/core/memory/obsidian_sync.py) (`ObsidianContextSyncEngine` vault chunk batching)
- [`jarvis/core/memory/transcript_distiller.py`](../../jarvis/core/memory/transcript_distiller.py) (`TranscriptDistiller` background conversation distillation)

---

## Verification Evidence
- [`test_milestone2_memory_hardening.py`](../../tests/test_milestone2_memory_hardening.py) (10 of 10 tests passed on 2026-11-02).
- [`test_memory_concurrency_stress.py`](../../tests/test_memory_concurrency_stress.py) and [`test_sqlite_vec_memory.py`](../../tests/test_sqlite_vec_memory.py) (Actor lifecycle and concurrent queue stress tests).
- [Preliminary Benchmark Report: SingleWriterActor vs Direct WAL Connections](../reports/benchmark_sqlite_wal_serialization.md) (Empirical evaluation across 1 to 16 producers).
- [Validation Round Report: High-Concurrency & Disaggregated Latency](../reports/benchmark_sqlite_wal_validation_round.md) (Full spectrum 1 to 128 callers, 10,000 ops / config across 5 repetitions).
- [ADR-001 Architectural Decision Memo](ADR-001-decision-memo.md) (Synthesis of trade-offs, operational boundaries, and deferral rationale).
- [SQLite Writer Ownership Audit Report](../reports/sqlite_writer_ownership_audit.md) (Forensic census of connection PRAGMAs, database path identity, and direct writer bypasses).

---

## Known Limitations and Unresolved Questions
- **Partial Writer Ownership:** Four direct-write paths (`photo_manager.py`, `reflex_macro_compiler.py`, habit graph decay, and `SleepPhaseConsolidator`) target `data/jarvis_memory.db` directly. Complete writer exclusivity requires migrating Priority 1 direct writers to the actor and formalizing an interruptible chunked maintenance policy for `SleepPhaseConsolidator`.
- **Connection-Specific PRAGMAs:** Unconfigured direct connections (`photo_manager.py`, `reflex_macro_compiler.py`) execute with connection-local `synchronous=FULL` (2) rather than `NORMAL` (1).
- **Vector Extension Loading:** Vector search via `sqlite-vec` extension is conditionally loaded; if extension loading fails, vector search falls back to brute-force NumPy evaluation.

---

## Related ADRs
- None.
