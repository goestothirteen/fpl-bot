"""The two-stage settlement lifecycle, seen from the ledger.

Weekly amounts are always recomputed from `gw_result` rather than stored, so a
gameweek that gets rewritten — by a late bonus correction, or by the confirm
step — flows straight through to the standings. These tests pin that, and pin
the rule that keeps real money out of provisional numbers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from fplbot.services import ledger as ledger_svc

LEAGUE = 166726          # ZonggersNIPS: mirrored ladder, so with three players
CENTS = 100              # the week pays +30 / 0 / -30 and the season +300/0/-300

MEMBERS = [
    SimpleNamespace(entry_id=1, team_name="Alpha", player_name="A"),
    SimpleNamespace(entry_id=2, team_name="Bravo", player_name="B"),
    SimpleNamespace(entry_id=3, team_name="Charlie", player_name="C"),
]


def row(entry_id: int, net_points: int, *, provisional: bool):
    return SimpleNamespace(entry_id=entry_id, net_points=net_points, provisional=provisional)


@pytest.fixture
def patched(monkeypatch):
    """Drive `ledger.build` off an in-memory `gw_result` table."""
    state: dict[str, dict] = {"stored": {}}

    @asynccontextmanager
    async def fake_scope():
        yield None

    async def league_members(_s, _league_id):
        return MEMBERS

    async def gw_results_for_league(_s, _league_id):
        return state["stored"]

    monkeypatch.setattr(ledger_svc, "session_scope", fake_scope)
    monkeypatch.setattr(
        ledger_svc, "repo",
        SimpleNamespace(
            league_members=league_members,
            gw_results_for_league=gw_results_for_league,
        ),
    )
    # No bootstrap events -> nothing is data_checked -> backfill never runs.
    monkeypatch.setattr(ledger_svc, "_settled_events", lambda _engine: _none())
    return state


async def _none():
    return []


async def build(state):
    return await ledger_svc.build(SimpleNamespace(), LEAGUE, "ZonggersNIPS")


async def test_a_provisional_week_is_settled_and_flagged(patched):
    patched["stored"] = {3: [row(1, 67, provisional=True), row(2, 55, provisional=True),
                             row(3, 44, provisional=True)]}
    led = await build(patched)

    assert led.events == [3]
    assert led.provisional == frozenset({3})
    assert led.has_provisional
    # The week pays out normally — provisional means "not yet confirmed", not
    # "not yet counted".
    assert led.balances[1] == 30 * CENTS
    assert led.balances[2] == 0
    assert led.balances[3] == -30 * CENTS


async def test_a_confirmed_week_carries_no_flag(patched):
    patched["stored"] = {3: [row(1, 67, provisional=False), row(2, 55, provisional=False),
                             row(3, 44, provisional=False)]}
    led = await build(patched)
    assert led.provisional == frozenset()
    assert not led.has_provisional


async def test_a_late_correction_rewrites_the_week_and_moves_the_money(patched):
    """The whole point of not storing weekly amounts: re-upserting gw_result
    with corrected points re-derives positions and cash on the next view."""
    patched["stored"] = {3: [row(1, 67, provisional=True), row(2, 55, provisional=True),
                             row(3, 44, provisional=True)]}
    before = await build(patched)
    assert before.balances[1] == 30 * CENTS
    assert before.by_event[3][0].entry_id == 1

    # FPL strips a bonus point off entry 1 and awards entry 2 a goal.
    patched["stored"] = {3: [row(1, 66, provisional=True), row(2, 70, provisional=True),
                             row(3, 44, provisional=True)]}
    after = await build(patched)

    assert after.by_event[3][0].entry_id == 2, "the corrected week re-ranks"
    assert after.balances[2] == 30 * CENTS, "entry 2 now takes the win"
    assert after.balances[1] == 0, "entry 1 drops to the middle and takes nothing"
    assert after.balances != before.balances


async def test_confirmation_clears_the_flag_and_leaves_the_figures_alone(patched):
    """data_checked arrives with no change to the points: same money, no flag."""
    rows = [(1, 67), (2, 55), (3, 44)]
    patched["stored"] = {3: [row(e, p, provisional=True) for e, p in rows]}
    provisional = await build(patched)

    patched["stored"] = {3: [row(e, p, provisional=False) for e, p in rows]}
    confirmed = await build(patched)

    assert provisional.balances == confirmed.balances
    assert [r.amount for r in provisional.by_event[3]] == \
           [r.amount for r in confirmed.by_event[3]]
    assert confirmed.provisional == frozenset()


async def test_one_provisional_week_withholds_the_whole_season_pot(patched):
    """`cmd_settle` freezes off `season_amounts`, so it must stay empty while
    any gameweek can still move. This is the guard on real money."""
    patched["stored"] = {
        gw: [row(1, 60, provisional=(gw == 38)), row(2, 50, provisional=(gw == 38)),
             row(3, 40, provisional=(gw == 38))]
        for gw in range(1, 39)
    }
    led = await ledger_svc.with_season(SimpleNamespace(), await build(patched), total_events=38)

    assert led.provisional == frozenset({38})
    assert led.season_amounts is None, "a provisional week must not produce a season pot"
    assert led.final_balances == led.balances


async def test_a_fully_confirmed_season_does_produce_the_pot(patched):
    patched["stored"] = {
        gw: [row(1, 60, provisional=False), row(2, 50, provisional=False),
             row(3, 40, provisional=False)]
        for gw in range(1, 39)
    }
    led = await ledger_svc.with_season(SimpleNamespace(), await build(patched), total_events=38)

    assert led.provisional == frozenset()
    assert led.season_amounts is not None
    assert led.season_amounts[1] == 300 * CENTS
    assert led.final_balances != led.balances
