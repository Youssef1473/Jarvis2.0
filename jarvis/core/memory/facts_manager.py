"""Atomic facts management, core user profile calibration, and hybrid fact recall."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from jarvis.core.memory.vector_engine import (
    fuse_vec_and_fts_rrf,
    sanitize_fts5_query,
    sanitize_memory_taint,
)

logger = logging.getLogger("Jarvis.Memory.FactsManager")


class FactsManagerMixin:
    """Provides atomic fact storage, core user preference self-editing, and hybrid fact search."""

    def remember_fact(self, key: str, value: str, category: str = "general") -> str:
        """Stores or updates a persistent fact about the user or system.
        Runs ONNX embedding inference OUTSIDE the active write transaction and
        serializes all database writes strictly through SingleWriterActor.
        """
        clean_cat = category.lower().strip()
        clean_key = key.lower().strip()
        clean_val = value.strip()

        # Step 1: Pre-compute ONNX dense embedding OUTSIDE write transaction
        blob = None
        try:
            import sqlite_vec
            embed_text = f"{clean_cat} {clean_key} {clean_val}"
            vec = self.vector_engine.dense_vectorize(embed_text)
            blob = sqlite_vec.serialize_float32(vec)
        except Exception as e:
            logger.warning("Fact vectorization failed (%s); continuing without vector index.", e)

        # Step 2: Route SQLite write strictly through SingleWriterActor
        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO facts (category, key, value, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    updated_at = CURRENT_TIMESTAMP
            """, (clean_cat, clean_key, clean_val))

            if blob is not None:
                try:
                    cursor.execute("SELECT id FROM facts WHERE key = ?", (clean_key,))
                    row = cursor.fetchone()
                    if row:
                        cursor.execute("INSERT OR REPLACE INTO facts_vec(fact_id, embedding) VALUES (?, ?)", (row["id"], blob))
                except Exception as e:
                    logger.warning("sqlite-vec fact indexing failed (%s); continuing without vector index.", e)

        self.execute_write(_write)

        with self._context_summary_lock:
            self._context_summary_cache = None
        return f"Remembered: '{key}' is '{value}'."

    def get_fact(self, key: str) -> str | None:
        """Retrieves a specific stored fact."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM facts WHERE key = ?", (key.lower().strip(),))
            row = cursor.fetchone()
            return row["value"] if row else None

    def update_user_preference(self, key: str, value: str, dimension_id: str = "custom", locked: int = 1) -> str:
        """Stores or updates a core user preference or behavioral calibration."""
        clean_key = sanitize_memory_taint(str(key or "").lower().strip().replace(" ", "_"))
        clean_val = sanitize_memory_taint(str(value or "").strip())
        if not clean_key or not clean_val:
            return "Preference key and value cannot be empty, Sir."

        self.remember_fact(clean_key, clean_val, category="user_preference")
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
        except Exception as e:
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
            except Exception as e:
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

    def search_facts(self, query: str) -> list[dict[str, str]]:
        """Searches remembered facts matching a query keyword using FTS5 BM25 with LIKE fallback."""
        sanitized = sanitize_fts5_query(query)
        if sanitized:
            try:
                with self._get_ro_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT f.category, f.key, f.value
                        FROM facts_fts ft
                        JOIN facts f ON ft.rowid = f.id
                        WHERE facts_fts MATCH ?
                        ORDER BY bm25(facts_fts) ASC
                    """, (sanitized,))
                    rows = [dict(row) for row in cursor.fetchall()]
                    if rows:
                        return rows
            except Exception as e:
                logger.debug("FTS5 facts search fallback to LIKE: %s", e)

        # Fallback to standard LIKE matching
        q = f"%{query.lower().strip()}%"
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT category, key, value FROM facts
                WHERE key LIKE ? OR value LIKE ? OR category LIKE ?
                ORDER BY category, key
            """, (q, q, q))
            return [dict(row) for row in cursor.fetchall()]

    def get_all_facts(self) -> list[dict[str, str]]:
        """Retrieves all stored facts."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT category, key, value FROM facts ORDER BY category, key")
            return [dict(row) for row in cursor.fetchall()]

    def semantic_search_facts(self, query: str, top_k: int = 5, min_score: float = 0.12) -> list[dict[str, Any]]:
        """Finds stored facts semantically relevant to query using sqlite-vec KNN search with TF-IDF fallback."""
        # 1. Native C sqlite-vec KNN Fast Path (<0.2ms)
        try:
            import sqlite_vec
            q_vec = self.vector_engine.embed_query(query)
            q_blob = sqlite_vec.serialize_float32(q_vec)
            with self._get_ro_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT f.category, f.key, f.value, v.distance
                    FROM facts_vec v
                    JOIN facts f ON v.fact_id = f.id
                    WHERE v.embedding MATCH ? AND k = ?
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
                                "relevance_score": round(sim, 3),
                                "distance": round(dist, 4),
                            })
                    if results:
                        results.sort(key=lambda x: x["relevance_score"], reverse=True)
                        return results[:top_k]
        except Exception as e:
            logger.warning("sqlite-vec KNN search failed (%s); falling back to lexical TF-IDF/BM25.", e)

        # 2. In-Memory TF-IDF Fallback
        facts = self.get_all_facts()
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

    def hybrid_search_facts(self, query: str, top_k: int = 5, k_rrf: int = 60, max_l2_distance: float = 0.78) -> list[dict[str, Any]]:
        """Hybrid search combining FTS5 BM25 and sqlite-vec KNN via Reciprocal Rank Fusion (RRF)."""
        fts_results = self.search_facts(query)
        vec_results = self.semantic_search_facts(query, top_k=max(top_k * 2, 10))
        fused = fuse_vec_and_fts_rrf(vec_results, fts_results, k=k_rrf, max_l2_distance=max_l2_distance)
        return fused[:top_k]

    async def search_facts_async(self, query: str) -> list[dict[str, str]]:
        """Asynchronously searches remembered facts matching a query keyword using VectorMemoryPool."""
        sanitized = sanitize_fts5_query(query)
        pool = self.get_vector_pool()
        if sanitized:
            try:
                def _fts_query(conn: sqlite3.Connection):
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT f.category, f.key, f.value
                        FROM facts_fts ft
                        JOIN facts f ON ft.rowid = f.id
                        WHERE facts_fts MATCH ?
                        ORDER BY bm25(facts_fts) ASC
                    """, (sanitized,))
                    return [dict(row) for row in cursor.fetchall()]

                rows = await pool.execute_read_async(_fts_query)
                if rows:
                    return rows
            except Exception as e:
                logger.debug("Async FTS5 facts search fallback to LIKE: %s", e)

        q = f"%{query.lower().strip()}%"
        def _like_query(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                SELECT category, key, value FROM facts
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
                    SELECT f.category, f.key, f.value, v.distance
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
        except Exception as e:
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
