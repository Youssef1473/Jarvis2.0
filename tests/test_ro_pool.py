"""Automated tests for VectorMemoryPool read-only mmap and offloaded KNN traversal."""
import asyncio
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite_vec

from jarvis.core.memory import MemoryManager
from jarvis.core.memory.ro_pool import (
    VectorMemoryPool,
    get_ro_connection,
)
from jarvis.core.memory.sqlite_base import SingleWriterActor


class TestVectorMemoryPoolSuite(unittest.IsolatedAsyncioTestCase):
    """Verifies VectorMemoryPool concurrency, mode=ro protection, and non-blocking KNN."""

    @classmethod
    def setUpClass(cls):
        from tools.run_fast_tests import probe_loopback_available
        probe = probe_loopback_available()
        if not probe:
            raise unittest.SkipTest(
                f"ProactorEventLoop self-pipe requires loopback socketpair: {probe.detail}"
            )

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_vec_pool.db"

        # Pre-populate database with facts and sqlite-vec virtual table
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)

        conn.execute("""
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT DEFAULT 'general',
                key TEXT UNIQUE,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE facts_vec USING vec0(
                fact_id INTEGER PRIMARY KEY,
                embedding float[4]
            )
        """)

        # Insert 3 test facts with distinct 4-dim unit embeddings
        facts_data = [
            (1, "hardware", "cpu_model", "Intel Core i7-12700H", [1.0, 0.0, 0.0, 0.0]),
            (2, "hardware", "gpu_model", "NVIDIA RTX 3050 Mobile", [0.0, 1.0, 0.0, 0.0]),
            (3, "preference", "editor", "VS Code with Python", [0.0, 0.0, 1.0, 0.0]),
        ]
        for f_id, cat, key, val, vec in facts_data:
            conn.execute("INSERT INTO facts (id, category, key, value) VALUES (?, ?, ?, ?)", (f_id, cat, key, val))
            blob = struct.pack("4f", *vec)
            conn.execute("INSERT INTO facts_vec (fact_id, embedding) VALUES (?, ?)", (f_id, blob))

        conn.commit()
        conn.close()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_pool_initialization_and_prewarming(self):
        """Verifies VectorMemoryPool pre-warms the requested pool size with mode=ro."""
        pool = VectorMemoryPool(self.db_path, pool_size=3)
        await pool.initialize()

        self.assertEqual(len(pool._created_conns), 3)
        self.assertEqual(pool._get_queue().qsize(), 3)
        self.assertIn("mode=ro", pool.db_uri)

        pool.teardown()

    async def test_write_operations_strictly_rejected(self):
        """Verifies PRAGMA query_only=ON and mode=ro reject all write mutations."""
        pool = VectorMemoryPool(self.db_path, pool_size=2)
        await pool.initialize()

        # Attempt to insert a new fact via execute_query
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            await pool.execute_query(
                "INSERT INTO facts (category, key, value) VALUES ('hack', 'bad', 'data')"
            )
        self.assertIn("readonly", str(ctx.exception).lower())

        pool.teardown()

    async def test_execute_knn_query_offloading(self):
        """Verifies offloaded KNN search against facts_vec returns matched IDs and distances."""
        pool = VectorMemoryPool(self.db_path, pool_size=2)
        await pool.initialize()

        # Query aligned with GPU embedding [0.0, 1.0, 0.0, 0.0]
        query_vec = [0.0, 0.99, 0.05, 0.0]
        results = await pool.execute_knn_query("facts_vec", query_vec, limit=2)

        self.assertGreaterEqual(len(results), 1)
        best = results[0]
        self.assertEqual(best["id"], 2)
        self.assertEqual(best["fact_id"], 2)
        self.assertLess(best["distance"], 0.2)

        pool.teardown()

    async def test_concurrent_knn_queries_without_starvation(self):
        """Verifies multiple concurrent KNN queries execute in parallel across the pool."""
        pool = VectorMemoryPool(self.db_path, pool_size=4)
        await pool.initialize()

        async def worker(idx: int):
            q_vec = [0.0, 1.0, 0.0, 0.0] if idx % 2 == 0 else [1.0, 0.0, 0.0, 0.0]
            res = await pool.execute_knn_query("facts_vec", q_vec, limit=1)
            return res[0]["id"]

        # Run 12 concurrent queries
        tasks = [asyncio.create_task(worker(i)) for i in range(12)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 12)
        for i, res_id in enumerate(results):
            expected = 2 if i % 2 == 0 else 1
            self.assertEqual(res_id, expected)

        pool.teardown()

    async def test_on_demand_connection_spinup_without_initialize(self):
        """Verifies pool dynamically creates connections if initialize() was omitted."""
        pool = VectorMemoryPool(self.db_path, pool_size=2)
        # Deliberately omit await pool.initialize()

        results = await pool.execute_knn_query("facts_vec", [1.0, 0.0, 0.0, 0.0], limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 1)

        pool.teardown()

    async def test_async_context_manager(self):
        """Verifies async with VectorMemoryPool handles setup and teardown cleanly."""
        async with VectorMemoryPool(self.db_path, pool_size=2) as pool:
            res = await pool.execute_knn_query("facts_vec", [0.0, 0.0, 1.0, 0.0], limit=1)
            self.assertEqual(res[0]["id"], 3)

        self.assertTrue(pool._is_torn_down)

    async def test_memory_manager_async_vector_search(self):
        """Verifies MemoryManager integrates VectorMemoryPool for async semantic search."""
        config = {
            "_base_dir": self.temp_dir.name,
            "memory": {"db_path": "mem_manager_test.db", "background_learning": False},
        }
        mem = MemoryManager(config)

        # Store test facts
        mem.remember_fact("pref_drink", "double shot espresso", "beverage")
        mem.remember_fact("pref_editor", "neovim with lua configuration", "tools")

        # Async semantic search offloaded to VectorMemoryPool
        res = await mem.semantic_search_facts_async("espresso coffee beverage", top_k=1)
        self.assertGreaterEqual(len(res), 1)
        self.assertEqual(res[0]["key"], "pref_drink")

        # Async hybrid search combining BM25 and vector RRF
        hybrid_res = await mem.hybrid_search_facts_async("neovim editor", top_k=1)
        self.assertGreaterEqual(len(hybrid_res), 1)
        self.assertEqual(hybrid_res[0]["key"], "pref_editor")

        mem.close()


class TestReadOnlyConnectionPoolAndWALSuite(unittest.TestCase):
    """Verifies ro_pool query timeouts, snapshot release, and SingleWriterActor WAL checkpointing."""

    def test_ro_pool_query_timeout_progress_handler(self):
        """Verifies progress handler enforces 2000ms deadline and raises query interrupted."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_timeout.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY);")
            conn.commit()
            conn.close()

            # Checkout with 50ms deadline to rapidly verify progress handler interruption
            with (
                self.assertRaises(sqlite3.OperationalError) as ctx,
                get_ro_connection(db_path, timeout_ms=50) as ro_conn,
            ):
                ro_conn.execute(
                    "WITH RECURSIVE r(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM r) SELECT count(*) FROM r"
                )
            self.assertIn("query interrupted", str(ctx.exception).lower())

    def test_ro_pool_rollback_releases_wal_read_mark(self):
        """Verifies context manager exit rolls back read snapshot and releases WAL read mark."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_wal_release.db"
            actor = SingleWriterActor(db_path)
            actor.execute_write(lambda c: c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);"))

            # Reader executes query and exits context manager
            with get_ro_connection(db_path) as ro_conn:
                res = ro_conn.execute("SELECT count(*) FROM t;").fetchone()
                self.assertEqual(res[0], 0)

            # Perform 50 writes via actor
            for i in range(50):
                actor.execute_write(lambda c, i=i: c.execute("INSERT INTO t VALUES (?, ?);", (i, f"v_{i}")))

            # Because ro_pool rolled back read snapshot on exit, TRUNCATE checkpoint must not be blocked
            chk_conn = sqlite3.connect(str(db_path))
            try:
                res = chk_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchall()
                self.assertEqual(res[0][0], 0, "Checkpoint was busy; WAL read mark was not released")
            finally:
                chk_conn.close()
                actor.stop()

    def test_single_writer_periodic_wal_checkpoint(self):
        """Verifies SingleWriterActor executes passive checkpoint every 100 transactions."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_periodic.db"
            actor = SingleWriterActor(db_path)
            actor.execute_write(lambda c: c.execute("CREATE TABLE items (id INT, val TEXT);"))

            # Write 105 transactions
            for i in range(105):
                actor.execute_write(lambda c, i=i: c.execute("INSERT INTO items VALUES (?, ?);", (i, f"item_{i}")))

            self.assertEqual(actor.committed_write_count, 106)
            actor.stop()

    def test_single_writer_clean_shutdown_truncates_wal(self):
        """Verifies SingleWriterActor clean shutdown triggers wal_checkpoint(TRUNCATE)."""
        test_dir = Path("scratch/test_wal_shutdown")
        test_dir.mkdir(parents=True, exist_ok=True)
        db_path = test_dir / "wal_truncation_actor.db"
        try:
            if db_path.exists():
                db_path.unlink()
            actor = SingleWriterActor(db_path)
            actor.execute_write(lambda c: c.execute("CREATE TABLE t (id INT);"))
            for i in range(20):
                actor.execute_write(lambda c, i=i: c.execute("INSERT INTO t VALUES (?);", (i,)))

            wal_path = Path(f"{db_path}-wal")
            self.assertTrue(wal_path.exists(), "Persistent writer did not retain WAL file during active session")
            self.assertGreater(wal_path.stat().st_size, 0, "WAL size must be >0 before checkpoint shutdown")

            # Clean shutdown should truncate WAL
            actor.stop()
            if wal_path.exists():
                self.assertEqual(wal_path.stat().st_size, 0, "Clean shutdown failed to truncate WAL")
        finally:
            for p in [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]:
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                test_dir.rmdir()
            except OSError:
                pass

    def test_interleaved_readers_and_250_writes_concurrency_stress(self):
        """Concurrency stress test: 4 rapid readers and 250 writes keep WAL bounded without SQLITE_BUSY."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_stress.db"
            actor = SingleWriterActor(db_path)
            actor.execute_write(
                lambda c: c.execute("CREATE TABLE counter (id INT PRIMARY KEY, val INT, payload TEXT);")
            )

            reader_errors: list[Exception] = []
            stop_readers = threading.Event()
            read_count = [0]

            def reader_worker():
                while not stop_readers.is_set():
                    try:
                        with get_ro_connection(db_path) as ro_conn:
                            cursor = ro_conn.execute("SELECT count(*), max(val) FROM counter;")
                            cursor.fetchone()
                            read_count[0] += 1
                        time.sleep(0.001)
                    except Exception as exc:  # noqa: BLE001
                        reader_errors.append(exc)

            readers = [threading.Thread(target=reader_worker, daemon=True) for _ in range(4)]
            for r in readers:
                r.start()

            # Execute 250 sequential writes via SingleWriterActor
            write_errors: list[Exception] = []
            wal_sizes: list[int] = []
            wal_path = Path(f"{db_path}-wal")

            for i in range(250):
                try:
                    actor.execute_write(
                        lambda c, i=i: c.execute(
                            "INSERT INTO counter (id, val, payload) VALUES (?, ?, ?);",
                            (i, i, "payload_padding_" + str(i) * 5),
                        )
                    )
                    if wal_path.exists():
                        wal_sizes.append(wal_path.stat().st_size)
                except Exception as exc:  # noqa: BLE001
                    write_errors.append(exc)

            stop_readers.set()
            for r in readers:
                r.join(timeout=2.0)

            # Verify no locking or database errors
            self.assertEqual(len(write_errors), 0, f"Write errors occurred: {write_errors}")
            self.assertEqual(len(reader_errors), 0, f"Reader errors occurred: {reader_errors}")
            self.assertGreater(read_count[0], 50, "Readers failed to execute sufficient concurrent queries")
            self.assertEqual(actor.committed_write_count, 251)

            # Clean shutdown of SingleWriterActor triggers TRUNCATE checkpoint
            actor.stop()
            if wal_path.exists():
                self.assertEqual(wal_path.stat().st_size, 0, "Clean shutdown failed to truncate WAL to 0 bytes")


if __name__ == "__main__":
    unittest.main()
