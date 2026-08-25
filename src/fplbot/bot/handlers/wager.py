"""The league side-bet: /wager, /wager <gw>, /wager all, /settle."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

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
        settled = len(led.events)
        await note.edit_text(
            render_settlement(led, payments)
            + f"\n<i>Provisional — {settled} of {SEASON_EVENTS} gameweeks settled. "
            "The season adjustment applies once GW38 is final.</i>"
        )
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
