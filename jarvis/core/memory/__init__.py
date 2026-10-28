"""Long-term persistent memory management and adaptive self-learning for Jarvis 2.0.

Decomposed into focused modular mixins:
- SqliteBaseMixin: SQLite WAL connection management, single-writer serialization, schema init
- TemporalGraphMixin: Bi-temporal knowledge graph edges, Markov habit transitions, undo journal
- FactsManagerMixin: Atomic facts, core user preferences, hybrid fact search
- TasksCalendarMixin: Tasks, reminders, calendar lifecycle, schedule archives
- DocumentVaultMixin: Document staging, JIT archival, hybrid retrieval, chunk sandwiching
- InteractionMemoryMixin: Conversation logging, context auto-learning, episodic lessons
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from jarvis.core.memory.document_vault import DocumentVaultMixin
from jarvis.core.memory.facts_manager import FactsManagerMixin
from jarvis.core.memory.interaction_memory import InteractionMemoryMixin
from jarvis.core.memory.sqlite_base import SingleWriterActor, SqliteBaseMixin
from jarvis.core.memory.tasks_calendar import TasksCalendarMixin
from jarvis.core.memory.temporal_graph import TemporalGraphMixin
from jarvis.core.memory.vector_engine import (
    VectorMemoryEngine,
    fuse_vec_and_fts_rrf,
    sanitize_fts5_query,
    sanitize_memory_taint,
)

from .obsidian_sync import (
    ObsidianChunk,
    ObsidianContextSyncEngine,
    ObsidianNoteParser,
    SyncStats,
)
from .sleep_consolidator import SleepPhaseConsolidator
from .transcript_distiller import TranscriptDistiller

logger = logging.getLogger("Jarvis.Memory")


class MemoryManager(
    SqliteBaseMixin,
    TemporalGraphMixin,
    FactsManagerMixin,
    TasksCalendarMixin,
    DocumentVaultMixin,
    InteractionMemoryMixin,
):
    """Stores user preferences, facts, tasks, and conversation history in SQLite with adaptive learning."""

    def __init__(self, config: dict[str, Any]):
        self.llm_config = config.get("llm", {})
        memory_config = config.get("memory", {})
        self.background_learning_enabled = bool(memory_config.get("background_learning", False))
        self._learning_lock = threading.Lock()
        self._learning_in_flight = False
        self._context_summary_lock = threading.Lock()
        self._context_summary_cache: str | None = None
        self._write_lock = threading.RLock()
        base_dir = Path(config.get("_base_dir", "."))
        db_rel_path = memory_config.get("db_path", "data/jarvis_memory.db")
        self.db_path = base_dir / db_rel_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_engine = VectorMemoryEngine()
        self._writer_actor = SingleWriterActor(self.db_path)
        self._ro_vector_pool: Any | None = None
        self._ro_pool_lock = threading.Lock()
        self._init_db()


__all__ = [
    "MemoryManager",
    "ObsidianChunk",
    "ObsidianContextSyncEngine",
    "ObsidianNoteParser",
    "SingleWriterActor",
    "SleepPhaseConsolidator",
    "SyncStats",
    "TranscriptDistiller",
    "VectorMemoryEngine",
    "fuse_vec_and_fts_rrf",
    "sanitize_fts5_query",
    "sanitize_memory_taint",
]
