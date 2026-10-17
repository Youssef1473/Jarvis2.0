"""Temperature-Scaled Energy & Margin Routing Cascade for Jarvis 2.0.

Implements the 3-Stage Deterministic Routing Policy:
- Tier 0: Fast Reflex Engine (<0.15ms regex/habit match)
- Tier 1: Direct Local Skill Execution (~0.55ms, 4-gate verification)
- Tier 2: Cloud LLM fallback with speculative pre-warm band (P >= 0.30)
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional

from jarvis.context.flow_state_mirror import FlowStateMirror
from jarvis.core.reflex_engine import DynamicReflexEngine
from jarvis.routing.onnx_intent_router import IntentPrediction, ONNXIntentRouter
from jarvis.skills.base import SkillRegistry
from jarvis.speculative.engine import SpeculativeExecutionEngine

logger = logging.getLogger("Jarvis.Routing.Cascade")

P1_GATE = 0.92
MARGIN_GATE = 0.25
ENERGY_GATE = -1.0
SLOT_CONFIDENCE_GATE = 0.88
SPECULATIVE_PREWARM_THRESHOLD = 0.30


@dataclass
class RoutingDecision:
    """Outcome of the 3-stage cascade evaluation."""
    tier: int
    skill_name: Optional[str]
    kwargs: Dict[str, Any] = field(default_factory=dict)
    execute_locally: bool = False
    prediction: Optional[IntentPrediction] = None
    speculative_keys: List[str] = field(default_factory=list)


class RoutingCascade:
    """Coordinates Tier 0, Tier 1, and Tier 2 routing with speculative pre-warming."""

    def __init__(
        self,
        reflex_engine: Optional[DynamicReflexEngine] = None,
        intent_router: Optional[ONNXIntentRouter] = None,
        speculative_engine: Optional[SpeculativeExecutionEngine] = None,
    ):
        self.reflex_engine = reflex_engine or DynamicReflexEngine()
        self.intent_router = intent_router or ONNXIntentRouter.get_instance()
        self.speculative_engine = speculative_engine or SpeculativeExecutionEngine.get_instance()

    def route_query(self, query: str, candidate_skills: Optional[List[str]] = None) -> RoutingDecision:
        """Evaluates query through the 3-stage deterministic routing pipeline."""
        # =========================================================================
        # Stage 0: Fast Reflex Engine (<= 0.15ms)
        # =========================================================================
        habit = self.reflex_engine.check_habit(query)
        if habit:
            skill = habit.get("skill_name")
            kwargs = habit.get("skill_kwargs", {})
            logger.debug("Cascade Tier 0 Hit: %s -> %s", query, skill)
            return RoutingDecision(
                tier=0,
                skill_name=skill,
                kwargs=kwargs,
                execute_locally=True,
            )

        # =========================================================================
        # Stage 1: Sub-Millisecond ONNX Edge Intent Router (~0.55ms)
        # =========================================================================
        prediction = self.intent_router.route(query, candidate_skills=candidate_skills)

        min_slot_conf = min(prediction.slot_confidences.values()) if prediction.slot_confidences else 1.0

        tier1_passed = (
            prediction.top1_prob >= P1_GATE and
            prediction.margin >= MARGIN_GATE and
            prediction.energy <= ENERGY_GATE and
            min_slot_conf >= SLOT_CONFIDENCE_GATE
        )

        if tier1_passed:
            logger.debug(
                "Cascade Tier 1 Hit: %s (p1=%.3f, margin=%.3f, energy=%.3f)",
                prediction.intent, prediction.top1_prob, prediction.margin, prediction.energy
            )
            return RoutingDecision(
                tier=1,
                skill_name=prediction.intent,
                kwargs=prediction.slots,
                execute_locally=True,
                prediction=prediction,
            )

        # =========================================================================
        # Stage 2: Cloud LLM Fallback + Speculative Pre-Warm Band (P >= 0.30)
        # =========================================================================
        speculative_keys: List[str] = []
        flow_snap = FlowStateMirror.get_instance().get_snapshot()

        # Select candidate read-only skills with P(y=k|x) >= 0.30
        candidates = []
        for skill, prob in prediction.calibrated_probs.items():
            if prob >= SPECULATIVE_PREWARM_THRESHOLD:
                meta = SkillRegistry.get_metadata(skill)
                if meta and meta.read_only:
                    candidates.append((skill, prob, meta))
        candidates.sort(key=lambda x: x[1], reverse=True)

        for skill, prob, meta in candidates[:3]:
            bound_args = dict(prediction.slots)
            # Supply flow state defaults for common parameters
            if "filepath" not in bound_args and flow_snap.active_file_path:
                bound_args["filepath"] = flow_snap.active_file_path
                bound_args["file_path"] = flow_snap.active_file_path
            if "cursor_line" not in bound_args and flow_snap.cursor_line is not None:
                bound_args["cursor_line"] = flow_snap.cursor_line
                bound_args["line"] = flow_snap.cursor_line

            import inspect
            sig = inspect.signature(meta.func)
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not has_var_keyword:
                bound_args = {k: v for k, v in bound_args.items() if k in sig.parameters}

            try:
                key = self.speculative_engine.speculate(skill, **bound_args)
                speculative_keys.append(key)
                logger.debug("Cascade Tier 2 Pre-Warm launched for '%s' (P=%.2f, key=%s)",
                             skill, prob, key[:8])
            except Exception as e:
                logger.debug("Speculative pre-warm dispatch failed for '%s': %s", skill, e)

        return RoutingDecision(
            tier=2,
            skill_name=None,
            kwargs={},
            execute_locally=False,
            prediction=prediction,
            speculative_keys=speculative_keys,
        )
