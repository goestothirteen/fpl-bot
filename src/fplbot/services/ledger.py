"""Assembling wager ledgers from finalised results.

`gw_result` is written by the poller when FPL sets `data_checked`. That is the
correct trigger — a postponed fixture keeps the gameweek open, so nothing is
ever settled on incomplete data — but it means a gameweek that finalises while
the bot happens to be down would be lost for ever. For a side-bet that is not
acceptable, so anything missing is rebuilt from `entry/{id}/history/`, which is
permanent and per-manager rather than a moment-in-time snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db import repo
from ..db.session import session_scope
from ..logging_conf import get_logger
from . import wager
from .wager import Scheme

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WeekRow:
    entry_id: int
    team_name: str
    manager_name: str
    points: int
    position: int
    amount: int          # cents for this gameweek
    balance: int         # cumulative cents after this gameweek
    agreed: bool = False # hand-settled rather than computed from the ladder


@dataclass(frozen=True, slots=True)
class Ledger:
    league_id: int
    league_name: str
    scheme: Scheme
    events: list[int]
    by_event: dict[int, list[WeekRow]]
    balances: dict[int, int]                 # entry_id -> cumulative cents
    names: dict[int, tuple[str, str]]        # entry_id -> (team, manager)
    season_amounts: dict[int, int] | None = None   # applied only once complete
    season_positions: dict[int, int] | None = None

    @property
    def final_balances(self) -> dict[int, int]:
        """Weekly running total plus the season adjustment, when it applies."""
        if not self.season_amounts:
            return dict(self.balances)
        out = dict(self.balances)
        for entry_id, cents in self.season_amounts.items():
            out[entry_id] = out.get(entry_id, 0) + cents
        return out


async def _backfill(engine, league_id: int, event: int, members: list) -> list[dict]:  # noqa: ANN001
    """Rebuild one gameweek's rows from each manager's permanent history."""
    rows = []
    for m in members:
        try:
            history = await engine.client.entry_history(m.entry_id)
        except Exception:  # noqa: BLE001 - a single unreachable manager must not stall the rest
            log.warning("ledger.history_failed", entry=m.entry_id, event=event)
            continue
        entry = next((h for h in history.get("current", []) if h.get("event") == event), None)
        if entry is None:
            continue
        rows.append(
            {
                "entry_id": m.entry_id,
                "points": entry.get("points", 0),
                "net_points": entry.get("points", 0) - entry.get("event_transfers_cost", 0),
                "rank": 0,
                "bench_points": entry.get("points_on_bench", 0),
                "transfer_cost": entry.get("event_transfers_cost", 0),
                "captain_element": None,
                "captain_points": 0,
                "chip": None,
            }
        )
    if rows:
        async with session_scope() as s:
            await repo.store_gw_results(s, league_id, event, rows)
    return rows


async def _settled_events(engine) -> list[int]:  # noqa: ANN001
    """Gameweeks FPL has fully checked — the same signal the poller finalises
    on, so a postponed fixture keeps its gameweek out of the wager entirely."""
    bootstrap = await engine.client.bootstrap()
    return [e["id"] for e in bootstrap.get("events", []) if e.get("data_checked")]


async def build(engine, league_id: int, league_name: str) -> Ledger | None:  # noqa: ANN001
    """Every finalised gameweek, in order, with running balances."""
    scheme = wager.scheme_for(league_id)
    if scheme is None:
        return None

    async with session_scope() as s:
        members = await repo.league_members(s, league_id)
        stored = await repo.gw_results_for_league(s, league_id)

    names = {m.entry_id: (m.team_name, m.player_name) for m in members}

    # Any gameweek FPL considers settled but we never stored gets rebuilt.
    for event in await _settled_events(engine):
        if event not in stored:
            rebuilt = await _backfill(engine, league_id, event, members)
            if rebuilt:
                async with session_scope() as s:
                    stored = await repo.gw_results_for_league(s, league_id)

    events = sorted(stored)
    by_event: dict[int, list[WeekRow]] = {}
    balances: dict[int, int] = {}

    for event in events:
        rows = stored[event]
        scores = [(r.entry_id, r.net_points) for r in rows]
        places = wager.positions(scores)
        # A hand-agreed gameweek replaces the ladder outright.
        override = wager.override_for(league_id, event)
        amounts = dict(override) if override else wager.settle(scheme, scores, season=False)

        week: list[WeekRow] = []
        for r in sorted(rows, key=lambda x: (-x.net_points, x.entry_id)):
            cents = amounts.get(r.entry_id, 0)
            balances[r.entry_id] = balances.get(r.entry_id, 0) + cents
            team, manager = names.get(r.entry_id, (str(r.entry_id), ""))
            week.append(
                WeekRow(
                    entry_id=r.entry_id,
                    team_name=team,
                    manager_name=manager,
                    points=r.net_points,
                    position=places.get(r.entry_id, 0),
                    amount=cents,
                    balance=balances[r.entry_id],
                    agreed=override is not None,
                )
            )
        by_event[event] = week

    return Ledger(
        league_id=league_id,
        league_name=league_name,
        scheme=scheme,
        events=events,
        by_event=by_event,
        balances=balances,
        names=names,
    )


async def with_season(engine, ledger: Ledger, *, total_events: int = 38) -> Ledger:  # noqa: ANN001
    """Add the season adjustment, but only once the season is genuinely over.

    Deliberately a separate step applied *after* the final gameweek's weekly
    settlement. The two pots are independent, so the order changes no
    arithmetic — but keeping them apart leaves the weekly ledger a clean
    38-row series anyone can check against the FPL site, lets the summary read
    `weekly + season = final`, and means a late correction to the last
    gameweek recomputes both without one contaminating the other.
    """
    if len(ledger.events) < total_events or total_events not in ledger.events:
        return ledger

    season_scores: list[tuple[int, int]] = []
    for entry_id in ledger.names:
        total = sum(
            row.points
            for event in ledger.events
            for row in ledger.by_event[event]
            if row.entry_id == entry_id
        )
        season_scores.append((entry_id, total))

    return Ledger(
        league_id=ledger.league_id,
        league_name=ledger.league_name,
        scheme=ledger.scheme,
        events=ledger.events,
        by_event=ledger.by_event,
        balances=ledger.balances,
        names=ledger.names,
        season_amounts=wager.settle(ledger.scheme, season_scores, season=True),
        season_positions=wager.positions(season_scores),
    )
