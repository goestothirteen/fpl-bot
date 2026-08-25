"""The gameweek views: /live /left /diff /captains /bench /eo, and the inline
button router that switches between them by editing one message."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ...db import repo
from ...db.session import session_scope
from ...fpl.errors import UpstreamUnavailable
from ...logging_conf import get_logger
from ...services import analysis
from ...services.live import LiveEngine
from ...services.models import GamePhase, LiveTable
from ...services.parsing import parse_live, parse_players
from ..formatting import (
    esc,
    render_bench,
    render_captains,
    render_differentials,
    render_edge,
    render_live_cards,
    render_remaining_detail,
    render_season_table,
    render_wager_table,
)
from ..keyboards import live_views

router = Router(name="live")
log = get_logger(__name__)

VIEWS = ("live", "season", "left", "edge", "caps", "diff", "bench", "wager")


async def _context(engine: LiveEngine, league_id: int, event: int):
    table = await engine.build_table(league_id, event)
    bootstrap = await engine.client.bootstrap()
    players = parse_players(bootstrap)
    live = parse_live(await engine.client.live(event))
    return table, players, live


async def _render(view: str, engine: LiveEngine, table: LiveTable, players, live) -> str:  # noqa: ANN001
    if view == "wager":
        return await _wager_view(engine, table)
    if view == "season":
        return render_season_table(table)
    if view == "left":
        return render_remaining_detail(table, players)
    if view == "edge":
        return render_edge(table, players)
    if view == "caps":
        return render_captains(analysis.captain_spread(table, live), players)
    if view == "diff":
        return render_differentials(analysis.differentials(table, live), players)
    if view == "bench":
        return render_bench(analysis.bench_disasters(table))
    return render_live_cards(table, players, live)


async def _wager_view(engine: LiveEngine, table: LiveTable) -> str:
    """Balances for the button on the /live keyboard."""
    from ...services import ledger as ledger_svc
    from ...services.wager import scheme_for

    if scheme_for(table.league_id) is None:
        return (
            f"💰 <b>{esc(table.league_name)}</b>\n\n"
            "No wager is configured for this league. The stakes live in code "
            "so they can't be edited from a chat."
        )
    led = await ledger_svc.build(engine, table.league_id, table.league_name)
    if led is None:
        return "Couldn't build the ledger for this league."
    return render_wager_table(await ledger_svc.with_season(engine, led, total_events=38))


async def _resolve_league(message: Message, default_league, command_arg: str | None):  # noqa: ANN001
    if command_arg and command_arg.strip().isdigit():
        return int(command_arg.strip())
    if default_league is not None:
        return default_league.id
    await message.answer(
        "No league linked to this chat yet.\n<code>/link &lt;league_id&gt;</code> to add one."
    )
    return None


async def _send_view(
    message: Message, engine: LiveEngine, league_id: int, view: str
) -> None:
    note = await message.answer("⏳ Building the table…")
    try:
        event = await engine.resolve_event()
        table, players, live = await _context(engine, league_id, event)
    except UpstreamUnavailable:
        await note.edit_text("FPL isn't answering right now — try again shortly.")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("live.view_failed", league=league_id, view=view)
        await note.edit_text(f"Something broke building that view: {esc(str(exc))}")
        return

    await note.edit_text(
        await _render(view, engine, table, players, live),
        reply_markup=live_views(league_id, event, view),
    )
    if table.phase is GamePhase.LIVE:
        async with session_scope() as s:
            await repo.register_live_message(
                s, message.chat.id, note.message_id, league_id, event, view
            )


@router.message(Command("live", "table"))
async def cmd_live(message: Message, command: CommandObject, engine: LiveEngine,
                   default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, command.args)
    if league_id:
        await _send_view(message, engine, league_id, "live")


@router.message(Command("season", "total"))
async def cmd_season(message: Message, command: CommandObject, engine: LiveEngine,
                     default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, command.args)
    if league_id:
        await _send_view(message, engine, league_id, "season")


@router.message(Command("edge", "swing"))
async def cmd_edge(message: Message, command: CommandObject, engine: LiveEngine,
                   default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, command.args)
    if league_id:
        await _send_view(message, engine, league_id, "edge")


@router.message(Command("left"))
async def cmd_left(message: Message, command: CommandObject, engine: LiveEngine,
                   default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, command.args)
    if league_id:
        await _send_view(message, engine, league_id, "left")


@router.message(Command("captains"))
async def cmd_captains(message: Message, command: CommandObject, engine: LiveEngine,
                       default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, command.args)
    if league_id:
        await _send_view(message, engine, league_id, "caps")


@router.message(Command("bench"))
async def cmd_bench(message: Message, command: CommandObject, engine: LiveEngine,
                    default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, command.args)
    if league_id:
        await _send_view(message, engine, league_id, "bench")


@router.message(Command("diff"))
async def cmd_diff(message: Message, command: CommandObject, engine: LiveEngine,
                   default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, None)
    if league_id is None:
        return
    # `/diff <team name>` switches to head-to-head mode against that manager.
    target = (command.args or "").strip()
    if not target:
        await _send_view(message, engine, league_id, "diff")
        return

    note = await message.answer("⏳ Comparing…")
    event = await engine.resolve_event()
    table, players, live = await _context(engine, league_id, event)

    async with session_scope() as s:
        ids = await repo.identities_for_entries(s, [r.entry_id for r in table.rows])
    me_entry = next(
        (eid for eid, i in ids.items()
         if message.from_user and i.tg_user_id == message.from_user.id), None
    )
    mine = next((r for r in table.rows if r.entry_id == me_entry), None)
    theirs = next(
        (r for r in table.rows
         if target.lower() in r.team_name.lower() or target.lower() in r.manager_name.lower()),
        None,
    )
    if mine is None:
        await note.edit_text("Claim your team first with <code>/me &lt;entry_id&gt;</code>.")
        return
    if theirs is None:
        await note.edit_text(f"No manager in this league matching “{esc(target)}”.")
        return

    only_a, only_b, swing = analysis.head_to_head(mine, theirs, live)

    def fmt(items):  # noqa: ANN001, ANN202
        return "\n".join(
            f"   <b>{d:+d}</b> · {esc(players[el].web_name)}"
            for el, d in items[:8] if el in players
        ) or "   —"

    await note.edit_text(
        f"⚔️ <b>{esc(mine.team_name)}</b> vs <b>{esc(theirs.team_name)}</b> · GW{event}\n"
        f"Swing so far: <b>{swing:+d}</b>\n\n"
        f"<b>Only yours</b>\n{fmt(only_a)}\n\n"
        f"<b>Only theirs</b>\n{fmt(only_b)}"
    )


@router.message(Command("eo"))
async def cmd_eo(message: Message, command: CommandObject, engine: LiveEngine,
                 default_league) -> None:  # noqa: ANN001
    league_id = await _resolve_league(message, default_league, None)
    if league_id is None:
        return
    event = await engine.resolve_event()
    table, players, live = await _context(engine, league_id, event)
    own = analysis.ownership(table)

    query = (command.args or "").strip().lower()
    rows = sorted(own.values(), key=lambda o: -o.effective_ownership)
    if query:
        rows = [
            o for o in rows
            if o.element in players and query in players[o.element].web_name.lower()
        ]
    blocks = [
        f"<b>{esc(players[o.element].web_name)}</b> — <b>{o.effective_ownership:.0f}%</b>\n"
        f"     {live[o.element].effective_points if o.element in live else 0} pts"
        for o in rows[:15] if o.element in players
    ]
    await message.answer(
        "📊 <b>Effective ownership</b> · ownership + captaincy\n\n"
        + ("\n\n".join(blocks) or "—")
    )


@router.callback_query(F.data.startswith("v:"))
async def on_view_button(cb: CallbackQuery, engine: LiveEngine) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 4:
        await cb.answer()
        return
    _, view, league_id_s, event_s = parts[:4]
    if view not in VIEWS:
        await cb.answer()
        return

    league_id, event = int(league_id_s), int(event_s)
    await cb.answer("Refreshing…" if len(parts) > 4 else None)
    try:
        table, players, live = await _context(engine, league_id, event)
        text = await _render(view, engine, table, players, live)
    except UpstreamUnavailable:
        await cb.answer("FPL isn't answering — try again shortly.", show_alert=True)
        return

    try:
        if cb.message:
            await cb.message.edit_text(
                text, reply_markup=live_views(league_id, event, view)
            )
    except TelegramBadRequest as exc:
        # "message is not modified" is normal when nothing has changed.
        if "not modified" not in str(exc):
            raise
