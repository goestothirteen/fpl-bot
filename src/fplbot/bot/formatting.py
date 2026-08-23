"""Rendering domain objects into Telegram HTML.

Rules of thumb learned the hard way:
  * <pre> monospace blocks are the only way to get columns to line up on mobile
  * keep tables under ~38 characters wide or Telegram wraps them on a phone
  * truncate team names hard; nobody's is meaningfully unique past 14 chars
"""
from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..services.analysis import Award, Ownership
from ..services.models import GamePhase, LiveTable, ManagerLive, Player, PlayerLive

CHIP_LABEL = {
    "3xc": "TC",
    "bboost": "BB",
    "freehit": "FH",
    "wildcard": "WC",
    "manager": "AM",
}


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def clip(text: str, width: int) -> str:
    text = text.strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _age_footer(table: LiveTable) -> str:
    bits = []
    if table.data_age_seconds > 90:
        bits.append(f"data {int(table.data_age_seconds // 60)}m old")
    if not table.bonus_confirmed:
        bits.append("bonus provisional")
    if table.phase is GamePhase.LIVE:
        bits.append("live")
    return " · ".join(bits)


def render_live_table(table: LiveTable, limit: int = 25) -> str:
    rows = table.ranked()[:limit]
    lines = [f"{'#':>2} {'Team':<14} {'GW':>4} {'Tot':>5} {'⏳':>3}"]
    lines.append("─" * 32)
    for i, r in enumerate(rows, 1):
        chip = CHIP_LABEL.get(r.active_chip or "", "")
        name = clip(r.team_name, 14 - len(chip) - (1 if chip else 0))
        if chip:
            name = f"{name} {chip}"
        gw = f"{r.net_points}"
        if r.transfer_cost:
            gw = f"{r.net_points}*"
        if r.predicted_subs:
            gw = f"{gw}↑"
        lines.append(f"{i:>2} {name:<14} {gw:>4} {r.live_total:>5} {r.remaining:>3}")

    body = "\n".join(lines)
    header = f"<b>{esc(table.league_name)}</b> · GW{table.event}"
    footer = _age_footer(table)
    out = f"{header}\n<pre>{esc(body)}</pre>"
    notes = []
    if any(r.transfer_cost for r in rows):
        notes.append("* after hit")
    if any(r.predicted_subs for r in rows):
        notes.append("↑ auto-sub projected")
    if notes:
        footer = " · ".join(notes + ([footer] if footer else []))
    if footer:
        out += f"\n<i>{esc(footer)}</i>"
    return out


def render_remaining(table: LiveTable, limit: int = 25) -> str:
    rows = sorted(table.ranked()[:limit], key=lambda r: (-r.remaining, -r.live_total))
    lines = [f"{'Team':<14} {'▶':>3} {'⏳':>3} {'✓':>3}", "─" * 26]
    for r in rows:
        lines.append(f"{clip(r.team_name, 14):<14} {r.in_play:>3} {r.to_play:>3} {r.played:>3}")
    return (
        f"<b>Players left</b> · GW{table.event}\n"
        f"<pre>{esc(chr(10).join(lines))}</pre>\n"
        f"<i>▶ on the pitch · ⏳ yet to kick off · ✓ done</i>"
    )


def render_differentials(
    diffs: list[tuple[Ownership, int]], players: dict[int, Player], limit: int = 12
) -> str:
    if not diffs:
        return "Nobody in this league owns a differential right now — you're all the same person."
    lines = []
    for own, pts in diffs[:limit]:
        name = players[own.element].web_name if own.element in players else str(own.element)
        owners = ", ".join(clip(o, 12) for o in own.owners[:3])
        cap = " (C)" if own.captains else ""
        lines.append(f"{pts:>3} {clip(name, 12):<12} {owners}{cap}")
    return (
        "<b>Differentials</b> — owned by ≤2 managers\n"
        f"<pre>{esc(chr(10).join(lines))}</pre>"
    )


def render_captains(
    spread: list[tuple[int, list[str], int]], players: dict[int, Player]
) -> str:
    lines = []
    for element, teams, pts in spread:
        name = players[element].web_name if element in players else str(element)
        lines.append(f"{pts:>3} {clip(name, 12):<12} ×{len(teams)}  {clip(', '.join(teams), 22)}")
    return f"<b>Captains</b>\n<pre>{esc(chr(10).join(lines))}</pre>"


def render_bench(rows: list[ManagerLive], limit: int = 12) -> str:
    lines = [f"{clip(r.team_name, 16):<16} {r.bench_points:>3}" for r in rows[:limit]]
    total = sum(r.bench_points for r in rows)
    return (
        "<b>Points on the bench</b>\n"
        f"<pre>{esc(chr(10).join(lines))}</pre>\n"
        f"<i>{total} points wasted across the league</i>"
    )


def render_awards(awards: list[Award]) -> str:
    if not awards:
        return "No awards yet — the gameweek hasn't produced any heroes or villains."
    lines = [f"{a.title}\n  <b>{esc(a.winner)}</b> — {esc(a.detail)}" for a in awards]
    return "<b>Gameweek awards</b>\n\n" + "\n\n".join(lines)


def render_deadline(deadline: datetime, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    local = deadline.astimezone(tz)
    delta = deadline - datetime.now(UTC)
    if delta.total_seconds() < 0:
        return "The deadline has passed. Teams are locked."
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [f"{days}d" if days else "", f"{hours}h" if hours or days else "", f"{minutes}m"]
    countdown = " ".join(p for p in parts if p)
    return (
        f"⏰ <b>Deadline in {countdown}</b>\n"
        f"{local:%a %d %b, %H:%M} ({tz_name})"
    )


def render_player_events(events: list[str]) -> str:
    return "\n".join(events)


def player_line(p: Player, live: PlayerLive | None = None) -> str:
    flag = "" if not p.flagged else {"d": "🟡", "i": "🔴", "s": "🟥", "u": "⛔"}.get(p.status, "⚠️")
    pts = f" · {live.effective_points} pts" if live else ""
    return f"{flag}<b>{esc(p.web_name)}</b> £{p.price:.1f}m · {p.selected_by_percent:.1f}% owned{pts}"
