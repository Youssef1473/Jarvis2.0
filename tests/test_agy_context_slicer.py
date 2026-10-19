"""Unit tests for Token-Efficient AST Context Slicer."""
import tempfile
import unittest
from pathlib import Path

from tools.agy_context_slicer import build_ast_skeleton


class TestAgyContextSlicer(unittest.TestCase):
    """Verifies build_ast_skeleton extraction accuracy, line ranges, and error handling."""

    def test_build_ast_skeleton_extracts_classes_methods_and_lines(self):
        code = """class SampleEngine:
    def __init__(self, name: str) -> None:
        self.name = name

    def compute(self, x: int, y: int = 10) -> int:
        return x + y

def standalone_func(val: float) -> str:
    return str(val)
"""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "sample.py"
            f.write_text(code, encoding="utf-8")

            skeleton = build_ast_skeleton(f, include_line_numbers=True)
            self.assertIn("class SampleEngine:", skeleton)
            self.assertIn("def __init__(self, name: str) -> None:", skeleton)
            self.assertIn("def compute(self, x: int, y: int = 10) -> int:", skeleton)
            self.assertIn("def standalone_func(val: float) -> str:", skeleton)
            self.assertIn("# L1-", skeleton)
            self.assertIn("# L2-", skeleton)

    def test_build_ast_skeleton_handles_nonexistent_and_syntax_error(self):
        missing = Path("nonexistent_path_xyz123.py")
        res = build_ast_skeleton(missing)
        self.assertIn("does not exist", res)

        with tempfile.TemporaryDirectory() as td:
            broken = Path(td) / "broken.py"
            broken.write_text("class Broken:\n  def (x):\n", encoding="utf-8")
            res_broken = build_ast_skeleton(broken)
            self.assertIn("SyntaxError", res_broken)


if __name__ == "__main__":
    unittest.main()
