"""End-to-End Verification Test for Context-Gated Document & PDF Ingestion Engine."""
import datetime
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
from jarvis.core.memory import MemoryManager
from jarvis.core.config_loader import load_config
import jarvis.skills.document_ingest
import jarvis.skills.calendar_manager
import pypdf


def create_sample_pdf(file_path: Path, title: str, content_lines: list[str]) -> None:
    """Generates a real PDF file with text layers for test verification."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    # We can create a simple PDF page or write valid PDF text
    writer.add_blank_page(width=612, height=792)
    
    # We can also write standard PDF streams using reportlab or minimal PDF object syntax
    # Minimal pure-python PDF with font and text:
    pdf_content = (
        "%PDF-1.4\n"
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        "4 0 obj << /Length {length} >> stream\n"
        "BT /F1 12 Tf 50 700 Td ({text}) Tj ET\n"
        "endstream\n"
        "endobj\n"
        "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        "xref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000117 00000 n \n0000000244 00000 n \n0000000350 00000 n \ntrailer << /Size 6 /Root 1 0 R >>\nstartxref\n436\n%%EOF"
    )
    raw_text = "\\n".join(content_lines)
    stream_content = f"BT /F1 12 Tf 50 700 Td ({title}) Tj 0 -20 Td ({raw_text}) Tj ET"
    full_pdf = (
        "%PDF-1.4\n"
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        f"4 0 obj << /Length {len(stream_content)} >> stream\n"
        f"{stream_content}\n"
        "endstream\nendobj\n"
        "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        "xref\n0 6\n0000000000 65535 f \n"
        "trailer << /Size 6 /Root 1 0 R >>\nstartxref\n100\n%%EOF"
    )
    file_path.write_bytes(full_pdf.encode("latin-1"))


def test_document_ingestion_suite():
    import tempfile
    import shutil
    print("=== Testing Context-Gated Universal Document Ingestion ===")
    temp_dir = tempfile.mkdtemp()
    temp_db_path = str(Path(temp_dir) / "test_doc_memory.db")
    cfg = load_config()
    cfg["memory"] = {"db_path": temp_db_path}
    mem = MemoryManager(cfg)
    jarvis.skills.document_ingest.set_memory_manager(mem)

    try:
        # 1. Verify tools are registered in SkillRegistry
        tools = SkillRegistry.get_all_tools()
        assert "list_staged_documents" in tools, "list_staged_documents skill missing!"
        assert "process_staged_document" in tools, "process_staged_document skill missing!"
        print("Tool registration: PASS")

        # 2. Create test PDF in temp_dir
        test_pdf = Path(temp_dir) / "test_quantum_physics_syllabus.pdf"
        
        today_iso = datetime.date.today().isoformat()
        syllabus_lines = [
            f"Course: Advanced Quantum Mechanics (PHYS 401)",
            f"Midterm Exam: {today_iso} at 10:00 AM in Hall B",
            f"Final Project Review: {today_iso} at 3:00 PM in Lab 4",
            f"Required Action: Submit lab notebook by end of week"
        ]
        create_sample_pdf(test_pdf, "Spring 2026 Physics Syllabus", syllabus_lines)
        assert test_pdf.exists(), "Sample test PDF was not created!"
        print(f"Sample PDF created at {test_pdf} ({test_pdf.stat().st_size} bytes)")

        # 3. Test Staging document
        doc_info = mem.stage_document(
            filename=test_pdf.name,
            file_path=str(test_pdf),
            file_hash="test_sha_quantum_123",
            file_size=test_pdf.stat().st_size
        )
        assert doc_info["id"] > 0, "Failed to stage document in SQLite!"
        print(f"Document staged successfully: ID #{doc_info['id']} ({doc_info['filename']})")

        # 4. Test list_staged_documents
        staged_list_msg = SkillRegistry.execute("list_staged_documents")
        print(f"Staged queue response:\n{staged_list_msg}")
        assert test_pdf.name in staged_list_msg, "Staged document missing from list output!"

        # 5. Process Staged Document with User Context
        user_instruction = (
            "Jarvis, that PDF is my university quantum physics syllabus. "
            "Please register the Midterm Exam and Final Project Review in my calendar, "
            "add the lab notebook task to my to-do list, and archive the syllabus."
        )
        print(f"\nTransmitting Sir's context: \"{user_instruction}\"")
        result_msg = SkillRegistry.execute(
            "process_staged_document",
            identifier=test_pdf.name,
            user_context=user_instruction
        )
        print(f"Jarvis Ingestion Output:\n{result_msg}")
        assert "Processed" in result_msg or "registered" in result_msg

        # 6. Verify SQLite Database State
        staged_after = mem.get_staged_documents(status="PENDING_CONTEXT")
        matching_pending = [d for d in staged_after if d["filename"] == test_pdf.name]
        assert len(matching_pending) == 0, "Document should no longer be in PENDING_CONTEXT status!"

        active_events = mem.get_calendar_events()
        print(f"\nActive calendar events count: {len(active_events)}")
        for ev in active_events:
            print(f"  - #{ev['id']}: '{ev['title']}' at {ev['start_time']} ({ev.get('location')})")

        print("\nALL CONTEXT-GATED DOCUMENT INGESTION TESTS PASSED 100%!")
    finally:
        jarvis.skills.document_ingest.set_memory_manager(None)
        shutil.rmtree(temp_dir, ignore_errors=True)



if __name__ == "__main__":
    test_document_ingestion_suite()
