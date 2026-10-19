"""Automated tests for Tier 3 Episodic Reflection (Reflexion Architecture) in Jarvis 2.0.
Verifies error post-mortems, deduplication, hybrid RRF recall, auto-reflection on tool failure,
and in-context heuristic prompt injection.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
import jarvis.skills.self_evolution as self_evo


class TestEpisodicReflectionSuite(unittest.TestCase):
    """Verifies SQLite WAL persistence, lessons_vec KNN, deduplication, and brain integration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "test_memory.db", "background_learning": False},
            "llm": {"provider": "ollama", "temperature": 0.2},
        }
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)
        self_evo.set_memory_manager(self.memory)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_and_deduplicate_episodic_lesson(self):
        """Verifies recording lessons into episodic_lessons and lessons_vec with deduplication."""
        id1 = self.memory.record_episodic_lesson(
            tool_name="focus_window",
            trigger_context="tool:focus_window args:{\"app_name\": \"Slack\"}",
            failure_signature="Could not find any window matching 'Slack'",
            lesson="Verify 'Slack' is running via list_running_applications or start it via launch_application before focusing.",
        )
        self.assertGreater(id1, 0)

        # Record again with identical tool and signature
        id2 = self.memory.record_episodic_lesson(
            tool_name="focus_window",
            trigger_context="tool:focus_window args:{\"app_name\": \"Slack\"}",
            failure_signature="Could not find any window matching 'Slack'",
            lesson="Verify 'Slack' is running via list_running_applications or start it via launch_application before focusing.",
        )
        self.assertEqual(id1, id2, "Duplicate failure signature must update existing row, not insert a duplicate.")

        with self.memory._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT hit_count, lesson FROM episodic_lessons WHERE id = ?", (id1,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["hit_count"], 2)

            # Verify lessons_vec table exists and row is indexed
            cursor.execute("SELECT lesson_id FROM lessons_vec WHERE lesson_id = ?", (id1,))
            vec_row = cursor.fetchone()
            self.assertIsNotNone(vec_row, "Lesson must be indexed in lessons_vec")

    def test_hybrid_rrf_recall_episodic_lessons(self):
        """Verifies hybrid RRF search across episodic_lessons_fts and lessons_vec."""
        self.memory.record_episodic_lesson(
            tool_name="focus_window",
            trigger_context="tool:focus_window",
            failure_signature="Could not find window matching 'Discord'",
            lesson="Check running applications before trying to focus Discord.",
        )
        self.memory.record_episodic_lesson(
            tool_name="patch_file",
            trigger_context="tool:patch_file",
            failure_signature="Target file not found at path /tmp/nonexistent.py",
            lesson="Inspect file existence and contents with read_file prior to patching.",
        )

        t0 = time.perf_counter()
        results = self.memory.recall_episodic_lessons(query="bring Discord to front", top_k=2)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertLess(elapsed_ms, 200.0, "Recall must execute quickly")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["tool_name"], "focus_window")
        self.assertIn("Discord", results[0]["lesson"])
        self.assertIn("rrf_score", results[0])

    def test_auto_reflection_on_tool_failure_in_brain(self):
        """Verifies JarvisBrain automatically reflects and records lessons when tools fail."""
        brain = JarvisBrain(self.config, self.memory, self.safety)

        # 1. Error execution triggers reflection
        brain._record_tool_execution(
            tool_name="focus_window",
            tool_args={"app_name": "Spotify"},
            tool_result="Could not find any window matching 'Spotify'",
        )

        lessons = self.memory.recall_episodic_lessons(query="Spotify", tool_name="focus_window")
        self.assertGreater(len(lessons), 0)
        self.assertEqual(lessons[0]["tool_name"], "focus_window")
        self.assertIn("list_running_applications", lessons[0]["lesson"])

        # 2. Success does NOT trigger error reflection
        brain._record_tool_execution(
            tool_name="adjust_volume",
            tool_args={"level": 50},
            tool_result="Volume set to 50%",
        )
        vol_lessons = self.memory.recall_episodic_lessons(query="volume", tool_name="adjust_volume")
        self.assertEqual(len(vol_lessons), 0)

    def test_prompt_injection_of_episodic_lessons(self):
        """Verifies past episodic lessons are injected into system prompt when relevant."""
        brain = JarvisBrain(self.config, self.memory, self.safety)

        # Record a past failure lesson
        self.memory.record_episodic_lesson(
            tool_name="focus_window",
            trigger_context="tool:focus_window args:{\"app_name\": \"Terminal\"}",
            failure_signature="Could not find window matching 'Terminal'",
            lesson="Verify 'Terminal' is running via list_running_applications before focusing.",
        )

        prompt = brain._get_system_instruction(user_text="Please focus the Terminal window")
        self.assertIn("Past Episodic Lessons", prompt)
        self.assertIn("focus_window", prompt)
        self.assertIn("list_running_applications", prompt)

        # Unrelated query should not trigger this lesson
        prompt_unrelated = brain._get_system_instruction(user_text="What is the weather today?")
        self.assertNotIn("Past Episodic Lessons", prompt_unrelated)

    def test_self_evolution_skills_episodic_reflection(self):
        """Verifies the recall_episodic_lessons and record_episodic_lesson skills work as expected."""
        rec_res = self_evo.record_episodic_lesson(
            tool_name="browse_and_read_webpage",
            failure_signature="Failed to connect to invalid://site",
            lesson="Ensure URL has a valid http/https scheme and is reachable.",
        )
        self.assertIn("successfully cataloged", rec_res)

        recall_res = self_evo.recall_episodic_lessons(query="browse website")
        self.assertIn("browse_and_read_webpage", recall_res)
        self.assertIn("Ensure URL has a valid http/https", recall_res)


if __name__ == "__main__":
    unittest.main()
