"""Hermetic tests for 64-bit Win32 ctypes ABI AST invariant enforcement."""
from __future__ import annotations

import unittest

from tools.diagnostics import (
    CRITICAL_WIN32_64BIT_APIS,
    check_win32_abi_source,
    run_win32_abi_check,
)


class TestDiagnosticsWin32Abi(unittest.TestCase):
    """Verifies that tools/diagnostics.py statically detects missing Win32 ctypes prototypes."""

    def test_critical_api_set_completeness(self) -> None:
        """Verifies that all 14 required Win32 handle/pointer APIs are in the invariant set."""
        required = {
            "GetModuleHandleW",
            "SetWindowsHookExW",
            "UnhookWindowsHookEx",
            "CallNextHookEx",
            "OpenProcess",
            "GetCurrentProcess",
            "CreateEventW",
            "CloseHandle",
            "OpenDesktopW",
            "CloseDesktop",
            "SetThreadDesktop",
            "GetLogicalProcessorInformationEx",
            "AvSetMmThreadCharacteristicsW",
            "AvRevertMmThreadCharacteristics",
        }
        self.assertTrue(required.issubset(CRITICAL_WIN32_64BIT_APIS))

    def test_unprototyped_get_module_handle_triggers_violation(self) -> None:
        """Calling kernel32.GetModuleHandleW(None) without .restype triggers WIN32_64BIT_ABI_PROTOTYPE."""
        snippet = """
import ctypes
kernel32 = ctypes.windll.kernel32
h = kernel32.GetModuleHandleW(None)
"""
        violations = check_win32_abi_source(snippet, filename="test_fixture.py")
        self.assertEqual(len(violations), 1)
        self.assertIn("WIN32_64BIT_ABI_PROTOTYPE", violations[0])
        self.assertIn("GetModuleHandleW", violations[0])
        self.assertIn(".restype", violations[0])
        self.assertIn(".argtypes", violations[0])

    def test_prototyped_get_module_handle_passes_cleanly(self) -> None:
        """Declaring .restype and .argtypes for GetModuleHandleW passes with 0 diagnostics."""
        snippet = """
import ctypes
from ctypes import wintypes
kernel32 = ctypes.windll.kernel32
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
h = kernel32.GetModuleHandleW(None)
"""
        violations = check_win32_abi_source(snippet, filename="test_fixture.py")
        self.assertEqual(len(violations), 0)

    def test_unprototyped_get_current_process_triggers_missing_restype(self) -> None:
        """Calling GetCurrentProcess() without arguments triggers missing .restype violation."""
        snippet = """
import ctypes
k32 = ctypes.windll.kernel32
proc = k32.GetCurrentProcess()
"""
        violations = check_win32_abi_source(snippet, filename="test_proc.py")
        self.assertEqual(len(violations), 1)
        self.assertIn("GetCurrentProcess", violations[0])
        self.assertIn(".restype", violations[0])

    def test_prototyped_get_current_process_passes_cleanly(self) -> None:
        """Declaring .restype for GetCurrentProcess passes with 0 diagnostics."""
        snippet = """
import ctypes
from ctypes import wintypes
k32 = ctypes.windll.kernel32
k32.GetCurrentProcess.restype = wintypes.HANDLE
proc = k32.GetCurrentProcess()
"""
        violations = check_win32_abi_source(snippet, filename="test_proc.py")
        self.assertEqual(len(violations), 0)

    def test_unprototyped_open_process_triggers_missing_prototype(self) -> None:
        """Calling OpenProcess with arguments triggers missing .restype and .argtypes."""
        snippet = """
import ctypes
kernel32 = ctypes.windll.kernel32
h = kernel32.OpenProcess(0x1000, False, 1234)
"""
        violations = check_win32_abi_source(snippet, filename="test_open.py")
        self.assertEqual(len(violations), 1)
        self.assertIn("OpenProcess", violations[0])
        self.assertIn(".restype", violations[0])
        self.assertIn(".argtypes", violations[0])

    def test_live_jarvis_codebase_passes_abi_invariants(self) -> None:
        """Verifies that all modules under jarvis/ pass the WIN32_64BIT_ABI_PROTOTYPE check."""
        ret = run_win32_abi_check(["jarvis"])
        self.assertEqual(ret, 0, "All jarvis/ modules must satisfy 64-bit Win32 ABI prototypes")


if __name__ == "__main__":
    unittest.main()
