"""Async client for the public Fantasy Premier League API.

Everything here is unauthenticated — see docs/API_RESEARCH.md. The client's job
is to be a good citizen of an undocumented API: throttle, retry with backoff,
cache aggressively, and prefer stale data over hammering.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from redis.asyncio import Redis

from ..logging_conf import get_logger
from .cache import CachePolicy, ResponseCache
from .errors import NotFound, SoftBlocked, UpstreamUnavailable

log = get_logger(__name__)

BASE_URL = "https://fantasy.premierleague.com/api"

# A plain browser UA. FPL serves JSON to anything, but a python-httpx UA is an
# unnecessary flag to wave at Cloudflare.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Cache policies, tuned in docs/API_RESEARCH.md §8.
POLICY = {
    "bootstrap": CachePolicy.of(6 * 3600, hard_multiplier=8),
    "bootstrap_near_deadline": CachePolicy.of(1800, hard_multiplier=24),
    "event_status": CachePolicy.of(60, hard_multiplier=60),
    "fixtures_live": CachePolicy.of(30, hard_multiplier=120),
    "fixtures_idle": CachePolicy.of(1800, hard_multiplier=24),
    "live": CachePolicy.of(45, hard_multiplier=80),
    "live_settled": CachePolicy.of(3600, hard_multiplier=24),
    "standings": CachePolicy.of(600, hard_multiplier=24),
    "picks": CachePolicy.of(6 * 3600, hard_multiplier=28),  # immutable post-deadline
    "entry": CachePolicy.of(3600, hard_multiplier=24),
    "history": CachePolicy.of(3600, hard_multiplier=24),
    "transfers": CachePolicy.of(600, hard_multiplier=36),
    "element": CachePolicy.of(6 * 3600, hard_multiplier=8),
    "misc": CachePolicy.of(3600, hard_multiplier=24),
}


class _TokenBucket:
    """Simple async rate limiter — `rate` requests per second, burst = rate."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = rate
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._rate, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)


class FPLClient:
    def __init__(
        self,
        redis: Redis | None = None,
        *,
        max_concurrency: int = 5,
        rate_per_sec: float = 4.0,
        timeout: float = 15.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(timeout, connect=8.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            http2=True,
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._bucket = _TokenBucket(rate_per_sec)
        self.cache = ResponseCache(redis)
        self._blocked_until = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FPLClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── transport ──────────────────────────────────────────────────────────
    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if time.monotonic() < self._blocked_until:
            raise SoftBlocked("backing off after an upstream block")

        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(4):
            await self._bucket.acquire()
            async with self._sem:
                try:
                    resp = await self._http.get(path, params=params)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 404:
                        raise NotFound(path)
                    if resp.status_code == 403:
                        # Public endpoints never legitimately 403. Assume block.
                        self._blocked_until = time.monotonic() + 300
                        log.error("fpl.soft_blocked", path=path)
                        raise SoftBlocked(path)
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        log.warning("fpl.rate_limited", path=path, retry_after=retry_after)
                        await asyncio.sleep(min(retry_after, 60))
                        delay = min(delay * 2, 60)
                        last_exc = UpstreamUnavailable(f"429 on {path}")
                        continue
                    if resp.status_code >= 500:
                        last_exc = UpstreamUnavailable(f"{resp.status_code} on {path}")
                    else:
                        resp.raise_for_status()
                        return resp.json()

            await asyncio.sleep(delay * (1 + 0.25 * attempt))
            delay = min(delay * 2, 30)

        raise UpstreamUnavailable(str(last_exc) or path)

    async def _cached(
        self, key: str, path: str, policy_name: str, params: dict[str, Any] | None = None, *,
        force: bool = False,
    ) -> Any:
        payload, _age = await self.cache.get_or_fetch(
            key, POLICY[policy_name], lambda: self._request(path, params), force=force
        )
        return payload

    async def cached_with_age(
        self, key: str, path: str, policy_name: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, float]:
        return await self.cache.get_or_fetch(
            key, POLICY[policy_name], lambda: self._request(path, params)
        )

    # ── endpoints ──────────────────────────────────────────────────────────
    async def bootstrap(self, *, near_deadline: bool = False) -> dict:
        policy = "bootstrap_near_deadline" if near_deadline else "bootstrap"
        return await self._cached("bootstrap", "/bootstrap-static/", policy)

    async def event_status(self) -> dict:
        return await self._cached("event-status", "/event-status/", "event_status")

    async def fixtures(self, event: int | None = None, *, live: bool = False) -> list[dict]:
        key = f"fixtures:{event or 'all'}"
        params = {"event": event} if event else None
        return await self._cached(key, "/fixtures/", "fixtures_live" if live else "fixtures_idle", params)

    async def live(self, event: int, *, settled: bool = False) -> dict:
        return await self._cached(
            f"live:{event}", f"/event/{event}/live/", "live_settled" if settled else "live"
        )

    async def entry(self, entry_id: int) -> dict:
        return await self._cached(f"entry:{entry_id}", f"/entry/{entry_id}/", "entry")

    async def entry_history(self, entry_id: int) -> dict:
        return await self._cached(f"history:{entry_id}", f"/entry/{entry_id}/history/", "history")

    async def entry_transfers(self, entry_id: int) -> list[dict]:
        return await self._cached(
            f"transfers:{entry_id}", f"/entry/{entry_id}/transfers/", "transfers"
        )

    async def picks(self, entry_id: int, event: int) -> dict:
        """404s before the gameweek deadline — callers must handle NotFound."""
        return await self._cached(
            f"picks:{entry_id}:{event}", f"/entry/{entry_id}/event/{event}/picks/", "picks"
        )

    async def element_summary(self, element_id: int) -> dict:
        return await self._cached(
            f"element:{element_id}", f"/element-summary/{element_id}/", "element"
        )

    async def dream_team(self, event: int) -> dict:
        return await self._cached(f"dream:{event}", f"/dream-team/{event}/", "misc")

    async def set_piece_notes(self) -> dict:
        return await self._cached("set-piece", "/team/set-piece-notes/", "misc")

    async def classic_standings(self, league_id: int, page: int = 1, phase: int = 1) -> dict:
        return await self._cached(
            f"standings:{league_id}:{page}:{phase}",
            f"/leagues-classic/{league_id}/standings/",
            "standings",
            {"page_standings": page, "phase": phase},
        )

    async def h2h_standings(self, league_id: int, page: int = 1) -> dict:
        return await self._cached(
            f"h2h-standings:{league_id}:{page}",
            f"/leagues-h2h/{league_id}/standings/",
            "standings",
            {"page_standings": page},
        )

    async def h2h_matches(self, league_id: int, page: int = 1, event: int | None = None) -> dict:
        params: dict[str, Any] = {"page": page}
        if event:
            params["event"] = event
        return await self._cached(
            f"h2h-matches:{league_id}:{page}:{event or 0}",
            f"/leagues-h2h-matches/league/{league_id}/",
            "standings",
            params,
        )

    async def cup_status(self, league_id: int) -> dict:
        return await self._cached(f"cup:{league_id}", f"/league/{league_id}/cup-status/", "misc")

    # ── composites ─────────────────────────────────────────────────────────
    async def all_classic_entries(
        self, league_id: int, max_pages: int = 4, limit: int | None = None
    ) -> tuple[dict, list[dict]]:
        """Walk the 50-per-page standings until exhausted.

        Returns ``(league_meta, results)``. A friend league is one page. The
        caps matter: someone typing `/link 314` would otherwise pull nine
        million rows and then try to fetch picks for every one of them.
        """
        first = await self.classic_standings(league_id, page=1)
        meta = first["league"]
        results = list(first["standings"]["results"])
        page, current = 1, first
        while current["standings"].get("has_next") and page < max_pages:
            if limit is not None and len(results) >= limit:
                break
            page += 1
            current = await self.classic_standings(league_id, page=page)
            results.extend(current["standings"]["results"])
        if limit is not None:
            results = results[:limit]
        return meta, results

    async def many_picks(self, entry_ids: list[int], event: int) -> dict[int, dict]:
        """Fetch picks for a whole league concurrently, tolerating individual
        failures (a manager who joined late has no picks for early gameweeks)."""

        async def one(eid: int) -> tuple[int, dict | None]:
            try:
                return eid, await self.picks(eid, event)
            except NotFound:
                return eid, None
            except UpstreamUnavailable as exc:
                log.warning("fpl.picks_failed", entry=eid, event=event, error=str(exc))
                return eid, None

        pairs = await asyncio.gather(*(one(e) for e in entry_ids))
        return {eid: p for eid, p in pairs if p is not None}
