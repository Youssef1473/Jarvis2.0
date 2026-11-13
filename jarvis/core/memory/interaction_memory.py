"""Interaction logging, autonomous context auto-learning, episodic reflection, and sleep phase consolidation."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from jarvis.core.memory.vector_engine import (
    sanitize_fts5_query,
    sanitize_memory_taint,
    scrub_sensitive_credentials,
)

logger = logging.getLogger("Jarvis.Memory.InteractionMemory")


def _scrub_tool_call_value(val: Any) -> Any:
    """Recursively traverses nested tool-call structures and scrubs sensitive credentials from string values."""
    if isinstance(val, str):
        return scrub_sensitive_credentials(val)
    if isinstance(val, dict):
        return {k: _scrub_tool_call_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_scrub_tool_call_value(item) for item in val]
    return val


def _sanitize_tool_calls_payload(tool_calls: Any) -> str:
    """Sanitizes tool-call payloads before database persistence, preserving JSON serialization and structure."""
    if tool_calls is None:
        return ""
    if isinstance(tool_calls, (list, dict)):
        try:
            scrubbed_obj = _scrub_tool_call_value(tool_calls)
            return json.dumps(scrubbed_obj)
        except Exception:  # noqa: BLE001
            return scrub_sensitive_credentials(str(tool_calls))
    if isinstance(tool_calls, str):
        stripped = tool_calls.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(tool_calls)
                if isinstance(parsed, (list, dict)):
                    scrubbed_obj = _scrub_tool_call_value(parsed)
                    return json.dumps(scrubbed_obj)
            except Exception:  # noqa: BLE001, S110
                pass
        return scrub_sensitive_credentials(tool_calls)
    return scrub_sensitive_credentials(str(tool_calls))


class InteractionMemoryMixin:
    """Provides interaction turn recording, auto-learning heuristics, and episodic reflection."""

    def log_interaction(
        self,
        user_input: str,
        jarvis_reply: str,
        tool_calls: str | None = None,
        source_provenance: str | None = None,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ):
        """Logs a conversation turn serialized through SingleWriterActor."""
        from jarvis.core.security_boundaries import TurnTaintTracker
        source_provenance = TurnTaintTracker.resolve_write_provenance(source_provenance, default_if_clean="user_supplied")
        clean_user_input = scrub_sensitive_credentials(user_input or "")
        clean_jarvis_reply = scrub_sensitive_credentials(jarvis_reply or "")
        clean_tool_calls = _sanitize_tool_calls_payload(tool_calls)

        sess_id = session_id or (self.get_active_session_id() if hasattr(self, "get_active_session_id") else None)
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else None)

        scope = "session" if sess_id and ws_id else "unscoped"
        if scope == "unscoped":
            sess_id = None
            ws_id = None

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO interactions (workspace_id, session_id, scope, user_input, jarvis_reply, tool_calls, source_provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ws_id, sess_id, scope, clean_user_input, clean_jarvis_reply, clean_tool_calls, source_provenance))

        self.execute_write(_write)

    def get_recent_interactions(
        self,
        limit: int = 8,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieves the most recent N interaction turns in chronological order."""
        sess_id = session_id or (self.get_active_session_id() if hasattr(self, "get_active_session_id") else None)
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            if sess_id:
                cursor.execute("""
                    SELECT id, user_input, jarvis_reply, tool_calls, source_provenance, created_at
                    FROM interactions
                    WHERE session_id = ? AND scope = 'session'
                    ORDER BY id DESC
                    LIMIT ?
                """, (sess_id, limit))
            else:
                cursor.execute("""
                    SELECT id, user_input, jarvis_reply, tool_calls, source_provenance, created_at
                    FROM interactions
                    WHERE scope = 'unscoped'
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_recent_interactions_for_session(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """Retrieves interaction turns strictly isolated to a specific session."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_input, jarvis_reply, tool_calls, source_provenance, created_at
                FROM interactions
                WHERE session_id = ? AND scope = 'session'
                ORDER BY id DESC
                LIMIT ?
            """, (session_id, limit))
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    def search_interactions(
        self,
        query: str,
        limit: int = 5,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Searches past conversation logs by keyword or phrase using FTS5 BM25 with LIKE fallback."""
        sanitized = sanitize_fts5_query(query)
        sess_id = session_id or (self.get_active_session_id() if hasattr(self, "get_active_session_id") else None)
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else None)

        if sanitized:
            try:
                with self._get_ro_connection() as conn:
                    cursor = conn.cursor()
                    if sess_id and ws_id:
                        cursor.execute("""
                            SELECT i.id, i.user_input, i.jarvis_reply, i.tool_calls, i.source_provenance, i.created_at
                            FROM interactions_fts inf
                            JOIN interactions i ON inf.rowid = i.id
                            WHERE interactions_fts MATCH ? AND i.session_id = ? AND i.workspace_id = ? AND i.scope = 'session'
                            ORDER BY bm25(interactions_fts) ASC
                            LIMIT ?
                        """, (sanitized, sess_id, ws_id, limit))
                    else:
                        cursor.execute("""
                            SELECT i.id, i.user_input, i.jarvis_reply, i.tool_calls, i.source_provenance, i.created_at
                            FROM interactions_fts inf
                            JOIN interactions i ON inf.rowid = i.id
                            WHERE interactions_fts MATCH ? AND i.scope = 'unscoped'
                            ORDER BY bm25(interactions_fts) ASC
                            LIMIT ?
                        """, (sanitized, limit))
                    rows = [dict(r) for r in cursor.fetchall()]
                    if rows:
                        return rows
            except Exception as e:  # noqa: BLE001
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
        if sess_id and ws_id:
            where_clause = f"({where_clause}) AND session_id = ? AND workspace_id = ? AND scope = 'session'"
            params.extend([sess_id, ws_id])
        else:
            where_clause = f"({where_clause}) AND scope = 'unscoped'"

        sql = f"""
            SELECT id, user_input, jarvis_reply, tool_calls, source_provenance, created_at
            FROM interactions
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(limit)
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()]

    def purge_session(
        self,
        target_session_id: str,
        target_workspace_id: str | None = None,
        allow_archived: bool = False,
    ) -> dict[str, Any]:
        """Executes a hardened session purge in a strict BEGIN IMMEDIATE transaction."""
        if not isinstance(allow_archived, bool):
            raise TypeError(f"allow_archived must be a strict bool, got {type(allow_archived).__name__}")

        ws_id = target_workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")

        # Step 1: Pre-burn authorization grant before transaction
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        grant_args = {
            "workspace_id": ws_id,
            "session_id": target_session_id,
            "allow_archived": allow_archived,
        }
        grant = approval_mgr.consume_grant_object("purge_session", grant_args)
        if not grant:
            raise PermissionError("Missing, expired, mismatched, or already consumed Dual-Key approval grant for purge_session")

        def _write(conn: sqlite3.Connection) -> dict[str, Any]:
            if conn.in_transaction:
                raise RuntimeError("Integrity Error: Purge transaction cannot inherit an active transaction")

            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.execute("BEGIN IMMEDIATE;")
            if not conn.in_transaction:
                raise RuntimeError("Integrity Error: Transaction not active after BEGIN IMMEDIATE")

            try:
                # 4-State Runtime Context Validation
                cursor.execute("SELECT active_workspace_id, active_session_id FROM active_context WHERE lock_id = 1")
                ctx_row = cursor.fetchone()
                if ctx_row:
                    act_ws = ctx_row[0]
                    act_sess = ctx_row[1]
                    if act_ws is None and act_sess is not None:
                        raise ValueError("State 4 violation: session cannot be active without active workspace")
                    if act_sess == target_session_id:
                        raise PermissionError(f"Target session '{target_session_id}' is currently active in runtime context. Active sessions cannot be purged.")

                # Validate target session status
                cursor.execute("SELECT workspace_id, status FROM sessions WHERE session_id = ?", (target_session_id,))
                row = cursor.fetchone()
                if not row:
                    raise KeyError(f"Session '{target_session_id}' not found")
                actual_ws_id, status = row[0], row[1]

                if actual_ws_id != ws_id:
                    raise ValueError(f"Workspace mismatch: Session belongs to '{actual_ws_id}', not '{ws_id}'")
                if status == "active":
                    raise PermissionError(f"Session '{target_session_id}' has database status 'active'. Only closed or paused sessions can be purged.")
                if status not in {"closed", "paused"}:
                    raise ValueError(f"Session '{target_session_id}' has unrecognized status '{status}'. Fails closed.")

                # Validate workspace archival
                cursor.execute("SELECT is_archived FROM workspaces WHERE workspace_id = ?", (actual_ws_id,))
                ws_row = cursor.fetchone()
                if not ws_row:
                    raise KeyError(f"Workspace '{actual_ws_id}' not found")
                is_archived = bool(ws_row[0])
                if is_archived and not allow_archived:
                    raise PermissionError(f"Workspace '{actual_ws_id}' is archived. Purge requires explicit allow_archived=True approval.")

                # Leaf-to-root deletion
                cursor.execute("DELETE FROM interactions WHERE session_id = ?", (target_session_id,))
                del_interactions = cursor.rowcount

                cursor.execute("DELETE FROM tasks WHERE session_id = ?", (target_session_id,))
                del_tasks = cursor.rowcount

                cursor.execute("DELETE FROM staged_documents WHERE session_id = ?", (target_session_id,))
                del_staged = cursor.rowcount

                cursor.execute("DELETE FROM sessions WHERE session_id = ?", (target_session_id,))
                del_sessions = cursor.rowcount

                # Foreign key check
                cursor.execute("PRAGMA foreign_key_check;")
                fk_violations = cursor.fetchall()
                if fk_violations:
                    raise RuntimeError(f"Foreign key integrity check failed after purge: {fk_violations}")

                conn.execute("COMMIT;")
                return {
                    "status": "purged",
                    "session_id": target_session_id,
                    "deleted_interactions": del_interactions,
                    "deleted_tasks": del_tasks,
                    "deleted_staged_documents": del_staged,
                    "deleted_sessions": del_sessions,
                }
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

        return self.execute_write(_write)

    def semantic_search_interactions(self, query: str, top_k: int = 5, min_score: float = 0.10) -> list[dict[str, Any]]:
        """Searches past conversation history using semantic vector similarity."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_input, jarvis_reply, tool_calls, source_provenance, created_at
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
                    except Exception as e:  # noqa: BLE001
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
                        except Exception as e_oe:  # noqa: BLE001
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

            except Exception as e:  # noqa: BLE001
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
        source_provenance: str | None = None,
        workspace_id: str | None = None,
    ) -> int:
        """Records an episodic failure post-mortem or heuristic lesson in SQLite WAL."""
        t_clean = sanitize_memory_taint((tool_name or "").strip())
        f_clean = sanitize_memory_taint((failure_signature or "").strip()[:300])
        c_clean = sanitize_memory_taint((trigger_context or "").strip()[:300])
        l_clean = scrub_sensitive_credentials(sanitize_memory_taint((lesson or "").strip()))

        if not t_clean or not l_clean:
            return 0

        from jarvis.core.security_boundaries import TurnTaintTracker
        source_provenance = TurnTaintTracker.resolve_write_provenance(source_provenance, default_if_clean="unknown")
        ws_id = workspace_id or (self.get_active_workspace_id() if hasattr(self, "get_active_workspace_id") else "ws_default")

        # Step 1: Pre-compute ONNX dense embedding OUTSIDE write transaction
        blob = None
        try:
            import sqlite_vec
            embed_text = f"{t_clean} {c_clean} {f_clean} {l_clean}"
            vec = self.vector_engine.dense_vectorize(embed_text)
            blob = sqlite_vec.serialize_float32(vec)
        except Exception as e:  # noqa: BLE001
            logger.warning("sqlite-vec episodic lesson vectorization failed (%s); continuing without vector index.", e)

        # Step 2: Route SQLite write strictly through SingleWriterActor
        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, hit_count FROM episodic_lessons
                WHERE workspace_id = ? AND tool_name = ? AND failure_signature = ? AND scope = 'workspace'
            """, (ws_id, t_clean, f_clean))
            row = cursor.fetchone()
            if row:
                lesson_id = row["id"]
                cursor.execute("""
                    UPDATE episodic_lessons
                    SET hit_count = hit_count + 1,
                        lesson = ?,
                        trigger_context = ?,
                        source_provenance = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (l_clean, c_clean, source_provenance, lesson_id))
            else:
                cursor.execute("""
                    INSERT INTO episodic_lessons (
                        workspace_id, scope, tool_name, trigger_context, failure_signature, lesson, hit_count, source_provenance, created_at, updated_at
                    ) VALUES (?, 'workspace', ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (ws_id, t_clean, c_clean, f_clean, l_clean, source_provenance))
                lesson_id = cursor.lastrowid

            if blob is not None:
                try:
                    cursor.execute("DELETE FROM lessons_vec WHERE lesson_id = ?", (lesson_id,))
                    cursor.execute("INSERT INTO lessons_vec(lesson_id, embedding) VALUES (?, ?)", (lesson_id, blob))
                except Exception as e:  # noqa: BLE001
                    logger.warning("sqlite-vec episodic lesson indexing failed (%s); continuing without vector index.", e)

            return lesson_id

        lesson_id = self.execute_write(_write)
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
                        SELECT l.id, l.tool_name, l.trigger_context, l.failure_signature, l.lesson, l.hit_count, l.source_provenance
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
            except Exception as e:  # noqa: BLE001
                logger.debug(f"FTS5 lessons search fallback: {e}")

        vec_matches = []
        try:
            import sqlite_vec
            q_vec = self.vector_engine.embed_query(query)
            q_blob = sqlite_vec.serialize_float32(q_vec)
            with self._get_ro_connection() as conn:
                cursor = conn.cursor()
                sql = """
                    SELECT l.id, l.tool_name, l.trigger_context, l.failure_signature, l.lesson, l.hit_count, l.source_provenance, lv.distance
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
        except Exception as e:  # noqa: BLE001
            logger.warning("sqlite-vec lessons KNN search failed (%s); falling back to lexical TF-IDF/BM25.", e)

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
