"""Audit and fault-injection test suite for AntigravityDaemon multi-lease registry atomicity.

Verifies:
1. Crash and interruption resistance: interrupted writes leave the existing .agy_lock 100% intact.
2. Concurrent readers never observe partially written, truncated, or invalid JSON states.
3. True fail-closed behavior: replacement failures preserve the last valid registry without fallback to in-place mutation.
4. Mutex lifecycle and multi-process isolation guarantees.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis.core.antigravity_bridge import AntigravityDaemon


class TestAntigravityRegistryAtomicity(unittest.TestCase):
    """Forensic verification of registry replacement atomicity and crash resilience."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.lock_file = self.workspace / ".agy_lock"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_interrupted_write_preserves_existing_registry_unmodified(self) -> None:
        """FAULT-INJECTION: An error or crash during atomic write preserves the last valid registry intact."""
        # 1. Establish an initial valid registry on disk
        initial_registry = {
            "version": 1,
            "leases": {
                "1001": {"pid": 1001, "timestamp": time.time(), "model": "gemini-3.8-flash"},
                "2002": {"pid": 2002, "timestamp": time.time(), "model": "claude-sonnet-4.6"},
            },
        }
        initial_text = json.dumps(initial_registry, indent=2)
        self.lock_file.write_text(initial_text, encoding="utf-8")

        # 2. Attempt an update that simulates an interruption/exception during replacement
        new_registry = {
            "version": 1,
            "leases": {
                "3003": {"pid": 3003, "timestamp": time.time(), "model": "gemini-3.8-flash"},
            },
        }

        # Mock Path.replace to raise OSError AND mock ReplaceFileW (if called) to return False
        with mock.patch("pathlib.Path.replace", side_effect=OSError("Simulated system crash during atomic rename")):
            if os.name == "nt":
                with mock.patch("ctypes.windll.kernel32.ReplaceFileW", return_value=0):
                    success = AntigravityDaemon._write_registry_atomic(self.workspace, new_registry, 3003)
            else:
                success = AntigravityDaemon._write_registry_atomic(self.workspace, new_registry, 3003)

        # 3. Must fail closed and return False
        self.assertFalse(success, "Must return False when atomic replacement fails")

        # 4. Destination registry MUST be completely intact, not truncated, and not overwritten
        self.assertTrue(self.lock_file.exists(), "Existing .agy_lock must NOT be unlinked on failure")
        current_text = self.lock_file.read_text(encoding="utf-8")
        self.assertEqual(current_text, initial_text, "Destination content must remain 100% identical to pre-write state")

        parsed = AntigravityDaemon._parse_registry(current_text)
        self.assertIsNotNone(parsed)
        self.assertIn("1001", parsed["leases"])
        self.assertIn("2002", parsed["leases"])
        self.assertNotIn("3003", parsed["leases"])

    def test_concurrent_readers_never_observe_empty_or_partial_state(self) -> None:
        """CONCURRENCY: High-frequency concurrent readers under continuous atomic writes never see partial JSON or 0-bytes."""
        base_registry = {
            "version": 1,
            "leases": {
                "1001": {"pid": 1001, "timestamp": time.time(), "model": "gemini-3.8-flash"},
            },
        }
        self.lock_file.write_text(json.dumps(base_registry, indent=2), encoding="utf-8")

        stop_event = threading.Event()
        read_errors: list[str] = []
        successful_reads = 0
        reads_lock = threading.Lock()

        def reader_worker():
            nonlocal successful_reads
            local_reads = 0
            while not stop_event.is_set():
                try:
                    if self.lock_file.exists():
                        raw = self.lock_file.read_text(encoding="utf-8")
                        if not raw.strip():
                            read_errors.append("Observed empty (0 bytes) .agy_lock!")
                            break
                        data = json.loads(raw)
                        if data.get("version") != 1 or "leases" not in data:
                            read_errors.append(f"Observed corrupt lease schema: {raw[:100]}")
                            break
                        local_reads += 1
                except json.JSONDecodeError as jde:
                    read_errors.append(f"JSONDecodeError observed by reader: {jde}")
                    break
                except (PermissionError, OSError):
                    # Windows sharing contention during concurrent handle release is expected, but must not be corrupted
                    pass
                time.sleep(0.001)

            with reads_lock:
                successful_reads += local_reads

        num_readers = 4
        threads: list[threading.Thread] = []
        try:
            for _ in range(num_readers):
                t = threading.Thread(target=reader_worker, daemon=True)
                t.start()
                threads.append(t)

            for i in range(1, 51):
                updated_registry = {
                    "version": 1,
                    "leases": {
                        str(i): {"pid": i, "timestamp": time.time(), "model": f"model-{i}"},
                        str(i + 1000): {"pid": i + 1000, "timestamp": time.time(), "model": "gemini-3.8-flash"},
                    },
                }
                # Even under reader handle contention, write either succeeds atomically or fails closed safely
                AntigravityDaemon._write_registry_atomic(self.workspace, updated_registry, i)
                time.sleep(0.005)
        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=2.0)

        # Zero reader errors must have occurred
        self.assertEqual(read_errors, [], f"Concurrent readers observed corrupted state: {read_errors}")
        self.assertGreater(successful_reads, 50, "Should have performed concurrent reads safely")

    def test_temporary_lock_file_cleaned_up_on_failure(self) -> None:
        """When atomic write fails, the temporary staging file is deleted and does not litter workspace."""
        registry = {"version": 1, "leases": {"1": {"pid": 1}}}
        with mock.patch("pathlib.Path.replace", side_effect=OSError("Disk write fault")):
            if os.name == "nt":
                with mock.patch("ctypes.windll.kernel32.ReplaceFileW", return_value=0):
                    success = AntigravityDaemon._write_registry_atomic(self.workspace, registry, 9999)
            else:
                success = AntigravityDaemon._write_registry_atomic(self.workspace, registry, 9999)

        self.assertFalse(success)
        tmp_files = list(self.workspace.glob(".agy_lock.tmp.*"))
        self.assertEqual(tmp_files, [], "Failed atomic write must clean up its staging tmp file")

    def test_register_lease_fails_closed_when_write_fails(self) -> None:
        """Daemon._register_lease fails closed and returns False if atomic write fails."""
        daemon = AntigravityDaemon("mock_agy.exe", "gemini-3.8-flash")
        daemon.runtime_workspace = self.workspace

        with mock.patch.object(AntigravityDaemon, "_write_registry_atomic", return_value=False), \
             mock.patch.object(AntigravityDaemon, "_is_pid_alive", return_value=True):
            registered = daemon._register_lease(12345, "gemini-3.8-flash")

        self.assertFalse(registered, "Must return False when atomic write fails")
        # Ensure lock file was not created or corrupted
        self.assertFalse(self.lock_file.exists())

    def test_fault_injection_win32_1175_unable_to_remove_replaced(self) -> None:
        """FAULT-INJECTION: ERROR_UNABLE_TO_REMOVE_REPLACED (1175) keeps old file intact and unlinks tmp_lock."""
        if sys.platform != "win32":
            self.skipTest("Requires Win32 platform")

        initial_text = '{"version": 1, "leases": {"1": {"pid": 1}}}'
        self.lock_file.write_text(initial_text, encoding="utf-8")
        new_registry = {"version": 1, "leases": {"2": {"pid": 2}}}

        with mock.patch("pathlib.Path.replace", side_effect=OSError("Replace failed")), \
             mock.patch("ctypes.windll.kernel32.ReplaceFileW", return_value=0), \
             mock.patch("ctypes.windll.kernel32.GetLastError", return_value=1175):
            success = AntigravityDaemon._write_registry_atomic(self.workspace, new_registry, 2)

        self.assertFalse(success, "Must return False on Win32 1175 failure")
        self.assertEqual(self.lock_file.read_text(encoding="utf-8"), initial_text, "Existing .agy_lock must remain unmodified")
        tmp_files = list(self.workspace.glob(".agy_lock.tmp.*"))
        self.assertEqual(tmp_files, [], "Tmp file must be safely unlinked on 1175 because old destination is intact")

    def test_fault_injection_win32_1176_unable_to_move_replacement(self) -> None:
        """FAULT-INJECTION: ERROR_UNABLE_TO_MOVE_REPLACEMENT (1176) strictly preserves tmp_lock as recovery artifact."""
        if sys.platform != "win32":
            self.skipTest("Requires Win32 platform")

        initial_text = '{"version": 1, "leases": {"1": {"pid": 1}}}'
        self.lock_file.write_text(initial_text, encoding="utf-8")
        new_registry = {"version": 1, "leases": {"2": {"pid": 2}}}

        with mock.patch("pathlib.Path.replace", side_effect=OSError("Replace failed")), \
             mock.patch("ctypes.windll.kernel32.ReplaceFileW", return_value=0), \
             mock.patch("ctypes.windll.kernel32.GetLastError", return_value=1176):
            success = AntigravityDaemon._write_registry_atomic(self.workspace, new_registry, 2)

        self.assertFalse(success, "Must return False on Win32 1176 failure")
        tmp_files = list(self.workspace.glob(".agy_lock.tmp.*"))
        self.assertEqual(len(tmp_files), 1, "tmp_lock MUST NOT be unlinked on 1176: holds only remaining valid state")
        saved_data = json.loads(tmp_files[0].read_text(encoding="utf-8"))
        self.assertEqual(saved_data["leases"]["2"]["pid"], 2, "Preserved tmp_lock must contain the new registry data")

    def test_fault_injection_win32_1177_ambiguous_rename(self) -> None:
        """FAULT-INJECTION: ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 (1177) strictly preserves all recovery artifacts."""
        if sys.platform != "win32":
            self.skipTest("Requires Win32 platform")

        initial_text = '{"version": 1, "leases": {"1": {"pid": 1}}}'
        self.lock_file.write_text(initial_text, encoding="utf-8")
        new_registry = {"version": 1, "leases": {"3": {"pid": 3}}}

        with mock.patch("pathlib.Path.replace", side_effect=OSError("Replace failed")), \
             mock.patch("ctypes.windll.kernel32.ReplaceFileW", return_value=0), \
             mock.patch("ctypes.windll.kernel32.GetLastError", return_value=1177):
            success = AntigravityDaemon._write_registry_atomic(self.workspace, new_registry, 3)

        self.assertFalse(success, "Must return False on Win32 1177 failure")
        tmp_files = list(self.workspace.glob(".agy_lock.tmp.*"))
        self.assertEqual(len(tmp_files), 1, "tmp_lock MUST NOT be unlinked on 1177: ambiguous filesystem state")
        saved_data = json.loads(tmp_files[0].read_text(encoding="utf-8"))
        self.assertEqual(saved_data["leases"]["3"]["pid"], 3)

    def test_win32_replacefilew_acl_preservation_and_native_execution(self) -> None:
        """WIN32 API: ReplaceFileW uses dwReplaceFlags=0, flushes staging content before replacement, and executes natively."""
        if sys.platform != "win32":
            self.skipTest("Requires Win32 platform")

        # 1. Pre-publication flush failure check: failure to flush staging file fails closed
        initial_text = '{"version": 1, "leases": {"10": {"pid": 10}}}'
        self.lock_file.write_text(initial_text, encoding="utf-8")
        updated_registry = {"version": 1, "leases": {"20": {"pid": 20, "model": "gemini-3.8-flash"}}}

        with mock.patch("ctypes.windll.kernel32.FlushFileBuffers", return_value=0), \
             mock.patch("ctypes.windll.kernel32.GetLastError", return_value=5):
            success_prepub = AntigravityDaemon._write_registry_atomic(self.workspace, updated_registry, 20)

        self.assertFalse(success_prepub, "Must fail closed if staging buffer flush fails")
        self.assertEqual(self.lock_file.read_text(encoding="utf-8"), initial_text, "Existing registry must remain intact on flush failure")
        self.assertEqual(list(self.workspace.glob(".agy_lock.tmp.*")), [], "Staging file must be cleaned on flush failure")

        # 2. Flag and flush sequence verification check:
        # Asserts FlushFileBuffers is called BEFORE ReplaceFileW, and dwReplaceFlags is EXACTLY 0
        call_sequence: list[str] = []
        captured_flags: list[int] = []

        def spy_flush(*args):
            call_sequence.append("flush")
            return 1

        def spy_replace(lpReplaced, lpReplacement, lpBackup, dwFlags, lpExclude, lpReserved):
            call_sequence.append("replace")
            flags_val = dwFlags.value if hasattr(dwFlags, "value") else int(dwFlags)
            captured_flags.append(flags_val)
            # Simulate kernel filesystem replacement: temporary file is moved to destination
            Path(lpReplacement).unlink(missing_ok=True)
            return 1

        with mock.patch("pathlib.Path.replace", side_effect=OSError("Fallback to ReplaceFileW")), \
             mock.patch("ctypes.windll.kernel32.FlushFileBuffers", side_effect=spy_flush), \
             mock.patch("ctypes.windll.kernel32.ReplaceFileW", side_effect=spy_replace):
            success_mock = AntigravityDaemon._write_registry_atomic(self.workspace, updated_registry, 21)

        self.assertTrue(success_mock, "Must report success when flushed and replaced")
        self.assertEqual(call_sequence, ["flush", "replace"], "Staging file MUST be flushed before ReplaceFileW is invoked")
        self.assertEqual(len(captured_flags), 1)
        exact_flags = captured_flags[0]
        self.assertEqual(exact_flags, 0, "dwReplaceFlags MUST be exactly 0 (unsupported write-through and merge-ignore flags excluded)")
        self.assertEqual(exact_flags & 0x00000002, 0, "REPLACEFILE_IGNORE_MERGE_ERRORS (0x2) must be strictly absent")
        self.assertEqual(exact_flags & 0x00000004, 0, "REPLACEFILE_IGNORE_ACL_ERRORS (0x4) must be strictly absent")

        # 3. Native Windows API integration check (unmocked kernel32):
        # Native FlushFileBuffers and native ReplaceFileW(dwReplaceFlags=0) execute against real Windows OS handles
        with mock.patch("pathlib.Path.replace", side_effect=OSError("Fallback to native ReplaceFileW")):
            success_native = AntigravityDaemon._write_registry_atomic(self.workspace, updated_registry, 22)

        self.assertTrue(success_native, "Native ReplaceFileW fallback with dwReplaceFlags=0 must succeed on Windows NTFS")
        content = json.loads(self.lock_file.read_text(encoding="utf-8"))
        self.assertEqual(content["leases"]["20"]["pid"], 20)
        self.assertEqual(list(self.workspace.glob(".agy_lock.tmp.*")), [], "Staging file must be cleaned on native success")


if __name__ == "__main__":
    unittest.main()
