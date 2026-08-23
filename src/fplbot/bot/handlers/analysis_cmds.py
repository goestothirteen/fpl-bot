"""/awards /form /transfers /template /rank."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ...db import repo
from ...db.session import session_scope
from ...services import analysis
from ...services.live import LiveEngine
from ...services.parsing import parse_live, parse_players
from ..formatting import clip, esc, render_awards

router = Router(name="analysis")


@router.message(Command("awards"))
async def cmd_awards(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first.")
        return
    event = await engine.resolve_event()
    table = await engine.build_table(default_league.id, event)
    players = parse_players(await engine.client.bootstrap())
    live = parse_live(await engine.client.live(event))
    await message.answer(render_awards(analysis.awards(table, players, live)))


@router.message(Command("template"))
async def cmd_template(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first.")
        return
    event = await engine.resolve_event()
    table = await engine.build_table(default_league.id, event)
    players = parse_players(await engine.client.bootstrap())
    core, overlap = analysis.template(table)

    names = ", ".join(players[e].web_name for e in core if e in players) or "nothing — you're all different"
    brave = "\n".join(f"{clip(n, 16):<16} {c:>2}/{len(core)}" for n, c in overlap[:12])
    await message.answer(
        f"<b>League template</b> — owned by &gt;50%\n{esc(names)}\n\n"
        f"<b>Bravest first</b>\n<pre>{esc(brave)}</pre>"
    )


@router.message(Command("transfers"))
async def cmd_transfers(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first.")
        return
    event = await engine.resolve_event()
    players = parse_players(await engine.client.bootstrap())
    async with session_scope() as s:
        members = await repo.league_members(s, default_league.id)

    blocks: list[str] = []
    for m in members[:30]:
        moves = [t for t in await engine.client.entry_transfers(m.entry_id) if t["event"] == event]
        if not moves:
            continue
        lines = [
            f"  {players[t['element_out']].web_name} → {players[t['element_in']].web_name}"
            for t in moves
            if t["element_in"] in players and t["element_out"] in players
        ]
        blocks.append(f"<b>{esc(clip(m.team_name, 20))}</b>\n" + "\n".join(lines))

    if not blocks:
        await message.answer(f"No transfers made in GW{event} yet.")
        return
    await message.answer(f"<b>Transfers · GW{event}</b>\n\n" + "\n\n".join(blocks))


@router.message(Command("form"))
async def cmd_form(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first.")
        return
    event = await engine.resolve_event()
    async with session_scope() as s:
        members = await repo.league_members(s, default_league.id)

    rows = []
    for m in members[:30]:
        history = await engine.client.entry_history(m.entry_id)
        recent = [h for h in history.get("current", []) if h["event"] > max(0, event - 4)]
        pts = sum(h["points"] - h.get("event_transfers_cost", 0) for h in recent)
        rows.append((m.team_name, pts, len(recent)))
    rows.sort(key=lambda t: -t[1])

    lines = [f"{clip(n, 16):<16} {p:>4}  ({g} GW)" for n, p, g in rows]
    await message.answer(
        f"<b>Form</b> — last {min(4, event)} gameweeks, net of hits\n"
        f"<pre>{esc(chr(10).join(lines))}</pre>"
    )
