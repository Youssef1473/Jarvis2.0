"""Dynamic VRAM-aware inference profile governor for Jarvis 2.0 local models.

Inspects real-time DXGI 1.4 WDDM budget tracking and NVML physical VRAM telemetry
from `jarvis.skills.nvml_dxgi_vram_arbiter`, dynamically scaling local LLM context
windows (num_ctx: 8192 -> 4096 -> 2048) and GPU layer offload (num_gpu: -1 -> <=12 -> 0),
or triggering seamless cloud failover to prevent WDDM shared-memory paging and TDR resets.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from jarvis.skills.nvml_dxgi_vram_arbiter import (
    VramPressureTier,
    execute_staged_shedding,
    get_vram_telemetry,
    register_vram_shed_hook,
)

logger = logging.getLogger("Jarvis.Routing.VramProfile")


class VramInferenceTier(str, Enum):
    """Deterministic VRAM inference pressure operating tiers."""

    NOMINAL = "NOMINAL"
    ELEVATED = "ELEVATED"
    CRITICAL = "CRITICAL"
    OOM_GUARD = "OOM_GUARD"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class VramInferenceProfile:
    """Immutable inference profile dynamically scaled to GPU VRAM headroom."""

    tier: VramPressureTier | VramInferenceTier | str
    headroom_mb: float
    num_ctx: int
    num_gpu: int
    allow_gpu: bool
    cloud_failover: bool
    recommended_provider: str  # "ollama" | "gemini" | "openrouter" | "cpu"
    reason: str


def resolve_vram_inference_profile(
    cloud_fallback_enabled: bool = True,
    preferred_cloud_provider: str = "gemini",
) -> VramInferenceProfile:
    """Queries real-time VRAM telemetry and derives calibrated LLM inference parameters.

    Calculates dynamic context window (`num_ctx`) and GPU layer offload (`num_gpu`)
    constraints based on the current WDDM/NVML effective headroom:
      - NOMINAL (>= 1200 MiB): num_ctx=8192, num_gpu=-1, allow_gpu=True, cloud_failover=False
      - ELEVATED (700-1199 MiB): triggers Stage 1 shed, num_ctx=4096, num_gpu=-1, allow_gpu=True, cloud_failover=False
      - CRITICAL (350-699 MiB): triggers Stage 2 shed, num_ctx=2048, num_gpu=12, allow_gpu=True, cloud_failover=cloud_fallback_enabled
      - OOM_GUARD (< 350 MiB): triggers Stage 2 shed, num_ctx=2048, num_gpu=0, allow_gpu=False, cloud_failover=cloud_fallback_enabled
      - UNAVAILABLE (CI/Non-NVIDIA): num_ctx=4096, num_gpu=0, allow_gpu=False, cloud_failover=False
    """
    telemetry = get_vram_telemetry()

    if telemetry.status != "ok":
        return VramInferenceProfile(
            tier=VramInferenceTier.UNAVAILABLE,
            headroom_mb=telemetry.headroom_mb,
            num_ctx=4096,
            num_gpu=0,
            allow_gpu=False,
            cloud_failover=False,
            recommended_provider="ollama",
            reason=f"VRAM telemetry unavailable ({telemetry.reason or telemetry.status}); safe CPU defaults applied.",
        )

    # Pressure Tier NOMINAL: >= 1200 MiB
    if telemetry.pressure_tier == VramPressureTier.NOMINAL:
        return VramInferenceProfile(
            tier=VramPressureTier.NOMINAL,
            headroom_mb=telemetry.headroom_mb,
            num_ctx=8192,
            num_gpu=-1,
            allow_gpu=True,
            cloud_failover=False,
            recommended_provider="ollama",
            reason=f"Sufficient VRAM headroom ({telemetry.headroom_mb:.1f} MiB); granting full GPU offload and 8k context.",
        )

    # Pressure Tier ELEVATED: 700 - 1199 MiB
    if telemetry.pressure_tier == VramPressureTier.ELEVATED:
        execute_staged_shedding(stage=1)
        telemetry = get_vram_telemetry()
        if telemetry.pressure_tier == VramPressureTier.NOMINAL:
            return VramInferenceProfile(
                tier=VramPressureTier.NOMINAL,
                headroom_mb=telemetry.headroom_mb,
                num_ctx=8192,
                num_gpu=-1,
                allow_gpu=True,
                cloud_failover=False,
                recommended_provider="ollama",
                reason=f"Headroom restored to NOMINAL ({telemetry.headroom_mb:.1f} MiB) after Stage 1 shed.",
            )
        return VramInferenceProfile(
            tier=VramPressureTier.ELEVATED,
            headroom_mb=telemetry.headroom_mb,
            num_ctx=4096,
            num_gpu=-1,
            allow_gpu=True,
            cloud_failover=False,
            recommended_provider="ollama",
            reason=f"Elevated VRAM pressure ({telemetry.headroom_mb:.1f} MiB); Stage 1 shed executed, context clamped to 4096.",
        )

    # Pressure Tier CRITICAL: 350 - 699 MiB
    if telemetry.pressure_tier == VramPressureTier.CRITICAL:
        execute_staged_shedding(stage=2)
        telemetry = get_vram_telemetry()
        if telemetry.pressure_tier == VramPressureTier.NOMINAL:
            return VramInferenceProfile(
                tier=VramPressureTier.NOMINAL,
                headroom_mb=telemetry.headroom_mb,
                num_ctx=8192,
                num_gpu=-1,
                allow_gpu=True,
                cloud_failover=False,
                recommended_provider="ollama",
                reason=f"Headroom restored to NOMINAL ({telemetry.headroom_mb:.1f} MiB) after Stage 2 shed.",
            )
        if telemetry.pressure_tier == VramPressureTier.ELEVATED:
            return VramInferenceProfile(
                tier=VramPressureTier.ELEVATED,
                headroom_mb=telemetry.headroom_mb,
                num_ctx=4096,
                num_gpu=-1,
                allow_gpu=True,
                cloud_failover=False,
                recommended_provider="ollama",
                reason=f"Headroom improved to ELEVATED ({telemetry.headroom_mb:.1f} MiB) after Stage 2 shed.",
            )
        return VramInferenceProfile(
            tier=VramPressureTier.CRITICAL,
            headroom_mb=telemetry.headroom_mb,
            num_ctx=2048,
            num_gpu=12,
            allow_gpu=True,
            cloud_failover=cloud_fallback_enabled,
            recommended_provider=preferred_cloud_provider if cloud_fallback_enabled else "ollama",
            reason=f"Critical VRAM pressure ({telemetry.headroom_mb:.1f} MiB); GPU offload bounded to 12 layers, context clamped to 2048.",
        )

    # Pressure Tier OOM_GUARD: < 350 MiB
    execute_staged_shedding(stage=2)
    telemetry = get_vram_telemetry()
    if telemetry.pressure_tier == VramPressureTier.NOMINAL:
        return VramInferenceProfile(
            tier=VramPressureTier.NOMINAL,
            headroom_mb=telemetry.headroom_mb,
            num_ctx=8192,
            num_gpu=-1,
            allow_gpu=True,
            cloud_failover=False,
            recommended_provider="ollama",
            reason=f"Headroom restored to NOMINAL ({telemetry.headroom_mb:.1f} MiB) after emergency Stage 2 shed.",
        )
    return VramInferenceProfile(
        tier=VramPressureTier.OOM_GUARD,
        headroom_mb=telemetry.headroom_mb,
        num_ctx=2048,
        num_gpu=0,
        allow_gpu=False,
        cloud_failover=cloud_fallback_enabled,
        recommended_provider=preferred_cloud_provider if cloud_fallback_enabled else "cpu",
        reason=f"OOM Guard active ({telemetry.headroom_mb:.1f} MiB); GPU offload denied (0 layers), routing to {preferred_cloud_provider if cloud_fallback_enabled else 'cpu'}.",
    )


def register_ollama_vram_shed_hook(
    base_url: str = "http://127.0.0.1:11434/v1",
    model: str = "qwen3.5:2b-q4_K_M",
) -> None:
    """Registers an emergency Stage 2 shedding hook with nvml_dxgi_vram_arbiter to unload Ollama models."""
    from jarvis.core.providers.ollama import OllamaProvider

    def _unload_hook() -> str:
        ok = OllamaProvider.unload_model(base_url=base_url, model=model)
        return "unloaded" if ok else "failed_or_offline"

    register_vram_shed_hook("ollama_local", _unload_hook)
    logger.debug("Registered Ollama local VRAM shedding hook (%s, %s)", base_url, model)


__all__ = [
    "VramInferenceProfile",
    "VramInferenceTier",
    "register_ollama_vram_shed_hook",
    "resolve_vram_inference_profile",
]
