"""Unit and Integration Tests for Tier 2/Tier 3 Data Ingestion & Cognitive Utilization.
Verifies Atomic Fact Distillation, Chunk Sandwiching, Document Retrieval, and Context Bounding.
"""
import datetime
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

from jarvis.skills.base import SkillRegistry
from jarvis.core.memory import MemoryManager
from jarvis.core.config_loader import load_config
import jarvis.skills.document_ingest
import jarvis.skills.productivity


class TestDataIngestionAndUtilization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        # Use a temporary test db to guarantee clean test isolation
        cls.test_db = Path("data/test_ingestion_utilization.db")
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(cls.test_db) + suffix)
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass
        cls.test_config = dict(cls.config)
        cls.test_config["memory"] = {"db_path": str(cls.test_db)}
        cls.memory = MemoryManager(cls.test_config)

    @classmethod
    def tearDownClass(cls):
        # Force garbage collection of memory manager before unlink
        cls.memory = None
        import gc
        gc.collect()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(cls.test_db) + suffix)
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass

    def setUp(self):
        # Reset memory instance for skills
        jarvis.skills.document_ingest._memory_instance = self.memory
        jarvis.skills.productivity._memory_instance = self.memory

    def test_chunk_sandwiching_and_reference_block(self):
        """Verifies format_document_reference_block places highest relevance chunks first under <reference_data>."""
        docs = [
            {
                "id": 1,
                "title": "Low Relevance Notes",
                "category": "notes",
                "summary": "Some incidental notes on python packaging.",
                "created_at": "2026-10-10 10:00:00",
                "relevance_score": 0.42,
            },
            {
                "id": 2,
                "title": "High Relevance Specification",
                "category": "spec",
                "summary": "Core architectural blueprint for Kokoro WASAPI audio streaming worker.",
                "created_at": "2026-10-12 12:00:00",
                "relevance_score": 0.96,
            },
            {
                "id": 3,
                "title": "Medium Relevance Manual",
                "category": "manual",
                "summary": "Windows UIA and pywinauto automation guidelines.",
                "created_at": "2026-10-11 11:00:00",
                "relevance_score": 0.75,
            },
        ]
        ref_block = self.memory.format_document_reference_block(docs)
        self.assertTrue(ref_block.startswith("<reference_data>\n"))
        self.assertTrue(ref_block.endswith("\n</reference_data>"))
        # Highest relevance (Score 0.96) must appear before lower relevance items
        idx_high = ref_block.find("High Relevance Specification")
        idx_med = ref_block.find("Medium Relevance Manual")
        idx_low = ref_block.find("Low Relevance Notes")
        self.assertTrue(idx_high < idx_med < idx_low, "Chunk Sandwiching failed: chunks not ordered by relevance!")
        self.assertIn("[Document #2: High Relevance Specification | Category: spec | Archived: 2026-10-12 12:00:00]", ref_block)

    def test_get_document_by_id_and_recent_documents(self):
        """Verifies get_document_by_id and get_recent_documents retrieve stored documents accurately."""
        doc_id = self.memory.save_document_knowledge(
            title="Quantum Physics Syllabus PHYS 401",
            category="syllabus",
            summary="Midterm exam on October 18, final presentation on November 20.",
            raw_content="Full course syllabus text with schedule details.",
            file_path="data/intake/phys401.pdf"
        )
        self.assertGreater(doc_id, 0)

        retrieved = self.memory.get_document_by_id(doc_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["id"], doc_id)
        self.assertEqual(retrieved["title"], "Quantum Physics Syllabus PHYS 401")
        self.assertEqual(retrieved["category"], "syllabus")

        recent = self.memory.get_recent_documents(limit=5)
        self.assertTrue(any(d["id"] == doc_id for d in recent))

    def test_search_archived_documents_skill(self):
        """Verifies search_archived_documents executes hybrid search and formats output under <reference_data>."""
        # Archive a unique document
        self.memory.save_document_knowledge(
            title="Project Chimera Security Protocols",
            category="security",
            summary="Port 9222 DevTools WebSocket credentials and Win32 firewall isolation.",
            raw_content="Project Chimera isolates Chrome DevTools Protocol on 127.0.0.1:9222 with zero focus stealing.",
            file_path="data/intake/chimera.pdf"
        )

        res = SkillRegistry.execute("search_archived_documents", query="Chimera Security Protocols")
        self.assertIn("<reference_data>", res)
        self.assertIn("Project Chimera Security Protocols", res)
        self.assertIn("Port 9222 DevTools WebSocket", res)

    def test_read_archived_document_skill(self):
        """Verifies read_archived_document retrieves document by ID and by title."""
        doc_id = self.memory.save_document_knowledge(
            title="Stark Industries Annual Telemetry 2026",
            category="finance",
            summary="Revenue targets, quarterly dividend payouts, and lab compute grants.",
            raw_content="Full report on Stark Industries annual revenue and engineering grants.",
            file_path="data/intake/stark2026.pdf"
        )

        # By numeric ID
        by_id_res = SkillRegistry.execute("read_archived_document", identifier=str(doc_id))
        self.assertIn("<reference_data>", by_id_res)
        self.assertIn("Stark Industries Annual Telemetry 2026", by_id_res)

        # By title search
        by_title_res = SkillRegistry.execute("read_archived_document", identifier="Stark Industries")
        self.assertIn("<reference_data>", by_title_res)
        self.assertIn("Revenue targets, quarterly dividend payouts", by_title_res)

    def test_semantic_search_memory_includes_documents(self):
        """Verifies semantic_search_memory includes document vaults when search_type is 'all' or 'documents'."""
        self.memory.save_document_knowledge(
            title="Kokoro Latent Style Tensors Guide",
            category="audio",
            summary="Normalized linear combinations of 510x1x256 float style tensors for sub-1ms timbre switches.",
            raw_content="Detailed guide on Kokoro style vectors and voice profiles.",
            file_path="data/intake/kokoro_styles.pdf"
        )

        # Search specifically for documents
        doc_res = SkillRegistry.execute("semantic_search_memory", query="Kokoro Latent Style Tensors", search_type="documents")
        self.assertIn("Matching Document Knowledge:", doc_res)
        self.assertIn("<reference_data>", doc_res)
        self.assertIn("Kokoro Latent Style Tensors Guide", doc_res)

        # Search all
        all_res = SkillRegistry.execute("semantic_search_memory", query="Kokoro Latent Style Tensors", search_type="all")
        self.assertIn("Matching Document Knowledge:", all_res)
        self.assertIn("Kokoro Latent Style Tensors Guide", all_res)

    def test_process_staged_document_atomic_fact_distillation(self):
        """Verifies process_staged_document automatically distills atomic facts into the SQLite facts table."""
        test_pdf = Path("data/intake/test_syllabus_distillation.pdf")
        test_pdf.parent.mkdir(parents=True, exist_ok=True)
        test_pdf.write_text("Dummy syllabus file for atomic distillation testing.")

        staged_info = self.memory.stage_document(
            filename=test_pdf.name,
            file_path=str(test_pdf),
            file_hash="test_distill_hash_999",
            file_size=120
        )
        staged_id = staged_info["id"]

        mock_extraction = {
            "calendar_events": [
                {"title": "Quantum Mechanics Lab", "start_time": "2026-10-25 14:00", "end_time": "2026-10-25 16:00", "location": "Lab 3"}
            ],
            "tasks": [
                {"description": "Read Chapter 4 on Wavefunctions", "due_date": "2026-10-24"}
            ],
            "atomic_facts": [
                {"category": "physics_course", "key": "quantum_professor", "value": "Dr. Aris Thorne"},
                {"category": "physics_course", "key": "quantum_lab_code", "value": "ROOM-H311-PASS-4402"},
                {"category": "physics_course", "key": "midterm_weight", "value": "35% of overall grade"}
            ],
            "knowledge_doc": {
                "title": "Quantum Mechanics Course Syllabus",
                "category": "syllabus",
                "summary": "Course policies, exam weights, and contact information for Dr. Thorne."
            }
        }

        mock_res = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(mock_extraction)
        mock_res.choices = [mock_choice]
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = mock_res

        with patch("jarvis.skills.document_ingest.extract_text_from_pdf", return_value="Quantum physics syllabus contents"):
            with patch("os.getenv", side_effect=lambda k, d="": "fake_groq_key" if k == "GROQ_API_KEY" else ""):
                with patch("jarvis.skills.document_ingest.load_config", return_value=self.test_config):
                    with patch("openai.OpenAI", return_value=mock_openai_client):
                        res = SkillRegistry.execute("process_staged_document", identifier=str(staged_id), user_context="Extract all rules and facts.")
                        self.assertIn("Processed", res)
                        self.assertIn("3 atomic facts", res)

        # Verify atomic facts were distilled into SQLite facts table
        prof_fact = self.memory.get_fact("quantum_professor")
        self.assertEqual(prof_fact, "Dr. Aris Thorne")

        code_fact = self.memory.get_fact("quantum_lab_code")
        self.assertEqual(code_fact, "ROOM-H311-PASS-4402")

        weight_fact = self.memory.get_fact("midterm_weight")
        self.assertEqual(weight_fact, "35% of overall grade")

        # Cleanup physical file
        if test_pdf.exists():
            test_pdf.unlink()

    def test_context_summary_bounding_and_relevance(self):
        """Verifies get_context_summary bounds categories to prevent prompt bloat while highlighting relevant facts."""
        # Add a unique distinct fact
        self.memory.remember_fact("quantum_supercomputer_ip", "10.42.0.99", category="compute")

        # Add multiple facts to a category
        for i in range(12):
            self.memory.remember_fact(f"server_node_{i}", f"192.168.1.{100 + i}", category="infrastructure")

        # Without query: bounded to top 8 items per category
        summary_unbiased = self.memory.get_context_summary()
        self.assertIn("[Infrastructure]", summary_unbiased)
        infra_lines = [l for l in summary_unbiased.split("\n") if "Server node" in l]
        self.assertLessEqual(len(infra_lines), 8)

        # With query: relevant facts prioritized at top, categories bounded to 4
        summary_biased = self.memory.get_context_summary(query="quantum supercomputer ip")
        self.assertIn("[Directly Relevant Knowledge to Current Context]", summary_biased)
        self.assertIn("Quantum supercomputer ip: 10.42.0.99", summary_biased)


if __name__ == "__main__":
    unittest.main()
