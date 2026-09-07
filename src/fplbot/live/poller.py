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
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select

from ..db import repo
from ..db.models import LiveSnapshot
from ..db.session import session_scope
from ..logging_conf import get_logger
from ..services.live import LiveEngine
from ..services.models import GamePhase
from ..services.parsing import parse_fixtures, parse_live, parse_players
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

        async with session_scope() as s:
            league_ids = await repo.all_active_leagues(s)

        # Settlement runs on its own schedule, before the dormant early-return
        # below. A gameweek can still be awaiting confirmation long after the
        # next one's deadline has passed — once `current_event` moves on, this
        # is the only thing still looking at the old week, and returning early
        # would strand it as provisional for ever.
        if league_ids:
            try:
                await self._settle_pass(event, league_ids)
            except Exception:  # noqa: BLE001 - settlement must not kill the loop
                log.exception("poller.settle_pass_failed", gw=event)

        if event is None or phase is GamePhase.DORMANT:
            log.debug("poller.dormant", phase=str(phase), next_kickoff=str(info.next_kickoff))
            return self.engine.poll_interval(info, self.base_interval)

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

    async def _settle_pass(self, current_event: int | None, league_ids: list[int]) -> None:
        """Advance every gameweek that is mid-settlement.

        Two stages, per `services.settlement`: write the week provisionally once
        the football is over and bonus has landed, then rewrite and un-flag it
        when FPL sets data_checked. Everything in between is a re-upsert, so a
        late correction overwrites the provisional figure instead of sticking.
        """
        bootstrap = await self.engine.client.bootstrap()
        confirmed = {e["id"] for e in bootstrap.get("events", []) if e.get("data_checked")}

        async with session_scope() as s:
            pending = await repo.provisional_events(s)
            done = set(await repo.confirmed_events(s))

        # Provisional weeks stay on the list until FPL confirms them, however
        # many weeks that takes. The current week joins them unless it is
        # already written and confirmed — once a week is locked there is
        # nothing left to re-derive, so we stop touching it.
        targets = set(pending)
        if current_event is not None and current_event not in done:
            targets.add(current_event)
        for event in sorted(targets):
            try:
                await self._settle_event(event, league_ids, confirmed=event in confirmed)
            except Exception:  # noqa: BLE001 - one bad week must not stop the rest
                log.exception("poller.settle_failed", gw=event)

    async def _settle_event(
        self, event: int, league_ids: list[int], *, confirmed: bool
    ) -> None:
        from ..services.settlement import settlement_ready

        if not confirmed:
            fixtures = parse_fixtures(await self.engine.client.fixtures(event, live=True))
            # `settlement_ready` rejects an unfinished week before it looks at
            # any player, so don't pay for `/event/{gw}/live/` until the
            # football is actually over — this runs on every poll.
            live_now = (
                parse_live(await self.engine.client.live(event))
                if fixtures and all(f.complete for f in fixtures)
                else {}
            )
            ready = settlement_ready(fixtures, live_now, now=datetime.now(UTC))
            if not ready:
                log.debug("poller.settle_not_ready", gw=event, reason=ready.reason)
                return

        for league_id in league_ids:
            await self._store_week(league_id, event, provisional=not confirmed)

        # `gw=` not `event=`: structlog reserves `event` for the message itself,
        # and colliding with it raises inside the log call.
        log.info(
            "poller.settled",
            gw=event,
            stage="confirmed" if confirmed else "provisional",
            leagues=len(league_ids),
        )
        if confirmed:
            await self._announce_final(event, league_ids)

    async def _store_week(self, league_id: int, event: int, *, provisional: bool) -> None:
        """Upsert one league's rows for one gameweek. Safe to repeat."""
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
            await repo.store_gw_results(s, league_id, event, rows, provisional=provisional)

    async def _announce_final(self, event: int, league_ids: list[int]) -> None:
        """Post the wrap-up exactly once per chat (the alert_log key makes that
        safe). Only ever fires on a confirmed gameweek — a provisional week is
        shown in /wager but never announced as final."""
        from ..bot.formatting import render_awards, render_live_cards
        from ..services.analysis import awards
        from ..services.parsing import parse_players

        players = parse_players(await self.engine.client.bootstrap())
        live = parse_live(await self.engine.client.live(event, settled=True))

        for league_id in league_ids:
            table = await self.engine.build_table(league_id, event)
            async with session_scope() as s:
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
                    await self.bot.send_message(
                        chat.id, text, disable_web_page_preview=True,
                        message_thread_id=repo.topic_of(chat),
                    )

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
            log.exception("poller.wager_failed", league=league_id, gw=event)
            return

        for chat in chats:
            async with session_scope() as s:
                fresh = await repo.claim_alert(s, chat.id, f"gw{event}:wager:{league_id}")
            if fresh:
                await self.bot.send_message(
                    chat.id, text, disable_web_page_preview=True,
                    message_thread_id=repo.topic_of(chat),
                )
