"""Unit & Integration Tests for Unified Diagnostic & Structural Analysis Workflow.

Validates:
1. Multi-tier ast-grep executable discovery across virtual environments and PATH.
2. Space-safe path handling and zero shell=True invocation.
3. Safe argument forwarding to canonical tools/lint.py and ast-grep.
4. Combined mode execution, clear findings separation, and exit-code propagation.
5. Structural invariant AST-Grep rules (subprocess-shell-true, prohibited-os-system).
6. Non-regression of Milestone B1 post-edit hook performance.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.diagnostics import (
    DEFAULT_RULES_DIR,
    AstGrepNotFoundError,
    main,
    resolve_ast_grep_command,
    run_ast_grep,
    run_combined_check,
    run_ruff,
)


class TestAstGrepResolution(unittest.TestCase):
    """Tests multi-tier executable resolution for ast-grep."""

    def test_resolve_from_virtual_env_scripts(self) -> None:
        """Resolves ast-grep in $VIRTUAL_ENV/Scripts (Windows)."""
        fake_env = {"VIRTUAL_ENV": r"C:\fake\env"}

        def fake_isfile(p: Path) -> bool:
            return str(p).lower() in (
                r"c:\fake\env\scripts\ast-grep.exe".lower(),
            )

        with mock.patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile):
            cmd = resolve_ast_grep_command(environ=fake_env, sys_exec=r"C:\python\python.exe")
            self.assertEqual(cmd, [r"C:\fake\env\Scripts\ast-grep.exe"])

    def test_resolve_from_conda_prefix_bin(self) -> None:
        """Resolves ast-grep in $CONDA_PREFIX/bin (POSIX)."""
        fake_env = {"CONDA_PREFIX": "/opt/conda"}

        def fake_isfile(p: Path) -> bool:
            return p.as_posix() == "/opt/conda/bin/ast-grep"

        with mock.patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile):
            cmd = resolve_ast_grep_command(environ=fake_env, sys_exec="/usr/bin/python3")
            self.assertEqual(cmd, [str(Path("/opt/conda/bin/ast-grep"))])

    def test_resolve_from_sys_exec_parent(self) -> None:
        """Resolves ast-grep in current Python interpreter parent directory."""
        python_exec = r"C:\Python312\Scripts\python.exe"

        def fake_isfile(p: Path) -> bool:
            return str(p).lower() == r"C:\Python312\Scripts\ast-grep.exe".lower()

        with mock.patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile):
            cmd = resolve_ast_grep_command(environ={}, sys_exec=python_exec)
            self.assertEqual(cmd, [r"C:\Python312\Scripts\ast-grep.exe"])

    def test_resolve_from_adjacent_venv(self) -> None:
        """Resolves ast-grep in adjacent workspace parent/.venv."""
        root = Path(r"C:\Users\Dev\Projects\Jarvis_2.0")
        target_bin = root.parent / ".venv" / "Scripts" / "ast-grep.exe"

        def fake_isdir(p: Path) -> bool:
            return p == (root.parent / ".venv")

        def fake_isfile(p: Path) -> bool:
            return p == target_bin

        with mock.patch("pathlib.Path.is_dir", autospec=True, side_effect=fake_isdir), \
             mock.patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile):
            cmd = resolve_ast_grep_command(workspace_root=root, environ={}, sys_exec=r"C:\sys\python.exe")
            self.assertEqual(cmd, [str(target_bin)])

    def test_resolve_from_local_venv(self) -> None:
        """Resolves ast-grep in local workspace .venv."""
        root = Path(r"C:\Users\Dev\Projects\Jarvis_2.0")
        target_bin = root / ".venv" / "Scripts" / "ast-grep.exe"

        def fake_isdir(p: Path) -> bool:
            return p == (root / ".venv")

        def fake_isfile(p: Path) -> bool:
            return p == target_bin

        with mock.patch("pathlib.Path.is_dir", autospec=True, side_effect=fake_isdir), \
             mock.patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile):
            cmd = resolve_ast_grep_command(workspace_root=root, environ={}, sys_exec=r"C:\sys\python.exe")
            self.assertEqual(cmd, [str(target_bin)])

    def test_resolve_from_path(self) -> None:
        """Resolves ast-grep from system PATH via which."""
        def fake_which(cmd_name: str) -> str | None:
            if cmd_name == "ast-grep":
                return r"C:\Program Files\ast-grep\ast-grep.exe"
            return None

        with mock.patch("pathlib.Path.is_file", return_value=False), \
             mock.patch("pathlib.Path.is_dir", return_value=False):
            cmd = resolve_ast_grep_command(environ={}, sys_exec=r"C:\sys\python.exe", which_func=fake_which)
            self.assertEqual(cmd, [r"C:\Program Files\ast-grep\ast-grep.exe"])

    def test_resolve_from_path_sg_fallback(self) -> None:
        """Resolves 'sg' alias from system PATH if 'ast-grep' not present."""
        def fake_which(cmd_name: str) -> str | None:
            if cmd_name == "sg":
                return r"C:\Tools\sg.exe"
            return None

        with mock.patch("pathlib.Path.is_file", return_value=False), \
             mock.patch("pathlib.Path.is_dir", return_value=False):
            cmd = resolve_ast_grep_command(environ={}, sys_exec=r"C:\sys\python.exe", which_func=fake_which)
            self.assertEqual(cmd, [r"C:\Tools\sg.exe"])

    def test_resolve_not_found_raises(self) -> None:
        """Raises AstGrepNotFoundError with installation guidance when unresolvable."""
        with mock.patch("pathlib.Path.is_file", return_value=False), \
             mock.patch("pathlib.Path.is_dir", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            with self.assertRaises(AstGrepNotFoundError) as ctx:
                resolve_ast_grep_command(environ={}, sys_exec=r"C:\sys\python.exe")
            self.assertIn("pip install ast-grep-cli", str(ctx.exception))

    def test_resolve_handles_paths_with_spaces(self) -> None:
        """Correctly handles paths containing spaces without string splitting."""
        spaced_env = {r"VIRTUAL_ENV": r"C:\My Project Spaces\venv space"}
        target_bin = Path(r"C:\My Project Spaces\venv space\Scripts\ast-grep.exe")

        def fake_isfile(p: Path) -> bool:
            return p == target_bin

        with mock.patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile):
            cmd = resolve_ast_grep_command(environ=spaced_env, sys_exec=r"C:\sys\python.exe")
            self.assertEqual(cmd, [str(target_bin)])


class TestDiagnosticsCommands(unittest.TestCase):
    """Tests diagnostics subcommands, argument forwarding, and exit codes."""

    def test_run_ruff_delegates_to_lint_main(self) -> None:
        """Verifies run_ruff forwards arguments directly to tools.lint.main."""
        with mock.patch("tools.lint.main", return_value=0) as mock_lint_main:
            code = run_ruff(["check", "tools"])
            self.assertEqual(code, 0)
            mock_lint_main.assert_called_once_with(["check", "tools"])

    def test_run_ast_grep_executes_safely(self) -> None:
        """Verifies ast-grep is executed without shell=True using argument lists."""
        with mock.patch("tools.diagnostics.resolve_ast_grep_command", return_value=["ast-grep.exe"]), \
             mock.patch("subprocess.run") as mock_sub:
            mock_sub.return_value = mock.Mock(returncode=0)
            code = run_ast_grep(["scan", "tools"])
            self.assertEqual(code, 0)
            mock_sub.assert_called_once_with(["ast-grep.exe", "scan", "tools"], check=False)

    def test_run_ast_grep_handles_missing_binary(self) -> None:
        """Returns 1 when ast-grep cannot be found."""
        with mock.patch("tools.diagnostics.resolve_ast_grep_command", side_effect=AstGrepNotFoundError("not found")):
            code = run_ast_grep(["scan"])
            self.assertEqual(code, 1)

    def test_run_ast_grep_keyboard_interrupt(self) -> None:
        """Returns 130 on KeyboardInterrupt."""
        with mock.patch("tools.diagnostics.resolve_ast_grep_command", return_value=["ast-grep.exe"]), \
             mock.patch("subprocess.run", side_effect=KeyboardInterrupt):
            code = run_ast_grep(["scan"])
            self.assertEqual(code, 130)

    def test_run_combined_check_both_pass(self) -> None:
        """Combined check returns 0 when both Ruff and AST-Grep report 0."""
        with mock.patch("tools.diagnostics.run_ruff", return_value=0) as mock_r, \
             mock.patch("tools.diagnostics.run_ast_grep", return_value=0) as mock_a:
            code = run_combined_check(["tools"])
            self.assertEqual(code, 0)
            mock_r.assert_called_once_with(["check", "tools"])
            mock_a.assert_called_once()

    def test_run_combined_check_ruff_fails(self) -> None:
        """Combined check returns non-zero when Ruff reports violations."""
        with mock.patch("tools.diagnostics.run_ruff", return_value=1), \
             mock.patch("tools.diagnostics.run_ast_grep", return_value=0):
            code = run_combined_check(["tools"])
            self.assertEqual(code, 1)

    def test_run_combined_check_ast_grep_fails(self) -> None:
        """Combined check returns non-zero when AST-Grep reports violations."""
        with mock.patch("tools.diagnostics.run_ruff", return_value=0), \
             mock.patch("tools.diagnostics.run_ast_grep", return_value=1):
            code = run_combined_check(["tools"])
            self.assertEqual(code, 1)

    def test_search_command_argument_construction(self) -> None:
        """Verifies 'search' subcommand constructs ast-grep run -p <pattern> arguments."""
        with mock.patch("tools.diagnostics.run_ast_grep", return_value=0) as mock_ag:
            code = main(["search", "subprocess.run($$$ARGS)", "tools"])
            self.assertEqual(code, 0)
            mock_ag.assert_called_once_with(["run", "-p", "subprocess.run($$$ARGS)", "tools"])

    def test_search_command_requires_pattern(self) -> None:
        """Verifies 'search' without arguments returns 1 with error message."""
        code = main(["search"])
        self.assertEqual(code, 1)

    def test_scan_command_argument_construction(self) -> None:
        """Verifies 'scan' subcommand forwards target paths."""
        with mock.patch("tools.diagnostics.run_ast_grep", return_value=0) as mock_ag:
            code = main(["scan", "jarvis"])
            self.assertEqual(code, 0)
            mock_ag.assert_called_once()
            called_args = mock_ag.call_args[0][0]
            self.assertEqual(called_args[0], "scan")
            self.assertIn("jarvis", called_args)

    def test_wrapper_info_command(self) -> None:
        """Verifies --wrapper-info returns 0."""
        code = main(["--wrapper-info"])
        self.assertEqual(code, 0)

    def test_help_command(self) -> None:
        """Verifies --help and empty argv return 0."""
        self.assertEqual(main(["--help"]), 0)
        self.assertEqual(main([]), 0)


class TestStructuralRules(unittest.TestCase):
    """Verifies that project AST-Grep rules correctly detect prohibited patterns."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.ast_grep_bin = resolve_ast_grep_command()[0]
        except AstGrepNotFoundError:
            raise unittest.SkipTest("ast-grep binary not found in environment; skipping rule tests.")

    def test_subprocess_shell_true_rule(self) -> None:
        """Rule jarvis-subprocess-shell-true matches prohibited shell=True and ignores shell=False."""
        rule_path = DEFAULT_RULES_DIR / "subprocess-shell-true.yml"
        self.assertTrue(rule_path.is_file(), f"Rule file missing: {rule_path}")

        # Positive test fixture: contains shell=True
        bad_code = "import subprocess\nsubprocess.run(['dir'], shell=True)\n"
        # Negative test fixture: contains shell=False
        good_code = "import subprocess\nsubprocess.run(['dir'], shell=False)\n"

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.py"
            good_file = Path(tmpdir) / "good.py"
            bad_file.write_text(bad_code, encoding="utf-8")
            good_file.write_text(good_code, encoding="utf-8")

            # Positive test: bad.py must trigger rule (exit code 1)
            res_bad = subprocess.run(
                [self.ast_grep_bin, "scan", "-r", str(rule_path), str(bad_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_bad.returncode, 1)
            self.assertIn("jarvis-subprocess-shell-true", res_bad.stdout + res_bad.stderr)

            # Negative test: good.py must pass cleanly (exit code 0)
            res_good = subprocess.run(
                [self.ast_grep_bin, "scan", "-r", str(rule_path), str(good_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_good.returncode, 0)

    def test_prohibited_os_system_rule(self) -> None:
        """Rule jarvis-prohibited-os-system matches os.system and ignores subprocess."""
        rule_path = DEFAULT_RULES_DIR / "prohibited-os-system.yml"
        self.assertTrue(rule_path.is_file(), f"Rule file missing: {rule_path}")

        bad_code = "import os\nos.system('cls')\n"
        good_code = "import subprocess\nsubprocess.run(['cls'], check=False)\n"

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.py"
            good_file = Path(tmpdir) / "good.py"
            bad_file.write_text(bad_code, encoding="utf-8")
            good_file.write_text(good_code, encoding="utf-8")

            res_bad = subprocess.run(
                [self.ast_grep_bin, "scan", "-r", str(rule_path), str(bad_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_bad.returncode, 1)
            self.assertIn("jarvis-prohibited-os-system", res_bad.stdout + res_bad.stderr)

            res_good = subprocess.run(
                [self.ast_grep_bin, "scan", "-r", str(rule_path), str(good_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_good.returncode, 0)


class TestB1HookPerformancePreservation(unittest.TestCase):
    """Verifies that Milestone B1 post-edit hook remains lightweight and unmodified."""

    def test_b1_hooks_json_configuration(self) -> None:
        """Ensures .agents/hooks.json does not execute heavy ast-grep scans."""
        hooks_path = WORKSPACE_ROOT / ".agents" / "hooks.json"
        self.assertTrue(hooks_path.is_file())
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        diagnostic_entry = data.get("python-lint-diagnostic", {})
        post_tool = diagnostic_entry.get("PostToolUse", [])
        self.assertTrue(len(post_tool) >= 1)

        hook_item = post_tool[0]
        hooks_list = hook_item.get("hooks", [])
        self.assertTrue(len(hooks_list) >= 1)
        cmd_str = hooks_list[0].get("command", "")
        # Hook must invoke lint_hook.py, not repository-wide ast-grep
        self.assertIn("lint_hook.py", cmd_str)
        self.assertNotIn("ast-grep", cmd_str)
        self.assertNotIn("diagnostics.py check", cmd_str)


if __name__ == "__main__":
    unittest.main()
