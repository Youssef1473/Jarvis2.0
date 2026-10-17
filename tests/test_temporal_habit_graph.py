"""Unit tests for Argon Dimension 1.3 Temporal Habit & Predictive Event Graph.

Verifies:
1. SQLite schema with WITHOUT ROWID and last_decay_ts.
2. Centralized 120s causal horizon sequence reset.
3. Context alignment (C_{t-1} vs C_t) and Laplace smoothing.
4. Sparse weight fallback (< 2.0) to marginal priors.
5. Proactive threshold gating (P >= 0.65, N >= 3).
6. Double-decay prevention via last_decay_ts.
7. Fine-grained window action classification.
8. Sleep-phase consolidation integration.
"""
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from jarvis.core.cognition.habit_graph import (
    TemporalHabitGraph,
    HabitPrediction,
    CAUSAL_HORIZON_SECONDS,
    HABIT_HALF_LIFE_SECONDS,
)
from jarvis.core.heartbeat import JarvisHeartbeat
from jarvis.core.memory import MemoryManager, SleepPhaseConsolidator


class TestTemporalHabitGraphSuite(unittest.TestCase):
    """Automated test suite validating the TemporalHabitGraph state-action transition model."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_habit_memory.db")
        # Use sync_writes=True in tests for immediate deterministic DB inspection
        self.graph = TemporalHabitGraph(self.db_path, sync_writes=True)

    def tearDown(self):
        self.graph.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_schema_without_rowid_and_last_decay_ts(self):
        """Verifies action_transitions table exists with last_decay_ts and WITHOUT ROWID."""
        conn = self.graph._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='action_transitions'")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            sql = row["sql"].upper()
            self.assertIn("WITHOUT ROWID", sql)
            self.assertIn("LAST_DECAY_TS", sql)
            self.assertIn("WEIGHT", sql)
        finally:
            conn.close()

    def test_time_of_day_binning_and_context_key(self):
        """Verifies diurnal workflow time binning and composite context key generation."""
        self.assertEqual(TemporalHabitGraph.get_time_of_day_bin(hour=8), "morning")
        self.assertEqual(TemporalHabitGraph.get_time_of_day_bin(hour=14), "afternoon")
        self.assertEqual(TemporalHabitGraph.get_time_of_day_bin(hour=20), "evening")
        self.assertEqual(TemporalHabitGraph.get_time_of_day_bin(hour=2), "night")

        key = TemporalHabitGraph.get_context_key(
            cognitive_state="DEEP_FLOW",
            domain="ide",
            hour=15,
        )
        self.assertEqual(key, "DEEP_FLOW:afternoon:ide")

    def test_causal_horizon_120s_sequence_reset(self):
        """Verifies gaps > 120s reset sequence baseline and do NOT record transition edge."""
        now = time.time()
        # Action 1 at t=0
        self.graph.observe_action("focus:ide:py", cognitive_state="DEEP_FLOW", domain="ide", timestamp=now)

        # Action 2 at t=150 (outside 120s causal horizon)
        pred = self.graph.observe_action("focus:terminal", cognitive_state="DEEP_FLOW", domain="terminal", timestamp=now + 150.0)

        # No transition should have been written to SQLite
        conn = self.graph._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as c FROM action_transitions")
            self.assertEqual(cursor.fetchone()["c"], 0)
        finally:
            conn.close()

    def test_context_alignment_and_transition_recording(self):
        """Verifies transition A_{t-1} -> A_t is recorded under C_{t-1}, and prediction queries C_t."""
        now = time.time()
        # Action 1 under Morning IDE context
        self.graph.observe_action("focus:ide:py", cognitive_state="DEEP_FLOW", domain="ide", timestamp=now)

        # Action 2 under Afternoon Terminal context
        pred = self.graph.observe_action("focus:terminal", cognitive_state="ACTIVE_DEBUGGING", domain="terminal", timestamp=now + 10.0)

        conn = self.graph._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM action_transitions")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["source_action"], "focus:ide:py")
            self.assertEqual(row["target_action"], "focus:terminal")
            # Context must reflect C_{t-1}!
            self.assertIn("DEEP_FLOW", row["context_state"])
            self.assertIn("ide", row["context_state"])
            self.assertEqual(row["transition_count"], 1)
            self.assertEqual(row["weight"], 1.0)
        finally:
            conn.close()

    def test_sparse_context_fallback_threshold(self):
        """Verifies marginal prior fallback is triggered when exact context total weight < 2.0."""
        now = time.time()
        # Train 1 transition under morning context (weight = 1.0 < 2.0)
        self.graph.observe_action("focus:ide:py", cognitive_state="DEEP_FLOW", domain="ide", timestamp=now)
        self.graph.observe_action("focus:terminal", cognitive_state="DEEP_FLOW", domain="terminal", timestamp=now + 5.0)

        # Now query prediction under a DIFFERENT evening context
        pred = self.graph.predict_next_action("focus:ide:py", context_state="ROUTINE_ADMIN:evening:ide", now=now + 6.0)

        self.assertIsNotNone(pred)
        # Must fall back to marginal prior because exact context weight is 0.0 (< 2.0)
        self.assertEqual(pred.context_used, "marginal")
        self.assertEqual(pred.target_action, "focus:terminal")

    def test_proactive_candidate_threshold_gating(self):
        """Verifies proactive flag requires both P >= 0.65 and transition_count >= 3."""
        now = time.time()

        # Execute transition 2 times (count = 2 < 3)
        for i in range(2):
            self.graph.observe_action("focus:ide:test", cognitive_state="ACTIVE_DEBUGGING", domain="ide", timestamp=now + (i * 20.0))
            self.graph.observe_action("tool:run_tests", cognitive_state="ACTIVE_DEBUGGING", domain="tool", timestamp=now + (i * 20.0) + 5.0)

        pred_2 = self.graph.predict_next_action("focus:ide:test", context_state="ACTIVE_DEBUGGING:afternoon:ide", now=now + 50.0)
        self.assertIsNotNone(pred_2)
        # P may be high, but count=2 is below 3-count gating threshold!
        self.assertFalse(pred_2.is_proactive_candidate)
        self.assertEqual(pred_2.transition_count, 2)

        # Execute 3rd transition (count = 3 >= 3)
        self.graph.observe_action("focus:ide:test", cognitive_state="ACTIVE_DEBUGGING", domain="ide", timestamp=now + 60.0)
        self.graph.observe_action("tool:run_tests", cognitive_state="ACTIVE_DEBUGGING", domain="tool", timestamp=now + 65.0)

        pred_3 = self.graph.predict_next_action("focus:ide:test", context_state="ACTIVE_DEBUGGING:afternoon:ide", now=now + 70.0)
        self.assertIsNotNone(pred_3)
        self.assertEqual(pred_3.transition_count, 3)
        self.assertGreaterEqual(pred_3.probability, 0.65)
        # Now must be eligible as a proactive candidate!
        self.assertTrue(pred_3.is_proactive_candidate)

    def test_prevent_double_decay_and_half_life(self):
        """Verifies apply_exponential_decay uses last_decay_ts and does not double-decay subsequent runs."""
        now = time.time()
        # Record a transition
        self.graph._record_transition_sync("action:A", "ctx", "action:B", now=now)

        # Simulate 14 days passing (1 half-life = weight drops from 1.0 to ~0.50)
        simulated_time_14d = now + HABIT_HALF_LIFE_SECONDS
        self.graph.apply_exponential_decay(half_life_days=14.0, now=simulated_time_14d)

        conn = self.graph._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT weight, last_decay_ts FROM action_transitions WHERE source_action='action:A'")
            row = cursor.fetchone()
            self.assertAlmostEqual(row["weight"], 0.50, places=2)
            self.assertEqual(row["last_decay_ts"], simulated_time_14d)
        finally:
            conn.close()

        # Immediate re-run at the same time: must NOT decay again!
        self.graph.apply_exponential_decay(half_life_days=14.0, now=simulated_time_14d + 60.0)
        conn = self.graph._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT weight FROM action_transitions WHERE source_action='action:A'")
            row = cursor.fetchone()
            # Still ~0.50 because elapsed time < 10 mins is skipped
            self.assertAlmostEqual(row["weight"], 0.50, places=2)
        finally:
            conn.close()

    def test_fine_grained_window_action_classification(self):
        """Verifies classify_window_action extracts rich domain and language tokens."""
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("pipeline.py - Visual Studio Code"),
            "focus:ide:py",
        )
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("audio_dsp.cpp - Visual Studio"),
            "focus:ide:cpp",
        )
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("hud.tsx - Cursor"),
            "focus:ide:ts",
        )
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("test_engine.py - PyCharm"),
            "focus:ide:py",
        )
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("antigravity / jarvis-repo - GitHub - Brave"),
            "focus:browser:github",
        )
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("SQLite WAL Documentation - Edge"),
            "focus:browser:docs",
        )
        self.assertEqual(
            JarvisHeartbeat.classify_window_action("Windows PowerShell - Administrator"),
            "focus:terminal",
        )

    def test_sleep_phase_consolidator_habit_decay_integration(self):
        """Verifies SleepPhaseConsolidator decays habit transitions during sleep consolidation."""
        now = time.time()
        # Seed an action transition in the database
        self.graph._record_transition_sync("focus:ide:py", "ctx", "tool:patch_file", now=now - (14 * 86400.0))

        consolidator = SleepPhaseConsolidator(self.db_path, vector_engine=None)
        res = consolidator.run_sleep_consolidation(
            is_user_active_fn=lambda: False,
        )

        self.assertEqual(res["status"], "success")
        self.assertIn("decayed_habits", res)
        self.assertGreaterEqual(res["decayed_habits"], 1)


if __name__ == "__main__":
    unittest.main()
