"""A standings page that cannot be ours must never become a table.

Pinned by 2026-10-20: FPL served two-manager strangers' leagues under our ids
for three polls, and the bot crowned them and billed them.
"""
from __future__ import annotations

from fplbot.services.live import foreign_standings

LEAGUE = 167008
META = {"id": LEAGUE, "name": "PNANIPILOTS"}
KNOWN = {854227, 897915, 2818107, 4886175}


def entries(*ids: int) -> list[dict]:
    return [{"entry": i, "entry_name": f"T{i}", "player_name": f"P{i}"} for i in ids]


def test_our_members_pass():
    assert foreign_standings(LEAGUE, META, entries(*KNOWN), KNOWN) is None


def test_strangers_only_is_rejected():
    assert foreign_standings(LEAGUE, META, entries(35, 58), KNOWN) == "no known member on the page"


def test_wrong_league_id_is_rejected():
    bad = {"id": 2430021, "name": "PNANIPILOTS"}
    assert foreign_standings(LEAGUE, bad, entries(*KNOWN), KNOWN) is not None


def test_new_joiner_and_leaver_still_pass():
    # One regular left, a stranger-looking id joined: still recognisably ours.
    page = entries(854227, 897915, 2818107, 9999999)
    assert foreign_standings(LEAGUE, META, page, KNOWN) is None


def test_unknown_membership_switches_check_off():
    assert foreign_standings(LEAGUE, META, entries(35, 58), set()) is None
