"""Temporal Habit & Predictive Event Graph Engine for Jarvis 2.0 (Argon Dimension 1.3).

Implements a SQLite-backed state-action transition model P(A_t | A_{t-1}, C_{t-1})
with Laplace-smoothed Bayesian habit prior inference, 120s causal horizon tracking,
prevented double-decay, and non-blocking asynchronous WAL writes.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.Cognition.HabitGraph")

# Causal Horizon: If elapsed time between actions exceeds 120.0s, reset sequence
CAUSAL_HORIZON_SECONDS = 120.0

# Habit half-life for exponential decay (14 days in seconds)
HABIT_HALF_LIFE_SECONDS = 14.0 * 86400.0


@dataclass
class HabitPrediction:
    """Represents a probabilistic next-action prediction from the habit graph."""
    target_action: str
    probability: float
    confidence: float
    transition_count: int
    is_proactive_candidate: bool
    context_used: str


class TemporalHabitGraph:
    """Probabilistic Markov transition graph modeling Sir's sequential desktop and tool habits.

    Mandatory Architectural Invariants:
    1. Prevent Double-Decay: Tracks last_decay_ts per row. Computes elapsed decay on upsert
       and during sleep consolidation without double-counting elapsed intervals.
    2. Centralized 120s Causal Horizon: Resets _last_action if time gap exceeds 120s so idle
       periods are never learned as transitions.
    3. Context Alignment (C_{t-1} vs C_t): Transitions are recorded under C_{t-1}, while
       next-action predictions query the candidate space under current context C_t.
    4. Sparse Weight Fallback: Falls back to marginal context prior whenever total context
       weight is < 2.0.
    5. Non-Blocking Writes: Dispatches DB mutations to a background worker queue so the
       Win32 event hook message pump is never stalled on disk I/O.
    """

    def __init__(self, db_path: str, sync_writes: bool = False) -> None:
        self.db_path = str(Path(db_path).resolve())
        self.sync_writes = sync_writes

        # Centralized sequence state
        self._last_action: Optional[str] = None
        self._last_context_key: Optional[str] = None
        self._last_action_ts: float = 0.0
        self._state_lock = threading.Lock()

        # Non-blocking write queue & background worker
        self._write_queue: queue.Queue[Optional[Tuple[str, str, str, float]]] = queue.Queue(maxsize=1000)
        self._worker_thread: Optional[threading.Thread] = None
        self._running = True

        self._init_db()

        if not self.sync_writes:
            self._worker_thread = threading.Thread(
                target=self._writer_loop,
                name="JarvisHabitGraphWriter",
                daemon=True,
            )
            self._worker_thread.start()

    def _get_connection(self) -> sqlite3.Connection:
        """Opens a connection to the SQLite database with WAL mode."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        """Initializes action_transitions schema with WITHOUT ROWID and last_decay_ts."""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS action_transitions (
                        source_action TEXT NOT NULL,
                        context_state TEXT NOT NULL,
                        target_action TEXT NOT NULL,
                        transition_count INTEGER NOT NULL DEFAULT 1,
                        last_transition_ts REAL NOT NULL,
                        last_decay_ts REAL NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        PRIMARY KEY (source_action, context_state, target_action)
                    ) WITHOUT ROWID;
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_transitions_source_context 
                    ON action_transitions(source_action, context_state);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_transitions_weight 
                    ON action_transitions(weight);
                """)
        finally:
            conn.close()

    @staticmethod
    def get_time_of_day_bin(hour: Optional[int] = None) -> str:
        """Bins the hour of day into diurnal workflow phases."""
        h = hour if hour is not None else datetime.datetime.now().hour
        if 6 <= h < 12:
            return "morning"
        elif 12 <= h < 18:
            return "afternoon"
        elif 18 <= h < 24:
            return "evening"
        return "night"

    @classmethod
    def get_context_key(
        cls,
        cognitive_state: str = "ROUTINE_ADMIN",
        domain: str = "general",
        hour: Optional[int] = None,
    ) -> str:
        """Generates the composite context key (e.g. 'DEEP_FLOW:afternoon:ide')."""
        time_bin = cls.get_time_of_day_bin(hour)
        clean_state = str(cognitive_state).upper()
        clean_domain = str(domain).lower()
        return f"{clean_state}:{time_bin}:{clean_domain}"

    def observe_action(
        self,
        action: str,
        cognitive_state: str = "ROUTINE_ADMIN",
        domain: str = "general",
        timestamp: Optional[float] = None,
    ) -> Optional[HabitPrediction]:
        """Central observation entrypoint for window focus and tool execution events.

        1. Enforces 120s causal horizon: resets sequence if gap > 120s.
        2. Inscribes transition under previous context C_{t-1}.
        3. Formulates current context C_t and queries predict_next_action(A_t, C_t).
        """
        now = timestamp if timestamp is not None else time.time()
        prediction: Optional[HabitPrediction] = None

        with self._state_lock:
            # 1. 120s Causal Horizon Check
            if self._last_action and (now - self._last_action_ts > CAUSAL_HORIZON_SECONDS):
                logger.debug(
                    "Habit causal horizon exceeded (%.1fs > 120s). Resetting sequence baseline.",
                    now - self._last_action_ts,
                )
                self._last_action = None
                self._last_context_key = None

            # 2. Record transition A_{t-1} -> A_t under context C_{t-1}
            if self._last_action and self._last_action != action:
                context_used = self._last_context_key or self.get_context_key(cognitive_state, domain)
                self._dispatch_record_transition(self._last_action, context_used, action, now)

            # 3. Formulate current context C_t
            current_context = self.get_context_key(cognitive_state, domain)

            # 4. Advance sequence pointers
            self._last_action = action
            self._last_context_key = current_context
            self._last_action_ts = now

            # 5. Predict next probable action A_{t+1} from current state (A_t, C_t)
            prediction = self.predict_next_action(action, current_context, now=now)

        return prediction

    def _dispatch_record_transition(
        self, source_action: str, context_state: str, target_action: str, now: float
    ) -> None:
        """Dispatches transition recording synchronously or non-blockingly via queue."""
        if self.sync_writes or not self._running:
            self._record_transition_sync(source_action, context_state, target_action, now)
        else:
            try:
                self._write_queue.put_nowait((source_action, context_state, target_action, now))
            except queue.Full:
                logger.debug("HabitGraph write queue full; dropping transition record.")

    def _record_transition_sync(
        self, source_action: str, context_state: str, target_action: str, now: float
    ) -> None:
        """Applies lazy decay and upserts the transition row in SQLite."""
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT transition_count, last_decay_ts, weight
                    FROM action_transitions
                    WHERE source_action = ? AND context_state = ? AND target_action = ?
                """, (source_action, context_state, target_action))
                row = cursor.fetchone()

                if row:
                    count, last_decay_ts, old_weight = row["transition_count"], row["last_decay_ts"], row["weight"]
                    # Apply lazy decay to current time before incrementing
                    elapsed = max(0.0, now - last_decay_ts)
                    decay_factor = 2.0 ** (-elapsed / HABIT_HALF_LIFE_SECONDS)
                    new_weight = (old_weight * decay_factor) + 1.0
                    new_count = count + 1

                    cursor.execute("""
                        UPDATE action_transitions
                        SET transition_count = ?, last_transition_ts = ?, last_decay_ts = ?, weight = ?
                        WHERE source_action = ? AND context_state = ? AND target_action = ?
                    """, (new_count, now, now, new_weight, source_action, context_state, target_action))
                else:
                    cursor.execute("""
                        INSERT INTO action_transitions (
                            source_action, context_state, target_action,
                            transition_count, last_transition_ts, last_decay_ts, weight
                        ) VALUES (?, ?, ?, 1, ?, ?, 1.0)
                    """, (source_action, context_state, target_action, now, now))
        except Exception as e:
            logger.debug("Error recording habit transition: %s", e)
        finally:
            conn.close()

    def predict_next_action(
        self,
        current_action: str,
        context_state: Optional[str] = None,
        alpha: float = 0.5,
        now: Optional[float] = None,
    ) -> Optional[HabitPrediction]:
        """Calculates Laplace-smoothed transition probability for candidate next actions.

        Applies sparse weight fallback to marginal prior if total exact context weight < 2.0.
        Gates proactive candidates at P >= 0.65 and N >= 3.
        """
        curr_time = now if now is not None else time.time()
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            candidates: List[Dict[str, Any]] = []
            context_used = context_state or "marginal"

            # 1. Attempt exact context query
            if context_state:
                cursor.execute("""
                    SELECT target_action, transition_count, last_decay_ts, weight
                    FROM action_transitions
                    WHERE source_action = ? AND context_state = ?
                """, (current_action, context_state))
                rows = cursor.fetchall()

                # Calculate decayed weights for exact context
                total_context_weight = 0.0
                exact_candidates = []
                for r in rows:
                    elapsed = max(0.0, curr_time - r["last_decay_ts"])
                    decayed_w = r["weight"] * (2.0 ** (-elapsed / HABIT_HALF_LIFE_SECONDS))
                    total_context_weight += decayed_w
                    exact_candidates.append({
                        "target_action": r["target_action"],
                        "transition_count": r["transition_count"],
                        "weight": decayed_w,
                    })

                # Adjustment 3: Fall back to marginal prior if total exact context weight < 2.0
                if total_context_weight >= 2.0:
                    candidates = exact_candidates
                    context_used = context_state

            # 2. Marginal Context Fallback
            if not candidates:
                cursor.execute("""
                    SELECT target_action, SUM(transition_count) as total_count,
                           SUM(weight) as total_weight, MAX(last_decay_ts) as max_decay_ts
                    FROM action_transitions
                    WHERE source_action = ?
                    GROUP BY target_action
                """, (current_action,))
                marginal_rows = cursor.fetchall()

                for r in marginal_rows:
                    elapsed = max(0.0, curr_time - (r["max_decay_ts"] or curr_time))
                    decayed_w = r["total_weight"] * (2.0 ** (-elapsed / HABIT_HALF_LIFE_SECONDS))
                    candidates.append({
                        "target_action": r["target_action"],
                        "transition_count": r["total_count"],
                        "weight": decayed_w,
                    })
                context_used = "marginal"

            if not candidates:
                return None

            # 3. Laplace Smoothing Inference
            # Effective candidate vocabulary size
            v_size = max(4, len(candidates))
            total_w = sum(c["weight"] for c in candidates)
            denominator = total_w + (alpha * v_size)

            scored = []
            for c in candidates:
                prob = (c["weight"] + alpha) / denominator
                scored.append((prob, c["transition_count"], c["target_action"]))

            scored.sort(key=lambda x: x[0], reverse=True)
            top_prob, top_count, top_target = scored[0]

            # Gating: High confidence proactive candidate requires P >= 0.65 and N >= 3
            is_proactive = (top_prob >= 0.65 and top_count >= 3)

            return HabitPrediction(
                target_action=top_target,
                probability=round(top_prob, 3),
                confidence=round(min(1.0, top_count / 5.0) * top_prob, 3),
                transition_count=top_count,
                is_proactive_candidate=is_proactive,
                context_used=context_used,
            )
        finally:
            conn.close()

    def apply_exponential_decay(
        self,
        half_life_days: float = 14.0,
        prune_threshold: float = 0.10,
        now: Optional[float] = None,
    ) -> int:
        """Applies exponential decay across all habit weights and prunes abandoned habits.

        Uses last_decay_ts to guarantee zero double-decay even when sleep consolidation
        triggers repeatedly during extended idle periods.
        """
        curr_time = now if now is not None else time.time()
        half_life_seconds = max(86400.0, half_life_days * 86400.0)

        conn = self._get_connection()
        total_updated = 0
        total_pruned = 0
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT source_action, context_state, target_action,
                           last_transition_ts, last_decay_ts, weight
                    FROM action_transitions
                """)
                rows = cursor.fetchall()

                for r in rows:
                    last_decay_ts = r["last_decay_ts"]
                    elapsed = max(0.0, curr_time - last_decay_ts)
                    # Only apply decay if at least 10 minutes have elapsed since last decay
                    if elapsed < 600.0:
                        continue

                    decay_factor = 2.0 ** (-elapsed / half_life_seconds)
                    new_weight = r["weight"] * decay_factor

                    # Prune if weight dropped below threshold and inactive for >30 days
                    if new_weight < prune_threshold and (curr_time - r["last_transition_ts"] > 30 * 86400.0):
                        cursor.execute("""
                            DELETE FROM action_transitions
                            WHERE source_action = ? AND context_state = ? AND target_action = ?
                        """, (r["source_action"], r["context_state"], r["target_action"]))
                        total_pruned += 1
                    else:
                        cursor.execute("""
                            UPDATE action_transitions
                            SET weight = ?, last_decay_ts = ?
                            WHERE source_action = ? AND context_state = ? AND target_action = ?
                        """, (new_weight, curr_time, r["source_action"], r["context_state"], r["target_action"]))
                        total_updated += 1

            logger.info(
                "Habit decay complete: %d weights decayed, %d abandoned transitions pruned.",
                total_updated, total_pruned
            )
            return total_updated + total_pruned
        finally:
            conn.close()

    def flush(self, timeout: float = 2.0) -> None:
        """Flushes pending async writes from the queue."""
        deadline = time.time() + timeout
        while not self._write_queue.empty() and time.time() < deadline:
            time.sleep(0.01)

    def stop(self) -> None:
        """Stops the background writer thread cleanly."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            try:
                self._write_queue.put_nowait(None)
            except Exception:
                pass
            self._worker_thread.join(timeout=1.0)
            self._worker_thread = None

    def _writer_loop(self) -> None:
        """Background worker thread draining write queue without blocking hook callbacks."""
        while self._running:
            try:
                item = self._write_queue.get(timeout=0.2)
                if item is None:
                    break
                source_action, context_state, target_action, now = item
                self._record_transition_sync(source_action, context_state, target_action, now)
                self._write_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.debug("HabitGraph writer worker exception: %s", e)
