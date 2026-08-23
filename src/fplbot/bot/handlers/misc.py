"""/start /help /settings and the fallback."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ...db import repo
from ...db.session import session_scope
from ..keyboards import league_picker

router = Router(name="misc")

HELP = """<b>FPL mini-league bot</b>

<b>Setup</b>
/link &lt;league_id&gt; — bind a league to this chat
/leagues — list and switch the default league
/me &lt;entry_id&gt; — claim your team so I can @-mention you
/whois — who's claimed their team

<b>During the gameweek</b>
/live — live table with points, totals and players left
/left — who still has players to play
/diff — league differentials · /diff &lt;name&gt; for head-to-head
/captains — armband spread and live returns
/bench — points rotting on benches
/eo &lt;player&gt; — effective ownership in your league

<b>Any time</b>
/deadline — countdown in your timezone
/chips — who has what left this half of the season
/news — injury flags, filtered to players your league owns
/player &lt;name&gt; — player card with league ownership
/fixtures [TEAM] — next five fixtures
/awards — gameweek awards

<b>Alerts</b>
/settings — all · big-moments · digest-only · off

<i>League standings on the FPL site lag by hours. Everything here is computed
live from match data, so it's current to the minute.</i>"""


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 I track FPL mini-leagues live, so nobody has to open ten team pages "
        "on a Saturday.\n\nStart with <code>/link &lt;league_id&gt;</code> — "
        "the number in your league's FPL URL.\n\n" + HELP
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("settings"))
async def cmd_settings(message: Message, chat_row) -> None:  # noqa: ANN001
    profile = getattr(chat_row, "alert_profile", "big-moments")
    await message.answer(
        f"<b>Alerts here: {profile}</b>\n\n"
        "<b>all</b> — every goal, assist and card for players you own\n"
        "<b>big-moments</b> — goals, red cards, lead changes, GW summary\n"
        "<b>digest-only</b> — one message when the gameweek finishes\n"
        "<b>off</b> — nothing unprompted",
        reply_markup=league_picker(
            [(0, "all"), (1, "big-moments"), (2, "digest-only"), (3, "off")], "profile"
        ),
    )


@router.callback_query(F.data.startswith("profile:"))
async def on_profile(cb: CallbackQuery) -> None:
    mapping = {"0": "all", "1": "big-moments", "2": "digest-only", "3": "off"}
    key = (cb.data or "").split(":")[-1]
    profile = mapping.get(key)
    if profile and cb.message:
        async with session_scope() as s:
            await repo.set_alert_profile(s, cb.message.chat.id, profile)
        await cb.message.edit_text(f"Alerts set to <b>{profile}</b>.")
    await cb.answer()


@router.callback_query(F.data.startswith("setdefault:"))
async def on_set_default(cb: CallbackQuery) -> None:
    league_id = int((cb.data or "0:0").split(":")[-1])
    if cb.message:
        async with session_scope() as s:
            await repo.set_default_league(s, cb.message.chat.id, league_id)
        await cb.answer("Default league updated")
        await cb.message.edit_reply_markup(reply_markup=None)
    else:
        await cb.answer()
