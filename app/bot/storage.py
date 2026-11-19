"""Small key-value store for non-financial, expiring state: update dedupe, rate limits,
inline-mode drafts, one-time confirmation tokens. Redis in production, memory in tests.

Losing this state never loses money: deals, payments and payouts live in PostgreSQL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from redis.asyncio import Redis


class KeyValueStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int) -> None: ...

    async def set_nx(self, key: str, value: str, ttl: int) -> bool: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl: int) -> int:
        """Increment a counter; the TTL starts with the first increment (fixed window)."""
        ...


class RedisKV:
    def __init__(self, redis: Redis, *, prefix: str = "escrow:") -> None:
        self._r = redis
        self._p = prefix

    async def get(self, key: str) -> str | None:
        value = await self._r.get(self._p + key)
        return value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._r.set(self._p + key, value, ex=ttl)

    async def set_nx(self, key: str, value: str, ttl: int) -> bool:
        return bool(await self._r.set(self._p + key, value, ex=ttl, nx=True))

    async def delete(self, key: str) -> None:
        await self._r.delete(self._p + key)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.incr(self._p + key)
            pipe.expire(self._p + key, ttl, nx=True)
            count, _ = await pipe.execute()
        return int(count)


class MemoryKV:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._clock = clock

    def _alive(self, key: str) -> tuple[str, float] | None:
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] <= self._clock():
            del self._data[key]
            return None
        return item

    async def get(self, key: str) -> str | None:
        item = self._alive(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._data[key] = (value, self._clock() + ttl)

    async def set_nx(self, key: str, value: str, ttl: int) -> bool:
        if self._alive(key):
            return False
        self._data[key] = (value, self._clock() + ttl)
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        item = self._alive(key)
        if item is None:
            self._data[key] = ("1", self._clock() + ttl)
            return 1
        count = int(item[0]) + 1
        self._data[key] = (str(count), item[1])
        return count
