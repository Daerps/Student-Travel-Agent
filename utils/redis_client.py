"""Optional Redis helpers used by memory and cache layers.

Redis is treated as a best-effort acceleration layer. Callers should keep a
durable source of truth, and this module will safely degrade to no-op when
Redis is disabled, unavailable, or the redis package is not installed.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

_CLIENT = None
_CLIENT_INITIALIZED = False


def get_redis_config() -> dict:
    try:
        from config import REDIS_CONFIG

        return REDIS_CONFIG
    except Exception:
        return {"enabled": False}


def is_redis_enabled() -> bool:
    return bool(get_redis_config().get("enabled"))


def get_redis_client():
    """Return a ping-checked Redis client, or None when unavailable."""
    global _CLIENT, _CLIENT_INITIALIZED

    if _CLIENT_INITIALIZED:
        return _CLIENT

    _CLIENT_INITIALIZED = True
    cfg = get_redis_config()
    if not cfg.get("enabled"):
        logger.debug("Redis disabled by config")
        return None

    try:
        import redis
    except ImportError:
        logger.warning("Redis enabled but redis package is not installed")
        return None

    try:
        _CLIENT = redis.Redis.from_url(
            cfg.get("url", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=cfg.get("socket_timeout", 1.0),
            socket_connect_timeout=cfg.get("socket_timeout", 1.0),
        )
        _CLIENT.ping()
        logger.info("Redis client initialized")
        return _CLIENT
    except Exception as exc:
        logger.warning("Redis unavailable, continuing without cache: %s", exc)
        _CLIENT = None
        return None


def key_for(*parts: Any) -> str:
    cfg = get_redis_config()
    prefix = str(cfg.get("key_prefix", "travel_agent")).strip(":")
    safe_parts = [quote(str(part), safe="") for part in parts if part is not None]
    return ":".join([prefix, *safe_parts])


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def get_json(client, key: str, default: Any = None) -> Any:
    if client is None:
        return default
    try:
        return loads(client.get(key), default=default)
    except Exception as exc:
        logger.debug("Redis GET failed for %s: %s", key, exc)
        return default


def set_json(client, key: str, value: Any, ttl: Optional[int] = None) -> bool:
    if client is None:
        return False
    try:
        payload = dumps(value)
        if ttl:
            client.setex(key, ttl, payload)
        else:
            client.set(key, payload)
        return True
    except Exception as exc:
        logger.debug("Redis SET failed for %s: %s", key, exc)
        return False


def delete_keys(client, keys: Iterable[str]) -> int:
    if client is None:
        return 0
    key_list = [key for key in keys if key]
    if not key_list:
        return 0
    try:
        return int(client.delete(*key_list))
    except Exception as exc:
        logger.debug("Redis DEL failed for %s: %s", key_list, exc)
        return 0


def delete_pattern(client, pattern: str) -> int:
    if client is None:
        return 0
    deleted = 0
    try:
        batch = []
        for key in client.scan_iter(match=pattern, count=100):
            batch.append(key)
            if len(batch) >= 100:
                deleted += int(client.delete(*batch))
                batch = []
        if batch:
            deleted += int(client.delete(*batch))
    except Exception as exc:
        logger.debug("Redis pattern delete failed for %s: %s", pattern, exc)
    return deleted
