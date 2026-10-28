"""SQLite WAL connection management, single-writer serialization, and database schema initialization."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.Memory.SqliteBase")


class SingleWriterActor:
    """Serializes all write operations (INSERT, UPDATE, DELETE) through an asynchronous queue worker.
    Guarantees zero SQLITE_BUSY write lock contention and atomic transaction sequencing.
    Enforces clean lifecycle state management, timeout cancellation, and leak-free shutdown.
    """

    STATE_RUNNING = "running"
    STATE_STOPPING = "stopping"
    STATE_STOPPED = "stopped"

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._queue: queue.Queue[tuple[Callable[[sqlite3.Connection], Any], threading.Event, list, list, threading.Event, dict, threading.Lock] | None] = queue.Queue()
        self._state = self.STATE_RUNNING
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="JarvisSingleWriter")
        self._thread.start()

    @property
    def _running(self) -> bool:
        with self._state_lock:
            return self._state == self.STATE_RUNNING and self._thread.is_alive()

    @_running.setter
    def _running(self, val: bool) -> None:
        with self._state_lock:
            self._state = self.STATE_RUNNING if val else self.STATE_STOPPED

    @property
    def state(self) -> str:
        """Returns the current actor lifecycle state, never reporting STOPPED if worker is still running."""
        with self._state_lock:
            if self._state == self.STATE_STOPPED and self._thread.is_alive():
                return self.STATE_STOPPING
            return self._state

    def _create_conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute("PRAGMA mmap_size=268435456;")
        c.execute("PRAGMA cache_size=-64000;")
        c.execute("PRAGMA temp_store=MEMORY;")
        c.row_factory = sqlite3.Row
        try:
            import sqlite_vec
            c.enable_load_extension(True)
            sqlite_vec.load(c)
        except Exception:
            pass
        return c

    def _worker_loop(self) -> None:
        key = str(self.db_path.resolve())
        is_temp = "tmp" in key.lower() or "temp" in key.lower() or self.db_path.name.startswith("test_")

        conn: sqlite3.Connection | None = None
        if not is_temp:
            try:
                conn = self._create_conn()
            except Exception as e:
                logger.debug("SingleWriterActor persistent conn init note: %s", e)

        while True:
            with self._state_lock:
                if self._state == self.STATE_STOPPED:
                    break
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._state_lock:
                    if self._state != self.STATE_RUNNING and self._queue.empty():
                        break
                continue

            if item is None:
                self._queue.task_done()
                break

            func, done_evt, res_box, err_box, cancel_evt, status, item_lock = item

            # Check if caller already timed out or cancelled this write under mutex
            with item_lock:
                if cancel_evt.is_set() or status.get("phase") == "cancelled_before_execution":
                    status["phase"] = "cancelled_before_execution"
                    done_evt.set()
                    self._queue.task_done()
                    continue
                # Atomically commit to executing this item so caller cannot cancel before execution
                status["phase"] = "executing"

            local_conn = conn
            opened_temp = False
            if local_conn is None:
                try:
                    local_conn = self._create_conn()
                    opened_temp = True
                except Exception as ce:
                    with item_lock:
                        err_box.append(ce)
                        status["phase"] = "failed"
                    done_evt.set()
                    self._queue.task_done()
                    continue

            try:
                with local_conn:
                    result = func(local_conn)
                with item_lock:
                    res_box.append(result)
                    status["phase"] = "completed"
            except Exception as e:
                with item_lock:
                    err_box.append(e)
                    status["phase"] = "failed"
            finally:
                if opened_temp and local_conn is not None:
                    try:
                        local_conn.close()
                    except Exception:
                        pass
                done_evt.set()
                self._queue.task_done()

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def execute_write(self, func: Callable[[sqlite3.Connection], Any], timeout: float = 30.0) -> Any:
        with self._state_lock:
            if self._state != self.STATE_RUNNING:
                raise RuntimeError(f"SingleWriterActor is {self._state}; cannot accept new write operations.")

        done_evt = threading.Event()
        cancel_evt = threading.Event()
        res_box: list = []
        err_box: list = []
        status: dict = {"phase": "pending"}
        item_lock = threading.Lock()
        self._queue.put((func, done_evt, res_box, err_box, cancel_evt, status, item_lock))

        if not done_evt.wait(timeout=timeout):
            # Check execution phase atomically under mutex to distinguish pre-execution cancellation vs in-flight execution
            with item_lock:
                if done_evt.is_set() and status.get("phase") == "completed":
                    if err_box:
                        raise err_box[0]
                    return res_box[0] if res_box else None

                if status.get("phase") == "pending":
                    cancel_evt.set()
                    status["phase"] = "cancelled_before_execution"
                    err = TimeoutError("Single-writer queue timeout exceeded before write execution started.")
                    err.cancelled_before_execution = True
                    err.timed_out_in_flight = False
                    err.transaction_outcome_unknown = False
                    err.transaction_outcome = "not_executed"
                    raise err
                else:
                    err = TimeoutError("Single-writer active write timed out during in-flight execution.")
                    err.cancelled_before_execution = False
                    err.timed_out_in_flight = True
                    err.transaction_outcome_unknown = True
                    err.transaction_outcome = "unknown"
                    raise err

        if err_box:
            raise err_box[0]
        return res_box[0] if res_box else None

    async def execute_write_async(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        return await asyncio.to_thread(self.execute_write, func)

    def _drain_and_reject_pending(self) -> None:
        """Drains remaining queue items and sets error on pending callers."""
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    _, done_evt, _, err_box, cancel_evt, status, item_lock = item
                    with item_lock:
                        cancel_evt.set()
                        status["phase"] = "cancelled_before_execution"
                        err_box.append(RuntimeError("SingleWriterActor shutdown before write could execute."))
                        done_evt.set()
                self._queue.task_done()
            except (queue.Empty, ValueError):
                break

    def stop(self, drain: bool = True, timeout: float = 3.0) -> None:
        """Stops the single-writer worker loop.

        Args:
            drain: If True, waits for queued items to be processed before shutting down.
                   If False, cancels and rejects queued items immediately.
            timeout: Maximum wait time for worker thread to terminate.
        """
        with self._state_lock:
            if self._state == self.STATE_STOPPED:
                return
            self._state = self.STATE_STOPPING

        if not drain:
            self._drain_and_reject_pending()

        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

        with self._state_lock:
            if not self._thread.is_alive():
                self._state = self.STATE_STOPPED
                self._drain_and_reject_pending()
            else:
                logger.error(
                    "SingleWriterActor worker thread failed to terminate within %.2fs timeout; state remains %s.",
                    timeout,
                    self.STATE_STOPPING,
                )


class SqliteBaseMixin:
    """Provides SQLite database connection management, pool access, and schema initialization."""

    @contextlib.contextmanager
    def _get_connection(self):
        with self._write_lock:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA mmap_size=268435456;")
            conn.execute("PRAGMA cache_size=-64000;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.row_factory = sqlite3.Row
            try:
                import sqlite_vec
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception:
                pass
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    @contextlib.contextmanager
    def _get_ro_connection(self):
        """Thread-local read-only SQLite connection from ReadOnlyConnectionPool."""
        from jarvis.memory.ro_pool import get_ro_connection
        with get_ro_connection(self.db_path) as conn:
            yield conn

    def execute_write(self, func: Callable[[sqlite3.Connection], Any], timeout: float = 30.0) -> Any:
        """Executes a write operation serialized through the Single-Writer Actor queue."""
        return self._writer_actor.execute_write(func, timeout=timeout)

    async def execute_write_async(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        """Asynchronously dispatches a write operation through the Single-Writer Actor queue."""
        return await self._writer_actor.execute_write_async(func)

    def get_vector_pool(self, pool_size: int = 4):
        """Retrieves or lazily instantiates the async VectorMemoryPool."""
        if self._ro_vector_pool is None:
            with self._ro_pool_lock:
                if self._ro_vector_pool is None:
                    from jarvis.memory.ro_pool import VectorMemoryPool
                    self._ro_vector_pool = VectorMemoryPool(self.db_path, pool_size=pool_size)
        return self._ro_vector_pool

    async def get_vector_pool_async(self, pool_size: int = 4):
        """Retrieves and asynchronously pre-warms the VectorMemoryPool."""
        pool = self.get_vector_pool(pool_size=pool_size)
        await pool.initialize()
        return pool

    def close(self) -> None:
        """Cleans up writer actor and read-only pool connections."""
        if hasattr(self, "_ro_vector_pool") and self._ro_vector_pool is not None:
            try:
                self._ro_vector_pool.teardown()
            except Exception:
                pass
            self._ro_vector_pool = None
        if hasattr(self, "_writer_actor") and self._writer_actor:
            self._writer_actor.stop()
        try:
            from jarvis.memory.ro_pool import ReadOnlyConnectionPool
            ReadOnlyConnectionPool.get_instance().close_db(self.db_path)
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()

    def _init_db(self):
        """Initializes database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Facts & User Profile
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT DEFAULT 'general',
                    key TEXT UNIQUE,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Tasks & Reminders
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    description TEXT NOT NULL,
                    due_date TEXT,
                    is_completed BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Interaction History
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_input TEXT NOT NULL,
                    jarvis_reply TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Calendar Events
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    location TEXT,
                    is_cancelled BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Staged Documents (Context-Gated Intake)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS staged_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT,
                    file_size INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'PENDING_CONTEXT',
                    user_context TEXT,
                    staged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP
                )
            """)
            # Knowledge Documents Archive
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    summary TEXT,
                    raw_content TEXT,
                    file_path TEXT,
                    source_provenance TEXT NOT NULL DEFAULT 'unknown',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Idempotent Migration: Ensure source_provenance column exists for pre-existing databases
            cursor.execute("PRAGMA table_info(documents)")
            doc_cols = {
                row["name"] if hasattr(row, "__getitem__") and "name" in row.keys() else row[1]
                for row in cursor.fetchall()
            }
            if "source_provenance" not in doc_cols:
                cursor.execute("ALTER TABLE documents ADD COLUMN source_provenance TEXT NOT NULL DEFAULT 'unknown'")
                logger.info("Migrated SQLite schema: added source_provenance column to documents table.")
            # Episodic Lessons & Self-Correction Buffer (Tier 3 Reflexion)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS episodic_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    trigger_context TEXT NOT NULL,
                    failure_signature TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    hit_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tool_name, failure_signature)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_lessons_tool ON episodic_lessons(tool_name)")

            # 1. Bi-Temporal Knowledge Graph Edges (Replaces flat facts with bi-temporal validity)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_edges (
                    edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject         TEXT NOT NULL COLLATE NOCASE,
                    predicate       TEXT NOT NULL COLLATE NOCASE,
                    object          TEXT NOT NULL,
                    is_functional   INTEGER NOT NULL DEFAULT 1,
                    confidence      REAL NOT NULL DEFAULT 1.0,
                    access_count    INTEGER NOT NULL DEFAULT 1,
                    decay_lambda    REAL NOT NULL DEFAULT 0.01,
                    valid_from      REAL NOT NULL,
                    valid_until     REAL DEFAULT NULL,
                    last_accessed   REAL NOT NULL,
                    superseded_by   INTEGER DEFAULT NULL REFERENCES knowledge_edges(edge_id),
                    source_context  TEXT DEFAULT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_kg_active_lookup 
                ON knowledge_edges(subject, predicate) 
                WHERE valid_until IS NULL
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_kg_supersede_functional
                BEFORE INSERT ON knowledge_edges
                WHEN NEW.is_functional = 1 AND NEW.valid_until IS NULL
                BEGIN
                    UPDATE knowledge_edges
                    SET valid_until = NEW.valid_from
                    WHERE subject = NEW.subject
                      AND predicate = NEW.predicate
                      AND valid_until IS NULL
                      AND object != NEW.object;
                END
            """)

            # 2. Time-Conditioned Markov Habit Graph
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS habit_transitions (
                    src_event       TEXT NOT NULL,
                    dst_event       TEXT NOT NULL,
                    hour_bucket     INTEGER NOT NULL,
                    is_weekend      INTEGER NOT NULL,
                    decayed_weight  REAL NOT NULL DEFAULT 1.0,
                    sample_count    INTEGER NOT NULL DEFAULT 1,
                    last_observed   REAL NOT NULL,
                    PRIMARY KEY (src_event, dst_event, hour_bucket, is_weekend)
                ) WITHOUT ROWID
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_habit_predict 
                ON habit_transitions(src_event, hour_bucket, is_weekend, decayed_weight DESC)
            """)

            # 3. Level-1 Reversible Action Undo Journal
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS action_journal (
                    action_id       TEXT PRIMARY KEY,
                    timestamp       REAL NOT NULL,
                    skill_name      TEXT NOT NULL,
                    risk_level      INTEGER NOT NULL DEFAULT 1,
                    inverse_skill   TEXT NOT NULL,
                    inverse_payload TEXT NOT NULL,
                    reverted        INTEGER NOT NULL DEFAULT 0
                )
            """)

            # 4. Core User Preferences (Tier 1 Sovereign Operational Profile)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    pref_key TEXT PRIMARY KEY NOT NULL,
                    pref_value TEXT NOT NULL,
                    dimension_id TEXT NOT NULL,
                    locked_by_sir INTEGER NOT NULL DEFAULT 1,
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ) WITHOUT ROWID
            """)

            # SQLite FTS5 Virtual Tables & Real-Time Sync Triggers
            try:
                # Facts FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                        category,
                        key,
                        value,
                        content='facts',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                        INSERT INTO facts_fts(rowid, category, key, value)
                        VALUES (new.id, new.category, new.key, new.value);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                        INSERT INTO facts_fts(facts_fts, rowid, category, key, value)
                        VALUES ('delete', old.id, old.category, old.key, old.value);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
                        INSERT INTO facts_fts(facts_fts, rowid, category, key, value)
                        VALUES ('delete', old.id, old.category, old.key, old.value);
                        INSERT INTO facts_fts(rowid, category, key, value)
                        VALUES (new.id, new.category, new.key, new.value);
                    END;
                """)

                # Documents FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                        title,
                        category,
                        summary,
                        raw_content,
                        content='documents',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                        INSERT INTO documents_fts(rowid, title, category, summary, raw_content)
                        VALUES (new.id, new.title, new.category, new.summary, new.raw_content);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                        INSERT INTO documents_fts(documents_fts, rowid, title, category, summary, raw_content)
                        VALUES ('delete', old.id, old.title, old.category, old.summary, old.raw_content);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                        INSERT INTO documents_fts(documents_fts, rowid, title, category, summary, raw_content)
                        VALUES ('delete', old.id, old.title, old.category, old.summary, old.raw_content);
                        INSERT INTO documents_fts(rowid, title, category, summary, raw_content)
                        VALUES (new.id, new.title, new.category, new.summary, new.raw_content);
                    END;
                """)

                # Interactions FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS interactions_fts USING fts5(
                        user_input,
                        jarvis_reply,
                        content='interactions',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS interactions_ai AFTER INSERT ON interactions BEGIN
                        INSERT INTO interactions_fts(rowid, user_input, jarvis_reply)
                        VALUES (new.id, new.user_input, new.jarvis_reply);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS interactions_ad AFTER DELETE ON interactions BEGIN
                        INSERT INTO interactions_fts(interactions_fts, rowid, user_input, jarvis_reply)
                        VALUES ('delete', old.id, old.user_input, old.jarvis_reply);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS interactions_au AFTER UPDATE ON interactions BEGIN
                        INSERT INTO interactions_fts(interactions_fts, rowid, user_input, jarvis_reply)
                        VALUES ('delete', old.id, old.user_input, old.jarvis_reply);
                        INSERT INTO interactions_fts(rowid, user_input, jarvis_reply)
                        VALUES (new.id, new.user_input, new.jarvis_reply);
                    END;
                """)

                # Episodic Lessons FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS episodic_lessons_fts USING fts5(
                        tool_name,
                        trigger_context,
                        failure_signature,
                        lesson,
                        content='episodic_lessons',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS episodic_lessons_ai AFTER INSERT ON episodic_lessons BEGIN
                        INSERT INTO episodic_lessons_fts(rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES (new.id, new.tool_name, new.trigger_context, new.failure_signature, new.lesson);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS episodic_lessons_ad AFTER DELETE ON episodic_lessons BEGIN
                        INSERT INTO episodic_lessons_fts(episodic_lessons_fts, rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES ('delete', old.id, old.tool_name, old.trigger_context, old.failure_signature, old.lesson);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS episodic_lessons_au AFTER UPDATE ON episodic_lessons BEGIN
                        INSERT INTO episodic_lessons_fts(episodic_lessons_fts, rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES ('delete', old.id, old.tool_name, old.trigger_context, old.failure_signature, old.lesson);
                        INSERT INTO episodic_lessons_fts(rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES (new.id, new.tool_name, new.trigger_context, new.failure_signature, new.lesson);
                    END;
                """)

                # Schema metadata table for one-time migrations and rebuild tracking
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)

                # One-time rebuild to backfill any pre-existing rows into FTS indexes
                cursor.execute("SELECT 1 FROM schema_meta WHERE key = 'fts5_initialized'")
                if not cursor.fetchone():
                    try:
                        cursor.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
                        cursor.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
                        cursor.execute("INSERT INTO interactions_fts(interactions_fts) VALUES('rebuild')")
                        cursor.execute("INSERT INTO episodic_lessons_fts(episodic_lessons_fts) VALUES('rebuild')")
                        cursor.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('fts5_initialized', '1')")
                    except Exception as rb_err:
                        logger.debug("FTS5 initial rebuild notice: %s", rb_err)
            except Exception as fts_err:
                logger.warning("SQLite FTS5 virtual tables could not be initialized (falling back to standard SQL): %s", fts_err)

            # sqlite-vec Native C Vector Virtual Table & Real-Time Sync Triggers (Native 384-dim BGE INT8)
            try:
                import sqlite_vec

                from jarvis.core.memory.onnx_embeddings import DEFAULT_EMBEDDING_DIM
                TARGET_VEC_DIM = DEFAULT_EMBEDDING_DIM  # 384
                current_backend = self.vector_engine.get_backend_name()

                cursor.execute("SELECT value FROM schema_meta WHERE key = 'vec_dimension'")
                row_dim = cursor.fetchone()
                stored_dim = row_dim["value"] if row_dim else None

                cursor.execute("SELECT value FROM schema_meta WHERE key = 'embedder_backend'")
                row_backend = cursor.fetchone()
                stored_backend = row_backend["value"] if row_backend else None

                if stored_dim != str(TARGET_VEC_DIM) or stored_backend != current_backend:
                    logger.info(
                        "Vector schema migration triggered (stored: dim=%s, backend=%s; target: dim=%s, backend=%s). Recreating virtual tables...",
                        stored_dim, stored_backend, TARGET_VEC_DIM, current_backend
                    )
                    cursor.execute("DROP TABLE IF EXISTS facts_vec")
                    cursor.execute("DROP TABLE IF EXISTS documents_vec")
                    cursor.execute("DROP TABLE IF EXISTS lessons_vec")
                    cursor.execute("DROP TABLE IF EXISTS kg_edges_vec")

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
                        fact_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_vec_ad AFTER DELETE ON facts BEGIN
                        DELETE FROM facts_vec WHERE fact_id = old.id;
                    END;
                """)

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_vec USING vec0(
                        doc_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_vec_ad AFTER DELETE ON documents BEGIN
                        DELETE FROM documents_vec WHERE doc_id = old.id;
                    END;
                """)

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS lessons_vec USING vec0(
                        lesson_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS lessons_vec_ad AFTER DELETE ON episodic_lessons BEGIN
                        DELETE FROM lessons_vec WHERE lesson_id = old.id;
                    END;
                """)

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS kg_edges_vec USING vec0(
                        edge_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS kg_edges_vec_ad AFTER DELETE ON knowledge_edges BEGIN
                        DELETE FROM kg_edges_vec WHERE edge_id = old.edge_id;
                    END;
                """)

                cursor.execute("""
                    SELECT f.id, f.category, f.key, f.value
                    FROM facts f
                    LEFT JOIN facts_vec v ON f.id = v.fact_id
                    WHERE v.fact_id IS NULL
                """)
                unindexed_facts = cursor.fetchall()
                if unindexed_facts:
                    texts = [f"{f['category']} {f['key']} {f['value']}" for f in unindexed_facts]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                        [(f["id"], sqlite_vec.serialize_float32(v)) for f, v in zip(unindexed_facts, vecs)]
                    )

                cursor.execute("""
                    SELECT d.id, d.title, d.category, d.summary, d.raw_content
                    FROM documents d
                    LEFT JOIN documents_vec dv ON d.id = dv.doc_id
                    WHERE dv.doc_id IS NULL
                """)
                unindexed_docs = cursor.fetchall()
                if unindexed_docs:
                    texts = [f"{d['title']} {d['category']} {d['summary']} {(d['raw_content'] or '')[:500]}" for d in unindexed_docs]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO documents_vec(doc_id, embedding) VALUES (?, ?)",
                        [(d["id"], sqlite_vec.serialize_float32(v)) for d, v in zip(unindexed_docs, vecs)]
                    )

                cursor.execute("""
                    SELECT l.id, l.tool_name, l.trigger_context, l.failure_signature, l.lesson
                    FROM episodic_lessons l
                    LEFT JOIN lessons_vec lv ON l.id = lv.lesson_id
                    WHERE lv.lesson_id IS NULL
                """)
                unindexed_lessons = cursor.fetchall()
                if unindexed_lessons:
                    texts = [f"{l['tool_name']} {l['trigger_context']} {l['failure_signature']} {l['lesson']}" for l in unindexed_lessons]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO lessons_vec(lesson_id, embedding) VALUES (?, ?)",
                        [(l["id"], sqlite_vec.serialize_float32(v)) for l, v in zip(unindexed_lessons, vecs)]
                    )

                cursor.execute("""
                    SELECT k.edge_id, k.subject, k.predicate, k.object
                    FROM knowledge_edges k
                    LEFT JOIN kg_edges_vec kv ON k.edge_id = kv.edge_id
                    WHERE kv.edge_id IS NULL
                """)
                unindexed_edges = cursor.fetchall()
                if unindexed_edges:
                    texts = [f"{k['subject']} {k['predicate']} {k['object']}" for k in unindexed_edges]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO kg_edges_vec(edge_id, embedding) VALUES (?, ?)",
                        [(k["edge_id"], sqlite_vec.serialize_float32(v)) for k, v in zip(unindexed_edges, vecs)]
                    )

                cursor.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('vec_dimension', ?)", (str(TARGET_VEC_DIM),))
                cursor.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('embedder_backend', ?)", (current_backend,))
            except Exception as vec_err:
                logger.debug("sqlite-vec virtual table initialization note: %s", vec_err)
