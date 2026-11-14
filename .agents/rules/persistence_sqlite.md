---
trigger: model_decision
description: SQLite WAL persistence, SingleWriterActor serialization, sqlite-vec KNN search, and memory chunk formatting invariants.
---

# Persistence & SQLite Integrity Invariants

These invariants govern persistent memory, SQLite transactions, vector embeddings, and retrieval formatting in Jarvis 2.0.

## 1. Concurrency & Transaction Serialization
- All write transactions across the database must serialize strictly through `SingleWriterActor`. Concurrent write transactions are prohibited.
- SQLite connections must use Write-Ahead Logging (`PRAGMA journal_mode=WAL`), `PRAGMA synchronous=NORMAL`, and memory-mapped I/O (`PRAGMA mmap_size=268435456` / 256MB).

## 2. Hybrid Retrieval & Vector Search Storage
- Semantic similarity searches must utilize `sqlite-vec` KNN bindings in conjunction with SQLite FTS5 BM25 text indexing.
- Keep persistence focused strictly on serialization, schema migrations, and storage invariants; document chunking and reference block formatting belong to retrieval/context orchestration (`jarvis.context`).

## 3. Configuration Exclusivity
- `config/config.yaml` is the canonical, exclusive configuration file loaded by `jarvis.core.config_loader:load_config()`.
- Never create, assume, or load alternative configuration paths (such as `config/config.json`) without verifying loader logic.

