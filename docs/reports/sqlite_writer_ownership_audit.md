# SQLite Physical Database Identity & Writer Ownership Audit

- **Subject:** Forensic Path-Identity, Connection Configuration, and Writer Ownership Census
- **Target Subsystems:** All SQLite Connection Sites in `jarvis/`
- **Canonical Database Reference:** `data/jarvis_memory.db`
- **Audit Date:** 2026-11-02
- **Audit Mode:** Read-Only Static AST & Call-Path Analysis
- **Status:** **COMPLETE — VERIFIED**

---

## Executive Summary

This forensic audit investigates the write-serialization architecture of Jarvis 2.0 to resolve the exact relationship between [`SingleWriterActor`](../../jarvis/core/memory/sqlite_base.py#L41-L596), domain-level background queues, and direct SQLite writer connections.

### Key Audit Findings:
1. **Single Canonical Physical Database:** The five separate database files cited in earlier documentation (`data/memory.db`, `data/facts.db`, `data/habits.db`, `data/reflex.db`, `data/obsidian_sync.db`) do **not** exist as distinct physical database files on disk in production. All core episodic memory, atomic facts, learned habits, temporal transitions, reflex macros, and photo metadata reside as tables within a **single canonical database file**: [`data/jarvis_memory.db`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/data/jarvis_memory.db).
2. **Double-Queued Subsystems:** [`TemporalHabitGraph`](../../jarvis/core/cognition/habit_graph.py#L41-L430) and [`ObsidianContextSyncEngine`](../../jarvis/core/memory/obsidian_sync.py#L228-L500) maintain internal domain queues to isolate calling threads from disk latency, but **they route final write operations directly through `SingleWriterActor`**. They are clients of the actor, not independent database writers.
3. **Verified Canonical Database Bypasses:** Four write paths write directly to `data/jarvis_memory.db` without dispatching through `SingleWriterActor`:
   - [`PhotoManager._db_worker_loop()`](../../jarvis/skills/photo_manager.py#L60-L97) (Background thread committing photo metadata).
   - [`ReflexMacroCompiler`](../../jarvis/skills/reflex_macro_compiler.py#L31-L285) (Caller-thread execution registering and updating reflex macro outcomes).
   - [`TemporalHabitGraph.apply_exponential_decay()`](../../jarvis/core/cognition/habit_graph.py#L340-L399) (Direct multi-row decay and pruning).
   - [`SleepPhaseConsolidator`](../../jarvis/core/memory/sleep_consolidator.py#L196-L380) (Interruptible multi-pass maintenance transactions with VDBE progress abort hooks).
4. **Architectural Consequence:** Because all four direct writers target `data/jarvis_memory.db`, `SingleWriterActor` does **not** have exclusive ownership over write transactions on the physical database file. Contention between the actor's persistent worker connection and direct writer connections is arbitrated at the SQLite OS file-lock level (`SQLITE_BUSY` backoff) rather than serialized entirely in Python memory.

---

## 1. Physical Database Path Identity Matrix

This matrix traces every SQLite connection in `jarvis/` from its configuration source and constructor argument down to the resolved path on the local filesystem.

| Subsystem / Module | Constructor Argument | Config / Resolver Source | Resolved Path | Same Physical File as Actor? | Connection Mode | Finding Status |
| :--- | :--- | :--- | :--- | :---: | :---: | :---: |
| **`MemoryManager` / `SqliteBaseMixin`**<br>[`jarvis/core/memory/__init__.py:63`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/__init__.py#L63) | `config["memory"]["db_path"]`<br>(Default: `"data/jarvis_memory.db"`) | [`config_loader.py:38`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/config_loader.py#L38)<br>`config/config.yaml` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **REFERENCE** | Writable (Actor) | **VERIFIED** |
| **`DynamicReflexEngine`**<br>[`jarvis/core/reflex_engine.py:80`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/reflex_engine.py#L80) | `db_path = "data/jarvis_memory.db"`<br>or `self.memory.db_path` | `brain.py:289`<br>`MemoryManager.db_path` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Actor) | **VERIFIED** |
| **`TemporalHabitGraph`**<br>(Routine Transitions)<br>[`jarvis/core/cognition/habit_graph.py:57`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/cognition/habit_graph.py#L57) | `db_path: str` | `heartbeat.py:198`<br>`orchestrator.memory.db_path` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Actor client via queue) | **VERIFIED** |
| **`TemporalHabitGraph`**<br>(Decay & Pruning)<br>[`jarvis/core/cognition/habit_graph.py:340`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/cognition/habit_graph.py#L340) | `self.db_path` | Inherited from `TemporalHabitGraph.__init__` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Direct bypass) | **VERIFIED** |
| **`ObsidianContextSyncEngine`**<br>[`jarvis/core/memory/obsidian_sync.py:245`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/obsidian_sync.py#L245) | `db_path: Path \| str \| None = None`<br>(Default: `data/jarvis_memory.db`) | Module default / workspace relative | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Actor client via queue) | **VERIFIED** |
| **`PhotoManager`**<br>[`jarvis/skills/photo_manager.py:29`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/photo_manager.py#L29) | `DB_PATH = WORKSPACE_DIR / "data" / "jarvis_memory.db"` | Module constant `WORKSPACE_DIR` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Direct bypass) | **VERIFIED** |
| **`ReflexMacroCompiler`**<br>[`jarvis/skills/reflex_macro_compiler.py:61`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/reflex_macro_compiler.py#L61) | `db_path: str = "data/jarvis_memory.db"` | Skill signature default | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Direct bypass) | **VERIFIED** |
| **`SleepPhaseConsolidator`**<br>[`jarvis/core/memory/sleep_consolidator.py:179`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/sleep_consolidator.py#L179) | `db_path: str` | `interaction_memory.py:451`<br>`MemoryManager.db_path` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Direct bypass) | **VERIFIED** |
| **`TranscriptDistiller`**<br>[`jarvis/core/memory/transcript_distiller.py:102`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/transcript_distiller.py#L102) | `db_path: str` | `interaction_memory.py:466`<br>`MemoryManager.db_path` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Writable (Actor) | **VERIFIED** |
| **`HealthCheck`**<br>[`jarvis/core/health_check.py:166`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/health_check.py#L166) | `root / "data" / "jarvis_memory.db"` | Repository root relative | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Read-Only (Diagnostic probe) | **VERIFIED** |
| **`HUD Server` Photo Query**<br>[`jarvis/ui/server.py:1111`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/ui/server.py#L1111) | `str(DB_PATH)` | Imported from `photo_manager.DB_PATH` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Read-Only (HUD matrix query) | **VERIFIED** |
| **`VectorMemoryPool`**<br>[`jarvis/memory/ro_pool.py:32`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/memory/ro_pool.py#L32) | `db_path: str \| Path` | `MemoryManager.db_path` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Read-Only (`?mode=ro`, query_only=ON) | **VERIFIED** |
| **`ReadOnlyConnectionPool`**<br>[`jarvis/memory/ro_pool.py:350`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/memory/ro_pool.py#L350) | `db_path: Path` | `MemoryManager.db_path` | `C:\Users\Youssef\Projects\Jarvis_2.0\data\jarvis_memory.db` | **YES** | Read-Only (`?mode=ro`) | **VERIFIED** |

---

## 2. Complete Writer Ownership Census

Every writer in the repository is categorized under the standard architectural classification:

- **Category A:** Writes routed strictly through `SingleWriterActor`.
- **Category B:** Domain queue buffer followed by dispatch to `SingleWriterActor`.
- **Category C:** Direct writer on the canonical shared database (`data/jarvis_memory.db`).
- **Category D:** Direct writer on an independent separate database.
- **Category E:** Read-only connection (no mutation capabilities).

```mermaid
flowchart TD
    subgraph Clients["Write Entry Points"]
        C1["Core Memory Mixins\n(Facts, Interactions, Docs, Tasks)"]
        C2["DynamicReflexEngine\n(Habit Shortcuts)"]
        C3["TemporalHabitGraph\n(Routine Transitions)"]
        C4["ObsidianContextSyncEngine\n(Vault Chunks)"]
        C5["PhotoManager\n(Photo Metadata)"]
        C6["ReflexMacroCompiler\n(Compiled Macros)"]
        C7["TemporalHabitGraph.decay\n(Maintenance Pruning)"]
        C8["SleepPhaseConsolidator\n(Maintenance Clustering)"]
    end

    subgraph Queues["Domain Queues"]
        Q_Habit["_write_queue (size 1000)\nJarvisHabitGraphWriter"]
        Q_Obsidian["_db_queue\nJarvis-Obsidian-DBWriter"]
        Q_Photo["_db_queue\nPhotoDBWorker"]
    end

    subgraph Actor["SingleWriterActor Serialization"]
        SWA_Q["SingleWriterActor._queue\nFIFO Queue"]
        SWA_Thread["JarvisSingleWriter\nPersistent Worker Connection"]
    end

    subgraph Database["Physical File: data/jarvis_memory.db (WAL Mode)"]
        DB[(data/jarvis_memory.db)]
    end

    %% Category A
    C1 -->|execute_write| SWA_Q
    C2 -->|execute_write| SWA_Q
    
    %% Category B
    C3 -->|observe_action| Q_Habit
    Q_Habit -->|execute_write| SWA_Q
    C4 -->|batch_ops| Q_Obsidian
    Q_Obsidian -->|execute_write| SWA_Q

    SWA_Q --> SWA_Thread
    SWA_Thread -->|Single Persistent Connection| DB

    %% Category C Bypasses
    C5 -->|enqueue| Q_Photo
    Q_Photo -->|Direct sqlite3.connect| DB
    C6 -->|Direct sqlite3.connect| DB
    C7 -->|Direct sqlite3.connect| DB
    C8 -->|Direct sqlite3.connect\n(VDBE Abort Hook)| DB

    classDef catA fill:#1f3d24,stroke:#38a169,stroke-width:2px;
    classDef catB fill:#2a3a4a,stroke:#3182ce,stroke-width:2px;
    classDef catC fill:#4a2828,stroke:#e53e3e,stroke-width:2px;
    classDef sharedDB fill:#1a202c,stroke:#a0aec0,stroke-width:2px;

    class C1,C2 catA;
    class C3,C4,Q_Habit,Q_Obsidian catB;
    class C5,C6,C7,C8,Q_Photo catC;
    class DB sharedDB;
```

### Detailed Ownership Breakdown:

1. **Category A (SingleWriterActor Serialized):**
   - **`FactsManagerMixin`:** Writes atomic user facts (`facts`, `facts_vec`). Pre-computes ONNX dense vectors outside write transaction; calls `self.execute_write(_write)`.
   - **`InteractionMemoryMixin`:** Logs conversation turns and episodic lessons (`interactions`, `lessons`). Calls `self.execute_write(_write)`.
   - **`DocumentVaultMixin`:** Writes staged documents and text chunks (`documents`, `document_chunks`, `documents_fts`). Calls `self.execute_write(_write)`.
   - **`TasksCalendarMixin`:** Inserts and completes tasks, reminders, and calendar events (`tasks`, `calendar_events`). Calls `self.execute_write(_write)`.
   - **`TemporalGraphMixin`:** Records bi-temporal knowledge graph edges and undo actions (`temporal_edges`, `action_journal`). Calls `self.execute_write(_write)`.
   - **`DynamicReflexEngine`:** Promotes successful tool trajectories to fast reflexes (`learned_habits`). Calls `SingleWriterActor.execute_or_fallback()`.
   - **`TranscriptDistiller`:** Records distilled conversation facts and marks processed interaction IDs (`distilled_topics`, `schema_meta`). Calls `SingleWriterActor.execute_or_fallback()`.

2. **Category B (Domain Queue Dispatching to SingleWriterActor):**
   - **`TemporalHabitGraph` (Transitions):** Pushes window focus and tool events to `self._write_queue` (`maxsize=1000`). Dedicated worker thread `JarvisHabitGraphWriter` drains queue and calls `SingleWriterActor.get_or_create(self.db_path).execute_write(_write)`.
   - **`ObsidianContextSyncEngine`:** File scanner pushes note chunks to `self._db_queue`. Dedicated worker thread `_db_writer_worker` batches up to 50 operations and calls `SingleWriterActor.get_or_create(self.db_path).execute_write(...)`.

3. **Category C (Direct Writers on Shared Canonical Database):**
   - **`PhotoManager`:** Pushes photo records to `_db_queue`. Dedicated worker thread `PhotoDBWorker` executes `sqlite3.connect(target_db)` and commits directly.
   - **`ReflexMacroCompiler`:** Directly executes `sqlite3.connect(db_path, timeout=5.0)` on the caller thread to register or update reflex macros (`compiled_reflex_macros`).
   - **`TemporalHabitGraph.apply_exponential_decay()`:** Directly opens `self._get_connection()` on caller thread and executes batch `DELETE`/`UPDATE` operations in `with conn:`.
   - **`SleepPhaseConsolidator`:** Opens an interruptible direct connection (`_open_interruptible_connection`) with `isolation_level=None` and executes multi-statement maintenance passes with VDBE abort hooks.

4. **Category D (Direct Writers on a Separate Database):**
   - **Zero production occurrences.** Jarvis 2.0 maintains zero auxiliary or independent production databases on disk. All persistent application state targets `data/jarvis_memory.db`.

5. **Category E (Read-Only Connections):**
   - **`HealthCheck`:** Ephemeral inspection connection verifying WAL mode, integrity, and table count.
   - **`HUD Server Photo Matrix`:** Ephemeral connection querying `photos` table for UI display.
   - **`VectorMemoryPool`:** Pool of worker threads maintaining read-only connections with `?mode=ro` and `PRAGMA query_only = ON`.
   - **`ReadOnlyConnectionPool`:** Thread-local cache of read-only connections with `?mode=ro` and `isolation_level=None`.

---

## 3. Connection PRAGMA & Timeout Matrix

> [!IMPORTANT]
> **Technical Principles for SQLite Connection Evaluation:**
> 1. **WAL Persistence vs. Connection-Local Pragmas:** `PRAGMA journal_mode=WAL` is a persistent database header property. Once WAL mode is established on `data/jarvis_memory.db`, subsequent connections operate in WAL mode automatically even if they do not execute `PRAGMA journal_mode=WAL` explicitly. In contrast, `PRAGMA synchronous` is connection-local and does **not** persist across independent connections; an unconfigured connection defaults to compile-time SQLite defaults (`synchronous=2` / `FULL`) and does **not** inherit `NORMAL` from `SingleWriterActor`.
> 2. **Busy Timeout Defaults:** In Python 3.12, `sqlite3.connect(..., timeout=5.0)` configures SQLite's internal busy handler to wait up to 5.0 seconds (5000 ms) before throwing `sqlite3.OperationalError: database is locked`. An absent `PRAGMA busy_timeout` statement does not indicate an un-timed connection if Python's `timeout=5.0` argument is present.
> 3. **wal_autocheckpoint:** Autocheckpoint thresholds (default 1,000 pages) are per-connection in SQLite. Committing threads that write past page 1,000 execute synchronous checkpointing on their own thread unless configured otherwise.

| Subsystem Connection | Target Path | Connection Call | Python Timeout | `journal_mode` | `synchronous` (Empirical) | `busy_timeout` | `wal_autocheckpoint` | Transaction Initiation | Commit / Rollback Mechanism |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
| **`SingleWriterActor` Persistent Worker** | `data/jarvis_memory.db` | `sqlite3.connect(str(self.db_path), check_same_thread=False)` | 5.0s (default) | Explicit `WAL` | Explicit `NORMAL` (1) | Explicit `5000` | SQLite Default (1000 pages) | Python context manager: `with local_conn:` | Auto-commit on `__exit__`; rollback on exception |
| **`PhotoManager` Worker** | `data/jarvis_memory.db` | `sqlite3.connect(target_db, check_same_thread=False)` | 5.0s (default) | Explicit `WAL`<br>(in `_init_photos_table`) | Default `FULL` (2)<br>(Unconfigured connection) | Explicit `5000`<br>(in `_init_photos_table`) | SQLite Default (1000 pages) | Implicit Python transaction upon DML | Explicit `conn.commit()`; uncommitted changes rollback on `conn.close()` |
| **`ReflexMacroCompiler`** | `data/jarvis_memory.db` | `sqlite3.connect(db_path, timeout=5.0)` | 5.0s (explicit) | Inherited `WAL`<br>(Persistent DB header) | Default `FULL` (2)<br>(Unconfigured connection) | 5000ms (configured via Python timeout) | SQLite Default (1000 pages) | Implicit Python transaction upon DML | Explicit `conn.commit()`; uncommitted changes rollback on `conn.close()` |
| **`TemporalHabitGraph.decay`** | `data/jarvis_memory.db` | `sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)` | 5.0s (explicit) | Explicit `WAL` | Explicit `NORMAL` (1) | Explicit `5000` | SQLite Default (1000 pages) | Python context manager: `with conn:` | Auto-commit on `__exit__`; rollback on exception |
| **`SleepPhaseConsolidator`** | `data/jarvis_memory.db` | `sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)` | 5.0s (explicit) | Explicit `WAL` | Explicit `NORMAL` (1) | Explicit `5000` | SQLite Default (1000 pages) | Explicit `rw_conn.execute("BEGIN IMMEDIATE;")` (micro-batches) or autocommit (Pass A/B) | Explicit `rw_conn.execute("COMMIT;")`; explicit `ROLLBACK;` on error; `conn.close()` in finally |

---

## 4. Canonical Database Bypass Analysis (Category C)

Each direct writer on `data/jarvis_memory.db` was evaluated to assess its transaction duration, concurrency collision profile, error handling, and migration feasibility.

### 4.1. Photo Manager (`jarvis/skills/photo_manager.py`)
- **Table Affected:** `photos`
- **Execution Context:** Asynchronous background thread `PhotoDBWorker` draining `_db_queue`.
- **Transaction Duration:** Sub-millisecond (single-row `INSERT ... ON CONFLICT DO UPDATE` or `UPDATE caption`).
- **Overlap with Actor:** High. Photo indexing occurs in background while interactive turns log memories.
- **Lock Contention Risk:** Low duration per operation, but creates periodic write lock acquisition contention against `SingleWriterActor`.
- **Durability Policy Consideration:** Currently operates under `synchronous=FULL` (2). Migrating to `SingleWriterActor` would transition photo indexing to `synchronous=NORMAL` (1).
- **Migration Feasibility:** **HIGH (Candidate for Migration, Pending Durability Decision).**
  - Moving the execution callable from `_db_worker_loop()` to `SingleWriterActor.get_or_create(DB_PATH).execute_write(...)` eliminates independent connection contention.
  - The project must explicitly decide whether photo indexing accepts `NORMAL` durability (where recent metadata writes could theoretically be lost on an immediate OS crash before a WAL checkpoint) before executing the migration.

### 4.2. Reflex Macro Compiler (`jarvis/skills/reflex_macro_compiler.py`)
- **Table Affected:** `compiled_reflex_macros`
- **Execution Context:** Synchronous caller thread during skill compilation (`compile_workflow_reflex`) or after macro execution (`_record_macro_outcome`).
- **Transaction Duration:** Sub-millisecond (single-row `INSERT OR REPLACE` or `UPDATE success_count`).
- **Overlap with Actor:** Moderate. Triggered during rapid macro execution while dialogue turns are recorded.
- **Lock Contention Risk:** Low duration, but can fail closed if `SingleWriterActor` holds an active write lock past 5.0s.
- **Durability Policy Consideration:** Currently operates under `synchronous=FULL` (2). Migrating to `SingleWriterActor` would transition macro compiles and telemetry to `synchronous=NORMAL` (1).
- **Migration Feasibility:** **HIGH (Candidate for Migration, Pending Durability Decision).**
  - Moving macro registrations and telemetry updates to `SingleWriterActor.get_or_create(db_path).execute_write(...)` centralizes writer lock ownership, provided `NORMAL` durability is formally accepted for compiled reflex macros.

### 4.3. Temporal Habit Graph Decay (`jarvis/core/cognition/habit_graph.py`)
- **Table Affected:** `action_transitions`
- **Execution Context:** Called during sleep consolidation or periodic background decay passes via `apply_exponential_decay()`.
- **Transaction Duration:** ~1.0 to 10.0 ms (loops over in-memory rows and executes batch `DELETE`/`UPDATE` statements).
- **Overlap with Actor:** Moderate. Can run concurrently with dialogue turns during background maintenance.
- **Lock Contention Risk:** Moderate. Holds write lock across multi-row updates.
- **Migration Feasibility:** **HIGH (Safe Migration).**
  - Row counts in `action_transitions` are small (~117 rows currently). Wrapping the batch update inside a callable passed to `SingleWriterActor` guarantees serialized execution without starving callers.

### 4.4. Sleep Phase Consolidator (`jarvis/core/memory/sleep_consolidator.py`)
- **Tables Affected:** `facts`, `facts_vec`, `compiled_reflex_macros`, `action_journal`.
- **Execution Context:** Dedicated maintenance thread triggered during system idle periods.
- **Transaction Duration:** **Long-running** (hundreds of milliseconds to multiple seconds across Pass A, Pass B, Pass C, and fact micro-batches).
- **Overlap with Actor:** High probability of collision when a user initiates speech ("barge-in") during an active sleep consolidation pass.
- **Cancellation & Rollback Safety Analysis:**
  - **Connection Configuration:** `sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)`. The `isolation_level=None` setting enables autocommit mode unless manual transaction control (`BEGIN`) is explicitly issued.
  - **Progress Handler Hook:** Configured with `conn.set_progress_handler(_vdbe_progress_hook, progress_steps)` where `progress_steps=250` in Pass A/B/C and `5000` in micro-batches.
    > [!NOTE]
    > **Empirical Qualification:** The reported `< 1.0 ms` lock release time is an empirical measurement from benchmarks under test hardware, not a hard SQLite API guarantee; wall-clock latency depends on the execution time of up to 250 VDBE opcodes and host OS thread scheduling.
  - **Micro-Batch Interruption Path ([`sleep_consolidator.py:350-384`](file:///c:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/sleep_consolidator.py#L350-L384)):
    1. Executes `rw_conn.execute("BEGIN IMMEDIATE;")`.
    2. Executes `DELETE FROM facts_vec` and `DELETE FROM facts` for 20 IDs.
    3. If user activity occurs, `_progress_abort_guard()` returns 1, raising `sqlite3.OperationalError: interrupted`.
    4. **Empirical Observation vs. SQLite Specification:** In the specific DML operations tested on Python 3.12 / SQLite 3.49.1, an interrupted statement was observed to leave `conn.in_transaction == False`. However, this is an empirical observation for the tested operations, not a universal SQLite specification guarantee. Upstream SQLite documentation notes that `SQLITE_INTERRUPT` may undo only the interrupted statement while retaining the surrounding transaction, or may roll back the entire transaction depending on the statement type and when the interruption occurs. Applications must not assume automatic transaction termination; they must explicitly inspect transaction state and roll back when active.
    5. The `except (sqlite3.OperationalError, sqlite3.DatabaseError)` block explicitly attempts `rw_conn.execute("ROLLBACK;")`.
    6. **Rollback Ordering & Safe Sequence Invariant:**
       - As verified in diagnostic tests, if `rw_conn.execute("ROLLBACK;")` is attempted while the progress handler remains active and still returning 1, the `ROLLBACK` statement itself is interrupted by SQLite.
       - The safer, production-grade cancellation sequence is:
         1. Catch cancellation exception (`sqlite3.OperationalError: interrupted`).
         2. Disable progress handler immediately (`rw_conn.set_progress_handler(None, 0)`).
         3. Inspect `rw_conn.in_transaction` to check whether the transaction remains active.
         4. If still active, execute `rw_conn.execute("ROLLBACK;")`.
         5. Distinguish expected inactive transaction (`cannot rollback - no transaction is active`) from unexpected errors (which must be logged).
         6. Close the connection.
    7. In the current implementation, `finally:` executes `rw_conn.set_progress_handler(None, 0)` and `rw_conn.close()`, guaranteeing that all file locks are released unconditionally upon exit.
  - **Pass A & Pass B Interruption Path ([`sleep_consolidator.py:388-566, 662-684`](file:///c:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/sleep_consolidator.py#L388-L566)):
    1. No explicit `BEGIN` is issued; each `conn.execute()` statement executes in autocommit mode.
    2. An interruption aborts the active statement; no multi-statement transaction remains pending.
    3. Caught by `except (sqlite3.OperationalError, sqlite3.DatabaseError) as opt_err`.
    4. `finally:` executes `rw_conn.set_progress_handler(None, 0)` and `rw_conn.close()`, releasing file locks unconditionally.

### 4.5. Actor Callback Feasibility for Maintenance Operations

To determine whether `SleepPhaseConsolidator` operations can be integrated into the actor without stalling interactive memory writes, the callback mechanics of `SingleWriterActor` were empirically tested on this platform.

#### Key Verification Findings:
1. **Dynamic Progress Handler Support:** `SingleWriterActor.execute_write(callable)` passes the worker connection `local_conn` to the user callable. The callback can safely install a temporary `local_conn.set_progress_handler(_hook, N)` before executing statements, and clear it via `set_progress_handler(None, 0)` in a `finally:` block.
2. **Transaction Rollback on Interruption:** In `SingleWriterActor._worker_loop()`, callables execute inside `with local_conn: result = func(local_conn)`. When an interruption error (`sqlite3.OperationalError: interrupted`) is raised by the progress hook:
   - Python's context manager `__exit__` catches the exception and executes a transaction rollback.
   - The callable's `finally:` block clears the progress handler so subsequent writes are unaffected.
   - The worker loop catches the exception, appends it to `err_box`, signals the caller, and returns to the queue loop.
   - The actor worker thread remains `running` and ready to process subsequent interactive turns.
3. **Critical Callback Invariant:**
   > [!CAUTION]
   > **Interruption Propagation Invariant:** The actor maintenance callback **must never swallow the interruption exception and return normally**. Swallowing the exception would allow Python's outer context manager `with local_conn:` to treat the block as successful and commit partial mutations that were intended to be rolled back. The callback must let the exception propagate out to the actor's context manager.
4. **Chunked Maintenance Design Feasibility & Necessary Qualifications:**
   - **Plausible Engineering Target, Not Production Guarantee:** The proposed 20–50 entity chunk size and ~1–3 ms interactive wait time are plausible engineering targets based on synthetic measurements, not established production guarantees.
   - Three operational caveats must be formally addressed:
     1. **Transaction Atomicity:** Each chunk commits independently. Splitting a previously unified maintenance pass changes the atomicity boundary of the entire consolidation operation; partial completion must be verified as safe, recoverable, and idempotent.
     2. **FIFO Scheduling Delays:** Interactive writes can still wait behind previously queued work in the actor's FIFO queue, even when individual maintenance chunks are short.
     3. **Workload Variance:** FTS5 virtual table synchronization, dense vector indexing via `sqlite-vec`, and cascading foreign keys may take longer than microbenchmark measurements under large production databases.
   - **Conclusion:** Wholesale submission of the multi-second consolidation pass to `SingleWriterActor` is an architectural defect, but **chunked dispatch through `SingleWriterActor` with per-chunk progress handlers is technically and semantically feasible**, provided partial completion is proven idempotent.

---

## 5. Separate Database Inventory

- **Verified Finding:** There are **zero** separate persistent SQLite database files in Jarvis 2.0.
- All persistent relational state is unified inside `data/jarvis_memory.db`.
- The following non-persistent/test databases exist and are verified as isolated:
  1. `:memory:` connections: Used only in `ro_pool.py` and `sqlite_base.py` as capability probes for `sqlite-vec` extension loading.
  2. `test_*.db` temporary files: Generated in `tests/` and benchmark harnesses.

---

## 6. Outstanding Uncertainties

1. **Autocheckpoint Tail Latency Impact:** While all connections use `journal_mode=WAL` and `synchronous=NORMAL`, neither `SingleWriterActor` nor the direct writers explicitly configure `PRAGMA wal_autocheckpoint`. Each connection defaults to 1,000 pages. If `SleepPhaseConsolidator` or a large batch write pushes the WAL past 1,000 pages, that specific connection performs an autocheckpoint on its own thread, contributing to observed tail latency (P99).
2. **Double-Queued Memory Buffering:** In `TemporalHabitGraph`, events are buffered in `_write_queue` (`maxsize=1000`) before dispatching to `SingleWriterActor._queue` (unbounded). Under high-frequency Win32 events, backpressure is enforced at the domain queue (`queue.Full` drops events), but once dispatched, items sit unbounded in the actor queue.

---

## 7. Recommended ADR-001 Scope Corrections

Based on the empirical findings of this audit, the scope and assumptions in [`ADR-001-sqlite-single-writer-wal.md`](../adr/ADR-001-sqlite-single-writer-wal.md) and [`ADR-001-decision-memo.md`](../adr/ADR-001-decision-memo.md) should be updated before ratification:

1. **Correct Database Scope Definition:**
   - *Current ADR-001 Text:* "Core persistent memory databases (`data/memory.db`, `data/facts.db`, `data/habits.db`), reflex action caching (`data/reflex.db`), and Obsidian vault sync state (`data/obsidian_sync.db`)."
   - *Required Correction:* Update scope to state: **"All write operations on the unified persistent database `data/jarvis_memory.db`."**
2. **Explicit Qualification of Writer Exclusivity:**
   - *Current ADR-001 Text:* Assumes universal write serialization via `SingleWriterActor`.
   - *Required Correction:* Document the three distinct writer policies:
     - **Policy 1 (Interactive In-Process Writes):** Core memory mixins, dynamic reflexes, and transcript distillation serialize strictly through `SingleWriterActor`.
     - **Policy 2 (Domain-Queued Ingestion):** Habit transitions and Obsidian notes buffer in domain queues before serialized dispatch to `SingleWriterActor`.
     - **Policy 3 (Long-Running Maintenance & Bypasses):** Document `SleepPhaseConsolidator` as an explicitly recognized direct writer using interruptible micro-transactions with VDBE abort hooks, and document `PhotoManager`, `ReflexMacroCompiler`, and habit decay as migration targets to achieve complete writer exclusivity.
3. **ADR-001 Status:**
   - Maintain **`PROPOSED`** status pending developer review of the three policies.

---

## 8. Implementation Order & Prerequisite Design Questions

Before executing production migrations, the transition must follow a staged, risk-bounded sequence:

### Recommended Migration Sequence:

| Order | Subsystem / Component | Architectural Decision |
| :---: | :--- | :--- |
| **1** | **`ReflexMacroCompiler`** | Candidate for actor migration; formally resolve durability policy first (`FULL` vs `NORMAL`). |
| **2** | **`PhotoManager`** | Candidate for actor migration; formally resolve durability policy first (`FULL` vs `NORMAL`). |
| **3** | **`TemporalHabitGraph.apply_exponential_decay`** | Strong candidate for actor migration; wrap batch updates in actor callable. |
| **4** | **`SleepPhaseConsolidator`** | Design an interruptible, chunked actor implementation with per-chunk progress handlers. |
| **5** | **`HabitGraph` & `Obsidian` Queues** | Retain upstream domain queues to preserve producer isolation and backpressure. |

### Three Prerequisite Design Questions Before Migration:
1. **Durability Policy Selection:** What synchronization policy (`synchronous=NORMAL` vs `synchronous=FULL`) should apply to `PhotoManager` and `ReflexMacroCompiler`? Migrating them to `SingleWriterActor` would alter their effective durability guarantees; the project must explicitly decide whether their workloads accept `NORMAL` synchronization before designing connection or actor configurations.
2. **Recoverability & Idempotency:** How will `SleepPhaseConsolidator` preserve recoverability and idempotency if a multi-chunk maintenance pass is aborted halfway through by user activity?
3. **Queue Admission & Starvation Gating:** What admission control or interleaving policy will prevent a burst of maintenance chunks from delaying high-priority interactive dialogue writes in `SingleWriterActor._queue`?

