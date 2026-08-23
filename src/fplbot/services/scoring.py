"""Provisional bonus and auto-sub prediction — the two things FPL doesn't give
you live, and the two things that make a live table trustworthy.

Both are pure functions. See tests/test_scoring.py.
"""
from __future__ import annotations

from collections import defaultdict

from .models import Fixture, FixtureState, Pick, Player, PlayerLive

# Squad legality, from bootstrap-static.element_types
FORMATION_MIN = {1: 1, 2: 3, 3: 2, 4: 1}
FORMATION_MAX = {1: 1, 2: 5, 3: 5, 4: 3}


def provisional_bonus(fixture: Fixture) -> dict[int, int]:
    """3/2/1 to the top three BPS scorers, ties sharing the higher award.

    FPL's real rule: 2 tied on top → both get 3, next gets 1. 3 tied on top →
    all get 3, no 2s or 1s. 1st alone, 2 tied 2nd → 3, 2, 2.
    """
    if not fixture.bps or fixture.state is not FixtureState.LIVE:
        return {}

    by_score: dict[int, list[int]] = defaultdict(list)
    for element, score in fixture.bps.items():
        if score > 0:
            by_score[score].append(element)

    awards = [3, 2, 1]
    out: dict[int, int] = {}
    idx = 0
    for score in sorted(by_score, reverse=True):
        if idx >= len(awards):
            break
        group = by_score[score]
        value = awards[idx]
        for element in group:
            out[element] = value
        idx += len(group)
    return out


def apply_provisional_bonus(
    live: dict[int, PlayerLive], fixtures: list[Fixture]
) -> bool:
    """Mutates ``live`` in place. Returns True if any provisional bonus was added.

    Only in-play fixtures get provisional bonus — once a fixture is
    ``finished_provisional`` the API's own ``stats.bonus`` is already populated
    (verified in GW1 2026/27), so adding ours on top would double-count.
    """
    added = False
    for fixture in fixtures:
        for element, value in provisional_bonus(fixture).items():
            pl = live.get(element)
            if pl is not None and pl.bonus == 0:
                pl.provisional_bonus = value
                added = True
    return added


def _positions(picks: list[Pick], players: dict[int, Player]) -> dict[int, int]:
    return {p.element: players[p.element].element_type for p in picks if p.element in players}


def predict_auto_subs(
    picks: list[Pick],
    live: dict[int, PlayerLive],
    players: dict[int, Player],
    team_fixture_done: dict[int, bool],
) -> list[tuple[int, int]]:
    """Predict FPL's automatic substitutions mid-gameweek.

    FPL only publishes ``automatic_subs`` once every one of a manager's players
    has finished. Until then we replicate the rule ourselves:

    * a starter is a "blank" if their team's fixture is done and they played 0'
    * bench players are considered in position order 12, 13, 14, 15
    * a bench player is only eligible if their own fixture is done and they
      played > 0'
    * the resulting XI must stay legal (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD)
    * the bench GK can only replace the starting GK

    Returns a list of ``(element_out, element_in)``.
    """
    types = _positions(picks, players)
    starters = [p for p in picks if p.position <= 11]
    bench = sorted((p for p in picks if p.position >= 12), key=lambda p: p.position)

    def done(element: int) -> bool:
        player = players.get(element)
        return bool(player and team_fixture_done.get(player.team, False))

    def played(element: int) -> bool:
        pl = live.get(element)
        return bool(pl and pl.minutes > 0)

    blanks = [p for p in starters if done(p.element) and not played(p.element)]
    if not blanks:
        return []

    counts: dict[int, int] = defaultdict(int)
    for p in starters:
        counts[types.get(p.element, 0)] += 1

    used_bench: set[int] = set()
    subs: list[tuple[int, int]] = []

    for blank in blanks:
        out_type = types.get(blank.element, 0)
        for cand in bench:
            if cand.element in used_bench:
                continue
            if not done(cand.element) or not played(cand.element):
                continue
            in_type = types.get(cand.element, 0)
            # goalkeeper swaps are 1-for-1 only
            if (out_type == 1) != (in_type == 1):
                continue
            trial = dict(counts)
            trial[out_type] -= 1
            trial[in_type] += 1
            if all(
                FORMATION_MIN[t] <= trial.get(t, 0) <= FORMATION_MAX[t]
                for t in (1, 2, 3, 4)
            ):
                counts = trial
                used_bench.add(cand.element)
                subs.append((blank.element, cand.element))
                break
    return subs
