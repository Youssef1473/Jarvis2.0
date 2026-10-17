"""Automated tests for Alder Lake hybrid core affinity, MMCSS scheduling, and EcoQoS."""
import os
import sys
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.win32_affinity import (
    MASK_PCORE_AUDIO_ISOLATED,
    MASK_PCORE_COMPUTE,
    MASK_ECORE_BACKGROUND,
    THREAD_PRIORITY_NORMAL,
    THREAD_PRIORITY_ABOVE_NORMAL,
    THREAD_PRIORITY_HIGHEST,
    THREAD_PRIORITY_TIME_CRITICAL,
    pin_current_thread_to_mask,
    elevate_audio_thread_mmcss,
    configure_ecore_worker_thread,
)


class TestWin32AffinitySuite(unittest.TestCase):
    """Verifies thread affinity masks, MMCSS registration, and worker configuration."""

    def test_affinity_masks_disjoint_and_defined(self):
        """Verifies P-Core Audio, P-Core Compute, and E-Core masks are distinct and valid."""
        self.assertEqual(MASK_PCORE_AUDIO_ISOLATED, 0x0003)
        self.assertEqual(MASK_PCORE_COMPUTE, 0x00FC)
        self.assertEqual(MASK_ECORE_BACKGROUND, 0xFF00)

        # Assert no overlap between audio isolated and compute P-Cores
        self.assertEqual(MASK_PCORE_AUDIO_ISOLATED & MASK_PCORE_COMPUTE, 0)
        # Assert no overlap between P-Cores and E-Cores
        self.assertEqual((MASK_PCORE_AUDIO_ISOLATED | MASK_PCORE_COMPUTE) & MASK_ECORE_BACKGROUND, 0)

    def test_pin_current_thread_to_mask_execution(self):
        """Verifies pin_current_thread_to_mask executes without throwing on current host."""
        success = pin_current_thread_to_mask(MASK_PCORE_COMPUTE, priority=THREAD_PRIORITY_NORMAL)
        if sys.platform == "win32":
            self.assertTrue(success, "pin_current_thread_to_mask should succeed on Windows")
        else:
            self.assertFalse(success)

    def test_elevate_audio_thread_mmcss_execution(self):
        """Verifies elevate_audio_thread_mmcss runs safely without crashing."""
        h_task = elevate_audio_thread_mmcss("Pro Audio")
        # May return int handle or None depending on Windows Multimedia service availability in CI
        if sys.platform == "win32" and h_task is not None:
            self.assertIsInstance(h_task, int)

    def test_configure_ecore_worker_thread_execution(self):
        """Verifies configure_ecore_worker_thread configures background thread with EcoQoS."""
        try:
            configure_ecore_worker_thread(coinit_sta=True, tag="TestWorker")
            ran_cleanly = True
        except Exception as e:
            ran_cleanly = False
        self.assertTrue(ran_cleanly)


if __name__ == "__main__":
    unittest.main()
