"""Cognitive State Tracking, Behavioral Profiling, and Expected Value of Interruption (EVI) Engine for Jarvis 2.0.

Infers real-time cognitive state (DEEP_FLOW, ACTIVE_DEBUGGING, EXPLORATORY_RESEARCH, ROUTINE_ADMIN, FATIGUED_DRIFT)
and gates proactive notifications, spoken voice, and HUD pulses using formal decision utility theory.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("Jarvis.Cognition.EVI")


class CognitiveState(str, Enum):  # noqa: UP042
    DEEP_FLOW = "DEEP_FLOW"
    ACTIVE_DEBUGGING = "ACTIVE_DEBUGGING"
    EXPLORATORY_RESEARCH = "EXPLORATORY_RESEARCH"
    ROUTINE_ADMIN = "ROUTINE_ADMIN"
    FATIGUED_DRIFT = "FATIGUED_DRIFT"


class DeliveryModality(str, Enum):  # noqa: UP042
    SUPPRESS = "SUPPRESS"
    SILENT_ACT = "SILENT_ACT"
    HUD_PULSE = "HUD_PULSE"
    VOICE = "VOICE"


@dataclass
class TelemetrySlice:
    """Snapshot of user motor cadence, window focus, and session metrics."""
    kpm: float = 0.0                     # Keystrokes per minute
    backspace_ratio: float = 0.0         # Backspace-to-character ratio (0.0 to 1.0)
    window_switches_per_min: float = 0.0 # Focus switch frequency
    ide_focus_ratio: float = 0.0         # Fraction of time in IDE/editor/terminal (0.0 to 1.0)
    active_debug_signals: int = 0        # Non-zero exit codes or stack traces in last window
    continuous_session_mins: float = 0.0 # Minutes since last rest break
    speech_rate_syl_sec: float = 4.2     # Spoken syllables per second


class CognitiveTwinEngine:
    """Infers Sir's latent cognitive state and gates proactive actions via EVI."""

    # Disruption cost matrix C_interrupt(state, modality)
    INTERRUPTION_COST: dict[CognitiveState, dict[DeliveryModality, float]] = {
        CognitiveState.DEEP_FLOW: {
            DeliveryModality.VOICE: 0.92,
            DeliveryModality.HUD_PULSE: 0.18,
            DeliveryModality.SILENT_ACT: 0.02,
        },
        CognitiveState.ACTIVE_DEBUGGING: {
            DeliveryModality.VOICE: 0.65,
            DeliveryModality.HUD_PULSE: 0.10,
            DeliveryModality.SILENT_ACT: 0.01,
        },
        CognitiveState.EXPLORATORY_RESEARCH: {
            DeliveryModality.VOICE: 0.40,
            DeliveryModality.HUD_PULSE: 0.08,
            DeliveryModality.SILENT_ACT: 0.01,
        },
        CognitiveState.ROUTINE_ADMIN: {
            DeliveryModality.VOICE: 0.22,
            DeliveryModality.HUD_PULSE: 0.05,
            DeliveryModality.SILENT_ACT: 0.01,
        },
        CognitiveState.FATIGUED_DRIFT: {
            DeliveryModality.VOICE: 0.50,
            DeliveryModality.HUD_PULSE: 0.12,
            DeliveryModality.SILENT_ACT: 0.01,
        },
    }

    def __init__(self) -> None:
        self.state: CognitiveState = CognitiveState.ROUTINE_ADMIN
        self._interruption_history: list[tuple[float, bool]] = []  # (timestamp, was_dismissed)
        self.last_telemetry: TelemetrySlice = TelemetrySlice()

    def infer_state(self, t: TelemetrySlice) -> CognitiveState:
        """Determines latent cognitive state from telemetry signature."""
        self.last_telemetry = t
        if t.continuous_session_mins > 135 and (t.backspace_ratio > 0.17 or t.window_switches_per_min > 12):
            self.state = CognitiveState.FATIGUED_DRIFT
        elif t.active_debug_signals > 0 and t.ide_focus_ratio > 0.50:
            self.state = CognitiveState.ACTIVE_DEBUGGING
        elif t.ide_focus_ratio >= 0.82 and t.kpm >= 140 and t.backspace_ratio < 0.09 and t.window_switches_per_min <= 2.5:
            self.state = CognitiveState.DEEP_FLOW
        elif t.ide_focus_ratio < 0.40 and t.window_switches_per_min >= 4.0:
            self.state = CognitiveState.EXPLORATORY_RESEARCH
        else:
            self.state = CognitiveState.ROUTINE_ADMIN
        return self.state

    def compute_fatigue_penalty(self, now: float) -> float:
        """Computes exponentially decaying interruption fatigue F_fatigue(t) with tau=45 min."""
        tau = 2700.0  # 45 minutes in seconds
        self._interruption_history = [(ts, dis) for ts, dis in self._interruption_history if now - ts < 7200]
        penalty = 0.0
        for ts, was_dismissed in self._interruption_history:
            weight = 3.0 if was_dismissed else 1.0
            penalty += weight * math.exp(-(now - ts) / tau)
        return min(0.85, 0.12 * penalty)

    def evaluate_proactive_action(
        self,
        intrinsic_utility: float,
        context_relevance: float,
        urgency: float,
        can_act_silently: bool = False,
    ) -> tuple[DeliveryModality, float]:
        """Evaluates EVI(a | c, s) = U_info * P_rel * (1 + gamma * Urgency) - C_interrupt(s, m) - beta * F_fatigue.

        Returns:
            Tuple of (DeliveryModality, EVI_score).
        """
        now = time.time()
        benefit = intrinsic_utility * context_relevance * (1.0 + 0.55 * urgency)
        fatigue = self.compute_fatigue_penalty(now)
        costs = self.INTERRUPTION_COST.get(self.state, self.INTERRUPTION_COST[CognitiveState.ROUTINE_ADMIN])

        evi_voice = benefit - costs[DeliveryModality.VOICE] - fatigue
        evi_hud = benefit - costs[DeliveryModality.HUD_PULSE] - (0.3 * fatigue)

        if evi_voice >= 0.80:
            self._interruption_history.append((now, False))
            return DeliveryModality.VOICE, evi_voice
        if evi_hud >= 0.35:
            return DeliveryModality.HUD_PULSE, evi_hud
        if can_act_silently and benefit >= 0.25:
            return DeliveryModality.SILENT_ACT, benefit - costs[DeliveryModality.SILENT_ACT]
        return DeliveryModality.SUPPRESS, max(evi_voice, evi_hud)

    def record_dismissal(self) -> None:
        """Records when user dismisses or silences a proactive notification."""
        now = time.time()
        self._interruption_history.append((now, True))

    def build_persona_telemetry_envelope(self, t: TelemetrySlice | None = None) -> str:
        """Generates the compact <live_telemetry_envelope> injected at the tail of the user message.
        Synchronizes J.A.R.V.I.S.'s cadence, brevity, and Socratic behavior with Sir's live state.
        """
        slice_obj = t or self.last_telemetry
        tempo_tag = "urgent/rapid" if slice_obj.speech_rate_syl_sec >= 5.4 else (
            "slow/fatigued" if slice_obj.speech_rate_syl_sec <= 3.3 else "composed"
        )

        state_directives = {
            CognitiveState.DEEP_FLOW: (
                "Sir is in deep engineering flow. Zero preamble. Max 1-2 crisp sentences. "
                "Zero small talk, zero Socratic delay—deliver the direct answer or execute immediately."
            ),
            CognitiveState.ACTIVE_DEBUGGING: (
                "Sir is in ACTIVE_DEBUGGING. Lead Sentence 1 with the exact root cause or diagnosis; "
                "Lead Sentence 2 with the surgical fix. Calm, steady British composure under fire."
            ),
            CognitiveState.EXPLORATORY_RESEARCH: (
                "Sir is in EXPLORATORY_RESEARCH. Engage as an intellectual co-designer with full dry British wit. "
                "Decompose complex ideas into first principles and offer crisp A/B architectural trade-offs."
            ),
            CognitiveState.ROUTINE_ADMIN: (
                "Standard operational cadence. Deliver concise, effortless British poise in 1 to 2 sentences."
            ),
            CognitiveState.FATIGUED_DRIFT: (
                "Extended session duration / fatigue indicators detected. Keep responses under 2 sentences, "
                "minimize cognitive load, default to safe autonomous choices, and wryly note the late hour if appropriate."
            ),
        }

        directive = state_directives.get(self.state, state_directives[CognitiveState.ROUTINE_ADMIN])
        if tempo_tag == "urgent/rapid" and self.state != CognitiveState.DEEP_FLOW:
            directive += " Vocal tempo is rapid—front-load the conclusion in the very first clause."

        return (
            f'<live_telemetry_envelope state="{self.state.value}" '
            f'kpm="{int(slice_obj.kpm)}" backspace_ratio="{slice_obj.backspace_ratio:.2f}" '
            f'session_mins="{int(slice_obj.continuous_session_mins)}" '
            f'speech_rate="{slice_obj.speech_rate_syl_sec:.1f}" vocal_tempo="{tempo_tag}">\n'
            f"  Directive: {directive}\n"
            f"</live_telemetry_envelope>"
        )

    def evaluate_speculative_prewarm(
        self,
        utility: float = 0.5,
        relevance: float = 0.6,
        epoch_vector: Any | None = None,
        current_vector: Any | None = None,
    ) -> tuple[bool, float]:
        """Evaluates whether to speculatively pre-warm read-only context under Option A.
        Returns (is_approved, score). Disapproves if epochs are incompatible or benefit too low.
        """
        if epoch_vector is not None and current_vector is not None:
            if hasattr(epoch_vector, "is_compatible_with"):
                if not epoch_vector.is_compatible_with(current_vector):
                    return False, 0.0
        benefit = utility * relevance
        costs = self.INTERRUPTION_COST.get(self.state, self.INTERRUPTION_COST[CognitiveState.ROUTINE_ADMIN])
        silent_cost = costs.get(DeliveryModality.SILENT_ACT, 0.01)
        net_score = benefit - silent_cost
        is_approved = benefit >= 0.25
        return is_approved, net_score

    def should_prewarm_context(
        self,
        utility: float = 0.5,
        relevance: float = 0.6,
        epoch_vector: Any | None = None,
        current_vector: Any | None = None,
    ) -> bool:
        """Convenience predicate for TOCTOU-guarded read-only context pre-warming."""
        approved, _ = self.evaluate_speculative_prewarm(
            utility=utility,
            relevance=relevance,
            epoch_vector=epoch_vector,
            current_vector=current_vector,
        )
        return approved


@dataclass
class PrewarmedContextEntry:
    """Represents a speculatively pre-warmed context object with TOCTOU epoch vector validation."""
    context_id: str
    data: Any
    epoch_vector: Any
    created_at: float = field(default_factory=time.time)

    def is_valid(self, current_vector: Any) -> bool:
        if hasattr(self.epoch_vector, "is_compatible_with"):
            return self.epoch_vector.is_compatible_with(current_vector)
        return True


__all__ = [
    "CognitiveState",
    "CognitiveTwinEngine",
    "DeliveryModality",
    "PrewarmedContextEntry",
    "TelemetrySlice",
]


