"""Unit tests for the PostToolUse linter hook helper (tools/lint_hook.py)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.lint_hook import (
    extract_target_paths,
    main,
    process_hook_payload,
    resolve_contained_python_file,
    run_fast_lint,
)


class TestLintHookExtraction(unittest.TestCase):
    """Test extraction of target paths from hook payloads."""

    def test_extract_target_paths_from_edit_tools(self) -> None:
        payloads = [
            ({"toolCall": {"name": "write_file", "args": {"Path": "jarvis/test.py"}}}, ["jarvis/test.py"]),
            ({"toolCall": {"name": "edit_file", "args": {"Path": "jarvis/core.py"}}}, ["jarvis/core.py"]),
            ({"toolCall": {"name": "replace_file_content", "args": {"filePath": "tools/lint.py"}}}, ["tools/lint.py"]),
            ({"toolCall": {"name": "write_to_file", "args": {"path": "main.py"}}}, ["main.py"]),
            (
                {
                    "toolCall": {
                        "name": "multi_replace_file_content",
                        "args": {"files": ["a.py", {"filePath": "b.py"}]},
                    }
                },
                ["a.py", "b.py"],
            ),
        ]
        for payload, expected in payloads:
            with self.subTest(tool=payload["toolCall"]["name"]):
                self.assertEqual(extract_target_paths(payload), expected)

    def test_extract_target_paths_documented_target_file_casing(self) -> None:
        """Verifies exact documented Antigravity tool arguments using TargetFile."""
        payloads = [
            (
                {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "jarvis/new_tool.py"}}},
                ["jarvis/new_tool.py"],
            ),
            (
                {"toolCall": {"name": "replace_file_content", "args": {"TargetFile": "jarvis/patch.py"}}},
                ["jarvis/patch.py"],
            ),
            (
                {
                    "toolCall": {
                        "name": "multi_replace_file_content",
                        "args": {"TargetFile": "jarvis/multi.py"},
                    }
                },
                ["jarvis/multi.py"],
            ),
            (
                {
                    "toolCall": {
                        "name": "multi_replace_file_content",
                        "args": {"files": [{"TargetFile": "jarvis/nested1.py"}, {"target_file": "jarvis/nested2.py"}]},
                    }
                },
                ["jarvis/nested1.py", "jarvis/nested2.py"],
            ),
        ]
        for payload, expected in payloads:
            with self.subTest(tool=payload["toolCall"]["name"], args=payload["toolCall"]["args"]):
                self.assertEqual(extract_target_paths(payload), expected)

    def test_extract_target_paths_ignores_unsupported_tools(self) -> None:
        payloads = [
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "python app.py"}}},
            {"toolCall": {"name": "read_file", "args": {"Path": "jarvis/core.py"}}},
            {"toolCall": {"name": "ask_question", "args": {"questions": []}}},
            {},
            {"toolCall": None},
        ]
        for payload in payloads:
            self.assertEqual(extract_target_paths(payload), [])


class TestLintHookResolution(unittest.TestCase):
    """Test file extension and repository boundary confinement."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        (self.repo_root / "jarvis").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "jarvis" / "app.py").write_text("print(1)\n", encoding="utf-8")
        (self.repo_root / "README.md").write_text("# Test\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_contained_python_file_success(self) -> None:
        resolved = resolve_contained_python_file("jarvis/app.py", self.repo_root)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved, (self.repo_root / "jarvis" / "app.py").resolve())

    def test_resolve_contained_non_python_file(self) -> None:
        resolved = resolve_contained_python_file("README.md", self.repo_root)
        self.assertIsNone(resolved)

    def test_resolve_outside_repository(self) -> None:
        outside_file = self.repo_root.parent / "outside.py"
        outside_file.write_text("print(2)\n", encoding="utf-8")
        try:
            resolved = resolve_contained_python_file(str(outside_file), self.repo_root)
            self.assertIsNone(resolved)
        finally:
            if outside_file.exists():
                outside_file.unlink()

    def test_resolve_nonexistent_file(self) -> None:
        resolved = resolve_contained_python_file("jarvis/nonexistent.py", self.repo_root)
        self.assertIsNone(resolved)


class TestLintHookExecution(unittest.TestCase):
    """Test fast linter execution and process lifecycle."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        self.test_file = self.repo_root / "test_file.py"
        self.test_file.write_text("x = 1\n", encoding="utf-8")
        self.lint_py = self.repo_root / "tools" / "lint.py"
        self.lint_py.parent.mkdir(parents=True, exist_ok=True)
        self.lint_py.write_text("# dummy lint.py\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("subprocess.run")
    def test_run_fast_lint_argument_construction(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="All checks passed!", stderr="")

        code, output = run_fast_lint(self.test_file, self.lint_py, timeout=5.0)

        self.assertEqual(code, 0)
        self.assertEqual(output, "All checks passed!")
        mock_run.assert_called_once_with(
            [
                sys.executable,
                str(self.lint_py),
                "check",
                "--select",
                "E9,F821",
                str(self.test_file),
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="lint", timeout=5.0))
    def test_run_fast_lint_timeout(self, mock_run: MagicMock) -> None:
        code, output = run_fast_lint(self.test_file, self.lint_py, timeout=5.0)
        self.assertEqual(code, 1)
        self.assertIn("timed out after 5.0s", output)

    @patch("subprocess.run", side_effect=OSError("Exec error"))
    def test_run_fast_lint_os_error(self, mock_run: MagicMock) -> None:
        code, output = run_fast_lint(self.test_file, self.lint_py, timeout=5.0)
        self.assertEqual(code, 1)
        self.assertIn("Subprocess execution error", output)

    def test_run_fast_lint_missing_linter(self) -> None:
        missing_lint = self.repo_root / "tools" / "nonexistent_lint.py"
        code, output = run_fast_lint(self.test_file, missing_lint)
        self.assertEqual(code, 1)
        self.assertIn("Linter wrapper not found", output)


class TestLintHookPayloadProcessing(unittest.TestCase):
    """Test full stdin parsing, stdout compliance, and diagnostic reporting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        self.test_py = self.repo_root / "module.py"
        self.test_py.write_text("y = 2\n", encoding="utf-8")
        self.lint_py = self.repo_root / "tools" / "lint.py"
        self.lint_py.parent.mkdir(parents=True, exist_ok=True)
        self.lint_py.write_text("# lint wrapper\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_process_hook_payload_malformed_json_outputs_contract(self) -> None:
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = process_hook_payload("{bad json", repo_root=self.repo_root, lint_py=self.lint_py)

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "{}\n")

    def test_process_hook_payload_empty_input_outputs_contract(self) -> None:
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = process_hook_payload("", repo_root=self.repo_root, lint_py=self.lint_py)

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "{}\n")

    @patch("tools.lint_hook.run_fast_lint", return_value=(0, "All checks passed!"))
    def test_process_hook_payload_clean_pass_no_stderr(self, mock_lint: MagicMock) -> None:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        payload = json.dumps({"toolCall": {"name": "write_file", "args": {"Path": "module.py"}}})

        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            code = process_hook_payload(payload, repo_root=self.repo_root, lint_py=self.lint_py)

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "{}\n")
        self.assertEqual(stderr_buf.getvalue(), "")

    @patch(
        "tools.lint_hook.run_fast_lint",
        return_value=(1, "module.py:1:1: F821 Undefined name 'unknown'"),
    )
    def test_process_hook_payload_lint_failure_reports_to_stderr(self, mock_lint: MagicMock) -> None:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        payload = json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": "module.py"}}})

        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            code = process_hook_payload(payload, repo_root=self.repo_root, lint_py=self.lint_py)

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "{}\n")
        self.assertIn("[PostToolUse Diagnostic]", stderr_buf.getvalue())
        self.assertIn("F821 Undefined name 'unknown'", stderr_buf.getvalue())

    @patch(
        "tools.lint_hook.run_fast_lint",
        return_value=(1, "jarvis/app.py:1:1: F821 Undefined name 'missing'"),
    )
    def test_process_hook_payload_documented_antigravity_target_file_payload(self, mock_lint: MagicMock) -> None:
        """Tests exact documented Antigravity PostToolUse payload structure with TargetFile."""
        target_py = self.repo_root / "jarvis" / "app.py"
        target_py.parent.mkdir(parents=True, exist_ok=True)
        target_py.write_text("broken_code\n", encoding="utf-8")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        payload = json.dumps({
            "toolCall": {
                "name": "write_to_file",
                "args": {
                    "TargetFile": str(target_py.resolve()),
                },
            },
            "stepIdx": 1,
            "error": "",
        })

        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            code = process_hook_payload(payload, repo_root=self.repo_root, lint_py=self.lint_py)

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "{}\n")
        self.assertIn("[PostToolUse Diagnostic]", stderr_buf.getvalue())
        self.assertIn("F821 Undefined name 'missing'", stderr_buf.getvalue())

    @patch("tools.lint_hook.run_fast_lint")
    def test_process_hook_payload_skips_non_python(self, mock_lint: MagicMock) -> None:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        txt_file = self.repo_root / "notes.txt"
        txt_file.write_text("plain text", encoding="utf-8")
        payload = json.dumps({"toolCall": {"name": "write_file", "args": {"Path": "notes.txt"}}})

        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            code = process_hook_payload(payload, repo_root=self.repo_root, lint_py=self.lint_py)

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "{}\n")
        mock_lint.assert_not_called()

    @patch("tools.lint_hook.process_hook_payload", return_value=0)
    def test_main_reads_stdin(self, mock_process: MagicMock) -> None:
        payload = '{"toolCall": {"name": "edit_file"}}'
        with patch("sys.stdin", io.StringIO(payload)):
            code = main()

        self.assertEqual(code, 0)
        mock_process.assert_called_once_with(payload)


if __name__ == "__main__":
    unittest.main()
