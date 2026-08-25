"""League side-bets: who owes what, per gameweek and at season end.

Money, so the rules live in code rather than in the database — reviewable in
git, changeable only by a deploy, and never editable from a chat command.

Three things make this safe to run repeatedly:

  * every amount is a pure function of ``gw_result``, which is itself an
    idempotent upsert keyed (league, event, entry). Nothing is appended to, so
    a rerun cannot double-count and an FPL points correction flows straight
    through to every balance downstream.
  * amounts are integer cents, so splitting a tied pot three ways never loses
    a penny to floating point.
  * a scheme is expressed as an award by position *from the top* plus an award
    by position *from the bottom*. Summing the two is exactly zero-sum at any
    league size, and degrades sanely when a league shrinks — in a five-player
    ZonggersNIPS, third place would be both third-from-top and third-from-
    bottom and correctly nets to nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CENTS = 100


@dataclass(frozen=True, slots=True)
class Scheme:
    name: str
    weekly_top: dict[int, int] = field(default_factory=dict)
    weekly_bottom: dict[int, int] = field(default_factory=dict)
    season_top: dict[int, int] = field(default_factory=dict)
    season_bottom: dict[int, int] = field(default_factory=dict)

    def table(self, *, season: bool) -> tuple[dict[int, int], dict[int, int]]:
        if season:
            return self.season_top, self.season_bottom
        return self.weekly_top, self.weekly_bottom


def _c(amount: int) -> int:
    return amount * CENTS


# ── the two live leagues ───────────────────────────────────────────────────
# ZonggersNIPS pays the top three and charges the bottom three, mirrored, so it
# stays balanced however many people are in it. Fourth place in a seven-player
# league is neither, and takes nothing.
ZONGGERSNIPS = Scheme(
    name="ZonggersNIPS",
    weekly_top={1: _c(50), 2: _c(30), 3: _c(20)},
    weekly_bottom={1: _c(-50), 2: _c(-30), 3: _c(-20)},
    season_top={1: _c(400), 2: _c(200), 3: _c(100)},
    season_bottom={1: _c(-400), 2: _c(-200), 3: _c(-100)},
)

# PNANIPILOTS is a flat four-place ladder rather than a mirror, so it is
# expressed from the top only. It sums to zero across exactly four players; a
# fifth would take nothing and the pot would still balance.
PNANIPILOTS = Scheme(
    name="PNANIPILOTS",
    weekly_top={1: _c(40), 2: _c(0), 3: _c(-10), 4: _c(-30)},
    season_top={1: _c(300), 2: _c(0), 3: _c(-100), 4: _c(-200)},
)

SCHEMES: dict[int, Scheme] = {
    166726: ZONGGERSNIPS,
    167008: PNANIPILOTS,
}


def scheme_for(league_id: int) -> Scheme | None:
    return SCHEMES.get(league_id)


def amount_for_position(scheme: Scheme, position: int, players: int, *, season: bool) -> int:
    """Cents awarded for finishing `position` of `players`."""
    top, bottom = scheme.table(season=season)
    return top.get(position, 0) + bottom.get(players - position + 1, 0)


def settle(
    scheme: Scheme,
    scores: list[tuple[int, int]],
    *,
    season: bool = False,
) -> dict[int, int]:
    """Map entry_id -> cents for one gameweek (or the whole season).

    `scores` is [(entry_id, points)]. Tied managers pool the awards for every
    position they jointly occupy and split them evenly; any indivisible
    remainder is handed out in entry_id order so the result is deterministic
    and the total stays exactly zero-sum.
    """
    players = len(scores)
    if not players:
        return {}

    ordered = sorted(scores, key=lambda t: (-t[1], t[0]))
    out: dict[int, int] = {}

    i = 0
    while i < players:
        j = i
        while j + 1 < players and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        tied = ordered[i : j + 1]

        pot = sum(
            amount_for_position(scheme, p, players, season=season)
            for p in range(i + 1, j + 2)
        )
        # Python floors on negative division, so the remainder is always
        # non-negative and adding one cent to the first `rem` shares is exact.
        share, rem = divmod(pot, len(tied))
        for k, (entry_id, _) in enumerate(sorted(tied, key=lambda t: t[0])):
            out[entry_id] = share + (1 if k < rem else 0)
        i = j + 1

    return out


def positions(scores: list[tuple[int, int]]) -> dict[int, int]:
    """entry_id -> finishing position, tied managers sharing the higher one."""
    ordered = sorted(scores, key=lambda t: (-t[1], t[0]))
    out: dict[int, int] = {}
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        for entry_id, _ in ordered[i : j + 1]:
            out[entry_id] = i + 1
        i = j + 1
    return out


def running_balances(weekly: dict[int, dict[int, int]]) -> dict[int, int]:
    """Cumulative cents per entry across {event: {entry_id: cents}}."""
    out: dict[int, int] = {}
    for amounts in weekly.values():
        for entry_id, cents in amounts.items():
            out[entry_id] = out.get(entry_id, 0) + cents
    return out


def transfers(balances: dict[int, int]) -> list[tuple[int, int, int]]:
    """Turn final balances into the fewest payments that clear them.

    Returns [(from_entry, to_entry, cents)]. Greedy largest-debtor against
    largest-creditor, which for a handful of people gives the minimum number of
    transfers in practice and never more than n-1.
    """
    owing = sorted(((e, -c) for e, c in balances.items() if c < 0), key=lambda t: -t[1])
    owed = sorted(((e, c) for e, c in balances.items() if c > 0), key=lambda t: -t[1])

    out: list[tuple[int, int, int]] = []
    i = j = 0
    debts = [list(t) for t in owing]
    credits = [list(t) for t in owed]
    while i < len(debts) and j < len(credits):
        pay = min(debts[i][1], credits[j][1])
        if pay > 0:
            out.append((debts[i][0], credits[j][0], pay))
        debts[i][1] -= pay
        credits[j][1] -= pay
        if debts[i][1] == 0:
            i += 1
        if credits[j][1] == 0:
            j += 1
    return out


def money(cents: int, *, signed: bool = True) -> str:
    """Cents as a human string: 4000 -> '+40', -1050 -> '-10.50'."""
    whole, frac = divmod(abs(cents), CENTS)
    body = f"{whole}" if not frac else f"{whole}.{frac:02d}"
    if not signed:
        return body
    sign = "+" if cents > 0 else ("-" if cents < 0 else "±")
    return f"{sign}{body}"
