"""Clock-driven jobs: deadline reminders, nightly maintenance, price alerts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..bot.formatting import esc, render_deadline
from ..db import repo
from ..db.session import session_scope
from ..live.notifier import Notifier
from ..logging_conf import get_logger
from ..services.live import LiveEngine
from ..services.parsing import parse_game_state, parse_players

log = get_logger(__name__)

# FPL deadlines are usually 18:30 UK time, which is 01:30 in Singapore. A naive
# 24h/2h/15m schedule would put two of the three reminders in the middle of the
# night, so reminders that land inside a chat's quiet hours are skipped and the
# 6h slot exists to guarantee at least one lands in waking hours.
REMINDERS = [
    (timedelta(hours=24), "24h"),
    (timedelta(hours=6), "6h"),
    (timedelta(hours=2), "2h"),
    (timedelta(minutes=15), "15m"),
]


async def deadline_reminders(engine: LiveEngine, bot: Bot) -> None:
    bootstrap = await engine.client.bootstrap(near_deadline=True)
    state = parse_game_state(bootstrap)
    if state.next_deadline is None:
        return
    remaining = state.next_deadline - datetime.now(UTC)

    for window, label in REMINDERS:
        # Fire when we're inside the window but not more than one job interval past it.
        if timedelta(0) < remaining <= window and remaining > window - timedelta(minutes=20):
            async with session_scope() as s:
                league_ids = await repo.all_active_leagues(s)
                chats = {c.id: c for lid in league_ids for c in await repo.chats_for_league(s, lid)}
            for chat in chats.values():
                if Notifier.in_quiet_hours(chat.timezone, chat.quiet_from, chat.quiet_to):
                    continue
                key = f"gw{state.next_event}:deadline:{label}"
                async with session_scope() as s:
                    fresh = await repo.claim_alert(s, chat.id, key)
                if fresh:
                    await bot.send_message(
                        chat.id, render_deadline(state.next_deadline, chat.timezone),
                        message_thread_id=repo.topic_of(chat),
                    )
            break


async def price_watch(engine: LiveEngine, bot: Bot) -> None:
    """Post price changes, but only for players somebody in the league owns."""
    bootstrap = await engine.client.bootstrap()
    players = parse_players(bootstrap)
    raw = {e["id"]: e for e in bootstrap["elements"]}
    movers = [
        (players[eid], e["cost_change_event"])
        for eid, e in raw.items()
        if e.get("cost_change_event") and eid in players
    ]
    if not movers:
        return

    async with session_scope() as s:
        league_ids = await repo.all_active_leagues(s)

    event = await engine.resolve_event()
    for league_id in league_ids:
        try:
            table = await engine.build_table(league_id, event)
        except Exception:  # noqa: BLE001
            continue
        owned = {p.element for r in table.rows for p in r.picks}
        relevant = [(p, d) for p, d in movers if p.id in owned]
        if not relevant:
            continue
        lines = [
            f"{'📈' if d > 0 else '📉'} {esc(p.web_name)} £{p.price:.1f}m ({d:+d})"
            for p, d in sorted(relevant, key=lambda t: -t[1])
        ]
        async with session_scope() as s:
            chats = await repo.chats_for_league(s, league_id)
        for chat in chats:
            # Price moves are ambient interest, not news — the quietest two
            # profiles opt out entirely.
            if chat.alert_profile in ("digest-only", "off"):
                continue
            if Notifier.in_quiet_hours(chat.timezone, chat.quiet_from, chat.quiet_to):
                continue
            key = f"prices:{datetime.now(UTC):%Y-%m-%d}:{league_id}"
            async with session_scope() as s:
                fresh = await repo.claim_alert(s, chat.id, key)
            if fresh:
                await bot.send_message(
                    chat.id, "<b>Price changes</b>\n" + "\n".join(lines),
                    message_thread_id=repo.topic_of(chat),
                )


async def nightly_maintenance() -> None:
    async with session_scope() as s:
        await repo.prune_alerts(s)
        await repo.expire_live_messages(s)
    log.info("maintenance.done")


def build_scheduler(engine: LiveEngine, bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(deadline_reminders, "interval", minutes=15, args=[engine, bot],
                  id="deadlines", max_instances=1, coalesce=True)
    # FPL applies price changes around 01:30 UTC.
    sched.add_job(price_watch, "cron", hour=2, minute=0, args=[engine, bot],
                  id="prices", max_instances=1, coalesce=True)
    sched.add_job(nightly_maintenance, "cron", hour=4, minute=30,
                  id="maintenance", max_instances=1, coalesce=True)
    return sched
