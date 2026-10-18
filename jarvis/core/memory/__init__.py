"""Long-term persistent memory management and adaptive self-learning for Jarvis 2.0."""
import asyncio
import contextlib
import datetime
import json
import logging
import math
import os
import queue
from collections import Counter
from pathlib import Path
import re
import sqlite3
import threading
from typing import Dict, Any, List, Optional, Tuple, Callable

logger = logging.getLogger("Jarvis.Memory")

_FTS_STOPWORDS = {
    'what', 'whats', 'who', 'when', 'where', 'why', 'how', 'tell', 'me', 'about',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'we', 'you', 'i', 'he', 'she', 'they', 'it', 'did', 'do', 'does', 'our',
    'my', 'your', 'their', 'his', 'her', 'its', 'that', 'this', 'these', 'those',
    'can', 'could', 'would', 'should', 'will', 'shall', 'may', 'might', 'must',
    'say', 'said', 'saying', 'talk', 'talked', 'talking', 'ask', 'asked', 'asking',
    'please', 'recall', 'remember', 'past', 'conversation', 'previous', 'discussed',
    'regarding', 'concerning', 'related',
    'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'as', 'into',
    'and', 'or', 'not', 'but', 'if', 'then', 'so', 'have', 'has', 'had'
}


def sanitize_fts5_query(query: str) -> Optional[str]:
    """Sanitizes raw user query for safe SQLite FTS5 search.
    Removes punctuation/special FTS operators, filters conversational stopwords,
    and returns OR-delimited prefix wildcard terms, e.g. '"weather"* OR "tomorrow"*'.
    Returns None if no searchable tokens remain."""
    if not query or not isinstance(query, str):
        return None
    cleaned = re.sub(r"[^\w\s]", " ", query.lower())
    words = [w for w in cleaned.split() if len(w) > 1]
    filtered = [w for w in words if w not in _FTS_STOPWORDS]
    tokens = filtered if filtered else words
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens)


_MEMORY_TAINT_REGEX = re.compile(
    r"(?i)(ignore\s+previous|system\s*:|update_user_preference|always\s+run\s+|override\s+instructions)"
)


def sanitize_memory_taint(text: str) -> str:
    """Strips raw untrusted ARIA/DOM prompt-injection patterns before persisting to memory."""
    if not text:
        return ""
    cleaned = _MEMORY_TAINT_REGEX.sub("", str(text))
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def fuse_vec_and_fts_rrf(
    vec_results: List[Dict[str, Any]],
    fts_results: List[Dict[str, Any]],
    k: int = 60,
    max_l2_distance: float = 0.90,
) -> List[Dict[str, Any]]:
    """Fuses vector KNN and FTS5 BM25 search results via Reciprocal Rank Fusion (RRF).

    Applies calibrated L2 distance gating (max_l2_distance = 0.90, S_cos >= 0.595)
    to eliminate orthogonal noise while retaining semantic synonyms.
    """
    scores: Dict[Any, float] = {}
    item_map: Dict[Any, Dict[str, Any]] = {}

    def _get_key(item: Dict[str, Any], default_val: Any) -> Any:
        if "key" in item:
            return item["key"]
        if "id" in item:
            return item["id"]
        if "doc_id" in item:
            return item["doc_id"]
        return default_val

    # 1. RRF score from FTS5 ranking
    for rank, item in enumerate(fts_results, start=1):
        key = _get_key(item, rank)
        scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
        item_map[key] = item

    # 2. Filter vector results by L2 distance gate
    filtered_vec: List[Dict[str, Any]] = []
    for item in vec_results:
        dist = item.get("distance")
        if dist is None:
            dist = item.get("l2_distance")
        if dist is not None and float(dist) > max_l2_distance:
            continue
        filtered_vec.append(item)

    # 3. RRF score from filtered Vector KNN ranking
    for rank, item in enumerate(filtered_vec, start=1):
        key = _get_key(item, rank)
        scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
        if key not in item_map:
            item_map[key] = item

    sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    results = []
    for key in sorted_keys:
        res = dict(item_map[key])
        res["rrf_score"] = round(scores[key], 4)
        results.append(res)
    return results


class VectorMemoryEngine:
    """Local vector recall engine using TF-IDF subword and token n-gram vectors with cosine similarity.
    Runs 100% locally with zero external network or model download dependencies in <1ms."""

    SEMANTIC_SYNONYMS = {
        "editor": ["ide", "vscode", "code", "programming", "sublime", "pycharm", "cursor", "neovim", "vim"],
        "ide": ["editor", "vscode", "code", "development", "programming"],
        "code": ["programming", "script", "developer", "ide", "editor", "software"],
        "favorite": ["preferred", "best", "liked", "fav", "top", "choice"],
        "preference": ["favorite", "preferred", "setting", "likes", "choice"],
        "music": ["song", "audio", "spotify", "playlist", "track", "tunes", "artist"],
        "work": ["job", "project", "career", "task", "company", "office", "business"],
        "home": ["house", "apartment", "residence", "address", "location"],
        "car": ["vehicle", "automobile", "drive", "transport"],
        "drink": ["coffee", "tea", "water", "beverage"],
        "food": ["meal", "lunch", "dinner", "breakfast", "snack"],
        "os": ["operating system", "windows", "linux", "mac", "wsl"],
        "city": ["town", "location", "country", "place", "residence"],
        "watchdog": ["sentinel", "monitor", "telemetry", "hardware", "vitals"],
        "note": ["notes", "memo", "reminder", "scratchpad", "jot"],
    }

    def tokenize(self, text: str) -> List[str]:
        """Extracts word tokens, semantic aliases, and subword n-grams."""
        if not text:
            return []
        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        words = [w for w in cleaned.split() if len(w) > 1]

        tokens = list(words)
        for w in words:
            syns = self.SEMANTIC_SYNONYMS.get(w, [])
            tokens.extend(syns)

        # Add character 3-grams and 4-grams for root matching and typo resilience
        for w in words:
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens.append(f"#{w[i:i+3]}")
            if len(w) >= 4:
                for i in range(len(w) - 3):
                    tokens.append(f"##{w[i:i+4]}")

        return tokens

    def vectorize(self, text: str, idf_weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """Calculates L2-normalized TF-IDF vector for a given text."""
        tokens = self.tokenize(text)
        if not tokens:
            return {}
        counts = Counter(tokens)
        total = len(tokens)

        vec = {}
        for token, count in counts.items():
            tf = count / total
            idf = idf_weights.get(token, 1.0) if idf_weights else 1.0
            vec[token] = tf * idf

        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for k in vec:
                vec[k] /= norm
        return vec

    def compute_idf(self, corpus: List[str]) -> Dict[str, float]:
        """Computes inverse document frequency across the corpus."""
        n_docs = len(corpus)
        if n_docs == 0:
            return {}
        doc_freq = Counter()
        for doc in corpus:
            unique_tokens = set(self.tokenize(doc))
            for tok in unique_tokens:
                doc_freq[tok] += 1

        idf = {}
        for tok, df in doc_freq.items():
            idf[tok] = math.log((1 + n_docs) / (1 + df)) + 1.0
        return idf

    @staticmethod
    def cosine_similarity(vec1: Dict[str, float], vec2: Dict[str, float]) -> float:
        """Computes cosine similarity between two unit-normalized sparse vectors."""
        if not vec1 or not vec2:
            return 0.0
        if len(vec1) > len(vec2):
            vec1, vec2 = vec2, vec1
        return sum(val * vec2.get(k, 0.0) for k, val in vec1.items())

    def dense_vectorize(self, text: str, dim: Optional[int] = None, is_query: bool = False) -> List[float]:
        """Calculates deterministic L2-normalized float vector (default native 384-dim) via OnnxEmbeddingEngine."""
        from jarvis.core.memory.onnx_embeddings import OnnxEmbeddingEngine, DEFAULT_EMBEDDING_DIM
        target_dim = dim if dim and dim > 0 else DEFAULT_EMBEDDING_DIM
        engine = OnnxEmbeddingEngine.get_instance(dim=target_dim)
        return engine.embed(text, is_query=is_query, dim=target_dim)

    def embed_query(self, query: str, dim: Optional[int] = None) -> List[float]:
        """Calculates query vector with asymmetric search prefix."""
        return self.dense_vectorize(query, dim=dim, is_query=True)

    def embed_batch(self, texts: List[str], batch_size: int = 64, is_query: bool = False, dim: Optional[int] = None) -> List[List[float]]:
        """Calculates batch embedding vectors using tokenizer.encode_batch()."""
        from jarvis.core.memory.onnx_embeddings import OnnxEmbeddingEngine, DEFAULT_EMBEDDING_DIM
        target_dim = dim if dim and dim > 0 else DEFAULT_EMBEDDING_DIM
        engine = OnnxEmbeddingEngine.get_instance(dim=target_dim)
        return engine.embed_batch(texts, batch_size=batch_size, is_query=is_query, dim=target_dim)

    def get_backend_name(self) -> str:
        """Returns the active embedding backend identifier string."""
        from jarvis.core.memory.onnx_embeddings import OnnxEmbeddingEngine
        return OnnxEmbeddingEngine.get_instance().get_backend_name()


class SingleWriterActor:
    """Serializes all write operations (INSERT, UPDATE, DELETE) through an asynchronous queue worker.
    Guarantees zero SQLITE_BUSY write lock contention and atomic transaction sequencing.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._queue: queue.Queue[Optional[Tuple[Callable[[sqlite3.Connection], Any], threading.Event, list, list]]] = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="JarvisSingleWriter")
        self._thread.start()

    def _create_conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute("PRAGMA mmap_size=268435456;")
        c.execute("PRAGMA cache_size=-64000;")
        c.execute("PRAGMA temp_store=MEMORY;")
        c.row_factory = sqlite3.Row
        try:
            import sqlite_vec
            c.enable_load_extension(True)
            sqlite_vec.load(c)
        except Exception:
            pass
        return c

    def _worker_loop(self) -> None:
        key = str(self.db_path.resolve())
        is_temp = "tmp" in key.lower() or "temp" in key.lower() or self.db_path.name.startswith("test_")

        conn: Optional[sqlite3.Connection] = None
        if not is_temp:
            try:
                conn = self._create_conn()
            except Exception as e:
                logger.debug("SingleWriterActor persistent conn init note: %s", e)

        while self._running:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break

            func, done_evt, res_box, err_box = item
            local_conn = conn
            opened_temp = False
            if local_conn is None:
                try:
                    local_conn = self._create_conn()
                    opened_temp = True
                except Exception as ce:
                    err_box.append(ce)
                    done_evt.set()
                    self._queue.task_done()
                    continue

            try:
                with local_conn:
                    result = func(local_conn)
                res_box.append(result)
            except Exception as e:
                err_box.append(e)
            finally:
                if opened_temp and local_conn is not None:
                    try:
                        local_conn.close()
                    except Exception:
                        pass
                done_evt.set()
                self._queue.task_done()

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def execute_write(self, func: Callable[[sqlite3.Connection], Any], timeout: float = 30.0) -> Any:
        done_evt = threading.Event()
        res_box: list = []
        err_box: list = []
        self._queue.put((func, done_evt, res_box, err_box))
        if not done_evt.wait(timeout=timeout):
            raise TimeoutError("Single-writer queue timeout exceeded")
        if err_box:
            raise err_box[0]
        return res_box[0] if res_box else None

    async def execute_write_async(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        return await asyncio.to_thread(self.execute_write, func)

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


class MemoryManager:
    """Stores user preferences, facts, tasks, and conversation history in SQLite with adaptive learning."""

    def __init__(self, config: Dict[str, Any]):
        self.llm_config = config.get("llm", {})
        memory_config = config.get("memory", {})
        self.background_learning_enabled = bool(memory_config.get("background_learning", False))
        self._learning_lock = threading.Lock()
        self._learning_in_flight = False
        self._context_summary_lock = threading.Lock()
        self._context_summary_cache: Optional[str] = None
        self._write_lock = threading.RLock()
        base_dir = Path(config.get("_base_dir", "."))
        db_rel_path = memory_config.get("db_path", "data/jarvis_memory.db")
        self.db_path = base_dir / db_rel_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_engine = VectorMemoryEngine()
        self._writer_actor = SingleWriterActor(self.db_path)
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        with self._write_lock:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA mmap_size=268435456;")
            conn.execute("PRAGMA cache_size=-64000;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.row_factory = sqlite3.Row
            try:
                import sqlite_vec
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception:
                pass
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    @contextlib.contextmanager
    def _get_ro_connection(self):
        """Thread-local read-only SQLite connection from ReadOnlyConnectionPool."""
        from jarvis.memory.ro_pool import get_ro_connection
        with get_ro_connection(self.db_path) as conn:
            yield conn

    def execute_write(self, func: Callable[[sqlite3.Connection], Any], timeout: float = 30.0) -> Any:
        """Executes a write operation serialized through the Single-Writer Actor queue."""
        return self._writer_actor.execute_write(func, timeout=timeout)

    async def execute_write_async(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        """Asynchronously dispatches a write operation through the Single-Writer Actor queue."""
        return await self._writer_actor.execute_write_async(func)

    def close(self) -> None:
        """Cleans up writer actor and read-only pool connections."""
        if hasattr(self, "_writer_actor") and self._writer_actor:
            self._writer_actor.stop()
        try:
            from jarvis.memory.ro_pool import ReadOnlyConnectionPool
            ReadOnlyConnectionPool.get_instance().close_db(self.db_path)
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()


    def _init_db(self):
        """Initializes database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Facts & User Profile
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT DEFAULT 'general',
                    key TEXT UNIQUE,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Tasks & Reminders
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    description TEXT NOT NULL,
                    due_date TEXT,
                    is_completed BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Interaction History
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_input TEXT NOT NULL,
                    jarvis_reply TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Calendar Events
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    location TEXT,
                    is_cancelled BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Staged Documents (Context-Gated Intake)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS staged_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT,
                    file_size INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'PENDING_CONTEXT',
                    user_context TEXT,
                    staged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP
                )
            """)
            # Knowledge Documents Archive
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    summary TEXT,
                    raw_content TEXT,
                    file_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Episodic Lessons & Self-Correction Buffer (Tier 3 Reflexion)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS episodic_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    trigger_context TEXT NOT NULL,
                    failure_signature TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    hit_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tool_name, failure_signature)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_lessons_tool ON episodic_lessons(tool_name)")

            # 1. Bi-Temporal Knowledge Graph Edges (Replaces flat facts with bi-temporal validity)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_edges (
                    edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject         TEXT NOT NULL COLLATE NOCASE,
                    predicate       TEXT NOT NULL COLLATE NOCASE,
                    object          TEXT NOT NULL,
                    is_functional   INTEGER NOT NULL DEFAULT 1,
                    confidence      REAL NOT NULL DEFAULT 1.0,
                    access_count    INTEGER NOT NULL DEFAULT 1,
                    decay_lambda    REAL NOT NULL DEFAULT 0.01,
                    valid_from      REAL NOT NULL,
                    valid_until     REAL DEFAULT NULL,
                    last_accessed   REAL NOT NULL,
                    superseded_by   INTEGER DEFAULT NULL REFERENCES knowledge_edges(edge_id),
                    source_context  TEXT DEFAULT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_kg_active_lookup 
                ON knowledge_edges(subject, predicate) 
                WHERE valid_until IS NULL
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_kg_supersede_functional
                BEFORE INSERT ON knowledge_edges
                WHEN NEW.is_functional = 1 AND NEW.valid_until IS NULL
                BEGIN
                    UPDATE knowledge_edges
                    SET valid_until = NEW.valid_from
                    WHERE subject = NEW.subject
                      AND predicate = NEW.predicate
                      AND valid_until IS NULL
                      AND object != NEW.object;
                END
            """)

            # 2. Time-Conditioned Markov Habit Graph
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS habit_transitions (
                    src_event       TEXT NOT NULL,
                    dst_event       TEXT NOT NULL,
                    hour_bucket     INTEGER NOT NULL,
                    is_weekend      INTEGER NOT NULL,
                    decayed_weight  REAL NOT NULL DEFAULT 1.0,
                    sample_count    INTEGER NOT NULL DEFAULT 1,
                    last_observed   REAL NOT NULL,
                    PRIMARY KEY (src_event, dst_event, hour_bucket, is_weekend)
                ) WITHOUT ROWID
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_habit_predict 
                ON habit_transitions(src_event, hour_bucket, is_weekend, decayed_weight DESC)
            """)

            # 3. Level-1 Reversible Action Undo Journal
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS action_journal (
                    action_id       TEXT PRIMARY KEY,
                    timestamp       REAL NOT NULL,
                    skill_name      TEXT NOT NULL,
                    risk_level      INTEGER NOT NULL DEFAULT 1,
                    inverse_skill   TEXT NOT NULL,
                    inverse_payload TEXT NOT NULL,
                    reverted        INTEGER NOT NULL DEFAULT 0
                )
            """)

            # 4. Core User Preferences (Tier 1 Sovereign Operational Profile)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    pref_key TEXT PRIMARY KEY NOT NULL,
                    pref_value TEXT NOT NULL,
                    dimension_id TEXT NOT NULL,
                    locked_by_sir INTEGER NOT NULL DEFAULT 1,
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ) WITHOUT ROWID
            """)

            # SQLite FTS5 Virtual Tables & Real-Time Sync Triggers
            try:
                # Facts FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                        category,
                        key,
                        value,
                        content='facts',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                        INSERT INTO facts_fts(rowid, category, key, value)
                        VALUES (new.id, new.category, new.key, new.value);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                        INSERT INTO facts_fts(facts_fts, rowid, category, key, value)
                        VALUES ('delete', old.id, old.category, old.key, old.value);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
                        INSERT INTO facts_fts(facts_fts, rowid, category, key, value)
                        VALUES ('delete', old.id, old.category, old.key, old.value);
                        INSERT INTO facts_fts(rowid, category, key, value)
                        VALUES (new.id, new.category, new.key, new.value);
                    END;
                """)

                # Documents FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                        title,
                        category,
                        summary,
                        raw_content,
                        content='documents',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                        INSERT INTO documents_fts(rowid, title, category, summary, raw_content)
                        VALUES (new.id, new.title, new.category, new.summary, new.raw_content);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                        INSERT INTO documents_fts(documents_fts, rowid, title, category, summary, raw_content)
                        VALUES ('delete', old.id, old.title, old.category, old.summary, old.raw_content);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                        INSERT INTO documents_fts(documents_fts, rowid, title, category, summary, raw_content)
                        VALUES ('delete', old.id, old.title, old.category, old.summary, old.raw_content);
                        INSERT INTO documents_fts(rowid, title, category, summary, raw_content)
                        VALUES (new.id, new.title, new.category, new.summary, new.raw_content);
                    END;
                """)

                # Interactions FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS interactions_fts USING fts5(
                        user_input,
                        jarvis_reply,
                        content='interactions',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS interactions_ai AFTER INSERT ON interactions BEGIN
                        INSERT INTO interactions_fts(rowid, user_input, jarvis_reply)
                        VALUES (new.id, new.user_input, new.jarvis_reply);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS interactions_ad AFTER DELETE ON interactions BEGIN
                        INSERT INTO interactions_fts(interactions_fts, rowid, user_input, jarvis_reply)
                        VALUES ('delete', old.id, old.user_input, old.jarvis_reply);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS interactions_au AFTER UPDATE ON interactions BEGIN
                        INSERT INTO interactions_fts(interactions_fts, rowid, user_input, jarvis_reply)
                        VALUES ('delete', old.id, old.user_input, old.jarvis_reply);
                        INSERT INTO interactions_fts(rowid, user_input, jarvis_reply)
                        VALUES (new.id, new.user_input, new.jarvis_reply);
                    END;
                """)

                # Episodic Lessons FTS5 Virtual Table & Triggers
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS episodic_lessons_fts USING fts5(
                        tool_name,
                        trigger_context,
                        failure_signature,
                        lesson,
                        content='episodic_lessons',
                        content_rowid='id'
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS episodic_lessons_ai AFTER INSERT ON episodic_lessons BEGIN
                        INSERT INTO episodic_lessons_fts(rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES (new.id, new.tool_name, new.trigger_context, new.failure_signature, new.lesson);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS episodic_lessons_ad AFTER DELETE ON episodic_lessons BEGIN
                        INSERT INTO episodic_lessons_fts(episodic_lessons_fts, rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES ('delete', old.id, old.tool_name, old.trigger_context, old.failure_signature, old.lesson);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS episodic_lessons_au AFTER UPDATE ON episodic_lessons BEGIN
                        INSERT INTO episodic_lessons_fts(episodic_lessons_fts, rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES ('delete', old.id, old.tool_name, old.trigger_context, old.failure_signature, old.lesson);
                        INSERT INTO episodic_lessons_fts(rowid, tool_name, trigger_context, failure_signature, lesson)
                        VALUES (new.id, new.tool_name, new.trigger_context, new.failure_signature, new.lesson);
                    END;
                """)

                # Schema metadata table for one-time migrations and rebuild tracking
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)

                # One-time rebuild to backfill any pre-existing rows into FTS indexes
                cursor.execute("SELECT 1 FROM schema_meta WHERE key = 'fts5_initialized'")
                if not cursor.fetchone():
                    try:
                        cursor.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
                        cursor.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
                        cursor.execute("INSERT INTO interactions_fts(interactions_fts) VALUES('rebuild')")
                        cursor.execute("INSERT INTO episodic_lessons_fts(episodic_lessons_fts) VALUES('rebuild')")
                        cursor.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('fts5_initialized', '1')")
                    except Exception as rb_err:
                        logger.debug("FTS5 initial rebuild notice: %s", rb_err)
            except Exception as fts_err:
                logger.warning("SQLite FTS5 virtual tables could not be initialized (falling back to standard SQL): %s", fts_err)

            # sqlite-vec Native C Vector Virtual Table & Real-Time Sync Triggers (Native 384-dim BGE INT8)
            try:
                import sqlite_vec
                from jarvis.core.memory.onnx_embeddings import DEFAULT_EMBEDDING_DIM
                TARGET_VEC_DIM = DEFAULT_EMBEDDING_DIM  # 384
                current_backend = self.vector_engine.get_backend_name()

                # Mandatory Constraint 2: Track embedder_backend in schema_meta alongside vec_dimension
                cursor.execute("SELECT value FROM schema_meta WHERE key = 'vec_dimension'")
                row_dim = cursor.fetchone()
                stored_dim = row_dim["value"] if row_dim else None

                cursor.execute("SELECT value FROM schema_meta WHERE key = 'embedder_backend'")
                row_backend = cursor.fetchone()
                stored_backend = row_backend["value"] if row_backend else None

                if stored_dim != str(TARGET_VEC_DIM) or stored_backend != current_backend:
                    logger.info(
                        "Vector schema migration triggered (stored: dim=%s, backend=%s; target: dim=%s, backend=%s). Recreating virtual tables...",
                        stored_dim, stored_backend, TARGET_VEC_DIM, current_backend
                    )
                    cursor.execute("DROP TABLE IF EXISTS facts_vec")
                    cursor.execute("DROP TABLE IF EXISTS documents_vec")
                    cursor.execute("DROP TABLE IF EXISTS lessons_vec")
                    cursor.execute("DROP TABLE IF EXISTS kg_edges_vec")

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
                        fact_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS facts_vec_ad AFTER DELETE ON facts BEGIN
                        DELETE FROM facts_vec WHERE fact_id = old.id;
                    END;
                """)

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_vec USING vec0(
                        doc_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS documents_vec_ad AFTER DELETE ON documents BEGIN
                        DELETE FROM documents_vec WHERE doc_id = old.id;
                    END;
                """)

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS lessons_vec USING vec0(
                        lesson_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS lessons_vec_ad AFTER DELETE ON episodic_lessons BEGIN
                        DELETE FROM lessons_vec WHERE lesson_id = old.id;
                    END;
                """)

                cursor.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS kg_edges_vec USING vec0(
                        edge_id INTEGER PRIMARY KEY,
                        embedding float[{TARGET_VEC_DIM}]
                    )
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS kg_edges_vec_ad AFTER DELETE ON knowledge_edges BEGIN
                        DELETE FROM kg_edges_vec WHERE edge_id = old.edge_id;
                    END;
                """)

                # Mandatory Constraint 3: Batch Re-Indexing using embed_batch
                cursor.execute("""
                    SELECT f.id, f.category, f.key, f.value
                    FROM facts f
                    LEFT JOIN facts_vec v ON f.id = v.fact_id
                    WHERE v.fact_id IS NULL
                """)
                unindexed_facts = cursor.fetchall()
                if unindexed_facts:
                    texts = [f"{f['category']} {f['key']} {f['value']}" for f in unindexed_facts]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                        [(f["id"], sqlite_vec.serialize_float32(v)) for f, v in zip(unindexed_facts, vecs)]
                    )

                cursor.execute("""
                    SELECT d.id, d.title, d.category, d.summary, d.raw_content
                    FROM documents d
                    LEFT JOIN documents_vec dv ON d.id = dv.doc_id
                    WHERE dv.doc_id IS NULL
                """)
                unindexed_docs = cursor.fetchall()
                if unindexed_docs:
                    texts = [f"{d['title']} {d['category']} {d['summary']} {(d['raw_content'] or '')[:500]}" for d in unindexed_docs]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO documents_vec(doc_id, embedding) VALUES (?, ?)",
                        [(d["id"], sqlite_vec.serialize_float32(v)) for d, v in zip(unindexed_docs, vecs)]
                    )

                cursor.execute("""
                    SELECT l.id, l.tool_name, l.trigger_context, l.failure_signature, l.lesson
                    FROM episodic_lessons l
                    LEFT JOIN lessons_vec lv ON l.id = lv.lesson_id
                    WHERE lv.lesson_id IS NULL
                """)
                unindexed_lessons = cursor.fetchall()
                if unindexed_lessons:
                    texts = [f"{l['tool_name']} {l['trigger_context']} {l['failure_signature']} {l['lesson']}" for l in unindexed_lessons]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO lessons_vec(lesson_id, embedding) VALUES (?, ?)",
                        [(l["id"], sqlite_vec.serialize_float32(v)) for l, v in zip(unindexed_lessons, vecs)]
                    )

                cursor.execute("""
                    SELECT k.edge_id, k.subject, k.predicate, k.object
                    FROM knowledge_edges k
                    LEFT JOIN kg_edges_vec kv ON k.edge_id = kv.edge_id
                    WHERE kv.edge_id IS NULL
                """)
                unindexed_edges = cursor.fetchall()
                if unindexed_edges:
                    texts = [f"{k['subject']} {k['predicate']} {k['object']}" for k in unindexed_edges]
                    vecs = self.vector_engine.embed_batch(texts, batch_size=64, is_query=False, dim=TARGET_VEC_DIM)
                    cursor.executemany(
                        "INSERT OR REPLACE INTO kg_edges_vec(edge_id, embedding) VALUES (?, ?)",
                        [(k["edge_id"], sqlite_vec.serialize_float32(v)) for k, v in zip(unindexed_edges, vecs)]
                    )

                cursor.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('vec_dimension', ?)", (str(TARGET_VEC_DIM),))
                cursor.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('embedder_backend', ?)", (current_backend,))
            except Exception as vec_err:
                logger.debug("sqlite-vec virtual table initialization note: %s", vec_err)

    # Bi-Temporal Knowledge Graph & Cognitive Twin Architecture
    def remember_edge(
        self,
        subject: str,
        predicate: str,
        object_val: str,
        is_functional: bool = True,
        confidence: float = 1.0,
        decay_lambda: float = 0.01,
        source_context: Optional[str] = None,
    ) -> int:
        """Stores a bi-temporal knowledge edge with automatic functional supersession."""
        import time
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
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Queries currently active knowledge edges (valid_until IS NULL) with ACT-R temporal activation."""
        import time
        now = time.time()
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM knowledge_edges WHERE valid_until IS NULL"
            params: List[Any] = []
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
        hour_bucket: Optional[int] = None,
        is_weekend: Optional[int] = None,
    ) -> None:
        """Records a user action sequence transition conditioned on circadian hour and weekend status."""
        import time
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
        hour_bucket: Optional[int] = None,
        is_weekend: Optional[int] = None,
        min_prob: float = 0.50,
    ) -> Optional[Tuple[str, float]]:
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
        inverse_payload: Dict[str, Any],
    ) -> None:
        """Logs a Level-1 reversible desktop mutation to the action journal for instant undo."""
        import time
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO action_journal (action_id, timestamp, skill_name, risk_level, inverse_skill, inverse_payload, reverted)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            """, (action_id, now, skill_name, risk_level, inverse_skill, json.dumps(inverse_payload)))

    def revert_action(self, action_id: Optional[str] = None) -> Tuple[bool, str]:
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

    # Fact & Preference Management
    def remember_fact(self, key: str, value: str, category: str = "general") -> str:
        """Stores or updates a persistent fact about the user or system."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO facts (category, key, value, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    updated_at = CURRENT_TIMESTAMP
            """, (category.lower().strip(), key.lower().strip(), value.strip()))

            # Index into sqlite-vec facts_vec
            try:
                import sqlite_vec
                cursor.execute("SELECT id FROM facts WHERE key = ?", (key.lower().strip(),))
                row = cursor.fetchone()
                if row:
                    fact_id = row["id"]
                    text = f"{category} {key} {value}"
                    vec = self.vector_engine.dense_vectorize(text)
                    blob = sqlite_vec.serialize_float32(vec)
                    cursor.execute("INSERT OR REPLACE INTO facts_vec(fact_id, embedding) VALUES (?, ?)", (fact_id, blob))
            except Exception as e:
                logger.debug("sqlite-vec fact indexing notice: %s", e)

            conn.commit()
        with self._context_summary_lock:
            self._context_summary_cache = None
        return f"Remembered: '{key}' is '{value}'."

    def get_fact(self, key: str) -> Optional[str]:
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
        try:
            with self._get_connection() as conn:
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
        except Exception as e:
            logger.debug("user_preferences table update notice: %s", e)

        return f"Core preference updated: '{clean_key}' is now set to '{clean_val}', Sir."

    def get_user_preferences(self) -> Dict[str, str]:
        """Retrieves all core user preferences as a key-value dictionary."""
        result: Dict[str, str] = {}
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

    def search_facts(self, query: str) -> List[Dict[str, str]]:
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

    def get_all_facts(self) -> List[Dict[str, str]]:
        """Retrieves all stored facts."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT category, key, value FROM facts ORDER BY category, key")
            return [dict(row) for row in cursor.fetchall()]

    def semantic_search_facts(self, query: str, top_k: int = 5, min_score: float = 0.12) -> List[Dict[str, Any]]:
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
            logger.debug("sqlite-vec KNN search fallback: %s", e)

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

    def hybrid_search_facts(self, query: str, top_k: int = 5, k_rrf: int = 60, max_l2_distance: float = 0.78) -> List[Dict[str, Any]]:
        """Hybrid search combining FTS5 BM25 and sqlite-vec KNN via Reciprocal Rank Fusion (RRF)."""
        fts_results = self.search_facts(query)
        vec_results = self.semantic_search_facts(query, top_k=max(top_k * 2, 10))
        fused = fuse_vec_and_fts_rrf(vec_results, fts_results, k=k_rrf, max_l2_distance=max_l2_distance)
        return fused[:top_k]

    def get_all_memory_nodes(
        self,
        category: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 150,
    ) -> Dict[str, Any]:
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
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if clean_type in ("fact", "facts", "preference", "user_preference"):
                if isinstance(node_id, int) or (isinstance(node_id, str) and node_id.isdigit()):
                    cursor.execute("DELETE FROM facts WHERE id = ?", (int(node_id),))
                else:
                    cursor.execute("DELETE FROM facts WHERE key = ?", (str(node_id).lower().strip(),))
                deleted = cursor.rowcount > 0
            elif clean_type in ("document", "documents"):
                cursor.execute("DELETE FROM documents WHERE id = ?", (int(node_id),))
                deleted = cursor.rowcount > 0
            elif clean_type in ("lesson", "lessons", "episodic_lesson"):
                cursor.execute("DELETE FROM episodic_lessons WHERE id = ?", (int(node_id),))
                deleted = cursor.rowcount > 0
            else:
                return False

            conn.commit()

        with self._context_summary_lock:
            self._context_summary_cache = None
        return deleted

    def delete_fact(self, key_or_id: Any) -> bool:
        """Deletes a stored fact by key or integer ID."""
        return self.delete_memory_node("fact", key_or_id)

    def delete_user_preference(self, key: str) -> bool:
        """Deletes a stored user preference by key."""
        return self.delete_memory_node("preference", key)

    # Tasks & Reminders
    def add_task(self, description: str, due_date: Optional[str] = None) -> str:
        """Adds a task or reminder."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (description, due_date, is_completed)
                VALUES (?, ?, 0)
            """, (description.strip(), due_date))
            conn.commit()
            task_id = cursor.lastrowid
        return f"Task #{task_id} added: {description}"

    def get_pending_tasks(self) -> List[Dict[str, Any]]:
        """Retrieves all incomplete tasks."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, description, due_date, created_at FROM tasks WHERE is_completed = 0")
            return [dict(row) for row in cursor.fetchall()]

    def complete_task(self, task_id: int) -> str:
        """Marks a task as completed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE tasks SET is_completed = 1 WHERE id = ?", (task_id,))
            conn.commit()
            if cursor.rowcount > 0:
                return f"Task #{task_id} marked as completed."
            return f"Task #{task_id} not found."

    # Calendar Events
    def add_calendar_event(self, title: str, start_time: str, end_time: Optional[str] = None, location: Optional[str] = None) -> str:
        """Adds a calendar event with deduplication for identical title and start time."""
        clean_title = title.strip()
        clean_start = start_time.strip()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM calendar_events
                WHERE is_cancelled = 0 AND LOWER(title) = ? AND start_time = ?
            """, (clean_title.lower(), clean_start))
            existing = cursor.fetchone()
            if existing:
                event_id = existing["id"]
                cursor.execute("""
                    UPDATE calendar_events
                    SET end_time = COALESCE(?, end_time),
                        location = COALESCE(?, location)
                    WHERE id = ?
                """, (end_time, location, event_id))
                conn.commit()
                return f"Event #{event_id} updated: '{clean_title}' at {clean_start}" + (f" ({location})" if location else "") + "."

            cursor.execute("""
                INSERT INTO calendar_events (title, start_time, end_time, location, is_cancelled)
                VALUES (?, ?, ?, ?, 0)
            """, (clean_title, clean_start, end_time, location))
            conn.commit()
            event_id = cursor.lastrowid
        return f"Event #{event_id} scheduled: '{clean_title}' at {clean_start}" + (f" ({location})" if location else "") + "."

    def get_calendar_events(self, include_cancelled: bool = False) -> List[Dict[str, Any]]:
        """Retrieves active calendar events."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT id, title, start_time, end_time, location, created_at FROM calendar_events WHERE is_cancelled = 0 ORDER BY start_time"
            if include_cancelled:
                query = "SELECT id, title, start_time, end_time, location, created_at, is_cancelled FROM calendar_events ORDER BY start_time"
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def cancel_calendar_event(self, event_id: Any = None, title: Optional[str] = None) -> str:
        """Marks a calendar event as cancelled by numerical ID or by matching title."""
        # Normalize arguments if event_id is passed as title string or digits
        if event_id is not None:
            if isinstance(event_id, str):
                s_id = event_id.strip()
                if s_id.isdigit():
                    event_id = int(s_id)
                elif not title:
                    title = s_id
                    event_id = None
                else:
                    event_id = None

        with self._get_connection() as conn:
            cursor = conn.cursor()
            if event_id is not None:
                cursor.execute("UPDATE calendar_events SET is_cancelled = 1 WHERE id = ?", (int(event_id),))
                conn.commit()
                if cursor.rowcount > 0:
                    return f"Event #{event_id} has been cancelled, Sir."
                return f"Event #{event_id} was not found."

            if title and title.strip():
                clean_title = title.strip()
                cursor.execute("""
                    SELECT id, title FROM calendar_events
                    WHERE is_cancelled = 0 AND LOWER(title) LIKE ?
                """, (f"%{clean_title.lower()}%",))
                rows = cursor.fetchall()
                if not rows:
                    return f"No active calendar event matching '{clean_title}' was found, Sir."

                cancelled_ids = [r["id"] for r in rows]
                cancelled_titles = [r["title"] for r in rows]
                placeholders = ",".join("?" * len(cancelled_ids))
                cursor.execute(f"UPDATE calendar_events SET is_cancelled = 1 WHERE id IN ({placeholders})", cancelled_ids)
                conn.commit()
                return f"Cancelled calendar event(s): {', '.join(cancelled_titles)}, Sir."

            # If neither provided, cancel the most recent or upcoming active event
            cursor.execute("SELECT id, title FROM calendar_events WHERE is_cancelled = 0 ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE calendar_events SET is_cancelled = 1 WHERE id = ?", (row["id"],))
                conn.commit()
                return f"Cancelled event '{row['title']}' (#{row['id']}), Sir."
            return "No active calendar events to cancel, Sir."

    # Staged & Ingested Document Management
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
        file_path: str = ""
    ) -> int:
        """Archives document content and structured summary into the persistent knowledge archive."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO documents (title, category, summary, raw_content, file_path)
                VALUES (?, ?, ?, ?, ?)
            """, (title.strip(), category.lower().strip(), summary.strip(), raw_content.strip(), file_path.strip()))
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
                        SELECT d.id, d.title, d.category, d.summary, d.file_path, d.created_at
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
                SELECT id, title, category, summary, file_path, created_at
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
                    SELECT d.id, d.title, d.category, d.summary, d.file_path, d.created_at, dv.distance
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
            cursor.execute("SELECT id, title, category, summary, file_path, created_at, raw_content FROM documents ORDER BY id DESC LIMIT 100")
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

    def get_document_by_id(self, doc_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single archived document by its primary key ID."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, raw_content, file_path, created_at
                FROM documents WHERE id = ?
            """, (doc_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_recent_documents(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Retrieves recently archived documents."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, raw_content, file_path, created_at
                FROM documents ORDER BY id DESC LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def format_document_reference_block(self, docs: List[Dict[str, Any]]) -> str:
        """Formats retrieved document chunks using Chunk Sandwiching and metadata headers under inert XML tags.
        Bypasses 'Lost in the Middle' attention degradation by placing highest-relevance chunks at the boundaries."""
        if not docs:
            return ""

        # Sort by relevance / rrf score descending if present (Chunk Sandwiching)
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

    def get_saved_calendars(self) -> List[Dict[str, Any]]:
        """Retrieves all calendar and schedule documents archived in memory."""
        with self._get_ro_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, category, summary, file_path, created_at
                FROM documents
                WHERE LOWER(category) IN ('schedule', 'syllabus')
                   OR LOWER(title) LIKE '%timetable%'
                   OR LOWER(title) LIKE '%calendar%'
                   OR LOWER(title) LIKE '%schedule%'
                   OR LOWER(title) LIKE '%emploi%'
                ORDER BY id DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def delete_saved_calendar(self, doc_id: int) -> bool:
        """Deletes a saved calendar document from knowledge memory."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_calendar_event(self, event_id: int) -> bool:
        """Permanently deletes a calendar event by numerical ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
            conn.commit()
            return cursor.rowcount > 0

    def clear_calendar_events(self) -> int:
        """Deletes all calendar events from the active database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM calendar_events")
            conn.commit()
            return cursor.rowcount

    # Interaction History & Autonomous Self-Learning
    def log_interaction(self, user_input: str, jarvis_reply: str, tool_calls: Optional[str] = None):
        """Logs a conversation turn."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO interactions (user_input, jarvis_reply, tool_calls)
                VALUES (?, ?, ?)
            """, (user_input, jarvis_reply, tool_calls or ""))
            conn.commit()

    def get_recent_interactions(self, limit: int = 8) -> List[Dict[str, Any]]:
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

    def search_interactions(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
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

    def semantic_search_interactions(self, query: str, top_k: int = 5, min_score: float = 0.10) -> List[Dict[str, Any]]:
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
        groq_api_key: Optional[str] = None,
        provider: str = "groq",
        extractor_fn: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
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
                items: List[Dict[str, Any]] = []
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
                        import time
                        if provider == "groq":
                            time.sleep(1.0)
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

    def get_context_summary(self, query: Optional[str] = None) -> str:
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
            matched = self.hybrid_search_facts(query, top_k=4)
            if matched:
                lines = [f"• {m['key'].replace('_', ' ').capitalize()}: {m['value']}" for m in matched]
                semantic_section = "[Directly Relevant Knowledge to Current Context]\n" + "\n".join(lines) + "\n\n"

        by_cat = {}
        for f in facts:
            cat = f.get("category", "general").capitalize()
            by_cat.setdefault(cat, []).append(f"{f['key'].replace('_', ' ').capitalize()}: {f['value']}")

        sections = []
        for cat, items in sorted(by_cat.items()):
            # Bound items per category to prevent prompt bloat & Stanford/Berkeley 'Lost in the Middle' attention drops
            max_items = 4 if query else 8
            bounded_items = items[:max_items]
            sections.append(f"[{cat}]\n" + "\n".join(f"• {item}" for item in bounded_items))
        summary = semantic_section + "\n\n".join(sections)
        if not query:
            with self._context_summary_lock:
                self._context_summary_cache = summary
        return summary

    # Tier 3: Episodic Reflection & Self-Correction
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
        tool_name: Optional[str] = None,
        top_k: int = 3,
        min_sim: float = 0.55,
        max_l2_distance: float = 0.90,
        k_rrf: int = 60,
    ) -> List[Dict[str, Any]]:
        """Recalls relevant episodic lessons using Hybrid RRF search (FTS5 BM25 + sqlite-vec KNN)."""
        if not query:
            return []

        # 1. FTS5 Search
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

        # 2. sqlite-vec KNN Search
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

        # 3. Reciprocal Rank Fusion
        scores: Dict[int, float] = {}
        lesson_map: Dict[int, Dict[str, Any]] = {}

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
        is_user_active_fn: Optional[Callable[[], bool]] = None,
        batch_size: int = 20,
        extractor_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Executes zero-contention sleep-phase memory consolidation on WAL snapshot.

        Discovers redundant facts on an isolated read-only connection, and applies
        compaction in <1.5ms micro-transactions that yield immediately on user activity.
        """
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
        is_user_active_fn: Optional[Callable[[], bool]] = None,
        extractor_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Harvests unprocessed dialogue turns, scrubs credentials, and distills traits via LLM."""
        from .transcript_distiller import TranscriptDistiller
        distiller = TranscriptDistiller(str(self.db_path), self.vector_engine, self.llm_config, memory_manager=self)
        return distiller.distill_unprocessed_interactions(
            is_user_active_fn=is_user_active_fn,
            max_turns=max_turns,
            extractor_fn=extractor_fn,
        )


from .obsidian_sync import (
    ObsidianContextSyncEngine,
    ObsidianChunk,
    ObsidianNoteParser,
    SyncStats,
)
from .sleep_consolidator import SleepPhaseConsolidator
from .transcript_distiller import TranscriptDistiller

__all__ = [
    "MemoryManager",
    "VectorMemoryEngine",
    "SleepPhaseConsolidator",
    "TranscriptDistiller",
    "ObsidianContextSyncEngine",
    "ObsidianChunk",
    "ObsidianNoteParser",
    "SyncStats",
    "fuse_vec_and_fts_rrf",
    "sanitize_memory_taint",
]
