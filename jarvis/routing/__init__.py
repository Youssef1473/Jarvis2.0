"""Routing package for edge intent classification, cascade dispatch, and VRAM-aware profiles."""
from jarvis.routing.cascade import RoutingCascade, RoutingDecision
from jarvis.routing.onnx_intent_router import IntentPrediction, ONNXIntentRouter
from jarvis.routing.vram_profile import (
    VramInferenceProfile,
    VramInferenceTier,
    register_ollama_vram_shed_hook,
    resolve_vram_inference_profile,
)

__all__ = [
    "IntentPrediction",
    "ONNXIntentRouter",
    "RoutingCascade",
    "RoutingDecision",
    "VramInferenceProfile",
    "VramInferenceTier",
    "register_ollama_vram_shed_hook",
    "resolve_vram_inference_profile",
]

