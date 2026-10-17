"""Unit tests for Voidtools Everything search integration and sovereign self-editing in Jarvis 2.0."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.core.config_loader import load_config
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.skills.base import SkillRegistry
from jarvis.skills.everything_search import find_es_executable, search_everything
from jarvis.skills.os_control import search_desktop_files_fast


class TestEverythingSearchAndSelfEditing(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def test_everything_skill_registration(self):
        """Verifies search_everything is properly registered in SkillRegistry."""
        self.assertIn("search_everything", SkillRegistry.get_all_tools())
        desc = SkillRegistry._descriptions.get("search_everything", "")
        self.assertIn("Voidtools Everything", desc)

    def test_find_es_executable(self):
        """Verifies discovery of local tools/es.exe or system path."""
        exe = find_es_executable()
        self.assertIsNotNone(exe)
        self.assertTrue(os.path.exists(exe))
        self.assertTrue(exe.lower().endswith("es.exe"))

    def test_search_everything_empty_query(self):
        """Verifies search_everything handles empty query gracefully."""
        res = search_everything("")
        self.assertIn("specify a search term", res)

    def test_search_everything_mocked_success(self):
        """Verifies search_everything formatting on successful CLI output."""
        mock_output = (
            "C:\\Users\\Youssef\\Projects\\Jarvis_2.0\\jarvis\\core\\brain.py\n"
            "C:\\Users\\Youssef\\Projects\\Jarvis_2.0\\jarvis\\skills\\dev_shell.py\n"
        )
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = mock_output
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc), \
             patch("jarvis.skills.everything_search.ensure_everything_service", return_value=True):
            res = search_everything("brain.py", max_results=5)
            self.assertIn("Found 2 instant result(s)", res)
            self.assertIn("brain.py", res)
            self.assertIn("dev_shell.py", res)

    def test_search_desktop_files_fast_uses_everything(self):
        """Verifies search_desktop_files_fast leverages Voidtools Everything fast-path."""
        with patch("jarvis.skills.everything_search.search_everything", return_value="Found 1 instant result(s) via Voidtools Everything, Sir:\n- test.py"):
            res = search_desktop_files_fast("test.py")
            self.assertIn("Voidtools Everything", res)
            self.assertIn("test.py", res)

    def test_brain_recognizes_self_editing_intent(self):
        """Verifies JarvisBrain _is_code_task triggers for sovereign self-building and editing in Jarvis_2.0."""
        self.assertTrue(self.brain._is_code_task("Jarvis, edit yourself to add a new command"))
        self.assertTrue(self.brain._is_code_task("Jarvis, enhance yourself with new skills"))
        self.assertTrue(self.brain._is_code_task("Jarvis, optimize yourself in Jarvis_2.0"))
        self.assertTrue(self.brain._is_code_task("Check your code in the jarvis 2.0 folder"))
        self.assertTrue(self.brain._is_code_task("Inspect your codebase and fix yourself"))

    def test_brain_selects_full_code_tools_for_self_editing(self):
        """Verifies JarvisBrain unlocks full codebase editing tools when Sir asks Jarvis to edit/enhance himself."""
        tools = self.brain._select_tools_for_query("Jarvis, edit yourself in Jarvis_2.0 to optimize speech")
        self.assertIn("read_file", tools)
        self.assertIn("patch_file", tools)
        self.assertIn("write_file", tools)
        self.assertIn("search_codebase", tools)
        self.assertIn("run_project_tests", tools)
        self.assertIn("create_or_update_custom_skill", tools)

    def test_brain_selects_everything_search_for_file_queries(self):
        """Verifies JarvisBrain selects search_everything when Sir asks to find or locate files."""
        tools = self.brain._select_tools_for_query("Jarvis, search everything for brain.py")
        self.assertIn("search_everything", tools)
        self.assertIn("search_desktop_files_fast", tools)


if __name__ == "__main__":
    unittest.main()
