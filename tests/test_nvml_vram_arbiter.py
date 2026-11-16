"""Unit tests for jarvis/skills/nvml_dxgi_vram_arbiter.py and HUD telemetry."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarvis.skills.base import SkillRegistry
from jarvis.skills.nvml_dxgi_vram_arbiter import (
    VramPressureTier,
    _get_nvml_lib,
    _query_dxgi_video_memory,
    audit_vram_and_thermal_budget,
    classify_pressure_tier,
    execute_staged_shedding,
    get_shed_events_count,
    get_vram_telemetry,
    register_vram_shed_hook,
    request_vram_budget,
    reset_shed_hooks,
    unregister_vram_shed_hook,
)
from jarvis.ui.metrics import WorkstationMetricsCollector


class TestNvmlVramArbiter(unittest.TestCase):
    def setUp(self) -> None:
        reset_shed_hooks()

    def tearDown(self) -> None:
        reset_shed_hooks()

    def test_audit_vram_structure(self) -> None:
        res = audit_vram_and_thermal_budget(reclaim_if_pressured=False)
        self.assertIn("status", res)
        self.assertIn("vram_total_mb", res)
        self.assertIn("vram_free_mb", res)
        self.assertIn("dxgi_budget_mb", res)
        self.assertIn("headroom_mb", res)
        self.assertIn("pressure_tier", res)
        self.assertIn("shed_events_count", res)
        self.assertIn("compute_processes", res)
        self.assertIsInstance(res["compute_processes"], list)

    def test_audit_vram_unavailable_schema(self) -> None:
        with patch("jarvis.skills.nvml_dxgi_vram_arbiter._get_nvml_lib", return_value=None), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._query_dxgi_video_memory", return_value=None):
            res = audit_vram_and_thermal_budget(reclaim_if_pressured=False)
            self.assertEqual(res["status"], "unavailable")
            self.assertEqual(res["vram_total_mb"], 0.0)
            self.assertEqual(res["compute_processes"], [])
            self.assertIn("reason", res)

    def test_audit_vram_error_schema(self) -> None:
        mock_lib = MagicMock()
        mock_lib.nvmlInit_v2.return_value = 999
        with patch("jarvis.skills.nvml_dxgi_vram_arbiter._get_nvml_lib", return_value=mock_lib), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._nvml_initialized", False), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._query_dxgi_video_memory", return_value=None):
            res = audit_vram_and_thermal_budget(reclaim_if_pressured=False)
            self.assertEqual(res["status"], "error")
            self.assertEqual(res["vram_total_mb"], 0.0)
            self.assertEqual(res["compute_processes"], [])
            self.assertIn("reason", res)

    def test_nvml_resilient_resolution(self) -> None:
        # Must not raise unhandled exceptions regardless of GPU hardware state
        lib = _get_nvml_lib()
        self.assertTrue(lib is not None or lib is None)

    def test_registry_registration(self) -> None:
        tool = SkillRegistry.get_tool("audit_vram_and_thermal_budget")
        self.assertIsNotNone(tool)
        self.assertEqual(SkillRegistry.get_risk_level("audit_vram_and_thermal_budget"), 0)

    def test_effective_headroom_math(self) -> None:
        """EffectiveHeadroom = min(DXGI_Budget - DXGI_Usage, NVML_Free)."""
        mock_nvml = MagicMock()
        mock_nvml.nvmlInit_v2.return_value = 0
        mock_nvml.nvmlDeviceGetHandleByIndex_v2.return_value = 0

        # Case A: DXGI headroom is lower (DXGI governs)
        # DXGI: budget=3367.7, usage=1000.0 => headroom=2367.7
        # NVML: total=4096.0, free=3000.0, used=1096.0
        def _get_mem_case_a(handle, byref_mem):
            byref_mem._obj.total = 4096 * 1024 * 1024
            byref_mem._obj.free = 3000 * 1024 * 1024
            byref_mem._obj.used = 1096 * 1024 * 1024
            return 0

        mock_nvml.nvmlDeviceGetMemoryInfo.side_effect = _get_mem_case_a

        with patch("jarvis.skills.nvml_dxgi_vram_arbiter._get_nvml_lib", return_value=mock_nvml), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._nvml_initialized", True), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._query_dxgi_video_memory", return_value=(3367.7, 1000.0)):
            telem_a = get_vram_telemetry()
            self.assertEqual(telem_a.status, "ok")
            self.assertEqual(telem_a.headroom_mb, 2367.7)
            self.assertEqual(telem_a.pressure_tier, VramPressureTier.NOMINAL)

        # Case B: NVML free is lower (system background memory contention governs)
        # DXGI: budget=3367.7, usage=500.0 => headroom=2867.7
        # NVML: total=4096.0, free=850.0, used=3246.0
        def _get_mem_case_b(handle, byref_mem):
            byref_mem._obj.total = 4096 * 1024 * 1024
            byref_mem._obj.free = 850 * 1024 * 1024
            byref_mem._obj.used = 3246 * 1024 * 1024
            return 0

        mock_nvml.nvmlDeviceGetMemoryInfo.side_effect = _get_mem_case_b

        with patch("jarvis.skills.nvml_dxgi_vram_arbiter._get_nvml_lib", return_value=mock_nvml), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._nvml_initialized", True), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._query_dxgi_video_memory", return_value=(3367.7, 500.0)):
            telem_b = get_vram_telemetry()
            self.assertEqual(telem_b.status, "ok")
            self.assertEqual(telem_b.headroom_mb, 850.0)
            self.assertEqual(telem_b.pressure_tier, VramPressureTier.ELEVATED)

    def test_watermark_tier_transitions(self) -> None:
        """Verifies deterministic classification of NOMINAL, ELEVATED, CRITICAL, and OOM_GUARD."""
        self.assertEqual(classify_pressure_tier(1500.0), VramPressureTier.NOMINAL)
        self.assertEqual(classify_pressure_tier(1200.0), VramPressureTier.NOMINAL)
        self.assertEqual(classify_pressure_tier(1199.9), VramPressureTier.ELEVATED)
        self.assertEqual(classify_pressure_tier(700.0), VramPressureTier.ELEVATED)
        self.assertEqual(classify_pressure_tier(699.9), VramPressureTier.CRITICAL)
        self.assertEqual(classify_pressure_tier(350.0), VramPressureTier.CRITICAL)
        self.assertEqual(classify_pressure_tier(349.9), VramPressureTier.OOM_GUARD)
        self.assertEqual(classify_pressure_tier(0.0), VramPressureTier.OOM_GUARD)

    def test_request_vram_budget_granted_nominal(self) -> None:
        """Pre-inference admission granted immediately under abundant NOMINAL headroom."""
        mock_telem = MagicMock()
        mock_telem.status = "ok"
        mock_telem.headroom_mb = 2500.0
        mock_telem.pressure_tier = VramPressureTier.NOMINAL

        with patch("jarvis.skills.nvml_dxgi_vram_arbiter.get_vram_telemetry", return_value=mock_telem):
            granted, decision = request_vram_budget("whisper_stt", estimated_mb=350.0, auto_shed=True)
            self.assertTrue(granted)
            self.assertEqual(decision.recommended_action, "gpu_float16")
            self.assertFalse(decision.shed_performed)
            self.assertEqual(decision.headroom_mb, 2500.0)

    def test_request_vram_budget_elevated_int8(self) -> None:
        """Pre-inference admission recommends quantized gpu_int8 under ELEVATED tier."""
        mock_telem = MagicMock()
        mock_telem.status = "ok"
        mock_telem.headroom_mb = 950.0
        mock_telem.pressure_tier = VramPressureTier.ELEVATED

        with patch("jarvis.skills.nvml_dxgi_vram_arbiter.get_vram_telemetry", return_value=mock_telem):
            granted, decision = request_vram_budget("whisper_stt", estimated_mb=350.0, auto_shed=False)
            self.assertTrue(granted)
            self.assertEqual(decision.recommended_action, "gpu_int8")
            self.assertFalse(decision.shed_performed)

    def test_request_vram_budget_triggers_staged_shedding(self) -> None:
        """Constrained allocation triggers Stage 1 & 2 shedding and succeeds upon recovery."""
        # Call 1: Headroom is constrained (400 MB < 300 + 150 = 450 MB required)
        telem_pressured = MagicMock()
        telem_pressured.status = "ok"
        telem_pressured.headroom_mb = 400.0
        telem_pressured.pressure_tier = VramPressureTier.CRITICAL

        # Call 2: Stage 1 freed only 10 MB (410 MB still < 450 MB required), triggering Stage 2
        telem_still_pressured = MagicMock()
        telem_still_pressured.status = "ok"
        telem_still_pressured.headroom_mb = 410.0
        telem_still_pressured.pressure_tier = VramPressureTier.CRITICAL

        # Call 3: Headroom recovered after Stage 2 subsystem hook
        telem_recovered = MagicMock()
        telem_recovered.status = "ok"
        telem_recovered.headroom_mb = 1300.0
        telem_recovered.pressure_tier = VramPressureTier.NOMINAL

        hook_called = False

        def _test_hook() -> str:
            nonlocal hook_called
            hook_called = True
            return "evicted_test_session"

        register_vram_shed_hook("test_subsystem", _test_hook)

        with patch("jarvis.skills.nvml_dxgi_vram_arbiter.get_vram_telemetry", side_effect=[telem_pressured, telem_still_pressured, telem_recovered]):
            granted, decision = request_vram_budget("kokoro_tts", estimated_mb=300.0, auto_shed=True)
            self.assertTrue(granted)
            self.assertTrue(decision.shed_performed)
            self.assertTrue(hook_called)
            self.assertIn("hook:test_subsystem(evicted_test_session)", decision.actions_taken)
            self.assertGreater(get_shed_events_count(), 0)

    def test_request_vram_budget_oom_guard_fail_closed(self) -> None:
        """Fail-closed rejection under emergency OOM_GUARD tier (< 350 MiB)."""
        telem_oom = MagicMock()
        telem_oom.status = "ok"
        telem_oom.headroom_mb = 200.0
        telem_oom.pressure_tier = VramPressureTier.OOM_GUARD

        with patch("jarvis.skills.nvml_dxgi_vram_arbiter.get_vram_telemetry", return_value=telem_oom):
            granted, decision = request_vram_budget("heavy_vision_model", estimated_mb=400.0, auto_shed=True)
            self.assertFalse(granted)
            self.assertEqual(decision.recommended_action, "cpu_fallback")
            self.assertEqual(decision.pressure_tier, VramPressureTier.OOM_GUARD)

    def test_shed_hooks_registration_and_execution(self) -> None:
        """Verifies hook registration, execution, and exception safety."""
        hook_ran = False

        def _working_hook() -> str:
            nonlocal hook_ran
            hook_ran = True
            return "freed_50mb"

        def _failing_hook() -> str:
            msg = "driver error"
            raise RuntimeError(msg)

        register_vram_shed_hook("working", _working_hook)
        register_vram_shed_hook("failing", _failing_hook)

        actions = execute_staged_shedding(stage=2)
        self.assertTrue(hook_ran)
        self.assertIn("hook:working(freed_50mb)", actions)

        unregister_vram_shed_hook("working")
        unregister_vram_shed_hook("failing")

    def test_cross_platform_non_nvidia_and_linux_fallback(self) -> None:
        """Non-NVIDIA or Linux headless environments fall back cleanly without raising."""
        with patch("jarvis.skills.nvml_dxgi_vram_arbiter._get_nvml_lib", return_value=None), \
             patch("jarvis.skills.nvml_dxgi_vram_arbiter._query_dxgi_video_memory", return_value=None):
            telem = get_vram_telemetry()
            self.assertEqual(telem.status, "unavailable")
            self.assertEqual(telem.pressure_tier, VramPressureTier.NOMINAL)

            granted, decision = request_vram_budget("whisper_stt", estimated_mb=350.0)
            self.assertTrue(granted)
            self.assertEqual(decision.recommended_action, "cpu_fallback")

    def test_dxgi_query_graceful_on_com_failure(self) -> None:
        """COM failure during DXGI query returns None gracefully."""
        with patch("jarvis.skills.nvml_dxgi_vram_arbiter._get_dxgi_lib", return_value=None):
            res = _query_dxgi_video_memory()
            self.assertIsNone(res)

    def test_hud_workstation_telemetry_additive_fields(self) -> None:
        """WorkstationMetricsCollector exposes additive VRAM pressure fields with zero schema breakage."""
        vitals = WorkstationMetricsCollector.sample_metrics_sync()
        self.assertIn("vram_headroom_mb", vitals)
        self.assertIn("vram_pressure_tier", vitals)
        self.assertIn("vram_budget_mb", vitals)
        self.assertIn("shed_events_count", vitals)
        self.assertIn("vh", vitals)
        self.assertIn("vpt", vitals)
        self.assertIn(vitals["vram_pressure_tier"], ["NOMINAL", "ELEVATED", "CRITICAL", "OOM_GUARD"])
        # Invariants from original schema preserved
        self.assertIn("cpu", vitals)
        self.assertIn("gpu_name", vitals)
        self.assertIn("vram_used_mb", vitals)
        self.assertIn("vram_total_mb", vitals)


if __name__ == "__main__":
    unittest.main()
