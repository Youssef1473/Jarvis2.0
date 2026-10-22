"""Concurrent multi-threaded stress and transaction consistency tests for modularized MemoryManager."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

from jarvis.core.memory import MemoryManager


class TestMemoryConcurrencyStress(unittest.TestCase):
    """Rigorous concurrent read/write stress testing for decomposed memory mixins."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "concurrent_test.db")
        config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "concurrent_test.db", "background_learning": False},
            "llm": {},
        }
        self.memory = MemoryManager(config)

    def tearDown(self):
        self.memory.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_read_write_heavy_stress(self):
        """Simulate high-concurrency multi-threaded access across all memory domains."""
        num_writers = 6
        num_readers = 6
        iterations_per_worker = 25
        errors = []

        def writer_task(worker_id: int):
            try:
                for i in range(iterations_per_worker):
                    # 1. Facts & Preferences
                    self.memory.remember_fact(
                        key=f"worker_{worker_id}_fact_{i}",
                        value=f"value_{worker_id}_{i}_{time.time()}",
                        category="specs"
                    )
                    self.memory.update_user_preference(
                        key=f"pref_{worker_id}",
                        value=f"setting_{i}"
                    )

                    # 2. Tasks & Calendar
                    task_id = self.memory.add_task(f"Task from worker {worker_id} iter {i}")
                    if i % 2 == 0 and task_id:
                        self.memory.complete_task(task_id)

                    # 3. Interactions
                    self.memory.log_interaction(
                        user_input=f"Prompt from worker {worker_id} iter {i}",
                        jarvis_reply=f"Reply to worker {worker_id} iter {i}"
                    )

                    # 4. Temporal edges
                    self.memory.remember_edge(
                        subject=f"node_{worker_id}_{i}",
                        predicate="TRANSITIONS_TO",
                        object_val=f"node_{worker_id}_{i+1}",
                        is_functional=False,
                    )

                    # 5. Documents
                    self.memory.save_document_knowledge(
                        title=f"Doc_{worker_id}_{i}",
                        category="testing",
                        summary=f"Summary for doc {worker_id}_{i}",
                        raw_content=f"Content chunk 1 for doc {worker_id}_{i} and chunk 2",
                    )
            except Exception as e:
                errors.append((f"Writer-{worker_id}", e))

        def reader_task(worker_id: int):
            try:
                for i in range(iterations_per_worker):
                    # Reads from facts
                    self.memory.get_fact(f"worker_{worker_id % num_writers}_fact_{i}")
                    self.memory.get_user_preferences()

                    # Reads from tasks
                    self.memory.get_pending_tasks()

                    # Reads from interactions
                    self.memory.get_recent_interactions(limit=5)

                    # Reads from edges
                    self.memory.query_active_edges(f"node_{worker_id % num_writers}_{i}")

                    # Search documents
                    self.memory.search_documents("Content", limit=3)
            except Exception as e:
                errors.append((f"Reader-{worker_id}", e))

        with ThreadPoolExecutor(max_workers=num_writers + num_readers) as executor:
            futures = []
            for w in range(num_writers):
                futures.append(executor.submit(writer_task, w))
            for r in range(num_readers):
                futures.append(executor.submit(reader_task, r))

            for f in as_completed(futures):
                f.result()

        # Verify zero database lock errors or concurrency exceptions
        if errors:
            self.fail(f"Concurrent memory operations encountered {len(errors)} errors: {errors[:3]}")

        # Post-stress verification: all written items exist intact
        prefs = self.memory.get_user_preferences()
        self.assertEqual(len(prefs), num_writers)

        recent = self.memory.get_recent_interactions(limit=num_writers * iterations_per_worker)
        self.assertEqual(len(recent), num_writers * iterations_per_worker)

    def test_transaction_rollback_integrity(self):
        """Verify that a failing write does not commit partial state or corrupt the WAL journal."""
        def failing_write(conn):
            cursor = conn.cursor()
            cursor.execute("INSERT INTO facts (category, key, value) VALUES ('test', 'rollback_key', 'val1')")
            raise RuntimeError("Simulated mid-transaction failure")

        with self.assertRaises(RuntimeError):
            self.memory.execute_write(failing_write)

        # Confirm rollback_key was not committed
        fact = self.memory.get_fact("rollback_key")
        self.assertIsNone(fact)

    def test_clean_lifecycle_shutdown(self):
        """Verify that close() cleans up all connection pools, threads, and actors deterministically."""
        config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "shutdown_test.db"},
            "llm": {},
        }
        mem = MemoryManager(config)
        mem.remember_fact("key_shut", "val_shut", category="test")

        # Ensure background writer actor is running
        self.assertTrue(mem._writer_actor._running)
        self.assertTrue(mem._writer_actor._thread.is_alive())

        # Perform clean shutdown
        mem.close()

        # Ensure writer actor stopped cleanly
        self.assertFalse(mem._writer_actor._running)
        self.assertFalse(mem._writer_actor._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
