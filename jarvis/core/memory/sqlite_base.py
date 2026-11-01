"""SQLite WAL connection management, single-writer serialization, and database schema initialization."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger("Jarvis.Memory.SqliteBase")


class SingleWriterError(RuntimeError):
    """Base error for SingleWriterActor failures."""

    cancelled_before_execution: bool = True
    timed_out_in_flight: bool = False
    transaction_outcome_unknown: bool = False
    transaction_outcome: str = "not_executed"


class ActorUnavailableError(SingleWriterError):
    """Raised when SingleWriterActor is not running before queuing a write operation

    and its worker thread is dead. Direct connection fallback is safe.
    """


class ActorStoppingError(SingleWriterError):
    """Raised when SingleWriterActor is stopping while its worker thread is still alive.

    Direct connection fallback is strictly prohibited to prevent concurrent SQLite writers.
    """


class SingleWriterActor:
    """Serializes all write operations (INSERT, UPDATE, DELETE) through an asynchronous queue worker.
    Guarantees zero SQLITE_BUSY write lock contention and atomic transaction sequencing.
    Enforces clean lifecycle state management, timeout cancellation, and leak-free shutdown.
    """

    STATE_RUNNING = "running"
    STATE_STOPPING = "stopping"
    STATE_STOPPED = "stopped"

    _instances: ClassVar[dict[str, SingleWriterActor]] = {}
    _registry_lock: ClassVar[threading.RLock] = threading.RLock()
    _db_write_locks: ClassVar[dict[str, threading.RLock]] = {}
    _active_fallback_threads: ClassVar[dict[str, int]] = {}

    @classmethod
    def _get_db_lock(cls, resolved_key: str) -> threading.RLock:
        with cls._registry_lock:
            lock = cls._db_write_locks.get(resolved_key)
            if lock is None:
                lock = threading.RLock()
                cls._db_write_locks[resolved_key] = lock
            return lock

    @classmethod
    def get_or_create(cls, db_path: Path | str) -> SingleWriterActor:
        """Retrieves or instantiates a singleton SingleWriterActor for the given database path."""
        resolved_key = os.path.normcase(str(Path(db_path).resolve()))
        db_lock = cls._get_db_lock(resolved_key)
        with db_lock:
            current_tid = threading.get_ident()
            with cls._registry_lock:
                if cls._active_fallback_threads.get(resolved_key) == current_tid:
                    raise SingleWriterError(
                        f"Reentrant SingleWriterActor instantiation prohibited: thread {current_tid} "
                        f"is actively executing a direct fallback write for database '{resolved_key}'."
                    )

                existing = cls._instances.get(resolved_key)
                if existing is not None:
                    with existing._state_lock:
                        if existing._state == cls.STATE_RUNNING and existing._thread.is_alive():
                            return existing

                    if not existing._thread.is_alive():
                        cls._instances.pop(resolved_key, None)
                        existing = None

            # Concurrency Invariant: Never allow two live writer threads for the same database.
            # Release _registry_lock while waiting so the terminating worker can acquire _registry_lock to pop itself.
            if existing is not None and existing._thread.is_alive():
                if threading.current_thread() is not existing._thread:
                    existing._thread.join(timeout=0.2)
                with cls._registry_lock:
                    if existing._thread.is_alive():
                        raise ActorStoppingError(
                            f"Previous SingleWriterActor for {resolved_key} is still terminating "
                            f"(state={existing._state}, thread={existing._thread.name}); cannot instantiate a competing writer."
                        )
                    cls._instances.pop(resolved_key, None)

            actor = cls(Path(db_path))
            return actor

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        resolved_key = os.path.normcase(str(self.db_path.resolve()))
        db_lock = self._get_db_lock(resolved_key)
        with db_lock:
            with self._registry_lock:
                current_tid = threading.get_ident()
                if self._active_fallback_threads.get(resolved_key) == current_tid:
                    raise SingleWriterError(
                        f"Direct SingleWriterActor instantiation prohibited: thread {current_tid} "
                        f"is actively executing a direct fallback write for database '{resolved_key}'."
                    )
                existing = self._instances.get(resolved_key)
                if existing is not None and existing is not self and existing._thread.is_alive():
                    if existing._state != self.STATE_RUNNING:
                        raise ActorStoppingError(
                            f"Previous SingleWriterActor for {resolved_key} is still terminating "
                            f"(state={existing._state}, thread={existing._thread.name}); cannot instantiate a competing writer."
                        )
                    raise SingleWriterError(
                        f"Active SingleWriterActor already exists for '{resolved_key}' "
                        f"(state={existing._state}, thread={existing._thread.name}). Use get_or_create()."
                    )

            self._current_item: tuple | None = None
            self._queue: queue.Queue[
                tuple[Callable[[sqlite3.Connection], Any], threading.Event, list, list, threading.Event, dict, threading.Lock]
                | None
            ] = queue.Queue()
            self._state = self.STATE_RUNNING
            self._state_lock = threading.Lock()
            self._vector_extension_loaded: bool = False
            self._vector_load_error: str | None = None
            try:
                import sqlite_vec

                probe = sqlite3.connect(":memory:")
                try:
                    probe.enable_load_extension(True)
                    sqlite_vec.load(probe)
                    self._vector_extension_loaded = True
                    self._vector_load_error = None
                finally:
                    probe.close()
            except Exception as exc:  # noqa: BLE001
                self._vector_extension_loaded = False
                self._vector_load_error = str(exc)

            self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="JarvisSingleWriter")

            try:
                with self._registry_lock:
                    self._instances[resolved_key] = self
                self._thread.start()
            except BaseException:
                if self._thread.is_alive():
                    # Concurrency Invariant: Thread was launched before exception occurred.
                    # Fail closed: safely stop the worker and never remove registration while worker is alive.
                    try:
                        self.stop(drain=False, timeout=1.0)
                    except Exception as stop_err:  # noqa: BLE001
                        logger.warning("Error stopping partially initialized actor worker: %s", stop_err)
                    with self._registry_lock:
                        if not self._thread.is_alive() and self._instances.get(resolved_key) is self:
                            self._instances.pop(resolved_key, None)
                else:
                    with self._registry_lock:
                        if self._instances.get(resolved_key) is self:
                            self._instances.pop(resolved_key, None)
                raise

    @property
    def is_vector_extension_loaded(self) -> bool:
        """Returns True if sqlite_vec was successfully loaded in worker connections."""
        return self._vector_extension_loaded

    @property
    def vector_load_error(self) -> str | None:
        """Returns the error message if sqlite_vec failed to load, else None."""
        return self._vector_load_error

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
        c.execute("PRAGMA recursive_triggers=ON;")
        c.row_factory = sqlite3.Row
        try:
            import sqlite_vec
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            self._vector_extension_loaded = True
            self._vector_load_error = None
        except Exception as exc:  # noqa: BLE001
            self._vector_extension_loaded = False
            self._vector_load_error = str(exc)
            logger.warning(
                "SingleWriterActor: sqlite_vec extension unavailable (%s); vector virtual tables degraded.",
                exc,
            )
        return c

    def _worker_loop(self) -> None:
        key = str(self.db_path.resolve())
        is_temp = "tmp" in key.lower() or "temp" in key.lower() or self.db_path.name.startswith("test_")

        conn: sqlite3.Connection | None = None
        try:
            if not is_temp:
                try:
                    conn = self._create_conn()
                except Exception as e:  # noqa: BLE001
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

                self._current_item = item
                func, done_evt, res_box, err_box, cancel_evt, status, item_lock = item

                # Check if caller already timed out or cancelled this write under mutex
                with item_lock:
                    if cancel_evt.is_set() or status.get("phase") == "cancelled_before_execution":
                        status["phase"] = "cancelled_before_execution"
                        done_evt.set()
                        self._queue.task_done()
                        self._current_item = None
                        continue
                    status["phase"] = "preparing"
                    status["callback_started"] = False

                local_conn = conn
                opened_temp = False
                if local_conn is None:
                    try:
                        local_conn = self._create_conn()
                        opened_temp = True
                    except BaseException as ce:
                        with item_lock:
                            if isinstance(ce, Exception):
                                conn_err = ce
                            else:
                                conn_err = RuntimeError(
                                    f"Worker loop fatal {type(ce).__name__} during connection initialization; "
                                    "write was not executed."
                                )
                            conn_err.cancelled_before_execution = True
                            conn_err.timed_out_in_flight = False
                            conn_err.transaction_outcome_unknown = False
                            conn_err.transaction_outcome = "not_executed"
                            err_box.append(conn_err)
                            status["phase"] = "failed_before_callback"
                        done_evt.set()
                        self._queue.task_done()
                        self._current_item = None
                        if not isinstance(ce, Exception):
                            raise
                        continue

                with item_lock:
                    status["phase"] = "executing"
                    status["callback_started"] = True

                try:
                    with local_conn:
                        result = func(local_conn)
                    with item_lock:
                        res_box.append(result)
                        status["phase"] = "completed"
                except Exception as e:  # noqa: BLE001
                    with item_lock:
                        err_box.append(e)
                        status["phase"] = "failed"
                except BaseException as fatal_be:
                    with item_lock:
                        status["phase"] = "failed"
                        fatal_err = RuntimeError(
                            f"Worker loop fatal {type(fatal_be).__name__} during write execution; "
                            "transaction outcome is unknown."
                        )
                        fatal_err.cancelled_before_execution = False
                        fatal_err.timed_out_in_flight = False
                        fatal_err.transaction_outcome_unknown = True
                        fatal_err.transaction_outcome = "unknown"
                        err_box.append(fatal_err)
                    raise
                finally:
                    if opened_temp and local_conn is not None:
                        try:
                            local_conn.close()
                        except Exception as close_err:  # noqa: BLE001
                            logger.debug("Error closing temporary local_conn: %s", close_err)
                    done_evt.set()
                    self._queue.task_done()
                    self._current_item = None
        except BaseException as fatal_exc:
            logger.critical("SingleWriterActor worker loop encountered fatal error: %s", fatal_exc)
            raise
        finally:
            with self._state_lock:
                self._state = self.STATE_STOPPED
            if self._current_item is not None:
                _, done_evt, _, err_box, cancel_evt, status, item_lock = self._current_item
                with item_lock:
                    if not done_evt.is_set():
                        callback_started = status.get("callback_started", False)
                        status["phase"] = "failed"
                        if not err_box:
                            if callback_started:
                                active_err = RuntimeError(
                                    "SingleWriterActor worker terminated fatally while write was executing; "
                                    "transaction outcome is unknown."
                                )
                                active_err.cancelled_before_execution = False
                                active_err.timed_out_in_flight = False
                                active_err.transaction_outcome_unknown = True
                                active_err.transaction_outcome = "unknown"
                            else:
                                active_err = RuntimeError(
                                    "SingleWriterActor worker terminated fatally before callback execution began; "
                                    "write was not executed."
                                )
                                active_err.cancelled_before_execution = True
                                active_err.timed_out_in_flight = False
                                active_err.transaction_outcome_unknown = False
                                active_err.transaction_outcome = "not_executed"
                            err_box.append(active_err)
                        done_evt.set()
                self._current_item = None
            self._drain_and_reject_pending(
                reason=RuntimeError("SingleWriterActor worker terminated unexpectedly; queued write rejected.")
            )
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_err:  # noqa: BLE001
                    logger.debug("SingleWriterActor persistent conn close note: %s", close_err)
            with self._registry_lock:
                resolved_key = os.path.normcase(str(self.db_path.resolve()))
                if self._instances.get(resolved_key) is self:
                    self._instances.pop(resolved_key, None)

    def execute_write(self, func: Callable[[sqlite3.Connection], Any], timeout: float = 30.0) -> Any:
        done_evt = threading.Event()
        cancel_evt = threading.Event()
        res_box: list = []
        err_box: list = []
        status: dict = {"phase": "pending"}
        item_lock = threading.Lock()

        # Atomic admission check and queue insertion under _state_lock
        with self._state_lock:
            if self._state != self.STATE_RUNNING:
                if self._thread.is_alive():
                    err = ActorStoppingError(
                        f"SingleWriterActor is {self._state} and worker thread is alive; cannot accept new write operations."
                    )
                else:
                    err = ActorUnavailableError(
                        f"SingleWriterActor is {self._state}; cannot accept new write operations."
                    )
                err.cancelled_before_execution = True
                err.timed_out_in_flight = False
                err.transaction_outcome_unknown = False
                err.transaction_outcome = "not_executed"
                raise err
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

    @classmethod
    def execute_or_fallback(
        cls,
        db_path: Path | str,
        func: Callable[[sqlite3.Connection], Any],
        fallback_conn_factory: Callable[[], Any],
        timeout: float = 30.0,
    ) -> Any:
        """Executes a write operation on SingleWriterActor, falling back to a direct connection

        ONLY if the actor was unavailable before queuing (never on in-flight timeouts, stopping actors, or SQL errors).
        """
        resolved_key = os.path.normcase(str(Path(db_path).resolve()))
        try:
            actor = cls.get_or_create(db_path)
            return actor.execute_write(func, timeout=timeout)
        except ActorStoppingError:
            # Concurrency Invariant: Actor is terminating with a live worker thread in-flight.
            # Direct fallback write MUST NOT execute concurrently with the terminating worker.
            raise
        except ActorUnavailableError as aue:
            logger.warning(
                "SingleWriterActor for %s is unavailable (%s); executing via fallback connection.",
                db_path,
                aue,
            )
            db_lock = cls._get_db_lock(resolved_key)
            with db_lock:
                current_tid = threading.get_ident()
                with cls._registry_lock:
                    if cls._active_fallback_threads.get(resolved_key) == current_tid:
                        raise SingleWriterError(
                            f"Reentrant fallback execution prohibited: thread {current_tid} is already executing fallback for '{resolved_key}'."
                        )
                    # Re-check under db_lock whether an actor became available or is stopping
                    existing = cls._instances.get(resolved_key)
                    if existing is not None:
                        with existing._state_lock:
                            if existing._state == cls.STATE_RUNNING and existing._thread.is_alive():
                                return existing.execute_write(func, timeout=timeout)
                        if existing._thread.is_alive():
                            raise ActorStoppingError(
                                f"SingleWriterActor for {resolved_key} is stopping while worker thread is alive."
                            )
                    cls._active_fallback_threads[resolved_key] = current_tid

                try:
                    ctx = fallback_conn_factory()
                    if isinstance(ctx, sqlite3.Connection):
                        conn = ctx
                        try:
                            res = func(conn)
                            if hasattr(conn, "commit"):
                                conn.commit()
                            return res
                        except Exception:
                            if hasattr(conn, "rollback"):
                                try:
                                    conn.rollback()
                                except Exception as rb_err:  # noqa: BLE001
                                    logger.debug("Rollback on fallback connection note: %s", rb_err)
                            raise
                        finally:
                            if hasattr(conn, "close"):
                                conn.close()
                    elif hasattr(ctx, "__enter__") and hasattr(ctx, "__exit__"):
                        with ctx as conn:
                            res = func(conn)
                            if hasattr(conn, "commit"):
                                conn.commit()
                            return res
                    else:
                        conn = ctx
                        try:
                            res = func(conn)
                            if hasattr(conn, "commit"):
                                conn.commit()
                            return res
                        except Exception:
                            if hasattr(conn, "rollback"):
                                try:
                                    conn.rollback()
                                except Exception as rb_err:  # noqa: BLE001
                                    logger.debug("Rollback on raw fallback connection note: %s", rb_err)
                            raise
                        finally:
                            if hasattr(conn, "close"):
                                conn.close()
                finally:
                    with cls._registry_lock:
                        cls._active_fallback_threads.pop(resolved_key, None)

    def _drain_and_reject_pending(self, reason: Exception | None = None) -> None:
        """Drains remaining queue items and sets error on pending callers."""
        err = reason if reason is not None else RuntimeError("SingleWriterActor shutdown before write could execute.")
        if not hasattr(err, "cancelled_before_execution"):
            err.cancelled_before_execution = True
            err.timed_out_in_flight = False
            err.transaction_outcome_unknown = False
            err.transaction_outcome = "not_executed"
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    _, done_evt, _, err_box, cancel_evt, status, item_lock = item
                    with item_lock:
                        cancel_evt.set()
                        status["phase"] = "cancelled_before_execution"
                        if not err_box:
                            err_box.append(err)
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
            if self._state == self.STATE_STOPPED and not self._thread.is_alive():
                return
            if self._state == self.STATE_RUNNING:
                self._state = self.STATE_STOPPING
                if not drain:
                    self._drain_and_reject_pending()
                self._queue.put(None)
        if self._thread.is_alive():
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=timeout)
            else:
                with self._state_lock:
                    self._state = self.STATE_STOPPED
                self._drain_and_reject_pending()
                return

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

        with self._registry_lock:
            resolved_key = os.path.normcase(str(self.db_path.resolve()))
            if not self._thread.is_alive() and self._instances.get(resolved_key) is self:
                self._instances.pop(resolved_key, None)


class SqliteBaseMixin:
    """Provides SQLite database connection management, pool access, and schema initialization."""

    _vector_extension_loaded: bool = False
    _vector_load_error: str | None = None

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
                self._vector_extension_loaded = True
                self._vector_load_error = None
            except Exception as exc:  # noqa: BLE001
                self._vector_extension_loaded = False
                self._vector_load_error = str(exc)
                logger.warning(
                    "SqliteBaseMixin: sqlite_vec extension unavailable (%s); vector virtual tables degraded.",
                    exc,
                )
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    @property
    def is_vector_search_available(self) -> bool:
        """Returns True if sqlite-vec extension and virtual tables are operational across all pools."""
        if not getattr(self, "_vector_extension_loaded", False):
            return False
        if hasattr(self, "_writer_actor") and self._writer_actor and not getattr(self._writer_actor, "is_vector_extension_loaded", True):
            return False
        if hasattr(self, "_ro_vector_pool") and self._ro_vector_pool and not getattr(self._ro_vector_pool, "is_vector_extension_loaded", True):
            return False
        try:
            from jarvis.memory.ro_pool import ReadOnlyConnectionPool
            ro_pool = ReadOnlyConnectionPool.get_instance()
            if hasattr(ro_pool, "vector_load_error") and ro_pool.vector_load_error is not None:
                return False
        except Exception as ro_err:  # noqa: BLE001
            logger.debug("RO pool health check note: %s", ro_err)
        return True

    @property
    def vector_load_error(self) -> str | None:
        """Returns the first sqlite-vec load error across all connection pools, or None if operational."""
        if getattr(self, "_vector_load_error", None):
            return self._vector_load_error
        if hasattr(self, "_writer_actor") and self._writer_actor and getattr(self._writer_actor, "vector_load_error", None):
            return self._writer_actor.vector_load_error
        if hasattr(self, "_ro_vector_pool") and self._ro_vector_pool and getattr(self._ro_vector_pool, "vector_load_error", None):
            return self._ro_vector_pool.vector_load_error
        try:
            from jarvis.memory.ro_pool import ReadOnlyConnectionPool
            ro_pool = ReadOnlyConnectionPool.get_instance()
            if hasattr(ro_pool, "vector_load_error") and ro_pool.vector_load_error:
                return ro_pool.vector_load_error
        except Exception as pool_err:  # noqa: BLE001
            logger.debug("Vector load error inspection note: %s", pool_err)
        return None

    def get_vector_search_health(self) -> dict[str, Any]:
        """Returns structured vector engine operational health status."""
        available = self.is_vector_search_available
        backend_name = "unknown"
        if hasattr(self, "vector_engine") and hasattr(self.vector_engine, "get_backend_name"):
            try:
                backend_name = self.vector_engine.get_backend_name()
            except (AttributeError, RuntimeError) as b_err:
                logger.debug("Embedder backend name resolution note: %s", b_err)
        return {
            "status": "HEALTHY" if available else "DEGRADED",
            "backend": "sqlite-vec" if available else "lexical-bm25-fallback",
            "embedder_backend": backend_name,
            "degraded": not available,
            "error": self.vector_load_error,
        }

    @contextlib.contextmanager
    def _get_ro_connection(self):
        """Thread-local read-only SQLite connection from ReadOnlyConnectionPool."""
        from jarvis.memory.ro_pool import get_ro_connection
        with get_ro_connection(self.db_path) as conn:
            yield conn

    def execute_write(self, func: Callable[[sqlite3.Connection], Any], timeout: float = 30.0) -> Any:
        """Executes a write operation serialized through the Single-Writer Actor queue,
        falling back to a direct connection context ONLY if the actor is unavailable before execution.
        """
        return SingleWriterActor.execute_or_fallback(
            self.db_path,
            func,
            self._get_connection,
            timeout=timeout,
        )

    async def execute_write_async(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        """Asynchronously dispatches a write operation through the Single-Writer Actor queue."""
        return await asyncio.to_thread(self.execute_write, func)

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
            except Exception as teardown_err:  # noqa: BLE001
                logger.debug("RO vector pool teardown note: %s", teardown_err)
            self._ro_vector_pool = None
        if hasattr(self, "_writer_actor") and self._writer_actor:
            self._writer_actor.stop()
        try:
            from jarvis.memory.ro_pool import ReadOnlyConnectionPool
            ReadOnlyConnectionPool.get_instance().close_db(self.db_path)
        except Exception as close_err:  # noqa: BLE001
            logger.debug("RO connection pool close note: %s", close_err)

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
                row["name"] if hasattr(row, "__getitem__") and "name" in row else row[1]
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

                # Compiled Reflex Macros (Canonical Deterministic Workflow Schema)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS compiled_reflex_macros (
                        macro_name TEXT PRIMARY KEY,
                        trigger_regex TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        failure_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        macro_id TEXT GENERATED ALWAYS AS (macro_name) VIRTUAL,
                        tool_sequence TEXT DEFAULT '',
                        occurrence_count INTEGER DEFAULT 0,
                        last_compiled_ts REAL DEFAULT 0.0
                    ) WITHOUT ROWID
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
                    except Exception as rb_err:  # noqa: BLE001
                        logger.debug("FTS5 initial rebuild notice: %s", rb_err)
            except Exception as fts_err:  # noqa: BLE001
                logger.warning("SQLite FTS5 virtual tables could not be initialized (falling back to standard SQL): %s", fts_err)

            # sqlite-vec Native C Vector Virtual Table & Real-Time Sync Triggers (Native 384-dim BGE INT8)
            try:
                if not getattr(self, "_vector_extension_loaded", False):
                    raise RuntimeError(
                        f"sqlite-vec extension is not loaded ({self._vector_load_error or 'extension load failed'})"
                    )
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
                    CREATE TRIGGER IF NOT EXISTS kg_edges_vec_au AFTER UPDATE OF valid_until ON knowledge_edges
                    WHEN new.valid_until IS NOT NULL AND old.valid_until IS NULL
                    BEGIN
                        DELETE FROM kg_edges_vec WHERE edge_id = old.edge_id;
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS trg_kg_supersede_functional_vec
                    BEFORE INSERT ON knowledge_edges
                    WHEN NEW.is_functional = 1 AND NEW.valid_until IS NULL
                    BEGIN
                        DELETE FROM kg_edges_vec WHERE edge_id IN (
                            SELECT edge_id FROM knowledge_edges
                            WHERE subject = NEW.subject
                              AND predicate = NEW.predicate
                              AND valid_until IS NULL
                              AND object != NEW.object
                        );
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
                self._vector_extension_loaded = True
                self._vector_load_error = None
            except Exception as vec_err:  # noqa: BLE001
                self._vector_extension_loaded = False
                self._vector_load_error = str(vec_err)
                logger.warning(
                    "sqlite-vec virtual table initialization failed; vector search degraded to lexical BM25/TF-IDF: %s",
                    vec_err,
                )
