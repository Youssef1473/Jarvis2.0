"""Two-Gear Prosodic Micro-Chunker for Sub-400ms First Audio Packet in Jarvis 2.0.

Provides dual-cadence phrase emission:
- Gear 1 (Chunk 0): Fast first-packet sprint emitting the initial prosodic phrase
  as soon as the buffer reaches 12-65 characters ending on punctuation or prosodic
  pivot words (and, but, so, because, while, for, to, in, on, with, sir), or hard-splitting
  at 48 characters on word boundaries to eliminate TTFT playback delay.
- Gear 2 (Chunk 1+): Sustained Bettany cadence emitting only on major clause or sentence
  boundaries (>= 24 chars) to prevent Kokoro fundamental frequency (F0) reset stutter.
"""
from __future__ import annotations

import re
from typing import List, Optional

_GEAR1_SPLIT_RE = re.compile(
    r"^(.{12,65}?(?:[,;:—\-\.\!\?]|\b(?:and|but|so|because|while|for|to|in|on|with|sir)\b))\s+(.*)$",
    re.DOTALL | re.IGNORECASE,
)
_GEAR2_SPLIT_RE = re.compile(
    r"^(.{24,}?(?:[\.\!\?\n]|[,;:—](?=\s+[A-Za-z])))\s+(.*)$",
    re.DOTALL,
)


class TwoGearProsodicChunker:
    """Incremental two-gear prosodic token chunker for neural TTS pipelining."""

    def __init__(self) -> None:
        self._buffer: str = ""
        self._chunks_emitted: int = 0

    def reset(self) -> None:
        """Resets the chunker buffer and resets gear counter to 0."""
        self._buffer = ""
        self._chunks_emitted = 0

    @property
    def chunks_emitted(self) -> int:
        """Returns the number of chunks emitted in the current turn."""
        return self._chunks_emitted

    @property
    def pending_text(self) -> str:
        """Returns un-emitted text currently residing in the buffer."""
        return self._buffer

    def is_empty(self) -> bool:
        """Returns True if the internal buffer contains no text."""
        return len(self._buffer.strip()) == 0

    def feed(self, text_delta: str) -> List[str]:
        """Ingests a text delta from the LLM stream and yields ready prosodic chunks."""
        if not text_delta:
            return []

        self._buffer += text_delta
        out: List[str] = []

        while True:
            pattern = _GEAR1_SPLIT_RE if self._chunks_emitted == 0 else _GEAR2_SPLIT_RE
            match = pattern.match(self._buffer)
            if match:
                chunk, remainder = match.group(1).strip(), match.group(2)
                if chunk:
                    out.append(chunk)
                    self._chunks_emitted += 1
                self._buffer = remainder
            elif self._chunks_emitted == 0 and len(self._buffer) >= 48 and " " in self._buffer[24:]:
                split_idx = self._buffer.rfind(" ", 0, 48)
                if split_idx <= 0:
                    break
                chunk = self._buffer[:split_idx].strip()
                self._buffer = self._buffer[split_idx + 1 :]
                if chunk:
                    out.append(chunk)
                    self._chunks_emitted += 1
            else:
                break

        return out

    def feed_tokens(self, tokens: List[str]) -> List[str]:
        """Feeds a sequence of tokens iteratively."""
        chunks: List[str] = []
        for token in tokens:
            chunks.extend(self.feed(token))
        return chunks

    def flush(self) -> List[str]:
        """Flushes and yields any remaining text at stream conclusion."""
        rem = self._buffer.strip()
        self.reset()
        return [rem] if rem else []


__all__ = ["TwoGearProsodicChunker"]
