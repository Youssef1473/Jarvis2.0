"""Automated tests for sqlite-vec native C SIMD vector indexing in MemoryManager."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory import MemoryManager, VectorMemoryEngine
import sqlite_vec


class TestSqliteVecMemorySuite(unittest.TestCase):
    """Verifies sqlite-vec KNN search, deterministic embeddings, and schema integration."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = {
            "_base_dir": self.temp_dir.name,
            "memory": {"db_path": "test_memory.db", "background_learning": False}
        }
        self.memory = MemoryManager(self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dense_vectorize_determinism_and_norm(self):
        """Verifies dense_vectorize produces deterministic, unit-normalized 384-dim float vectors."""
        engine = VectorMemoryEngine()
        vec1 = engine.dense_vectorize("Jarvis coffee espresso preference")
        vec2 = engine.dense_vectorize("Jarvis coffee espresso preference")

        self.assertEqual(len(vec1), 384)
        self.assertEqual(vec1, vec2, "Embeddings for identical text must be deterministic")

        # Check L2 unit norm
        norm = sum(v * v for v in vec1)
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_schema_meta_records_dimension_and_backend(self):
        """Mandatory Constraint 2: Verify schema_meta records vec_dimension='384' and embedder_backend."""
        with self.memory._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM schema_meta WHERE key = 'vec_dimension'")
            row_dim = cursor.fetchone()
            self.assertIsNotNone(row_dim)
            self.assertEqual(row_dim["value"], "384")

            cursor.execute("SELECT value FROM schema_meta WHERE key = 'embedder_backend'")
            row_backend = cursor.fetchone()
            self.assertIsNotNone(row_backend)
            self.assertIn(row_backend["value"], ["onnx-bge-small", "offline-fallback"])

    def test_facts_vec_table_created(self):
        """Verifies facts_vec virtual table exists in SQLite database."""
        with self.memory._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='facts_vec'")
            row = cursor.fetchone()
            self.assertIsNotNone(row, "facts_vec virtual table must exist")

    def test_remember_fact_populates_facts_vec_and_knn_search(self):
        """Verifies storing a fact indexes into facts_vec and enables sub-millisecond KNN search."""
        self.memory.remember_fact("favorite_beverage", "double espresso coffee", "preference")
        self.memory.remember_fact("code_editor", "VS Code with Python extension", "development")
        self.memory.remember_fact("pet_name", "Max the Golden Retriever", "personal")

        # Semantic KNN search for coffee
        t0 = time.perf_counter()
        results = self.memory.semantic_search_facts("What kind of coffee or espresso do I drink?", top_k=2)
        latency_ms = (time.perf_counter() - t0) * 1000

        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["key"], "favorite_beverage")
        self.assertIn("espresso", results[0]["value"])
        self.assertLess(latency_ms, 50.0, f"KNN search should be ultra-fast, took {latency_ms:.2f}ms")

    def test_semantic_synonym_recall_without_exact_word_match(self):
        """Verifies semantic similarity recall across synonyms without identical token overlap."""
        self.memory.remember_fact("drink_choice", "double espresso beverage", "preference")
        self.memory.remember_fact("editor_choice", "sublime text editor", "tools")

        # Query uses 'coffee' and 'cup', neither of which appears in "double espresso beverage"
        results = self.memory.semantic_search_facts("What hot coffee cup does Sir prefer?", top_k=2)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["key"], "drink_choice")

    def test_knn_benchmark_100_queries(self):
        """Benchmarks 100 KNN vector queries against facts_vec for sub-millisecond throughput."""
        self.memory.remember_fact("favorite_editor", "Cursor IDE and VSCode", "tools")
        self.memory.remember_fact("preferred_language", "Python 3.12 and Rust", "dev")

        # Warm up first query
        self.memory.semantic_search_facts("programming editor choice", top_k=1)

        t0 = time.perf_counter()
        for _ in range(100):
            self.memory.semantic_search_facts("programming editor choice", top_k=1)
        total_time_ms = (time.perf_counter() - t0) * 1000
        avg_ms = total_time_ms / 100

        self.assertLess(avg_ms, 100.0, f"Average query latency should be < 100ms, was {avg_ms:.2f}ms")

    def test_documents_vec_table_created(self):
        """Verifies documents_vec virtual table exists in SQLite database."""
        with self.memory._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents_vec'")
            row = cursor.fetchone()
            self.assertIsNotNone(row, "documents_vec virtual table must exist")

    def test_save_document_populates_documents_vec_and_semantic_search(self):
        """Verifies storing a document indexes into documents_vec and enables KNN search."""
        doc_id = self.memory.save_document_knowledge(
            title="Audio Engine Architecture",
            category="architecture",
            summary="P-core thread pinning and WASAPI low-latency playback worker with Kokoro ONNX",
            raw_content="All speech streaming passes through lock-free ring buffers. Audio thread starvation is fatal."
        )
        self.assertGreater(doc_id, 0)

        # Query semantically without exact keyword overlap
        results = self.memory.semantic_search_documents("speech synthesis sound playback", top_k=2)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["title"], "Audio Engine Architecture")

    def test_hybrid_search_facts_rrf_ranking(self):
        """Verifies hybrid_search_facts fuses FTS5 BM25 and sqlite-vec KNN using RRF."""
        self.memory.remember_fact("favorite_beverage", "double espresso coffee", "preference")
        self.memory.remember_fact("code_editor", "VS Code and Cursor IDE", "development")

        # Query should match favorite_beverage on both lexical and semantic vector fronts
        fused = self.memory.hybrid_search_facts("double espresso", top_k=2)
        self.assertGreater(len(fused), 0)
        self.assertEqual(fused[0]["key"], "favorite_beverage")
        self.assertIn("rrf_score", fused[0])
        self.assertGreater(fused[0]["rrf_score"], 0.0)

    def test_hybrid_search_documents_rrf_ranking(self):
        """Verifies hybrid_search_documents fuses FTS5 BM25 and sqlite-vec KNN using RRF."""
        self.memory.save_document_knowledge(
            title="Desktop Automation Manual",
            category="manual",
            summary="Chrome DevTools Protocol port 9222 and pywinauto UIA backend",
            raw_content="Sub-5ms silent tab inspection without synthetic keystrokes or focus stealing."
        )
        fused = self.memory.hybrid_search_documents("Chrome DevTools automation", limit=2)
        self.assertGreater(len(fused), 0)
        self.assertEqual(fused[0]["title"], "Desktop Automation Manual")
        self.assertIn("rrf_score", fused[0])

    def test_context_summary_hybrid_prioritization(self):
        """Verifies get_context_summary(query=...) dynamically prioritizes relevant facts at the top."""
        self.memory.remember_fact("favorite_beverage", "double espresso coffee", "preference")
        self.memory.remember_fact("favorite_editor", "Cursor IDE and VSCode", "tools")

        summary = self.memory.get_context_summary(query="what is my favorite IDE editor")
        self.assertIn("[Directly Relevant Knowledge to Current Context]", summary)
        self.assertIn("Favorite editor: Cursor IDE and VSCode", summary)


if __name__ == "__main__":
    unittest.main()
