"""Interaction logging, autonomous context auto-learning, episodic reflection, and sleep phase consolidation."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from jarvis.core.memory.vector_engine import (
    sanitize_fts5_query,
    sanitize_memory_taint,
)

logger = logging.getLogger("Jarvis.Memory.InteractionMemory")


class InteractionMemoryMixin:
    """Provides interaction turn recording, auto-learning heuristics, and episodic reflection."""

    def log_interaction(self, user_input: str, jarvis_reply: str, tool_calls: str | None = None):
        """Logs a conversation turn."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO interactions (user_input, jarvis_reply, tool_calls)
                VALUES (?, ?, ?)
            """, (user_input, jarvis_reply, tool_calls or ""))
            conn.commit()

    def get_recent_interactions(self, limit: int = 8) -> list[dict[str, Any]]:
        """Retrieves the most recent N interaction turns in chronological order."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_input, jarvis_reply, tool_calls, created_at
                FROM interactions
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    def search_interactions(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Searches past conversation logs by keyword or phrase using FTS5 BM25 with LIKE fallback."""
        sanitized = sanitize_fts5_query(query)
        if sanitized:
            try:
                with self._get_ro_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT i.id, i.user_input, i.jarvis_reply, i.tool_calls, i.created_at
                        FROM interactions_fts inf
                        JOIN interactions i ON inf.rowid = i.id
                        WHERE interactions_fts MATCH ?
                        ORDER BY bm25(interactions_fts) ASC
                        LIMIT ?
                    """, (sanitized, limit))
                    rows = [dict(r) for r in cursor.fetchall()]
                    if rows:
                        return rows
            except Exception as e:
                logger.debug("FTS5 interactions search fallback to LIKE: %s", e)

        # Fallback to standard LIKE matching
        terms = [t.strip().lower() for t in query.split() if len(t.strip()) > 2]
        if not terms:
            terms = [query.strip().lower()]

        conditions = []
        params = []
        for term in terms:
            conditions.append("(LOWER(user_input) LIKE ? OR LOWER(jarvis_reply) LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%"])

        where_clause = " OR ".join(conditions)
        sql = f"""
            SELECT id, user_input, jarvis_reply, tool_calls, created_at
            FROM interactions
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(limit)

        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            return [dict(r) for r in cursor.fetchall()]

    def semantic_search_interactions(self, query: str, top_k: int = 5, min_score: float = 0.10) -> list[dict[str, Any]]:
        """Searches past conversation history using semantic vector similarity."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_input, jarvis_reply, tool_calls, created_at
                FROM interactions
                ORDER BY id DESC
                LIMIT 150
            """)
            rows = [dict(r) for r in cursor.fetchall()]

        if not rows:
            return []

        corpus = [f"{r['user_input']} {r['jarvis_reply']}" for r in rows]
        idf = self.vector_engine.compute_idf(corpus)
        query_vec = self.vector_engine.vectorize(query, idf)
        if not query_vec:
            return []

        scored = []
        for r, doc_text in zip(rows, corpus):
            doc_vec = self.vector_engine.vectorize(doc_text, idf)
            score = self.vector_engine.cosine_similarity(query_vec, doc_vec)
            if score >= min_score:
                r_dict = dict(r)
                r_dict["relevance_score"] = round(score, 3)
                scored.append(r_dict)

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored[:top_k]

    def auto_learn_from_interaction(
        self,
        user_text: str,
        reply_text: str,
        groq_api_key: str | None = None,
        provider: str = "groq",
        extractor_fn: Callable[[str, str], list[dict[str, Any]]] | None = None,
    ):
        """Asynchronously analyzes user utterances and interactions to extract and persist
        user preferences, habits, active technical topics, and facts across sessions."""
        cleaned = user_text.strip().lower()
        if not self.background_learning_enabled:
            return
        # Fast filter for trivial greetings, one-word queries, and short commands
        trivial = {"hey jarvis", "exit", "quit", "goodbye", "help", "who are you", "yes", "no", "ok", "okay", "cancel", "thanks", "thank you"}
        if len(cleaned) < 8 or cleaned in trivial:
            return

        with self._learning_lock:
            if self._learning_in_flight:
                return
            self._learning_in_flight = True

        def _extract():
            try:
                items: list[dict[str, Any]] = []
                if extractor_fn is not None:
                    try:
                        extracted = extractor_fn(user_text, reply_text)
                        if isinstance(extracted, list):
                            items = extracted
                    except Exception as e:
                        logger.debug("Custom extractor notice: %s", e)

                if not items:
                    # Built-in provider fallback (ollama / groq)
                    system_prompt = (
                        "You are an ambient background memory extractor for Sir's personal assistant JARVIS. "
                        "Analyze the user's utterance and determine if Sir revealed any permanent personal facts, "
                        "preferences, habits, routine details, projects, or behavioral rules about himself. "
                        "If so, output a JSON array of objects with keys: 'category' (identity|preference|habit|project|topic|general), "
                        "'key' (snake_case, concise), 'value' (clear description). "
                        "If no persistent personal facts about Sir were shared, output ONLY an empty array: []\n"
                        "Output valid JSON without markdown fences."
                    )
                    if provider == "ollama":
                        api_key = "ollama"
                        base_url = self.llm_config.get("ollama_base_url", "http://127.0.0.1:11434/v1")
                        model = self.llm_config.get("ollama_model", "qwen3.5:2b-q4_K_M")
                    elif provider == "groq" or (groq_api_key or os.getenv("GROQ_API_KEY")):
                        api_key = groq_api_key or os.getenv("GROQ_API_KEY", "").strip()
                        base_url = "https://api.groq.com/openai/v1"
                        model = self.llm_config.get("groq_model", "qwen/qwen3.8-27b")
                    else:
                        api_key = None

                    if api_key:
                        if provider == "groq":
                            time.sleep(1.0)
                        try:
                            from openai import OpenAI
                            client = OpenAI(
                                api_key=api_key,
                                base_url=base_url,
                                max_retries=0,
                                timeout=90.0 if provider == "ollama" else 8.0,
                            )
                            res = client.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": f"User statement: '{user_text}'"}
                                ],
                                max_tokens=250,
                                temperature=0.0,
                                **({"reasoning_effort": "none"} if provider == "ollama" else {}),
                            )
                            content = res.choices[0].message.content.strip()
                            if content.startswith("```"):
                                content = re.sub(r"^```(?:json)?\n?", "", content)
                                content = re.sub(r"\n?```$", "", content)
                            parsed = json.loads(content)
                            if isinstance(parsed, list):
                                items = parsed
                        except Exception as e_oe:
                            logger.debug("Memory extractor client execution note: %s", e_oe)

                # Sensitive data patterns to prevent storing secrets/credentials
                sensitive_pattern = re.compile(
                    r"(?:api[_-]?key|secret|token|password|bearer|auth|credit[_-]?card)[\s:=]+[\"']?([A-Za-z0-9_\-\.]{12,})[\"']?",
                    re.IGNORECASE
                )

                if isinstance(items, list):
                    for item in items:
                        key = str(item.get("key", "")).strip().lower().replace(" ", "_")
                        val = str(item.get("value", "")).strip()
                        cat = str(item.get("category", "general")).strip().lower()

                        if not key or not val:
                            continue

                        # Security check: redact or skip sensitive credentials
                        if sensitive_pattern.search(val) or sensitive_pattern.search(key):
                            logger.warning("Redacting sensitive credential token from background memory inscription: %s", key)
                            continue

                        # Dual-crystallization:
                        # 1. User preferences & habits go to Tier 1 core user preferences (loaded every turn)
                        if cat in ("preference", "habit", "identity") or "preference" in key or "habit" in key:
                            self.update_user_preference(key, val)

                        # 2. All items are permanently recorded into Tier 2 vector memory with sqlite-vec & FTS5
                        self.remember_fact(key, val, cat)
                        logger.info("Learned context about Sir: [%s] %s -> %s", cat, key, val)

            except Exception as e:
                logger.debug("%s background memory extraction notice: %s", provider, e)
            finally:
                with self._learning_lock:
                    self._learning_in_flight = False

        threading.Thread(target=_extract, daemon=True).start()

    def get_context_summary(self, query: str | None = None) -> str:
        """Generates a structured, categorized summary of remembered facts for the LLM prompt.
        If a query is provided, semantically prioritizes matched knowledge at the top."""
        if not query:
            with self._context_summary_lock:
                if self._context_summary_cache is not None:
                    return self._context_summary_cache

        facts = self.get_all_facts()
        if not facts:
            summary = "No prior facts recorded."
            if not query:
                with self._context_summary_lock:
                    self._context_summary_cache = summary
            return summary

        semantic_section = ""
        if query:
            matched = self.hybrid_search_facts(query, top_k=4, max_l2_distance=0.95)
            if matched:
                lines = [f"• {m['key'].replace('_', ' ').capitalize()}: {m['value']}" for m in matched]
                semantic_section = "[Directly Relevant Knowledge to Current Context]\n" + "\n".join(lines) + "\n\n"

        by_cat = {}
        for f in facts:
            cat = f.get("category", "general").capitalize()
            by_cat.setdefault(cat, []).append(f"{f['key'].replace('_', ' ').capitalize()}: {f['value']}")

        sections = []
        for cat, items in sorted(by_cat.items()):
            max_items = 4 if query else 8
            bounded_items = items[:max_items]
            sections.append(f"[{cat}]\n" + "\n".join(f"• {item}" for item in bounded_items))
        summary = semantic_section + "\n\n".join(sections)
        if not query:
            with self._context_summary_lock:
                self._context_summary_cache = summary
        return summary

    def record_episodic_lesson(
        self,
        tool_name: str,
        trigger_context: str,
        failure_signature: str,
        lesson: str,
    ) -> int:
        """Records an episodic failure post-mortem or heuristic lesson in SQLite WAL."""
        t_clean = sanitize_memory_taint((tool_name or "").strip())
        f_clean = sanitize_memory_taint((failure_signature or "").strip()[:300])
        c_clean = sanitize_memory_taint((trigger_context or "").strip()[:300])
        l_clean = sanitize_memory_taint((lesson or "").strip())

        if not t_clean or not l_clean:
            return 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO episodic_lessons (
                    tool_name, trigger_context, failure_signature, lesson, hit_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(tool_name, failure_signature) DO UPDATE SET
                    hit_count = episodic_lessons.hit_count + 1,
                    lesson = excluded.lesson,
                    trigger_context = excluded.trigger_context,
                    updated_at = CURRENT_TIMESTAMP
            """, (t_clean, c_clean, f_clean, l_clean))
            cursor.execute("SELECT id FROM episodic_lessons WHERE tool_name = ? AND failure_signature = ?", (t_clean, f_clean))
            row = cursor.fetchone()
            lesson_id = row["id"] if row else cursor.lastrowid

            # Index into lessons_vec
            try:
                import sqlite_vec
                embed_text = f"{t_clean} {c_clean} {f_clean} {l_clean}"
                vec = self.vector_engine.dense_vectorize(embed_text)
                blob = sqlite_vec.serialize_float32(vec)
                cursor.execute("INSERT OR REPLACE INTO lessons_vec(lesson_id, embedding) VALUES (?, ?)", (lesson_id, blob))
            except Exception as e:
                logger.debug(f"sqlite-vec lesson indexing notice: {e}")

            conn.commit()
            logger.info(f"Recorded episodic lesson #{lesson_id} for tool '{t_clean}': {l_clean[:60]}...")
            return lesson_id

    def recall_episodic_lessons(
        self,
        query: str,
        tool_name: str | None = None,
        top_k: int = 3,
        min_sim: float = 0.55,
        max_l2_distance: float = 0.90,
        k_rrf: int = 60,
    ) -> list[dict[str, Any]]:
        """Recalls relevant episodic lessons using Hybrid RRF search (FTS5 BM25 + sqlite-vec KNN)."""
        if not query:
            return []

        fts_matches = []
        sanitized = sanitize_fts5_query(query)
        if sanitized:
            try:
                with self._get_ro_connection() as conn:
                    cursor = conn.cursor()
                    sql = """
                        SELECT l.id, l.tool_name, l.trigger_context, l.failure_signature, l.lesson, l.hit_count
                        FROM episodic_lessons_fts lf
                        JOIN episodic_lessons l ON lf.rowid = l.id
                        WHERE episodic_lessons_fts MATCH ?
                    """
                    params = [sanitized]
                    if tool_name:
                        sql += " AND l.tool_name = ?"
                        params.append(tool_name.strip())
                    sql += " ORDER BY bm25(episodic_lessons_fts) ASC LIMIT ?"
                    params.append(max(top_k * 2, 8))
                    cursor.execute(sql, params)
                    fts_matches = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                logger.debug(f"FTS5 lessons search fallback: {e}")

        vec_matches = []
        try:
            import sqlite_vec
            q_vec = self.vector_engine.embed_query(query)
            q_blob = sqlite_vec.serialize_float32(q_vec)
            with self._get_ro_connection() as conn:
                cursor = conn.cursor()
                sql = """
                    SELECT l.id, l.tool_name, l.trigger_context, l.failure_signature, l.lesson, l.hit_count, lv.distance
                    FROM lessons_vec lv
                    JOIN episodic_lessons l ON lv.lesson_id = l.id
                    WHERE lv.embedding MATCH ? AND k = ?
                """
                params = [q_blob, max(top_k * 2, 8)]
                if tool_name:
                    sql += " AND l.tool_name = ?"
                    params.append(tool_name.strip())
                sql += " ORDER BY lv.distance ASC"
                cursor.execute(sql, params)
                for r in cursor.fetchall():
                    dist = float(r["distance"])
                    if dist <= max_l2_distance:
                        cos_sim = max(0.0, 1.0 - ((dist * dist) / 2.0))
                        if cos_sim >= min_sim:
                            item = dict(r)
                            item["sim"] = round(cos_sim, 3)
                            vec_matches.append(item)
        except Exception as e:
            logger.debug(f"sqlite-vec lessons KNN search fallback: {e}")

        scores: dict[int, float] = {}
        lesson_map: dict[int, dict[str, Any]] = {}

        for rank, item in enumerate(fts_matches, start=1):
            l_id = item["id"]
            scores[l_id] = scores.get(l_id, 0.0) + (1.0 / (k_rrf + rank))
            lesson_map[l_id] = item

        for rank, item in enumerate(vec_matches, start=1):
            l_id = item["id"]
            scores[l_id] = scores.get(l_id, 0.0) + (1.0 / (k_rrf + rank))
            if l_id not in lesson_map:
                lesson_map[l_id] = item

        sorted_ids = sorted(scores.keys(), key=lambda lid: scores[lid], reverse=True)
        results = []
        for l_id in sorted_ids[:top_k]:
            res_item = dict(lesson_map[l_id])
            res_item["rrf_score"] = round(scores[l_id], 4)
            results.append(res_item)
        return results

    def consolidate_sleep_phase(
        self,
        is_user_active_fn: Callable[[], bool] | None = None,
        batch_size: int = 20,
        extractor_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Executes zero-contention sleep-phase memory consolidation on WAL snapshot."""
        from .sleep_consolidator import SleepPhaseConsolidator
        consolidator = SleepPhaseConsolidator(str(self.db_path), self.vector_engine)
        return consolidator.run_sleep_consolidation(
            is_user_active_fn=is_user_active_fn,
            batch_size=batch_size,
            extractor_fn=extractor_fn,
        )

    def distill_transcripts(
        self,
        max_turns: int = 25,
        is_user_active_fn: Callable[[], bool] | None = None,
        extractor_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Harvests unprocessed dialogue turns, scrubs credentials, and distills traits via LLM."""
        from .transcript_distiller import TranscriptDistiller
        distiller = TranscriptDistiller(str(self.db_path), self.vector_engine, self.llm_config, memory_manager=self)
        return distiller.distill_unprocessed_interactions(
            is_user_active_fn=is_user_active_fn,
            max_turns=max_turns,
            extractor_fn=extractor_fn,
        )
