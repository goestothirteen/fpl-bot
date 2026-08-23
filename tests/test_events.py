"""Alert detection and, crucially, idempotency keys."""
from __future__ import annotations

from datetime import UTC, datetime

from conftest import live, pick

from fplbot.live.events import attribute, detect_lead_change, diff_live, snapshot_of
from fplbot.live.notifier import Notifier
from fplbot.services.models import GamePhase, LiveTable, ManagerLive


def mgr(eid, name, elements, captain=None, total=0):  # noqa: ANN001
    picks = [pick(e, i + 1, 2 if e == captain else 1, captain=e == captain)
             for i, e in enumerate(elements)]
    return ManagerLive(entry_id=eid, manager_name=name, team_name=name, picks=picks,
                       active_chip=None, transfer_cost=0, season_total_before=total,
                       captain_element=captain)


def test_diff_detects_a_goal_and_ignores_unchanged_players():
    before = {1: {"goals": 0, "assists": 0, "red_cards": 0, "yellow_cards": 0},
              2: {"goals": 1, "assists": 0, "red_cards": 0, "yellow_cards": 0}}
    now = {1: live(1, points=8), 2: live(2, points=6)}
    now[1].goals = 1
    now[2].goals = 1
    events = diff_live(before, now)
    assert len(events) == 1
    assert events[0].element == 1 and events[0].kind == "goals"


def test_unseen_player_does_not_retro_alert():
    """A player absent from the previous snapshot must not fire an alert for
    stats accumulated before the bot started."""
    now = {1: live(1, points=13)}
    now[1].goals = 2
    assert diff_live({}, now) == []


def test_attribute_names_owners_and_captains():
    t = LiveTable(event=4, league_id=1, league_name="L", phase=GamePhase.LIVE, rows=[
        mgr(1, "Mark", [7], captain=7),
        mgr(2, "Wei", [7]),
        mgr(3, "Jia", [8]),
    ])
    before = {7: {"goals": 0, "assists": 0, "red_cards": 0, "yellow_cards": 0}}
    now = {7: live(7, points=10)}
    now[7].goals = 1
    events = attribute(diff_live(before, now), t, {7: "Saka"})
    assert len(events) == 1
    assert "Saka" in events[0].text
    assert "Mark" in events[0].text and "Wei" in events[0].text
    assert "captained by Mark" in events[0].text


def test_unowned_player_generates_no_league_event():
    t = LiveTable(event=4, league_id=1, league_name="L", phase=GamePhase.LIVE,
                  rows=[mgr(1, "Mark", [7])])
    before = {99: {"goals": 0, "assists": 0, "red_cards": 0, "yellow_cards": 0}}
    now = {99: live(99, points=6)}
    now[99].goals = 1
    assert attribute(diff_live(before, now), t, {99: "Nobody"}) == []


def test_alert_key_is_stable_across_restarts():
    """The key is derived from resulting state, not from when we noticed, so a
    restart mid-gameweek can't replay the same goal."""
    t = LiveTable(event=4, league_id=1, league_name="L", phase=GamePhase.LIVE,
                  rows=[mgr(1, "Mark", [7])])
    before = {7: {"goals": 0, "assists": 0, "red_cards": 0, "yellow_cards": 0}}
    now = {7: live(7, points=10)}
    now[7].goals = 1
    k1 = attribute(diff_live(before, now), t, {7: "Saka"})[0].key
    k2 = attribute(diff_live(before, now), t, {7: "Saka"})[0].key
    assert k1 == k2 == "gw4:goals:7:1:10"


def test_lead_change_only_fires_on_an_actual_change():
    rows = [mgr(1, "A", [1], total=100), mgr(2, "B", [2], total=90)]
    rows[0].gw_points, rows[1].gw_points = 0, 0
    t = LiveTable(event=4, league_id=1, league_name="L", phase=GamePhase.LIVE, rows=rows)
    assert detect_lead_change(t, previous_leader=1) is None
    ev = detect_lead_change(t, previous_leader=2)
    assert ev is not None and "A" in ev.text and "+10" in ev.text


def test_snapshot_roundtrip():
    lv = {1: live(1, points=6)}
    lv[1].goals = 1
    snap = snapshot_of(lv)
    assert snap[1]["goals"] == 1 and snap[1]["points"] == 6


class TestQuietHours:
    """SGT is UTC+8, so European night matches land at 03:00-06:00 local. The
    default quiet window is 01:00-08:00 and therefore wraps midnight."""

    @staticmethod
    def at(hour_utc: int) -> datetime:
        return datetime(2026, 8, 22, hour_utc, 0, tzinfo=UTC)

    def test_inside_the_wrapping_window(self):
        # 20:00 UTC == 04:00 SGT, squarely inside 01:00-08:00.
        assert Notifier.in_quiet_hours("Asia/Singapore", 1, 8, self.at(20)) is True

    def test_just_before_the_window_opens(self):
        # 16:00 UTC == 00:00 SGT — the window starts at 01:00.
        assert Notifier.in_quiet_hours("Asia/Singapore", 1, 8, self.at(16)) is False

    def test_just_after_the_window_closes(self):
        # 00:00 UTC == 08:00 SGT — quiet_to is exclusive.
        assert Notifier.in_quiet_hours("Asia/Singapore", 1, 8, self.at(0)) is False

    def test_non_wrapping_window(self):
        assert Notifier.in_quiet_hours("UTC", 9, 17, self.at(12)) is True
        assert Notifier.in_quiet_hours("UTC", 9, 17, self.at(20)) is False

    def test_disabled_when_from_equals_to(self):
        assert Notifier.in_quiet_hours("UTC", 3, 3, self.at(3)) is False
