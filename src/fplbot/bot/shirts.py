"""Club shirt images for the pitch view, fetched from FPL's own static assets.

The FPL web app draws each pick as the club's shirt, served as transparent
PNGs from fantasy.premierleague.com/dist/img/shirts/standard/shirt_<code>-<w>.png,
with a `_1` suffix for the goalkeeper kit. `<code>` is the club's stable FPL
code (teams[].code in bootstrap-static), not its 1–20 id for the season.

There are only ~20 clubs × 2 kits, so everything is cached in memory for the
life of the process. A failed fetch is not cached: the renderer draws a plain
placeholder shirt for that pick and we try again on the next view. These are
static files on a CDN, not the rate-limited API, so they get their own small
client rather than going through FplClient's throttle.
"""
from __future__ import annotations

import asyncio

import httpx

from ..fpl.client import USER_AGENT
from ..logging_conf import get_logger

log = get_logger(__name__)

SHIRT_URL = "https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_{code}{kit}-220.png"

ShirtKey = tuple[int, bool]   # (club code, is goalkeeper)

_cache: dict[ShirtKey, bytes] = {}
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/*"},
            follow_redirects=True,
        )
    return _client


def shirt_url(code: int, goalkeeper: bool) -> str:
    return SHIRT_URL.format(code=code, kit="_1" if goalkeeper else "")


async def _fetch(key: ShirtKey) -> bytes | None:
    if key in _cache:
        return _cache[key]
    code, gk = key
    try:
        resp = await _http().get(shirt_url(code, gk))
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        log.warning("shirt.fetch_failed", code=code, goalkeeper=gk, error=str(exc))
        return None
    if not resp.content or not resp.headers.get("content-type", "").startswith("image/"):
        log.warning("shirt.not_an_image", code=code, goalkeeper=gk)
        return None
    _cache[key] = resp.content
    return resp.content


async def fetch_shirts(keys: set[ShirtKey]) -> dict[ShirtKey, bytes]:
    """PNG bytes for every (club code, is_gk) requested that could be fetched.

    Missing entries mean "draw the placeholder"; the caller never has to care
    whether that was a network blip or a club FPL has no image for.
    """
    wanted = list(keys)
    results = await asyncio.gather(*(_fetch(k) for k in wanted))
    return {k: png for k, png in zip(wanted, results, strict=True) if png is not None}


def cached_count() -> int:
    return len(_cache)
