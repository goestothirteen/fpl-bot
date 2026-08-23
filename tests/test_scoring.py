"""Provisional bonus and auto-sub prediction — the two bits of live scoring FPL
doesn't hand you, and therefore the two most worth testing."""
from __future__ import annotations

from conftest import fixture, live, pick

from fplbot.services.scoring import apply_provisional_bonus, predict_auto_subs, provisional_bonus


class TestProvisionalBonus:
    def test_clean_three_way(self):
        f = fixture(1, 1, 2, provisional=False, bps={10: 40, 20: 30, 30: 20, 40: 10})
        assert provisional_bonus(f) == {10: 3, 20: 2, 30: 1}

    def test_tie_for_first_shares_three(self):
        f = fixture(1, 1, 2, provisional=False, bps={10: 40, 20: 40, 30: 20})
        # Two on 3, then the next player drops straight to 1 — FPL's actual rule.
        assert provisional_bonus(f) == {10: 3, 20: 3, 30: 1}

    def test_tie_for_second(self):
        f = fixture(1, 1, 2, provisional=False, bps={10: 40, 20: 30, 30: 30})
        assert provisional_bonus(f) == {10: 3, 20: 2, 30: 2}

    def test_three_way_tie_top(self):
        f = fixture(1, 1, 2, provisional=False, bps={10: 40, 20: 40, 30: 40, 40: 5})
        assert provisional_bonus(f) == {10: 3, 20: 3, 30: 3}

    def test_finished_fixture_gets_nothing(self):
        """Once a fixture is finished_provisional the API supplies real bonus,
        so adding ours would double-count."""
        f = fixture(1, 1, 2, provisional=True, bps={10: 40, 20: 30})
        assert provisional_bonus(f) == {}

    def test_apply_skips_players_with_confirmed_bonus(self):
        lv = {10: live(10, points=8), 20: live(20, points=6)}
        lv[10].bonus = 3
        f = fixture(1, 1, 2, provisional=False, bps={10: 40, 20: 30})
        assert apply_provisional_bonus(lv, [f]) is True
        assert lv[10].provisional_bonus == 0   # already had real bonus
        assert lv[20].provisional_bonus == 2
        assert lv[20].effective_points == 8


class TestAutoSubs:
    def test_no_blanks_no_subs(self, squad, players):
        lv = {i: live(i) for i in range(1, 16)}
        done = {i: True for i in range(1, 16)}
        assert predict_auto_subs(squad, lv, players, done) == []

    def test_blank_midfielder_replaced_by_bench_midfielder(self, squad, players):
        lv = {i: live(i) for i in range(1, 16)}
        lv[9] = live(9, minutes=0, points=0)          # MID starter blanked
        done = {i: True for i in range(1, 16)}
        subs = predict_auto_subs(squad, lv, players, done)
        # First eligible bench player in order 12,13,14,15 that keeps it legal.
        # 12 is a GK (illegal), 13 is a DEF (4→5 DEF, 4→3 MID: legal).
        assert subs == [(9, 13)]

    def test_gk_only_swaps_with_gk(self, squad, players):
        lv = {i: live(i) for i in range(1, 16)}
        lv[1] = live(1, minutes=0, points=0)
        done = {i: True for i in range(1, 16)}
        assert predict_auto_subs(squad, lv, players, done) == [(1, 12)]

    def test_bench_player_who_also_blanked_is_skipped(self, squad, players):
        lv = {i: live(i) for i in range(1, 16)}
        lv[9] = live(9, minutes=0, points=0)
        lv[13] = live(13, minutes=0, points=0)        # bench DEF also blanked
        done = {i: True for i in range(1, 16)}
        assert predict_auto_subs(squad, lv, players, done) == [(9, 14)]

    def test_no_sub_while_the_fixture_is_still_going(self, squad, players):
        lv = {i: live(i) for i in range(1, 16)}
        lv[9] = live(9, minutes=0, points=0)
        done = {i: True for i in range(1, 16)}
        done[9] = False                                # his match hasn't finished
        assert predict_auto_subs(squad, lv, players, done) == []

    def test_formation_floor_respected(self, players):
        """3 DEF minimum: blank a defender with only forwards on the bench and
        no legal swap exists."""
        picks = [pick(1, 1, 1)]
        picks += [pick(i, i, 1) for i in (2, 3, 4)]              # exactly 3 DEF
        picks += [pick(i, i, 1) for i in (6, 7, 8, 9)]           # 4 MID
        picks += [pick(10, 9, 1), pick(11, 10, 1), pick(15, 11, 1)]  # 3 FWD
        picks += [pick(12, 12, 0), pick(14, 13, 0)]              # bench: GK, MID
        lv = {i: live(i) for i in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 14, 15)}
        lv[2] = live(2, minutes=0, points=0)
        done = dict.fromkeys(range(1, 16), True)
        # DEF 3→2 is illegal, and the bench MID would take MID to 5 which is fine
        # but DEF would drop below 3 — so no sub.
        assert predict_auto_subs(picks, lv, players, done) == []
