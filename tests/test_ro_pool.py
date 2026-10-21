"""Automated tests for VectorMemoryPool read-only mmap and offloaded KNN traversal."""
import asyncio
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.memory.ro_pool import VectorMemoryPool
from jarvis.core.ro_pool import VectorMemoryPool as CoreVectorMemoryPool
from jarvis.core.memory import MemoryManager
import sqlite_vec


class TestVectorMemoryPoolSuite(unittest.IsolatedAsyncioTestCase):
    """Verifies VectorMemoryPool concurrency, mode=ro protection, and non-blocking KNN."""

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


if __name__ == "__main__":
    unittest.main()
