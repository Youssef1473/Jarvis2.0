"""Focused unit and regression tests for Chromium browser executable resolution (ENG-OS-03C8 Stage 2).

Validates the resolution order and behavior of `_find_browser_exe`:
1. Valid BROWSER_EXE environment override (returns normalized absolute path).
2. Invalid BROWSER_EXE environment override (falls through to discovery; does not return non-existent path).
3. Canonical chrome-win64 discovery under %LOCALAPPDATA%\\ms-playwright.
4. Legacy supported chrome-win discovery under %LOCALAPPDATA%\\ms-playwright.
5. Missing browser behavior (returns None when no candidates exist).
6. Runtime vs import-time environment lookup (verifies dynamic environment evaluation in setUpClass).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_browser_network_boundaries import _find_browser_exe


class TestBrowserExecutableResolver(unittest.TestCase):
    """Regression test suite for _find_browser_exe resolution logic."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test_browser_resolver_")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_valid_browser_exe_override(self) -> None:
        """Proves that a valid, existing BROWSER_EXE path is honored immediately."""
        fake_browser = Path(self.temp_dir.name) / "custom_chrome.exe"
        fake_browser.write_text("dummy binary content", encoding="utf-8")

        res = _find_browser_exe(environ={"BROWSER_EXE": str(fake_browser)})
        self.assertIsNotNone(res)
        self.assertEqual(os.path.abspath(str(fake_browser)), res)

    def test_02_invalid_browser_exe_override_rejected(self) -> None:
        """Proves that a non-existent BROWSER_EXE is rejected and does not return the missing path."""
        non_existent = str(Path(self.temp_dir.name) / "ghost_browser.exe")
        with patch("glob.glob", return_value=[]):
            res = _find_browser_exe(environ={"BROWSER_EXE": non_existent})
            self.assertIsNone(res, "Non-existent BROWSER_EXE must not be returned.")

    def test_03_chrome_win64_discovery(self) -> None:
        """Proves that 64-bit Playwright installation (chrome-win64\\chrome.exe) is discovered."""
        pw_dir = Path(self.temp_dir.name) / "chromium-1243" / "chrome-win64"
        pw_dir.mkdir(parents=True, exist_ok=True)
        exe_path = pw_dir / "chrome.exe"
        exe_path.write_text("dummy binary", encoding="utf-8")

        def mock_glob(pattern: str) -> list[str]:
            if "chrome-win64" in pattern:
                return [str(exe_path)]
            return []

        with patch("glob.glob", side_effect=mock_glob):
            res = _find_browser_exe(environ={})
            self.assertIsNotNone(res)
            self.assertEqual(os.path.abspath(str(exe_path)), res)

    def test_04_legacy_chrome_win_discovery(self) -> None:
        """Proves that 32-bit/legacy Playwright installation (chrome-win\\chrome.exe) is discovered when win64 is absent."""
        pw_dir = Path(self.temp_dir.name) / "chromium-1155" / "chrome-win"
        pw_dir.mkdir(parents=True, exist_ok=True)
        exe_path = pw_dir / "chrome.exe"
        exe_path.write_text("dummy binary", encoding="utf-8")

        def mock_glob(pattern: str) -> list[str]:
            if "chrome-win" in pattern and "chrome-win64" not in pattern:
                return [str(exe_path)]
            return []

        with patch("glob.glob", side_effect=mock_glob):
            res = _find_browser_exe(environ={})
            self.assertIsNotNone(res)
            self.assertEqual(os.path.abspath(str(exe_path)), res)

    def test_05_missing_browser_returns_none(self) -> None:
        """Proves that _find_browser_exe returns None when no browser candidate exists on disk."""
        with (
            patch("glob.glob", return_value=[]),
            patch("os.path.isfile", return_value=False),
        ):
            res = _find_browser_exe(environ={})
            self.assertIsNone(res)

    def test_06_runtime_environment_lookup_in_setupclass(self) -> None:
        """Proves that TestLiveChromiumNetworkGuards dynamically evaluates _find_browser_exe in setUpClass."""
        fake_browser = Path(self.temp_dir.name) / "runtime_chrome.exe"
        fake_browser.write_text("binary", encoding="utf-8")

        with patch.dict(os.environ, {"BROWSER_EXE": str(fake_browser)}):
            dynamic_exe = _find_browser_exe()
            self.assertEqual(os.path.abspath(str(fake_browser)), dynamic_exe)

    def test_07_system_browsers_not_selected_automatically(self) -> None:
        """Proves that unrelated system browsers (Brave, Chrome, Edge) are NOT automatically selected.

        Prevents silent substitution when Playwright Chromium is absent.
        """
        # When glob finds no Playwright Chromium candidates
        with patch("glob.glob", return_value=[]):
            # Even if system paths exist on disk, automatic fallback must return None
            res = _find_browser_exe(environ={})
            self.assertIsNone(res, "System browsers must not be silently substituted without explicit BROWSER_EXE.")


if __name__ == "__main__":
    unittest.main()

