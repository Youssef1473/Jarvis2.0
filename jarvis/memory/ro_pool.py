"""Thread-local read-only SQLite connection pool for speculative workers and
VectorMemoryPool for asynchronous non-blocking sqlite-vec KNN traversals.

Ensures concurrent vector/BM25 lookups never serialize on a shared connection mutex,
trigger SQLITE_BUSY write locks, or starve the asyncio event loop during duplex audio.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import logging
import os
from pathlib import Path
import sqlite3
import struct
import threading
from typing import Any, Callable, Dict, Generator, List, Optional, Union

logger = logging.getLogger("Jarvis.Memory.ROPool")

DEFAULT_DB_PATH = Path("data/jarvis_memory.db")


class VectorMemoryPool:
    """Thread-safe Read-Only SQLite pool optimized for sqlite-vec KNN traversals.

    Guarantees the asyncio event loop remains unblocked during heavy RRF operations.
    Enforces mode=ro, PRAGMA query_only = ON, and 256MB mmap memory mapping.
    """

    def __init__(self, db_path: str | Path, pool_size: int = 4) -> None:
        target = Path(db_path).resolve()
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            init_conn = sqlite3.connect(str(target))
            init_conn.execute("PRAGMA journal_mode=WAL;")
            init_conn.close()

        self.db_path: Path = target
        # The file URI with ?mode=ro enforces a strict read-only lock at the VFS level
        self.db_uri: str = f"file:{target.as_posix()}?mode=ro"
        self.pool_size: int = max(1, pool_size)

        self._executor = ThreadPoolExecutor(
            max_workers=self.pool_size,
            thread_name_prefix="Jarvis_RO_MemPool",
        )
        self._pool: Optional[asyncio.Queue[sqlite3.Connection]] = None
        self._pool_lock = threading.Lock()
        self._created_conns: list[sqlite3.Connection] = []
        self._is_torn_down = False

    def _get_queue(self) -> asyncio.Queue[sqlite3.Connection]:
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = asyncio.Queue(maxsize=self.pool_size)
        return self._pool

    async def initialize(self) -> None:
        """Asynchronously pre-warms the read-only connection pool."""
        if self._is_torn_down:
            raise RuntimeError("Cannot initialize a torn-down VectorMemoryPool.")
        pool = self._get_queue()
        loop = asyncio.get_running_loop()
        needed = self.pool_size - len(self._created_conns)
        for _ in range(needed):
            conn = await loop.run_in_executor(
                self._executor, self._create_optimized_connection
            )
            with self._pool_lock:
                self._created_conns.append(conn)
            await pool.put(conn)
        logger.info("Initialized %d RO vector connections via mmap.", len(self._created_conns))

    def _create_optimized_connection(self) -> sqlite3.Connection:
        """Synchronous factory enforcing aggressive read-heavy pragmas."""
        conn = sqlite3.connect(self.db_uri, uri=True, check_same_thread=False)
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except Exception as e:
            logger.debug("sqlite_vec load in RO connection: %s", e)

        # Elite SQLite optimizations for vector workloads
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("PRAGMA mmap_size = 268435456;")  # 256MB memory mapping
        conn.execute("PRAGMA cache_size = -64000;")    # 64MB page cache
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA busy_timeout = 5000;")

        conn.row_factory = sqlite3.Row
        return conn

    async def _acquire_conn(self) -> sqlite3.Connection:
        if self._is_torn_down:
            raise RuntimeError("VectorMemoryPool is torn down.")
        pool = self._get_queue()
        # If pool is empty and capacity remains, dynamically spin up connection
        if pool.empty() and len(self._created_conns) < self.pool_size:
            loop = asyncio.get_running_loop()
            conn = await loop.run_in_executor(
                self._executor, self._create_optimized_connection
            )
            with self._pool_lock:
                self._created_conns.append(conn)
            return conn
        return await pool.get()

    async def _release_conn(self, conn: sqlite3.Connection) -> None:
        if self._is_torn_down:
            try:
                conn.close()
            except Exception:
                pass
            return
        pool = self._get_queue()
        try:
            pool.put_nowait(conn)
        except asyncio.QueueFull:
            try:
                conn.close()
            except Exception:
                pass

    async def execute_knn_query(
        self,
        table: str,
        query_vector: Union[List[float], bytes, Any],
        limit: int = 5,
        id_column: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves a connection and offloads the blocking C-extension vector

        traversal to the thread pool, yielding control to the event loop.
        """
        conn = await self._acquire_conn()
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                self._executor,
                self._sync_knn_traversal,
                conn,
                table,
                query_vector,
                limit,
                id_column,
            )
            return results
        finally:
            await self._release_conn(conn)

    def _sync_knn_traversal(
        self,
        conn: sqlite3.Connection,
        table: str,
        query_vector: Union[List[float], bytes, Any],
        limit: int,
        id_column: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Synchronous workload executed off the main thread."""
        # sqlite-vec requires contiguous float32 byte packing
        if isinstance(query_vector, (bytes, bytearray)):
            vector_blob = bytes(query_vector)
        elif hasattr(query_vector, "tobytes"):
            vector_blob = query_vector.astype("float32").tobytes()
        else:
            vector_blob = struct.pack(f"{len(query_vector)}f", *query_vector)

        pk = id_column or {
            "facts_vec": "fact_id",
            "documents_vec": "doc_id",
            "lessons_vec": "lesson_id",
            "kg_edges_vec": "edge_id",
        }.get(table, "id")
        cols = f"{pk} AS id, {pk}, distance" if pk != "id" else "id, distance"

        try:
            cursor = conn.execute(
                f"""
                SELECT {cols}
                FROM {table}
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
                """,
                (vector_blob, limit),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            try:
                cursor = conn.execute(
                    f"""
                    SELECT {cols}
                    FROM {table}
                    WHERE embedding MATCH ? AND k = ?
                    ORDER BY distance ASC
                    """,
                    (vector_blob, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.Error:
                logger.error("Vector traversal failed on table '%s': %s", table, e)
                return []

    async def execute_query(
        self, sql: str, params: tuple = ()
    ) -> List[Dict[str, Any]]:
        """Executes an arbitrary read-only SQL query in worker thread."""
        conn = await self._acquire_conn()
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor,
                self._sync_query,
                conn,
                sql,
                params,
            )
        finally:
            await self._release_conn(conn)

    def _sync_query(
        self, conn: sqlite3.Connection, sql: str, params: tuple
    ) -> List[Dict[str, Any]]:
        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    async def execute_read_async(
        self, func: Callable[[sqlite3.Connection], Any]
    ) -> Any:
        """Asynchronously executes an arbitrary read operation off the main thread."""
        conn = await self._acquire_conn()
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, func, conn)
        finally:
            await self._release_conn(conn)

    def teardown(self) -> None:
        """Cleanly drains the pool and terminates worker threads."""
        self._is_torn_down = True
        self._executor.shutdown(wait=True)
        with self._pool_lock:
            for conn in self._created_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._created_conns.clear()
            if self._pool is not None:
                while not self._pool.empty():
                    try:
                        conn = self._pool.get_nowait()
                        conn.close()
                    except Exception:
                        pass

    def __del__(self) -> None:
        try:
            self.teardown()
        except Exception:
            pass

    async def __aenter__(self) -> VectorMemoryPool:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.teardown()


class ReadOnlyConnectionPool:
    """Thread-local pool of read-only SQLite connections."""

    _instance: Optional[ReadOnlyConnectionPool] = None
    _lock = threading.Lock()

    def __init__(self, default_db_path: Optional[Path | str] = None):
        self.default_db_path = Path(default_db_path or DEFAULT_DB_PATH)
        self._local = threading.local()

    @classmethod
    def get_instance(cls, default_db_path: Optional[Path | str] = None) -> ReadOnlyConnectionPool:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(default_db_path)
            elif default_db_path is not None:
                cls._instance.default_db_path = Path(default_db_path)
            return cls._instance

    def _create_connection(self, db_path: Path) -> sqlite3.Connection:
        resolved = db_path.resolve()
        if not resolved.exists():
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # Ensure DB exists before opening mode=ro
            init_conn = sqlite3.connect(str(resolved))
            init_conn.execute("PRAGMA journal_mode=WAL;")
            init_conn.close()

        # Connect with URI mode=ro
        uri_path = f"file:{resolved.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri_path, uri=True, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA mmap_size = 268435456;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except Exception:
            pass
        return conn

    @contextlib.contextmanager
    def connection(self, db_path: Optional[Path | str] = None) -> Generator[sqlite3.Connection, None, None]:
        target = Path(db_path or self.default_db_path)
        connections: dict[str, sqlite3.Connection] = getattr(self._local, "connections", None)
        if connections is None:
            connections = {}
            self._local.connections = connections

        key = str(target.resolve())
        is_temp = "tmp" in key.lower() or "temp" in key.lower() or target.name.startswith("test_")
        if is_temp:
            conn = self._create_connection(target)
            try:
                yield conn
            finally:
                try:
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            return

        conn = connections.get(key)
        if conn is None:
            conn = self._create_connection(target)
            connections[key] = conn

        try:
            yield conn
        finally:
            try:
                # Release historical WAL read-mark to avoid pinning frames during checkpoint(TRUNCATE)
                conn.commit()
            except Exception:
                pass

    def close_current_thread(self) -> None:
        """Closes all connections owned by calling thread."""
        connections: dict[str, sqlite3.Connection] = getattr(self._local, "connections", None)
        if connections:
            for conn in connections.values():
                try:
                    conn.close()
                except Exception:
                    pass
            connections.clear()

    def close_db(self, db_path: Optional[Path | str] = None) -> None:
        """Closes cached connection for a specific database path on calling thread."""
        connections: dict[str, sqlite3.Connection] = getattr(self._local, "connections", None)
        if connections:
            target = Path(db_path or self.default_db_path)
            key = str(target.resolve())
            conn = connections.pop(key, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @classmethod
    def reset_pool(cls) -> None:
        """Resets singleton instance."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close_current_thread()
                cls._instance = None


def get_ro_connection(db_path: Optional[Path | str] = None):
    """Convenience context manager returning thread-local read-only connection."""
    return ReadOnlyConnectionPool.get_instance().connection(db_path)
