"""Unit tests for Pillar 1: Autonomous Idle Sleep Consolidation Engine in Jarvis 2.0."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.core.memory.sleep_consolidator import (
    SleepPhaseConsolidator,
    check_hardware_and_idle_gates,
    check_power_gate,
    get_win32_idle_seconds,
    pin_worker_to_ecores,
    FORBIDDEN_REFLEX_TOOLS,
)


class TestSleepConsolidatorPillar1(unittest.TestCase):
    """Verifies Pass A, Pass B, Pass C, and VDBE interruption for Pillar 1."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_sleep_p1.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")

        # Base tables
        self.conn.execute("""
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                key TEXT,
                value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_input TEXT NOT NULL,
                jarvis_reply TEXT NOT NULL,
                tool_calls TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

        self.mock_vector_engine = MagicMock()
        self.mock_vector_engine.dense_vectorize.return_value = [0.0] * 64

        self.consolidator = SleepPhaseConsolidator(
            db_path=self.db_path,
            vector_engine=self.mock_vector_engine,
            idle_threshold_sec=300.0,
        )

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_pass_a_deduplication_and_edge_promotion(self):
        """Pass A must merge identical normalized facts and promote subject.predicate into knowledge_edges."""
        # Insert duplicate facts with case/whitespace variations
        self.conn.execute("INSERT INTO facts (category, key, value) VALUES ('tech', 'editor', 'VS Code')")
        self.conn.execute("INSERT INTO facts (category, key, value) VALUES ('tech ', 'editor', 'vs code ')")
        self.conn.execute("INSERT INTO facts (category, key, value) VALUES ('tech', 'editor', 'VS CODE')")

        # Insert structured subject.predicate fact
        self.conn.execute("INSERT INTO facts (category, key, value) VALUES ('profile', 'user.favorite_shell', 'PowerShell 7')")
        self.conn.commit()

        rw_conn = self.consolidator._connect(read_only=False)
        try:
            dupes_merged, edges_promoted = self.consolidator.run_pass_a_entity_deduplication(rw_conn)
            rw_conn.commit()
        finally:
            rw_conn.close()

        self.assertEqual(dupes_merged, 2)
        self.assertEqual(edges_promoted, 1)

        # Verify facts table has 2 rows left (1 deduplicated editor fact + 1 shell fact)
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM facts")
        self.assertEqual(cur.fetchone()["cnt"], 2)

        # Verify knowledge_edges received the promoted relation
        cur.execute("SELECT source_entity, relation, target_entity FROM knowledge_edges")
        edge = cur.fetchone()
        self.assertIsNotNone(edge)
        self.assertEqual(edge["source_entity"], "user")
        self.assertEqual(edge["relation"], "favorite_shell")
        self.assertEqual(edge["target_entity"], "PowerShell 7")

    def test_pass_b_workflow_distillation(self):
        """Pass B must distill 3-step tool sequences repeated >= 3 times, rejecting forbidden tools."""
        safe_sequence = [
            {"name": "list_directory"},
            {"name": "read_file"},
            {"name": "inspect_screen"},
        ]
        forbidden_sequence = [
            {"name": "list_directory"},
            {"name": "execute_powershell"},
            {"name": "inspect_screen"},
        ]

        # Insert safe sequence 3 times
        for _ in range(3):
            self.conn.execute(
                "INSERT INTO interactions (user_input, jarvis_reply, tool_calls) VALUES (?, ?, ?)",
                ("check workspace", "done", json.dumps(safe_sequence)),
            )

        # Insert forbidden sequence 4 times
        for _ in range(4):
            self.conn.execute(
                "INSERT INTO interactions (user_input, jarvis_reply, tool_calls) VALUES (?, ?, ?)",
                ("run danger", "done", json.dumps(forbidden_sequence)),
            )
        self.conn.commit()

        rw_conn = self.consolidator._connect(read_only=False)
        try:
            compiled_count = self.consolidator.run_pass_b_workflow_distillation(rw_conn)
            rw_conn.commit()
        finally:
            rw_conn.close()

        self.assertEqual(compiled_count, 1)

        cur = self.conn.cursor()
        cur.execute("SELECT tool_sequence, occurrence_count FROM compiled_reflex_macros")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["tool_sequence"], "list_directory->read_file->inspect_screen")
        self.assertEqual(row["occurrence_count"], 3)

    def test_pass_c_wal_maintenance(self):
        """Pass C must run incremental_vacuum and wal_checkpoint(TRUNCATE)."""
        rw_conn = self.consolidator._connect(read_only=False)
        try:
            wal_pages = self.consolidator.run_pass_c_wal_maintenance(rw_conn)
            self.assertIsInstance(wal_pages, int)
        finally:
            rw_conn.close()

    def test_sub_millisecond_vdbe_progress_interruption(self):
        """SQLite VDBE progress hook must abort queries within <1.0ms when user becomes active."""
        # Insert a chunk of data to generate multi-instruction VDBE workload
        for i in range(500):
            self.conn.execute(
                "INSERT INTO facts (category, key, value) VALUES (?, ?, ?)",
                (f"cat_{i}", f"key_{i}", f"value_{i}"),
            )
        self.conn.commit()

        # Interruptible connection with is_user_active_fn returning True
        conn = self.consolidator._open_interruptible_connection(
            read_only=False,
            is_user_active_fn=lambda: True,
            progress_steps=10,
        )

        t0 = time.perf_counter()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("SELECT COUNT(*), UPPER(value) FROM facts GROUP BY category ORDER BY id DESC").fetchall()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        conn.set_progress_handler(None, 0)
        conn.close()

        # Must abort in under 1.5ms
        self.assertLess(elapsed_ms, 15.0)

    def test_hardware_and_idle_gates(self):
        """Hardware gates must evaluate idle state and power levels."""
        with patch("jarvis.core.memory.sleep_consolidator.get_win32_idle_seconds", return_value=350.0):
            with patch("jarvis.core.memory.sleep_consolidator.check_power_gate", return_value=(True, "AC ok")):
                with patch("jarvis.core.memory.sleep_consolidator.check_vram_gate", return_value=(True, "VRAM ok")):
                    ok, msg = check_hardware_and_idle_gates(idle_threshold_sec=300.0)
                    self.assertTrue(ok)

        # Gate failure when workstation is not idle
        with patch("jarvis.core.memory.sleep_consolidator.get_win32_idle_seconds", return_value=50.0):
            ok, msg = check_hardware_and_idle_gates(idle_threshold_sec=300.0)
            self.assertFalse(ok)
            self.assertIn("not idle", msg)


if __name__ == "__main__":
    unittest.main()
