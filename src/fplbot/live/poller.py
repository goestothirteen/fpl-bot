"""The single shared poller.

One loop feeds every chat. Ten friend groups watching the same gameweek create
exactly the same upstream traffic as one, because the expensive calls
(`event/{gw}/live/`, `fixtures/`) are per-gameweek, not per-league, and picks
are cached for the whole week.

Cadence is adaptive — see docs/ARCHITECTURE.md §5. At the 45 s live rate a busy
Saturday costs roughly 80 upstream calls.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot
from sqlalchemy import select

from ..db import repo
from ..db.models import LiveSnapshot
from ..db.session import session_scope
from ..logging_conf import get_logger
from ..services.live import LiveEngine
from ..services.models import GamePhase
from ..services.parsing import parse_live, parse_players
from .events import attribute, detect_lead_change, diff_live, snapshot_of
from .notifier import Notifier

log = get_logger(__name__)


class LivePoller:
    def __init__(self, engine: LiveEngine, bot: Bot, notifier: Notifier,
                 base_interval: int = 45) -> None:
        self.engine = engine
        self.bot = bot
        self.notifier = notifier
        self.base_interval = base_interval
        self._leaders: dict[int, int] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="live-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        log.info("poller.started")
        while not self._stopping.is_set():
            try:
                interval = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("poller.tick_failed")
                interval = 120
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        info = await self.engine.phase()
        phase, event = info.phase, info.event
        if event is None or phase is GamePhase.DORMANT:
            log.debug("poller.dormant", phase=str(phase), next_kickoff=str(info.next_kickoff))
            return self.engine.poll_interval(info, self.base_interval)

        async with session_scope() as s:
            league_ids = await repo.all_active_leagues(s)
        if not league_ids:
            return self.engine.poll_interval(info, self.base_interval)

        settled = phase in (GamePhase.SETTLING, GamePhase.FINALISED)
        live = parse_live(await self.engine.client.live(event, settled=settled))
        players = parse_players(await self.engine.client.bootstrap())
        names = {p.id: p.web_name for p in players.values()}

        previous = await self._load_snapshot(event)
        raw_events = diff_live(previous, live) if previous else []

        for league_id in league_ids:
            try:
                await self._process_league(league_id, event, raw_events, names)
            except Exception:  # noqa: BLE001 - one bad league must not stop the rest
                log.exception("poller.league_failed", league=league_id)

        await self._store_snapshot(event, live)
        await self._refresh_live_messages()

        if phase is GamePhase.FINALISED:
            await self._finalise(event, league_ids)

        return self.engine.poll_interval(info, self.base_interval)

    async def _process_league(self, league_id: int, event: int, raw_events, names) -> None:  # noqa: ANN001
        table = await self.engine.build_table(league_id, event)
        league_events = attribute(raw_events, table, names)

        lead = detect_lead_change(
            table, self._leaders.get(league_id), raw_events, names
        )
        if lead:
            league_events.append(lead)
        ranked = table.ranked()
        if ranked:
            self._leaders[league_id] = ranked[0].entry_id

        if not league_events:
            return
        async with session_scope() as s:
            chats = await repo.chats_for_league(s, league_id)
        for chat in chats:
            await self.notifier.dispatch(chat, league_events)

    async def _load_snapshot(self, event: int) -> dict[int, dict]:
        async with session_scope() as s:
            rows = await s.execute(select(LiveSnapshot).where(LiveSnapshot.event == event))
            return {r.element: r.stats for r in rows.scalars().all()}

    async def _store_snapshot(self, event: int, live) -> None:  # noqa: ANN001
        from sqlalchemy.dialects.postgresql import insert

        payload = snapshot_of(live)
        async with session_scope() as s:
            for element, stats in payload.items():
                await s.execute(
                    insert(LiveSnapshot)
                    .values(event=event, element=element, stats=stats)
                    .on_conflict_do_update(
                        index_elements=[LiveSnapshot.event, LiveSnapshot.element],
                        set_={"stats": stats},
                    )
                )

    async def _refresh_live_messages(self) -> None:
        """Edit self-refreshing /live messages instead of posting new ones."""
        from ..bot.handlers.live import _context, _render  # local import: avoids a cycle
        from ..bot.keyboards import live_views

        async with session_scope() as s:
            messages = await repo.active_live_messages(s)
            await repo.expire_live_messages(s)

        for m in messages:
            try:
                table, players, live = await _context(self.engine, m.league_id, m.event)
                await self.bot.edit_message_text(
                    await _render(m.view, self.engine, table, players, live),
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    reply_markup=live_views(m.league_id, m.event, m.view),
                )
            except Exception as exc:  # noqa: BLE001 - "not modified" et al
                log.debug("poller.edit_skipped", chat=m.chat_id, error=str(exc))

    async def _finalise(self, event: int, league_ids: list[int]) -> None:
        """Persist the confirmed table once FPL says data_checked, then post the
        wrap-up exactly once per chat (the alert_log key makes that safe)."""
        from ..bot.formatting import render_awards, render_live_cards
        from ..services.analysis import awards
        from ..services.parsing import parse_players

        players = parse_players(await self.engine.client.bootstrap())
        live = parse_live(await self.engine.client.live(event, settled=True))

        for league_id in league_ids:
            table = await self.engine.build_table(league_id, event)
            rows = [
                {
                    "entry_id": r.entry_id,
                    "points": r.gw_points,
                    "net_points": r.net_points,
                    "rank": i + 1,
                    "bench_points": r.bench_points,
                    "transfer_cost": r.transfer_cost,
                    "captain_element": r.captain_element,
                    "captain_points": r.captain_points,
                    "chip": r.active_chip,
                }
                for i, r in enumerate(table.ranked_gw())
            ]
            async with session_scope() as s:
                await repo.store_gw_results(s, league_id, event, rows)
                chats = await repo.chats_for_league(s, league_id)

            text = (
                f"🏁 <b>Gameweek {event} is final</b>\n\n"
                + render_live_cards(table)
                + "\n\n"
                + render_awards(awards(table, players, live))
            )
            for chat in chats:
                async with session_scope() as s:
                    fresh = await repo.claim_alert(s, chat.id, f"gw{event}:final:{league_id}")
                if fresh:
                    await self.bot.send_message(chat.id, text, disable_web_page_preview=True)

            await self._post_wager(league_id, event, chats)

    async def _post_wager(self, league_id: int, event: int, chats) -> None:  # noqa: ANN001
        """Settle the side-bet for a finalised gameweek, once per chat.

        Amounts are derived from gw_result rather than accumulated, so running
        this twice is a no-op; the alert_log key stops the *message* repeating.
        """
        from ..bot.formatting import render_wager_week
        from ..services import ledger as ledger_svc
        from ..services.wager import scheme_for

        if scheme_for(league_id) is None:
            return
        try:
            async with session_scope() as s:
                league = await repo.get_league(s, league_id)
            led = await ledger_svc.build(self.engine, league_id, getattr(league, "name", ""))
            if led is None or event not in led.by_event:
                return
            text = render_wager_week(led, event)
        except Exception:  # noqa: BLE001 - a wager failure must not break finalisation
            log.exception("poller.wager_failed", league=league_id, event=event)
            return

        for chat in chats:
            async with session_scope() as s:
                fresh = await repo.claim_alert(s, chat.id, f"gw{event}:wager:{league_id}")
            if fresh:
                await self.bot.send_message(chat.id, text, disable_web_page_preview=True)
