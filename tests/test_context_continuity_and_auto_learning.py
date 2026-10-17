"""Tests for continuous context retention, dual-tier background crystallization, and credential redaction."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.core.memory import MemoryManager
from jarvis.core.brain import JarvisBrain
from jarvis.core.safety import SafetyGate


class TestContextContinuityAndAutoLearning(unittest.TestCase):
    """Verifies dual-tier memory crystallization, topic retention, and security redaction."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {
                "db_path": "memory.db",
                "background_learning": True,
            },
            "llm": {
                "provider": "antigravity",
                "antigravity_model": "gemini-3.8-flash-low",
            },
        }
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dual_tier_crystallization_preference(self):
        """Tests that user preferences and habits crystallize into both Tier 1 user_preferences
        and Tier 2 facts table with vector search support."""
        mock_insights = [
            {"category": "preference", "key": "favorite_editor", "value": "Visual Studio Code with Vim bindings"},
            {"category": "habit", "key": "coding_hours", "value": "Prefers late night architecture sessions with espresso"},
            {"category": "topic", "key": "active_project_focus", "value": "Designing autonomous self-learning memory pipeline"},
        ]

        def mock_extractor(user_text, reply_text):
            return mock_insights

        self.memory.auto_learn_from_interaction(
            user_text="I always use Visual Studio Code with Vim bindings and late night coding sessions with espresso.",
            reply_text="Noted with pleasure, Sir.",
            extractor_fn=mock_extractor,
        )

        # Allow background daemon thread to complete
        time.sleep(0.3)

        # 1. Tier 1 Verification: user_preferences contains preference & habit
        prefs = self.memory.get_user_preferences()
        self.assertEqual(prefs.get("favorite_editor"), "Visual Studio Code with Vim bindings")
        self.assertEqual(prefs.get("coding_hours"), "Prefers late night architecture sessions with espresso")

        # 2. Tier 2 Verification: facts table contains all items including topic
        fact_editor = self.memory.get_fact("favorite_editor")
        self.assertEqual(fact_editor, "Visual Studio Code with Vim bindings")

        fact_topic = self.memory.get_fact("active_project_focus")
        self.assertEqual(fact_topic, "Designing autonomous self-learning memory pipeline")

        # 3. Context Summary Verification: prompt context reflects knowledge
        summary = self.memory.get_context_summary(query="what is my favorite editor")
        self.assertIn("Visual Studio Code with Vim bindings", summary)

    def test_credential_and_sensitive_token_redaction(self):
        """Verifies that secrets, bearer tokens, and API keys are blocked from memory inscription."""
        mock_insights_with_leak = [
            {"category": "preference", "key": "github_auth", "value": "api_key=ghp_9876543210abcdefghijklmnop"},
            {"category": "topic", "key": "cloud_db", "value": "password: supersecret12345678"},
            {"category": "preference", "key": "safe_setting", "value": "Always use dark mode"},
        ]

        def mock_extractor(user_text, reply_text):
            return mock_insights_with_leak

        self.memory.auto_learn_from_interaction(
            user_text="Here is my token and db password and I like dark mode.",
            reply_text="Acknowledged, Sir.",
            extractor_fn=mock_extractor,
        )

        time.sleep(0.3)

        # Sensitive items must NOT be in preferences or facts
        prefs = self.memory.get_user_preferences()
        self.assertNotIn("github_auth", prefs)
        self.assertNotIn("cloud_db", prefs)
        self.assertEqual(prefs.get("safe_setting"), "Always use dark mode")

        self.assertIsNone(self.memory.get_fact("github_auth"))
        self.assertIsNone(self.memory.get_fact("cloud_db"))
        self.assertEqual(self.memory.get_fact("safe_setting"), "Always use dark mode")

    def test_trivial_filter_skips_extraction(self):
        """Verifies fast filter ignores trivial greetings or single-word confirmations."""
        extractor_called = False

        def mock_extractor(user_text, reply_text):
            nonlocal extractor_called
            extractor_called = True
            return []

        self.memory.auto_learn_from_interaction(
            user_text="ok",
            reply_text="Very good, Sir.",
            extractor_fn=mock_extractor,
        )

        time.sleep(0.1)
        self.assertFalse(extractor_called)


if __name__ == "__main__":
    unittest.main()
