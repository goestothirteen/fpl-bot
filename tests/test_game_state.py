"""Which gameweek is *current* — the question every command starts from.

Regression cover for the GW3 2026/27 rollover, where `bootstrap-static` kept
reporting GW2 as current for well over half an hour after the GW3 deadline and
the six-hour cache then froze that answer in place.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fplbot.services.parsing import parse_game_state

DEADLINES = {
    1: "2026-08-21T17:30:00Z",
    2: "2026-08-28T17:30:00Z",
    3: "2026-09-04T17:30:00Z",
    4: "2026-09-12T12:30:00Z",
}


def bootstrap(
    *,
    flagged_current: int | None,
    flagged_next: int | None,
    overrides: dict[int, dict] | None = None,
) -> dict:
    """A bootstrap whose `is_current`/`is_next` flags we control independently
    of the deadlines, so we can reproduce FPL's post-deadline lag."""
    events = []
    for eid, deadline in DEADLINES.items():
        events.append(
            {
                "id": eid,
                "deadline_time": deadline,
                "is_current": eid == flagged_current,
                "is_next": eid == flagged_next,
                "is_previous": False,
                "finished": eid < (flagged_current or 1),
                "data_checked": eid < (flagged_current or 1),
                "average_entry_score": 0,
                "highest_score": None,
                **(overrides or {}).get(eid, {}),
            }
        )
    return {"events": events}


def at(when: str) -> datetime:
    return datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(UTC)


def test_steady_state_agrees_with_fpls_own_flags():
    """Mid-week, flags and deadlines say the same thing — we must not change it."""
    boot = bootstrap(flagged_current=2, flagged_next=3)
    state = parse_game_state(boot, now=at("2026-09-01T09:00:00Z"))
    assert state.current_event == 2
    assert state.next_event == 3
    assert state.next_deadline == at("2026-09-04T17:30:00Z")


def test_deadline_passing_rolls_the_gameweek_over_before_fpl_does():
    """The bug: 35 minutes past the GW3 deadline FPL still flagged GW2.

    Every command resolved to GW2 — `/live` showed a finished gameweek and
    `/transfers` filtered on event 2, hiding every GW2→GW3 move.
    """
    boot = bootstrap(flagged_current=2, flagged_next=3)
    state = parse_game_state(boot, now=at("2026-09-04T18:05:35Z"))
    assert state.current_event == 3, "deadline has passed — GW3 is live regardless of the flag"
    assert state.next_event == 4
    assert state.next_deadline == at("2026-09-12T12:30:00Z")


def test_rollover_is_immediate_at_the_deadline():
    boot = bootstrap(flagged_current=2, flagged_next=3)
    assert parse_game_state(boot, now=at("2026-09-04T17:29:59Z")).current_event == 2
    assert parse_game_state(boot, now=at("2026-09-04T17:30:00Z")).current_event == 3


def test_stale_current_events_metadata_is_not_carried_over():
    """GW2's `finished`/`data_checked`/average must not leak onto GW3 — the
    poller treats data_checked as 'settle the gameweek and pay out'."""
    boot = bootstrap(
        flagged_current=2,
        flagged_next=3,
        overrides={2: {"finished": True, "data_checked": True, "average_entry_score": 81,
                       "highest_score": 161}},
    )
    state = parse_game_state(boot, now=at("2026-09-04T18:05:35Z"))
    assert state.current_event == 3
    assert state.finished is False
    assert state.data_checked is False
    assert state.average_score == 0
    assert state.highest_score is None


def test_never_resolves_backwards_from_fpls_flag():
    """If a deadline is pushed back after a gameweek opens, the flag is the more
    accurate of the two and must win."""
    boot = bootstrap(flagged_current=4, flagged_next=None)
    state = parse_game_state(boot, now=at("2026-09-05T00:00:00Z"))
    assert state.current_event == 4


def test_before_the_season_there_is_no_current_gameweek():
    boot = bootstrap(flagged_current=None, flagged_next=1)
    state = parse_game_state(boot, now=at("2026-08-01T00:00:00Z"))
    assert state.current_event is None
    assert state.next_event == 1
    assert state.next_deadline == at("2026-08-21T17:30:00Z")


def test_after_the_final_deadline_there_is_no_next():
    boot = bootstrap(flagged_current=4, flagged_next=None)
    state = parse_game_state(boot, now=at("2026-09-20T00:00:00Z"))
    assert state.current_event == 4
    assert state.next_event is None


def test_missing_deadlines_fall_back_to_the_flags():
    """FPL has shipped events without a deadline_time before now."""
    boot = {"events": [{"id": n, "is_current": n == 2, "is_next": n == 3} for n in (1, 2, 3)]}
    state = parse_game_state(boot, now=at("2026-09-04T18:05:35Z"))
    assert state.current_event == 2
    assert state.next_event == 3


def test_empty_bootstrap_does_not_raise():
    state = parse_game_state({}, now=at("2026-09-04T18:05:35Z"))
    assert state.current_event is None
    assert state.next_event is None
    assert state.next_deadline is None


def test_defaults_to_wall_clock_when_no_now_is_given():
    """The production call passes no `now`."""
    far_past = {
        "events": [
            {"id": 1, "deadline_time": "2020-08-21T17:30:00Z", "is_current": False,
             "is_next": False},
        ]
    }
    assert parse_game_state(far_past).current_event == 1

    far_future = {
        "events": [
            {"id": 1, "deadline_time": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
             "is_current": False, "is_next": False},
        ]
    }
    assert parse_game_state(far_future).current_event is None
    assert parse_game_state(far_future).next_event == 1
