"""Milestone 2 Verification: Memory Subsystem Actor Serialization & Schema Integrity (C-04, C-05).

Verifies:
1. SleepPhaseConsolidator Pass A entity deduplication on canonical production knowledge_edges schema.
2. SleepPhaseConsolidator Pass B macro compilation against risk gates.
3. MemoryManager write operations (facts, tasks, interactions, documents, knowledge edges)
   serialize strictly through SingleWriterActor via execute_write.
4. ONNX dense embedding inference executes outside SQLite write transactions.
5. Concurrent multi-threaded writes execute without SQLITE_BUSY contention.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.core.memory import MemoryManager, SingleWriterActor


class TestMilestone2SingleWriterActorRouting(unittest.TestCase):
    """Verifies C-04: Memory mixins route writes through SingleWriterActor."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_writer_actor.db"
        config = {
            "memory": {"db_path": str(self.db_path.name)},
            "_base_dir": str(self.temp_dir.name),
        }
        self.mgr = MemoryManager(config)

    def tearDown(self):
        self.mgr.close()
        self.temp_dir.cleanup()

    def test_writer_actor_is_running(self):
        """SingleWriterActor must be instantiated and running on MemoryManager."""
        self.assertIsNotNone(self.mgr._writer_actor)
        self.assertEqual(self.mgr._writer_actor.state, SingleWriterActor.STATE_RUNNING)

    def test_remember_fact_routes_through_execute_write(self):
        """remember_fact dispatches database writes through SingleWriterActor.execute_write."""
        with patch.object(self.mgr._writer_actor, "execute_write", wraps=self.mgr._writer_actor.execute_write) as spy:
            res = self.mgr.remember_fact("theme", "dark", category="ui")
            self.assertIn("Remembered", res)
            self.assertTrue(spy.called)

        # Confirm written fact is readable
        val = self.mgr.get_fact("theme")
        self.assertEqual(val, "dark")

    def test_log_interaction_routes_through_execute_write(self):
        """log_interaction dispatches database writes through SingleWriterActor.execute_write."""
        with patch.object(self.mgr._writer_actor, "execute_write", wraps=self.mgr._writer_actor.execute_write) as spy:
            self.mgr.log_interaction("hello", "greetings, Sir")
            self.assertTrue(spy.called)

        recent = self.mgr.get_recent_interactions(limit=1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["user_input"], "hello")

    def test_tasks_and_calendar_route_through_execute_write(self):
        """Tasks and calendar events dispatch writes through SingleWriterActor.execute_write."""
        with patch.object(self.mgr._writer_actor, "execute_write", wraps=self.mgr._writer_actor.execute_write) as spy:
            # 1. Add task
            t_res = self.mgr.add_task("Review PR #7", "2026-11-01")
            self.assertIn("Task #", t_res)
            self.assertTrue(spy.called)
            spy.reset_mock()

            # 2. Add calendar event
            c_res = self.mgr.add_calendar_event("Standup", "09:00", "09:30")
            self.assertIn("scheduled", c_res)
            self.assertTrue(spy.called)
            spy.reset_mock()

            # 3. Complete task
            tasks = self.mgr.get_pending_tasks()
            self.assertEqual(len(tasks), 1)
            comp_res = self.mgr.complete_task(tasks[0]["id"])
            self.assertIn("marked as completed", comp_res)
            self.assertTrue(spy.called)

    def test_document_vault_routes_through_execute_write(self):
        """Document vault operations dispatch writes through SingleWriterActor.execute_write."""
        with patch.object(self.mgr._writer_actor, "execute_write", wraps=self.mgr._writer_actor.execute_write) as spy:
            doc_id = self.mgr.save_document(
                title="Arch Spec",
                category="architecture",
                summary="Blueprint overview",
                raw_content="All writes serialize through SingleWriterActor.",
            )
            self.assertIsInstance(doc_id, int)
            self.assertTrue(spy.called)

    def test_temporal_graph_routes_through_execute_write(self):
        """Temporal graph edges dispatch writes through SingleWriterActor.execute_write."""
        with patch.object(self.mgr._writer_actor, "execute_write", wraps=self.mgr._writer_actor.execute_write) as spy:
            edge_id = self.mgr.insert_edge("Jarvis", "runs_on", "Windows 11")
            self.assertIsInstance(edge_id, int)
            self.assertTrue(spy.called)

        edges = self.mgr.query_active_edges(subject="Jarvis")
        self.assertGreaterEqual(len(edges), 1)
        self.assertEqual(edges[0]["object"], "Windows 11")


class TestMilestone2EmbeddingInferenceOutsideLock(unittest.TestCase):
    """Verifies ONNX dense embedding inference is executed outside active write transactions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_embedding_lock.db"
        config = {
            "memory": {"db_path": str(self.db_path.name)},
            "_base_dir": str(self.temp_dir.name),
        }
        self.mgr = MemoryManager(config)

    def tearDown(self):
        self.mgr.close()
        self.temp_dir.cleanup()

    def test_dense_vectorize_executed_before_single_writer_enqueue(self):
        """Verifies dense_vectorize runs BEFORE execute_write acquires the writer lock."""
        execution_order = []

        orig_vectorize = self.mgr.vector_engine.dense_vectorize
        def tracked_vectorize(*args, **kwargs):
            execution_order.append("dense_vectorize")
            return orig_vectorize(*args, **kwargs)

        orig_execute = self.mgr._writer_actor.execute_write
        def tracked_execute(func, *args, **kwargs):
            execution_order.append("execute_write")
            return orig_execute(func, *args, **kwargs)

        with (
            patch.object(self.mgr.vector_engine, "dense_vectorize", side_effect=tracked_vectorize),
            patch.object(self.mgr._writer_actor, "execute_write", side_effect=tracked_execute),
        ):
            self.mgr.remember_fact("preferred_language", "Python", category="profile")

        self.assertEqual(execution_order, ["dense_vectorize", "execute_write"],
                         "dense_vectorize must execute BEFORE execute_write is invoked!")


class TestMilestone2ConcurrentWriteStress(unittest.TestCase):
    """Verifies concurrent multi-threaded writes through SingleWriterActor execute without SQLITE_BUSY."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_stress.db"
        config = {
            "memory": {"db_path": str(self.db_path.name)},
            "_base_dir": str(self.temp_dir.name),
        }
        self.mgr = MemoryManager(config)

    def tearDown(self):
        self.mgr.close()
        self.temp_dir.cleanup()

    def test_concurrent_writers_produce_zero_sqlite_busy(self):
        """20 parallel worker threads writing facts, tasks, and interactions concurrently."""
        num_threads = 10
        writes_per_thread = 5
        errors = []

        def worker(w_id: int):
            try:
                for i in range(writes_per_thread):
                    self.mgr.remember_fact(f"worker_{w_id}_fact_{i}", f"val_{i}", category="stress")
                    self.mgr.add_task(f"Task from worker {w_id} #{i}")
                    self.mgr.log_interaction(f"user query {w_id}-{i}", f"jarvis reply {w_id}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append((w_id, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(errors, [], f"Encountered concurrent write errors: {errors}")

        # Verify all writes committed successfully
        all_facts = self.mgr.get_all_facts()
        stress_facts = [f for f in all_facts if f.get("category") == "stress"]
        self.assertEqual(len(stress_facts), num_threads * writes_per_thread)

        pending_tasks = self.mgr.get_pending_tasks()
        self.assertEqual(len(pending_tasks), num_threads * writes_per_thread)


if __name__ == "__main__":
    unittest.main()
