"""Unit tests for Win32 Event-Driven Window Hook Daemon."""
import sys
import time
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.vision.window_hook_daemon import (
    WinEventHookDaemon,
    EVENT_SYSTEM_FOREGROUND,
    EVENT_OBJECT_NAMECHANGE,
    OBJID_WINDOW,
    CHILDID_SELF,
)


class TestWindowHookDaemon(unittest.TestCase):
    """Verifies WinEventHookDaemon listeners, C callback persistence, event filtering, and lifecycle."""

    def test_listener_registration_and_removal(self):
        """Verifies listeners can be added and removed thread-safely."""
        daemon = WinEventHookDaemon()
        received = []

        def my_listener(hwnd: int, event: int, title: str):
            received.append((hwnd, event, title))

        daemon.add_listener(my_listener)
        self.assertIn(my_listener, daemon._listeners)

        # Trigger simulated callback
        daemon._event_callback(0, EVENT_SYSTEM_FOREGROUND, 1234, OBJID_WINDOW, CHILDID_SELF, 0, 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], 1234)
        self.assertEqual(received[0][1], EVENT_SYSTEM_FOREGROUND)

        daemon.remove_listener(my_listener)
        self.assertNotIn(my_listener, daemon._listeners)

    def test_child_control_filtering(self):
        """Mandatory Constraint 1: filter strictly for OBJID_WINDOW (0) and CHILDID_SELF (0)."""
        daemon = WinEventHookDaemon()
        received = []

        def listener(hwnd: int, event: int, title: str):
            received.append((hwnd, event, title))

        daemon.add_listener(listener)

        # Event with non-zero idObject (e.g. child control or scrollbar) must be ignored
        daemon._event_callback(0, EVENT_OBJECT_NAMECHANGE, 1001, 1, CHILDID_SELF, 0, 0)
        self.assertEqual(len(received), 0, "Non-zero idObject must be filtered out")

        # Event with non-zero idChild (e.g. child list item) must be ignored
        daemon._event_callback(0, EVENT_OBJECT_NAMECHANGE, 1001, OBJID_WINDOW, 5, 0, 0)
        self.assertEqual(len(received), 0, "Non-zero idChild must be filtered out")

        # Top-level window event must be received
        daemon._event_callback(0, EVENT_OBJECT_NAMECHANGE, 1001, OBJID_WINDOW, CHILDID_SELF, 0, 0)
        self.assertEqual(len(received), 1, "OBJID_WINDOW + CHILDID_SELF must pass filter")

    def test_persistent_proc_reference_on_self(self):
        """Mandatory Constraint 1: Persistent reference on self to prevent GC collecting C callback."""
        daemon = WinEventHookDaemon()
        if sys.platform == "win32":
            started = daemon.start()
            self.assertTrue(started)
            time.sleep(0.1)
            # Verify persistent callback reference is non-None
            self.assertIsNotNone(daemon._win_event_proc, "self._win_event_proc must hold persistent callback reference")
            daemon.stop()
            self.assertFalse(daemon._running)
            self.assertIsNone(daemon._thread)
        else:
            self.assertFalse(daemon.start())

    def test_stop_unblocks_cleanly_via_wm_quit(self):
        """Mandatory Constraint 1: stop() posts WM_QUIT and terminates cleanly without hanging."""
        daemon = WinEventHookDaemon()
        if sys.platform == "win32":
            daemon.start()
            time.sleep(0.05)
            self.assertTrue(daemon._running)
            start_t = time.time()
            daemon.stop(timeout=1.5)
            elapsed = time.time() - start_t
            self.assertLess(elapsed, 1.5, "stop() must unblock message pump cleanly via WM_QUIT")
            self.assertFalse(daemon._running)


if __name__ == "__main__":
    unittest.main()
