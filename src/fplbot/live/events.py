"""Detect what changed between two polls, and turn it into league-relevant news.

Everything here is pure: `diff_live` takes two snapshots and returns events.
`attribute` maps those events onto the managers who own the player. That
separation is what makes the alert engine testable without a Telegram token.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..services.models import LiveTable, PlayerLive

# (stat name, emoji, template, importance) — importance gates alert profiles
WATCHED = [
    ("goals", "⚽", "{player} scores", 3),
    ("assists", "🅰️", "{player} assists", 2),
    ("red_cards", "🟥", "{player} is sent off", 3),
    ("yellow_cards", "🟨", "{player} booked", 1),
    ("saves", None, None, 0),  # tracked for state, never alerted individually
]


@dataclass(frozen=True, slots=True)
class PlayerEvent:
    element: int
    kind: str
    delta: int
    emoji: str
    importance: int
    new_points: int


@dataclass(slots=True)
class LeagueEvent:
    text: str
    key: str
    importance: int
    entries: list[int] = field(default_factory=list)


def diff_live(
    previous: dict[int, dict], current: dict[int, PlayerLive]
) -> list[PlayerEvent]:
    """Compare the last stored snapshot against the current live payload."""
    events: list[PlayerEvent] = []
    for element, now in current.items():
        before = previous.get(element)
        if before is None:
            continue  # first sight of this player — don't retro-alert
        for stat, emoji, _tmpl, importance in WATCHED:
            if emoji is None:
                continue
            delta = getattr(now, stat) - before.get(stat, 0)
            if delta > 0:
                events.append(
                    PlayerEvent(
                        element=element,
                        kind=stat,
                        delta=delta,
                        emoji=emoji,
                        importance=importance,
                        new_points=now.effective_points,
                    )
                )
    return events


def snapshot_of(live: dict[int, PlayerLive]) -> dict[int, dict]:
    return {
        el: {
            "goals": p.goals,
            "assists": p.assists,
            "red_cards": p.red_cards,
            "yellow_cards": p.yellow_cards,
            "minutes": p.minutes,
            "bonus": p.bonus,
            "points": p.points,
        }
        for el, p in live.items()
    }


def attribute(
    events: list[PlayerEvent],
    table: LiveTable,
    player_names: dict[int, str],
) -> list[LeagueEvent]:
    """Turn raw player events into messages that name the managers affected.

    A goal by someone nobody owns is not news in a friend league, so it's
    dropped. A goal by someone eight of ten own is barely news either — but it
    still gets posted, because the two who don't own him want to know.
    """
    owners: dict[int, list[tuple[str, bool]]] = {}
    for row in table.rows:
        for pick in row.picks:
            if pick.effective_multiplier == 0:
                continue
            owners.setdefault(pick.element, []).append((row.team_name, pick.is_captain))

    out: list[LeagueEvent] = []
    for ev in events:
        holders = owners.get(ev.element)
        if not holders:
            continue
        name = player_names.get(ev.element, str(ev.element))
        names = [n for n, _ in holders]
        caps = [n for n, c in holders if c]
        who = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
        verb = {
            "goals": "scores",
            "assists": "assists",
            "red_cards": "is sent off",
            "yellow_cards": "is booked",
        }[ev.kind]
        text = f"{ev.emoji} <b>{name}</b> {verb} — {who}"
        if caps:
            text += f"\n   ©️ captained by {', '.join(caps)}"
        out.append(
            LeagueEvent(
                text=text,
                # Keyed by resulting *state*, not by when we noticed, so a
                # restart mid-gameweek can't replay it.
                key=f"gw{table.event}:{ev.kind}:{ev.element}:{ev.delta}:{ev.new_points}",
                importance=ev.importance,
                entries=[r.entry_id for r in table.rows
                         if any(p.element == ev.element and p.effective_multiplier
                                for p in r.picks)],
            )
        )
    return out


def detect_lead_change(
    table: LiveTable, previous_leader: int | None
) -> LeagueEvent | None:
    ranked = table.ranked()
    if not ranked:
        return None
    leader = ranked[0]
    if previous_leader is None or leader.entry_id == previous_leader:
        return None
    margin = leader.live_total - (ranked[1].live_total if len(ranked) > 1 else 0)
    return LeagueEvent(
        text=f"👑 <b>{leader.team_name}</b> takes the lead, +{margin}",
        key=f"gw{table.event}:lead:{leader.entry_id}:{leader.live_total}",
        importance=3,
    )
