"""Between-gameweek information: /deadline /player /fixtures /news /chips /prices."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ...db import repo
from ...db.session import session_scope
from ...services import analysis
from ...services.live import LiveEngine
from ...services.parsing import parse_game_state, parse_players, parse_teams
from ..formatting import clip, esc, player_line, render_deadline

router = Router(name="info")


@router.message(Command("deadline"))
async def cmd_deadline(message: Message, engine: LiveEngine, chat_row) -> None:  # noqa: ANN001
    bootstrap = await engine.client.bootstrap(near_deadline=True)
    state = parse_game_state(bootstrap)
    if state.next_deadline is None:
        await message.answer("No upcoming deadline — the season is over.")
        return
    tz = getattr(chat_row, "timezone", "Asia/Singapore")
    await message.answer(render_deadline(state.next_deadline, tz))


@router.message(Command("player"))
async def cmd_player(message: Message, command: CommandObject, engine: LiveEngine,
                     default_league) -> None:  # noqa: ANN001
    query = (command.args or "").strip().lower()
    if not query:
        await message.answer("Usage: <code>/player haaland</code>")
        return

    bootstrap = await engine.client.bootstrap()
    players = parse_players(bootstrap)
    teams = parse_teams(bootstrap)
    raw = {e["id"]: e for e in bootstrap["elements"]}

    matches = [p for p in players.values() if query in p.web_name.lower()]
    if not matches:
        await message.answer(f"No player matching “{esc(query)}”.")
        return
    p = max(matches, key=lambda x: x.total_points)
    e = raw[p.id]

    owners: list[str] = []
    if default_league is not None:
        try:
            event = await engine.resolve_event()
            table = await engine.build_table(default_league.id, event)
            own = analysis.ownership(table, starters_only=False)
            owners = own[p.id].owners if p.id in own else []
        except Exception:  # noqa: BLE001 - ownership is a nice-to-have here
            owners = []

    lines = [
        player_line(p),
        f"{teams[p.team].short_name} · {['', 'GK', 'DEF', 'MID', 'FWD'][p.element_type]}",
        f"Form {p.form} · {p.total_points} pts · PPG {e.get('points_per_game', '—')}",
        f"xG90 {e.get('expected_goals_per_90', '—')} · xA90 {e.get('expected_assists_per_90', '—')}",
        f"Def. contribution {e.get('defensive_contribution', 0)} · BPS {e.get('bps', 0)}",
    ]
    if e.get("price_change_projections") is not None:
        lines.append(f"Price projection: {e['price_change_projections']}")
    if p.news:
        lines.append(f"⚠️ {esc(p.news)}")
    if owners:
        lines.append(f"Owned in your league by: {esc(', '.join(clip(o, 12) for o in owners))}")
    await message.answer("\n".join(lines))


@router.message(Command("fixtures"))
async def cmd_fixtures(message: Message, command: CommandObject, engine: LiveEngine) -> None:
    bootstrap = await engine.client.bootstrap()
    teams = parse_teams(bootstrap)
    state = parse_game_state(bootstrap)
    target = (command.args or "").strip().upper()

    start = state.next_event or state.current_event or 1
    out: list[str] = []
    for gw in range(start, min(start + 5, 39)):
        raw = await engine.client.fixtures(gw)
        for f in raw:
            h, a = teams[f["team_h"]].short_name, teams[f["team_a"]].short_name
            if target and target not in (h, a):
                continue
            diff = f.get("team_h_difficulty", 0) if target == h else f.get("team_a_difficulty", 0)
            marker = "🟢🟩⬜🟧🟥"[min(max(diff - 1, 0), 4)] if target else ""
            out.append(f"GW{gw:<2} {h}–{a} {marker}")
    if not out:
        await message.answer("No fixtures found for that team.")
        return
    await message.answer(
        f"<b>Next fixtures{' · ' + esc(target) if target else ''}</b>\n"
        f"<pre>{esc(chr(10).join(out[:40]))}</pre>"
    )


@router.message(Command("news"))
async def cmd_news(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first: <code>/link &lt;league_id&gt;</code>")
        return
    event = await engine.resolve_event()
    table = await engine.build_table(default_league.id, event)
    bootstrap = await engine.client.bootstrap()
    players = parse_players(bootstrap)
    own = analysis.ownership(table, starters_only=False)

    flagged = [
        (players[el], o) for el, o in own.items()
        if el in players and players[el].flagged and players[el].news
    ]
    if not flagged:
        await message.answer("No injury or availability flags on anyone your league owns. 🎉")
        return
    flagged.sort(key=lambda t: -t[1].count)
    lines = [
        f"{player_line(p)}\n  <i>{esc(p.news)}</i>\n  owned by {o.count}: "
        f"{esc(', '.join(clip(x, 12) for x in o.owners[:4]))}"
        for p, o in flagged[:12]
    ]
    await message.answer("<b>Availability watch</b>\n\n" + "\n\n".join(lines))


@router.message(Command("chips"))
async def cmd_chips(message: Message, engine: LiveEngine, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first.")
        return
    bootstrap = await engine.client.bootstrap()
    chips_meta = bootstrap.get("chips", [])
    event = await engine.resolve_event()

    async with session_scope() as s:
        members = await repo.league_members(s, default_league.id)

    lines = []
    for m in members[:30]:
        history = await engine.client.entry_history(m.entry_id)
        avail = analysis.chip_availability(chips_meta, history.get("chips", []), event)
        left = "".join(
            {"wildcard": "W", "freehit": "F", "bboost": "B", "3xc": "T"}.get(k, "?")
            for k, v in sorted(avail.items()) if v
        ) or "—"
        lines.append(f"{clip(m.team_name, 16):<16} {left}")

    half = "GW20–38" if event >= 20 else "GW1–19"
    await message.answer(
        f"<b>Chips left</b> · {half} half\n"
        f"<pre>{esc(chr(10).join(lines))}</pre>\n"
        "<i>W wildcard · F free hit · B bench boost · T triple captain</i>\n"
        "<i>Chips reset at GW20 this season, so this is the current half only.</i>"
    )
