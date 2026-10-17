"""Unit tests for Pillar 3: Autonomous Diagnostic & Self-Healing AST Code Surgeon in Jarvis 2.0."""
import ast
import os
import subprocess
import sys
import tempfile
import unittest

from jarvis.skills.base import SkillRegistry
from jarvis.skills.ast_code_surgeon import (
    parse_traceback_frames,
    localize_ast_fault_node,
    diagnose_test_failure_ast,
    apply_and_verify_surgical_patch,
)


class TestASTCodeSurgeonPillar3(unittest.TestCase):
    """Verifies traceback parsing, AST fault localization, sandboxed verification, and byte-for-byte rollback."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.temp_dir.name

        # Create a sample python module with a bug
        self.buggy_file = os.path.join(self.workspace, "calc_service.py")
        self.initial_code = (
            "def calculate_total(subtotal: float, tax_rate: float) -> float:\n"
            "    # Faulty implementation multiplying instead of adding tax\n"
            "    tax = subtotal * tax_rate * 100\n"
            "    return subtotal + tax\n"
        )
        with open(self.buggy_file, "w", encoding="utf-8") as f:
            f.write(self.initial_code)

        # Create the target unit test
        self.test_file = os.path.join(self.workspace, "test_calc.py")
        test_code = (
            "import unittest\n"
            "from calc_service import calculate_total\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_total(self):\n"
            "        # 100 + (100 * 0.10) = 110.0\n"
            "        self.assertEqual(calculate_total(100.0, 0.10), 110.0)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        with open(self.test_file, "w", encoding="utf-8") as f:
            f.write(test_code)

        # Create a regression test
        self.reg_file = os.path.join(self.workspace, "test_calc_regression.py")
        reg_code = (
            "import unittest\n"
            "from calc_service import calculate_total\n\n"
            "class TestCalcRegression(unittest.TestCase):\n"
            "    def test_zero_tax(self):\n"
            "        self.assertEqual(calculate_total(50.0, 0.0), 50.0)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        with open(self.reg_file, "w", encoding="utf-8") as f:
            f.write(reg_code)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_skill_registry_registration(self):
        """Verifies diagnose_test_failure_ast and apply_and_verify_surgical_patch are registered in SkillRegistry."""
        tool_diag = SkillRegistry.get_tool("diagnose_test_failure_ast")
        tool_patch = SkillRegistry.get_tool("apply_and_verify_surgical_patch")
        self.assertIsNotNone(tool_diag)
        self.assertIsNotNone(tool_patch)
        self.assertEqual(SkillRegistry.get_risk_level("diagnose_test_failure_ast"), 0)
        self.assertEqual(SkillRegistry.get_risk_level("apply_and_verify_surgical_patch"), 1)

    def test_parse_traceback_frames_and_fault_localization(self):
        """Verifies traceback parsing and AST node boundary localization."""
        # Run test_calc to produce a real AssertionError traceback
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "test_calc"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        tb_text = proc.stderr

        parsed = parse_traceback_frames(tb_text, workspace_root=self.workspace)
        self.assertEqual(parsed["exception_type"], "AssertionError")
        self.assertIn("110.0", parsed["exception_message"])
        self.assertIsNotNone(parsed["innermost_frame"])

        # Localize the AST fault node in calc_service.py
        fault = localize_ast_fault_node(self.buggy_file, target_line=3)
        self.assertEqual(fault["status"], "ok")
        self.assertEqual(fault["node_type"], "FunctionDef")
        self.assertEqual(fault["node_name"], "calculate_total")
        self.assertEqual(fault["node_start_line"], 1)
        self.assertEqual(fault["node_end_line"], 4)
        self.assertIn("tax_rate", fault["local_symbols"])
        self.assertIn("subtotal", fault["local_symbols"])

        # Call diagnose_test_failure_ast directly
        diag = diagnose_test_failure_ast(tb_text, workspace_root=self.workspace)
        self.assertEqual(diag["status"], "ok")
        self.assertEqual(diag["exception_type"], "AssertionError")
        self.assertIn("calc", diag["file_path"].lower())

    def test_apply_patch_rejects_syntax_error_without_touching_disk(self):
        """Pre-validation must reject syntactically invalid replacement code without modifying disk."""
        invalid_code = "def calculate_total(subtotal, tax_rate):\n    invalid syntax !@@#\n"
        res = apply_and_verify_surgical_patch(
            file_path=self.buggy_file,
            node_start_line=1,
            node_end_line=4,
            replacement_code=invalid_code,
            target_test_module="test_calc",
            workspace_root=self.workspace,
        )

        self.assertEqual(res.get("status"), "rejected_syntax_error")
        self.assertFalse(res.get("rolled_back"))
        self.assertIn("syntax", res.get("error", "").lower())

        # Verify disk file is completely unaltered
        with open(self.buggy_file, "r", encoding="utf-8") as f:
            disk_content = f.read()
        self.assertEqual(disk_content, self.initial_code)

    def test_apply_patch_rolls_back_when_target_test_fails(self):
        """Semantically failing replacement must execute in sandbox, fail, and roll back byte-for-byte."""
        still_failing_code = (
            "def calculate_total(subtotal: float, tax_rate: float) -> float:\n"
            "    return subtotal + 999.0\n"
        )
        res = apply_and_verify_surgical_patch(
            file_path=self.buggy_file,
            node_start_line=1,
            node_end_line=4,
            replacement_code=still_failing_code,
            target_test_module="test_calc",
            workspace_root=self.workspace,
            timeout_sec=5.0,
        )

        self.assertEqual(res.get("status"), "rejected_target_test_failed")
        self.assertTrue(res.get("rolled_back"))
        self.assertNotEqual(res.get("exit_code"), 0)

        # Verify disk file was rolled back byte-for-byte to initial content
        with open(self.buggy_file, "r", encoding="utf-8") as f:
            disk_content = f.read()
        self.assertEqual(disk_content, self.initial_code)

    def test_apply_patch_commits_when_target_and_regression_tests_pass(self):
        """Valid patch that satisfies target and regression tests commits cleanly."""
        valid_fix = (
            "def calculate_total(subtotal: float, tax_rate: float) -> float:\n"
            "    tax = subtotal * tax_rate\n"
            "    return subtotal + tax\n"
        )
        res = apply_and_verify_surgical_patch(
            file_path=self.buggy_file,
            node_start_line=1,
            node_end_line=4,
            replacement_code=valid_fix,
            target_test_module="test_calc",
            workspace_root=self.workspace,
            regression_test_modules=["test_calc_regression"],
            timeout_sec=5.0,
        )

        self.assertEqual(res.get("status"), "committed")
        self.assertFalse(res.get("rolled_back"))

        # Verify disk file contains the fix
        with open(self.buggy_file, "r", encoding="utf-8") as f:
            disk_content = f.read()
        self.assertEqual(disk_content.strip(), valid_fix.strip())

    def test_apply_patch_rolls_back_when_regression_test_fails(self):
        """Patch that passes target test but breaks regression test must roll back."""
        # Hardcodes 110.0: passes test_calc, but breaks test_calc_regression (which expects 50.0 for 50.0 + 0)
        hardcoded_patch = (
            "def calculate_total(subtotal: float, tax_rate: float) -> float:\n"
            "    return 110.0\n"
        )
        res = apply_and_verify_surgical_patch(
            file_path=self.buggy_file,
            node_start_line=1,
            node_end_line=4,
            replacement_code=hardcoded_patch,
            target_test_module="test_calc",
            workspace_root=self.workspace,
            regression_test_modules=["test_calc_regression"],
            timeout_sec=5.0,
        )

        self.assertEqual(res.get("status"), "rejected_regression_failed")
        self.assertTrue(res.get("rolled_back"))
        self.assertEqual(res.get("failed_module"), "test_calc_regression")

        # Verify disk file is restored byte-for-byte
        with open(self.buggy_file, "r", encoding="utf-8") as f:
            disk_content = f.read()
        self.assertEqual(disk_content, self.initial_code)


if __name__ == "__main__":
    unittest.main()
