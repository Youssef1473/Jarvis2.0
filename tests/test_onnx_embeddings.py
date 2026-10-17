"""Unit tests for CPU INT8 ONNX Vector Embedding Engine."""
import sys
import unittest
from pathlib import Path
import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory.onnx_embeddings import (
    OnnxEmbeddingEngine,
    BGE_QUERY_PREFIX,
    DEFAULT_EMBEDDING_DIM,
)


class TestOnnxEmbeddingEngine(unittest.TestCase):
    """Verifies ONNX INT8 embedding dimensions, query prefixing, batch encoding, and fallback."""

    def test_native_384_dimension_and_unit_norm(self):
        """Mandatory Adjustment 1: Native 384-dim float vectors with unit L2 norm."""
        engine = OnnxEmbeddingEngine.get_instance()
        vec = engine.embed("Jarvis workstation memory architecture", is_query=False)

        self.assertEqual(len(vec), 384, "Embedding must have native 384 dimensions")
        norm = np.linalg.norm(vec)
        self.assertAlmostEqual(float(norm), 1.0, places=4, msg="Embedding vector must be L2 unit-normalized")

    def test_asymmetric_query_prefix(self):
        """Mandatory Adjustment 4: Asymmetric query prefix produces distinct query-calibrated vector."""
        engine = OnnxEmbeddingEngine.get_instance()
        text = "What is the workstation battery reserve?"
        doc_vec = engine.embed(text, is_query=False)
        query_vec = engine.embed_query(text)

        self.assertEqual(len(doc_vec), 384)
        self.assertEqual(len(query_vec), 384)
        if engine.is_available():
            # Neural BGE model must produce calibrated directional shift between query and document
            self.assertNotEqual(doc_vec, query_vec, "Query embedding must differ from document embedding")

    def test_batch_encoding_and_determinism(self):
        """Mandatory Adjustment 3: Batch encoding produces unit-normalized vectors in single pass."""
        engine = OnnxEmbeddingEngine.get_instance()
        texts = [
            "P-core thread pinning and WASAPI low latency.",
            "Double espresso coffee beverage.",
            "Set-of-Mark visual grounding badges.",
        ]
        batch_vecs = engine.embed_batch(texts, batch_size=2)

        self.assertEqual(len(batch_vecs), 3)
        for i, vec in enumerate(batch_vecs):
            self.assertEqual(len(vec), 384)
            norm = np.linalg.norm(vec)
            self.assertAlmostEqual(float(norm), 1.0, places=4)
            # Individual call matches batch call
            single_vec = engine.embed(texts[i], is_query=False)
            self.assertGreater(float(np.dot(vec, single_vec)), 0.99)

    def test_semantic_synonym_similarity_vs_orthogonal(self):
        """Verifies semantic synonyms achieve high cosine similarity while orthogonal topics are low."""
        engine = OnnxEmbeddingEngine.get_instance()
        q = engine.embed_query("What hot coffee cup does Sir prefer?")
        d_synonym = engine.embed_document("User favorite beverage is double espresso")
        d_orthogonal = engine.embed_document("PCIe NVMe solid state drive partition format")

        sim_synonym = float(np.dot(q, d_synonym))
        sim_orthogonal = float(np.dot(q, d_orthogonal))

        self.assertGreater(sim_synonym, sim_orthogonal, "Synonym must have strictly higher similarity than orthogonal text")
        # With max_l2_distance = 0.90 (S_cos >= 0.595), synonym clears the gate cleanly
        l2_dist_synonym = float(np.linalg.norm(np.array(q) - np.array(d_synonym)))
        self.assertLess(l2_dist_synonym, 0.90, f"Synonym L2 distance ({l2_dist_synonym:.4f}) must clear 0.90 gate")

    def test_offline_fallback_resilience(self):
        """Mandatory Adjustment 2: Resilient offline fallback when model files are not found."""
        fallback_engine = OnnxEmbeddingEngine(model_dir=Path("nonexistent/mock/path"), dim=384)
        self.assertFalse(fallback_engine.is_available())
        self.assertEqual(fallback_engine.get_backend_name(), "offline-fallback")

        vec = fallback_engine.embed("Testing offline vector fallback mechanism")
        self.assertEqual(len(vec), 384)
        norm = np.linalg.norm(vec)
        self.assertAlmostEqual(float(norm), 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
