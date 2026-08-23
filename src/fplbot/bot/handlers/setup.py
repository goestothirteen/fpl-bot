"""Binding leagues and claiming teams: /link /leagues /unlink /me /whois."""
from __future__ import annotations

import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ...db import repo
from ...db.session import session_scope
from ...fpl.errors import NotFound, UpstreamUnavailable
from ...services.live import LiveEngine
from ..formatting import clip, esc
from ..keyboards import league_picker

router = Router(name="setup")

_ID_RE = re.compile(r"(\d{2,10})")


def _extract_id(text: str | None) -> int | None:
    if not text:
        return None
    m = _ID_RE.search(text)
    return int(m.group(1)) if m else None


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject, engine: LiveEngine) -> None:
    league_id = _extract_id(command.args)
    if league_id is None:
        await message.answer(
            "Send me a league ID: <code>/link 123456</code>\n\n"
            "Find it in the URL when you open the league on the FPL site — "
            "<code>.../leagues/<b>123456</b>/standings/c</code>"
        )
        return

    note = await message.answer("⏳ Looking that league up…")
    try:
        meta, entries = await engine.client.all_classic_entries(league_id)
    except NotFound:
        await note.edit_text(
            f"I can't find league <code>{league_id}</code>. "
            "Double-check the number in the FPL URL."
        )
        return
    except UpstreamUnavailable:
        await note.edit_text("FPL isn't answering right now. Try again in a minute.")
        return

    async with session_scope() as s:
        await repo.upsert_league(s, meta)
        is_default = await repo.link_league(
            s, message.chat.id, league_id, message.from_user.id if message.from_user else None
        )
        await repo.sync_league_members(s, league_id, entries)

    suffix = " and set as this chat's default" if is_default else ""
    await note.edit_text(
        f"✅ Linked <b>{esc(meta.get('name', str(league_id)))}</b> "
        f"({len(entries)} managers){suffix}.\n\nTry /live."
    )


@router.message(Command("leagues"))
async def cmd_leagues(message: Message) -> None:
    async with session_scope() as s:
        rows = await repo.chat_leagues(s, message.chat.id)
    if not rows:
        await message.answer("No leagues linked here yet. Use <code>/link &lt;league_id&gt;</code>.")
        return
    lines = [
        f"{'★' if is_default else '·'} <b>{esc(lg.name)}</b> — <code>{lg.id}</code>"
        for lg, is_default in rows
    ]
    await message.answer(
        "<b>Leagues in this chat</b>\n" + "\n".join(lines),
        reply_markup=league_picker([(lg.id, lg.name) for lg, _ in rows], "setdefault"),
    )


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, command: CommandObject) -> None:
    league_id = _extract_id(command.args)
    if league_id is None:
        await message.answer("Usage: <code>/unlink &lt;league_id&gt;</code>")
        return
    async with session_scope() as s:
        await repo.unlink_league(s, message.chat.id, league_id)
    await message.answer(f"Unlinked league <code>{league_id}</code> from this chat.")


@router.message(Command("me"))
async def cmd_me(message: Message, command: CommandObject, engine: LiveEngine) -> None:
    entry_id = _extract_id(command.args)
    if entry_id is None:
        await message.answer(
            "Tell me which team is yours:\n<code>/me 1234567</code>\n\n"
            "That's the number in <code>fantasy.premierleague.com/entry/<b>1234567</b>/…</code> "
            "when you view your own team. You can paste the whole URL too."
        )
        return
    try:
        entry = await engine.client.entry(entry_id)
    except NotFound:
        await message.answer(f"No FPL team with id <code>{entry_id}</code>.")
        return

    name = f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip()
    async with session_scope() as s:
        await repo.claim_identity(
            s,
            message.from_user.id if message.from_user else 0,
            entry_id,
            message.from_user.username if message.from_user else None,
            message.from_user.full_name if message.from_user else name,
        )
    await message.answer(
        f"Got it — you're <b>{esc(entry.get('name', '?'))}</b> ({esc(name)}). "
        "I'll @-mention you when your players do something."
    )


@router.message(Command("whois"))
async def cmd_whois(message: Message, default_league) -> None:  # noqa: ANN001
    if default_league is None:
        await message.answer("Link a league first: <code>/link &lt;league_id&gt;</code>")
        return
    async with session_scope() as s:
        members = await repo.league_members(s, default_league.id)
        ids = await repo.identities_for_entries(s, [m.entry_id for m in members])

    claimed, unclaimed = [], []
    for m in members:
        ident = ids.get(m.entry_id)
        if ident:
            handle = f"@{ident.username}" if ident.username else esc(ident.display or "?")
            claimed.append(f"· {esc(clip(m.team_name, 18))} — {handle}")
        else:
            unclaimed.append(f"· {esc(clip(m.team_name, 18))}")

    parts = [f"<b>{esc(default_league.name)}</b>"]
    if claimed:
        parts.append("<b>Claimed</b>\n" + "\n".join(claimed))
    if unclaimed:
        parts.append(
            "<b>Unclaimed</b>\n" + "\n".join(unclaimed)
            + "\n\n<i>Run /me &lt;entry_id&gt; to claim yours and get @-mentioned.</i>"
        )
    await message.answer("\n\n".join(parts))
