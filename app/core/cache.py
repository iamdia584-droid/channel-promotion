"""Redis helpers: client, distributed locks, rate limiting, pacing buckets."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Iterator

import redis

from app.core.config import settings
from app.core.errors import Conflict, RateLimited

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=5, health_check_interval=30
        )
    return _client


# Release only if we still own the lock. A naive DEL can free someone else's lock
# after our TTL expired mid-work.
_UNLOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@contextmanager
def lock(key: str, ttl: int = 30, blocking: float = 0.0) -> Iterator[None]:
    """Distributed mutex. Used to serialise per-channel delivery and per-wallet writes."""
    client = get_redis()
    full = f"lock:{key}"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + blocking
    while True:
        if client.set(full, token, nx=True, ex=ttl):
            break
        if time.monotonic() >= deadline:
            raise Conflict("resource is locked", key=key)
        time.sleep(0.05)
    try:
        yield
    finally:
        try:
            client.eval(_UNLOCK, 1, full, token)
        except redis.RedisError:  # pragma: no cover - lock expires on its own
            pass


# Sliding-window counter. Atomic so concurrent requests cannot both slip through.
_RATE = """
local current = redis.call('incr', KEYS[1])
if current == 1 then
  redis.call('expire', KEYS[1], ARGV[1])
end
return current
"""


def rate_limit(key: str, limit: int, window: int) -> None:
    """Raise :class:`RateLimited` once ``limit`` hits occur inside ``window`` seconds."""
    if not settings.rate_limit_enabled:
        return
    client = get_redis()
    try:
        current = int(client.eval(_RATE, 1, f"rl:{key}", window))
    except redis.RedisError:
        return  # fail open: Redis being down must not lock users out of the bot
    if current > limit:
        raise RateLimited("too many requests", key=key, limit=limit, window=window)


def counter_incr(key: str, amount: int = 1, ttl: int | None = None) -> int:
    client = get_redis()
    pipe = client.pipeline()
    pipe.incrby(key, amount)
    if ttl:
        pipe.expire(key, ttl)
    return int(pipe.execute()[0])


def cache_get(key: str) -> str | None:
    try:
        return get_redis().get(key)
    except redis.RedisError:
        return None


def cache_set(key: str, value: str, ttl: int = 60) -> None:
    try:
        get_redis().setex(key, ttl, value)
    except redis.RedisError:
        pass


def cache_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        get_redis().delete(*keys)
    except redis.RedisError:
        pass
