"""Non-blocking, background-threaded Obsidian Context Sync Engine for Jarvis 2.0.

Synchronizes local Markdown technical notes, YAML frontmatter, and semantic wiki-links
into SQLite episodic memory (data/jarvis_memory.db) operating under WAL mode.
Provides incremental delta tracking (mtime + SHA-256) and thread-isolated execution
to guarantee zero audio micro-stutters or lockups on the CUDA voice pipeline.
"""
import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.core.memory import sanitize_fts5_query

logger = logging.getLogger("Jarvis.Memory.Obsidian")


@dataclass
class ObsidianChunk:
    """Represents a structured semantic chunk extracted from an Obsidian Markdown note."""
    file_path: str
    note_title: str
    breadcrumb: str
    content: str
    chunk_index: int
    tags: list[str] = field(default_factory=list)
    wiki_links: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncStats:
    """Telemetry outcome of an incremental Obsidian vault synchronization pass."""
    total_files: int = 0
    synced_files: int = 0
    modified_files: int = 0
    deleted_files: int = 0
    total_chunks: int = 0
    elapsed_ms: float = 0.0


class ObsidianNoteParser:
    """Extracts YAML frontmatter metadata, wiki-links, and hierarchical Markdown chunks."""

    FRONTMATTER_PATTERN = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
    WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]\|]+)(?:\|([^\[\]]+))?\]\]")
    HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    @classmethod
    def parse_frontmatter(cls, content: str) -> tuple[dict[str, Any], str]:
        """Separates YAML frontmatter from Markdown body, returning parsed dict and clean body."""
        match = cls.FRONTMATTER_PATTERN.match(content)
        if not match:
            return {}, content

        raw_yaml = match.group(1).strip()
        body = content[match.end():]

        parsed: dict[str, Any] = {}
        # 1. Attempt PyYAML parsing
        try:
            import yaml
            loaded = yaml.safe_load(raw_yaml)
            if isinstance(loaded, dict):
                parsed = loaded
        except Exception:
            # 2. Resilient fallback key-value / list regex parser
            for line in raw_yaml.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, val = line.split(":", 1)
                    k = key.strip()
                    v = val.strip().strip("'\"")
                    if v.startswith("[") and v.endswith("]"):
                        items = [i.strip().strip("'\"") for i in v[1:-1].split(",") if i.strip()]
                        parsed[k] = items
                    else:
                        parsed[k] = v

        # Normalize tags into list
        tags = parsed.get("tags", [])
        if isinstance(tags, str):
            parsed["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        elif not isinstance(tags, list):
            parsed["tags"] = [str(tags)]

        return parsed, body

    @classmethod
    def extract_wiki_links(cls, content: str) -> list[str]:
        """Extracts unique referenced wiki-link targets (e.g. [[Target Note|Alias]] -> Target Note)."""
        links: set[str] = set()
        for m in cls.WIKILINK_PATTERN.finditer(content):
            target = m.group(1).strip()
            if target:
                links.add(target)
        return sorted(links)

    @classmethod
    def extract_inline_tags(cls, content: str) -> list[str]:
        """Extracts inline hash tags (e.g. #architecture #audio) excluding markdown headings."""
        tag_pattern = re.compile(r"(?:^|\s)#([a-zA-Z0-9_\-\/]+)\b")
        tags: set[str] = set()
        for m in tag_pattern.finditer(content):
            tag = m.group(1).strip()
            # Guard against hex colors or pure numbers
            if not re.match(r"^[0-9a-fA-F]{3,6}$", tag) and not tag.isdigit():
                tags.add(tag)
        return sorted(tags)

    @classmethod
    def chunk_markdown(
        cls,
        rel_path: str,
        content: str,
        max_chunk_chars: int = 1200,
    ) -> list[ObsidianChunk]:
        """Parses and chunks a note hierarchically by Markdown headings into structured units."""
        frontmatter, body = cls.parse_frontmatter(content)
        note_title = frontmatter.get("title") or Path(rel_path).stem
        global_tags = list(frontmatter.get("tags", []))
        global_links = cls.extract_wiki_links(body)

        chunks: list[ObsidianChunk] = []
        lines = body.splitlines(keepends=True)

        current_heading = "Overview"
        current_lines: list[str] = []
        chunk_idx = 0

        def _flush_section(heading: str, sec_lines: list[str]):
            nonlocal chunk_idx
            sec_text = "".join(sec_lines).strip()
            if not sec_text:
                return

            # Combine tags: frontmatter + inline tags in this section
            sec_tags = sorted(set(global_tags + cls.extract_inline_tags(sec_text)))
            sec_links = cls.extract_wiki_links(sec_text) or global_links

            # If section text is within max size, emit single chunk
            if len(sec_text) <= max_chunk_chars:
                chunks.append(ObsidianChunk(
                    file_path=rel_path,
                    note_title=str(note_title),
                    breadcrumb=f"{note_title} > {heading}",
                    content=sec_text,
                    chunk_index=chunk_idx,
                    tags=sec_tags,
                    wiki_links=sec_links,
                    frontmatter=frontmatter,
                ))
                chunk_idx += 1
                return

            # Sub-split long section by paragraphs
            paragraphs = re.split(r"\n\s*\n", sec_text)
            current_sub: list[str] = []
            current_len = 0

            for p in paragraphs:
                p_clean = p.strip()
                if not p_clean:
                    continue

                if current_len + len(p_clean) > max_chunk_chars and current_sub:
                    sub_text = "\n\n".join(current_sub).strip()
                    chunks.append(ObsidianChunk(
                        file_path=rel_path,
                        note_title=str(note_title),
                        breadcrumb=f"{note_title} > {heading}",
                        content=sub_text,
                        chunk_index=chunk_idx,
                        tags=sec_tags,
                        wiki_links=cls.extract_wiki_links(sub_text) or sec_links,
                        frontmatter=frontmatter,
                    ))
                    chunk_idx += 1
                    current_sub = [p_clean]
                    current_len = len(p_clean)
                else:
                    current_sub.append(p_clean)
                    current_len += len(p_clean) + 2

            if current_sub:
                sub_text = "\n\n".join(current_sub).strip()
                chunks.append(ObsidianChunk(
                    file_path=rel_path,
                    note_title=str(note_title),
                    breadcrumb=f"{note_title} > {heading}",
                    content=sub_text,
                    chunk_index=chunk_idx,
                    tags=sec_tags,
                    wiki_links=cls.extract_wiki_links(sub_text) or sec_links,
                    frontmatter=frontmatter,
                ))
                chunk_idx += 1

        for line in lines:
            h_match = cls.HEADING_PATTERN.match(line)
            if h_match:
                if current_lines:
                    _flush_section(current_heading, current_lines)
                    current_lines = []
                current_heading = h_match.group(2).strip()
            else:
                current_lines.append(line)

        if current_lines:
            _flush_section(current_heading, current_lines)

        return chunks


class ObsidianContextSyncEngine:
    """Non-blocking, thread-isolated background sync engine for Obsidian Markdown vaults."""

    IGNORED_DIRS = {
        ".obsidian", ".git", ".trash", ".stversions", ".vscode", "node_modules", ".cache"
    }

    def __init__(
        self,
        vault_path: Path | str,
        db_path: Path | str | None = None,
        sync_interval: float = 15.0,
        batch_size: int = 50,
        max_chunk_chars: int = 1200,
    ):
        self.vault_path = Path(vault_path).resolve()
        workspace_dir = Path(__file__).resolve().parent.parent.parent.parent
        self.db_path = Path(db_path).resolve() if db_path else (workspace_dir / "data" / "jarvis_memory.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.sync_interval = max(3.0, float(sync_interval))
        self.batch_size = max(10, int(batch_size))
        self.max_chunk_chars = int(max_chunk_chars)

        # In-memory fast delta cache: rel_path -> (mtime, sha256)
        self._stat_cache: dict[str, tuple[float, str]] = {}
        self._cache_lock = threading.Lock()

        # WAL-safeguarded asynchronous database queue
        self._db_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1000)

        # Thread isolation & lifecycle primitives
        self._stop_event = threading.Event()
        self._sync_trigger = threading.Event()
        self._is_running = False

        self._db_thread: threading.Thread | None = None
        self._scan_thread: threading.Thread | None = None

    def start(self):
        """Launches the dedicated database writer thread and background scanner thread."""
        if self._is_running:
            return
        self._is_running = True
        self._stop_event.clear()
        self._sync_trigger.clear()

        # 1. Start dedicated background SQLite WAL writer
        self._db_thread = threading.Thread(
            target=self._db_writer_loop,
            daemon=True,
            name="Jarvis-Obsidian-DBWriter",
        )
        self._db_thread.start()

        # 2. Start dedicated background vault scanner
        self._scan_thread = threading.Thread(
            target=self._scanner_loop,
            daemon=True,
            name="Jarvis-Obsidian-Scanner",
        )
        self._scan_thread.start()
        logger.info("ObsidianContextSyncEngine online. Vault: '%s' | Interval: %.1fs", self.vault_path, self.sync_interval)

    def stop(self, timeout: float = 3.0):
        """Stops background threads cleanly and drains the database queue."""
        if not self._is_running:
            return
        self._is_running = False
        self._stop_event.set()
        self._sync_trigger.set()

        # Signal DB queue termination
        try:
            self._db_queue.put(None, timeout=0.5)
        except Exception:
            pass

        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=timeout)
        if self._db_thread and self._db_thread.is_alive():
            self._db_thread.join(timeout=timeout)
        logger.info("ObsidianContextSyncEngine stopped.")

    def trigger_sync(self):
        """Triggers an immediate out-of-band incremental sync pass."""
        self._sync_trigger.set()

    def _init_db_schema(self, conn: sqlite3.Connection):
        """Initializes WAL mode and schemas for delta tracking and note chunks."""
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Delta sync state table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS obsidian_sync_state (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                mtime REAL NOT NULL,
                chunk_count INTEGER DEFAULT 0,
                last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Structured chunk storage for episodic memory
        conn.execute("""
            CREATE TABLE IF NOT EXISTS obsidian_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                note_title TEXT NOT NULL,
                breadcrumb TEXT,
                content TEXT NOT NULL,
                tags TEXT,
                wiki_links TEXT,
                frontmatter TEXT,
                chunk_index INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_chunks_path ON obsidian_chunks(file_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_chunks_title ON obsidian_chunks(note_title)")

        # Schema metadata table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # SQLite FTS5 Virtual Table & Real-Time Sync Triggers for Obsidian Chunks
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS obsidian_chunks_fts USING fts5(
                    note_title,
                    breadcrumb,
                    content,
                    tags,
                    wiki_links,
                    content='obsidian_chunks',
                    content_rowid='id'
                )
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS obsidian_chunks_ai AFTER INSERT ON obsidian_chunks BEGIN
                    INSERT INTO obsidian_chunks_fts(rowid, note_title, breadcrumb, content, tags, wiki_links)
                    VALUES (new.id, new.note_title, new.breadcrumb, new.content, new.tags, new.wiki_links);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS obsidian_chunks_ad AFTER DELETE ON obsidian_chunks BEGIN
                    INSERT INTO obsidian_chunks_fts(obsidian_chunks_fts, rowid, note_title, breadcrumb, content, tags, wiki_links)
                    VALUES ('delete', old.id, old.note_title, old.breadcrumb, old.content, old.tags, old.wiki_links);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS obsidian_chunks_au AFTER UPDATE ON obsidian_chunks BEGIN
                    INSERT INTO obsidian_chunks_fts(obsidian_chunks_fts, rowid, note_title, breadcrumb, content, tags, wiki_links)
                    VALUES ('delete', old.id, old.note_title, old.breadcrumb, old.content, old.tags, old.wiki_links);
                    INSERT INTO obsidian_chunks_fts(rowid, note_title, breadcrumb, content, tags, wiki_links)
                    VALUES (new.id, new.note_title, new.breadcrumb, new.content, new.tags, new.wiki_links);
                END;
            """)

            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM schema_meta WHERE key = 'obsidian_fts5_initialized'")
            if not cursor.fetchone():
                try:
                    conn.execute("INSERT INTO obsidian_chunks_fts(obsidian_chunks_fts) VALUES('rebuild')")
                    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('obsidian_fts5_initialized', '1')")
                except Exception as rb_err:
                    logger.debug("Obsidian FTS5 initial rebuild notice: %s", rb_err)
        except Exception as fts_err:
            logger.warning("Obsidian FTS5 virtual table could not be initialized: %s", fts_err)

        conn.commit()

    def _load_persisted_state(self, conn: sqlite3.Connection):
        """Pre-populates the in-memory delta cache from SQLite on startup."""
        cursor = conn.cursor()
        cursor.execute("SELECT file_path, file_hash, mtime FROM obsidian_sync_state")
        rows = cursor.fetchall()
        with self._cache_lock:
            for path, h, m in rows:
                self._stat_cache[path] = (float(m), str(h))

    def _db_writer_loop(self):
        """Dedicated thread executing batched SQLite transactions without touching cognitive loops."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_db_schema(conn)
        self._load_persisted_state(conn)

        batch_ops: list[dict[str, Any]] = []

        def _flush_batch():
            nonlocal batch_ops
            if not batch_ops:
                return
            try:
                with conn:
                    for op in batch_ops:
                        action = op["action"]
                        if action == "UPSERT_NOTE":
                            f_path = op["file_path"]
                            f_hash = op["file_hash"]
                            mtime = op["mtime"]
                            chunks: list[ObsidianChunk] = op["chunks"]

                            # Delete existing chunks for this note
                            conn.execute("DELETE FROM obsidian_chunks WHERE file_path = ?", (f_path,))

                            # Insert newly parsed chunks
                            insert_rows = [
                                (
                                    c.file_path,
                                    c.note_title,
                                    c.breadcrumb,
                                    c.content,
                                    json.dumps(c.tags),
                                    json.dumps(c.wiki_links),
                                    json.dumps(c.frontmatter),
                                    c.chunk_index,
                                )
                                for c in chunks
                            ]
                            conn.executemany("""
                                INSERT INTO obsidian_chunks
                                (file_path, note_title, breadcrumb, content, tags, wiki_links, frontmatter, chunk_index)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, insert_rows)

                            # Update sync state
                            conn.execute("""
                                INSERT INTO obsidian_sync_state (file_path, file_hash, mtime, chunk_count, last_synced)
                                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(file_path) DO UPDATE SET
                                    file_hash = excluded.file_hash,
                                    mtime = excluded.mtime,
                                    chunk_count = excluded.chunk_count,
                                    last_synced = CURRENT_TIMESTAMP
                            """, (f_path, f_hash, mtime, len(chunks)))

                        elif action == "DELETE_NOTE":
                            f_path = op["file_path"]
                            conn.execute("DELETE FROM obsidian_chunks WHERE file_path = ?", (f_path,))
                            conn.execute("DELETE FROM obsidian_sync_state WHERE file_path = ?", (f_path,))
            except Exception as e:
                logger.error("Obsidian DB batch commit error: %s", e)
            finally:
                batch_ops = []

        while not self._stop_event.is_set():
            try:
                item = self._db_queue.get(timeout=0.1)
                if item is None:
                    _flush_batch()
                    break

                batch_ops.append(item)
                if len(batch_ops) >= self.batch_size:
                    _flush_batch()
                self._db_queue.task_done()

            except queue.Empty:
                if batch_ops:
                    _flush_batch()

        _flush_batch()
        conn.close()

    def _scanner_loop(self):
        """Main non-blocking scanner loop executing incremental differential sweeps."""
        # Initial boot sweep after brief grace cushion (1.0s)
        time.sleep(1.0)

        while not self._stop_event.is_set():
            if not self.vault_path.exists():
                # Vault directory not yet configured or mounted; sleep interval
                self._stop_event.wait(timeout=self.sync_interval)
                continue

            try:
                self.sync_now()
            except Exception as err:
                logger.error("Error during Obsidian incremental scan pass: %s", err)

            # Wait for next interval or explicit manual trigger
            self._sync_trigger.wait(timeout=self.sync_interval)
            self._sync_trigger.clear()

    def sync_now(self) -> SyncStats:
        """Executes a single differential incremental synchronization sweep."""
        start_time = time.monotonic()
        stats = SyncStats()

        if not self.vault_path.exists() or not self.vault_path.is_dir():
            return stats

        discovered_rel_paths: set[str] = set()

        # 1. Fast recursive filesystem traversal using os.scandir
        def _scan_dir(current_dir: Path):
            try:
                with os.scandir(str(current_dir)) as entries:
                    for entry in entries:
                        if self._stop_event.is_set():
                            break
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in self.IGNORED_DIRS and not entry.name.startswith("."):
                                _scan_dir(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
                            try:
                                rel_path = str(Path(entry.path).relative_to(self.vault_path)).replace("\\", "/")
                                stat = entry.stat()
                                discovered_rel_paths.add(rel_path)
                                self._process_file_delta(rel_path, Path(entry.path), stat.st_mtime, stats)
                            except Exception as file_err:
                                logger.debug("Skipping file '%s': %s", entry.name, file_err)

                        # Micro-sleep to yield CPU and protect sub-100ms CUDA voice pipeline
                        time.sleep(0.001)
            except Exception as scan_err:
                logger.debug("Directory scan notice for '%s': %s", current_dir, scan_err)

        _scan_dir(self.vault_path)

        # 2. Identify deleted files (present in stat cache / DB, absent from disk)
        with self._cache_lock:
            cached_paths = set(self._stat_cache.keys())
            deleted_paths = cached_paths - discovered_rel_paths
            for del_path in deleted_paths:
                del self._stat_cache[del_path]
                self._db_queue.put({
                    "action": "DELETE_NOTE",
                    "file_path": del_path,
                })
                stats.deleted_files += 1

        stats.total_files = len(discovered_rel_paths)
        stats.elapsed_ms = (time.monotonic() - start_time) * 1000.0

        # If background DB thread is not active, flush queue synchronously
        if not self._is_running or not (self._db_thread and self._db_thread.is_alive()):
            self._flush_pending_db_queue()

        if stats.modified_files > 0 or stats.deleted_files > 0:
            logger.info(
                "Obsidian Sync: %d modified, %d deleted, %d total chunks (%.1fms)",
                stats.modified_files, stats.deleted_files, stats.total_chunks, stats.elapsed_ms
            )
        return stats

    def _flush_pending_db_queue(self):
        """Flushes queued operations synchronously when background DB thread is inactive."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_db_schema(conn)
        try:
            with conn:
                while not self._db_queue.empty():
                    try:
                        op = self._db_queue.get_nowait()
                    except queue.Empty:
                        break
                    if op is None:
                        continue
                    action = op.get("action")
                    if action == "UPSERT_NOTE":
                        f_path = op["file_path"]
                        f_hash = op["file_hash"]
                        mtime = op["mtime"]
                        chunks: list[ObsidianChunk] = op["chunks"]

                        conn.execute("DELETE FROM obsidian_chunks WHERE file_path = ?", (f_path,))
                        insert_rows = [
                            (
                                c.file_path,
                                c.note_title,
                                c.breadcrumb,
                                c.content,
                                json.dumps(c.tags),
                                json.dumps(c.wiki_links),
                                json.dumps(c.frontmatter),
                                c.chunk_index,
                            )
                            for c in chunks
                        ]
                        conn.executemany("""
                            INSERT INTO obsidian_chunks
                            (file_path, note_title, breadcrumb, content, tags, wiki_links, frontmatter, chunk_index)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, insert_rows)

                        conn.execute("""
                            INSERT INTO obsidian_sync_state (file_path, file_hash, mtime, chunk_count, last_synced)
                            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(file_path) DO UPDATE SET
                                file_hash = excluded.file_hash,
                                mtime = excluded.mtime,
                                chunk_count = excluded.chunk_count,
                                last_synced = CURRENT_TIMESTAMP
                        """, (f_path, f_hash, mtime, len(chunks)))

                    elif action == "DELETE_NOTE":
                        f_path = op["file_path"]
                        conn.execute("DELETE FROM obsidian_chunks WHERE file_path = ?", (f_path,))
                        conn.execute("DELETE FROM obsidian_sync_state WHERE file_path = ?", (f_path,))
                    self._db_queue.task_done()
        except Exception as e:
            logger.error("Sync direct DB flush error: %s", e)
        finally:
            conn.close()

    def _process_file_delta(self, rel_path: str, full_path: Path, mtime: float, stats: SyncStats):
        """Evaluates mtime and inline SHA-256 to only ingest new or altered Markdown files."""
        with self._cache_lock:
            cached = self._stat_cache.get(rel_path)

        # Fast-Path: mtime unchanged -> zero file reading, zero SHA computation
        if cached and cached[0] == mtime:
            return

        # Read content and compute SHA-256
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            logger.warning("Could not read note '%s': %s", rel_path, e)
            return

        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # If mtime shifted but content SHA matches, update mtime in cache and skip
        if cached and cached[1] == sha:
            with self._cache_lock:
                self._stat_cache[rel_path] = (mtime, sha)
            return

        # New or genuinely modified note: parse and chunk
        chunks = ObsidianNoteParser.chunk_markdown(
            rel_path=rel_path,
            content=content,
            max_chunk_chars=self.max_chunk_chars,
        )

        with self._cache_lock:
            self._stat_cache[rel_path] = (mtime, sha)

        self._db_queue.put({
            "action": "UPSERT_NOTE",
            "file_path": rel_path,
            "file_hash": sha,
            "mtime": mtime,
            "chunks": chunks,
        })

        stats.modified_files += 1
        stats.total_chunks += len(chunks)

    def query_context(
        self,
        query: str,
        limit: int = 5,
        tag_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Queries Obsidian episodic context chunks using FTS5 BM25 ranking with keyword hit-density fallback."""
        sanitized = sanitize_fts5_query(query)
        if sanitized:
            try:
                conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    sql = """
                        SELECT c.id, c.file_path, c.note_title, c.breadcrumb, c.content, c.tags, c.wiki_links
                        FROM obsidian_chunks_fts fts
                        JOIN obsidian_chunks c ON fts.rowid = c.id
                        WHERE obsidian_chunks_fts MATCH ?
                    """
                    params: list[Any] = [sanitized]
                    if tag_filter:
                        sql += " AND c.tags LIKE ?"
                        params.append(f"%{tag_filter}%")
                    sql += " ORDER BY bm25(obsidian_chunks_fts) ASC LIMIT ?"
                    params.append(limit)

                    cursor = conn.cursor()
                    cursor.execute(sql, params)
                    rows = cursor.fetchall()
                    if rows:
                        results = []
                        for r in rows:
                            raw_tags = r["tags"]
                            raw_links = r["wiki_links"]
                            results.append({
                                "id": r["id"],
                                "file_path": r["file_path"],
                                "note_title": r["note_title"],
                                "breadcrumb": r["breadcrumb"],
                                "content": r["content"],
                                "tags": json.loads(raw_tags) if isinstance(raw_tags, str) else (raw_tags or []),
                                "wiki_links": json.loads(raw_links) if isinstance(raw_links, str) else (raw_links or []),
                            })
                        return results
                finally:
                    conn.close()
            except Exception as e:
                logger.debug("Obsidian FTS5 query_context fallback to LIKE: %s", e)

        # Standard LIKE token hit-density fallback
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            tokens = [t.lower() for t in re.sub(r"[^\w\s]", " ", query).split() if len(t) > 1]
            if not tokens:
                return []

            sql = "SELECT id, file_path, note_title, breadcrumb, content, tags, wiki_links FROM obsidian_chunks"
            params: list[Any] = []
            conditions: list[str] = []

            if tag_filter:
                conditions.append("tags LIKE ?")
                params.append(f"%{tag_filter}%")

            # Like search on breadcrumbs or content
            token_likes = []
            for tok in tokens[:4]:
                token_likes.append("(content LIKE ? OR breadcrumb LIKE ? OR note_title LIKE ?)")
                params.extend([f"%{tok}%", f"%{tok}%", f"%{tok}%"])

            if token_likes:
                conditions.append(f"({' OR '.join(token_likes)})")

            if conditions:
                sql += f" WHERE {' AND '.join(conditions)}"

            sql += " ORDER BY id DESC LIMIT 50"

            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()

            # Rank candidate chunks by token hit density
            scored_candidates = []
            for r in rows:
                content_lower = (r["content"] or "").lower()
                title_lower = (r["note_title"] or "").lower()
                breadcrumb_lower = (r["breadcrumb"] or "").lower()

                score = 0
                for tok in tokens:
                    if tok in title_lower:
                        score += 5
                    if tok in breadcrumb_lower:
                        score += 3
                    score += content_lower.count(tok)

                scored_candidates.append((score, {
                    "id": r["id"],
                    "file_path": r["file_path"],
                    "note_title": r["note_title"],
                    "breadcrumb": r["breadcrumb"],
                    "content": r["content"],
                    "tags": json.loads(r["tags"] or "[]"),
                    "wiki_links": json.loads(r["wiki_links"] or "[]"),
                }))

            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            return [item for _, item in scored_candidates[:limit]]
        finally:
            conn.close()
