"""Detect what changed between two polls, and turn it into league-relevant news.

Everything here is pure: `diff_live` takes two snapshots and returns events.
`attribute` maps those events onto the managers who own the player. That
separation is what makes the alert engine testable without a Telegram token.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from ..services.models import LiveTable, ManagerLive, PlayerLive

# (stat name, emoji, template, importance) — importance gates alert profiles
WATCHED = [
    ("goals", "⚽", "{player} scores", 3),
    ("assists", "🅰️", "{player} assists", 2),
    ("red_cards", "🟥", "{player} is sent off", 3),
    ("yellow_cards", "🟨", "{player} booked", 1),
    ("saves", None, None, 0),  # tracked for state, never alerted individually
]

VERB = {
    "goals": "scores",
    "assists": "assists",
    "red_cards": "is sent off",
    "yellow_cards": "is booked",
}

# Provisional bonus swings a point or two and flips the lead back and forth all
# evening. Below this margin an unconfirmed lead change is noise, not news.
PROVISIONAL_LEAD_MARGIN = 3


def _esc(text: str) -> str:
    """Team names are user-chosen and land inside HTML — an unescaped & or <
    makes Telegram reject the whole message, so the alert silently never
    arrives."""
    return html.escape(str(text), quote=False)


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
        names = [_esc(n) for n, _ in holders]
        caps = [_esc(n) for n, c in holders if c]
        who = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
        verb = VERB[ev.kind]
        text = f"{ev.emoji} <b>{_esc(name)}</b> {verb} — now {ev.new_points} pts\n   {who}"
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


def _lead_cause(
    leader: ManagerLive,
    events: list[PlayerEvent],
    player_names: dict[int, str],
) -> str:
    """What the new leader's players just did, if anything did."""
    owned = {p.element for p in leader.picks if p.effective_multiplier}
    bits = [
        f"{ev.emoji} {_esc(player_names.get(ev.element, str(ev.element)))} {VERB[ev.kind]}"
        for ev in events
        if ev.element in owned and ev.kind in VERB
    ]
    return " · ".join(dict.fromkeys(bits))   # de-duplicated, order preserved


def detect_lead_change(
    table: LiveTable,
    previous_leader: int | None,
    events: list[PlayerEvent] | None = None,
    player_names: dict[int, str] | None = None,
) -> LeagueEvent | None:
    """Announce a new leader, and say why.

    "takes the lead, +1" told nobody anything: it named no league, no
    gameweek, no displaced rival, and gave no cause — and +1 read as a point
    gained rather than the margin. Worse, provisional bonus flipped it back
    and forth every few minutes with nothing visible to explain it.
    """
    ranked = table.ranked()
    if not ranked:
        return None
    leader = ranked[0]
    if previous_leader is None or leader.entry_id == previous_leader:
        return None

    runner = ranked[1] if len(ranked) > 1 else None
    margin = leader.live_total - (runner.live_total if runner else 0)
    if not table.bonus_confirmed and margin < PROVISIONAL_LEAD_MARGIN:
        return None

    who = f"<b>{_esc(leader.team_name)}</b>"
    if leader.manager_name and leader.manager_name != "?":
        who += f" ({_esc(leader.manager_name)})"

    lines = [
        f"👑 <b>New leader</b> · {_esc(table.league_name)} · GW{table.event}",
        f"{who} — {leader.live_total} pts",
    ]
    if runner:
        ahead = "level with" if not margin else f"{margin} ahead of"
        lines.append(f"Now {ahead} <b>{_esc(runner.team_name)}</b>")
    if cause := _lead_cause(leader, events or [], player_names or {}):
        lines.append(cause)
    elif not table.bonus_confirmed:
        lines.append("↑ bonus points shifted")

    return LeagueEvent(
        text="\n".join(lines),
        key=f"gw{table.event}:lead:{leader.entry_id}:{leader.live_total}",
        importance=3,
    )
