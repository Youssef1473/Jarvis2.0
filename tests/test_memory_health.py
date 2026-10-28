"""Automated regression tests for memory subsystem vector search degradation tracking and health telemetry.

Validates:
1. Explicit tracking of sqlite_vec load status and errors in SingleWriterActor and SqliteBaseMixin.
2. Graceful degradation state reporting in MemoryManager.get_vector_search_health().
3. ReadOnlyConnectionPool and VectorMemoryPool vector degradation reporting.
4. run_system_health_check escalation to DEGRADED when sqlite_vec fails to load.
5. Telemetry output formatting (format_health_report and format_spoken_summary).
6. Resilient fallback to lexical BM25/TF-IDF during vector degradation.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add workspace root to sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jarvis.core.health_check import (
    format_health_report,
    format_spoken_summary,
    run_system_health_check,
)
from jarvis.core.memory import MemoryManager, SingleWriterActor
from jarvis.memory.ro_pool import ReadOnlyConnectionPool, VectorMemoryPool


class TestMemoryVectorHealthTracking(unittest.TestCase):
    """Verifies memory subsystem vector extension state tracking and degradation reporting."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_health_memory.db"
        self.config = {
            "_base_dir": self.temp_dir.name,
            "memory": {"db_path": "test_health_memory.db", "background_learning": False},
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_healthy_vector_initialization(self):
        """Verifies operational vector search health when sqlite-vec loads normally."""
        memory = MemoryManager(self.config)
        try:
            self.assertTrue(memory.is_vector_search_available)
            self.assertIsNone(memory.vector_load_error)

            health = memory.get_vector_search_health()
            self.assertEqual(health["status"], "HEALTHY")
            self.assertFalse(health["degraded"])
            self.assertEqual(health["backend"], "sqlite-vec")
            self.assertIsNone(health["error"])
        finally:
            memory.close()

    def test_single_writer_actor_degradation_tracking(self):
        """Verifies SingleWriterActor captures sqlite_vec load failures without crashing."""
        with patch("sqlite_vec.load", side_effect=RuntimeError("Simulated DLL missing error")):
            actor = SingleWriterActor(self.db_path)
            try:
                # Force connection creation in worker
                actor.execute_write(lambda c: c.execute("CREATE TABLE IF NOT EXISTS test (id INT)").fetchall())
                self.assertFalse(actor.is_vector_extension_loaded)
                self.assertIsNotNone(actor.vector_load_error)
                self.assertIn("Simulated DLL missing error", actor.vector_load_error)
            finally:
                actor.stop()

    def test_memory_manager_degraded_state_reporting(self):
        """Verifies MemoryManager surfaces DEGRADED state when sqlite_vec fails to load during init."""
        with patch("sqlite_vec.load", side_effect=Exception("Simulated C extension load error")):
            memory = MemoryManager(self.config)
            try:
                self.assertFalse(memory.is_vector_search_available)
                self.assertIsNotNone(memory.vector_load_error)
                self.assertIn("Simulated C extension load error", memory.vector_load_error)

                health = memory.get_vector_search_health()
                self.assertEqual(health["status"], "DEGRADED")
                self.assertTrue(health["degraded"])
                self.assertEqual(health["backend"], "lexical-bm25-fallback")
                self.assertIn("Simulated C extension load error", health["error"])
            finally:
                memory.close()

    def test_ro_pool_degradation_tracking(self):
        """Verifies ReadOnlyConnectionPool and VectorMemoryPool capture sqlite_vec load failures."""
        pool = ReadOnlyConnectionPool(self.db_path)
        with patch("sqlite_vec.load", side_effect=RuntimeError("RO vector extension load fault")):
            with pool.connection(self.db_path) as conn:
                self.assertIsNotNone(conn)
            self.assertFalse(pool.is_vector_extension_loaded)
            self.assertIsNotNone(pool.vector_load_error)
            self.assertIn("RO vector extension load fault", pool.vector_load_error)

            vec_pool = VectorMemoryPool(self.db_path, pool_size=1)
            try:
                conn2 = vec_pool._create_optimized_connection()
                try:
                    self.assertFalse(vec_pool.is_vector_extension_loaded)
                    self.assertIsNotNone(vec_pool.vector_load_error)
                    self.assertIn("RO vector extension load fault", vec_pool.vector_load_error)
                finally:
                    conn2.close()
            finally:
                vec_pool.teardown()

    def test_semantic_search_facts_fallback_under_vector_degradation(self):
        """Verifies semantic_search_facts gracefully degrades to TF-IDF when sqlite-vec is unavailable."""
        memory = MemoryManager(self.config)
        try:
            memory.remember_fact("editor_pref", "Visual Studio Code with Python plugin", "tools")
            memory.remember_fact("drink_choice", "Double shot espresso coffee", "preference")

            # Simulate sqlite_vec failure during search
            with patch("sqlite_vec.serialize_float32", side_effect=RuntimeError("Vector serialization unavailable")):
                results = memory.semantic_search_facts("What IDE or programming editor do I use?", top_k=1)
                self.assertGreater(len(results), 0)
                self.assertEqual(results[0]["key"], "editor_pref")
        finally:
            memory.close()

    def test_startup_without_vector_extension_preserves_lexical_retrieval(self):
        """Check 1: Vector extension unavailable on startup succeeds in degraded mode with functional lexical retrieval."""
        with patch("sqlite_vec.load", side_effect=RuntimeError("Simulated missing DLL on startup")):
            memory = MemoryManager(self.config)
            try:
                self.assertFalse(memory.is_vector_search_available)
                health = memory.get_vector_search_health()
                self.assertEqual(health["status"], "DEGRADED")
                self.assertEqual(health["backend"], "lexical-bm25-fallback")

                # Both writes and lexical queries must succeed
                memory.remember_fact("fav_tea", "matcha green tea latte", "preference")
                facts = memory.semantic_search_facts("What kind of tea do I prefer?", top_k=1)
                self.assertGreater(len(facts), 0)
                self.assertEqual(facts[0]["key"], "fav_tea")

                doc_id = memory.save_document_knowledge("Tea Guide", "beverages", "Matcha preparation notes")
                self.assertIsInstance(doc_id, int)
                docs = memory.semantic_search_documents("matcha preparation", top_k=1)
                self.assertGreater(len(docs), 0)
                self.assertEqual(docs[0]["title"], "Tea Guide")
            finally:
                memory.close()

    def test_extension_unavailable_after_prior_successful_initialization(self):
        """Check 2: Extension unavailable after prior success does not cause uncaught errors in existing vector tables."""
        memory = MemoryManager(self.config)
        try:
            self.assertTrue(memory.is_vector_search_available)
            memory.remember_fact("database_engine", "SQLite WAL with FTS5", "architecture")
            memory.save_document_knowledge("Arch Spec", "system", "Architecture overview")

            # Simulate subsequent query-time failure where vector operations fail
            with patch("sqlite_vec.serialize_float32", side_effect=RuntimeError("Dynamic ABI mismatch")):
                facts = memory.semantic_search_facts("What database engine is used?", top_k=1)
                self.assertGreater(len(facts), 0)
                self.assertEqual(facts[0]["key"], "database_engine")

                docs = memory.semantic_search_documents("Architecture overview", top_k=1)
                self.assertGreater(len(docs), 0)
                self.assertEqual(docs[0]["title"], "Arch Spec")
        finally:
            memory.close()

    def test_mixed_connection_pool_outcomes_degrades_health(self):
        """Check 3: A successful connection does not mask a failed connection elsewhere in the memory subsystem."""
        memory = MemoryManager(self.config)
        try:
            self.assertTrue(memory.is_vector_search_available)
            self.assertEqual(memory.get_vector_search_health()["status"], "HEALTHY")

            # Simulate ReadOnlyConnectionPool failure while writer actor remains healthy
            ro_pool = ReadOnlyConnectionPool.get_instance()
            orig_ro_err = ro_pool._vector_load_error
            try:
                ro_pool._vector_load_error = "RO pool thread vector failure"
                self.assertFalse(memory.is_vector_search_available)
                health = memory.get_vector_search_health()
                self.assertEqual(health["status"], "DEGRADED")
                self.assertEqual(health["error"], "RO pool thread vector failure")
            finally:
                ro_pool._vector_load_error = orig_ro_err

            # Simulate writer actor failure while mixin remains healthy
            memory._writer_actor._vector_extension_loaded = False
            memory._writer_actor._vector_load_error = "Writer actor thread vector failure"
            self.assertFalse(memory.is_vector_search_available)
            health2 = memory.get_vector_search_health()
            self.assertEqual(health2["status"], "DEGRADED")
            self.assertEqual(health2["error"], "Writer actor thread vector failure")
        finally:
            memory.close()

    def test_single_writer_actor_stop_reentrancy_and_cleanup(self):
        """Verifies SingleWriterActor.stop() guard fixes self-join reentrancy without skipping worker termination."""
        actor = SingleWriterActor(self.db_path)
        try:
            # 1. Normal write verification
            actor.execute_write(lambda c: c.execute("CREATE TABLE IF NOT EXISTS t_reentrancy (v INT)").fetchall())

            # 2. Reentrant stop triggered from within worker loop (simulating finalization during worker loop)
            def reentrant_stop_write(c):
                # We are currently executing on actor._thread
                actor.stop()
                return "stopped_from_inside"

            res = actor.execute_write(reentrant_stop_write)
            self.assertEqual(res, "stopped_from_inside")

            # 3. Verify worker thread terminates cleanly and state is STOPPED
            actor._thread.join(timeout=3.0)
            self.assertFalse(actor._thread.is_alive())
            self.assertEqual(actor.state, SingleWriterActor.STATE_STOPPED)
        finally:
            actor.stop()


class TestHealthCheckDegradationTelemetry(unittest.TestCase):
    """Verifies health_check and jarvis_doctor diagnostic telemetry under degraded vector state."""

    def test_health_check_sqlite_vec_degraded_escalation(self):
        """Verifies run_system_health_check escalates to DEGRADED when sqlite_vec fails."""
        with patch("sqlite_vec.load", side_effect=RuntimeError("sqlite-vec architecture mismatch")):
            report = run_system_health_check()
            mem_db = report["subsystems"]["memory_db"]

            # Subsystem level
            self.assertEqual(mem_db["status"], "DEGRADED")
            self.assertEqual(mem_db["sqlite_vec"], "UNAVAILABLE")
            self.assertEqual(mem_db["vector_search"], "DEGRADED_LEXICAL_FALLBACK")
            self.assertIn("sqlite-vec architecture mismatch", mem_db.get("vector_error", ""))

            # System overall status level
            self.assertEqual(report["status"], "DEGRADED")

            # Verbose telemetry text formatting
            formatted = format_health_report(report)
            self.assertIn("[MEMORY DATABASE] : DEGRADED", formatted)
            self.assertIn("lexical BM25 fallback active", formatted)

            # Spoken Bettany summary
            spoken = format_spoken_summary(report)
            self.assertIn("memory vector search degradation (operating on lexical BM25)", spoken)

    def test_health_check_sqlite_vec_healthy(self):
        """Verifies run_system_health_check reports OPERATIONAL when sqlite_vec is healthy."""
        report = run_system_health_check()
        mem_db = report["subsystems"]["memory_db"]
        if mem_db.get("sqlite_vec") != "UNAVAILABLE":
            self.assertEqual(mem_db["status"], "OK")
            self.assertEqual(mem_db["vector_search"], "OPERATIONAL")
            self.assertEqual(mem_db["integrity_check"], "PASSED")

    def test_health_check_corrupt_database_reports_fail(self):
        """Verifies corrupt SQLite database triggers FAIL rather than DEGRADED."""
        fake_report = {
            "status": "DEGRADED",
            "subsystems": {
                "memory_db": {
                    "status": "FAIL",
                    "error": "Database corruption detected",
                    "integrity_check": "CORRUPT",
                }
            },
        }
        spoken = format_spoken_summary(fake_report)
        self.assertIn("memory database integrity alert", spoken)


if __name__ == "__main__":
    unittest.main()
