"""Pattern-Aware Speculative Tool Execution (PASTE / TomasuLLM) Engine for Jarvis 2.0.

Overlaps STT streaming and LLM token generation with background execution of candidate
read-only skills, driving perceived tool execution latency to 0ms with strict TOCTOU
dual-stamp invalidation.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.context.flow_state_mirror import ResourceScope
from jarvis.core.thread_affinity import init_speculative_worker_thread
from jarvis.skills.base import SkillMetadata, SkillRegistry, SpeculationForbiddenError

logger = logging.getLogger("Jarvis.Speculative.Engine")


@dataclass(slots=True, frozen=True)
class EpochVector:
    fs_usn_epoch: int = 0
    git_head_crc: int = 0
    terminal_seq: int = 0
    active_hwnd: int = 0

    def is_compatible_with(self, current: EpochVector) -> bool:
        return (
            self.fs_usn_epoch == current.fs_usn_epoch
            and self.git_head_crc == current.git_head_crc
            and self.terminal_seq == current.terminal_seq
            and self.active_hwnd == current.active_hwnd
        )


class EnvironmentEpochTracker:
    __slots__ = ("_active_hwnd", "_fs_usn_epoch", "_git_head_crc", "_terminal_seq")

    _instance: EnvironmentEpochTracker | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._fs_usn_epoch: int = 0
        self._git_head_crc: int = 0
        self._terminal_seq: int = 0
        self._active_hwnd: int = 0

    @classmethod
    def get_instance(cls) -> EnvironmentEpochTracker:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def bump_fs_epoch(self, usn: int | None = None) -> None:
        self._fs_usn_epoch = int(usn) if usn is not None else (self._fs_usn_epoch + 1)
        try:
            from jarvis.context.flow_state_mirror import (
                EnvironmentEpochTracker as FlowEpochTracker,
            )
            from jarvis.context.flow_state_mirror import ResourceScope
            with FlowEpochTracker._lock:
                FlowEpochTracker._epochs[ResourceScope.FILESYSTEM] = FlowEpochTracker._epochs.get(ResourceScope.FILESYSTEM, 0) + 1
        except Exception:
            pass

    def update_git_head(self, head_ref: str) -> None:
        digest = hashlib.blake2b(head_ref.encode("utf-8"), digest_size=4).digest()
        self._git_head_crc = int.from_bytes(digest, "little")
        try:
            from jarvis.context.flow_state_mirror import (
                EnvironmentEpochTracker as FlowEpochTracker,
            )
            from jarvis.context.flow_state_mirror import ResourceScope
            with FlowEpochTracker._lock:
                FlowEpochTracker._epochs[ResourceScope.GIT] = FlowEpochTracker._epochs.get(ResourceScope.GIT, 0) + 1
        except Exception:
            pass

    def bump_terminal_seq(self) -> None:
        self._terminal_seq += 1
        try:
            from jarvis.context.flow_state_mirror import (
                EnvironmentEpochTracker as FlowEpochTracker,
            )
            from jarvis.context.flow_state_mirror import ResourceScope
            with FlowEpochTracker._lock:
                FlowEpochTracker._epochs[ResourceScope.TERMINAL] = FlowEpochTracker._epochs.get(ResourceScope.TERMINAL, 0) + 1
        except Exception:
            pass

    def set_active_hwnd(self, hwnd: int) -> None:
        self._active_hwnd = int(hwnd)

    def snapshot(self) -> EpochVector:
        return EpochVector(
            fs_usn_epoch=self._fs_usn_epoch,
            git_head_crc=self._git_head_crc,
            terminal_seq=self._terminal_seq,
            active_hwnd=self._active_hwnd,
        )

    def reset_instance(self) -> None:
        self._fs_usn_epoch = 0
        self._git_head_crc = 0
        self._terminal_seq = 0
        self._active_hwnd = 0

    @classmethod
    def bump(cls, scope: Any) -> int:
        from jarvis.context.flow_state_mirror import (
            EnvironmentEpochTracker as FlowEpochTracker,
        )
        return FlowEpochTracker.bump(scope)

    @classmethod
    def get_epoch(cls, scope: Any) -> int:
        from jarvis.context.flow_state_mirror import (
            EnvironmentEpochTracker as FlowEpochTracker,
        )
        return FlowEpochTracker.get_epoch(scope)

    @classmethod
    def get_snapshot(cls) -> dict[Any, int]:
        from jarvis.context.flow_state_mirror import (
            EnvironmentEpochTracker as FlowEpochTracker,
        )
        return FlowEpochTracker.get_snapshot()

    @classmethod
    def reset(cls) -> None:
        from jarvis.context.flow_state_mirror import (
            EnvironmentEpochTracker as FlowEpochTracker,
        )
        FlowEpochTracker.reset()


@dataclass(frozen=True)
class FileStatStamp:
    """Captured filesystem stat stamp for TOCTOU validation."""
    path: str
    mtime_ns: int
    size_bytes: int


@dataclass(frozen=True)
class ValidationSnapshot:
    """Dual-stamp snapshot of environment epochs and tracked file stats."""
    scope_epochs: dict[ResourceScope, int]
    file_stamps: tuple[FileStatStamp, ...]
    epoch_vector: EpochVector | None = None


@dataclass
class SpeculativeEntry:
    """In-flight or completed speculative task state."""
    cache_key: str
    future: Future
    snapshot: ValidationSnapshot
    meta: SkillMetadata
    bound_args: dict[str, Any]
    submit_monotonic_ns: int = field(default_factory=time.monotonic_ns)


@dataclass
class ExecutionMetrics:
    """Telemetry tracking speculative tool hit rates and saved wall-clock latency."""
    speculative_launches: int = 0
    cache_hits_completed: int = 0
    cache_hits_inflight_coalesced: int = 0
    stale_invalidations: int = 0
    unclaimed_cancellations: int = 0
    saved_wall_clock_ms: float = 0.0


def compute_deterministic_cache_key(skill_name: str, bound_args: dict[str, Any]) -> str:
    """Computes BLAKE2b-128(skill_name || canonical_json(bound_args))."""
    def _json_serializable(obj):
        if isinstance(obj, (int, float, str, bool)) or obj is None:
            return obj
        if isinstance(obj, (list, tuple)):
            return [_json_serializable(x) for x in obj]
        if isinstance(obj, dict):
            return {str(k): _json_serializable(v) for k, v in sorted(obj.items())}
        if isinstance(obj, (Path, os.PathLike)):
            return str(obj)
        return repr(obj)

    canonical_dict = {k: _json_serializable(v) for k, v in sorted(bound_args.items())}
    canonical_payload = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    key_input = f"{skill_name}:{canonical_payload}".encode()
    return hashlib.blake2b(key_input, digest_size=16).hexdigest()


class SpeculativeExecutionEngine:
    """Autonomous speculative execution coordinator managing E-core thread pool and cache."""

    _instance: SpeculativeExecutionEngine | None = None
    _lock = threading.Lock()

    def __init__(self, max_workers: int = 4, epoch_tracker: EnvironmentEpochTracker | None = None):
        self.max_workers = max_workers
        self._epoch_tracker = epoch_tracker or EnvironmentEpochTracker.get_instance()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="SpeculativeWorker",
            initializer=init_speculative_worker_thread,
        )
        self._active_entries: dict[str, SpeculativeEntry] = {}
        self._entries_lock = threading.Lock()
        self.metrics = ExecutionMetrics()

    @property
    def epoch_tracker(self) -> EnvironmentEpochTracker:
        return self._epoch_tracker

    @classmethod
    def get_instance(cls) -> SpeculativeExecutionEngine:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _capture_validation_snapshot(self, meta: SkillMetadata, bound_args: dict[str, Any]) -> ValidationSnapshot:
        scope_epochs = EnvironmentEpochTracker.get_snapshot()
        epoch_vector = self._epoch_tracker.snapshot()
        file_stamps: list[FileStatStamp] = []

        for arg_name in meta.path_arg_names:
            val = bound_args.get(arg_name)
            if val and isinstance(val, (str, Path)):
                p = Path(val)
                if p.exists():
                    try:
                        st = os.stat(p)
                        file_stamps.append(FileStatStamp(
                            path=str(p.resolve()),
                            mtime_ns=getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
                            size_bytes=st.st_size,
                        ))
                    except Exception:
                        pass
        return ValidationSnapshot(
            scope_epochs=scope_epochs,
            file_stamps=tuple(file_stamps),
            epoch_vector=epoch_vector,
        )

    def _is_snapshot_valid(self, entry: SpeculativeEntry) -> bool:
        meta = entry.meta
        snap = entry.snapshot

        # 1. Verify resource scope epochs
        current_epochs = EnvironmentEpochTracker.get_snapshot()
        for scope_flag in ResourceScope:
            if scope_flag != ResourceScope.NONE and (meta.scopes & scope_flag):
                if current_epochs.get(scope_flag, 0) != snap.scope_epochs.get(scope_flag, 0):
                    logger.debug("Speculative cache invalidated: scope %s changed (%d -> %d)",
                                 scope_flag, snap.scope_epochs.get(scope_flag, 0), current_epochs.get(scope_flag, 0))
                    return False

        # 2. Verify 4-dimensional EpochVector TOCTOU compatibility
        if snap.epoch_vector is not None:
            current_vector = self._epoch_tracker.snapshot()
            if not snap.epoch_vector.is_compatible_with(current_vector):
                logger.debug(
                    "Speculative cache invalidated: EpochVector mismatch (%s -> %s)",
                    snap.epoch_vector,
                    current_vector,
                )
                return False

        # 3. Verify file stat stamps
        for stamp in snap.file_stamps:
            try:
                p = Path(stamp.path)
                if not p.exists():
                    return False
                st = os.stat(p)
                current_mtime = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
                if current_mtime != stamp.mtime_ns or st.st_size != stamp.size_bytes:
                    logger.debug("Speculative cache invalidated: file %s modified", stamp.path)
                    return False
            except Exception:
                return False

        return True

    def speculate(self, skill_name: str, *args, **kwargs) -> str:
        """Dispatches a candidate skill speculatively if marked read_only=True.
        
        Raises:
            SpeculationForbiddenError: if the target skill is not strictly read_only=True.
        """
        meta = SkillRegistry.get_metadata(skill_name)
        if meta is None or not meta.read_only:
            raise SpeculationForbiddenError(
                f"Speculative execution forbidden: skill '{skill_name}' is not registered as read_only=True"
            )

        sig = inspect.signature(meta.func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        bound_args = bound.arguments

        cache_key = compute_deterministic_cache_key(skill_name, bound_args)

        with self._entries_lock:
            existing = self._active_entries.get(cache_key)
            if existing is not None:
                if self._is_snapshot_valid(existing):
                    return cache_key
                else:
                    self._active_entries.pop(cache_key, None)

            snapshot = self._capture_validation_snapshot(meta, bound_args)
            future = self._executor.submit(meta.func, **bound_args)
            entry = SpeculativeEntry(
                cache_key=cache_key,
                future=future,
                snapshot=snapshot,
                meta=meta,
                bound_args=bound_args,
                submit_monotonic_ns=time.monotonic_ns(),
            )
            self._active_entries[cache_key] = entry
            self.metrics.speculative_launches += 1
            logger.debug("Speculative task dispatched: %s (key=%s)", skill_name, cache_key[:8])
            return cache_key

    def execute_or_claim(self, skill_name: str, *args, **kwargs) -> Any:
        """Claims a speculative in-flight or completed execution, or falls back to synchronous execution."""
        meta = SkillRegistry.get_metadata(skill_name)
        if meta is None:
            # Fall back to base registry
            return SkillRegistry.execute(skill_name, **kwargs)

        sig = inspect.signature(meta.func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        bound_args = bound.arguments

        if not meta.read_only:
            # Must run synchronously on calling thread
            return meta.func(**bound_args)

        cache_key = compute_deterministic_cache_key(skill_name, bound_args)

        with self._entries_lock:
            entry = self._active_entries.pop(cache_key, None)

        if entry is not None:
            # Check pre-wait snapshot validity
            if self._is_snapshot_valid(entry):
                try:
                    was_done = entry.future.done()
                    timeout_sec = meta.default_timeout_ms / 1000.0
                    result = entry.future.result(timeout=timeout_sec)

                    # Check post-wait snapshot validity
                    if self._is_snapshot_valid(entry):
                        saved_ms = (time.monotonic_ns() - entry.submit_monotonic_ns) / 1e6
                        self.metrics.saved_wall_clock_ms += saved_ms
                        if was_done:
                            self.metrics.cache_hits_completed += 1
                        else:
                            self.metrics.cache_hits_inflight_coalesced += 1
                        logger.debug("Speculative cache HIT: %s (saved %.2fms, completed=%s)",
                                     skill_name, saved_ms, was_done)
                        return result
                    else:
                        self.metrics.stale_invalidations += 1
                        logger.debug("Speculative result discarded: modified mid-flight (%s)", skill_name)
                except Exception as e:
                    logger.debug("Speculative execution encountered exception: %s", e)
            else:
                self.metrics.stale_invalidations += 1
                entry.future.cancel()

        # Cache miss, timeout, or invalidation fallback
        logger.debug("Speculative cache MISS/FALLBACK for %s; running synchronously", skill_name)
        return meta.func(**bound_args)

    def discard_unclaimed(self) -> int:
        """Cancels all active speculative futures and flushes the turn cache."""
        cancelled = 0
        with self._entries_lock:
            for entry in self._active_entries.values():
                if not entry.future.done():
                    if entry.future.cancel():
                        cancelled += 1
                    else:
                        cancelled += 1
            self.metrics.unclaimed_cancellations += len(self._active_entries)
            self._active_entries.clear()
        return cancelled

    def shutdown(self, wait: bool = False):
        self.discard_unclaimed()
        self._executor.shutdown(wait=wait)


__all__ = [
    "EnvironmentEpochTracker",
    "EpochVector",
    "ExecutionMetrics",
    "FileStatStamp",
    "SpeculativeEntry",
    "SpeculativeExecutionEngine",
    "ValidationSnapshot",
    "compute_deterministic_cache_key",
]
