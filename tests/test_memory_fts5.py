"""Comprehensive test suite for SQLite FTS5 Full-Text Search Indexing in Jarvis 2.0.

Verifies:
1. Conversational query sanitization (stop-word pruning, prefix wildcards, special character scrubbing).
2. External content virtual tables and automatic synchronization triggers (INSERT, UPDATE, DELETE).
3. BM25 relevance ranking across facts, archived documents, interactions, and Obsidian chunks.
4. Resilient LIKE query fallback behavior on empty token sets or syntax boundaries.
"""
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from jarvis.core.memory import MemoryManager, sanitize_fts5_query
from jarvis.core.memory.obsidian_sync import ObsidianContextSyncEngine, ObsidianChunk


class TestFTS5QuerySanitizer(unittest.TestCase):
    """Verifies safe SQLite FTS5 query transformation and conversational stop-word removal."""

    def test_single_and_multi_word_sanitization(self):
        self.assertEqual(sanitize_fts5_query("weather"), '"weather"*')
        self.assertEqual(sanitize_fts5_query("python architecture"), '"python"* OR "architecture"*')

    def test_conversational_stopword_pruning(self):
        # Query containing stopwords like 'what', 'is', 'the', 'tell', 'me', 'about'
        sanitized = sanitize_fts5_query("What is the weather today?")
        self.assertEqual(sanitized, '"weather"* OR "today"*')

        sanitized_conv = sanitize_fts5_query("Can you please tell me about our past conversation regarding quantum?")
        self.assertEqual(sanitized_conv, '"quantum"*')

    def test_all_stopwords_fallback(self):
        # When all words are in stopwords list, fallback retains words rather than returning None
        query = "what who when"
        sanitized = sanitize_fts5_query(query)
        self.assertIsNotNone(sanitized)
        self.assertIn('"what"*', sanitized)
        self.assertIn('"who"*', sanitized)
        self.assertIn('"when"*', sanitized)

    def test_punctuation_and_special_character_scrubbing(self):
        # FTS5 control characters (*, ^, :, ", (, ), -, /) must be safely stripped
        query = 'C:\\Projects\\Jarvis (v2.0): "Neural" *test* & [audio]!'
        sanitized = sanitize_fts5_query(query)
        self.assertNotIn(":", sanitized)
        self.assertNotIn("\\", sanitized)
        self.assertNotIn("(", sanitized)
        self.assertNotIn(")", sanitized)
        self.assertNotIn("[", sanitized)
        self.assertNotIn("]", sanitized)
        self.assertNotIn("!", sanitized)
        self.assertIn('"projects"*', sanitized)
        self.assertIn('"jarvis"*', sanitized)
        self.assertIn('"neural"*', sanitized)

    def test_empty_or_invalid_inputs(self):
        self.assertIsNone(sanitize_fts5_query(""))
        self.assertIsNone(sanitize_fts5_query("   "))
        self.assertIsNone(sanitize_fts5_query("!@#$%^&*()"))
        self.assertIsNone(sanitize_fts5_query(None))  # type: ignore


class TestMemoryFTS5Indexing(unittest.TestCase):
    """Verifies SQLite FTS5 external content indexing, triggers, and BM25 ranking for MemoryManager."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_fts5_memory.db"
        self.config = {
            "memory": {
                "db_path": str(self.db_path)
            }
        }
        self.mem = MemoryManager(self.config)

    def tearDown(self):
        import gc
        del self.mem
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_facts_fts_triggers_and_bm25_ranking(self):
        # 1. Insert facts
        self.mem.remember_fact("favorite_language", "Python and Rust", category="programming")
        self.mem.remember_fact("favorite_ide", "Visual Studio Code with Python extensions", category="tools")
        self.mem.remember_fact("pet_name", "Archimedes the cat", category="personal")

        # 2. Search facts with FTS5 BM25
        results = self.mem.search_facts("Python")
        self.assertEqual(len(results), 2)
        # Verify returned structure
        self.assertTrue(any(r["key"] == "favorite_language" for r in results))
        self.assertTrue(any(r["key"] == "favorite_ide" for r in results))

        # 3. Test Trigger on UPDATE (via remember_fact conflict update)
        self.mem.remember_fact("favorite_language", "C++ exclusively", category="programming")
        # Searching for 'Rust' should now yield 0 results for this key
        results_rust = self.mem.search_facts("Rust")
        self.assertEqual(len(results_rust), 0)
        # Searching for 'C++' should now find favorite_language
        results_cpp = self.mem.search_facts("exclusively")
        self.assertEqual(len(results_cpp), 1)
        self.assertEqual(results_cpp[0]["key"], "favorite_language")

        # 4. Direct DELETE trigger verification
        with self.mem._get_connection() as conn:
            conn.execute("DELETE FROM facts WHERE key = 'pet_name'")
            conn.commit()
        results_pet = self.mem.search_facts("Archimedes")
        self.assertEqual(len(results_pet), 0)

    def test_documents_fts_search_and_ranking(self):
        # 1. Save documents
        doc1_id = self.mem.save_document_knowledge(
            title="Audio Concurrency Specification",
            category="architecture",
            summary="PortAudio thread priorities and lock-free ring buffer design for sub-100ms voice pipeline.",
            raw_content="Low-latency audio streaming requires real-time priority on Windows WASAPI.",
        )
        doc2_id = self.mem.save_document_knowledge(
            title="Database WAL Mode Guide",
            category="database",
            summary="SQLite WAL journal mode with PRAGMA busy_timeout prevents multi-threaded locking.",
            raw_content="Database concurrency in Python requires WAL mode for simultaneous reader threads.",
        )

        # 2. Search documents with BM25
        audio_docs = self.mem.search_documents("audio priority")
        self.assertTrue(len(audio_docs) >= 1)
        self.assertEqual(audio_docs[0]["id"], doc1_id)
        self.assertEqual(audio_docs[0]["title"], "Audio Concurrency Specification")

        # 3. Search database docs
        db_docs = self.mem.search_documents("WAL journal")
        self.assertTrue(len(db_docs) >= 1)
        self.assertEqual(db_docs[0]["id"], doc2_id)

    def test_interactions_fts_dialogue_recall(self):
        # 1. Log interactions
        self.mem.log_interaction("What's the weather today?", "The weather in London is currently overcast with light rain, Sir.")
        self.mem.log_interaction("Set a reminder for the design review tomorrow at 3 PM.", "Reminder scheduled for 3 PM tomorrow, Sir.")
        self.mem.log_interaction("What is the speed of light in vacuum?", "Approximately 299,792,458 meters per second, Sir.")

        # 2. Conversational dialogue search
        results_weather = self.mem.search_interactions("What did we say about the weather in London?")
        self.assertEqual(len(results_weather), 1)
        self.assertIn("overcast", results_weather[0]["jarvis_reply"])

        # 3. Keyword recall with BM25 ranking
        results_speed = self.mem.search_interactions("speed of light")
        self.assertTrue(len(results_speed) >= 1)
        # BM25 must rank the joint match ('speed' + 'light') above the single-token match ('light rain')
        self.assertIn("299,792,458", results_speed[0]["jarvis_reply"])

    def test_fts5_rebuild_migration_for_existing_records(self):
        # Verify that if records existed before FTS tables were initialized,
        # the rebuild migration backfills them cleanly
        test_db = Path(self.temp_dir.name) / "preexisting.db"
        conn = sqlite3.connect(str(test_db))
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, category TEXT, key TEXT, value TEXT, updated_at TIMESTAMP)")
        conn.execute("INSERT INTO facts VALUES (1, 'general', 'owner', 'Youssef', CURRENT_TIMESTAMP)")
        conn.commit()
        conn.close()

        # Initialize MemoryManager on the pre-existing db
        cfg = {"memory": {"db_path": str(test_db)}}
        migrated_mem = MemoryManager(cfg)
        res = migrated_mem.search_facts("Youssef")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["value"], "Youssef")


class TestObsidianFTS5Indexing(unittest.TestCase):
    """Verifies SQLite FTS5 BM25 search and tag filtering for ObsidianContextSyncEngine."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.temp_dir.name) / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir.name) / "test_obsidian_fts5.db"

        self.engine = ObsidianContextSyncEngine(
            vault_path=self.vault_dir,
            db_path=self.db_path,
            sync_interval=10.0,
        )

    def tearDown(self):
        import gc
        try:
            self.engine.stop(timeout=1.0)
        except Exception:
            pass
        del self.engine
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_obsidian_fts5_search_and_tag_filter(self):
        # 1. Create notes in vault
        note1 = self.vault_dir / "AudioPipeline.md"
        note1.write_text("""---
title: "Kokoro Audio Architecture"
tags: [audio, cuda, latency]
---
# Reflex Audio
Sub-100ms voice reflex pipeline using pinned P-Cores on Windows WASAPI.
""", encoding="utf-8")

        note2 = self.vault_dir / "MemoryFTS.md"
        note2.write_text("""---
title: "Episodic Memory FTS5"
tags: [memory, sqlite, fts5]
---
# Full Text Search
SQLite FTS5 virtual tables provide sub-millisecond BM25 keyword recall.
""", encoding="utf-8")

        # 2. Sync to DB
        stats = self.engine.sync_now()
        self.assertEqual(stats.total_files, 2)
        self.assertEqual(stats.modified_files, 2)

        # Drain DB writer queue
        while not self.engine._db_queue.empty():
            time.sleep(0.02)

        # 3. Query context using FTS5 BM25
        results_fts = self.engine.query_context("SQLite FTS5 keyword recall", limit=5)
        self.assertTrue(len(results_fts) >= 1)
        self.assertEqual(results_fts[0]["note_title"], "Episodic Memory FTS5")
        self.assertIn("sub-millisecond BM25", results_fts[0]["content"])

        # 4. Query with tag filter
        results_audio = self.engine.query_context("reflex pipeline", limit=5, tag_filter="audio")
        self.assertEqual(len(results_audio), 1)
        self.assertEqual(results_audio[0]["note_title"], "Kokoro Audio Architecture")

        # Query with non-matching tag filter returns empty
        results_none = self.engine.query_context("reflex pipeline", limit=5, tag_filter="unrelated_tag")
        self.assertEqual(len(results_none), 0)


if __name__ == "__main__":
    unittest.main()
