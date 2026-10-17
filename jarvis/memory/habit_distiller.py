"""Autonomous Habit Distillation & Markov 3-Gram Workflow Detector for Jarvis 2.0.

Tracks operational skill executions and automatically identifies repeatable triples
executed >= 3 times within a session, proposing deterministic macro promotion.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from jarvis.core.reflex_engine import DynamicReflexEngine

logger = logging.getLogger("Jarvis.Memory.HabitDistiller")


@dataclass(frozen=True)
class ActionRecord:
    skill_name: str
    normalized_args: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class DistilledHabitProposal:
    macro_name: str
    trigger_suggestion: str
    steps: List[Tuple[str, str]]
    occurrence_count: int
    proposal_text: str


class AutonomousHabitDistiller:
    """Detects recurring 3-gram workflow sequences and synthesizes reflex macro proposals."""

    def __init__(
        self,
        window_size: int = 100,
        repetition_threshold: int = 3,
        reflex_engine: Optional[DynamicReflexEngine] = None,
    ):
        self.window_size = window_size
        self.repetition_threshold = repetition_threshold
        self.reflex_engine = reflex_engine or DynamicReflexEngine()
        self._history: Deque[ActionRecord] = deque(maxlen=window_size)
        self._trigram_counts: Dict[Tuple[str, str, str], int] = {}
        self._proposed_macros: set[str] = set()
        self._lock = threading.Lock()

    def record_action(self, skill_name: str, kwargs: Optional[Dict[str, Any]] = None) -> Optional[DistilledHabitProposal]:
        """Records an executed skill and checks if a 3-gram sequence hits the repetition threshold."""
        # Strip transient secrets or tokens from args before hashing
        clean_kwargs = dict(kwargs or {})
        for secret_key in ("api_key", "password", "token", "secret", "auth"):
            clean_kwargs.pop(secret_key, None)

        norm_args = json.dumps(clean_kwargs, sort_keys=True)
        record = ActionRecord(skill_name=skill_name, normalized_args=norm_args)

        with self._lock:
            self._history.append(record)

            if len(self._history) < 3:
                return None

            # Inspect the latest 3-gram
            triplet = (
                self._history[-3].skill_name,
                self._history[-2].skill_name,
                self._history[-1].skill_name,
            )

            # Ignore self-loops of same tool 3 times
            if triplet[0] == triplet[1] == triplet[2]:
                return None

            count = self._trigram_counts.get(triplet, 0) + 1
            self._trigram_counts[triplet] = count

            if count >= self.repetition_threshold:
                macro_key = f"{triplet[0]}_{triplet[1]}_{triplet[2]}"
                if macro_key not in self._proposed_macros:
                    self._proposed_macros.add(macro_key)
                    proposal = self._synthesize_proposal(triplet, count)
                    logger.info("Habit Distiller detected recurring 3-gram workflow: %s (count=%d)",
                                macro_key, count)
                    return proposal

        return None

    def _synthesize_proposal(self, triplet: Tuple[str, str, str], count: int) -> DistilledHabitProposal:
        macro_name = f"macro_{triplet[0]}_{triplet[1]}_{triplet[2]}"
        trigger = f"run {triplet[0]} and {triplet[2]}"

        steps = [
            (self._history[-3].skill_name, self._history[-3].normalized_args),
            (self._history[-2].skill_name, self._history[-2].normalized_args),
            (self._history[-1].skill_name, self._history[-1].normalized_args),
        ]

        text = (
            f"Sir, you have executed the workflow sequence ({triplet[0]} -> {triplet[1]} -> {triplet[2]}) "
            f"{count} times this session. Would you like me to register a single reflex macro for it?"
        )

        return DistilledHabitProposal(
            macro_name=macro_name,
            trigger_suggestion=trigger,
            steps=steps,
            occurrence_count=count,
            proposal_text=text,
        )

    def reset(self):
        with self._lock:
            self._history.clear()
            self._trigram_counts.clear()
            self._proposed_macros.clear()
