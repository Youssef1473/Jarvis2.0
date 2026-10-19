"""Thread-local read-only SQLite connection pool for speculative workers.

Ensures concurrent vector/BM25 lookups never serialize on a shared connection mutex
or trigger SQLITE_BUSY write locks.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Generator, Optional

logger = logging.getLogger("Jarvis.Memory.ROPool")

DEFAULT_DB_PATH = Path("data/jarvis_memory.db")


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
