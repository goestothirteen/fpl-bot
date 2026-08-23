#!/usr/bin/env python
"""Render a real league's live table to the terminal — no Telegram, no database.

    python scripts/smoke.py 314

The fastest way to check the FPL side of the bot still works after an API
change, and a handy way to eyeball formatting.
"""
from __future__ import annotations

import asyncio
import re
import sys

from fplbot.fpl.client import FPLClient
from fplbot.services import analysis
from fplbot.services.live import LiveEngine
from fplbot.services.parsing import parse_live, parse_players

TAGS = re.compile(r"<[^>]+>")


def plain(html: str) -> str:
    """Strip the Telegram HTML so it reads in a terminal."""
    return TAGS.sub("", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def main(league_id: int, limit: int) -> None:
    from fplbot.bot.formatting import (
        render_bench,
        render_captains,
        render_differentials,
        render_live_table,
        render_remaining,
    )

    async with FPLClient(None, max_concurrency=4, rate_per_sec=4) as client:
        engine = LiveEngine(client)
        event = await engine.resolve_event()
        info = await engine.phase()
        print(
            f"gameweek {event} · phase {info.phase} · "
            f"next kickoff {info.next_kickoff} · league {league_id}\n"
        )

        table = await engine.build_table(league_id, event, limit=limit)
        players = parse_players(await client.bootstrap())
        live = parse_live(await client.live(event))

        for block in (
            render_live_table(table),
            render_remaining(table),
            render_captains(analysis.captain_spread(table, live), players),
            render_differentials(analysis.differentials(table, live), players),
            render_bench(analysis.bench_disasters(table)),
        ):
            print(plain(block), "\n")

        from fplbot.bot.formatting import render_awards

        print(plain(render_awards(analysis.awards(table, players, live))))


if __name__ == "__main__":
    league = int(sys.argv[1]) if len(sys.argv) > 1 else 314
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    asyncio.run(main(league, count))
