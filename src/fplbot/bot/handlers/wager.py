"""The league side-bet: /wager, /wager grid, /wager <gw>, /wager all, /settle."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ...db import repo
from ...db.session import session_scope
from ...fpl.errors import UpstreamUnavailable
from ...logging_conf import get_logger
from ...services import ledger as ledger_svc
from ...services import wager
from ...services.live import LiveEngine
from ..formatting import (
    esc,
    render_settlement,
    render_wager_table,
    render_wager_week,
)
from ..wager_grid import render_wager_grid

router = Router(name="wager")
log = get_logger(__name__)

SEASON_EVENTS = 38


async def _ledger(message: Message, engine: LiveEngine, default_league):  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first with <code>/link &lt;league_id&gt;</code>.")
        return None
    if wager.scheme_for(default_league.id) is None:
        await message.answer(
            f"No wager is configured for <b>{esc(default_league.name)}</b>.\n"
            "The stakes live in code so they can't be edited from a chat — "
            "ask for them to be added."
        )
        return None
    built = await ledger_svc.build(engine, default_league.id, default_league.name)
    if built is None:
        await message.answer("Couldn't build the ledger for this league.")
        return None
    return await ledger_svc.with_season(engine, built, total_events=SEASON_EVENTS)


@router.message(Command("wager", "money", "bet"))
async def cmd_wager(message: Message, command: CommandObject, engine: LiveEngine,
                    default_league) -> None:  # noqa: ANN001
    arg = (command.args or "").strip().lower()
    note = await message.answer("💰 Totting up…")
    try:
        led = await _ledger(message, engine, default_league)
    except UpstreamUnavailable:
        await note.edit_text("FPL isn't answering right now — try again shortly.")
        return
    if led is None:
        await note.delete()
        return

    if arg in {"grid", "table", "weeks", "history"}:
        # The whole season on one picture: a row per gameweek, a column per
        # manager, totals underneath. Telegram can't turn a text message into a
        # photo, so the placeholder goes and the grid arrives as its own message.
        if not led.events:
            await note.edit_text(render_wager_table(led))
            return
        span = (f"GW{led.events[0]}" if len(led.events) == 1
                else f"GW{led.events[0]}–{led.events[-1]}")
        caption = f"💰 <b>{esc(led.league_name)}</b> · wager, week by week · {span}"
        if led.provisional:
            weeks = ", ".join(f"GW{e}" for e in sorted(led.provisional))
            caption += f"\n<i>⏳ {weeks} provisional — can still change.</i>"
        png = render_wager_grid(led)
        await note.delete()
        await message.answer_photo(
            BufferedInputFile(png, filename=f"wager_{led.league_id}.png"), caption=caption,
        )
        return

    if arg in {"all", "full", "verbose"}:
        if not led.events:
            await note.edit_text(render_wager_table(led))
            return
        # Verbose mode is one message per gameweek: every manager's weekly
        # amount and running total, so the whole season is auditable.
        await note.edit_text(render_wager_table(led))
        for event in led.events:
            await message.answer(render_wager_week(led, event))
        return

    if arg.replace("gw", "").isdigit():
        event = int(arg.replace("gw", ""))
        await note.edit_text(render_wager_week(led, event))
        return

    await note.edit_text(render_wager_table(led))


@router.message(Command("settle"))
async def cmd_settle(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    note = await message.answer("🧾 Working out who owes whom…")
    try:
        led = await _ledger(message, engine, default_league)
    except UpstreamUnavailable:
        await note.edit_text("FPL isn't answering right now — try again shortly.")
        return
    if led is None:
        await note.delete()
        return

    async with session_scope() as s:
        frozen = await repo.get_settlement(s, led.league_id)

    if frozen is not None:
        # Once money has moved the numbers must stop moving, even if FPL
        # later corrects a gameweek.
        payments = [(int(a), int(b), int(c)) for a, b, c in frozen.payments]
        await note.edit_text(
            render_settlement(led, payments)
            + f"\n<i>Settled at GW{frozen.season_end_event} — frozen, "
            "later FPL corrections won't move it.</i>"
        )
        return

    balances = led.final_balances
    payments = wager.transfers(balances)

    if not led.season_amounts:
        # No season pot yet, so nothing is frozen and no money moves. Either the
        # season is still running, or every gameweek is in but one of them is
        # still provisional — `ledger.with_season` withholds the pot until FPL
        # has confirmed all 38.
        settled = len(led.events)
        if led.has_provisional:
            weeks = ", ".join(f"GW{e}" for e in sorted(led.provisional))
            why = (
                f"\n<i>Provisional — {weeks} still awaiting FPL confirmation. "
                "Nothing is frozen until every gameweek is confirmed.</i>"
            )
        else:
            why = (
                f"\n<i>Provisional — {settled} of {SEASON_EVENTS} gameweeks settled. "
                "The season adjustment applies once GW38 is final.</i>"
            )
        await note.edit_text(render_settlement(led, payments) + why)
        return

    async with session_scope() as s:
        await repo.record_settlement(
            s, led.league_id, SEASON_EVENTS, balances,
            [[a, b, c] for a, b, c in payments],
            message.from_user.id if message.from_user else None,
        )
    await note.edit_text(
        render_settlement(led, payments)
        + "\n<i>Season complete — these numbers are now frozen.</i>"
    )
