"""Redis cache with in-memory TTL fallback when REDIS_URL is empty."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

_memory: dict[str, tuple[float, str]] = {}
_memory_lock = threading.Lock()
_client = None
_client_failed = False


def _redis():
    global _client, _client_failed
    if _client_failed:
        return None
    if _client is not None:
        return _client
    url = (settings().redis_url or "").strip()
    if not url:
        return None
    try:
        import redis

        _client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        _client.ping()
        log.info("Redis cache connected")
        return _client
    except Exception as error:  # noqa: BLE001
        _client_failed = True
        log.warning("Redis unavailable, using memory cache: %s", error)
        return None


def cache_get(key: str) -> Any | None:
    client = _redis()
    if client is not None:
        try:
            raw = client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as error:  # noqa: BLE001
            log.warning("Redis get failed for %s: %s", key, error)
            return None
    now = time.time()
    with _memory_lock:
        item = _memory.get(key)
        if not item:
            return None
        expires, raw = item
        if expires < now:
            _memory.pop(key, None)
            return None
        return json.loads(raw)


def cache_set(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    ttl = int(ttl_seconds if ttl_seconds is not None else settings().retail_cache_ttl_seconds)
    ttl = max(5, ttl)
    raw = json.dumps(value, ensure_ascii=False, default=str)
    client = _redis()
    if client is not None:
        try:
            client.setex(key, ttl, raw)
            return
        except Exception as error:  # noqa: BLE001
            log.warning("Redis set failed for %s: %s", key, error)
    with _memory_lock:
        _memory[key] = (time.time() + ttl, raw)


def cache_delete(*keys: str) -> None:
    client = _redis()
    if client is not None and keys:
        try:
            client.delete(*keys)
        except Exception as error:  # noqa: BLE001
            log.warning("Redis delete failed: %s", error)
    with _memory_lock:
        for key in keys:
            _memory.pop(key, None)


def cache_delete_prefix(prefix: str) -> None:
    client = _redis()
    if client is not None:
        try:
            for key in client.scan_iter(match=f"{prefix}*", count=200):
                client.delete(key)
        except Exception as error:  # noqa: BLE001
            log.warning("Redis prefix delete failed: %s", error)
    with _memory_lock:
        for key in [k for k in _memory if k.startswith(prefix)]:
            _memory.pop(key, None)


RETAIL_RECOMMENDED_KEY = "retail:recommended:v1"
RETAIL_JOB_KEY = "retail:job:active:v1"
RETAIL_ANALYSIS_PREFIX = "retail:analysis:"


def invalidate_retail_lists() -> None:
    cache_delete(RETAIL_RECOMMENDED_KEY, RETAIL_JOB_KEY)
