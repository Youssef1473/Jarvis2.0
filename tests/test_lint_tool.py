"""Unit tests for the Ruff linter and formatter wrapper (tools/lint.py)."""
from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.lint import RuffNotFoundError, main, resolve_ruff_command


class TestLintToolResolution(unittest.TestCase):
    """Test executable resolution precedence and fallback tiers."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_mock_ruff(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        bin_name = "ruff.exe" if os.name == "nt" else "ruff"
        ruff_file = directory / bin_name
        ruff_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        return ruff_file

    def test_resolve_from_virtual_env_var(self) -> None:
        """Accepts VIRTUAL_ENV candidate in designated local workspace .venv."""
        venv_dir = self.base / "ws" / ".venv"
        scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        expected = self._create_mock_ruff(scripts_dir)

        result = resolve_ruff_command(
            workspace_root=self.base / "ws",
            environ={"VIRTUAL_ENV": str(venv_dir)},
            sys_exec=str(self.base / "python_exec"),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_from_conda_prefix_var(self) -> None:
        """Accepts CONDA_PREFIX candidate in designated local workspace .venv."""
        conda_dir = self.base / "ws" / ".venv"
        scripts_dir = conda_dir / ("Scripts" if os.name == "nt" else "bin")
        expected = self._create_mock_ruff(scripts_dir)

        result = resolve_ruff_command(
            workspace_root=self.base / "ws",
            environ={"CONDA_PREFIX": str(conda_dir)},
            sys_exec=str(self.base / "python_exec"),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_rejects_arbitrary_in_repo_subdirectory(self) -> None:
        """Rejects VIRTUAL_ENV candidate located in arbitrary in-repository subdirectory."""
        ws_root = self.base / "ws"
        sub_dir = ws_root / "arbitrary_folder" / "custom_env"
        scripts_dir = sub_dir / ("Scripts" if os.name == "nt" else "bin")
        self._create_mock_ruff(scripts_dir)

        with self.assertRaises(RuffNotFoundError):
            resolve_ruff_command(
                workspace_root=ws_root,
                environ={"VIRTUAL_ENV": str(sub_dir)},
                sys_exec=str(self.base / "isolated" / "python.exe"),
                which_func=lambda _: None,
                module_checker=lambda: False,
            )

    def test_resolve_untrusted_env_var_falls_through_to_next_tier(self) -> None:
        """Verifies rejected VIRTUAL_ENV falls through to next permitted discovery tier."""
        ws_root = self.base / "ws"
        untrusted_base = Path("C:\\untrusted\\env" if os.name == "nt" else "/tmp/untrusted/env")
        scripts_untrusted = untrusted_base / ("Scripts" if os.name == "nt" else "bin")
        self._create_mock_ruff(scripts_untrusted)

        # Valid Tier 2 binary in python interpreter directory
        python_home = self.base / "python_home"
        expected = self._create_mock_ruff(python_home)
        sys_exec = python_home / "python.exe"

        result = resolve_ruff_command(
            workspace_root=ws_root,
            environ={"VIRTUAL_ENV": str(untrusted_base)},
            sys_exec=str(sys_exec),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_rejects_untrusted_virtual_env(self) -> None:
        """Rejects VIRTUAL_ENV candidate located outside trusted workspace roots."""
        untrusted_base = Path("C:\\untrusted_attacker_root\\env" if os.name == "nt" else "/tmp/untrusted_root/env")
        scripts_dir = untrusted_base / ("Scripts" if os.name == "nt" else "bin")
        fake_ruff = scripts_dir / ("ruff.exe" if os.name == "nt" else "ruff")

        def fake_isfile(p: Path) -> bool:
            return p == fake_ruff

        with patch("pathlib.Path.is_file", autospec=True, side_effect=fake_isfile), \
             self.assertRaises(RuffNotFoundError):
            resolve_ruff_command(
                workspace_root=self.base / "ws",
                environ={"VIRTUAL_ENV": str(untrusted_base)},
                sys_exec=str(self.base / "ws" / ".venv" / "Scripts" / "python.exe"),
                which_func=lambda _: None,
                module_checker=lambda: False,
            )

    def test_resolve_rejects_arbitrary_sibling_directory(self) -> None:
        """Rejects VIRTUAL_ENV candidate located in arbitrary sibling directory under workspace parent."""
        ws_root = self.base / "ws"
        sibling_base = self.base / "unrelated_sibling_project" / ".venv"
        scripts_dir = sibling_base / ("Scripts" if os.name == "nt" else "bin")
        self._create_mock_ruff(scripts_dir)

        with self.assertRaises(RuffNotFoundError):
            resolve_ruff_command(
                workspace_root=ws_root,
                environ={"VIRTUAL_ENV": str(sibling_base)},
                sys_exec=str(ws_root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python.exe"),
                which_func=lambda _: None,
                module_checker=lambda: False,
            )

    def test_resolve_accepts_designated_adjacent_venv_env_var(self) -> None:
        """Accepts VIRTUAL_ENV candidate located in designated adjacent workspace_root.parent/.venv."""
        ws_root = self.base / "ws"
        adjacent_venv = self.base / ".venv"
        scripts_dir = adjacent_venv / ("Scripts" if os.name == "nt" else "bin")
        expected = self._create_mock_ruff(scripts_dir)

        result = resolve_ruff_command(
            workspace_root=ws_root,
            environ={"VIRTUAL_ENV": str(adjacent_venv)},
            sys_exec=str(self.base / "python_home" / "python.exe"),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_from_sys_exec_dir(self) -> None:
        python_dir = self.base / "python_home"
        expected = self._create_mock_ruff(python_dir)
        sys_exec = python_dir / "python.exe"

        result = resolve_ruff_command(
            workspace_root=self.base / "ws",
            environ={},
            sys_exec=str(sys_exec),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_from_parent_venv(self) -> None:
        # Workspace layout: <base>/workspace and <base>/.venv
        parent_venv = self.base / ".venv"
        scripts_dir = parent_venv / ("Scripts" if os.name == "nt" else "bin")
        expected = self._create_mock_ruff(scripts_dir)

        ws_dir = self.base / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)

        result = resolve_ruff_command(
            workspace_root=ws_dir,
            environ={},
            sys_exec=str(self.base / "isolated" / "python.exe"),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_from_local_venv(self) -> None:
        ws_dir = self.base / "workspace"
        local_venv = ws_dir / ".venv"
        scripts_dir = local_venv / ("Scripts" if os.name == "nt" else "bin")
        expected = self._create_mock_ruff(scripts_dir)

        result = resolve_ruff_command(
            workspace_root=ws_dir,
            environ={},
            sys_exec=str(self.base / "isolated" / "python.exe"),
            which_func=lambda _: None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [str(expected)])

    def test_resolve_from_path(self) -> None:
        expected_path = "C:\\Tools\\ruff.exe" if os.name == "nt" else "/usr/local/bin/ruff"

        result = resolve_ruff_command(
            workspace_root=self.base / "ws",
            environ={},
            sys_exec=str(self.base / "isolated" / "python.exe"),
            which_func=lambda binary: expected_path if binary == "ruff" else None,
            module_checker=lambda: False,
        )
        self.assertEqual(result, [expected_path])

    def test_resolve_module_fallback(self) -> None:
        sys_exec = str(self.base / "isolated" / "python.exe")

        result = resolve_ruff_command(
            workspace_root=self.base / "ws",
            environ={},
            sys_exec=sys_exec,
            which_func=lambda _: None,
            module_checker=lambda: True,
        )
        self.assertEqual(result, [sys_exec, "-m", "ruff"])

    def test_resolve_failure_raises(self) -> None:
        with self.assertRaises(RuffNotFoundError) as ctx:
            resolve_ruff_command(
                workspace_root=self.base / "ws",
                environ={},
                sys_exec=str(self.base / "isolated" / "python.exe"),
                which_func=lambda _: None,
                module_checker=lambda: False,
            )
        self.assertIn("Could not locate 'ruff' executable", str(ctx.exception))


class TestLintToolExecution(unittest.TestCase):
    """Test CLI forwarding, return codes, and error reporting."""

    @patch("tools.lint.resolve_ruff_command", return_value=["dummy_ruff.exe"])
    @patch("subprocess.run")
    def test_main_forwards_arguments(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)

        code = main(["check", "jarvis/core", "--fix"])

        mock_resolve.assert_called_once()
        mock_run.assert_called_once_with(
            ["dummy_ruff.exe", "check", "jarvis/core", "--fix"],
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            timeout=300.0,
        )
        self.assertEqual(code, 0)

    @patch("tools.lint.resolve_ruff_command", return_value=["C:\\Program Files\\Ruff\\ruff.exe"])
    @patch("subprocess.run")
    def test_main_preserves_spaces_without_shell_interpolation(
        self, mock_run: MagicMock, mock_resolve: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)

        path_with_space = "C:\\User Projects\\test file.py"
        code = main(["check", path_with_space])

        mock_run.assert_called_once_with(
            ["C:\\Program Files\\Ruff\\ruff.exe", "check", path_with_space],
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            timeout=300.0,
        )
        self.assertEqual(code, 0)

    @patch("tools.lint.resolve_ruff_command", return_value=["dummy_ruff.exe"])
    @patch("subprocess.run")
    def test_main_preserves_exit_code(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=42)

        code = main(["check", "."])
        self.assertEqual(code, 42)

    @patch("tools.lint.resolve_ruff_command", side_effect=RuffNotFoundError("Not found"))
    def test_main_handles_missing_ruff(self, mock_resolve: MagicMock) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            code = main(["check", "."])

        self.assertEqual(code, 1)
        self.assertIn("Action required:", stderr_buf.getvalue())
        self.assertIn("pip install ruff", stderr_buf.getvalue())

    @patch("tools.lint.resolve_ruff_command", return_value=["dummy_ruff.exe"])
    @patch("subprocess.run", side_effect=KeyboardInterrupt)
    def test_main_handles_keyboard_interrupt(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        code = main(["check", "."])
        self.assertEqual(code, 130)

    @patch("tools.lint.resolve_ruff_command", return_value=["dummy_ruff.exe"])
    @patch("subprocess.run", side_effect=OSError("Exec format error"))
    def test_main_handles_os_error(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            code = main(["check", "."])
        self.assertEqual(code, 1)
        self.assertIn("Error executing Ruff process", stderr_buf.getvalue())

    @patch("tools.lint.resolve_ruff_command", return_value=["C:\\tools\\ruff.exe"])
    def test_main_wrapper_info(self, mock_resolve: MagicMock) -> None:
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = main(["--wrapper-info"])

        self.assertEqual(code, 0)
        self.assertIn("Resolved Ruff command: C:\\tools\\ruff.exe", stdout_buf.getvalue())

    @patch("tools.lint.resolve_ruff_command", return_value=["C:\\tools\\ruff.exe"])
    @patch("subprocess.run")
    def test_main_help_flag(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = main(["--help"])

        self.assertEqual(code, 0)
        self.assertIn("Jarvis 2.0 Ruff Linter & Formatter Wrapper", stdout_buf.getvalue())
        mock_run.assert_called_once_with(
            ["C:\\tools\\ruff.exe", "--help"],
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            timeout=15.0,
        )

    @patch("tools.lint.resolve_ruff_command", return_value=["dummy_ruff.exe"])
    @patch("subprocess.run")
    def test_main_timeout_handling(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(["dummy_ruff.exe"], 300.0)
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            code = main(["check", "jarvis"])
        self.assertEqual(code, 124)
        self.assertIn("timed out after 300.0 seconds", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
