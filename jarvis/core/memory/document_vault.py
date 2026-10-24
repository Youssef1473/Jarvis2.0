"""Document intake queue, knowledge document archival, semantic search, and chunk sandwiching."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from jarvis.core.memory.vector_engine import (
    fuse_vec_and_fts_rrf,
    sanitize_fts5_query,
)

logger = logging.getLogger("Jarvis.Memory.DocumentVault")


class DocumentVaultMixin:
    """Provides document staging, JIT archival, hybrid retrieval, and reference block formatting."""

    def stage_document(self, filename: str, file_path: str, file_hash: str = "", file_size: int = 0) -> Dict[str, Any]:
        """Registers an uploaded document into the staged queue awaiting user context."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO staged_documents (filename, file_path, file_hash, file_size, status)
                VALUES (?, ?, ?, ?, 'PENDING_CONTEXT')
            """, (filename.strip(), file_path.strip(), file_hash, file_size))
            conn.commit()
            doc_id = cursor.lastrowid
            return {
                "id": doc_id,
                "filename": filename.strip(),
                "file_path": file_path.strip(),
                "status": "PENDING_CONTEXT",
            }

    def get_staged_documents(self, status: str = "PENDING_CONTEXT") -> List[Dict[str, Any]]:
        """Retrieves documents waiting in the staged queue."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            if status == "ALL":
                cursor.execute("SELECT * FROM staged_documents ORDER BY id DESC")
            else:
                cursor.execute("SELECT * FROM staged_documents WHERE status = ? ORDER BY id DESC", (status,))
            return [dict(row) for row in cursor.fetchall()]

    def get_staged_document(self, doc_id: Optional[int] = None, filename: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Finds a specific staged document or the most recent pending one."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            if doc_id:
                cursor.execute("SELECT * FROM staged_documents WHERE id = ?", (doc_id,))
            elif filename:
                cursor.execute(
                    "SELECT * FROM staged_documents WHERE LOWER(filename) LIKE ? AND status = 'PENDING_CONTEXT' ORDER BY id DESC LIMIT 1",
                    (f"%{filename.lower().strip()}%",)
                )
            else:
                cursor.execute("SELECT * FROM staged_documents WHERE status = 'PENDING_CONTEXT' ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    def mark_document_processed(self, doc_id: int, user_context: str = "") -> None:
        """Marks a staged document as processed with the provided context."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE staged_documents
                SET status = 'PROCESSED', user_context = ?, processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (user_context, doc_id))
            conn.commit()

    def save_document_knowledge(
        self,
        title: str,
        category: str = "general",
        summary: str = "",
        raw_content: str = "",
        file_path: str = "",
        source_provenance: str = "unknown",
    ) -> int:
        """Archives document content and structured summary into the persistent knowledge archive."""
        from jarvis.core.security_boundaries import TurnTaintTracker
        norm_prov = TurnTaintTracker.normalize_document_provenance(source_provenance)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO documents (title, category, summary, raw_content, file_path, source_provenance)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                title.strip(),
                category.lower().strip(),
                summary.strip(),
                raw_content.strip(),
                file_path.strip(),
                norm_prov,
            ))
            doc_id = cursor.lastrowid

            # Index into documents_vec
            try:
                import sqlite_vec
                raw_snippet = (raw_content or "")[:500]
                text = f"{title} {category} {summary} {raw_snippet}"
                vec = self.vector_engine.dense_vectorize(text)
                blob = sqlite_vec.serialize_float32(vec)
                cursor.execute("INSERT OR REPLACE INTO documents_vec(doc_id, embedding) VALUES (?, ?)", (doc_id, blob))
            except Exception as e:
                logger.debug("sqlite-vec document indexing notice: %s", e)

            conn.commit()
            return doc_id

    def search_documents(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Searches archived document summaries and titles using FTS5 BM25 with LIKE fallback."""
        sanitized = sanitize_fts5_query(query)
        if sanitized:
            try:
                with self._get_ro_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT d.id, d.title, d.category, d.summary, d.file_path, d.source_provenance, d.created_at
                        FROM documents_fts df
                        JOIN documents d ON df.rowid = d.id
                        WHERE documents_fts MATCH ?
                        ORDER BY bm25(documents_fts) ASC
                        LIMIT ?
                    """, (sanitized, limit))
                    rows = [dict(row) for row in cursor.fetchall()]
                    if rows:
                        return rows
            except Exception as e:
                logger.debug("FTS5 documents search fallback to LIKE: %s", e)

        # Fallback to standard LIKE matching
        q = f"%{query.lower().strip()}%"
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, file_path, source_provenance, created_at
                FROM documents
                WHERE LOWER(title) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(category) LIKE ?
                ORDER BY id DESC LIMIT ?
            """, (q, q, q, limit))
            return [dict(row) for row in cursor.fetchall()]

    def semantic_search_documents(self, query: str, top_k: int = 5, min_score: float = 0.10) -> List[Dict[str, Any]]:
        """Finds archived documents semantically relevant to query using sqlite-vec KNN search with TF-IDF fallback."""
        try:
            import sqlite_vec
            q_vec = self.vector_engine.embed_query(query)
            q_blob = sqlite_vec.serialize_float32(q_vec)
            with self._get_ro_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT d.id, d.title, d.category, d.summary, d.file_path, d.source_provenance, d.created_at, dv.distance
                    FROM documents_vec dv
                    JOIN documents d ON dv.doc_id = d.id
                    WHERE dv.embedding MATCH ? AND k = ?
                    ORDER BY dv.distance ASC
                """, (q_blob, max(1, top_k * 2)))
                knn_rows = cursor.fetchall()
                if knn_rows:
                    results = []
                    for row in knn_rows:
                        dist = float(row["distance"])
                        sim = max(0.0, 1.0 - (dist / 2.0))
                        if sim >= min_score:
                            results.append({
                                "id": row["id"],
                                "title": row["title"],
                                "category": row["category"],
                                "summary": row["summary"],
                                "file_path": row["file_path"],
                                "source_provenance": row["source_provenance"] if "source_provenance" in row.keys() else "unknown",
                                "created_at": row["created_at"],
                                "relevance_score": round(sim, 3),
                                "distance": round(dist, 4),
                            })
                    if results:
                        results.sort(key=lambda x: x["relevance_score"], reverse=True)
                        return results[:top_k]
        except Exception as e:
            logger.debug("sqlite-vec documents KNN search fallback: %s", e)

        # In-Memory TF-IDF Fallback
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, title, category, summary, file_path, source_provenance, created_at, raw_content FROM documents ORDER BY id DESC LIMIT 100")
            docs = [dict(r) for r in cursor.fetchall()]

        if not docs:
            return []

        corpus = [f"{d['title']} {d['category']} {d['summary']} {(d.get('raw_content') or '')[:500]}" for d in docs]
        idf = self.vector_engine.compute_idf(corpus)
        query_vec = self.vector_engine.vectorize(query, idf)
        if not query_vec:
            return []

        scored = []
        for d, doc_text in zip(docs, corpus):
            doc_vec = self.vector_engine.vectorize(doc_text, idf)
            score = self.vector_engine.cosine_similarity(query_vec, doc_vec)
            if score >= min_score:
                d_copy = {k: v for k, v in d.items() if k != "raw_content"}
                d_copy["relevance_score"] = round(score, 3)
                scored.append(d_copy)

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored[:top_k]

    def fuse_vec_and_fts_rrf(
        self,
        vec_results: List[Dict[str, Any]],
        fts_results: List[Dict[str, Any]],
        k: int = 60,
        max_l2_distance: float = 0.78,
    ) -> List[Dict[str, Any]]:
        """Fuses vector KNN and FTS5 BM25 search results via Reciprocal Rank Fusion (RRF)."""
        return fuse_vec_and_fts_rrf(vec_results, fts_results, k=k, max_l2_distance=max_l2_distance)

    def hybrid_search_documents(self, query: str, limit: int = 5, k_rrf: int = 60, max_l2_distance: float = 0.78) -> List[Dict[str, Any]]:
        """Hybrid search combining FTS5 BM25 and sqlite-vec KNN via Reciprocal Rank Fusion (RRF)."""
        fts_results = self.search_documents(query, limit=max(limit * 2, 10))
        vec_results = self.semantic_search_documents(query, top_k=max(limit * 2, 10))
        fused = fuse_vec_and_fts_rrf(vec_results, fts_results, k=k_rrf, max_l2_distance=max_l2_distance)
        return fused[:limit]

    async def search_documents_async(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Asynchronously searches archived documents via FTS5 BM25 with LIKE fallback."""
        sanitized = sanitize_fts5_query(query)
        pool = self.get_vector_pool()
        if sanitized:
            try:
                def _fts_query(conn: sqlite3.Connection):
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT d.id, d.title, d.category, d.summary, d.file_path, d.source_provenance, d.created_at
                        FROM documents_fts ft
                        JOIN documents d ON ft.rowid = d.id
                        WHERE documents_fts MATCH ?
                        ORDER BY bm25(documents_fts) ASC
                        LIMIT ?
                    """, (sanitized, limit))
                    return [dict(row) for row in cursor.fetchall()]

                rows = await pool.execute_read_async(_fts_query)
                if rows:
                    return rows
            except Exception as e:
                logger.debug("Async FTS5 documents search fallback: %s", e)

        q = f"%{query.lower().strip()}%"
        def _like_query(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, file_path, source_provenance, created_at
                FROM documents
                WHERE title LIKE ? OR summary LIKE ? OR category LIKE ?
                ORDER BY created_at DESC LIMIT ?
            """, (q, q, q, limit))
            return [dict(row) for row in cursor.fetchall()]

        return await pool.execute_read_async(_like_query)

    async def semantic_search_documents_async(
        self, query: str, top_k: int = 5, min_score: float = 0.10
    ) -> List[Dict[str, Any]]:
        """Asynchronously finds archived documents semantically relevant to query using VectorMemoryPool."""
        pool = self.get_vector_pool()
        try:
            import sqlite_vec
            q_vec = await asyncio.to_thread(self.vector_engine.embed_query, query)
            q_blob = sqlite_vec.serialize_float32(q_vec)

            def _knn_query(conn: sqlite3.Connection):
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT d.id, d.title, d.category, d.summary, d.file_path, d.source_provenance, d.created_at, dv.distance
                    FROM documents_vec dv
                    JOIN documents d ON dv.doc_id = d.id
                    WHERE dv.embedding MATCH ? AND k = ?
                    ORDER BY dv.distance ASC
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
                            "id": row["id"],
                            "title": row["title"],
                            "category": row["category"],
                            "summary": row["summary"],
                            "file_path": row["file_path"],
                            "source_provenance": row["source_provenance"] if "source_provenance" in row.keys() else "unknown",
                            "created_at": row["created_at"],
                            "relevance_score": round(sim, 3),
                            "distance": round(dist, 4),
                        })
                if results:
                    results.sort(key=lambda x: x["relevance_score"], reverse=True)
                    return results[:top_k]
        except Exception as e:
            logger.debug("Async sqlite-vec documents KNN search fallback: %s", e)

        return await asyncio.to_thread(self.semantic_search_documents, query, top_k, min_score)

    async def hybrid_search_documents_async(
        self, query: str, limit: int = 5, k_rrf: int = 60, max_l2_distance: float = 0.78
    ) -> List[Dict[str, Any]]:
        """Asynchronously performs hybrid search combining FTS5 BM25 and sqlite-vec KNN via RRF."""
        fts_task = asyncio.create_task(self.search_documents_async(query, limit=max(limit * 2, 10)))
        vec_task = asyncio.create_task(self.semantic_search_documents_async(query, top_k=max(limit * 2, 10)))
        fts_results, vec_results = await asyncio.gather(fts_task, vec_task)
        fused = fuse_vec_and_fts_rrf(vec_results, fts_results, k=k_rrf, max_l2_distance=max_l2_distance)
        return fused[:limit]

    def get_document_by_id(self, doc_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single archived document by its primary key ID."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, raw_content, file_path, source_provenance, created_at
                FROM documents WHERE id = ?
            """, (doc_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_recent_documents(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Retrieves recently archived documents."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, raw_content, file_path, source_provenance, created_at
                FROM documents ORDER BY id DESC LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def format_document_reference_block(self, docs: List[Dict[str, Any]]) -> str:
        """Formats retrieved document chunks using Chunk Sandwiching and metadata headers under inert XML tags.
        Bypasses 'Lost in the Middle' attention degradation by placing highest-relevance chunks at the boundaries."""
        if not docs:
            return ""

        sorted_docs = sorted(
            docs,
            key=lambda x: x.get("rrf_score", x.get("relevance_score", 0.0)),
            reverse=True
        )

        chunks = []
        for d in sorted_docs:
            title = d.get("title", "Untitled Document")
            cat = d.get("category", "general")
            date = d.get("created_at", "Unknown Date")
            summary = d.get("summary", "").strip()
            raw = (d.get("raw_content") or "").strip()
            content = summary if summary else (raw[:600] if raw else "No content summary recorded.")
            header = f"[Document #{d.get('id', '?')}: {title} | Category: {cat} | Archived: {date}]"
            chunks.append(f"{header}\n{content}")

        inner = "\n\n---\n\n".join(chunks)
        return f"<reference_data>\n{inner}\n</reference_data>"
