"""Read-Only SQLite connection pooling and offloaded vector search shims for Jarvis 2.0.

Re-exports canonical pool implementations from jarvis.memory.ro_pool.
"""
from jarvis.memory.ro_pool import (
    VectorMemoryPool,
    ReadOnlyConnectionPool,
    get_ro_connection,
)

__all__ = [
    "VectorMemoryPool",
    "ReadOnlyConnectionPool",
    "get_ro_connection",
]
