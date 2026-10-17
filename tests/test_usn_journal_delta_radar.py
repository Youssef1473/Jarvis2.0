"""Unit tests for jarvis/skills/usn_journal_delta_radar.py."""
import os
import tempfile
import time
import unittest

from jarvis.skills.base import SkillRegistry
from jarvis.skills.usn_journal_delta_radar import (
    WorkspaceMutationRing,
    get_recent_workspace_mutations,
    record_workspace_mutation,
)


class TestUsnJournalDeltaRadar(unittest.TestCase):
    def setUp(self):
        WorkspaceMutationRing.get_instance().clear()

    def tearDown(self):
        WorkspaceMutationRing.get_instance().clear()

    def test_ring_buffer_noise_filtering(self):
        ring = WorkspaceMutationRing.get_instance()
        # Noise paths must be discarded
        ring.record("modified", r"C:\Projects\Jarvis_2.0\.git\objects\1234")
        ring.record("modified", r"C:\Projects\Jarvis_2.0\.venv\Lib\site-packages\pkg.py")
        self.assertEqual(len(ring.events), 0)

        # Valid paths must be recorded
        ring.record("modified", r"C:\Projects\Jarvis_2.0\jarvis\core\brain.py")
        self.assertEqual(len(ring.events), 1)

    def test_cold_start_mtime_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_file = os.path.join(tmpdir, "new_code.py")
            with open(sample_file, "w") as f:
                f.write("print('radar test')")

            res = get_recent_workspace_mutations(
                workspace_root=tmpdir,
                within_minutes=5.0,
            )
            self.assertEqual(res.get("status"), "ok")
            self.assertEqual(res.get("source"), "cold_start_mtime_scan")
            self.assertGreaterEqual(res.get("count", 0), 1)
            mutations = res.get("mutations", [])
            paths = [m["path"] for m in mutations]
            self.assertIn("new_code.py", paths)

    def test_record_tool_invocation(self):
        res = record_workspace_mutation("created", "tests/dummy.py")
        self.assertEqual(res.get("status"), "recorded")

    def test_registry_registration(self):
        tool = SkillRegistry.get_tool("get_recent_workspace_mutations")
        self.assertIsNotNone(tool)
        self.assertEqual(SkillRegistry.get_risk_level("get_recent_workspace_mutations"), 0)


if __name__ == "__main__":
    unittest.main()
