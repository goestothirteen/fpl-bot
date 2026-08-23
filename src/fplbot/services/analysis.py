"""Everything derived from a LiveTable: differentials, effective ownership,
captains, benches, template, awards. All pure — no I/O, no Telegram."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .models import LiveTable, ManagerLive, Player, PlayerLive


@dataclass(frozen=True, slots=True)
class Ownership:
    element: int
    owners: list[str]          # team names
    captains: list[str]
    count: int
    total_managers: int

    @property
    def percent(self) -> float:
        return 100 * self.count / self.total_managers if self.total_managers else 0.0

    @property
    def effective_ownership(self) -> float:
        """Ownership + captaincy — the number that decides whether a haul helps
        or hurts you relative to the league."""
        if not self.total_managers:
            return 0.0
        return 100 * (self.count + len(self.captains)) / self.total_managers


def ownership(table: LiveTable, *, starters_only: bool = True) -> dict[int, Ownership]:
    owners: dict[int, list[str]] = defaultdict(list)
    caps: dict[int, list[str]] = defaultdict(list)
    n = len([r for r in table.rows if r.picks])
    for row in table.rows:
        for pick in row.picks:
            if starters_only and pick.effective_multiplier == 0:
                continue
            owners[pick.element].append(row.team_name)
            if pick.is_captain:
                caps[pick.element].append(row.team_name)
    return {
        el: Ownership(el, names, caps.get(el, []), len(names), n)
        for el, names in owners.items()
    }


def differentials(
    table: LiveTable, live: dict[int, PlayerLive], *, max_owners: int = 2
) -> list[tuple[Ownership, int]]:
    """League-wide differentials: rarely-owned players, sorted by live points."""
    own = ownership(table)
    out = [
        (o, live[el].effective_points if el in live else 0)
        for el, o in own.items()
        if o.count <= max_owners
    ]
    return sorted(out, key=lambda t: (-t[1], t[0].count))


def head_to_head(
    a: ManagerLive, b: ManagerLive, live: dict[int, PlayerLive]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
    """Players unique to A, players unique to B, and the net swing so far.

    Swing accounts for captaincy: a shared player captained by only one of them
    still counts as a differential worth their base score.
    """
    ma = {p.element: p.effective_multiplier for p in a.picks}
    mb = {p.element: p.effective_multiplier for p in b.picks}
    only_a: list[tuple[int, int]] = []
    only_b: list[tuple[int, int]] = []
    swing = 0
    for el in set(ma) | set(mb):
        pts = live[el].effective_points if el in live else 0
        delta = pts * (ma.get(el, 0) - mb.get(el, 0))
        swing += delta
        if delta > 0 or (el in ma and el not in mb):
            only_a.append((el, delta))
        elif delta < 0 or (el in mb and el not in ma):
            only_b.append((el, delta))
    only_a.sort(key=lambda t: -t[1])
    only_b.sort(key=lambda t: t[1])
    return only_a, only_b, swing


def captain_spread(table: LiveTable, live: dict[int, PlayerLive]) -> list[tuple[int, list[str], int]]:
    """(element, team names captaining them, live captain return)."""
    by_cap: dict[int, list[str]] = defaultdict(list)
    for row in table.rows:
        if row.captain_element:
            by_cap[row.captain_element].append(row.team_name)
    out = [
        (el, names, (live[el].effective_points if el in live else 0) * 2)
        for el, names in by_cap.items()
    ]
    return sorted(out, key=lambda t: (-len(t[1]), -t[2]))


def bench_disasters(table: LiveTable) -> list[ManagerLive]:
    return sorted((r for r in table.rows if r.picks), key=lambda r: -r.bench_points)


def template(table: LiveTable, threshold: float = 50.0) -> tuple[list[int], list[tuple[str, int]]]:
    """Players owned by more than ``threshold``% of the league, plus each
    manager's overlap with that template (lower = braver)."""
    own = ownership(table)
    core = [el for el, o in own.items() if o.percent > threshold]
    core_set = set(core)
    overlap = [
        (r.team_name, sum(1 for p in r.picks if p.effective_multiplier and p.element in core_set))
        for r in table.rows
        if r.picks
    ]
    overlap.sort(key=lambda t: t[1])
    return core, overlap


def rank_movement(table: LiveTable) -> list[tuple[ManagerLive, int]]:
    """Live rank versus rank at the start of the gameweek."""
    before = sorted(table.rows, key=lambda r: -r.season_total_before)
    prior = {r.entry_id: i + 1 for i, r in enumerate(before)}
    now = table.ranked()
    return [(r, prior.get(r.entry_id, i + 1) - (i + 1)) for i, r in enumerate(now)]


@dataclass(frozen=True, slots=True)
class Award:
    title: str
    winner: str
    detail: str


def awards(table: LiveTable, players: dict[int, Player], live: dict[int, PlayerLive]) -> list[Award]:
    rows = [r for r in table.rows if r.picks]
    if not rows:
        return []
    out: list[Award] = []

    best = max(rows, key=lambda r: r.net_points)
    out.append(Award("🏆 Manager of the Week", best.team_name, f"{best.net_points} pts"))

    worst = min(rows, key=lambda r: r.net_points)
    out.append(Award("💩 Wooden Spoon", worst.team_name, f"{worst.net_points} pts"))

    bench = max(rows, key=lambda r: r.bench_points)
    if bench.bench_points > 0:
        out.append(
            Award("🪑 Bench Warmer", bench.team_name, f"{bench.bench_points} pts left behind")
        )

    caps = [r for r in rows if r.captain_element]
    if caps:
        flop = min(caps, key=lambda r: r.captain_points)
        name = players[flop.captain_element].web_name if flop.captain_element in players else "?"
        out.append(Award("©️ Captain Disaster", flop.team_name, f"{name}, {flop.captain_points} pts"))

        hero = max(caps, key=lambda r: r.captain_points)
        hname = players[hero.captain_element].web_name if hero.captain_element in players else "?"
        out.append(Award("🎯 Armband Genius", hero.team_name, f"{hname}, {hero.captain_points} pts"))

    diffs = differentials(table, live, max_owners=1)
    if diffs and diffs[0][1] > 0:
        o, pts = diffs[0]
        name = players[o.element].web_name if o.element in players else "?"
        out.append(Award("🔮 Best Differential", o.owners[0], f"{name}, {pts} pts, owned by 1"))

    hits = [r for r in rows if r.transfer_cost > 0]
    if hits:
        biggest = max(hits, key=lambda r: r.transfer_cost)
        out.append(
            Award("🔨 Biggest Gambler", biggest.team_name, f"-{biggest.transfer_cost} on hits")
        )
    return out


def chip_availability(chips_meta: list[dict], used: list[dict], current_event: int) -> dict[str, bool]:
    """2026/27 resets chips at GW20 — each chip exists twice in bootstrap.chips,
    once for events 1-19 and once for 20-38. Report only the current half."""
    half_start = 20 if current_event >= 20 else 1
    half_end = 38 if current_event >= 20 else 19
    available: dict[str, bool] = {}
    for chip in chips_meta:
        if chip.get("start_event", 1) >= half_start and chip.get("stop_event", 38) <= half_end:
            available[chip["name"]] = True
    for entry in used:
        gw = entry.get("event", 0)
        if half_start <= gw <= half_end:
            available[entry.get("name", "")] = False
    return available


def most_common_captain(table: LiveTable) -> int | None:
    counts = Counter(r.captain_element for r in table.rows if r.captain_element)
    return counts.most_common(1)[0][0] if counts else None
