"""Unit tests for jarvis/skills/rstrtmgr_lock_surgeon.py."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from jarvis.skills.base import SkillRegistry
from jarvis.skills.rstrtmgr_lock_surgeon import (
    inspect_file_locks,
    inspect_port_holders,
    release_file_lock,
)


class TestRstrtmgrLockSurgeon(unittest.TestCase):
    def test_inspect_file_locks_unlocked(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"lock test data")
            temp_path = tf.name

        try:
            res = inspect_file_locks(temp_path)
            self.assertEqual(res.get("status"), "unlocked")
            self.assertEqual(len(res.get("holders", [])), 0)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_inspect_port_holders(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        try:
            res = inspect_port_holders(port)
            self.assertEqual(res.get("status"), "listening")
            holders = res.get("holders", [])
            self.assertGreaterEqual(len(holders), 1)
            pids = [h["pid"] for h in holders]
            self.assertIn(os.getpid(), pids)
        finally:
            sock.close()

    def test_release_file_lock_protection(self):
        # Test that protected PIDs (like current process) are shielded from termination
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            temp_path = tf.name

        # Hold open file in current process
        with open(temp_path, "wb") as f:
            f.write(b"holding lock")
            res = release_file_lock(temp_path, allow_kill=True)
            self.assertEqual(res.get("status"), "processed")
            actions = res.get("actions", [])
            # Current PID must be blocked from killing
            self.assertTrue(any(a.get("action") == "blocked" for a in actions))

        if os.path.exists(temp_path):
            os.remove(temp_path)

    def test_registry_registration(self):
        self.assertIsNotNone(SkillRegistry.get_tool("inspect_file_locks"))
        self.assertIsNotNone(SkillRegistry.get_tool("inspect_port_holders"))
        self.assertIsNotNone(SkillRegistry.get_tool("release_file_lock"))
        self.assertEqual(SkillRegistry.get_risk_level("release_file_lock"), 2)
        self.assertEqual(SkillRegistry.get_risk_level("inspect_file_locks"), 0)


if __name__ == "__main__":
    unittest.main()
