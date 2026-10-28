"""Bi-temporal knowledge graph edges, Markov habit transitions, and reversible action journaling."""
from __future__ import annotations

import datetime
import json
import logging
import math
import time
from typing import Any

logger = logging.getLogger("Jarvis.Memory.TemporalGraph")


class TemporalGraphMixin:
    """Provides bi-temporal knowledge graph operations, habit prediction, and action undo."""

    def remember_edge(
        self,
        subject: str,
        predicate: str,
        object_val: str,
        is_functional: bool = True,
        confidence: float = 1.0,
        decay_lambda: float = 0.01,
        source_context: str | None = None,
    ) -> int:
        """Stores a bi-temporal knowledge edge with automatic functional supersession."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO knowledge_edges (
                    subject, predicate, object, is_functional,
                    confidence, access_count, decay_lambda,
                    valid_from, valid_until, last_accessed, source_context
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
            """, (
                subject.strip(),
                predicate.strip(),
                str(object_val).strip(),
                1 if is_functional else 0,
                float(confidence),
                float(decay_lambda),
                now,
                now,
                source_context,
            ))
            edge_id = cursor.lastrowid
            try:
                import sqlite_vec
                embed_text = f"{subject} {predicate} {object_val}"
                vec = self.vector_engine.dense_vectorize(embed_text)
                blob = sqlite_vec.serialize_float32(vec)
                cursor.execute("INSERT OR REPLACE INTO kg_edges_vec(edge_id, embedding) VALUES (?, ?)", (edge_id, blob))
            except Exception:
                pass
            return edge_id

    def query_active_edges(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Queries currently active knowledge edges (valid_until IS NULL) with ACT-R temporal activation."""
        now = time.time()
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM knowledge_edges WHERE valid_until IS NULL"
            params: list[Any] = []
            if subject:
                query += " AND subject = ?"
                params.append(subject.strip())
            if predicate:
                query += " AND predicate = ?"
                params.append(predicate.strip())
            query += " ORDER BY last_accessed DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            rows = [dict(r) for r in cursor.fetchall()]

            # Compute ACT-R temporal activation score: Confidence * exp(-decay_lambda * delta_days)
            for r in rows:
                delta_days = max(0.0, (now - float(r.get("last_accessed", now))) / 86400.0)
                decay_rate = float(r.get("decay_lambda", 0.01))
                r["temporal_score"] = round(float(r.get("confidence", 1.0)) * math.exp(-decay_rate * delta_days), 4)

        # Update access tracking via writer connection
        if rows:
            try:
                with self._get_connection() as wconn:
                    wcursor = wconn.cursor()
                    ids = [r["edge_id"] for r in rows]
                    placeholders = ",".join("?" * len(ids))
                    wcursor.execute(
                        f"UPDATE knowledge_edges SET last_accessed = ?, access_count = access_count + 1 WHERE edge_id IN ({placeholders})",
                        [now] + ids
                    )
                    wconn.commit()
            except Exception as e:
                logger.debug("knowledge_edges access tracking update notice: %s", e)

        return sorted(rows, key=lambda x: x.get("temporal_score", 0.0), reverse=True)

    def record_habit_transition(
        self,
        src_event: str,
        dst_event: str,
        hour_bucket: int | None = None,
        is_weekend: int | None = None,
    ) -> None:
        """Records a user action sequence transition conditioned on circadian hour and weekend status."""
        now_dt = datetime.datetime.now()
        h = hour_bucket if hour_bucket is not None else now_dt.hour
        w = is_weekend if is_weekend is not None else (1 if now_dt.weekday() >= 5 else 0)
        now = time.time()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO habit_transitions (src_event, dst_event, hour_bucket, is_weekend, decayed_weight, sample_count, last_observed)
                VALUES (?, ?, ?, ?, 1.0, 1, ?)
                ON CONFLICT(src_event, dst_event, hour_bucket, is_weekend) DO UPDATE SET
                    sample_count = sample_count + 1,
                    decayed_weight = decayed_weight + 1.0,
                    last_observed = excluded.last_observed
            """, (src_event.strip(), dst_event.strip(), h, w, now))

    def predict_next_action(
        self,
        src_event: str,
        hour_bucket: int | None = None,
        is_weekend: int | None = None,
        min_prob: float = 0.50,
    ) -> tuple[str, float] | None:
        """Predicts the most probable next action using time-conditioned Dirichlet/Laplace smoothed transitions."""
        now_dt = datetime.datetime.now()
        h = hour_bucket if hour_bucket is not None else now_dt.hour
        w = is_weekend if is_weekend is not None else (1 if now_dt.weekday() >= 5 else 0)

        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT dst_event, decayed_weight, sample_count
                FROM habit_transitions
                WHERE src_event = ? AND hour_bucket = ? AND is_weekend = ?
                ORDER BY decayed_weight DESC
            """, (src_event.strip(), h, w))
            rows = cursor.fetchall()
            if not rows:
                return None

            alpha = 0.5
            v_size = max(1, len(rows))
            total_weight = sum(r["decayed_weight"] for r in rows)
            denom = total_weight + (alpha * v_size)

            best = rows[0]
            prob = (best["decayed_weight"] + alpha) / denom
            if prob >= min_prob and best["sample_count"] >= 1:
                return (best["dst_event"], round(prob, 4))
            return None

    def log_reversible_action(
        self,
        action_id: str,
        skill_name: str,
        risk_level: int,
        inverse_skill: str,
        inverse_payload: dict[str, Any],
    ) -> None:
        """Logs a Level-1 reversible desktop mutation to the action journal for instant undo."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO action_journal (action_id, timestamp, skill_name, risk_level, inverse_skill, inverse_payload, reverted)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            """, (action_id, now, skill_name, risk_level, inverse_skill, json.dumps(inverse_payload)))

    def revert_action(self, action_id: str | None = None) -> tuple[bool, str]:
        """Reverts the latest non-reverted action (or specified action_id)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if action_id:
                cursor.execute("SELECT * FROM action_journal WHERE action_id = ? AND reverted = 0", (action_id,))
            else:
                cursor.execute("SELECT * FROM action_journal WHERE reverted = 0 ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return False, "No active reversible action found to undo."

            act_dict = dict(row)
            inv_skill = act_dict["inverse_skill"]
            try:
                inv_args = json.loads(act_dict["inverse_payload"])
            except Exception:
                inv_args = {}

            from jarvis.skills.base import SkillRegistry
            try:
                res = SkillRegistry.execute(inv_skill, **inv_args)
                cursor.execute("UPDATE action_journal SET reverted = 1 WHERE action_id = ?", (act_dict["action_id"],))
                return True, f"Successfully reverted action '{act_dict['skill_name']}': {res}"
            except Exception as e:
                return False, f"Failed to revert action '{act_dict['skill_name']}': {e}"
