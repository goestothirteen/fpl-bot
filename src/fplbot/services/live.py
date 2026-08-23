"""Builds the live league table — the thing the FPL API refuses to give you.

`leagues-classic/.../standings/` lags by hours (verified: `last_updated_data`
was 04:58Z while matches kicked off in the evening). So we compute:

    live_points = Σ (live_total_points[element] × multiplier)  over all 15 picks

`multiplier` already encodes bench (0), captain (2), triple captain (3) and
Bench Boost (bench comes back as 1), so no chip needs special-casing. Verified
exact against the FPL Overall league top 5 during GW1 2026/27.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..fpl.client import FPLClient
from ..logging_conf import get_logger
from .models import (
    Fixture,
    FixtureState,
    GamePhase,
    LiveTable,
    ManagerLive,
    Pick,
    Player,
    PlayerLive,
)
from .parsing import (
    parse_fixtures,
    parse_game_state,
    parse_live,
    parse_picks,
    parse_players,
    parse_teams,
)
from .scoring import apply_provisional_bonus, predict_auto_subs

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PhaseInfo:
    """Where the gameweek is, and when the next thing happens."""

    phase: GamePhase
    event: int | None
    next_kickoff: datetime | None = None


class LiveEngine:
    """Stateless over the wire, cached underneath. One instance per process."""

    def __init__(self, client: FPLClient) -> None:
        self.client = client

    # ── phase detection ────────────────────────────────────────────────────
    async def phase(self) -> PhaseInfo:
        bootstrap = await self.client.bootstrap()
        state = parse_game_state(bootstrap)
        event = state.current_event
        if event is None:
            return PhaseInfo(GamePhase.DORMANT, state.next_event, state.next_deadline)

        fixtures = parse_fixtures(await self.client.fixtures(event, live=True))
        now = datetime.now(UTC)
        upcoming = [f.kickoff for f in fixtures if f.state is FixtureState.UPCOMING and f.kickoff]
        next_kickoff = min(upcoming) if upcoming else None

        if any(f.state is FixtureState.LIVE for f in fixtures):
            return PhaseInfo(GamePhase.LIVE, event, next_kickoff)

        if next_kickoff is not None and next_kickoff - now < timedelta(minutes=30):
            return PhaseInfo(GamePhase.PRE_KICKOFF, event, next_kickoff)

        if state.data_checked:
            return PhaseInfo(GamePhase.FINALISED, event, next_kickoff)

        if all(f.state in (FixtureState.FINISHED, FixtureState.CONFIRMED) for f in fixtures):
            status = await self.client.event_status()
            days = status.get("status", [])
            bonus_done = all(d.get("bonus_added") for d in days) if days else False
            if not bonus_done:
                return PhaseInfo(GamePhase.BONUS_PENDING, event, next_kickoff)
            return PhaseInfo(GamePhase.SETTLING, event, next_kickoff)

        return PhaseInfo(GamePhase.DORMANT, event, next_kickoff)

    def poll_interval(self, info: PhaseInfo, base: int = 45) -> int:
        """Sleep as long as we safely can.

        When dormant with a kickoff on the horizon, wake two minutes before it
        rather than burning a flat 30-minute cycle and arriving late — a poller
        that misses the first ten minutes of a match misses the best alerts.
        """
        fixed = {
            GamePhase.LIVE: base,
            GamePhase.PRE_KICKOFF: 300,
            GamePhase.BONUS_PENDING: 180,
            GamePhase.SETTLING: 600,
            GamePhase.FINALISED: 1800,
        }
        if info.phase is not GamePhase.DORMANT:
            return fixed[info.phase]

        if info.next_kickoff is not None:
            until = (info.next_kickoff - datetime.now(UTC)).total_seconds() - 120
            return int(max(60, min(1800, until)))
        return 1800

    # ── the table ──────────────────────────────────────────────────────────
    async def snapshot(self, event: int, *, settled: bool = False):
        """Fetch the three shared payloads a live table needs. One call each,
        no matter how many leagues or chats are watching."""
        bootstrap = await self.client.bootstrap()
        raw_live, age = await self.client.cached_with_age(
            f"live:{event}", f"/event/{event}/live/", "live_settled" if settled else "live"
        )
        fixtures = parse_fixtures(await self.client.fixtures(event, live=not settled))
        live = parse_live(raw_live)
        provisional = apply_provisional_bonus(live, fixtures)
        return (
            parse_players(bootstrap),
            parse_teams(bootstrap),
            live,
            fixtures,
            provisional,
            age,
        )

    # A friend league is 5-30 managers. The cap stops someone linking the
    # global league (nine million entries) from fanning out to a picks call per
    # manager; the top N is still a sensible answer for a big public league.
    MAX_MANAGERS = 100

    async def build_table(
        self, league_id: int, event: int, *, limit: int | None = None
    ) -> LiveTable:
        meta, entries = await self.client.all_classic_entries(
            league_id, limit=limit or self.MAX_MANAGERS
        )
        players, _teams, live, fixtures, provisional, age = await self.snapshot(event)

        picks_by_entry = await self.client.many_picks([e["entry"] for e in entries], event)
        team_state = fixture_state_by_team(fixtures)
        team_done = {t: s is not FixtureState.UPCOMING and s is not FixtureState.LIVE
                     for t, s in team_state.items()}

        rows: list[ManagerLive] = []
        for entry in entries:
            raw = picks_by_entry.get(entry["entry"])
            if raw is None:
                rows.append(
                    ManagerLive(
                        entry_id=entry["entry"],
                        manager_name=entry.get("player_name", "?"),
                        team_name=entry.get("entry_name", "?"),
                        picks=[],
                        active_chip=None,
                        transfer_cost=0,
                        season_total_before=entry.get("total", 0) - entry.get("event_total", 0),
                    )
                )
                continue
            rows.append(
                build_manager_row(
                    entry_id=entry["entry"],
                    manager_name=entry.get("player_name", "?"),
                    team_name=entry.get("entry_name", "?"),
                    raw_picks=raw,
                    live=live,
                    players=players,
                    team_state=team_state,
                    team_done=team_done,
                )
            )

        info = await self.phase()
        return LiveTable(
            event=event,
            league_id=league_id,
            league_name=meta.get("name", str(league_id)),
            rows=rows,
            phase=info.phase,
            data_age_seconds=age,
            bonus_confirmed=not provisional,
            team_state=team_state,
        )

    async def resolve_event(self) -> int:
        bootstrap = await self.client.bootstrap()
        state = parse_game_state(bootstrap)
        if state.current_event:
            return state.current_event
        return max(1, (state.next_event or 1) - 1)


# ── pure helpers, unit-tested without a network ────────────────────────────
def fixture_state_by_team(fixtures: list[Fixture]) -> dict[int, FixtureState]:
    """Least-advanced state per team, so a double gameweek counts as unfinished
    until *both* fixtures are done."""
    order = {
        FixtureState.UPCOMING: 0,
        FixtureState.LIVE: 1,
        FixtureState.FINISHED: 2,
        FixtureState.CONFIRMED: 3,
    }
    out: dict[int, FixtureState] = {}
    for f in fixtures:
        for team in (f.team_h, f.team_a):
            cur = out.get(team)
            if cur is None or order[f.state] < order[cur]:
                out[team] = f.state
    return out


def build_manager_row(
    *,
    entry_id: int,
    manager_name: str,
    team_name: str,
    raw_picks: dict,
    live: dict[int, PlayerLive],
    players: dict[int, Player],
    team_state: dict[int, FixtureState],
    team_done: dict[int, bool],
    predict_subs: bool = True,
) -> ManagerLive:
    """Compute one manager's live gameweek line.

    ``predict_subs`` controls whether we anticipate FPL's automatic
    substitutions. FPL itself only applies them once **every** fixture in the
    gameweek has finished, so mid-gameweek our number can legitimately differ
    from `standings.event_total` — ours is the projected final score, theirs is
    the score so far. That is the more useful number in a group chat, but the
    difference is real and is surfaced in the UI with a `↑` marker.

    Pass ``predict_subs=False`` to reproduce FPL's own arithmetic exactly.
    """
    picks: list[Pick] = parse_picks(raw_picks)
    history = raw_picks.get("entry_history", {}) or {}
    chip = raw_picks.get("active_chip")

    # If FPL hasn't published auto-subs yet, predict them.
    if predict_subs and not raw_picks.get("automatic_subs") and chip != "bboost":
        for out_el, in_el in predict_auto_subs(picks, live, players, team_done):
            for p in picks:
                if p.element == out_el:
                    p.subbed_out = True
                elif p.element == in_el:
                    p.subbed_in = True
    predicted = [(p.element, q.element) for p in picks if p.subbed_out for q in picks if q.subbed_in]

    gw_points = 0
    bench_points = 0
    chip_points = 0
    to_play = in_play = played = 0
    captain_element = None
    captain_points = 0
    captain_played = False
    has_prov = False

    for pick in picks:
        pl = live.get(pick.element)
        pts = pl.effective_points if pl else 0
        if pl and pl.provisional_bonus:
            has_prov = True
        mult = pick.effective_multiplier
        gw_points += pts * mult
        # Bench boost pays out the four bench slots (which carry multiplier 1
        # instead of 0); triple captain pays one extra multiple of the captain.
        if chip == "bboost" and pick.position >= 12:
            chip_points += pts * mult
        elif chip == "3xc" and pick.is_captain:
            chip_points += pts
        if mult == 0:
            bench_points += pts
        else:
            player = players.get(pick.element)
            state = team_state.get(player.team) if player else None
            if state is FixtureState.UPCOMING:
                to_play += 1
            elif state is FixtureState.LIVE:
                in_play += 1
            else:
                played += 1
        if pick.is_captain:
            captain_element = pick.element
            captain_points = pts * max(mult, 1)
            captain_played = bool(pl and pl.minutes > 0)

    total = history.get("total_points", 0)
    event_pts = history.get("points", 0)

    return ManagerLive(
        entry_id=entry_id,
        manager_name=manager_name,
        team_name=team_name,
        picks=picks,
        active_chip=chip,
        transfer_cost=history.get("event_transfers_cost", 0),
        season_total_before=max(0, total - event_pts),
        gw_points=gw_points,
        bench_points=bench_points,
        captain_element=captain_element,
        captain_points=captain_points,
        captain_played=captain_played,
        chip_points=chip_points,
        to_play=to_play,
        in_play=in_play,
        played=played,
        predicted_subs=predicted,
        has_provisional_bonus=has_prov,
    )
