"""Unit tests for Pillar 3: Autonomous Diagnostic & Self-Healing AST Code Surgeon in Jarvis 2.0."""
import os
import tempfile
import unittest
from unittest.mock import patch

from jarvis.skills.ast_code_surgeon import (
    apply_and_verify_surgical_patch,
    localize_ast_fault_node,
    parse_traceback_frames,
)
from jarvis.skills.base import SkillRegistry


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
        try:
            self.temp_dir.cleanup()
        except (OSError, PermissionError):
            pass

    def test_skill_registry_registration(self):
        tools = SkillRegistry.get_all_tools()
        self.assertIn("diagnose_test_failure_ast", tools)
        self.assertIn("apply_and_verify_surgical_patch", tools)

    def test_parse_traceback_frames_and_fault_localization(self):
        tb_sample = (
            "Traceback (most recent call last):\n"
            '  File "C:\\Python310\\lib\\unittest\\case.py", line 549, in _callTestMethod\n'
            "    method()\n"
            f'  File "{self.test_file}", line 6, in test_total\n'
            "    self.assertEqual(calculate_total(100.0, 0.10), 110.0)\n"
            f'  File "{self.buggy_file}", line 3, in calculate_total\n'
            "    tax = subtotal * tax_rate * 100\n"
            "AssertionError: 1100.0 != 110.0\n"
        )

        parsed = parse_traceback_frames(tb_sample, workspace_root=self.workspace)
        self.assertEqual(parsed.get("exception_type"), "AssertionError")
        self.assertEqual(parsed.get("exception_message"), "1100.0 != 110.0")

        innermost = parsed.get("innermost_frame")
        self.assertIsNotNone(innermost)
        self.assertEqual(innermost.get("file_path"), self.buggy_file)
        self.assertEqual(innermost.get("line_number"), 3)

        # Localize AST node spanning line 3 in calc_service.py
        fault = localize_ast_fault_node(self.buggy_file, target_line=3)
        self.assertEqual(fault.get("status"), "ok")
        self.assertEqual(fault.get("node_type"), "FunctionDef")
        self.assertEqual(fault.get("node_name"), "calculate_total")
        self.assertEqual(fault.get("node_start_line"), 1)
        self.assertEqual(fault.get("node_end_line"), 4)
        self.assertIn("subtotal", fault.get("local_symbols", []))

    def test_apply_patch_rejects_syntax_error_without_touching_disk(self):
        """Syntax errors must fail fast before touching disk or launching sandbox."""
        invalid_syntax_code = "def calculate_total(subtotal: float: float)\n    invalid syntax here!"
        res = apply_and_verify_surgical_patch(
            file_path=self.buggy_file,
            node_start_line=1,
            node_end_line=4,
            replacement_code=invalid_syntax_code,
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
        # 1. Negative test: unhandled process execution error fails closed and rolls back
        from jarvis.core.security_boundaries import SecurityBoundaryViolation
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", side_effect=SecurityBoundaryViolation("Authorization Required")):
            res_err = apply_and_verify_surgical_patch(
                file_path=self.buggy_file,
                node_start_line=1,
                node_end_line=4,
                replacement_code=still_failing_code,
                target_test_module="test_calc",
                workspace_root=self.workspace,
            )
            self.assertEqual(res_err.get("status"), "rejected_execution_error")
            self.assertTrue(res_err.get("rolled_back"))
            with open(self.buggy_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), self.initial_code)

        # 2. Positive test: failing target test triggers clean rollback
        mock_result = {
            "status": "failed",
            "exit_code": 1,
            "stdout_tail": "",
            "stderr_tail": "AssertionError: 1099.0 != 110.0",
        }
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_result) as mock_proc:
            res = apply_and_verify_surgical_patch(
                file_path=self.buggy_file,
                node_start_line=1,
                node_end_line=4,
                replacement_code=still_failing_code,
                target_test_module="test_calc",
                workspace_root=self.workspace,
                timeout_sec=5.0,
            )
            self.assertTrue(mock_proc.called)

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
        mock_ok = {
            "status": "ok",
            "exit_code": 0,
            "stdout_tail": "OK",
            "stderr_tail": "",
        }
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", return_value=mock_ok) as mock_proc:
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
            self.assertEqual(mock_proc.call_count, 2)

        self.assertEqual(res.get("status"), "committed")
        self.assertFalse(res.get("rolled_back"))

        # Verify disk file contains the fix
        with open(self.buggy_file, "r", encoding="utf-8") as f:
            disk_content = f.read()
        self.assertEqual(disk_content.strip(), valid_fix.strip())

    def test_apply_patch_rolls_back_when_regression_test_fails(self):
        """Patch that passes target test but breaks regression test must roll back."""
        hardcoded_patch = (
            "def calculate_total(subtotal: float, tax_rate: float) -> float:\n"
            "    return 110.0\n"
        )
        mock_target = {"status": "ok", "exit_code": 0, "stdout_tail": "OK", "stderr_tail": ""}
        mock_reg = {"status": "failed", "exit_code": 1, "stdout_tail": "", "stderr_tail": "AssertionError"}
        with patch("jarvis.skills.job_object_sandbox.run_job_sandboxed_process", side_effect=[mock_target, mock_reg]) as mock_proc:
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
            self.assertEqual(mock_proc.call_count, 2)

        self.assertEqual(res.get("status"), "rejected_regression_failed")
        self.assertTrue(res.get("rolled_back"))
        self.assertEqual(res.get("failed_module"), "test_calc_regression")

        # Verify disk file is restored byte-for-byte
        with open(self.buggy_file, "r", encoding="utf-8") as f:
            disk_content = f.read()
        self.assertEqual(disk_content, self.initial_code)


if __name__ == "__main__":
    unittest.main()
