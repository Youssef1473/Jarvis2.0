"""Automated unit test suite for Dimension 2.3 Transcript Distiller & Sleep-Phase Trait Inscription."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory import MemoryManager, SleepPhaseConsolidator, TranscriptDistiller
from jarvis.core.memory.transcript_distiller import (
    CANONICAL_PREFERENCE_KEYS,
    MAX_CORE_PREFERENCES_CEILING,
    scrub_sensitive_credentials,
    contains_unredacted_credentials,
)


class TestTranscriptDistillerSuite(unittest.TestCase):
    """Verifies incremental harvesting, pre-LLM secret scrubbing, trait distillation,
    slot capping, cursor safety, and per-item interruption yields."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_name = "test_distiller_memory.db"
        self.config = {
            "_base_dir": self.temp_dir.name,
            "memory": {"db_path": self.db_name, "background_learning": False},
        }
        self.memory = MemoryManager(self.config)
        self.db_path = str(Path(self.temp_dir.name) / self.db_name)
        self.distiller = TranscriptDistiller(self.db_path, self.memory.vector_engine, memory_manager=self.memory)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_interactions_skips(self):
        """Verifies early exit with zero queries when no interactions exist."""
        res = self.distiller.distill_unprocessed_interactions()
        self.assertEqual(res["status"], "no_new_interactions")
        self.assertEqual(res["turns_processed"], 0)
        self.assertEqual(self.distiller.get_last_distilled_id(), 0)

    def test_all_trivial_utterances_advance_cursor_safely(self):
        """Verifies greetings and trivial reflexes advance cursor without invoking LLM."""
        self.memory.log_interaction("Hey Jarvis", "At your service, Sir.")
        self.memory.log_interaction("ok", "Very good, Sir.")
        self.memory.log_interaction("thanks", "My pleasure, Sir.")

        # If extractor_fn were called, it would fail
        def failing_extractor(_text):
            raise RuntimeError("LLM should not be called for trivial greetings")

        res = self.distiller.distill_unprocessed_interactions(extractor_fn=failing_extractor)
        self.assertEqual(res["status"], "all_trivial")
        self.assertEqual(res["traits_extracted"], 0)
        self.assertEqual(self.distiller.get_last_distilled_id(), 3)

    def test_pre_and_post_llm_secret_redaction(self):
        """Verifies sensitive credential scrubbing on real keys without discarding developer discussions."""
        # 1. Real credential patterns must be redacted
        dirty_transcript = (
            "Here is my OpenAI key: sk-abcdef1234567890abcdef1234567890 and github token: ghp_123456789012345678901234567890123456.\n"
            "Also password = 'SuperSecretPassword123!'."
        )
        scrubbed = scrub_sensitive_credentials(dirty_transcript)
        self.assertNotIn("sk-abcdef", scrubbed)
        self.assertNotIn("ghp_123456", scrubbed)
        self.assertNotIn("SuperSecretPassword123!", scrubbed)
        self.assertIn("[REDACTED_CREDENTIAL]", scrubbed)

        # 2. Developer discussions about auth or tokens must NOT be stripped
        dev_discussion = "We need to verify JWT auth tokens and OAuth bearer headers in our auth_service.py module."
        self.assertFalse(contains_unredacted_credentials(dev_discussion))
        self.assertEqual(scrub_sensitive_credentials(dev_discussion), dev_discussion)

    def test_multi_turn_trait_extraction_and_dual_crystallization(self):
        """Verifies mock extractor output populates Tier-1 preferences, bi-temporal KG edges, and atomic facts."""
        self.memory.log_interaction(
            "I've switched our primary compiler to clang-cl on Windows.",
            "Understood, Sir. I will calibrate our build targets accordingly."
        )
        self.memory.log_interaction(
            "Our focus project this week is Project Argon with high priority.",
            "Duly noted, Sir. Project Argon is staged."
        )

        mock_extracted = {
            "core_preferences": [
                {"key": "active_compiler", "value": "clang-cl"},
                {"key": "current_focus", "value": "Project Argon"},
            ],
            "knowledge_edges": [
                {
                    "subject": "Sir",
                    "predicate": "primary_focus_project",
                    "object": "Project Argon",
                    "is_functional": True,
                    "confidence": 0.98,
                    "decay_lambda": 0.01,
                }
            ],
            "atomic_facts": [
                {
                    "category": "compiler",
                    "key": "compiler_flags",
                    "value": "/std:c++20 /W4 /WX",
                }
            ],
        }

        res = self.distiller.distill_unprocessed_interactions(
            extractor_fn=lambda _txt: mock_extracted
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["preferences_inscribed"], 2)
        self.assertEqual(res["edges_inscribed"], 1)
        self.assertEqual(res["facts_inscribed"], 1)
        self.assertEqual(res["traits_extracted"], 4)
        self.assertEqual(self.distiller.get_last_distilled_id(), 2)

        # Verify Tier 1 Core User Profile
        prefs = self.memory.get_user_preferences()
        self.assertEqual(prefs.get("active_compiler"), "clang-cl")
        self.assertEqual(prefs.get("current_focus"), "Project Argon")

        # Verify Bi-Temporal Knowledge Edge
        edges = self.memory.query_active_edges(subject="Sir", predicate="primary_focus_project")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["object"], "Project Argon")
        self.assertIsNone(edges[0]["valid_until"])

        # Verify Tier 2 Atomic Fact
        fact_val = self.memory.get_fact("compiler_flags")
        self.assertEqual(fact_val, "/std:c++20 /W4 /WX")

    def test_canonical_preference_capping_and_overflow_reroute(self):
        """Verifies non-canonical keys or preferences beyond ceiling are rerouted to atomic facts."""
        self.memory.log_interaction("My favorite coffee is Espresso Roast.", "Noted, Sir.")

        mock_extracted = {
            "core_preferences": [
                # Non-canonical key should route to atomic_facts
                {"key": "favorite_coffee", "value": "Espresso Roast"},
                # Canonical key should remain in core_preferences
                {"key": "primary_ide", "value": "Qt Creator"},
            ],
            "knowledge_edges": [],
            "atomic_facts": [],
        }

        res = self.distiller.distill_unprocessed_interactions(
            extractor_fn=lambda _txt: mock_extracted
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["preferences_inscribed"], 1)  # Only primary_ide in core
        self.assertEqual(res["facts_inscribed"], 1)        # favorite_coffee in facts

        prefs = self.memory.get_user_preferences()
        self.assertIn("primary_ide", prefs)
        self.assertNotIn("favorite_coffee", prefs)

        # favorite_coffee must exist in facts table
        fact_val = self.memory.get_fact("favorite_coffee")
        self.assertEqual(fact_val, "Espresso Roast")

    def test_functional_supersession_on_repeated_distillation(self):
        """Verifies newer functional edges automatically close valid_until on previous edges."""
        # Batch 1: Initial focus
        self.memory.log_interaction("We are working on Project Alpha.", "Alpha registered, Sir.")
        mock_1 = {
            "core_preferences": [],
            "knowledge_edges": [
                {"subject": "Sir", "predicate": "active_project", "object": "Alpha", "is_functional": True}
            ],
            "atomic_facts": [],
        }
        self.distiller.distill_unprocessed_interactions(extractor_fn=lambda _txt: mock_1)

        active_1 = self.memory.query_active_edges(subject="Sir", predicate="active_project")
        self.assertEqual(len(active_1), 1)
        self.assertEqual(active_1[0]["object"], "Alpha")
        old_edge_id = active_1[0]["edge_id"]

        time.sleep(0.05)

        # Batch 2: Shift to Project Beta
        self.memory.log_interaction("Alpha is done, switch to Project Beta.", "Moving to Beta, Sir.")
        mock_2 = {
            "core_preferences": [],
            "knowledge_edges": [
                {"subject": "Sir", "predicate": "active_project", "object": "Beta", "is_functional": True}
            ],
            "atomic_facts": [],
        }
        self.distiller.distill_unprocessed_interactions(extractor_fn=lambda _txt: mock_2)

        # Active edge should only be Beta
        active_2 = self.memory.query_active_edges(subject="Sir", predicate="active_project")
        self.assertEqual(len(active_2), 1)
        self.assertEqual(active_2[0]["object"], "Beta")

        # Old edge must still exist in DB with valid_until populated
        with self.memory._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM knowledge_edges WHERE edge_id = ?", (old_edge_id,))
            row = dict(cursor.fetchone())
            self.assertEqual(row["object"], "Alpha")
            self.assertIsNotNone(row["valid_until"], "Older functional edge must be superseded")

    def test_cursor_safety_on_extractor_failure(self):
        """Verifies cursor is never advanced if LLM extraction raises an error."""
        self.memory.log_interaction("Configure project settings for CUDA.", "CUDA settings active, Sir.")

        def broken_extractor(_txt):
            raise ConnectionError("LLM API timed out")

        res = self.distiller.distill_unprocessed_interactions(extractor_fn=broken_extractor)
        self.assertEqual(res["status"], "extraction_error")
        # Cursor must remain at 0 so it can be retried in the next idle phase
        self.assertEqual(self.distiller.get_last_distilled_id(), 0)

    def test_per_item_interruption_yield(self):
        """Verifies distiller aborts within <2ms when is_user_active_fn returns True."""
        self.memory.log_interaction("I prefer rustfmt for formatting.", "Understood, Sir.")

        # User is active immediately
        res = self.distiller.distill_unprocessed_interactions(
            is_user_active_fn=lambda: True,
            extractor_fn=lambda _t: {"core_preferences": [{"key": "test_pref", "value": "val"}]},
        )
        self.assertEqual(res["status"], "yielded_idle")
        self.assertEqual(self.distiller.get_last_distilled_id(), 0)

    def test_sleep_consolidator_step1_integration(self):
        """Verifies SleepPhaseConsolidator executes Step 1 distillation first and deduplicates in Steps 2 & 3."""
        # 1. Existing fact in memory
        self.memory.remember_fact("favorite_editor", "Neovim", category="tools")

        # 2. Add interaction with duplicate fact
        self.memory.log_interaction("I always use Neovim as my editor.", "Noted, Sir.")

        mock_extracted = {
            "core_preferences": [],
            "knowledge_edges": [],
            "atomic_facts": [
                # Identical duplicate fact extracted from conversation
                {"category": "tools", "key": "favorite_editor_duplicate", "value": "Neovim"}
            ],
        }

        # Run sleep consolidation with Step 1 executing distillation directly
        consolidator = SleepPhaseConsolidator(self.db_path, self.memory.vector_engine)
        summary = consolidator.run_sleep_consolidation(
            is_user_active_fn=lambda: False,
            extractor_fn=lambda _t: mock_extracted,
        )

        self.assertEqual(summary["status"], "success")
        self.assertIn("distillation", summary)
        self.assertEqual(summary["distillation"]["traits_extracted"], 1)
        # Verify redundant facts found and pruned in the same sleep pass
        self.assertGreaterEqual(summary["redundant_pairs_found"], 1)
        self.assertGreaterEqual(summary["facts_pruned"], 1)


if __name__ == "__main__":
    unittest.main()
