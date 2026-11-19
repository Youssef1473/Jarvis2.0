"""Unit tests for Windows Win32 Thread Affinity and Real-Time Priority Scheduler."""
import sys
import unittest
from unittest.mock import patch

from jarvis.core.win32_affinity import (
    THREAD_PRIORITY_HIGHEST,
    THREAD_PRIORITY_TIME_CRITICAL,
    elevate_thread_priority,
    get_pcore_affinity_mask,
    pin_and_elevate_current_thread,
    pin_thread_to_pcores,
)


class TestAudioAffinity(unittest.TestCase):
    def test_get_pcore_affinity_mask_topology(self):
        """Verifies calculation of Performance core affinity bitmask across CPU topologies."""
        with patch("os.cpu_count", return_value=16):
            # 16-thread hybrid CPU (i5-12500H): 8 P-threads (mask 0x00FF = 255)
            self.assertEqual(get_pcore_affinity_mask(), 0x00FF)

        with patch("os.cpu_count", return_value=12):
            self.assertEqual(get_pcore_affinity_mask(), 0x00FF)

        with patch("os.cpu_count", return_value=8):
            # 8-core CPU: all 8 cores (mask 0xFF = 255)
            self.assertEqual(get_pcore_affinity_mask(), 0xFF)

        with patch("os.cpu_count", return_value=4):
            # 4-core CPU: mask 0xF = 15
            self.assertEqual(get_pcore_affinity_mask(), 0x0F)

    def test_get_physical_pcore_mask_topology(self):
        """Verifies calculation of non-SMT physical P-core bitmask across topologies."""
        from jarvis.core.win32_affinity import get_physical_pcore_mask

        with patch("os.cpu_count", return_value=16):
            # 16-thread hybrid: physical cores 0, 2, 4, 6 -> 0x55
            self.assertEqual(get_physical_pcore_mask(), 0x55)

        with patch("os.cpu_count", return_value=12):
            self.assertEqual(get_physical_pcore_mask(), 0x55)

        with patch("os.cpu_count", return_value=8):
            self.assertEqual(get_physical_pcore_mask(), 0x55)

        with patch("os.cpu_count", return_value=4):
            self.assertEqual(get_physical_pcore_mask(), 0x05)

    def test_get_wakeword_affinity_mask_topology(self):
        """Verifies calculation of wake-word affinity mask avoiding P-Core 0 across topologies."""
        from jarvis.core.win32_affinity import (
            MASK_PCORE_AUDIO_ISOLATED,
            MASK_PCORE_WAKEWORD,
            get_wakeword_affinity_mask,
        )

        with patch("os.cpu_count", return_value=16):
            # 16-thread hybrid: 0x0054 (Logical CPUs 2, 4, 6)
            mask = get_wakeword_affinity_mask()
            self.assertEqual(mask, MASK_PCORE_WAKEWORD)
            self.assertEqual(mask & MASK_PCORE_AUDIO_ISOLATED, 0)

        with patch("os.cpu_count", return_value=12):
            mask = get_wakeword_affinity_mask()
            self.assertEqual(mask, MASK_PCORE_WAKEWORD)
            self.assertEqual(mask & MASK_PCORE_AUDIO_ISOLATED, 0)

        with patch("os.cpu_count", return_value=8):
            mask = get_wakeword_affinity_mask()
            self.assertEqual(mask, MASK_PCORE_WAKEWORD)
            self.assertEqual(mask & MASK_PCORE_AUDIO_ISOLATED, 0)

        with patch("os.cpu_count", return_value=4):
            # 4-core CPU: 0x04 (Logical CPU 2)
            mask = get_wakeword_affinity_mask()
            self.assertEqual(mask, 0x04)
            self.assertEqual(mask & MASK_PCORE_AUDIO_ISOLATED, 0)

    @unittest.skipUnless(sys.platform == "win32", "Win32 native API required for live affinity tests")
    def test_live_win32_pin_and_elevate(self):
        """Verifies live Win32 thread affinity and priority elevation on Windows host."""
        success = pin_and_elevate_current_thread(tag="UnitTestAffinity")
        self.assertTrue(success, "Win32 pin_and_elevate_current_thread should succeed on Windows")

    @unittest.skipUnless(sys.platform == "win32", "Win32 native API required for live affinity tests")
    def test_live_win32_mmcss_registration(self):
        """Verifies Windows MMCSS 'Pro Audio' elevation and revert on Windows host."""
        from jarvis.core.win32_affinity import elevate_to_pro_audio_mmcss, revert_mmcss
        h_task = elevate_to_pro_audio_mmcss("Pro Audio")
        if h_task:
            self.assertIsInstance(h_task, int)
            self.assertGreater(h_task, 0)
            rev = revert_mmcss(h_task)
            self.assertTrue(rev)

    @unittest.skipUnless(sys.platform == "win32", "Win32 native API required for live affinity tests")
    def test_live_win32_elevate_priority_levels(self):
        """Verifies setting THREAD_PRIORITY_HIGHEST and TIME_CRITICAL on Windows host."""
        res_high = elevate_thread_priority(priority=THREAD_PRIORITY_HIGHEST)
        self.assertTrue(res_high)

        res_critical = elevate_thread_priority(priority=THREAD_PRIORITY_TIME_CRITICAL)
        self.assertTrue(res_critical)

        # Revert back to HIGHEST
        elevate_thread_priority(priority=THREAD_PRIORITY_HIGHEST)

    def test_non_windows_graceful_fallback(self):
        """Verifies that on non-Windows platforms, methods safely return False without error."""
        from jarvis.core.win32_affinity import elevate_to_pro_audio_mmcss, revert_mmcss
        with patch("sys.platform", "linux"):
            self.assertFalse(pin_thread_to_pcores())
            self.assertFalse(elevate_thread_priority())
            self.assertFalse(pin_and_elevate_current_thread())
            self.assertIsNone(elevate_to_pro_audio_mmcss())
            self.assertFalse(revert_mmcss(123))

    def test_alder_lake_4p_8e_exact_parity(self):
        """Verifies exact bit-for-bit parity with Alder Lake i5-12500H topology."""
        from jarvis.core.win32_affinity import compute_topology_from_cores

        # 4 P-cores (SMT, eff=1) + 8 E-cores (non-SMT, eff=0)
        alder_lake_cores = [
            (0x0003, 1),
            (0x000C, 1),
            (0x0030, 1),
            (0x00C0, 1),
            (0x0100, 0),
            (0x0200, 0),
            (0x0400, 0),
            (0x0800, 0),
            (0x1000, 0),
            (0x2000, 0),
            (0x4000, 0),
            (0x8000, 0),
        ]
        profile = compute_topology_from_cores(alder_lake_cores)
        self.assertTrue(profile.is_hybrid)
        self.assertEqual(profile.logical_cpu_count, 16)
        self.assertEqual(profile.playback_mask, 0x0003)
        self.assertEqual(profile.wakeword_mask, 0x0054)
        self.assertEqual(profile.compute_mask, 0x00FC)
        self.assertEqual(profile.background_mask, 0xFF00)
        self.assertEqual(profile.playback_mask & profile.wakeword_mask, 0)
        self.assertEqual(profile.playback_mask & profile.compute_mask, 0)
        self.assertEqual(profile.playback_mask & profile.background_mask, 0)
        self.assertEqual(profile.compute_mask & profile.background_mask, 0)

    def test_homogeneous_8core_ryzen_topology(self):
        """Verifies homogeneous 8-core / 16-thread CPU partitioning (e.g. AMD Ryzen 7)."""
        from jarvis.core.win32_affinity import compute_topology_from_cores

        ryzen_cores = [
            (0x0003, 0),
            (0x000C, 0),
            (0x0030, 0),
            (0x00C0, 0),
            (0x0300, 0),
            (0x0C00, 0),
            (0x3000, 0),
            (0xC000, 0),
        ]
        profile = compute_topology_from_cores(ryzen_cores)
        self.assertFalse(profile.is_hybrid)
        self.assertEqual(profile.logical_cpu_count, 16)
        self.assertNotEqual(profile.playback_mask, 0)
        self.assertNotEqual(profile.wakeword_mask, 0)
        self.assertNotEqual(profile.compute_mask, 0)
        self.assertNotEqual(profile.background_mask, 0)
        self.assertEqual(profile.playback_mask & profile.wakeword_mask, 0)
        self.assertEqual(profile.playback_mask & profile.compute_mask, 0)
        self.assertEqual(profile.playback_mask, 0x0003)
        self.assertEqual(profile.wakeword_mask, 0x0054)
        self.assertEqual(profile.compute_mask, 0xFFFC)
        self.assertEqual(profile.background_mask, 0xFF00)

    def test_small_vm_core_fallback(self):
        """Verifies graceful fallback on 1-core and 2-core VMs with non-zero masks."""
        from jarvis.core.win32_affinity import compute_topology_from_cores

        # 2-core non-SMT VM
        vm_2c = [(0x0001, 0), (0x0002, 0)]
        p2 = compute_topology_from_cores(vm_2c)
        self.assertFalse(p2.is_hybrid)
        self.assertEqual(p2.logical_cpu_count, 2)
        self.assertGreater(p2.playback_mask, 0)
        self.assertGreater(p2.wakeword_mask, 0)
        self.assertGreater(p2.compute_mask, 0)
        self.assertGreater(p2.background_mask, 0)
        self.assertEqual(p2.playback_mask & p2.wakeword_mask, 0)

        # 1-core VM
        vm_1c = [(0x0001, 0)]
        p1 = compute_topology_from_cores(vm_1c)
        self.assertFalse(p1.is_hybrid)
        self.assertEqual(p1.logical_cpu_count, 1)
        self.assertGreater(p1.playback_mask, 0)
        self.assertGreater(p1.wakeword_mask, 0)
        self.assertGreater(p1.compute_mask, 0)
        self.assertGreater(p1.background_mask, 0)

    def test_win32_topology_failure_and_non_windows_fallback(self):
        """Verifies graceful fallback when Win32 discovery fails or runs on non-Windows."""
        from jarvis.core.win32_affinity import discover_cpu_topology

        with patch("sys.platform", "linux"):
            p_linux = discover_cpu_topology(force_refresh=True)
            self.assertEqual(p_linux.source, "non_windows")
            self.assertGreater(p_linux.playback_mask, 0)
            self.assertGreater(p_linux.wakeword_mask, 0)
            self.assertGreater(p_linux.compute_mask, 0)
            self.assertGreater(p_linux.background_mask, 0)

        with patch("sys.platform", "win32"), patch("ctypes.WinDLL", side_effect=OSError("DLL load failed")):
            p_err = discover_cpu_topology(force_refresh=True)
            self.assertEqual(p_err.source, "fallback_count")
            self.assertGreater(p_err.playback_mask, 0)
            self.assertGreater(p_err.wakeword_mask, 0)

        # Restore cached topology for subsequent tests
        discover_cpu_topology(force_refresh=True)


if __name__ == "__main__":
    unittest.main()
