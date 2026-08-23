"""Domain objects.

Deliberately plain dataclasses over the raw API dicts: the services layer is
pure and unit-testable, and nothing below this line knows Telegram exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class FixtureState(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"
    FINISHED = "finished"       # finished_provisional — 90' blown, bonus may be provisional
    CONFIRMED = "confirmed"     # FPL has confirmed everything


class GamePhase(StrEnum):
    """What the live poller should be doing right now."""

    DORMANT = "dormant"
    PRE_KICKOFF = "pre_kickoff"
    LIVE = "live"
    BONUS_PENDING = "bonus_pending"
    SETTLING = "settling"
    FINALISED = "finalised"


@dataclass(frozen=True, slots=True)
class Team:
    id: int
    name: str
    short_name: str


@dataclass(frozen=True, slots=True)
class Player:
    id: int
    web_name: str
    team: int
    element_type: int        # 1 GK, 2 DEF, 3 MID, 4 FWD
    now_cost: int            # tenths of a million
    status: str              # a=available d=doubtful i=injured s=suspended u=unavailable
    news: str
    selected_by_percent: float
    form: float
    total_points: int
    chance_of_playing: int | None

    @property
    def price(self) -> float:
        return self.now_cost / 10

    @property
    def flagged(self) -> bool:
        return self.status != "a"


@dataclass(frozen=True, slots=True)
class Fixture:
    id: int
    event: int | None
    team_h: int
    team_a: int
    team_h_score: int | None
    team_a_score: int | None
    kickoff: datetime | None
    minutes: int
    started: bool
    finished: bool
    finished_provisional: bool
    bps: dict[int, int] = field(default_factory=dict)   # element id -> bps

    @property
    def state(self) -> FixtureState:
        if self.finished:
            return FixtureState.CONFIRMED
        if self.finished_provisional:
            return FixtureState.FINISHED
        if self.started:
            return FixtureState.LIVE
        return FixtureState.UPCOMING


@dataclass(slots=True)
class PlayerLive:
    """A player's live gameweek line, with provisional bonus folded in."""

    element: int
    minutes: int
    points: int                  # includes confirmed bonus from the API
    provisional_bonus: int = 0   # our own BPS-derived bonus for in-play fixtures
    bps: int = 0
    goals: int = 0
    assists: int = 0
    clean_sheets: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    saves: int = 0
    bonus: int = 0
    defensive_contribution: int = 0
    explain: list[dict] = field(default_factory=list)

    @property
    def effective_points(self) -> int:
        return self.points + self.provisional_bonus


@dataclass(slots=True)
class Pick:
    element: int
    position: int          # 1-11 pitch, 12-15 bench
    multiplier: int        # 0 bench, 1 playing, 2 captain, 3 triple captain
    is_captain: bool
    is_vice_captain: bool
    subbed_in: bool = False
    subbed_out: bool = False

    @property
    def effective_multiplier(self) -> int:
        if self.subbed_out:
            return 0
        if self.subbed_in and self.multiplier == 0:
            return 1
        return self.multiplier


@dataclass(slots=True)
class ManagerLive:
    """One manager's live gameweek, computed by us — not read from standings."""

    entry_id: int
    manager_name: str
    team_name: str
    picks: list[Pick]
    active_chip: str | None
    transfer_cost: int
    season_total_before: int      # total_points at end of previous GW
    gw_points: int = 0            # gross, before hit
    bench_points: int = 0
    captain_element: int | None = None
    captain_points: int = 0
    captain_played: bool = False
    to_play: int = 0              # starters whose fixture hasn't kicked off
    in_play: int = 0              # starters currently on the pitch
    played: int = 0
    predicted_subs: list[tuple[int, int]] = field(default_factory=list)  # (out, in)
    has_provisional_bonus: bool = False
    chip_points: int = 0          # points attributable to an active chip

    @property
    def net_points(self) -> int:
        return self.gw_points - self.transfer_cost

    @property
    def base_points(self) -> int:
        """Net points with the chip's contribution stripped out.

        A bench boost folds four extra players into the same total, so the raw
        score can't be compared against a manager who didn't play one. This is
        the number to settle a side-bet on.
        """
        return self.net_points - self.chip_points

    @property
    def live_total(self) -> int:
        return self.season_total_before + self.net_points

    @property
    def remaining(self) -> int:
        return self.to_play + self.in_play


@dataclass(slots=True)
class LiveTable:
    event: int
    league_id: int
    league_name: str
    rows: list[ManagerLive]
    phase: GamePhase
    data_age_seconds: float = 0.0
    bonus_confirmed: bool = True
    team_state: dict[int, FixtureState] = field(default_factory=dict)

    def ranked(self) -> list[ManagerLive]:
        """Season standings — cumulative total first."""
        return sorted(self.rows, key=lambda m: (-m.live_total, -m.net_points, m.team_name))

    def ranked_gw(self) -> list[ManagerLive]:
        """This gameweek only. `/live` shows GW points, so it must order by them
        too — ordering by season total while displaying GW points put the rows
        in an order the numbers didn't explain."""
        return sorted(self.rows, key=lambda m: (-m.net_points, -m.live_total, m.team_name))


@dataclass(frozen=True, slots=True)
class GameState:
    """The bootstrap-derived view of where we are in the season."""

    current_event: int | None
    next_event: int | None
    next_deadline: datetime | None
    finished: bool
    data_checked: bool
    average_score: int
    highest_score: int | None
