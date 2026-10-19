"""Unit tests for Zero-Contention SQLite WAL Sleep-Phase Memory Consolidation."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory import MemoryManager, VectorMemoryEngine, SleepPhaseConsolidator
from jarvis.core.heartbeat import JarvisHeartbeat


class TestSleepConsolidationWALSuite(unittest.TestCase):
    """Verifies read-only snapshot isolation, micro-batched commits, user activity interruption, and zero lock starvation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = {
            "_base_dir": self.temp_dir.name,
            "memory": {"db_path": "test_sleep_memory.db", "background_learning": False},
            "heartbeat": {"enabled": True, "idle_threshold_seconds": 300},
        }
        self.memory = MemoryManager(self.config)

    def tearDown(self):
        try:
            self.memory.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_discover_redundant_facts_readonly(self):
        """Verifies redundant and near-duplicate facts are identified on an isolated read-only WAL snapshot."""
        # Insert base facts
        self.memory.remember_fact("favorite_beverage", "double espresso coffee", "preference")
        self.memory.remember_fact("favorite_editor", "Visual Studio Code with Python", "tools")

        # Insert near-duplicate fact with high semantic similarity
        self.memory.remember_fact("favorite_drink", "double espresso coffee", "preference")

        consolidator = SleepPhaseConsolidator(str(self.memory.db_path), self.memory.vector_engine)
        redundant_pairs = consolidator.discover_redundant_facts_readonly()

        self.assertGreater(len(redundant_pairs), 0, "Should detect near-duplicate espresso fact")
        keep, stale, dist = redundant_pairs[0]
        self.assertEqual(keep["category"], "preference")
        self.assertIn("espresso", stale["value"].lower())

    def test_apply_consolidation_micro_batches_prunes_stale_facts(self):
        """Verifies applying micro-batches prunes stale facts and updates facts_vec."""
        self.memory.remember_fact("target_fact_1", "test data alpha", "test")
        self.memory.remember_fact("target_fact_2", "test data duplicate alpha", "test")

        consolidator = SleepPhaseConsolidator(str(self.memory.db_path), self.memory.vector_engine)
        pairs = consolidator.discover_redundant_facts_readonly()
        self.assertGreater(len(pairs), 0)

        stale_id = pairs[0][1]["id"]
        pruned_count = consolidator.apply_consolidation_micro_batches(
            stale_fact_ids=[stale_id],
            is_user_active_fn=lambda: False,
            batch_size=10,
        )
        self.assertEqual(pruned_count, 1)

        # Stale fact should no longer be present
        all_facts = self.memory.get_all_facts()
        all_keys = [f["key"] for f in all_facts]
        self.assertNotIn("target_fact_2", all_keys)

    def test_user_activity_aborts_consolidation_instantly(self):
        """Verifies that user activity (is_user_active_fn=True) immediately yields the WAL lock."""
        stale_ids = [101, 102, 103, 104, 105]

        consolidator = SleepPhaseConsolidator(str(self.memory.db_path), self.memory.vector_engine)
        # Simulate user moving mouse / typing immediately
        pruned = consolidator.apply_consolidation_micro_batches(
            stale_fact_ids=stale_ids,
            is_user_active_fn=lambda: True,
            batch_size=2,
        )
        self.assertEqual(pruned, 0, "Consolidation should yield immediately on active user")

    def test_concurrent_fast_path_writes_zero_sqlite_busy(self):
        """Verifies fast-path writes never encounter SQLITE_BUSY during sleep consolidation."""
        # Pre-seed facts
        for i in range(15):
            self.memory.remember_fact(f"fact_{i}", f"value payload {i}", "bench")

        errors: list = []

        def background_fast_path_writer():
            try:
                for j in range(10):
                    self.memory.remember_fact(f"fast_fact_{j}", f"fast value {j}", "fast")
                    time.sleep(0.002)
            except Exception as e:
                errors.append(e)

        writer_thread = threading.Thread(target=background_fast_path_writer)
        writer_thread.start()

        # Run consolidation concurrently
        res = self.memory.consolidate_sleep_phase(is_user_active_fn=lambda: False, batch_size=5)
        writer_thread.join()

        self.assertEqual(len(errors), 0, f"No SQLITE_BUSY errors should occur: {errors}")
        self.assertEqual(res["status"], "success")

    def test_heartbeat_triggers_sleep_consolidation_on_idle(self):
        """Verifies JarvisHeartbeat triggers sleep consolidation when user becomes idle."""
        mock_orch = MagicMock()
        mock_orch.memory = self.memory

        hb = JarvisHeartbeat(mock_orch, self.config)
        hb.idle_threshold_seconds = 300

        # Simulate user idle for 350 seconds
        with patch.object(hb, "get_idle_seconds", return_value=350.0), \
             patch.object(hb, "get_active_window_title", return_value="Desktop"), \
             patch.object(self.memory, "consolidate_sleep_phase") as mock_consolidate:

            hb._check_desktop_presence()
            self.assertTrue(hb._is_user_idle)
            mock_consolidate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
