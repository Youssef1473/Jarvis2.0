"""Hermetic unit tests for Plan 044: Dynamic VRAM-Aware Local Model Routing."""
from __future__ import annotations

import concurrent.futures
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from jarvis.core.brain_openai import OpenAIProviderMixin
from jarvis.core.cognition.cognitive_router import CognitiveRouter
from jarvis.core.providers.ollama import OllamaProvider
from jarvis.routing.vram_profile import (
    VramInferenceProfile,
    VramInferenceTier,
    register_ollama_vram_shed_hook,
    resolve_vram_inference_profile,
)
from jarvis.skills.nvml_dxgi_vram_arbiter import (
    VramPressureTelemetry,
    VramPressureTier,
    execute_staged_shedding,
    reset_shed_hooks,
)


def _make_telemetry(
    status: str = "ok",
    headroom_mb: float = 2000.0,
    tier: VramPressureTier = VramPressureTier.NOMINAL,
    reason: str | None = None,
) -> VramPressureTelemetry:
    """Creates a deterministic VramPressureTelemetry instance for testing."""
    return VramPressureTelemetry(
        status=status,
        total_mb=4096.0,
        used_mb=max(0.0, 4096.0 - headroom_mb),
        free_mb=headroom_mb,
        dxgi_budget_mb=3367.7,
        dxgi_current_usage_mb=max(0.0, 3367.7 - headroom_mb),
        headroom_mb=headroom_mb,
        pressure_tier=tier,
        gpu_temp_c=55,
        compute_processes=[],
        shed_events_count=0,
        reclaimed_action=None,
        reason=reason,
    )


class MockBrain(OpenAIProviderMixin):
    """Hermetic test harness for OpenAIProviderMixin testing."""

    def __init__(self) -> None:
        self.provider = "ollama"
        self.config = {"llm": {}}
        self.openai_messages: list[dict[str, Any]] = []
        self.active_tool_names = frozenset()
        self.active_task_mode = "general"
        self.temperature = 0.2
        self.openai_model = "qwen3.5:2b-q4_K_M"
        self.gemini_candidates = ("gemini-3.8-flash",)
        self.service_manager = MagicMock()
        self.last_routing_metadata: dict[str, Any] = {}
        self.openai_client = MagicMock()
        self.memory = MagicMock()
        self.last_latencies: dict[str, float] = {}

    def _extract_memory_insights(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def _is_gemini_configured(self) -> bool:
        return True

    def should_capture_screen(self, text: str) -> bool:
        return False

    def _get_system_instruction(self, text: str) -> str:
        return "System prompt"

    def _process_gemini_query(self, user_text: str, sentence_callback: Any = None) -> str:
        return f"Gemini failover reply to: {user_text}"


class TestVramAwareRouting(unittest.TestCase):
    """Validates VRAM pressure profile resolution, shedding triggers, and routing cascades."""

    def setUp(self) -> None:
        reset_shed_hooks()

    def tearDown(self) -> None:
        reset_shed_hooks()

    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_nominal_tier_profile_grants_full_offload(self, mock_telemetry: MagicMock) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=2000.0, tier=VramPressureTier.NOMINAL)
        profile = resolve_vram_inference_profile()

        self.assertEqual(profile.tier, VramPressureTier.NOMINAL)
        self.assertEqual(profile.num_ctx, 8192)
        self.assertEqual(profile.num_gpu, -1)
        self.assertTrue(profile.allow_gpu)
        self.assertFalse(profile.cloud_failover)
        self.assertEqual(profile.recommended_provider, "ollama")

    @patch("jarvis.routing.vram_profile.execute_staged_shedding")
    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_elevated_tier_triggers_stage1_shed_and_clamps_ctx(
        self, mock_telemetry: MagicMock, mock_shed: MagicMock
    ) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=900.0, tier=VramPressureTier.ELEVATED)
        profile = resolve_vram_inference_profile()

        mock_shed.assert_called_once_with(stage=1)
        self.assertEqual(profile.tier, VramPressureTier.ELEVATED)
        self.assertEqual(profile.num_ctx, 4096)
        self.assertEqual(profile.num_gpu, -1)
        self.assertTrue(profile.allow_gpu)
        self.assertFalse(profile.cloud_failover)
        self.assertEqual(profile.recommended_provider, "ollama")

    @patch("jarvis.routing.vram_profile.execute_staged_shedding")
    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_critical_tier_triggers_stage2_shed_and_bounds_gpu(
        self, mock_telemetry: MagicMock, mock_shed: MagicMock
    ) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=500.0, tier=VramPressureTier.CRITICAL)
        profile = resolve_vram_inference_profile(cloud_fallback_enabled=True, preferred_cloud_provider="gemini")

        mock_shed.assert_called_once_with(stage=2)
        self.assertEqual(profile.tier, VramPressureTier.CRITICAL)
        self.assertEqual(profile.num_ctx, 2048)
        self.assertEqual(profile.num_gpu, 12)
        self.assertTrue(profile.allow_gpu)
        self.assertTrue(profile.cloud_failover)
        self.assertEqual(profile.recommended_provider, "gemini")

    @patch("jarvis.routing.vram_profile.execute_staged_shedding")
    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_oom_guard_denies_gpu_and_forces_cloud_failover(
        self, mock_telemetry: MagicMock, mock_shed: MagicMock
    ) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=200.0, tier=VramPressureTier.OOM_GUARD)
        profile = resolve_vram_inference_profile(cloud_fallback_enabled=True, preferred_cloud_provider="gemini")

        mock_shed.assert_called_once_with(stage=2)
        self.assertEqual(profile.tier, VramPressureTier.OOM_GUARD)
        self.assertEqual(profile.num_ctx, 2048)
        self.assertEqual(profile.num_gpu, 0)
        self.assertFalse(profile.allow_gpu)
        self.assertTrue(profile.cloud_failover)
        self.assertEqual(profile.recommended_provider, "gemini")

    @patch("jarvis.routing.vram_profile.execute_staged_shedding")
    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_oom_guard_without_cloud_falls_back_to_cpu(
        self, mock_telemetry: MagicMock, mock_shed: MagicMock
    ) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=200.0, tier=VramPressureTier.OOM_GUARD)
        profile = resolve_vram_inference_profile(cloud_fallback_enabled=False)

        mock_shed.assert_called_once_with(stage=2)
        self.assertEqual(profile.tier, VramPressureTier.OOM_GUARD)
        self.assertEqual(profile.num_ctx, 2048)
        self.assertEqual(profile.num_gpu, 0)
        self.assertFalse(profile.allow_gpu)
        self.assertFalse(profile.cloud_failover)
        self.assertEqual(profile.recommended_provider, "cpu")

    @patch("jarvis.core.providers.ollama.OllamaProvider.unload_model", return_value=True)
    def test_ollama_shed_hook_unloads_model(self, mock_unload: MagicMock) -> None:
        register_ollama_vram_shed_hook(base_url="http://127.0.0.1:11434/v1", model="qwen3.5:2b-q4_K_M")
        actions = execute_staged_shedding(stage=2)

        mock_unload.assert_called_once_with(base_url="http://127.0.0.1:11434/v1", model="qwen3.5:2b-q4_K_M")
        self.assertTrue(any("hook:ollama_local(unloaded)" in a for a in actions))

    @patch("jarvis.core.providers.ollama.OpenAI")
    def test_ollama_provider_passes_extra_body_options(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_stream = [MagicMock(choices=[MagicMock(delta=MagicMock(content="Hello", tool_calls=None))])]
        mock_client.chat.completions.create.return_value = mock_stream
        mock_openai_cls.return_value = mock_client

        provider = OllamaProvider(model="qwen3.5:2b-q4_K_M")
        response = provider.generate_turn(
            messages=[{"role": "user", "content": "hi"}],
            options={"num_ctx": 4096, "num_gpu": -1},
        )

        self.assertEqual(response.content, "Hello")
        mock_client.chat.completions.create.assert_called_once()
        _, kwargs = mock_client.chat.completions.create.call_args
        self.assertEqual(kwargs.get("extra_body"), {"options": {"num_ctx": 4096, "num_gpu": -1}})

    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    @patch("jarvis.routing.vram_profile.execute_staged_shedding")
    def test_brain_openai_executes_cloud_failover_under_oom_guard(
        self, mock_shed: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=200.0, tier=VramPressureTier.OOM_GUARD)
        brain = MockBrain()

        res = brain._process_openai_compatible_query("what is my battery level?")

        self.assertEqual(res, "Gemini failover reply to: what is my battery level?")
        self.assertEqual(brain.last_routing_metadata.get("vram_tier"), "OOM_GUARD")
        self.assertTrue(brain.last_routing_metadata.get("cloud_failover"))

    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_brain_openai_records_vram_telemetry_metadata(self, mock_telemetry: MagicMock) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=2000.0, tier=VramPressureTier.NOMINAL)
        brain = MockBrain()
        mock_stream = [MagicMock(choices=[MagicMock(delta=MagicMock(content="OK", tool_calls=None))])]
        brain.openai_client.chat.completions.create.return_value = mock_stream

        res = brain._process_openai_compatible_query("hello")

        self.assertEqual(res, "OK")
        self.assertEqual(brain.last_routing_metadata.get("vram_tier"), "NOMINAL")
        self.assertEqual(brain.last_routing_metadata.get("applied_num_ctx"), 8192)
        self.assertEqual(brain.last_routing_metadata.get("applied_num_gpu"), -1)
        self.assertFalse(brain.last_routing_metadata.get("cloud_failover"))

    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_non_nvidia_ci_fallback_produces_safe_defaults(self, mock_telemetry: MagicMock) -> None:
        mock_telemetry.return_value = _make_telemetry(
            status="unavailable", headroom_mb=0.0, tier=VramPressureTier.NOMINAL, reason="No GPU"
        )
        profile = resolve_vram_inference_profile()

        self.assertEqual(profile.tier, VramInferenceTier.UNAVAILABLE)
        self.assertEqual(profile.num_ctx, 4096)
        self.assertEqual(profile.num_gpu, 0)
        self.assertFalse(profile.allow_gpu)
        self.assertFalse(profile.cloud_failover)
        self.assertEqual(profile.recommended_provider, "ollama")

    @patch("jarvis.routing.vram_profile.get_vram_telemetry")
    def test_vram_profile_thread_safety(self, mock_telemetry: MagicMock) -> None:
        mock_telemetry.return_value = _make_telemetry(headroom_mb=1500.0, tier=VramPressureTier.NOMINAL)

        def _resolve() -> VramInferenceProfile:
            return resolve_vram_inference_profile()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_resolve) for _ in range(32)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(results), 32)
        for r in results:
            self.assertEqual(r.tier, VramPressureTier.NOMINAL)
            self.assertEqual(r.num_ctx, 8192)
            self.assertEqual(r.num_gpu, -1)
            self.assertFalse(r.cloud_failover)

    @patch("jarvis.core.providers.ollama.OpenAI")
    def test_clamped_context_preserves_conversation_turn(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_stream = [
            MagicMock(choices=[MagicMock(delta=MagicMock(content="Clamped response.", tool_calls=None))])
        ]
        mock_client.chat.completions.create.return_value = mock_stream
        mock_openai_cls.return_value = mock_client

        provider = OllamaProvider(model="qwen3.5:2b-q4_K_M")
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": "turn 2"},
        ]
        resp = provider.generate_turn(messages=messages, options={"num_ctx": 2048, "num_gpu": 0})

        self.assertEqual(resp.content, "Clamped response.")
        self.assertEqual(resp.model, "qwen3.5:2b-q4_K_M")

    def test_cognitive_router_hybrid_route_vram_failover(self) -> None:
        router = CognitiveRouter()
        failover_profile = VramInferenceProfile(
            tier=VramPressureTier.CRITICAL,
            headroom_mb=500.0,
            num_ctx=2048,
            num_gpu=12,
            allow_gpu=True,
            cloud_failover=True,
            recommended_provider="gemini",
            reason="Pressure failover",
        )
        route = router.determine_hybrid_route(
            user_text="adjust the volume to 50%",
            active_tool_names=frozenset({"adjust_volume"}),
            has_screen_intent=False,
            active_task_mode="general",
            has_antigravity=True,
            has_ollama=True,
            has_groq=True,
            has_gemini=True,
            has_copilot=True,
            vram_profile=failover_profile,
        )
        self.assertEqual(route, "gemini_default")

    def test_cognitive_router_hybrid_route_nominal_no_failover(self) -> None:
        router = CognitiveRouter()
        nominal_profile = VramInferenceProfile(
            tier=VramPressureTier.NOMINAL,
            headroom_mb=2000.0,
            num_ctx=8192,
            num_gpu=-1,
            allow_gpu=True,
            cloud_failover=False,
            recommended_provider="ollama",
            reason="Nominal",
        )
        route = router.determine_hybrid_route(
            user_text="adjust the volume to 50%",
            active_tool_names=frozenset({"adjust_volume"}),
            has_screen_intent=False,
            active_task_mode="general",
            has_antigravity=True,
            has_ollama=True,
            has_groq=True,
            has_gemini=True,
            has_copilot=True,
            vram_profile=nominal_profile,
        )
        self.assertEqual(route, "ollama_os")


if __name__ == "__main__":
    unittest.main()
