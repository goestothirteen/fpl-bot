"""Live smoke tests against the real FPL API.

Skipped by default — run with `pytest -m integration --run-integration`, or in
CI on a schedule. They exist because the single biggest risk in this project is
FPL quietly changing a payload shape between seasons.
"""
from __future__ import annotations

import pytest

from fplbot.fpl.client import FPLClient
from fplbot.services.live import LiveEngine, build_manager_row, fixture_state_by_team
from fplbot.services.models import FixtureState
from fplbot.services.parsing import (
    parse_fixtures,
    parse_game_state,
    parse_live,
    parse_players,
    parse_teams,
)

pytestmark = pytest.mark.integration

OVERALL_LEAGUE = 314   # the global league; always exists, always populated


@pytest.fixture
async def client():
    c = FPLClient(None, max_concurrency=3, rate_per_sec=3)
    try:
        yield c
    finally:
        await c.aclose()


async def test_bootstrap_shape_is_what_we_expect(client):
    boot = await client.bootstrap()
    for key in ("elements", "events", "teams", "element_types", "chips", "game_config"):
        assert key in boot, f"bootstrap-static lost `{key}` — parsing will break"
    assert len(boot["events"]) == 38
    assert len(boot["teams"]) == 20
    assert len(boot["element_types"]) == 4

    players = parse_players(boot)
    assert len(players) > 400
    teams = parse_teams(boot)
    assert all(t.short_name for t in teams.values())


async def test_chips_still_come_in_two_halves(client):
    """2026/27 resets chips at GW20. If this ever goes back to four chips the
    /chips command needs revisiting."""
    boot = await client.bootstrap()
    names = [c["name"] for c in boot["chips"]]
    assert len(boot["chips"]) >= 4
    if len(boot["chips"]) == 8:
        assert names.count("wildcard") == 2


async def test_no_auth_required_for_anything_we_use(client):
    boot = await client.bootstrap()
    state = parse_game_state(boot)
    event = state.current_event or 1
    await client.event_status()
    await client.fixtures(event)
    await client.live(event)
    await client.classic_standings(OVERALL_LEAGUE)
    await client.dream_team(event)


async def test_computed_live_points_match_the_api(client):
    """The load-bearing assertion of the whole project.

    We compute each manager's gameweek score from picks × live points and check
    it against FPL's own `event_total`. If this drifts, every table the bot
    posts is wrong.

    Two caveats baked into the assertions:

    * standings lag by hours, so we only check managers with nothing left to
      play;
    * FPL applies auto-subs only after *every* fixture in the gameweek has
      finished, so we disable our own prediction for the equality check and
      verify separately that predicting can only ever add points.
    """
    boot = await client.bootstrap()
    state = parse_game_state(boot)
    event = state.current_event
    if event is None:
        pytest.skip("no current gameweek")

    players = parse_players(boot)
    live = parse_live(await client.live(event))
    fixtures = parse_fixtures(await client.fixtures(event))
    team_state = fixture_state_by_team(fixtures)
    team_done = {
        t: s not in (FixtureState.UPCOMING, FixtureState.LIVE) for t, s in team_state.items()
    }

    standings = await client.classic_standings(OVERALL_LEAGUE)
    entries = standings["standings"]["results"][:10]
    picks = await client.many_picks([e["entry"] for e in entries], event)

    checked = 0
    for entry in entries:
        raw = picks.get(entry["entry"])
        if raw is None:
            continue
        kwargs = dict(
            entry_id=entry["entry"],
            manager_name=entry.get("player_name", ""),
            team_name=entry.get("entry_name", ""),
            raw_picks=raw,
            live=live,
            players=players,
            team_state=team_state,
            team_done=team_done,
        )
        exact = build_manager_row(**kwargs, predict_subs=False)
        if exact.remaining:
            continue    # still in play — the standings snapshot can't match

        assert exact.gw_points == entry["event_total"], (
            f"{entry['entry_name']}: computed {exact.gw_points}, "
            f"API says {entry['event_total']}"
        )

        projected = build_manager_row(**kwargs, predict_subs=True)
        assert projected.gw_points >= exact.gw_points, (
            "predicting an auto-sub should never lose points"
        )
        checked += 1

    assert checked > 0, "no settled managers to verify against"


async def test_picks_are_hidden_before_the_deadline(client):
    """Confirms the bot can never leak someone's unlocked team."""
    from fplbot.fpl.errors import NotFound

    boot = await client.bootstrap()
    state = parse_game_state(boot)
    if state.next_event is None:
        pytest.skip("no upcoming gameweek")
    with pytest.raises(NotFound):
        await client.picks(1000000, state.next_event)


async def test_engine_builds_a_table_end_to_end(client):
    engine = LiveEngine(client)
    event = await engine.resolve_event()
    table = await engine.build_table(OVERALL_LEAGUE, event, limit=20)
    assert table.rows
    assert table.league_name
    ranked = table.ranked()
    assert all(
        ranked[i].live_total >= ranked[i + 1].live_total for i in range(len(ranked) - 1)
    )
