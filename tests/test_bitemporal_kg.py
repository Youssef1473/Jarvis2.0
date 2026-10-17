"""Automated tests for Bi-Temporal Knowledge Graph, habit prediction, and action journal."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory import MemoryManager
from jarvis.skills.base import SkillRegistry


class TestBiTemporalKGSuite(unittest.TestCase):
    """Verifies bi-temporal knowledge graph edges, automatic supersession, and habit graph."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = {
            "_base_dir": self.temp_dir.name,
            "memory": {"db_path": "test_kg_memory.db", "background_learning": False}
        }
        self.memory = MemoryManager(self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_remember_edge_and_query_active(self):
        """Verifies storing a knowledge edge inserts an active edge with valid_until IS NULL."""
        edge_id = self.memory.remember_edge(
            subject="Sir",
            predicate="primary_os",
            object_val="Windows 11",
            is_functional=True,
            confidence=0.98,
        )
        self.assertIsInstance(edge_id, int)

        active = self.memory.query_active_edges(subject="Sir", predicate="primary_os")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["object"], "Windows 11")
        self.assertIsNone(active[0]["valid_until"])
        self.assertGreater(active[0]["temporal_score"], 0.8)

    def test_automatic_functional_supersession_trigger(self):
        """Verifies inserting a new functional edge automatically closes valid_until on the old edge."""
        edge1 = self.memory.remember_edge(
            subject="Sir",
            predicate="active_focus_project",
            object_val="ÉLÉGANTE",
            is_functional=True,
        )
        time.sleep(0.05)
        edge2 = self.memory.remember_edge(
            subject="Sir",
            predicate="active_focus_project",
            object_val="Jarvis 3.0",
            is_functional=True,
        )

        # Only the newest edge should be active
        active = self.memory.query_active_edges(subject="Sir", predicate="active_focus_project")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["object"], "Jarvis 3.0")

        # The historical edge must still exist in DB with valid_until populated
        with self.memory._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM knowledge_edges WHERE edge_id = ?", (edge1,))
            row = dict(cursor.fetchone())
            self.assertEqual(row["object"], "ÉLÉGANTE")
            self.assertIsNotNone(row["valid_until"], "Old functional edge must have valid_until set by trigger")

    def test_habit_transition_and_prediction(self):
        """Verifies habit transition recording and time-conditioned Laplace action prediction."""
        # Record repeated transition: IDE:Save:.py -> Term:Exec:pytest
        for _ in range(5):
            self.memory.record_habit_transition(
                src_event="IDE:Save:.py",
                dst_event="Term:Exec:pytest",
                hour_bucket=14,
                is_weekend=0,
            )

        pred = self.memory.predict_next_action(
            src_event="IDE:Save:.py",
            hour_bucket=14,
            is_weekend=0,
            min_prob=0.50,
        )
        self.assertIsNotNone(pred)
        dst, prob = pred
        self.assertEqual(dst, "Term:Exec:pytest")
        self.assertGreaterEqual(prob, 0.70)

    def test_action_journal_and_revert(self):
        """Verifies logging reversible mutations to action_journal and executing rollback."""
        # Register a mock reversal skill
        test_state = {"theme": "dark"}

        @SkillRegistry.register("mock_set_theme")
        def mock_set_theme(theme: str) -> str:
            test_state["theme"] = theme
            return f"theme set to {theme}"

        # Perform change to light theme, log inverse dark theme
        test_state["theme"] = "light"
        self.memory.log_reversible_action(
            action_id="act_001",
            skill_name="set_theme",
            risk_level=1,
            inverse_skill="mock_set_theme",
            inverse_payload={"theme": "dark"},
        )

        # Revert action
        success, msg = self.memory.revert_action("act_001")
        self.assertTrue(success)
        self.assertEqual(test_state["theme"], "dark", "Action reversal must execute inverse skill")

        # Re-reverting should fail because already reverted
        success2, msg2 = self.memory.revert_action("act_001")
        self.assertFalse(success2)


if __name__ == "__main__":
    unittest.main()
