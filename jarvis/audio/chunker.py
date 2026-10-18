"""Backward-compatible re-export shim for audio chunking.
Canonical implementation resides in jarvis.audio.prosodic_chunker.
"""
from jarvis.audio.prosodic_chunker import (
    sanitize_clause_for_ear,
    TwoGearProsodicChunker,
    StreamingSentenceChunker,
)

__all__ = [
    "sanitize_clause_for_ear",
    "TwoGearProsodicChunker",
    "StreamingSentenceChunker",
]
