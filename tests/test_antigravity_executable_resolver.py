"""Regression test suite for Antigravity executable discovery, validation, and dependency injection (JARVIS-2.0 Phase 1A).

Verifies:
1. Valid executable discovered (PATH, LOCALAPPDATA, USER_HOME).
2. Executable not found (deterministic unavailable state).
3. PermissionError during path inspection handled safely (no crash, status INACCESSIBLE).
4. Inaccessible parent directory handled safely (no crash, status INACCESSIBLE).
5. Invalid executable candidates rejected (directories, empty strings, non-executable extensions).
6. Multiple candidates where an earlier candidate fails (PermissionError) but a later one succeeds.
7. JarvisBrain initialization when Antigravity is unavailable (reflexes work, reasoning explains unavailable).
8. Normal bridge initialization when Antigravity is available.
9. Correct degraded status when no reasoning provider is configured.
10. Resolver dependency injection through constructor and configuration.
11. Unit-test execution without accessing host installation.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.core.antigravity_bridge import (
    AntigravityBridge,
    find_agy_executable,
    inspect_candidate_executable,
    validate_agy_executable,
)
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestAntigravityExecutableResolver(unittest.TestCase):
    """Hermetic unit tests for executable discovery isolation and dependency injection."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "test_memory.db"},
            "llm": {"provider": "gemini"},
        }
        self.memory = MagicMock(spec=MemoryManager)
        self.safety = MagicMock(spec=SafetyGate)

    # -------------------------------------------------------------------------
    # 1. Valid Executable Discovered
    # -------------------------------------------------------------------------
    def test_1_valid_executable_discovered(self):
        """Verifies that a valid, accessible executable is discovered in PATH, LOCALAPPDATA, or USER_HOME."""
        # Case A: Discovered in PATH
        with (
            patch("shutil.which", return_value="C:\\tools\\bin\\agy.exe"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=False),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "resolve", return_value=Path("C:\\tools\\bin\\agy.exe")),
            patch("os.access", return_value=True),
        ):
            diag = []
            found = find_agy_executable(diagnostics=diag)
            self.assertEqual(found, str(Path("C:\\tools\\bin\\agy.exe")))
            self.assertEqual(diag[0]["status"], "VALID")
            self.assertEqual(diag[0]["source"], "PATH")

        # Case B: Discovered in LOCALAPPDATA when PATH misses
        with (
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local"}),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=False),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "resolve", return_value=Path("C:\\Users\\Test\\AppData\\Local\\agy\\bin\\agy.exe")),
            patch("os.access", return_value=True),
        ):
            diag = []
            found = find_agy_executable(diagnostics=diag)
            self.assertEqual(found, str(Path("C:\\Users\\Test\\AppData\\Local\\agy\\bin\\agy.exe")))
            self.assertEqual(diag[0]["status"], "NOT_FOUND")  # PATH miss
            self.assertEqual(diag[1]["status"], "VALID")      # LOCALAPPDATA hit

    # -------------------------------------------------------------------------
    # 2. Executable Not Found
    # -------------------------------------------------------------------------
    def test_2_executable_not_found(self):
        """Verifies that when no candidates exist on disk, find_agy_executable returns None and records NOT_FOUND."""
        with (
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"LOCALAPPDATA": "C:\\EmptyAppData"}),
            patch.object(Path, "home", return_value=Path("C:\\EmptyHome")),
            patch.object(Path, "exists", return_value=False),
        ):
            diag = []
            found = find_agy_executable(diagnostics=diag)
            self.assertIsNone(found)
            self.assertTrue(all(d["status"] == "NOT_FOUND" for d in diag))

        # Bridge with resolver returning None
        bridge = AntigravityBridge(self.config, executable_resolver=lambda: None)
        self.assertIsNone(bridge.agy_path)
        self.assertFalse(bridge.is_available())
        self.assertEqual(bridge.availability_status, "NOT_FOUND")
        self.assertIn("not found in PATH or standard", bridge.availability_diagnostic)
        self.assertIsNone(bridge._active_daemon)

    # -------------------------------------------------------------------------
    # 3. PermissionError During Path Inspection Handled Safely
    # -------------------------------------------------------------------------
    def test_3_permission_error_during_path_inspection_handled_safely(self):
        """Verifies that PermissionError (e.g. WinError 5 Access is denied) during path inspection does not crash."""
        with (
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"LOCALAPPDATA": "C:\\RestrictedAppData"}),
            patch.object(Path, "home", return_value=Path("C:\\RestrictedHome")),
            patch.object(
                Path,
                "exists",
                side_effect=PermissionError(5, "Access is denied: 'C:\\RestrictedAppData\\agy\\bin\\agy.exe'"),
            ),
        ):
            diag = []
            # Must NOT raise PermissionError!
            found = find_agy_executable(diagnostics=diag)
            self.assertIsNone(found)
            inacc = [d for d in diag if d["status"] == "INACCESSIBLE"]
            self.assertTrue(len(inacc) >= 1)
            self.assertIn("Access denied", inacc[0]["detail"])

        # Bridge initialization with a resolver that raises PermissionError must NOT crash
        def failing_resolver():
            raise PermissionError(5, "Access is denied: 'C:\\Protected\\agy.exe'")

        bridge = AntigravityBridge(self.config, executable_resolver=failing_resolver)
        self.assertIsNone(bridge.agy_path)
        self.assertFalse(bridge.is_available())
        self.assertEqual(bridge.availability_status, "INACCESSIBLE")
        self.assertIn("Access is denied", bridge.availability_diagnostic)

    # -------------------------------------------------------------------------
    # 4. Inaccessible Parent Directory Handled Safely
    # -------------------------------------------------------------------------
    def test_4_inaccessible_parent_directory_handled_safely(self):
        """Verifies that an inaccessible parent directory raising OSError/PermissionError is handled cleanly."""
        with (
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"LOCALAPPDATA": "C:\\RestrictedParent\\SubDir"}),
            patch.object(Path, "home", return_value=Path("C:\\RestrictedHome")),
            patch.object(
                Path,
                "exists",
                side_effect=PermissionError(13, "Permission denied in parent directory traversal"),
            ),
        ):
            diag = []
            found = find_agy_executable(diagnostics=diag)
            self.assertIsNone(found)
            self.assertTrue(any(d["status"] == "INACCESSIBLE" for d in diag))

    # -------------------------------------------------------------------------
    # 5. Invalid Executable Rejected
    # -------------------------------------------------------------------------
    def test_5_invalid_executable_rejected(self):
        """Verifies that directories, empty strings, and non-executable files are safely rejected without crashing."""
        # 1. validate_agy_executable unit checks
        self.assertFalse(validate_agy_executable(None))
        self.assertFalse(validate_agy_executable(""))
        self.assertFalse(validate_agy_executable("   "))
        self.assertFalse(validate_agy_executable(self.temp_dir))  # Directory is invalid

        if sys.platform == "win32":
            self.assertFalse(validate_agy_executable("C:/tools/script.txt"))
            self.assertFalse(validate_agy_executable("C:/tools/script.py"))
            self.assertTrue(validate_agy_executable("C:/tools/agy.exe"))
            self.assertTrue(validate_agy_executable("C:/tools/agy.cmd"))

        # 2. inspect_candidate_executable checks
        is_val, status, reason = inspect_candidate_executable(self.temp_dir)
        self.assertFalse(is_val)
        self.assertEqual(status, "INVALID")
        self.assertIn("directory", reason.lower())

        # 3. Bridge supplied with invalid candidate (directory) enters INVALID state without raising ValueError
        bridge = AntigravityBridge(self.config, agy_path=self.temp_dir)
        self.assertIsNone(bridge.agy_path)
        self.assertFalse(bridge.is_available())
        self.assertEqual(bridge.availability_status, "INVALID")
        self.assertIn("Invalid Antigravity executable candidate", bridge.availability_diagnostic)

    # -------------------------------------------------------------------------
    # 6. Multiple Candidates: Earlier Fails (PermissionError), Later Succeeds
    # -------------------------------------------------------------------------
    def test_6_multiple_candidates_earlier_fails_later_succeeds(self):
        """Verifies that if LOCALAPPDATA candidate fails with PermissionError, discovery continues and finds USER_HOME."""
        target_valid = str(Path("C:\\ValidHome\\.gemini\\antigravity-cli\\bin\\agy.exe"))

        def mock_inspect(cand):
            c_str = str(cand)
            if "RestrictedAppData" in c_str:
                return False, "INACCESSIBLE", f"Access denied (WinError 5 / PermissionError): [WinError 5] Access is denied: '{c_str}'"
            if "ValidHome" in c_str:
                return True, "VALID", target_valid
            return False, "NOT_FOUND", f"Path does not exist: {c_str}"

        with (
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"LOCALAPPDATA": "C:\\RestrictedAppData"}),
            patch.object(Path, "home", return_value=Path("C:\\ValidHome")),
            patch("jarvis.core.antigravity_bridge.inspect_candidate_executable", side_effect=mock_inspect),
        ):
            diag = []
            found = find_agy_executable(diagnostics=diag)
            self.assertEqual(found, target_valid)
            # PATH was NOT_FOUND, LOCALAPPDATA was INACCESSIBLE, USER_HOME was VALID
            self.assertEqual(diag[0]["status"], "NOT_FOUND")
            self.assertEqual(diag[1]["status"], "INACCESSIBLE")
            self.assertEqual(diag[2]["status"], "VALID")

    # -------------------------------------------------------------------------
    # 7. JarvisBrain Initialization When Antigravity Is Unavailable
    # -------------------------------------------------------------------------
    def test_7_jarvis_brain_initialization_when_antigravity_unavailable(self):
        """Verifies that JarvisBrain initializes cleanly when Antigravity is unavailable, reflexes work, and reasoning reports status."""
        cfg = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "mem.db"},
            "llm": {"provider": "antigravity"},
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            brain = JarvisBrain(
                cfg,
                self.memory,
                self.safety,
                antigravity_resolver=lambda: None,
            )
            self.assertFalse(brain.antigravity_bridge.is_available())
            self.assertEqual(brain.antigravity_bridge.availability_status, "NOT_FOUND")

            # Deterministic fast reflex executes in <10ms without requiring Antigravity CLI
            reflex_reply = brain.process_query("What time is it?")
            self.assertIn("Sir", reflex_reply)
            self.assertIn("time is", reflex_reply.lower())

            # Non-reflex query explains that primary reasoning provider is unavailable without crashing
            reasoning_reply = brain.process_query("Explain how quantum superposition works.")
            self.assertIn("antigravity", reasoning_reply.lower())
            self.assertIn("unavailable", reasoning_reply.lower())
            brain.shutdown()

    # -------------------------------------------------------------------------
    # 8. Normal Bridge Initialization When Antigravity Is Available
    # -------------------------------------------------------------------------
    def test_8_normal_bridge_initialization_when_antigravity_available(self):
        """Verifies that a valid executable sets agy_path, is_available=True, and status=AVAILABLE."""
        fake_path = "C:/mock_tools/bin/agy.exe"
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
        ):
            bridge = AntigravityBridge(
                self.config,
                executable_resolver=lambda: fake_path,
            )
            self.assertEqual(bridge.agy_path, fake_path)
            self.assertTrue(bridge.is_available())
            self.assertEqual(bridge.availability_status, "AVAILABLE")
            self.assertIn("available at", bridge.availability_diagnostic)

    # -------------------------------------------------------------------------
    # 9. Correct Degraded Status When No Reasoning Provider Is Configured
    # -------------------------------------------------------------------------
    def test_9_correct_degraded_status_when_no_reasoning_provider_configured(self):
        """Verifies get_provider_status reports structured availability across providers."""
        cfg = {
            "_base_dir": self.temp_dir,
            "memory": {"db_path": "mem.db"},
            "llm": {"provider": "antigravity"},
        }
        brain = JarvisBrain(
            cfg,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        status = brain.get_provider_status()
        self.assertEqual(status["active_provider"], "antigravity")
        self.assertFalse(status["providers"]["antigravity"]["available"])
        self.assertEqual(status["providers"]["antigravity"]["status"], "NOT_FOUND")

    # -------------------------------------------------------------------------
    # 10. Resolver Dependency Injection
    # -------------------------------------------------------------------------
    def test_10_resolver_dependency_injection(self):
        """Verifies resolver and path dependency injection through JarvisBrain constructor and config."""
        fake_exe = "C:/injected/bin/agy.exe"
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
        ):
            # 1. Via constructor keyword argument antigravity_resolver
            brain1 = JarvisBrain(
                self.config,
                self.memory,
                self.safety,
                antigravity_resolver=lambda: fake_exe,
            )
            self.assertEqual(brain1.antigravity_bridge.agy_path, fake_exe)

            # 2. Via config["llm"]["antigravity_resolver"]
            cfg2 = {
                "_base_dir": self.temp_dir,
                "memory": {"db_path": "mem.db"},
                "llm": {"provider": "gemini", "antigravity_resolver": lambda: fake_exe},
            }
            brain2 = JarvisBrain(cfg2, self.memory, self.safety)
            self.assertEqual(brain2.antigravity_bridge.agy_path, fake_exe)

            # 3. Via config["llm"]["antigravity_path"]
            cfg3 = {
                "_base_dir": self.temp_dir,
                "memory": {"db_path": "mem.db"},
                "llm": {"provider": "gemini", "antigravity_path": fake_exe},
            }
            brain3 = JarvisBrain(cfg3, self.memory, self.safety)
            self.assertEqual(brain3.antigravity_bridge.agy_path, fake_exe)

    # -------------------------------------------------------------------------
    # 11. Unit-Test Isolation
    # -------------------------------------------------------------------------
    def test_11_unit_test_isolation_guaranteed(self):
        """Verifies that injecting an isolated resolver guarantees zero access to host LOCALAPPDATA or user home."""
        with (
            patch("jarvis.core.antigravity_bridge.find_agy_executable") as mock_find,
            patch("shutil.which") as mock_which,
        ):
            brain = JarvisBrain(
                self.config,
                self.memory,
                self.safety,
                antigravity_resolver=lambda: None,
            )
            self.assertIsNone(brain.antigravity_bridge.agy_path)
            mock_find.assert_not_called()
            agy_which_calls = [c for c in mock_which.mock_calls if "agy" in str(c)]
            self.assertEqual(agy_which_calls, [])


if __name__ == "__main__":
    unittest.main()
