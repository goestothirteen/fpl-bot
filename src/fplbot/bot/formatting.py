"""Rendering domain objects into Telegram HTML.

Telegram has no table markup. <pre> is the only thing that aligns columns, but
it renders in the small code font and reads like a code snippet, so every view
here uses ordinary proportional text: a bold title line per row, then an
indented detail line. Nothing in this module emits <pre>.
"""
from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..services.analysis import Award, Ownership
from ..services.models import (
    FixtureState,
    GamePhase,
    LiveTable,
    ManagerLive,
    Player,
    PlayerLive,
)

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


def _summary(rows: list[ManagerLive]) -> list[str]:
    """The one-glance numbers a fixed-width table has no room for."""
    out: list[str] = []
    if len(rows) > 1:
        gap = rows[0].live_total - rows[1].live_total
        leader = clip(rows[0].team_name, 24)
        out.append(f"{leader} leads by {gap}" if gap else f"{leader} level at the top")
    out.append(f"avg {sum(r.net_points for r in rows) / len(rows):.1f}")
    if bench := sum(r.bench_points for r in rows):
        out.append(f"{bench} benched")
    if on_pitch := sum(r.in_play for r in rows):
        out.append(f"{on_pitch} on the pitch")
    return out


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def render_live_cards(
    table: LiveTable,
    players: dict[int, Player] | None = None,
    live: dict[int, PlayerLive] | None = None,
    limit: int = 25,
) -> str:
    """One block per manager, in ordinary proportional text.

    Telegram has no table markup — <pre> is the only thing that aligns columns,
    and it renders in the small code font. This trades the alignment away to get
    normal-sized text and untruncated team names.
    """
    rows = table.ranked_gw()[:limit]
    head = f"⚡ <b>{esc(table.league_name)}</b> · GW{table.event} only"
    if not rows:
        return f"{head}\nNo managers here yet — <code>/link</code> a league."

    blocks = []
    for i, r in enumerate(rows, 1):
        rank = MEDALS.get(i, f"<b>{i}</b>")
        title = f"{rank} <b>{esc(clip(r.team_name, 28))}</b> — <b>{r.net_points}</b>"
        chip = CHIP_LABEL.get(r.active_chip or "", "")
        if chip:
            title += f" · {chip}"

        bits = []
        if chip and r.chip_points:
            # Split the chip out: a boosted score can't be compared like-for-like
            # against someone who didn't play one.
            bits.append(f"{r.base_points} base")
            bits.append(f"+{r.chip_points} {chip}")
        # The manager's real name comes free from the standings endpoint and
        # answers "whose team is that?" without anyone assigning anything.
        if r.manager_name and r.manager_name != "?":
            bits.append(clip(r.manager_name, 22))
        if r.captain_element:
            bits.append(f"C {r.captain_points}" if r.captain_played else "C not started")
        bits.append(f"{r.remaining} to play" if r.remaining else "all played")
        if r.transfer_cost:
            bits.append(f"−{r.transfer_cost} hit")
        if r.predicted_subs:
            bits.append("↑ incl. bench sub")
        blocks.append(f"{title}\n     {esc(' · '.join(bits))}")

    out = f"{head}\n\n" + "\n\n".join(blocks)
    if summary := _summary(rows):
        out += f"\n\n{esc(' · '.join(summary))}"
    if age := _age_footer(table):
        out += f"\n<i>{esc(age)}</i>"
    return out


def render_season_table(table: LiveTable, limit: int = 25) -> str:
    """Cumulative season standings — the running total, not this gameweek."""
    rows = table.ranked()[:limit]
    head = f"🏆 <b>{esc(table.league_name)}</b> · season after GW{table.event}"
    if not rows:
        return f"{head}\nNo managers here yet — <code>/link</code> a league."

    blocks = []
    for i, r in enumerate(rows, 1):
        rank = MEDALS.get(i, f"<b>{i}</b>")
        title = f"{rank} <b>{esc(clip(r.team_name, 28))}</b> — <b>{r.live_total}</b>"
        bits = []
        if r.manager_name and r.manager_name != "?":
            bits.append(clip(r.manager_name, 22))
        bits.append(f"GW{table.event} {r.net_points:+d}")
        gap = rows[0].live_total - r.live_total
        if gap:
            bits.append(f"{gap} behind")
        blocks.append(f"{title}\n     {esc(' · '.join(bits))}")

    out = f"{head}\n\n" + "\n\n".join(blocks)
    if age := _age_footer(table):
        out += f"\n<i>{esc(age)}</i>"
    return out


def render_remaining_detail(
    table: LiveTable, players: dict[int, Player], limit: int = 25
) -> str:
    """Who each manager is still waiting on, by name."""
    rows = sorted(table.ranked_gw()[:limit], key=lambda r: (-r.remaining, -r.net_points))
    head = f"⏳ <b>Still to come</b> · GW{table.event}"
    if not table.team_state:
        return f"{head}\nNo fixture data yet."

    blocks = []
    for r in rows:
        waiting, on_pitch = [], []
        for pick in r.picks:
            if pick.effective_multiplier == 0:
                continue
            player = players.get(pick.element)
            if player is None:
                continue
            state = table.team_state.get(player.team)
            tag = player.web_name + ("ⓒ" if pick.is_captain else "")
            if state is FixtureState.UPCOMING:
                waiting.append(tag)
            elif state is FixtureState.LIVE:
                on_pitch.append(tag)

        title = f"<b>{esc(clip(r.team_name, 28))}</b>"
        if not waiting and not on_pitch:
            blocks.append(f"{title}\n     ✅ all played")
            continue
        detail = []
        if on_pitch:
            detail.append(f"▶️ on now: {esc(', '.join(on_pitch))}")
        if waiting:
            detail.append(f"⏳ to come ({len(waiting)}): {esc(', '.join(waiting))}")
        blocks.append(title + "\n     " + "\n     ".join(detail))

    return f"{head}\n\n" + "\n\n".join(blocks) + "\n\n<i>ⓒ = captain</i>"


def render_edge(table: LiveTable, players: dict[int, Player], limit: int = 25) -> str:
    """Remaining players split by who else still owns them.

    Points already banked can't be changed; what decides the rest of the
    gameweek is who is left to play. A remaining player nobody else owns is
    pure gain on the league — one everyone owns cancels out however much he
    hauls. Worth separating, and not something the points-scored differential
    view can tell you.
    """
    rows = table.ranked_gw()[:limit]
    head = f"🔮 <b>Where you can still gain</b> · GW{table.event}"
    if not table.team_state:
        return f"{head}\nNo fixture data yet."

    def pending(r: ManagerLive) -> list[tuple[int, bool]]:
        out = []
        for pick in r.picks:
            if pick.effective_multiplier == 0:
                continue
            player = players.get(pick.element)
            if player is None:
                continue
            if table.team_state.get(player.team) in (FixtureState.UPCOMING, FixtureState.LIVE):
                out.append((pick.element, pick.is_captain))
        return out

    # Fixture state is shared across the league — if he hasn't played for you,
    # he hasn't played for anyone — so this is a fair like-for-like count.
    pending_by_row = {r.entry_id: pending(r) for r in rows}
    owners: dict[int, int] = {}
    for items in pending_by_row.values():
        for element, _ in items:
            owners[element] = owners.get(element, 0) + 1

    total = len(rows)
    blocks = []
    for r in rows:
        items = pending_by_row[r.entry_id]
        if not items:
            blocks.append(f"<b>{esc(clip(r.team_name, 28))}</b>\n     ✅ all played — nothing left")
            continue

        def tag(element: int, is_cap: bool) -> str:
            name = players[element].web_name
            return f"{name} ⓒ" if is_cap else name

        mine = [tag(e, c) for e, c in items if owners.get(e) == 1]
        some = [f"{tag(e, c)} ×{owners[e]}" for e, c in items if 1 < owners.get(e, 0) < total]
        everyone = [tag(e, c) for e, c in items if owners.get(e) == total]

        title = (f"<b>{esc(clip(r.team_name, 28))}</b> — "
                 f"<b>{len(mine)}</b> unique of {len(items)} left")
        detail = []
        if mine:
            detail.append(f"🔥 only you: {esc(', '.join(mine))}")
        if some:
            detail.append(f"◐ shared: {esc(', '.join(some))}")
        if everyone:
            detail.append(f"⚖️ everyone: {esc(', '.join(everyone))}")
        blocks.append(title + "\n     " + "\n     ".join(detail))

    return (
        f"{head}\n\n" + "\n\n".join(blocks)
        + "\n\n<i>🔥 nobody else has him left — every point is a point gained\n"
        "◐ ×n = how many managers still have him\n"
        "⚖️ everyone has him, so he cancels out\n"
        "ⓒ = captain, counts double</i>"
    )


def render_differentials(
    diffs: list[tuple[Ownership, int]], players: dict[int, Player], limit: int = 12
) -> str:
    if not diffs:
        return "Nobody in this league owns a differential right now — you're all the same person."
    blocks = []
    for own, pts in diffs[:limit]:
        name = players[own.element].web_name if own.element in players else str(own.element)
        owners = ", ".join(clip(o, 20) for o in own.owners[:3])
        cap = " ©️" if own.captains else ""
        blocks.append(f"<b>{esc(name)}</b>{cap} — <b>{pts}</b>\n     {esc(owners)}")
    return (
        "💎 <b>Differentials</b> · owned by ≤2 managers\n\n"
        + "\n\n".join(blocks)
        + "\n\n<i>©️ = captained by an owner</i>"
    )


def render_captains(
    spread: list[tuple[int, list[str], int]], players: dict[int, Player]
) -> str:
    if not spread:
        return "No captains set yet."
    blocks = []
    for element, teams, pts in spread:
        name = players[element].web_name if element in players else str(element)
        owned = ", ".join(clip(t, 20) for t in teams)
        plural = "s" if len(teams) != 1 else ""
        blocks.append(
            f"©️ <b>{esc(name)}</b> — <b>{pts}</b>\n"
            f"     {len(teams)} team{plural} · {esc(owned)}"
        )
    return "©️ <b>Captains</b> · who backed whom\n\n" + "\n\n".join(blocks)


def render_bench(rows: list[ManagerLive], limit: int = 12) -> str:
    if not rows:
        return "No bench data yet."
    total = sum(r.bench_points for r in rows)
    blocks = []
    for r in rows[:limit]:
        who = clip(r.manager_name, 22) if r.manager_name and r.manager_name != "?" else ""
        note = "nothing wasted" if not r.bench_points else f"{r.bench_points} left behind"
        detail = " · ".join(x for x in (who, note) if x)
        blocks.append(
            f"<b>{esc(clip(r.team_name, 28))}</b> — <b>{r.bench_points}</b>\n     {esc(detail)}"
        )
    return (
        "🪑 <b>Points on the bench</b>\n\n"
        + "\n\n".join(blocks)
        + f"\n\n<i>{total} points wasted across the league</i>"
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
