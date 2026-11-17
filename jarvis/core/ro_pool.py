"""Read-Only SQLite connection pooling and offloaded vector search shims for Jarvis 2.0.

Re-exports canonical pool implementations from jarvis.memory.ro_pool.
"""
from jarvis.memory.ro_pool import (
    DEFAULT_RO_QUERY_TIMEOUT_MS,
    DEFAULT_RO_QUERY_TIMEOUT_SECONDS,
    ReadOnlyConnectionPool,
    VectorMemoryPool,
    bounded_ro_connection,
    get_ro_connection,
)

__all__ = [
    "DEFAULT_RO_QUERY_TIMEOUT_MS",
    "DEFAULT_RO_QUERY_TIMEOUT_SECONDS",
    "ReadOnlyConnectionPool",
    "VectorMemoryPool",
    "bounded_ro_connection",
    "get_ro_connection",
]
