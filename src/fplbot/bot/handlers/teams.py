"""The "⚽ Teams" view: a per-manager pitch visualisation on its own photo
message.

A Telegram message is either text or a photo — you can't edit one into the
other. The GW/Season/Edge/... views all live on a single text message that gets
edited in place. The pitch is an image, so it gets its OWN photo message,
spawned when the user taps "⚽ Teams" on the text keyboard. From then on,
switching managers or flipping between the formation and the GW-score view edits
THAT photo in place (editMessageMedia) — it never spams a new photo per tap.

Callback namespaces (kept separate from the text router's `v:` prefix):
  teams:open:<league>:<event>              — tapped on the text keyboard; spawn photo
  tm:<league>:<event>:<entry>:<mode>       — set a specific manager/mode (unused direct, reserved)
  tmnav:<league>:<event>:<entry>:<mode>:<prev|next>  — cycle managers
  tmmode:<league>:<event>:<entry>:<mode>   — toggle pitch<->score
  tmclose                                  — delete the photo message
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
from ...services.parsing import parse_live, parse_players
from ..pitch import render_pitch

router = Router(name="teams")
log = get_logger(__name__)

MODES = ("pitch", "score")


def _kb(league_id: int, event: int, entry_id: int, mode: str):  # noqa: ANN202
    """Switcher under the photo: manager nav, mode toggle, close."""
    b = InlineKeyboardBuilder()
    b.button(text="◀ Prev", callback_data=f"tmnav:{league_id}:{event}:{entry_id}:{mode}:prev")
    b.button(text="Next ▶", callback_data=f"tmnav:{league_id}:{event}:{entry_id}:{mode}:next")
    other = "score" if mode == "pitch" else "pitch"
    label = "📊 GW score" if other == "score" else "🅿️ Pitch"
    b.button(text=label, callback_data=f"tmmode:{league_id}:{event}:{entry_id}:{other}")
    b.button(text="✖ Close", callback_data="tmclose")
    b.adjust(2, 1, 1)
    return b.as_markup()


async def _table(engine: LiveEngine, league_id: int, event: int):  # noqa: ANN202
    table = await engine.build_table(league_id, event)
    bootstrap = await engine.client.bootstrap()
    players = parse_players(bootstrap)
    live = parse_live(await engine.client.live(event))
    return table, players, live


def _pick_entry(table: LiveTable, entry_id: int | None, step: int) -> int | None:
    """Resolve which manager to show. `step` of -1/+1 cycles; 0 keeps/ defaults."""
    rows = table.ranked_gw()
    if not rows:
        return None
    ids = [r.entry_id for r in rows]
    if entry_id is None or entry_id not in ids:
        return ids[0]
    idx = (ids.index(entry_id) + step) % len(ids)
    return ids[idx]


def _photo(table: LiveTable, players, live, entry_id: int, mode: str):  # noqa: ANN202
    row = next((r for r in table.rows if r.entry_id == entry_id), None)
    if row is None:
        return None
    png = render_pitch(
        row, players, live, mode=mode, event=table.event, league_name=table.league_name
    )
    return BufferedInputFile(png, filename=f"pitch_{entry_id}_{mode}.png")


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
        table, players, live = await _table(engine, league_id, event)
    except UpstreamUnavailable:
        await cb.answer("FPL isn't answering — try again shortly.", show_alert=True)
        return

    entry_id = _pick_entry(table, None, 0)
    if entry_id is None:
        await cb.answer("No managers in this league yet.", show_alert=True)
        return
    photo = _photo(table, players, live, entry_id, "pitch")
    if photo is None:
        await cb.answer("Couldn't build that team.", show_alert=True)
        return
    await cb.message.answer_photo(
        photo, reply_markup=_kb(league_id, event, entry_id, "pitch")
    )


async def _edit(cb: CallbackQuery, engine: LiveEngine, league_id: int,
                entry_id: int | None, mode: str, step: int) -> None:
    if not cb.message:
        await cb.answer()
        return
    await cb.answer()
    try:
        # Re-resolve the event every tap: a stale event baked into the button
        # would pin the wrong gameweek for ever (same reason as the text router).
        event = await engine.resolve_event()
        table, players, live = await _table(engine, league_id, event)
    except UpstreamUnavailable:
        await cb.answer("FPL isn't answering — try again shortly.", show_alert=True)
        return

    target = _pick_entry(table, entry_id, step)
    if target is None:
        await cb.answer("No managers to show.", show_alert=True)
        return
    photo = _photo(table, players, live, target, mode)
    if photo is None:
        await cb.answer("Couldn't build that team.", show_alert=True)
        return
    try:
        await cb.message.edit_media(
            InputMediaPhoto(media=photo),
            reply_markup=_kb(league_id, event, target, mode),
        )
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


@router.callback_query(F.data.startswith("tmnav:"))
async def on_nav(cb: CallbackQuery, engine: LiveEngine) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 6:
        await cb.answer()
        return
    _, league_s, _event, entry_s, mode, direction = parts[:6]
    step = -1 if direction == "prev" else 1
    mode = mode if mode in MODES else "pitch"
    await _edit(cb, engine, int(league_s), int(entry_s), mode, step)


@router.callback_query(F.data.startswith("tmmode:"))
async def on_mode(cb: CallbackQuery, engine: LiveEngine) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 5:
        await cb.answer()
        return
    _, league_s, _event, entry_s, mode = parts[:5]
    mode = mode if mode in MODES else "pitch"
    await _edit(cb, engine, int(league_s), int(entry_s), mode, 0)


@router.callback_query(F.data == "tmclose")
async def on_close(cb: CallbackQuery) -> None:
    if cb.message:
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
    await cb.answer()
