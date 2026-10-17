---
name: sqlite-optimization
description: >-
  High-performance SQLite WAL tuning, memory-mapped I/O, FTS5 BM25 search,
  and local C-vector search via sqlite-vec for Jarvis 2.0. Eliminates Windows file-lock
  contention and enables zero-server semantic similarity retrieval.
---

# SQLite Performance Tuning & Vector Memory Engine

This skill optimizes Jarvis 2.0 persistent memory, habit cache, and vector embeddings
for maximum throughput, concurrency, and sub-millisecond query latency.

---

## 1. Production SQLite Pragmas for Windows

Every database connection in Jarvis (`MemoryManager`, `ReflexEngine`) should execute
these pragmas immediately upon opening:

```python
conn.execute("PRAGMA journal_mode=WAL;")              # Write-Ahead Logging for non-blocking concurrent reads
conn.execute("PRAGMA synchronous=NORMAL;")            # Reduces fsync frequency without compromising WAL durability
conn.execute("PRAGMA busy_timeout=5000;")             # 5s wait instead of instant SQLITE_BUSY error
conn.execute("PRAGMA mmap_size=268435456;")           # 256MB memory-mapped I/O for lightning-fast reads
conn.execute("PRAGMA cache_size=-64000;")             # 64MB in-memory page cache
conn.execute("PRAGMA temp_store=MEMORY;")             # Store temporary tables in RAM
```

---

## 2. In-Database Vector Search with `sqlite-vec`

Load `sqlite-vec` without external server infrastructure (Milvus/Pinecone/Chroma):

```python
import sqlite3
import sqlite_vec
import struct

conn = sqlite3.connect("data/jarvis_memory.db")
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)

# Create virtual vector tables for 64-dim L2 SIMD embeddings in Jarvis 2.0
conn.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
    fact_id INTEGER PRIMARY KEY,
    embedding float[64]
);
""")
conn.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS documents_vec USING vec0(
    doc_id INTEGER PRIMARY KEY,
    embedding float[64]
);
""")
conn.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_vec USING vec0(
    lesson_id INTEGER PRIMARY KEY,
    embedding float[64]
);
""")

# Serialization via official sqlite_vec helper
blob = sqlite_vec.serialize_float32(query_vec)

# KNN Query with Exact Cosine Similarity (sub-0.5ms)
def search_nearest(conn, query_vec: list[float], limit=5, min_sim=0.30):
    blob = sqlite_vec.serialize_float32(query_vec)
    cur = conn.execute("""
        SELECT fact_id, distance
        FROM facts_vec
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance ASC
    """, [blob, limit])
    results = []
    for r in cur.fetchall():
        dist = float(r["distance"])
        cos_sim = max(0.0, 1.0 - ((dist * dist) / 2.0))
        if cos_sim >= min_sim:
            results.append({"id": r["fact_id"], "sim": cos_sim})
    return results

# Hybrid Reciprocal Rank Fusion (RRF: FTS5 BM25 + sqlite-vec KNN)
def hybrid_rrf_rank(fts_matches, vec_matches, k_rrf=60):
    scores = {}
    for rank, item in enumerate(fts_matches, start=1):
        scores[item["id"]] = scores.get(item["id"], 0.0) + (1.0 / (k_rrf + rank))
    for rank, item in enumerate(vec_matches, start=1):
        scores[item["id"]] = scores.get(item["id"], 0.0) + (1.0 / (k_rrf + rank))
    return sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
```

---

## 3. Safe Context-Managed Connection Protocol

Always wrap SQLite connections with explicit contextmanager cleanup to avoid WAL file locks on Windows:

```python
import contextlib
import sqlite3

@contextlib.contextmanager
def get_db_connection(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()
```
