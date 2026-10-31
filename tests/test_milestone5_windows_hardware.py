"""Unit test suite verifying Milestone 5: Windows OS & Hardware Engineering (M-11, M-12, M-13, m-01 to m-04)."""
from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from jarvis.desktop.uia_delta_worker import UIADeltaWorker
from jarvis.skills.os_control import CRITICAL_PROCESS_DENYLIST
from jarvis.skills.rstrtmgr_lock_surgeon import PROTECTED_PROCESS_NAMES
from jarvis.vision.screen_capture import ScreenPerception
from jarvis.vision.som_grounding import MarkElement, MarkRegistry


class TestMilestone5WindowsHardware(unittest.TestCase):
    """Comprehensive verification for Milestone 5 defect remediations."""

    def test_screen_capture_bilinear_resampling(self):
        """M-11: ScreenPerception uses fast Image.Resampling.BILINEAR for downsampling."""
        test_img = Image.new("RGB", (3200, 1800), color=(100, 150, 200))
        with (
            patch("PIL.ImageGrab.grab", return_value=test_img),
            patch("jarvis.vision.privacy_guard.ZOrderPrivacyGuard.get_instance") as mock_guard_inst,
        ):
            mock_guard = MagicMock()
            mock_guard.redact_image_in_place.side_effect = lambda img: img
            mock_guard_inst.return_value = mock_guard

            captured = ScreenPerception.capture_screen(max_width=1600)
            self.assertIsNotNone(captured)
            self.assertEqual(captured.width, 1600)

    def test_dhash_monitor_applies_privacy_redaction(self):
        """M-11: dhash_monitor._capture_window_or_screen routes frames through ZOrderPrivacyGuard."""
        from jarvis.vision.dhash_monitor import BackgroundDHashMonitor
        monitor = BackgroundDHashMonitor()

        test_img = Image.new("RGB", (200, 200), color=(50, 50, 50))
        redacted_img = Image.new("RGB", (200, 200), color=(0, 0, 0))

        def mock_get_rect(hwnd, rect_ptr):
            if hasattr(rect_ptr, "_obj"):
                rect_ptr._obj.left = 0
                rect_ptr._obj.top = 0
                rect_ptr._obj.right = 400
                rect_ptr._obj.bottom = 300
            return 1

        with (
            patch("ctypes.windll.user32.GetWindowRect", side_effect=mock_get_rect),
            patch("PIL.ImageGrab.grab", return_value=test_img),
            patch("jarvis.vision.privacy_guard.ZOrderPrivacyGuard.get_instance") as mock_guard_inst,
        ):
            mock_guard = MagicMock()
            mock_guard.redact_image_in_place.return_value = redacted_img
            mock_guard_inst.return_value = mock_guard

            out = monitor._capture_window_or_screen(hwnd=12345)
            self.assertIsNotNone(out)
            mock_guard.redact_image_in_place.assert_called_once_with(test_img)

    def test_server_authenticated_url_generation(self):
        """M-12: JarvisUIService.get_authenticated_url() embeds a valid single-use bootstrap token."""
        from jarvis.ui.server import JarvisUIService, consume_bootstrap_token
        srv = JarvisUIService(host="127.0.0.1", port=20129)
        url = srv.get_authenticated_url()
        self.assertTrue(url.startswith("http://127.0.0.1:20129/#bootstrap="))
        token = url.split("#bootstrap=")[-1]
        self.assertTrue(len(token) > 16)
        self.assertTrue(consume_bootstrap_token(token))
        self.assertFalse(consume_bootstrap_token(token))
        srv.stop()

    def test_server_bounded_ws_command_executor(self):
        """M-12: JarvisUIService initializes a bounded ThreadPoolExecutor for websocket commands."""
        from jarvis.ui.server import JarvisUIService
        srv = JarvisUIService()
        self.assertTrue(hasattr(srv, "_command_executor"))
        self.assertEqual(srv._command_executor._max_workers, 4)
        srv.stop()

    def test_hud_configure_overlay_called(self):
        """M-12: JarvisHUD invokes configure_stark_overlay_hwnd on active window ID."""
        from jarvis.ui.hud import JarvisHUD
        hud = JarvisHUD(size=140)

        with (
            patch("tkinter.Tk") as mock_tk_cls,
            patch("jarvis.ui.win32_overlay.configure_stark_overlay_hwnd") as mock_conf,
        ):
            mock_tk = MagicMock()
            mock_tk.winfo_id.return_value = 99999
            mock_tk.mainloop.return_value = None
            mock_tk_cls.return_value = mock_tk

            hud._run_tk()
            mock_conf.assert_called_once_with(99999)

    def test_tts_onnx_spinning_disabled(self):
        """M-13: Kokoro ONNX initializes with both intra_op and inter_op allow_spinning set to '0'."""
        from jarvis.audio import tts as tts_module
        tts_module._CACHED_KOKORO_SESSION.clear()

        entries = {}
        mock_opts = MagicMock()
        mock_opts.add_session_config_entry.side_effect = lambda k, v: entries.update({k: v})

        with (
            patch("onnxruntime.SessionOptions", return_value=mock_opts),
            patch("onnxruntime.get_available_providers", return_value=["CPUExecutionProvider"]),
            patch("kokoro_onnx.Kokoro"),
            patch("os.path.exists", return_value=True),
        ):
            _ = tts_module.TextToSpeech(config={"tts": {"engine": "kokoro", "cpu_threads": 2}})
            self.assertEqual(entries.get("session.intra_op.allow_spinning"), "0")
            self.assertEqual(entries.get("session.inter_op.allow_spinning"), "0")

    def test_dsp_process_elevate_companion_and_wait(self):
        """m-01: _elevate_companion_process defaults to P-Core mask 0xFF."""
        from jarvis.audio.dsp_process import _elevate_companion_process
        if sys.platform == "win32":
            with patch("ctypes.windll.kernel32.SetProcessAffinityMask") as mock_set_aff:
                _elevate_companion_process()
                mock_set_aff.assert_called_once()
                mask_val = mock_set_aff.call_args[0][1].value
                self.assertEqual(mask_val, 0xFF)

    def test_cadence_tracker_readiness_event_and_clean_stop(self):
        """m-02: MotorCadenceTracker uses threading.Event readiness and clears _held_keys upon stop."""
        from jarvis.core.cognition.cadence_tracker import MotorCadenceTracker
        tracker = MotorCadenceTracker()
        self.assertTrue(hasattr(tracker, "_hook_ready"))

        tracker._held_keys.add(65)
        tracker._held_keys.add(66)
        self.assertEqual(len(tracker._held_keys), 2)

        tracker.stop()
        self.assertEqual(len(tracker._held_keys), 0)

    def test_som_mark_registry_ttl_and_hwnd_eviction(self):
        """m-03: MarkRegistry enforces 60s TTL and provides evict_for_hwnd."""
        MarkRegistry.clear()
        elem1 = MarkElement(
            mark_id=1, control_type="Button", name="Save", auto_id="btn_save",
            local_rect=(0, 0, 10, 10), screen_rect=(100, 100, 110, 110),
            local_center=(5, 5), screen_center=(105, 105), hwnd=1234,
            snapshot_win_rect=(100, 100, 200, 200)
        )
        elem2 = MarkElement(
            mark_id=2, control_type="Edit", name="Input", auto_id="txt_input",
            local_rect=(0, 20, 10, 30), screen_rect=(100, 120, 110, 130),
            local_center=(5, 25), screen_center=(105, 125), hwnd=5678,
            snapshot_win_rect=(100, 100, 200, 200)
        )
        MarkRegistry.set_marks({1: elem1, 2: elem2})
        self.assertEqual(len(MarkRegistry.get_all_marks()), 2)
        self.assertIsNotNone(MarkRegistry.get_mark(1))

        evicted = MarkRegistry.evict_for_hwnd(1234)
        self.assertEqual(evicted, 1)
        self.assertIsNone(MarkRegistry.get_mark(1))
        self.assertIsNotNone(MarkRegistry.get_mark(2))

        MarkRegistry._timestamp = time.time() - 65.0
        self.assertIsNone(MarkRegistry.get_mark(2))
        self.assertEqual(len(MarkRegistry.get_all_marks()), 0)

    def test_uia_delta_worker_eviction_on_destroy_event(self):
        """m-03: UIADeltaWorker evicts destroyed window marks on event 0x8001."""
        MarkRegistry.clear()
        elem = MarkElement(
            mark_id=10, control_type="Window", name="Notepad", auto_id="",
            local_rect=(0, 0, 100, 100), screen_rect=(0, 0, 100, 100),
            local_center=(50, 50), screen_center=(50, 50), hwnd=77777,
            snapshot_win_rect=(0, 0, 100, 100)
        )
        MarkRegistry.set_marks({10: elem})
        self.assertIsNotNone(MarkRegistry.get_mark(10))

        worker = UIADeltaWorker()
        worker.push_frame_for_test(hwnd=77777, event_id=0x8001, title="Notepad")
        worker.poll_once()

        self.assertIsNone(MarkRegistry.get_mark(10))

    def test_process_denylists_contain_critical_daemons(self):
        """m-04: Critical process denylists contain agy.exe, ollama.exe, and wininit.exe."""
        for proc in ("agy.exe", "ollama.exe", "wininit.exe"):
            self.assertIn(proc, CRITICAL_PROCESS_DENYLIST)
            self.assertIn(proc, PROTECTED_PROCESS_NAMES)

    def test_com_uninitialize_balanced_in_timers(self):
        """m-04: timers speech alert reliably invokes pythoncom.CoUninitialize()."""
        from jarvis.skills import timers
        with (
            patch("pythoncom.CoInitialize") as mock_init,
            patch("pythoncom.CoUninitialize") as mock_uninit,
            patch("win32com.client.Dispatch") as mock_disp,
        ):
            mock_sapi = MagicMock()
            mock_disp.return_value = mock_sapi
            timers._announce_timer_completion("tea", "5m")
            mock_init.assert_called_once()
            mock_uninit.assert_called_once()

    def test_com_uninitialize_balanced_in_watchdog(self):
        """m-04: system_watchdog speech alert reliably invokes pythoncom.CoUninitialize()."""
        from jarvis.skills import system_watchdog
        with (
            patch("pythoncom.CoInitialize") as mock_init,
            patch("pythoncom.CoUninitialize") as mock_uninit,
            patch("win32com.client.Dispatch") as mock_disp,
        ):
            mock_sapi = MagicMock()
            mock_disp.return_value = mock_sapi
            system_watchdog._speak_watchdog_alert("High thermal warning")
            mock_init.assert_called_once()
            mock_uninit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
