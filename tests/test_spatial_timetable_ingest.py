"""Unit tests for 2D spatial timetable PDF extraction and calendar deduplication."""
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pypdf

from jarvis.skills.document_ingest import _extract_page_content, extract_text_from_pdf
from jarvis.core.memory import MemoryManager
from jarvis.skills.calendar_manager import list_calendar_events

class TestSpatialTimetableIngest(unittest.TestCase):
    def setUp(self):
        self.sample_pdf = Path("data/intake/emploi_du_temps_12-10-2026_14-17-18.pdf")
        self.config = {
            "memory": {"db_path": "data/test_memory.db"},
            "_base_dir": str(Path.cwd()),
        }
        self.memory = MemoryManager(self.config)

    def test_spatial_timetable_extraction_detects_all_days_and_friday_classes(self):
        """Verifies that 2D column clustering correctly maps classes under Monday, Tuesday, Wednesday, and Friday."""
        if not self.sample_pdf.exists():
            self.skipTest("Sample timetable PDF not found in data/intake.")

        reader = pypdf.PdfReader(str(self.sample_pdf))
        extracted = _extract_page_content(reader.pages[0])

        self.assertIn("--- Timetable Grid Extracted By Day Column ---", extracted)
        self.assertIn("### Day: Monday", extracted)
        self.assertIn("### Day: Tuesday", extracted)
        self.assertIn("### Day: Wednesday", extracted)
        self.assertIn("### Day: Thursday", extracted)
        self.assertIn("### Day: Friday", extracted)
        self.assertIn("### Day: Saturday", extracted)

        # Check Monday has Math de Base 3
        monday_section = extracted.split("### Day: Monday")[1].split("### Day: Tuesday")[0]
        self.assertIn("MATH", monday_section)
        self.assertIn("13:30 - 16:45", monday_section)

        # Check Tuesday has Projet C++ and Réseaux
        tuesday_section = extracted.split("### Day: Tuesday")[1].split("### Day: Wednesday")[0]
        self.assertIn("PROJET C++", tuesday_section)
        self.assertIn("09:00 - 12:15", tuesday_section)
        self.assertIn("13:30 - 16:45", tuesday_section)

        # Check Wednesday has POO C++
        wednesday_section = extracted.split("### Day: Wednesday")[1].split("### Day: Thursday")[0]
        self.assertIn("PROGRAMMATION ORIENT", wednesday_section)
        self.assertIn("09:00 - 12:15", wednesday_section)

        # Check Thursday has NO classes
        thursday_section = extracted.split("### Day: Thursday")[1].split("### Day: Friday")[0]
        self.assertIn("(No classes or events scheduled)", thursday_section)

        # Check Friday has Math de Base 3 and Bases de Données
        friday_section = extracted.split("### Day: Friday")[1].split("### Day: Saturday")[0]
        self.assertIn("MATH", friday_section)
        self.assertIn("09:00 - 12:15", friday_section)
        self.assertIn("BASES DE DONN", friday_section)
        self.assertIn("13:45 - 17:00", friday_section)

        # Check Saturday has NO classes
        saturday_section = extracted.split("### Day: Saturday")[1]
        self.assertIn("(No classes or events scheduled)", saturday_section)

    def test_calendar_event_deduplication(self):
        """Verifies that adding the same event twice updates rather than duplicating."""
        import uuid
        unique_title = f"PROJET C++ {uuid.uuid4().hex[:6]}"
        msg1 = self.memory.add_calendar_event(unique_title, "2026-10-13 09:00", "2026-10-13 12:15", "G011")
        self.assertIn("scheduled", msg1)

        # Second add should update the existing event
        msg2 = self.memory.add_calendar_event(unique_title, "2026-10-13 09:00", "2026-10-13 12:15", "G011 Room Updated")
        self.assertIn("updated", msg2)

        events = [e for e in self.memory.get_calendar_events() if e["title"] == unique_title and e["start_time"] == "2026-10-13 09:00"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["location"], "G011 Room Updated")

if __name__ == "__main__":
    unittest.main()
