"""When a gameweek may be provisionally settled.

Modelled on GW3 2026/27: every fixture finished by 15:30Z on the Sunday with
bonus awarded and folded, while `event.data_checked` stayed false for another
fourteen hours. The old trigger waited for the flag; these rules don't.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fplbot.services.models import Fixture, PlayerLive
from fplbot.services.settlement import (
    MATCH_LENGTH,
    SETTLE_BUFFER,
    bonus_is_folded,
    settlement_ready,
)

LAST_KICKOFF = datetime(2026, 9, 6, 15, 30, tzinfo=UTC)
# The moment the buffer expires: last kickoff + match length + buffer.
READY_AT = LAST_KICKOFF + MATCH_LENGTH + SETTLE_BUFFER


def fx(
    fid: int,
    *,
    kickoff: datetime = LAST_KICKOFF,
    provisional: bool = True,
    finished: bool = False,
    bonus: dict[int, int] | None = None,
) -> Fixture:
    return Fixture(
        id=fid, event=3, team_h=fid, team_a=fid + 50, team_h_score=1, team_a_score=0,
        kickoff=kickoff, minutes=90, started=True, finished=finished,
        finished_provisional=provisional,
        bonus={fid: 3} if bonus is None else bonus,
    )


def live_for(fixtures: list[Fixture], *, folded: bool = True) -> dict[int, PlayerLive]:
    """A live map that agrees (or deliberately disagrees) with the bonus awards."""
    out: dict[int, PlayerLive] = {}
    for f in fixtures:
        for element, awarded in f.bonus.items():
            out[element] = PlayerLive(
                element=element, minutes=90, points=8 if folded else 8 - awarded,
                bonus=awarded if folded else 0,
            )
    return out


def test_ready_when_football_is_over_bonus_folded_and_buffer_elapsed():
    fixtures = [fx(1), fx(2, kickoff=LAST_KICKOFF - timedelta(days=1))]
    ready = settlement_ready(fixtures, live_for(fixtures), now=READY_AT)
    assert ready.ready, ready.reason


def test_bonus_awarded_but_not_yet_folded_into_totals_is_not_ready():
    """The window where FPL has published the 3/2/1 but the players' totals
    haven't caught up. Settling here banks scores that are about to rise."""
    fixtures = [fx(1)]
    live = live_for(fixtures, folded=False)
    assert not bonus_is_folded(fixtures, live)
    ready = settlement_ready(fixtures, live, now=READY_AT)
    assert not ready.ready
    assert "folded" in ready.reason


def test_no_bonus_published_at_all_is_not_ready():
    fixtures = [fx(1, bonus={})]
    ready = settlement_ready(fixtures, {}, now=READY_AT)
    assert not ready.ready
    assert "folded" in ready.reason


def test_a_postponed_fixture_keeps_the_week_open():
    """The safety property the old data_checked trigger gave us for free."""
    fixtures = [fx(1), fx(2, provisional=False)]
    ready = settlement_ready(fixtures, live_for(fixtures), now=READY_AT)
    assert not ready.ready
    assert "not finished" in ready.reason


def test_double_gameweek_needs_every_fixture_done():
    """Two fixtures for the same team; one still to play."""
    fixtures = [
        fx(1),
        Fixture(id=2, event=3, team_h=1, team_a=9, team_h_score=None, team_a_score=None,
                kickoff=LAST_KICKOFF, minutes=0, started=False, finished=False,
                finished_provisional=False, bonus={}),
    ]
    assert not settlement_ready(fixtures, live_for(fixtures), now=READY_AT).ready


def test_buffer_not_yet_elapsed_is_not_ready():
    fixtures = [fx(1)]
    live = live_for(fixtures)
    assert not settlement_ready(fixtures, live, now=READY_AT - timedelta(seconds=1)).ready
    assert settlement_ready(fixtures, live, now=READY_AT).ready


def test_buffer_is_measured_from_the_last_kickoff_in_the_week():
    """A Monday-night game holds the whole week open, not just its own fixture."""
    monday = LAST_KICKOFF + timedelta(days=1)
    fixtures = [fx(1), fx(2, kickoff=monday)]
    live = live_for(fixtures)
    assert not settlement_ready(fixtures, live, now=READY_AT).ready
    assert settlement_ready(
        fixtures, live, now=monday + MATCH_LENGTH + SETTLE_BUFFER
    ).ready


def test_a_confirmed_fixture_counts_as_complete():
    """`finished` implies the whistle went, even though FPL sets it later."""
    fixtures = [fx(1, provisional=False, finished=True)]
    assert settlement_ready(fixtures, live_for(fixtures), now=READY_AT).ready


def test_no_fixtures_is_never_ready():
    assert not settlement_ready([], {}, now=READY_AT).ready


def test_readiness_is_falsy_so_it_reads_like_a_bool():
    assert not settlement_ready([], {}, now=READY_AT)
    fixtures = [fx(1)]
    assert settlement_ready(fixtures, live_for(fixtures), now=READY_AT)


def test_buffer_length_is_configurable_without_touching_the_rules():
    fixtures = [fx(1)]
    live = live_for(fixtures)
    relaxed = LAST_KICKOFF + MATCH_LENGTH + timedelta(minutes=5)
    assert not settlement_ready(fixtures, live, now=relaxed).ready
    assert settlement_ready(fixtures, live, now=relaxed, buffer=timedelta(minutes=5)).ready
