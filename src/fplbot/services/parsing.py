"""Defensive parsing of raw FPL payloads into domain objects.

FPL changes field sets between seasons (2026/27 added `price_change_projections`,
`region`, `known_name`, `scout_risks`). Everything here treats unknown fields as
absent and never raises on a missing optional.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .models import Fixture, GameState, Pick, Player, PlayerLive, Team


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _f(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def parse_teams(bootstrap: dict) -> dict[int, Team]:
    return {
        t["id"]: Team(id=t["id"], name=t["name"], short_name=t["short_name"])
        for t in bootstrap.get("teams", [])
    }


def parse_players(bootstrap: dict) -> dict[int, Player]:
    out: dict[int, Player] = {}
    for e in bootstrap.get("elements", []):
        out[e["id"]] = Player(
            id=e["id"],
            web_name=e.get("web_name", "?"),
            team=e.get("team", 0),
            element_type=e.get("element_type", 0),
            now_cost=e.get("now_cost", 0),
            status=e.get("status", "a"),
            news=e.get("news", "") or "",
            selected_by_percent=_f(e.get("selected_by_percent")),
            form=_f(e.get("form")),
            total_points=e.get("total_points", 0),
            chance_of_playing=e.get("chance_of_playing_next_round"),
        )
    return out


def parse_game_state(bootstrap: dict) -> GameState:
    events = bootstrap.get("events", [])
    cur = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    return GameState(
        current_event=cur["id"] if cur else None,
        next_event=nxt["id"] if nxt else None,
        next_deadline=_dt((nxt or cur or {}).get("deadline_time")),
        finished=bool(cur and cur.get("finished")),
        data_checked=bool(cur and cur.get("data_checked")),
        average_score=(cur or {}).get("average_entry_score", 0) or 0,
        highest_score=(cur or {}).get("highest_score"),
    )


def parse_fixtures(raw: list[dict]) -> list[Fixture]:
    fixtures: list[Fixture] = []
    for f in raw:
        bps: dict[int, int] = {}
        for stat in f.get("stats", []):
            if stat.get("identifier") == "bps":
                for side in ("h", "a"):
                    for item in stat.get(side, []):
                        bps[item["element"]] = item["value"]
        fixtures.append(
            Fixture(
                id=f["id"],
                event=f.get("event"),
                team_h=f["team_h"],
                team_a=f["team_a"],
                team_h_score=f.get("team_h_score"),
                team_a_score=f.get("team_a_score"),
                kickoff=_dt(f.get("kickoff_time")),
                minutes=f.get("minutes", 0),
                started=bool(f.get("started")),
                finished=bool(f.get("finished")),
                finished_provisional=bool(f.get("finished_provisional")),
                bps=bps,
            )
        )
    return fixtures


def parse_live(raw: dict) -> dict[int, PlayerLive]:
    out: dict[int, PlayerLive] = {}
    for e in raw.get("elements", []):
        s = e.get("stats", {})
        out[e["id"]] = PlayerLive(
            element=e["id"],
            minutes=s.get("minutes", 0),
            points=s.get("total_points", 0),
            bps=s.get("bps", 0),
            goals=s.get("goals_scored", 0),
            assists=s.get("assists", 0),
            clean_sheets=s.get("clean_sheets", 0),
            yellow_cards=s.get("yellow_cards", 0),
            red_cards=s.get("red_cards", 0),
            saves=s.get("saves", 0),
            bonus=s.get("bonus", 0),
            defensive_contribution=s.get("defensive_contribution", 0),
            explain=e.get("explain", []),
        )
    return out


def parse_picks(raw: dict) -> list[Pick]:
    subs_out = {s["element_out"] for s in raw.get("automatic_subs", [])}
    subs_in = {s["element_in"] for s in raw.get("automatic_subs", [])}
    picks: list[Pick] = []
    for p in raw.get("picks", []):
        picks.append(
            Pick(
                element=p["element"],
                position=p["position"],
                multiplier=p.get("multiplier", 0),
                is_captain=bool(p.get("is_captain")),
                is_vice_captain=bool(p.get("is_vice_captain")),
                subbed_out=p["element"] in subs_out,
                subbed_in=p["element"] in subs_in,
            )
        )
    return picks
