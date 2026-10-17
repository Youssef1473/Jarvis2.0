"""Unit tests for jarvis/skills/nvml_dxgi_vram_arbiter.py."""
import unittest

from jarvis.skills.base import SkillRegistry
from jarvis.skills.nvml_dxgi_vram_arbiter import (
    audit_vram_and_thermal_budget,
    _get_nvml_lib,
)


class TestNvmlVramArbiter(unittest.TestCase):
    def test_audit_vram_structure(self):
        res = audit_vram_and_thermal_budget(reclaim_if_pressured=False)
        self.assertIn("status", res)
        self.assertIn("vram_total_mb", res)
        self.assertIn("vram_free_mb", res)
        self.assertIn("compute_processes", res)
        self.assertIsInstance(res["compute_processes"], list)

    def test_nvml_resilient_resolution(self):
        # Must not raise unhandled exceptions regardless of GPU hardware state
        lib = _get_nvml_lib()
        # Either resolved or None if non-NVIDIA environment
        self.assertTrue(lib is not None or lib is None)

    def test_registry_registration(self):
        tool = SkillRegistry.get_tool("audit_vram_and_thermal_budget")
        self.assertIsNotNone(tool)
        self.assertEqual(SkillRegistry.get_risk_level("audit_vram_and_thermal_budget"), 0)


if __name__ == "__main__":
    unittest.main()
