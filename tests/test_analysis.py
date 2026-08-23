from __future__ import annotations

from conftest import live, pick

from fplbot.services import analysis
from fplbot.services.models import GamePhase, LiveTable, ManagerLive


def manager(eid: int, name: str, elements: list[int], captain: int, total: int = 0) -> ManagerLive:
    picks = [pick(e, i + 1, 2 if e == captain else 1, captain=e == captain)
             for i, e in enumerate(elements[:11])]
    picks += [pick(e, 12 + i, 0) for i, e in enumerate(elements[11:])]
    return ManagerLive(
        entry_id=eid, manager_name=name, team_name=name, picks=picks,
        active_chip=None, transfer_cost=0, season_total_before=total,
        captain_element=captain,
    )


def table(rows: list[ManagerLive]) -> LiveTable:
    return LiveTable(event=1, league_id=1, league_name="Test", rows=rows, phase=GamePhase.LIVE)


def test_ownership_and_effective_ownership():
    t = table([
        manager(1, "A", list(range(1, 12)), captain=5),
        manager(2, "B", list(range(1, 12)), captain=5),
        manager(3, "C", list(range(5, 16)), captain=9),
    ])
    own = analysis.ownership(t)
    assert own[5].count == 3
    assert own[5].percent == 100.0
    # owned by 3 of 3, captained by 2 → 5/3 = 166%
    assert round(own[5].effective_ownership) == 167
    assert own[1].count == 2


def test_differentials_are_rare_and_sorted_by_points():
    t = table([
        manager(1, "A", list(range(1, 12)), captain=1),
        manager(2, "B", list(range(1, 12)), captain=1),
        manager(3, "C", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 99], captain=1),
    ])
    lv = {99: live(99, points=13), 11: live(11, points=2)}
    diffs = analysis.differentials(t, lv, max_owners=1)
    assert diffs[0][0].element == 99
    assert diffs[0][1] == 13
    assert diffs[0][0].owners == ["C"]


def test_head_to_head_swing():
    a = manager(1, "A", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], captain=1)
    b = manager(2, "B", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 99], captain=1)
    lv = {11: live(11, points=9), 99: live(99, points=2)}
    only_a, only_b, swing = analysis.head_to_head(a, b, lv)
    assert swing == 7
    assert only_a[0][0] == 11
    assert only_b[0][0] == 99


def test_head_to_head_counts_a_differential_captain():
    a = manager(1, "A", list(range(1, 12)), captain=3)
    b = manager(2, "B", list(range(1, 12)), captain=7)
    lv = {3: live(3, points=12), 7: live(7, points=2)}
    _a, _b, swing = analysis.head_to_head(a, b, lv)
    assert swing == 10          # 12 extra for A's captain, 2 extra for B's


def test_template_and_bravery():
    t = table([
        manager(1, "A", list(range(1, 12)), captain=1),
        manager(2, "B", list(range(1, 12)), captain=1),
        manager(3, "C", list(range(20, 31)), captain=20),
    ])
    core, overlap = analysis.template(t, threshold=50)
    assert set(core) == set(range(1, 12))
    assert overlap[0] == ("C", 0)          # bravest first


def test_chip_availability_respects_the_gw20_reset():
    chips_meta = [
        {"name": "wildcard", "start_event": 2, "stop_event": 19},
        {"name": "wildcard", "start_event": 20, "stop_event": 38},
        {"name": "bboost", "start_event": 1, "stop_event": 19},
        {"name": "bboost", "start_event": 20, "stop_event": 38},
    ]
    used = [{"name": "wildcard", "event": 8}, {"name": "bboost", "event": 12}]

    first_half = analysis.chip_availability(chips_meta, used, current_event=10)
    assert first_half == {"wildcard": False, "bboost": False}

    # After the reset both are back, despite having been used in the first half.
    second_half = analysis.chip_availability(chips_meta, used, current_event=25)
    assert second_half == {"wildcard": True, "bboost": True}


def test_awards_pick_the_right_villains():
    rows = [
        manager(1, "A", list(range(1, 12)), captain=1),
        manager(2, "B", list(range(1, 12)), captain=2),
    ]
    rows[0].gw_points, rows[0].bench_points, rows[0].captain_points = 80, 2, 24
    rows[1].gw_points, rows[1].bench_points, rows[1].captain_points = 30, 21, 0
    t = table(rows)
    result = {a.title: a.winner for a in analysis.awards(t, {}, {})}
    assert result["🏆 Manager of the Week"] == "A"
    assert result["💩 Wooden Spoon"] == "B"
    assert result["🪑 Bench Warmer"] == "B"
    assert result["©️ Captain Disaster"] == "B"
