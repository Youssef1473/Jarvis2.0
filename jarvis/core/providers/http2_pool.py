"""HTTP/2 Multiplexed Session Pool for Jarvis 2.0.

Provides a warm, connection-pooled HTTP client with HTTP/2 support (when h2 is installed)
and keepalive reuse, eliminating 200-350ms TLS handshake penalties on external API turns.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import httpx

logger = logging.getLogger("Jarvis.HTTP2Pool")

_lock = threading.Lock()
_warm_client: Optional[httpx.Client] = None


def _check_h2_installed() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False


def get_warm_http2_client() -> httpx.Client:
    """Returns the process-wide warm HTTP/2 client singleton.

    Configured with max 20 keepalive connections, 300.0s keepalive expiry,
    and automatic HTTP/2 negotiation if h2 is present.
    """
    global _warm_client
    if _warm_client is not None and not _warm_client.is_closed:
        return _warm_client

    with _lock:
        if _warm_client is None or _warm_client.is_closed:
            has_h2 = _check_h2_installed()
            limits = httpx.Limits(max_keepalive_connections=20, keepalive_expiry=300.0)
            timeout = httpx.Timeout(35.0, connect=10.0)
            _warm_client = httpx.Client(
                http2=has_h2,
                limits=limits,
                timeout=timeout,
            )
            logger.info("Initialized warm HTTP client (http2=%s, max_keepalive=20)", has_h2)
    return _warm_client


def close_warm_http2_client() -> None:
    """Terminates and closes the warm HTTP client singleton."""
    global _warm_client
    with _lock:
        if _warm_client is not None and not _warm_client.is_closed:
            try:
                _warm_client.close()
            except Exception as e:
                logger.debug("Error closing warm HTTP client: %s", e)
            finally:
                _warm_client = None


__all__ = ["get_warm_http2_client", "close_warm_http2_client"]
