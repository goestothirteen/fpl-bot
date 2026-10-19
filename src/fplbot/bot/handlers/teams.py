"""The "⚽ Teams" view: a per-manager pitch visualisation on its own photo
message, with the league's managers listed as tappable buttons underneath.

A Telegram message is either text or a photo — you can't edit one into the
other. The GW/Season/Edge/... views all live on a single text message that gets
edited in place. The pitch is an image, so it gets its OWN photo message,
spawned when the user taps "⚽ Teams" on the text keyboard. The keyboard under
the photo is a persistent LIST of the league's managers: tapping any name swaps
the pitch to that manager (editing the photo in place via editMessageMedia), and
the full list stays visible the whole time — no paging, no prev/next.

Callback namespaces (kept separate from the text router's `v:` prefix):
  teams:open:<league>:<event>        — tapped on the text keyboard; spawn photo + list
  tm:<league>:<entry>                — show this manager's pitch (event re-resolved)
  tmclose                            — delete the photo message
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...fpl.errors import UpstreamUnavailable
from ...logging_conf import get_logger
from ...services.live import LiveEngine
from ...services.models import LiveTable
from ...services.parsing import parse_live, parse_players, parse_teams
from ..pitch import render_pitch, shirt_keys
from ..shirts import fetch_shirts

router = Router(name="teams")
log = get_logger(__name__)

# Keep the keyboard inside Telegram's limits even for a big public league.
MAX_LISTED = 30


def _kb(league_id: int, table: LiveTable, active_entry: int):  # noqa: ANN202
    """A row per manager (tappable to swap the pitch) + a close button.

    The active manager is marked and placed first so the list reads as "showing
    X, tap another to switch". Two managers per row to keep it compact.
    """
    b = InlineKeyboardBuilder()
    rows = table.ranked_gw()[:MAX_LISTED]
    for r in rows:
        mark = "• " if r.entry_id == active_entry else ""
        label = f"{mark}{r.team_name}"[:30]
        b.button(text=label, callback_data=f"tm:{league_id}:{r.entry_id}")
    b.button(text="✖ Close", callback_data="tmclose")
    # Two managers per row; the close button on its own final row.
    b.adjust(*([2] * ((len(rows) + 1) // 2)), 1)
    return b.as_markup()


async def _table(engine: LiveEngine, league_id: int, event: int):  # noqa: ANN202
    table = await engine.build_table(league_id, event)
    bootstrap = await engine.client.bootstrap()
    players = parse_players(bootstrap)
    teams = parse_teams(bootstrap)
    live = parse_live(await engine.client.live(event))
    return table, players, teams, live


async def _photo(table: LiveTable, players, teams, live, entry_id: int):  # noqa: ANN202
    row = next((r for r in table.rows if r.entry_id == entry_id), None)
    if row is None:
        return None
    # The renderer keys shirts by (team id, is_gk); FPL serves them by club
    # code. Translate, fetch (cached after the first view), translate back.
    # Anything that couldn't be fetched is simply left out: the renderer draws
    # a plain shirt for it rather than failing the whole picture.
    wanted = {(teams[t].code, gk): (t, gk) for t, gk in shirt_keys(row, players) if t in teams}
    fetched = await fetch_shirts(set(wanted))
    shirts = {wanted[k]: png for k, png in fetched.items()}
    png = render_pitch(
        row, players, live, event=table.event, league_name=table.league_name, shirts=shirts
    )
    return BufferedInputFile(png, filename=f"pitch_{entry_id}.png")


@router.callback_query(F.data.startswith("teams:open:"))
async def on_open(cb: CallbackQuery, engine: LiveEngine) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 4 or not cb.message:
        await cb.answer()
        return
    league_id = int(parts[2])
    await cb.answer("Drawing the pitch…")
    try:
        event = await engine.resolve_event()
        table, players, teams, live = await _table(engine, league_id, event)
    except UpstreamUnavailable:
        await cb.answer("FPL isn't answering — try again shortly.", show_alert=True)
        return

    rows = table.ranked_gw()
    if not rows:
        await cb.answer("No managers in this league yet.", show_alert=True)
        return
    entry_id = rows[0].entry_id
    photo = await _photo(table, players, teams, live, entry_id)
    if photo is None:
        await cb.answer("Couldn't build that team.", show_alert=True)
        return
    await cb.message.answer_photo(photo, reply_markup=_kb(league_id, table, entry_id))


@router.callback_query(F.data.startswith("tm:"))
async def on_pick(cb: CallbackQuery, engine: LiveEngine) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 3 or not cb.message:
        await cb.answer()
        return
    league_id, entry_id = int(parts[1]), int(parts[2])
    await cb.answer()
    try:
        # Re-resolve the event every tap: a stale event baked into the button
        # would pin the wrong gameweek for ever (same reason as the text router).
        event = await engine.resolve_event()
        table, players, teams, live = await _table(engine, league_id, event)
    except UpstreamUnavailable:
        await cb.answer("FPL isn't answering — try again shortly.", show_alert=True)
        return

    photo = await _photo(table, players, teams, live, entry_id)
    if photo is None:
        await cb.answer("Couldn't build that team.", show_alert=True)
        return
    try:
        await cb.message.edit_media(
            InputMediaPhoto(media=photo),
            reply_markup=_kb(league_id, table, entry_id),
        )
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


@router.callback_query(F.data == "tmclose")
async def on_close(cb: CallbackQuery) -> None:
    if cb.message:
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
    await cb.answer()
