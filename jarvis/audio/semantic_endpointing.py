"""Semantic Endpointing Engine integrating VAD acoustic silence tracking 
with atomic partial token consumption over Shared Memory.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncGenerator, Optional, Tuple

from jarvis.audio.partial_streamer import ShmPartialTokenChannel

logger = logging.getLogger("Jarvis.Audio.SemanticEndpointing")


class SemanticVADCoordinator:
    """Dynamically modulates VAD trailing silence thresholds based on partial syntax."""

    def __init__(self, channel_name: Optional[str] = None) -> None:
        self.base_threshold_ms = 500
        self.aggressive_cutoff_ms = 150
        self.contemplation_padding_ms = 850
        self.hard_acoustic_floor_ms = 1200

        target_name = channel_name or "JarvisWhisperPartialShm"
        try:
            self.channel: Optional[ShmPartialTokenChannel] = ShmPartialTokenChannel(
                name=target_name,
                create=False,
            )
        except Exception:
            # Fallback to create mode if channel doesn't exist yet (e.g. unit tests or standalone)
            try:
                self.channel = ShmPartialTokenChannel(
                    name=target_name,
                    create=True,
                )
            except Exception:
                self.channel = None

        # Hanging clauses: Prepositions, conjunctions, articles, copulas
        self.hanging_patterns = re.compile(
            r"\b(and|or|but|because|if|then|for|with|about|to|the|a|an|is|are|was|were|so|that|of)\s*$",
            re.IGNORECASE,
        )

        # Terminal WH-questions
        self.terminal_wh_patterns = re.compile(
            r"(.+)(what|why|how|when|where|who)\s*$",
            re.IGNORECASE,
        )

    def evaluate_syntactic_completeness(self, partial_transcript: str) -> int:
        """Determines dynamic trailing silence threshold from the partial transcript."""
        cleaned = partial_transcript.strip()
        if not cleaned:
            return self.base_threshold_ms

        if self.hanging_patterns.search(cleaned):
            return self.contemplation_padding_ms

        if cleaned[-1] in {".", "?", "!"}:
            return self.aggressive_cutoff_ms

        if self.terminal_wh_patterns.search(cleaned):
            return self.aggressive_cutoff_ms

        # Word count completion heuristic for standalone imperatives/confirmations
        tokens = cleaned.split()
        if len(tokens) >= 2 and tokens[0].lower() in {"jarvis", "system"}:
            if tokens[-1].lower() in {"run", "stop", "cancel", "status", "clear", "execute"}:
                return self.aggressive_cutoff_ms

        # Command imperatives without terminal punctuation (e.g. "Execute tests", "Run diagnostics")
        if len(tokens) >= 2 and tokens[0].lower() in {
            "execute", "run", "open", "close", "start", "stop", "deploy", "build", "kill", "cancel"
        }:
            return self.aggressive_cutoff_ms

        return self.base_threshold_ms

    async def monitor_utterance(
        self,
        vad_silence_stream: AsyncGenerator[float, None],
        on_speculative_tripwire: Optional[asyncio.Event] = None,
    ) -> Tuple[str, bool]:
        """Monitors acoustic silence and shared memory partial tokens concurrently.

        Triggers on_speculative_tripwire the moment the threshold condition is met.
        """
        current_partial = ""
        current_threshold_ms = self.base_threshold_ms
        tripwire_fired = False

        async for silence_duration_ms in vad_silence_stream:
            # Poll shared memory slot (<0.5µs atomic read)
            channel_state = self.channel.read_latest() if self.channel is not None else None
            if channel_state is not None:
                current_partial, is_final = channel_state
                current_threshold_ms = self.evaluate_syntactic_completeness(current_partial)
                if is_final:
                    return current_partial, True

            # Evaluate endpoint conditions
            is_endpoint_reached = (
                silence_duration_ms >= current_threshold_ms
                or silence_duration_ms >= self.hard_acoustic_floor_ms
            )

            if is_endpoint_reached:
                if not tripwire_fired and on_speculative_tripwire is not None:
                    on_speculative_tripwire.set()
                    tripwire_fired = True

                logger.debug(
                    "Semantic endpoint tripped at %.1fms silence (Threshold: %dms, Text: '%s')",
                    silence_duration_ms,
                    current_threshold_ms,
                    current_partial,
                )
                return current_partial, False

        return current_partial, False

    def close(self) -> None:
        """Releases the shared memory IPC mapping."""
        if self.channel is not None:
            self.channel.close()
            self.channel = None

    def __del__(self) -> None:
        self.close()
