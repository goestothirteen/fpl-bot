"""Redis-backed HTTP cache with soft/hard TTLs, single-flight and stale-if-error.

Why two TTLs: a Telegram command must never block on a cold 1.6 MB
`bootstrap-static` fetch, and it must still answer when FPL is having a bad day.

    soft TTL  — past this, the value is refreshed, but the stale copy is
                returned immediately to the caller that triggered the refresh
                only when `serve_stale_while_revalidate` is set.
    hard TTL  — past this the value is deleted; it is only ever served if the
                upstream fetch raised.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import orjson
from redis.asyncio import Redis

from ..logging_conf import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CachePolicy:
    """How long a given endpoint's payload stays useful."""

    soft: float
    hard: float

    @classmethod
    def of(cls, soft: float, hard_multiplier: float = 20.0) -> CachePolicy:
        return cls(soft=soft, hard=soft * hard_multiplier)


@dataclass
class CacheEntry:
    payload: Any
    fetched_at: float

    @property
    def age(self) -> float:
        return time.time() - self.fetched_at


class ResponseCache:
    """Namespaced cache over Redis, with an in-process fallback for tests."""

    def __init__(self, redis: Redis | None, namespace: str = "fpl") -> None:
        self._redis = redis
        self._ns = namespace
        self._local: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _key(self, key: str) -> str:
        return f"{self._ns}:cache:{key}"

    async def get(self, key: str) -> CacheEntry | None:
        if self._redis is None:
            return self._local.get(key)
        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        try:
            obj = orjson.loads(raw)
            return CacheEntry(payload=obj["p"], fetched_at=obj["t"])
        except (orjson.JSONDecodeError, KeyError, TypeError):
            return None

    async def set(self, key: str, payload: Any, hard_ttl: float) -> None:
        entry = CacheEntry(payload=payload, fetched_at=time.time())
        if self._redis is None:
            self._local[key] = entry
            return
        await self._redis.set(
            self._key(key),
            orjson.dumps({"p": payload, "t": entry.fetched_at}),
            ex=int(hard_ttl),
        )

    def _lock(self, key: str) -> asyncio.Lock:
        # Process-local single-flight. With one bot instance (the supported
        # deployment) this is sufficient; a multi-instance setup would swap this
        # for a Redis SET NX lock.
        return self._locks.setdefault(key, asyncio.Lock())

    async def get_or_fetch(
        self,
        key: str,
        policy: CachePolicy,
        fetcher: Callable[[], Awaitable[Any]],
        *,
        force: bool = False,
    ) -> tuple[Any, float]:
        """Return ``(payload, age_seconds)``.

        ``age_seconds`` lets callers render a "data from N min ago" footer.
        """
        entry = None if force else await self.get(key)
        if entry is not None and entry.age < policy.soft:
            return entry.payload, entry.age

        async with self._lock(key):
            # Another coroutine may have refreshed while we waited on the lock.
            if not force:
                entry2 = await self.get(key)
                if entry2 is not None and entry2.age < policy.soft:
                    return entry2.payload, entry2.age
            try:
                payload = await fetcher()
            except Exception as exc:  # noqa: BLE001 - deliberate stale-if-error
                stale = entry or await self.get(key)
                if stale is not None:
                    log.warning("cache.stale_if_error", key=key, age=stale.age, error=str(exc))
                    return stale.payload, stale.age
                raise
            await self.set(key, payload, policy.hard)
            return payload, 0.0

    async def invalidate(self, key: str) -> None:
        self._local.pop(key, None)
        if self._redis is not None:
            await self._redis.delete(self._key(key))
