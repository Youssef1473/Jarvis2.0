"""Atomic facts management, core user profile calibration, and hybrid fact recall."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import sqlite3
from typing import Any

from jarvis.core.memory.vector_engine import (
    VectorMemoryEngine,
    fuse_vec_and_fts_rrf,
    sanitize_fts5_query,
    sanitize_memory_taint,
    scrub_sensitive_credentials,
)

logger = logging.getLogger("Jarvis.Memory.FactsManager")


class LocalPrefixBM25Scorer:
    """Robertson-Zaragoza Positive-IDF BM25 Scorer with Prefix Expansion & Workspace Isolation."""

    def __init__(self, conn: sqlite3.Connection, workspace_id: str, k1: float = 1.2, b: float = 0.75):
        self.conn = conn
        self.workspace_id = workspace_id
        self.k1 = k1
        self.b = b
        self._compute_stats()

    def _compute_stats(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, value FROM facts WHERE workspace_id = ? AND scope = 'workspace'", (self.workspace_id,))
        rows = cur.fetchall()
        self.N = len(rows)
        self.doc_tokens_map = {}
        total_len = 0
        for row in rows:
            doc_id = row["id"] if hasattr(row, "__getitem__") and "id" in row else row[0]
            val = row["value"] if hasattr(row, "__getitem__") and "value" in row else row[1]
            toks = self._tokenize(str(val))
            self.doc_tokens_map[doc_id] = toks
            total_len += len(toks)
        self.avgdl = (total_len / self.N) if self.N > 0 else 1.0

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        # Matches SQLite unicode61 tokenization: words split by punctuation/whitespace/underscores
        cleaned = re.sub(r"[^\w\s]|_", " ", text.lower(), flags=re.UNICODE)
        return [w for w in cleaned.split() if w]

    def score_candidates(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not query or self.N == 0:
            return []
        prefixes = [p for p in self._tokenize(query) if len(p) >= 1]
        if not prefixes:
            return []

        fts_match = " OR ".join(f'"{p}"*' for p in prefixes)
        cur = self.conn.cursor()
        try:
            cur.execute("""
                SELECT f.id, f.category, f.key, f.value, f.source_provenance, f.updated_at
                FROM facts_fts ft
                JOIN facts f ON ft.rowid = f.id
                WHERE facts_fts MATCH ? AND f.workspace_id = ? AND f.scope = 'workspace'
            """, (fts_match, self.workspace_id))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return []
        if not candidates:
            return []

        n_qi = {}
        for p in prefixes:
            count = sum(1 for doc_id, toks in self.doc_tokens_map.items() if any(t.startswith(p) for t in toks))
            n_qi[p] = count

        scored = []
        for row in candidates:
            row_dict = dict(row) if hasattr(row, "keys") else {}
            doc_id = row_dict.get("id", row[0])
            toks = self.doc_tokens_map.get(doc_id, [])
            dl = len(toks)
            score = 0.0
            exact_matches = 0

            for p in prefixes:
                matching = [t for t in toks if t.startswith(p)]
                tf = len(matching)
                exact_matches += sum(1 for t in matching if t == p)
                if tf > 0:
                    df = n_qi.get(p, 1)
                    # Robertson-Zaragoza Positive-IDF
                    idf = math.log(1.0 + (self.N - df + 0.5) / (df + 0.5))
                    tf_comp = (tf * (self.k1 + 1.0)) / (tf + self.k1 * (1.0 - self.b + self.b * (dl / self.avgdl)))
                    score += idf * tf_comp

            scored.append({
                "category": row_dict.get("category", row[1]),
                "key": row_dict.get("key", row[2]),
                "value": row_dict.get("value", row[3]),
                "source_provenance": row_dict.get("source_provenance", row[4] if len(row) > 4 else "unknown"),
                "updated_at": row_dict.get("updated_at", row[5] if len(row) > 5 else None),
                "bm25_score": round(score, 4),
                "exact_matches": exact_matches,
            })

        scored.sort(key=lambda x: (-x["bm25_score"], -x["exact_matches"], x["key"] or ""))
        return scored[:top_k]


class FactsManagerMixin:
    """Provides atomic fact storage, core user preference self-editing, and hybrid fact search."""

    def remember_fact(
        self,
        key: str,
        value: str,
        category: str = "general",
        source_provenance: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        """Stores or updates a persistent fact about the user or system scoped to a workspace.
        Runs ONNX embedding inference OUTSIDE the active write transaction and
        serializes all database writes strictly through SingleWriterActor.
        """
        clean_cat = category.lower().strip()
        clean_key = scrub_sensitive_credentials(key.lower().strip())
        clean_val = scrub_sensitive_credentials(value.strip())

        from jarvis.core.security_boundaries import TurnTaintTracker
        source_provenance = TurnTaintTracker.resolve_write_provenance(source_provenance, default_if_clean="unknown")
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")

        # Step 1: Pre-compute ONNX dense embedding OUTSIDE write transaction
        blob = None
        try:
            import sqlite_vec
            embed_text = f"{clean_cat} {clean_key} {clean_val}"
            vec = self.vector_engine.dense_vectorize(embed_text)
            blob = sqlite_vec.serialize_float32(vec)
        except Exception as e:  # noqa: BLE001
            logger.warning("Fact vectorization failed (%s); continuing without vector index.", e)

        # Step 2: Route SQLite write strictly through SingleWriterActor
        def _write(conn: sqlite3.Connection) -> None:
            VectorMemoryEngine.check_workspace_vector_capacity(conn, ws_id)

            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM facts WHERE workspace_id = ? AND key = ? AND scope = 'workspace'",
                (ws_id, clean_key),
            )
            existing_row = cursor.fetchone()
            if existing_row:
                fact_id = existing_row["id"]
                cursor.execute("""
                    UPDATE facts
                    SET value = ?, category = ?, source_provenance = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (clean_val, clean_cat, source_provenance, fact_id))
            else:
                cursor.execute("""
                    INSERT INTO facts (workspace_id, scope, category, key, value, source_provenance, updated_at)
                    VALUES (?, 'workspace', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (ws_id, clean_cat, clean_key, clean_val, source_provenance))
                fact_id = cursor.lastrowid

            if blob is not None and fact_id:
                try:
                    cursor.execute("DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,))
                    cursor.execute("INSERT INTO facts_vec(fact_id, embedding, workspace_id) VALUES (?, ?, ?)", (fact_id, blob, ws_id))
                except Exception as e:  # noqa: BLE001  # noqa: BLE001
                    logger.warning("sqlite-vec fact indexing failed (%s); continuing without vector index.", e)

        self.execute_write(_write)

        with self._context_summary_lock:
            self._context_summary_cache = None
        return f"Remembered: '{clean_key}' is '{clean_val}'."

    def get_fact(self, key: str, workspace_id: str | None = None) -> str | None:
        """Retrieves a specific stored fact scoped to workspace."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value FROM facts
                WHERE key = ? AND workspace_id = ? AND scope = 'workspace'
            """, (key.lower().strip(), ws_id))
            row = cursor.fetchone()
            if row:
                return row["value"]
            # Fallback check for legacy unscoped fact if querying default
            if ws_id == "ws_default":
                cursor.execute("SELECT value FROM facts WHERE key = ? AND scope = 'unscoped'", (key.lower().strip(),))
                unscoped_row = cursor.fetchone()
                if unscoped_row:
                    return unscoped_row["value"]
            return None

    def get_fact_for_workspace(self, key: str, workspace: str) -> str | None:
        """Retrieves a fact specifically scoped to a workspace path or workspace_id."""
        ws_id = self.resolve_workspace_id(workspace) if hasattr(self, "resolve_workspace_id") else workspace
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value FROM facts
                WHERE key = ? AND workspace_id = ? AND scope = 'workspace'
            """, (key.lower().strip(), ws_id))
            row = cursor.fetchone()
            return row["value"] if row else None

    def get_fact_with_provenance(self, key: str, workspace_id: str | None = None) -> tuple[str | None, str]:
        """Retrieves a specific stored fact along with its source provenance."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT value, source_provenance FROM facts
                    WHERE key = ? AND workspace_id = ? AND scope = 'workspace'
                """, (key.lower().strip(), ws_id))
                row = cursor.fetchone()
                if row:
                    row_dict = dict(row) if hasattr(row, "keys") else {}
                    prov = row_dict.get("source_provenance", "unknown")
                    return row["value"], prov
                if ws_id == "ws_default":
                    cursor.execute("""
                        SELECT value, source_provenance FROM facts
                        WHERE key = ? AND scope = 'unscoped'
                    """, (key.lower().strip(),))
                    u_row = cursor.fetchone()
                    if u_row:
                        u_dict = dict(u_row) if hasattr(u_row, "keys") else {}
                        prov = u_dict.get("source_provenance", "unknown")
                        return u_row["value"], prov
            except sqlite3.OperationalError:
                try:
                    cursor.execute("SELECT value FROM facts WHERE key = ?", (key.lower().strip(),))
                    row = cursor.fetchone()
                    if row:
                        return row["value"], "unknown"
                except Exception as e:  # noqa: BLE001
                    logger.debug("Unmigrated facts table fallback failed: %s", e)
            return None, "unknown"

    def update_user_preference(self, key: str, value: str, dimension_id: str = "custom", locked: int = 1) -> str:
        """Stores or updates a core user preference or behavioral calibration."""
        clean_key = sanitize_memory_taint(str(key or "").lower().strip().replace(" ", "_"))
        clean_val = sanitize_memory_taint(str(value or "").strip())
        if not clean_key or not clean_val:
            return "Preference key and value cannot be empty, Sir."

        self.remember_fact(clean_key, clean_val, category="user_preference", source_provenance="user_supplied")
        def _write_pref(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_preferences (pref_key, pref_value, dimension_id, locked_by_sir, updated_at_utc)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(pref_key) DO UPDATE SET
                    pref_value = excluded.pref_value,
                    dimension_id = excluded.dimension_id,
                    locked_by_sir = excluded.locked_by_sir,
                    updated_at_utc = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """, (clean_key, clean_val, dimension_id, locked))

        try:
            self.execute_write(_write_pref)
        except Exception as e:  # noqa: BLE001
            logger.debug("user_preferences table update notice: %s", e)

        return f"Core preference updated: '{clean_key}' is now set to '{clean_val}', Sir."

    def get_user_preferences(self) -> dict[str, str]:
        """Retrieves all core user preferences as a key-value dictionary."""
        result: dict[str, str] = {}
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT pref_key, pref_value FROM user_preferences ORDER BY pref_key")
                for row in cursor.fetchall():
                    result[row["pref_key"]] = row["pref_value"]
            except Exception as e:  # noqa: BLE001
                logger.debug("user_preferences table select notice: %s", e)

            cursor.execute("""
                SELECT key, value FROM facts
                WHERE category IN ('user_preference', 'preference', 'core_preference', 'habit')
                ORDER BY updated_at DESC
            """)
            for row in cursor.fetchall():
                result[row["key"]] = row["value"]

        return result

    def format_core_preferences_xml(self) -> str:
        """Serializes Sir's active 7-Dimension operational profile into compact XML (<600 bytes)."""
        return (
            "<core_user_preferences>\n"
            '  <persona style="bettany_mcu_wit_litotes" vocative="Sir:max_1_pivot_no_repeat" diction="ear_normalized_phonetic" />\n'
            '  <proactivity anomaly="hybrid_hud_diff_plus_5word_chime" circadian="ironic_quip_after_0000_120m_flow" />\n'
            '  <engineering design="socratic_constraint_plus_ab_fork" execution="autonomous_pytest_loop_until_green" />\n'
            '  <actuation vision="instant_with_privacy_guard" browser="silent_cdp_9222" />\n'
            '  <memory facts="ask_confirmation_before_sql_commit" habits="auto_compile_3gram_at_3x_0.85p" />\n'
            '  <barge_in mode="1_sentence_answer_then_lifo_resume" safety="read_test_instant_gate_destructive" />\n'
            "</core_user_preferences>"
        )

    def format_core_preferences_block(self) -> str:
        """Formats active core user preferences into a structured block for working memory."""
        prefs = self.get_user_preferences()
        if not prefs:
            return ""
        lines = [f"- {k}: {v}" for k, v in sorted(prefs.items())]
        return "Core User Profile & Preferences (Always adhere to these directives):\n" + "\n".join(lines)

    def search_facts(self, query: str, workspace_id: str | None = None) -> list[dict[str, str]]:
        """Searches remembered facts matching a query keyword using LocalPrefixBM25Scorer with LIKE fallback."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        with self._get_ro_connection() as conn:
            scorer = LocalPrefixBM25Scorer(conn, ws_id)
            scored = scorer.score_candidates(query, top_k=10)
            if scored:
                return [{
                    "category": s["category"],
                    "key": s["key"],
                    "value": s["value"],
                    "source_provenance": s["source_provenance"],
                } for s in scored]

            # If default workspace has no scoped hits, allow unscoped FTS lookup
            sanitized = sanitize_fts5_query(query)
            if sanitized and ws_id == "ws_default":
                try:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT f.category, f.key, f.value, f.source_provenance, f.updated_at
                        FROM facts_fts ft
                        JOIN facts f ON ft.rowid = f.id
                        WHERE facts_fts MATCH ? AND f.scope = 'unscoped'
                        ORDER BY bm25(facts_fts) ASC
                    """, (sanitized,))
                    rows = [dict(row) for row in cursor.fetchall()]
                    if rows:
                        return rows
                except Exception as e:  # noqa: BLE001
                    logger.debug("FTS5 unscoped facts search note: %s", e)

            # Fallback to standard LIKE matching scoped to workspace
            q = f"%{query.lower().strip()}%"
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT category, key, value, source_provenance, updated_at FROM facts
                    WHERE (key LIKE ? OR value LIKE ? OR category LIKE ?)
                      AND (workspace_id = ? AND scope = 'workspace')
                    ORDER BY category, key
                """, (q, q, q, ws_id))
                rows = cursor.fetchall()
                if not rows and ws_id == "ws_default":
                    cursor.execute("""
                        SELECT category, key, value, source_provenance, updated_at FROM facts
                        WHERE (key LIKE ? OR value LIKE ? OR category LIKE ?)
                          AND scope = 'unscoped'
                        ORDER BY category, key
                    """, (q, q, q))
                    rows = cursor.fetchall()
                return [dict(row) for row in rows]
            except sqlite3.OperationalError:
                cursor.execute("""
                    SELECT category, key, value FROM facts
                    WHERE (key LIKE ? OR value LIKE ? OR category LIKE ?)
                      AND (workspace_id = ? AND scope = 'workspace')
                    ORDER BY category, key
                """, (q, q, q, ws_id))
                rows = cursor.fetchall()
                if not rows and ws_id == "ws_default":
                    cursor.execute("""
                        SELECT category, key, value FROM facts
                        WHERE (key LIKE ? OR value LIKE ? OR category LIKE ?)
                          AND scope = 'unscoped'
                        ORDER BY category, key
                    """, (q, q, q))
                    rows = cursor.fetchall()
                return [{**dict(row), "source_provenance": "unknown", "updated_at": None} for row in rows]

    def get_all_facts(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieves all stored facts for the workspace."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT category, key, value, source_provenance, updated_at
                    FROM facts
                    WHERE workspace_id = ? AND scope = 'workspace'
                    ORDER BY category, key
                """, (ws_id,))
                rows = cursor.fetchall()
                if not rows and ws_id == "ws_default":
                    cursor.execute("""
                        SELECT category, key, value, source_provenance, updated_at
                        FROM facts
                        WHERE scope = 'unscoped'
                        ORDER BY category, key
                    """)
                    rows = cursor.fetchall()
                return [dict(row) for row in rows]
            except sqlite3.OperationalError:
                cursor.execute("""
                    SELECT category, key, value FROM facts
                    WHERE workspace_id = ? AND scope = 'workspace'
                    ORDER BY category, key
                """, (ws_id,))
                rows = cursor.fetchall()
                if not rows and ws_id == "ws_default":
                    cursor.execute("SELECT category, key, value FROM facts WHERE scope = 'unscoped' ORDER BY category, key")
                    rows = cursor.fetchall()
                return [{**dict(row), "source_provenance": "unknown", "updated_at": None} for row in rows]

    def semantic_search_facts(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.12,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Finds stored facts semantically relevant to query using sqlite-vec KNN search with TF-IDF fallback."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        # 1. Native C sqlite-vec KNN Fast Path (<0.2ms)
        try:
            import sqlite_vec
            q_vec = self.vector_engine.embed_query(query)
            q_blob = sqlite_vec.serialize_float32(q_vec)
            with self._get_ro_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT f.category, f.key, f.value, f.source_provenance, f.updated_at, v.distance
                    FROM facts_vec v
                    JOIN facts f ON v.fact_id = f.id
                    WHERE v.embedding MATCH ? AND k = ? AND v.workspace_id = ?
                    ORDER BY v.distance ASC
                """, (q_blob, max(1, top_k * 2), ws_id))
                knn_rows = cursor.fetchall()
                if not knn_rows and ws_id == "ws_default":
                    cursor.execute("""
                        SELECT f.category, f.key, f.value, f.source_provenance, f.updated_at, v.distance
                        FROM facts_vec v
                        JOIN facts f ON v.fact_id = f.id
                        WHERE v.embedding MATCH ? AND k = ? AND v.workspace_id = 'unscoped'
                        ORDER BY v.distance ASC
                    """, (q_blob, max(1, top_k * 2)))
                    knn_rows = cursor.fetchall()

                if knn_rows:
                    results = []
                    for row in knn_rows:
                        dist = float(row["distance"])
                        sim = max(0.0, 1.0 - (dist / 2.0))
                        if sim >= min_score:
                            results.append({
                                "category": row["category"],
                                "key": row["key"],
                                "value": row["value"],
                                "source_provenance": row["source_provenance"] if "source_provenance" in set(row.keys()) else "unknown",
                                "updated_at": row["updated_at"] if "updated_at" in set(row.keys()) else None,
                                "relevance_score": round(sim, 3),
                                "distance": round(dist, 4),
                            })
                    if results:
                        results.sort(key=lambda x: x["relevance_score"], reverse=True)
                        return results[:top_k]
        except Exception as e:  # noqa: BLE001
            logger.warning("sqlite-vec KNN search failed (%s); falling back to lexical TF-IDF/BM25.", e)

        # 2. In-Memory TF-IDF Fallback
        facts = self.get_all_facts(workspace_id=ws_id)
        if not facts:
            return []

        corpus = [f"{f.get('category', '')} {f.get('key', '')} {f.get('value', '')}" for f in facts]
        idf = self.vector_engine.compute_idf(corpus)
        query_vec = self.vector_engine.vectorize(query, idf)
        if not query_vec:
            return []

        scored = []
        for f, doc_text in zip(facts, corpus):
            doc_vec = self.vector_engine.vectorize(doc_text, idf)
            score = self.vector_engine.cosine_similarity(query_vec, doc_vec)
            if score >= min_score:
                f_dict = dict(f)
                f_dict["relevance_score"] = round(score, 3)
                scored.append(f_dict)

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored[:top_k]

    def hybrid_search_facts(
        self,
        query: str,
        top_k: int = 5,
        k_rrf: int = 60,
        max_l2_distance: float = 0.78,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining FTS5 BM25 and sqlite-vec KNN via Reciprocal Rank Fusion (RRF)."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        fts_results = self.search_facts(query, workspace_id=ws_id)
        vec_results = self.semantic_search_facts(query, top_k=max(top_k * 2, 10), workspace_id=ws_id)
        fused = fuse_vec_and_fts_rrf(vec_results, fts_results, k=k_rrf, max_l2_distance=max_l2_distance)
        return fused[:top_k]

    async def search_facts_async(self, query: str, workspace_id: str | None = None) -> list[dict[str, str]]:
        """Asynchronously searches remembered facts matching a query keyword using VectorMemoryPool."""
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")
        sanitized = sanitize_fts5_query(query)
        pool = self.get_vector_pool()
        if sanitized:
            try:
                def _fts_query(conn: sqlite3.Connection):
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT f.category, f.key, f.value, f.source_provenance
                        FROM facts_fts ft
                        JOIN facts f ON ft.rowid = f.id
                        WHERE facts_fts MATCH ? AND f.workspace_id = ? AND f.scope = 'workspace'
                        ORDER BY bm25(facts_fts) ASC
                    """, (sanitized, ws_id))
                    rows = [dict(row) for row in cursor.fetchall()]
                    if not rows and ws_id == "ws_default":
                        cursor.execute("""
                            SELECT f.category, f.key, f.value, f.source_provenance
                            FROM facts_fts ft
                            JOIN facts f ON ft.rowid = f.id
                            WHERE facts_fts MATCH ? AND f.scope = 'unscoped'
                            ORDER BY bm25(facts_fts) ASC
                        """, (sanitized,))
                        rows = [dict(row) for row in cursor.fetchall()]
                    return rows

                rows = await pool.execute_read_async(_fts_query)
                if rows:
                    return rows
            except Exception as e:  # noqa: BLE001
                logger.debug("Async FTS5 facts search fallback to LIKE: %s", e)

        q = f"%{query.lower().strip()}%"
        def _like_query(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                SELECT category, key, value, source_provenance FROM facts
                WHERE key LIKE ? OR value LIKE ? OR category LIKE ?
                ORDER BY category, key
            """, (q, q, q))
            return [dict(row) for row in cursor.fetchall()]

        return await pool.execute_read_async(_like_query)

    async def semantic_search_facts_async(
        self, query: str, top_k: int = 5, min_score: float = 0.12
    ) -> list[dict[str, Any]]:
        """Asynchronously finds stored facts semantically relevant to query using VectorMemoryPool."""
        pool = self.get_vector_pool()
        try:
            import sqlite_vec
            q_vec = await asyncio.to_thread(self.vector_engine.embed_query, query)
            q_blob = sqlite_vec.serialize_float32(q_vec)

            def _knn_query(conn: sqlite3.Connection):
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT f.category, f.key, f.value, f.source_provenance, v.distance
                    FROM facts_vec v
                    JOIN facts f ON v.fact_id = f.id
                    WHERE v.embedding MATCH ? AND k = ?
                    ORDER BY v.distance ASC
                """, (q_blob, max(1, top_k * 2)))
                return [dict(row) for row in cursor.fetchall()]

            knn_rows = await pool.execute_read_async(_knn_query)
            if knn_rows:
                results = []
                for row in knn_rows:
                    dist = float(row["distance"])
                    sim = max(0.0, 1.0 - (dist / 2.0))
                    if sim >= min_score:
                        results.append({
                            "category": row["category"],
                            "key": row["key"],
                            "value": row["value"],
                            "relevance_score": round(sim, 3),
                            "distance": round(dist, 4),
                        })
                if results:
                    results.sort(key=lambda x: x["relevance_score"], reverse=True)
                    return results[:top_k]
        except Exception as e:  # noqa: BLE001
            logger.debug("Async sqlite-vec facts KNN search fallback: %s", e)

        return await asyncio.to_thread(self.semantic_search_facts, query, top_k, min_score)

    async def hybrid_search_facts_async(
        self, query: str, top_k: int = 5, k_rrf: int = 60, max_l2_distance: float = 0.78
    ) -> list[dict[str, Any]]:
        """Asynchronously performs hybrid search combining FTS5 BM25 and sqlite-vec KNN via RRF."""
        fts_task = asyncio.create_task(self.search_facts_async(query))
        vec_task = asyncio.create_task(self.semantic_search_facts_async(query, top_k=max(top_k * 2, 10)))
        fts_results, vec_results = await asyncio.gather(fts_task, vec_task)
        fused = fuse_vec_and_fts_rrf(vec_results, fts_results, k=k_rrf, max_l2_distance=max_l2_distance)
        return fused[:top_k]

    def get_all_memory_nodes(
        self,
        category: str | None = None,
        search: str | None = None,
        limit: int = 150,
    ) -> dict[str, Any]:
        """Retrieves structured memory nodes across facts, preferences, documents, and episodic lessons."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()

            facts_list = []
            prefs_list = []

            if search:
                q = f"%{search.lower().strip()}%"
                cursor.execute("""
                    SELECT id, category, key, value, updated_at
                    FROM facts
                    WHERE key LIKE ? OR value LIKE ? OR category LIKE ?
                    ORDER BY updated_at DESC LIMIT ?
                """, (q, q, q, limit))
            else:
                cursor.execute("""
                    SELECT id, category, key, value, updated_at
                    FROM facts
                    ORDER BY updated_at DESC LIMIT ?
                """, (limit,))

            for row in cursor.fetchall():
                item = {
                    "id": row["id"],
                    "category": row["category"],
                    "key": row["key"],
                    "value": row["value"],
                    "updated_at": str(row["updated_at"] or ""),
                }
                if row["category"] in ("user_preference", "preference", "core_preference", "habit"):
                    prefs_list.append(item)
                else:
                    facts_list.append(item)

            doc_list = []
            if search:
                cursor.execute("""
                    SELECT id, title, category, summary, created_at
                    FROM documents
                    WHERE title LIKE ? OR summary LIKE ? OR category LIKE ?
                    ORDER BY created_at DESC LIMIT ?
                """, (q, q, q, limit))
            else:
                cursor.execute("""
                    SELECT id, title, category, summary, created_at
                    FROM documents
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,))
            for row in cursor.fetchall():
                doc_list.append({
                    "id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "summary": row["summary"] or "",
                    "created_at": str(row["created_at"] or ""),
                })

            lesson_list = []
            if search:
                cursor.execute("""
                    SELECT id, tool_name, trigger_context, failure_signature, lesson, hit_count, created_at
                    FROM episodic_lessons
                    WHERE tool_name LIKE ? OR trigger_context LIKE ? OR lesson LIKE ?
                    ORDER BY created_at DESC LIMIT ?
                """, (q, q, q, limit))
            else:
                cursor.execute("""
                    SELECT id, tool_name, trigger_context, failure_signature, lesson, hit_count, created_at
                    FROM episodic_lessons
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,))
            for row in cursor.fetchall():
                lesson_list.append({
                    "id": row["id"],
                    "tool_name": row["tool_name"],
                    "trigger_context": row["trigger_context"],
                    "failure_signature": row["failure_signature"],
                    "lesson": row["lesson"],
                    "hit_count": row["hit_count"],
                    "created_at": str(row["created_at"] or ""),
                })

            cursor.execute("SELECT COUNT(*) as c FROM facts WHERE category NOT IN ('user_preference', 'preference', 'core_preference', 'habit')")
            facts_count = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) as c FROM facts WHERE category IN ('user_preference', 'preference', 'core_preference', 'habit')")
            prefs_count = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) as c FROM documents")
            docs_count = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) as c FROM episodic_lessons")
            lessons_count = cursor.fetchone()["c"]

            return {
                "total": facts_count + prefs_count + docs_count + lessons_count,
                "counts": {
                    "facts": facts_count,
                    "preferences": prefs_count,
                    "documents": docs_count,
                    "lessons": lessons_count,
                },
                "facts": facts_list,
                "preferences": prefs_list,
                "documents": doc_list,
                "lessons": lesson_list,
            }

    def delete_memory_node(self, node_type: str, node_id: Any) -> bool:
        """Deletes a memory record across facts, preferences, documents, or episodic lessons."""
        clean_type = str(node_type or "").lower().strip()
        def _delete(conn: sqlite3.Connection) -> bool:
            cursor = conn.cursor()
            if clean_type in ("fact", "facts", "preference", "user_preference"):
                if isinstance(node_id, int) or (isinstance(node_id, str) and node_id.isdigit()):
                    cursor.execute("DELETE FROM facts WHERE id = ?", (int(node_id),))
                else:
                    cursor.execute("DELETE FROM facts WHERE key = ?", (str(node_id).lower().strip(),))
                return cursor.rowcount > 0
            elif clean_type in ("document", "documents"):
                cursor.execute("DELETE FROM documents WHERE id = ?", (int(node_id),))
                return cursor.rowcount > 0
            elif clean_type in ("lesson", "lessons", "episodic_lesson"):
                cursor.execute("DELETE FROM episodic_lessons WHERE id = ?", (int(node_id),))
                return cursor.rowcount > 0
            return False

        deleted = bool(self.execute_write(_delete))

        with self._context_summary_lock:
            self._context_summary_cache = None
        return deleted

    def delete_fact(self, key_or_id: Any) -> bool:
        """Deletes a stored fact by key or integer ID."""
        return self.delete_memory_node("fact", key_or_id)

    def delete_user_preference(self, key: str) -> bool:
        """Deletes a stored user preference by key."""
        return self.delete_memory_node("preference", key)
