"""Backward-compatible re-export shim for audio chunking.
Canonical implementation resides in jarvis.audio.prosodic_chunker.
"""
from jarvis.audio.prosodic_chunker import (
    StreamingSentenceChunker,
    TwoGearProsodicChunker,
    sanitize_clause_for_ear,
)

__all__ = [
    "StreamingSentenceChunker",
    "TwoGearProsodicChunker",
    "sanitize_clause_for_ear",
]
