"""Thin data-access helpers. Handlers never write SQL."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AlertLog,
    Chat,
    ChatLeague,
    GWResult,
    Identity,
    League,
    LeagueManager,
    LiveMessage,
    Manager,
    PicksCache,
    WagerSettlement,
)


# ── chats ──────────────────────────────────────────────────────────────────
async def upsert_chat(s: AsyncSession, chat_id: int, chat_type: str, title: str | None,
                      tz: str) -> Chat:
    stmt = (
        insert(Chat)
        .values(id=chat_id, type=chat_type, title=title, timezone=tz)
        .on_conflict_do_update(index_elements=[Chat.id], set_={"type": chat_type, "title": title})
        .returning(Chat)
    )
    return (await s.execute(stmt)).scalar_one()


async def get_chat(s: AsyncSession, chat_id: int) -> Chat | None:
    return await s.get(Chat, chat_id)


async def migrate_chat(s: AsyncSession, old_id: int, new_id: int) -> int:
    """Telegram rewrites a group's chat id when it becomes a supergroup, which
    happens the first time you add enough members or make it public. Without
    this, every linked league silently disappears at that moment and the bot
    looks broken. Returns the number of leagues carried over.
    """
    old = await s.get(Chat, old_id)
    if old is None:
        return 0

    await s.execute(
        insert(Chat)
        .values(
            id=new_id, type="supergroup", title=old.title, timezone=old.timezone,
            alert_profile=old.alert_profile, quiet_from=old.quiet_from,
            quiet_to=old.quiet_to, settings=old.settings,
        )
        .on_conflict_do_nothing()
    )

    links = (
        await s.execute(select(ChatLeague).where(ChatLeague.chat_id == old_id))
    ).scalars().all()
    for link in links:
        await s.execute(
            insert(ChatLeague)
            .values(
                chat_id=new_id, league_id=link.league_id,
                is_default=link.is_default, added_by=link.added_by,
            )
            .on_conflict_do_nothing()
        )

    await s.execute(delete(ChatLeague).where(ChatLeague.chat_id == old_id))
    await s.execute(delete(Chat).where(Chat.id == old_id))
    return len(links)


async def set_alert_profile(s: AsyncSession, chat_id: int, profile: str) -> None:
    await s.execute(update(Chat).where(Chat.id == chat_id).values(alert_profile=profile))


# ── leagues ────────────────────────────────────────────────────────────────
async def upsert_league(s: AsyncSession, meta: dict, kind: str = "classic") -> League:
    stmt = (
        insert(League)
        .values(
            id=meta["id"],
            name=meta.get("name", str(meta["id"])),
            kind=kind,
            scoring=meta.get("scoring"),
            start_event=meta.get("start_event", 1),
            admin_entry=meta.get("admin_entry"),
            last_synced_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=[League.id],
            set_={"name": meta.get("name"), "last_synced_at": datetime.now(UTC)},
        )
        .returning(League)
    )
    return (await s.execute(stmt)).scalar_one()


async def link_league(s: AsyncSession, chat_id: int, league_id: int, added_by: int | None) -> bool:
    existing = (
        await s.execute(select(ChatLeague).where(ChatLeague.chat_id == chat_id))
    ).scalars().all()
    is_default = not existing
    await s.execute(
        insert(ChatLeague)
        .values(chat_id=chat_id, league_id=league_id, is_default=is_default, added_by=added_by)
        .on_conflict_do_nothing()
    )
    return is_default


async def unlink_league(s: AsyncSession, chat_id: int, league_id: int) -> None:
    await s.execute(
        delete(ChatLeague).where(ChatLeague.chat_id == chat_id, ChatLeague.league_id == league_id)
    )


async def set_default_league(s: AsyncSession, chat_id: int, league_id: int) -> None:
    await s.execute(update(ChatLeague).where(ChatLeague.chat_id == chat_id).values(is_default=False))
    await s.execute(
        update(ChatLeague)
        .where(ChatLeague.chat_id == chat_id, ChatLeague.league_id == league_id)
        .values(is_default=True)
    )


async def chat_leagues(s: AsyncSession, chat_id: int) -> list[tuple[League, bool]]:
    rows = await s.execute(
        select(League, ChatLeague.is_default)
        .join(ChatLeague, ChatLeague.league_id == League.id)
        .where(ChatLeague.chat_id == chat_id)
        .order_by(ChatLeague.is_default.desc(), League.name)
    )
    return [(r[0], r[1]) for r in rows.all()]


async def default_league(s: AsyncSession, chat_id: int) -> League | None:
    row = await s.execute(
        select(League)
        .join(ChatLeague, ChatLeague.league_id == League.id)
        .where(ChatLeague.chat_id == chat_id, ChatLeague.is_default.is_(True))
    )
    return row.scalar_one_or_none()


async def get_league(s: AsyncSession, league_id: int) -> League | None:
    return await s.get(League, league_id)


async def all_active_leagues(s: AsyncSession) -> list[int]:
    """Leagues bound to at least one chat that wants alerts."""
    rows = await s.execute(
        select(ChatLeague.league_id)
        .join(Chat, Chat.id == ChatLeague.chat_id)
        .where(Chat.alert_profile != "off")
        .distinct()
    )
    return [r[0] for r in rows.all()]


async def chats_for_league(s: AsyncSession, league_id: int) -> list[Chat]:
    rows = await s.execute(
        select(Chat)
        .join(ChatLeague, ChatLeague.chat_id == Chat.id)
        .where(ChatLeague.league_id == league_id, Chat.alert_profile != "off")
    )
    return list(rows.scalars().all())


# ── managers & identity ────────────────────────────────────────────────────
async def sync_league_members(s: AsyncSession, league_id: int, entries: list[dict]) -> None:
    for e in entries:
        await s.execute(
            insert(Manager)
            .values(
                entry_id=e["entry"],
                player_name=e.get("player_name", ""),
                team_name=e.get("entry_name", ""),
                refreshed_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                index_elements=[Manager.entry_id],
                set_={"player_name": e.get("player_name", ""), "team_name": e.get("entry_name", "")},
            )
        )
        await s.execute(
            insert(LeagueManager)
            .values(league_id=league_id, entry_id=e["entry"])
            .on_conflict_do_nothing()
        )

    # Drop anyone who has left. Insert-only sync kept departed managers for
    # ever; in a side-bet that means someone who has gone still holds a paying
    # position. Their finalised gw_result rows are untouched, so past
    # gameweeks keep whoever actually played them.
    current = [e["entry"] for e in entries]
    if current:
        await s.execute(
            delete(LeagueManager).where(
                LeagueManager.league_id == league_id,
                LeagueManager.entry_id.notin_(current),
            )
        )


async def league_members(s: AsyncSession, league_id: int) -> list[Manager]:
    rows = await s.execute(
        select(Manager)
        .join(LeagueManager, LeagueManager.entry_id == Manager.entry_id)
        .where(LeagueManager.league_id == league_id)
        .order_by(Manager.team_name)
    )
    return list(rows.scalars().all())


async def claim_identity(s: AsyncSession, tg_user_id: int, entry_id: int, username: str | None,
                         display: str | None) -> None:
    await s.execute(
        insert(Identity)
        .values(tg_user_id=tg_user_id, entry_id=entry_id, username=username, display=display)
        .on_conflict_do_update(
            index_elements=[Identity.tg_user_id, Identity.entry_id],
            set_={"username": username, "display": display},
        )
    )


async def identities_for_entries(s: AsyncSession, entry_ids: list[int]) -> dict[int, Identity]:
    if not entry_ids:
        return {}
    rows = await s.execute(select(Identity).where(Identity.entry_id.in_(entry_ids)))
    return {i.entry_id: i for i in rows.scalars().all()}


# ── picks cache ────────────────────────────────────────────────────────────
async def store_picks(s: AsyncSession, entry_id: int, event: int, payload: dict) -> None:
    await s.execute(
        insert(PicksCache)
        .values(entry_id=entry_id, event=event, payload=payload, fetched_at=datetime.now(UTC))
        .on_conflict_do_update(
            index_elements=[PicksCache.entry_id, PicksCache.event],
            set_={"payload": payload, "fetched_at": datetime.now(UTC)},
        )
    )


async def load_picks(s: AsyncSession, entry_ids: list[int], event: int) -> dict[int, dict]:
    rows = await s.execute(
        select(PicksCache).where(PicksCache.entry_id.in_(entry_ids), PicksCache.event == event)
    )
    return {p.entry_id: p.payload for p in rows.scalars().all()}


# ── alert idempotency ──────────────────────────────────────────────────────
async def claim_alert(s: AsyncSession, chat_id: int, event_key: str) -> bool:
    """True if this alert has not been sent to this chat before."""
    res = await s.execute(
        insert(AlertLog)
        .values(chat_id=chat_id, event_key=event_key)
        .on_conflict_do_nothing()
        .returning(AlertLog.event_key)
    )
    return res.scalar_one_or_none() is not None


async def prune_alerts(s: AsyncSession, older_than_days: int = 14) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    await s.execute(delete(AlertLog).where(AlertLog.sent_at < cutoff))


# ── results & live messages ────────────────────────────────────────────────
async def store_gw_results(s: AsyncSession, league_id: int, event: int, rows: list[dict]) -> None:
    for r in rows:
        await s.execute(
            insert(GWResult)
            .values(league_id=league_id, event=event, **r)
            .on_conflict_do_update(
                index_elements=[GWResult.league_id, GWResult.event, GWResult.entry_id],
                set_=r,
            )
        )


async def register_live_message(s: AsyncSession, chat_id: int, message_id: int, league_id: int,
                                event: int, view: str, minutes: int = 15) -> None:
    await s.execute(
        insert(LiveMessage)
        .values(
            chat_id=chat_id, message_id=message_id, league_id=league_id, event=event, view=view,
            expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        )
        .on_conflict_do_update(
            index_elements=[LiveMessage.chat_id, LiveMessage.message_id],
            set_={"expires_at": datetime.now(UTC) + timedelta(minutes=minutes), "view": view},
        )
    )


async def active_live_messages(s: AsyncSession) -> list[LiveMessage]:
    rows = await s.execute(select(LiveMessage).where(LiveMessage.expires_at > datetime.now(UTC)))
    return list(rows.scalars().all())


async def expire_live_messages(s: AsyncSession) -> None:
    await s.execute(delete(LiveMessage).where(LiveMessage.expires_at <= datetime.now(UTC)))


# ── wagers ─────────────────────────────────────────────────────────────────
async def gw_results_for_league(s: AsyncSession, league_id: int) -> dict[int, list[GWResult]]:
    """{event: [rows]} for every finalised gameweek of a league."""
    rows = await s.execute(
        select(GWResult).where(GWResult.league_id == league_id).order_by(GWResult.event)
    )
    out: dict[int, list[GWResult]] = {}
    for r in rows.scalars().all():
        out.setdefault(r.event, []).append(r)
    return out


async def finalised_events(s: AsyncSession, league_id: int) -> list[int]:
    rows = await s.execute(
        select(GWResult.event).where(GWResult.league_id == league_id).distinct()
    )
    return sorted(rows.scalars().all())


async def get_settlement(s: AsyncSession, league_id: int) -> WagerSettlement | None:
    rows = await s.execute(
        select(WagerSettlement)
        .where(WagerSettlement.league_id == league_id)
        .order_by(WagerSettlement.season_end_event.desc())
    )
    return rows.scalars().first()


async def record_settlement(
    s: AsyncSession,
    league_id: int,
    season_end_event: int,
    balances: dict,
    payments: list,
    settled_by: int | None,
) -> bool:
    """Freeze a season's numbers. False if it was already settled."""
    res = await s.execute(
        insert(WagerSettlement)
        .values(
            league_id=league_id,
            season_end_event=season_end_event,
            balances={str(k): v for k, v in balances.items()},
            payments=payments,
            settled_by=settled_by,
        )
        .on_conflict_do_nothing()
        .returning(WagerSettlement.league_id)
    )
    return res.scalar_one_or_none() is not None
