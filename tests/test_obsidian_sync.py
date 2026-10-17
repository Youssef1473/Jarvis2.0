"""Unit tests for the non-blocking, background-threaded Obsidian Context Sync Engine."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from jarvis.core.memory.obsidian_sync import (
    ObsidianContextSyncEngine,
    ObsidianNoteParser,
    ObsidianChunk,
    SyncStats,
)


class TestObsidianNoteParser(unittest.TestCase):
    """Verifies YAML frontmatter parsing, wiki-link extraction, and markdown chunking."""

    def test_parse_frontmatter_yaml(self):
        sample = """---
title: "Audio Pipeline Architecture"
tags: [audio, cuda, kokoro]
project: "Jarvis 2.0"
date: 2026-10-13
---
# Audio Pipeline
This is the core audio engine using Kokoro-82M.
"""
        fm, body = ObsidianNoteParser.parse_frontmatter(sample)
        self.assertEqual(fm.get("title"), "Audio Pipeline Architecture")
        self.assertEqual(fm.get("project"), "Jarvis 2.0")
        self.assertIn("audio", fm.get("tags", []))
        self.assertIn("cuda", fm.get("tags", []))
        self.assertNotIn("---", body)
        self.assertIn("# Audio Pipeline", body)

    def test_extract_wiki_links(self):
        text = """
See also [[Neural Synthesis Engine]] and [[Kokoro-82M|Kokoro Voice]].
Also reference embedded image ![[Diagram_Flow.png]] and [[WASAPI Device]].
"""
        links = ObsidianNoteParser.extract_wiki_links(text)
        self.assertIn("Neural Synthesis Engine", links)
        self.assertIn("Kokoro-82M", links)
        self.assertIn("WASAPI Device", links)
        self.assertIn("Diagram_Flow.png", links)

    def test_chunk_markdown_hierarchy_and_breadcrumbs(self):
        doc = """---
title: "Concurrency Model"
tags: [concurrency, threads]
---
## Overview
Jarvis uses a multi-threaded architecture with thread priorities.

## Performance Cores
Pinning WASAPI callbacks to P-Cores prevents micro-stutters.

### PortAudio Callback
The callback reads from an AudioRingBuffer lock-free.
"""
        chunks = ObsidianNoteParser.chunk_markdown("System/Concurrency.md", doc, max_chunk_chars=500)
        self.assertTrue(len(chunks) >= 3)

        titles = [c.breadcrumb for c in chunks]
        self.assertTrue(any("Overview" in b for b in titles))
        self.assertTrue(any("Performance Cores" in b for b in titles))
        self.assertTrue(any("PortAudio Callback" in b for b in titles))

        first_chunk = chunks[0]
        self.assertEqual(first_chunk.file_path, "System/Concurrency.md")
        self.assertIn("concurrency", first_chunk.tags)


class TestObsidianContextSyncEngine(unittest.TestCase):
    """Verifies differential sync, WAL transactions, thread lifecycle, and context querying."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.temp_dir.name) / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir.name) / "test_obsidian_memory.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_incremental_sync_lifecycle_and_delta_tracking(self):
        # 1. Create initial notes in vault
        note1 = self.vault_dir / "Architecture.md"
        note1.write_text("""---
title: "Jarvis Architecture"
tags: [architecture, core]
---
# Overview
The architecture is structured into 6 operational pillars.
""", encoding="utf-8")

        sub_dir = self.vault_dir / "Audio"
        sub_dir.mkdir(parents=True, exist_ok=True)
        note2 = sub_dir / "ReflexPipeline.md"
        note2.write_text("""---
title: "Reflex Pipeline"
tags: [voice, audio, reflex]
---
# Streaming Reflex
Sub-100ms voice reflex using circular PCM buffer [[Architecture]].
""", encoding="utf-8")

        engine = ObsidianContextSyncEngine(
            vault_path=self.vault_dir,
            db_path=self.db_path,
            sync_interval=10.0,
        )

        # 2. First sync pass (Ingests 2 files)
        stats1 = engine.sync_now()
        self.assertEqual(stats1.total_files, 2)
        self.assertEqual(stats1.modified_files, 2)
        self.assertEqual(stats1.deleted_files, 0)
        self.assertTrue(stats1.total_chunks >= 2)

        # Drain queue
        while not engine._db_queue.empty():
            time.sleep(0.02)

        # 3. Second sync pass without changes: mtime fast-path must result in 0 modified
        stats2 = engine.sync_now()
        self.assertEqual(stats2.total_files, 2)
        self.assertEqual(stats2.modified_files, 0)
        self.assertEqual(stats2.deleted_files, 0)

        # 4. Modify one note
        time.sleep(0.05)
        note1.write_text("""---
title: "Jarvis Architecture V2"
tags: [architecture, core, v2]
---
# Overview Updated
The updated architecture includes an off-thread Obsidian Context Sync Engine.
""", encoding="utf-8")

        stats3 = engine.sync_now()
        self.assertEqual(stats3.modified_files, 1)

        # 5. Delete a note
        note2.unlink()
        stats4 = engine.sync_now()
        self.assertEqual(stats4.deleted_files, 1)

        # 6. Verify query_context retrieves updated note
        while not engine._db_queue.empty():
            time.sleep(0.02)

        results = engine.query_context("Obsidian Context Sync Engine", limit=5)
        self.assertTrue(len(results) >= 1)
        top_match = results[0]
        self.assertIn("Obsidian Context Sync Engine", top_match["content"])
        self.assertEqual(top_match["note_title"], "Jarvis Architecture V2")

    def test_background_worker_thread_lifecycle(self):
        """Verifies start(), trigger_sync(), and stop() thread safety without deadlocks."""
        note = self.vault_dir / "BackgroundTest.md"
        note.write_text("# Background Thread\nTesting background sync.\n", encoding="utf-8")

        engine = ObsidianContextSyncEngine(
            vault_path=self.vault_dir,
            db_path=self.db_path,
            sync_interval=5.0,
        )

        engine.start()
        self.assertTrue(engine._is_running)
        self.assertTrue(engine._scan_thread.is_alive())
        self.assertTrue(engine._db_thread.is_alive())

        # Trigger on-demand sync
        engine.trigger_sync()
        time.sleep(0.2)

        # Stop cleanly
        engine.stop(timeout=2.0)
        self.assertFalse(engine._is_running)
        self.assertFalse(engine._scan_thread.is_alive())
        self.assertFalse(engine._db_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
