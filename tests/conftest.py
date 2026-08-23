from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fplbot.services.models import Fixture, Pick, Player, PlayerLive


def player(pid: int, team: int, etype: int, name: str = "P") -> Player:
    return Player(
        id=pid, web_name=f"{name}{pid}", team=team, element_type=etype, now_cost=50,
        status="a", news="", selected_by_percent=10.0, form=3.0, total_points=20,
        chance_of_playing=None,
    )


def live(pid: int, minutes: int = 90, points: int = 2, bps: int = 0) -> PlayerLive:
    return PlayerLive(element=pid, minutes=minutes, points=points, bps=bps)


def fixture(
    fid: int, h: int, a: int, *, started: bool = True, provisional: bool = True,
    finished: bool = False, bps: dict[int, int] | None = None,
) -> Fixture:
    return Fixture(
        id=fid, event=1, team_h=h, team_a=a, team_h_score=1, team_a_score=0,
        kickoff=datetime(2026, 8, 22, 14, 0, tzinfo=UTC), minutes=90,
        started=started, finished=finished, finished_provisional=provisional,
        bps=bps or {},
    )


def pick(element: int, position: int, multiplier: int, captain: bool = False) -> Pick:
    return Pick(
        element=element, position=position, multiplier=multiplier,
        is_captain=captain, is_vice_captain=False,
    )


@pytest.fixture
def squad() -> list[Pick]:
    """1 GK, 4 DEF, 4 MID, 2 FWD on the pitch; GK + DEF + MID + FWD on the bench."""
    picks = [pick(1, 1, 1)]                                  # GK
    picks += [pick(i, i, 1) for i in range(2, 6)]            # DEF 2-5
    picks += [pick(i, i, 1) for i in range(6, 10)]           # MID 6-9
    picks += [pick(10, 10, 2, captain=True), pick(11, 11, 1)]  # FWD 10-11
    picks += [pick(12, 12, 0), pick(13, 13, 0), pick(14, 14, 0), pick(15, 15, 0)]
    return picks


@pytest.fixture
def players() -> dict[int, Player]:
    types = {1: 1, 2: 2, 3: 2, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3, 9: 3, 10: 4, 11: 4,
             12: 1, 13: 2, 14: 3, 15: 4}
    return {pid: player(pid, team=pid, etype=t) for pid, t in types.items()}


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration", action="store_true",
        help="run the tests that call the real FPL API",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="needs --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
