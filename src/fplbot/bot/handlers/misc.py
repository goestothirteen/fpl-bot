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
/topic — run it inside a forum topic to send my alerts only there

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


@router.message(F.migrate_to_chat_id)
async def on_group_upgraded(message: Message) -> None:
    """Carry the chat's leagues and settings across when Telegram upgrades a
    group to a supergroup and hands it a brand new chat id."""
    new_id = message.migrate_to_chat_id
    if new_id is None:
        return
    async with session_scope() as s:
        carried = await repo.migrate_chat(s, message.chat.id, new_id)
    if carried:
        await message.bot.send_message(
            new_id,
            f"This group was upgraded to a supergroup, so I moved your "
            f"{carried} linked league{'s' if carried != 1 else ''} and settings "
            "across. Nothing to redo.",
        )


@router.message(Command("topic"))
async def cmd_topic(message: Message, chat_row) -> None:  # noqa: ANN001
    """Pin unprompted messages to one forum topic.

    Commands always answer wherever they were typed — Telegram threads a reply
    to its own message automatically. This only governs the messages the bot
    sends on its own: goal alerts, the gameweek wrap-up, the wager settlement,
    deadline reminders and price changes.
    """
    arg = (message.text or "").split(maxsplit=1)
    arg = arg[1].strip().lower() if len(arg) > 1 else ""
    current = repo.topic_of(chat_row)

    if arg in {"off", "clear", "none"}:
        async with session_scope() as s:
            await repo.set_topic(s, message.chat.id, None)
        await message.answer("Alerts will go to the whole group again, not one topic.")
        return

    thread_id = message.message_thread_id if message.is_topic_message else None
    if thread_id is None:
        if current is not None:
            await message.answer(
                f"Alerts currently go to topic <code>{current}</code>.\n"
                "Run <code>/topic</code> inside the topic you want them in to move them, "
                "or <code>/topic off</code> to send them to the whole group."
            )
            return
        await message.answer(
            "Run this <b>inside the topic</b> you want my alerts in, and I'll send "
            "them all there.\n\nThis is the General topic (or not a forum group), so "
            "there's nothing to pin to."
        )
        return

    async with session_scope() as s:
        await repo.set_topic(s, message.chat.id, thread_id)
    await message.answer(
        "📌 Got it — goal alerts, gameweek wrap-ups, wager settlements, deadline "
        "reminders and price changes will all come to this topic only.\n\n"
        "Commands still answer wherever you type them. "
        "<code>/topic off</code> undoes this."
    )


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
