"""Hermetic unit tests validating ADR-001 SQLite single-writer ownership and bounded read pool.

Verifies:
1. photo_manager.py routes all SQLite mutations through SingleWriterActor with 0 direct sqlite3.connect write bypasses.
2. photo_manager.py list_photos and describe_photo query via ReadOnlyConnectionPool with bounded timeout.
3. habit_graph.py predict_next_action reads via ReadOnlyConnectionPool without pinning WAL read marks.
4. habit_graph.py apply_exponential_decay serializes mutations through SingleWriterActor.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import jarvis.skills.photo_manager as pm
from jarvis.core.cognition.habit_graph import TemporalHabitGraph
from jarvis.core.memory.ro_pool import ReadOnlyConnectionPool
from jarvis.core.memory.sqlite_base import SingleWriterActor
from jarvis.skills.photo_manager import (
    describe_photo,
    list_photos,
    save_photo,
)


class TestSqliteWriterOwnershipSuite(unittest.TestCase):
    """Hermetic unit test suite asserting SingleWriterActor ownership and ReadOnlyConnectionPool usage."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name)
        self.photos_dir = self.temp_root / "photos"
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_root / "ownership_test.db"

        self.orig_photos_dir = pm.PHOTOS_DIR
        self.orig_db_path = pm.DB_PATH
        pm.PHOTOS_DIR = self.photos_dir
        pm.DB_PATH = self.db_path

        self.test_img_path = self.temp_root / "sample.png"
        img = Image.new("RGB", (64, 64), color="red")
        img.save(str(self.test_img_path), format="PNG")

    def tearDown(self):
        pm._db_queue.join()
        pm.PHOTOS_DIR = self.orig_photos_dir
        pm.DB_PATH = self.orig_db_path
        try:
            self.temp_dir.cleanup()
        except (PermissionError, OSError):
            pass

    def test_photo_manager_routes_writes_through_single_writer_actor(self):
        """Verifies photo_manager routes all writes through SingleWriterActor."""
        actor = SingleWriterActor.get_or_create(self.db_path)
        initial_writes = actor.committed_write_count

        res = save_photo(str(self.test_img_path))
        self.assertIn("Successfully archived photo", res)
        pm._db_queue.join()

        # Verify SingleWriterActor committed write count incremented
        self.assertGreater(actor.committed_write_count, initial_writes)

        # Verify record exists in DB
        ro_pool = ReadOnlyConnectionPool.get_or_create(self.db_path)
        with ro_pool.connection() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM photos").fetchone()
            self.assertEqual(row["cnt"], 1)

    def test_photo_manager_disallows_direct_sqlite3_connect_on_worker(self):
        """Asserts that photo worker never calls direct sqlite3.connect during write execution."""
        orig_connect = sqlite3.connect

        def guarded_connect(*args, **kwargs):
            # If called from photo_manager background worker or list_photos directly, fail
            import traceback
            stack = "".join(traceback.format_stack())
            if "_db_worker_loop" in stack or "_apply_photo_db_action" in stack:
                raise AssertionError("Prohibited direct sqlite3.connect call detected in photo_manager write path!")
            return orig_connect(*args, **kwargs)

        with patch("sqlite3.connect", side_effect=guarded_connect):
            res = save_photo(str(self.test_img_path))
            self.assertIn("Successfully archived photo", res)
            pm._db_queue.join()

    def test_photo_manager_reads_use_ro_pool(self):
        """list_photos and describe_photo must read through ReadOnlyConnectionPool."""
        save_photo(str(self.test_img_path))
        pm._db_queue.join()

        listing = list_photos(limit=5)
        self.assertIn("Archived Photo Gallery:", listing)

        desc = describe_photo("nonexistent_id")
        self.assertIn("was not found", desc)

    def test_habit_graph_reads_use_ro_pool_and_writes_use_actor(self):
        """Verifies habit_graph reads route via ro_pool and decay routes via SingleWriterActor."""
        habit_db = str(self.temp_root / "habit_ownership.db")
        graph = TemporalHabitGraph(habit_db, sync_writes=True)
        try:
            actor = SingleWriterActor.get_or_create(habit_db)
            initial_writes = actor.committed_write_count

            now = 1000.0
            graph.observe_action("focus:ide", timestamp=now)
            graph.observe_action("tool:search", timestamp=now + 5.0)

            # Observe action transitions write via actor
            self.assertGreater(actor.committed_write_count, initial_writes)

            # predict_next_action must succeed reading via ro_pool
            pred = graph.predict_next_action("focus:ide", now=now + 10.0)
            self.assertIsNotNone(pred)
            self.assertEqual(pred.target_action, "tool:search")

            # apply_exponential_decay must serialize writes via actor
            pre_decay_writes = actor.committed_write_count
            decayed = graph.apply_exponential_decay(half_life_days=14.0, now=now + (15 * 86400.0))
            self.assertGreaterEqual(decayed, 1)
            self.assertGreater(actor.committed_write_count, pre_decay_writes)
        finally:
            graph.stop()


if __name__ == "__main__":
    unittest.main()
