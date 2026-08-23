"""The live table calculation itself."""
from __future__ import annotations

from conftest import fixture, live

from fplbot.services.live import build_manager_row, fixture_state_by_team
from fplbot.services.models import FixtureState


def raw_picks(picks, chip=None, transfers_cost=0, total=100, points=40, subs=None):  # noqa: ANN001
    return {
        "active_chip": chip,
        "automatic_subs": subs or [],
        "entry_history": {
            "points": points, "total_points": total,
            "event_transfers_cost": transfers_cost,
        },
        "picks": [
            {
                "element": p.element, "position": p.position, "multiplier": p.multiplier,
                "is_captain": p.is_captain, "is_vice_captain": p.is_vice_captain,
            }
            for p in picks
        ],
    }


class TestFixtureStateByTeam:
    def test_double_gameweek_takes_the_least_advanced_state(self):
        fixtures = [
            fixture(1, 1, 2, started=True, provisional=True),
            fixture(2, 1, 3, started=False, provisional=False),
        ]
        state = fixture_state_by_team(fixtures)
        # Team 1 has one done and one to come — they are not finished.
        assert state[1] is FixtureState.UPCOMING
        assert state[2] is FixtureState.FINISHED


class TestManagerRow:
    def _row(self, squad, players, *, chip=None, live_map=None, states=None, **kw):  # noqa: ANN001
        lv = live_map or {i: live(i, points=2) for i in range(1, 16)}
        st = states or dict.fromkeys(range(1, 16), FixtureState.FINISHED)
        return build_manager_row(
            entry_id=1, manager_name="M", team_name="T",
            raw_picks=raw_picks(squad, chip=chip, **kw),
            live=lv, players=players, team_state=st,
            team_done={t: True for t in st},
        )

    def test_captain_doubles_and_bench_excluded(self, squad, players):
        row = self._row(squad, players)
        # 11 starters × 2 points, captain (element 10) counted twice = 24
        assert row.gw_points == 24
        assert row.bench_points == 8          # 4 bench × 2
        assert row.captain_element == 10
        assert row.captain_points == 4

    def test_transfer_hit_subtracted_from_net_only(self, squad, players):
        row = self._row(squad, players, transfers_cost=8)
        assert row.gw_points == 24
        assert row.net_points == 16

    def test_bench_boost_counts_the_bench(self, squad, players):
        boosted = [
            type(p)(element=p.element, position=p.position,
                    multiplier=1 if p.multiplier == 0 else p.multiplier,
                    is_captain=p.is_captain, is_vice_captain=p.is_vice_captain)
            for p in squad
        ]
        row = self._row(boosted, players, chip="bboost")
        assert row.gw_points == 32            # 15 players + captain again
        assert row.bench_points == 0

    def test_triple_captain(self, squad, players):
        tripled = [
            type(p)(element=p.element, position=p.position,
                    multiplier=3 if p.is_captain else p.multiplier,
                    is_captain=p.is_captain, is_vice_captain=p.is_vice_captain)
            for p in squad
        ]
        row = self._row(tripled, players, chip="3xc")
        assert row.gw_points == 26            # 24 + one more captain multiple
        assert row.captain_points == 6

    def test_players_left_to_play_counts_only_starters(self, squad, players):
        states = dict.fromkeys(range(1, 16), FixtureState.FINISHED)
        states[3] = FixtureState.UPCOMING     # a starting defender
        states[5] = FixtureState.LIVE
        states[12] = FixtureState.UPCOMING    # bench GK — must not count
        row = self._row(squad, players, states=states)
        assert row.to_play == 1
        assert row.in_play == 1
        assert row.played == 9
        assert row.remaining == 2

    def test_season_total_excludes_the_current_gameweek(self, squad, players):
        row = self._row(squad, players, total=100, points=40)
        assert row.season_total_before == 60
        assert row.live_total == 60 + row.net_points

    def test_predicted_sub_swaps_the_points(self, squad, players):
        lv = {i: live(i, points=2) for i in range(1, 16)}
        lv[9] = live(9, minutes=0, points=0)     # starting MID blanked
        lv[13] = live(13, minutes=90, points=7)  # bench DEF hauled
        row = self._row(squad, players, live_map=lv)
        # 10 remaining starters (20) + captain again (2) + subbed-in 7 = 29
        assert row.gw_points == 29
        assert (9, 13) in row.predicted_subs

    def test_real_auto_subs_from_the_api_are_respected(self, squad, players):
        lv = {i: live(i, points=2) for i in range(1, 16)}
        lv[9] = live(9, minutes=0, points=0)
        lv[14] = live(14, minutes=90, points=9)
        row = build_manager_row(
            entry_id=1, manager_name="M", team_name="T",
            raw_picks=raw_picks(
                squad, subs=[{"element_out": 9, "element_in": 14, "event": 1}]
            ),
            live=lv, players=players,
            team_state=dict.fromkeys(range(1, 16), FixtureState.FINISHED),
            team_done=dict.fromkeys(range(1, 16), True),
        )
        assert row.gw_points == 31            # 20 + 2 captain + 9


class TestPollCadence:
    """The poller must not sleep through a kickoff."""

    def _engine(self):
        from fplbot.services.live import LiveEngine
        return LiveEngine(client=None)  # poll_interval touches no I/O

    def test_live_uses_the_base_interval(self):
        from fplbot.services.live import GamePhase, PhaseInfo
        info = PhaseInfo(GamePhase.LIVE, 4, None)
        assert self._engine().poll_interval(info, base=45) == 45

    def test_dormant_wakes_two_minutes_before_kickoff(self):
        from datetime import UTC, datetime, timedelta

        from fplbot.services.live import GamePhase, PhaseInfo
        ko = datetime.now(UTC) + timedelta(minutes=12)
        info = PhaseInfo(GamePhase.DORMANT, 4, ko)
        interval = self._engine().poll_interval(info)
        assert 590 <= interval <= 610          # ~10 minutes

    def test_dormant_far_from_kickoff_is_capped(self):
        from datetime import UTC, datetime, timedelta

        from fplbot.services.live import GamePhase, PhaseInfo
        info = PhaseInfo(GamePhase.DORMANT, 4, datetime.now(UTC) + timedelta(days=3))
        assert self._engine().poll_interval(info) == 1800

    def test_dormant_never_busy_loops(self):
        from datetime import UTC, datetime, timedelta

        from fplbot.services.live import GamePhase, PhaseInfo
        info = PhaseInfo(GamePhase.DORMANT, 4, datetime.now(UTC) + timedelta(seconds=5))
        assert self._engine().poll_interval(info) == 60

    def test_dormant_with_no_fixtures_at_all(self):
        from fplbot.services.live import GamePhase, PhaseInfo
        assert self._engine().poll_interval(PhaseInfo(GamePhase.DORMANT, None, None)) == 1800
