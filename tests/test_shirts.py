"""Shirt image fetching: cached after the first hit, tolerant of failures."""
from __future__ import annotations

import httpx
import pytest
import respx

from fplbot.bot import shirts

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _fresh_cache():
    shirts._cache.clear()
    yield
    shirts._cache.clear()


def test_url_uses_club_code_and_goalkeeper_suffix():
    assert shirts.shirt_url(3, False).endswith("/shirt_3-220.png")
    assert shirts.shirt_url(3, True).endswith("/shirt_3_1-220.png")


@respx.mock
async def test_fetches_each_kit_once_then_serves_from_cache():
    route = respx.get(shirts.shirt_url(3, False)).mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )
    first = await shirts.fetch_shirts({(3, False)})
    second = await shirts.fetch_shirts({(3, False)})
    assert first == {(3, False): PNG} == second
    assert route.call_count == 1
    assert shirts.cached_count() == 1


@respx.mock
async def test_failures_are_left_out_and_not_cached():
    respx.get(shirts.shirt_url(3, False)).mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )
    respx.get(shirts.shirt_url(99, False)).mock(return_value=httpx.Response(404))
    respx.get(shirts.shirt_url(98, True)).mock(side_effect=httpx.ConnectError("down"))
    respx.get(shirts.shirt_url(97, False)).mock(
        return_value=httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    )
    got = await shirts.fetch_shirts({(3, False), (99, False), (98, True), (97, False)})
    assert got == {(3, False): PNG}
    assert shirts.cached_count() == 1
