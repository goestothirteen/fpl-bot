"""The wager is money, so the arithmetic gets tested harder than the rest."""
from __future__ import annotations

from fplbot.services import wager
from fplbot.services.wager import PNANIPILOTS, ZONGGERSNIPS, money, settle, transfers


def amounts(scheme, scores, *, season=False):  # noqa: ANN001
    return settle(scheme, scores, season=season)


class TestZonggersNIPS:
    """Seven players: top three paid, bottom three charged, fourth neutral."""

    SEVEN = [(1, 90), (2, 80), (3, 70), (4, 60), (5, 50), (6, 40), (7, 30)]

    def test_weekly_ladder(self):
        a = amounts(ZONGGERSNIPS, self.SEVEN)
        assert a[1] == 5000 and a[2] == 3000 and a[3] == 2000
        assert a[4] == 0
        assert a[5] == -2000 and a[6] == -3000 and a[7] == -5000

    def test_season_ladder(self):
        a = amounts(ZONGGERSNIPS, self.SEVEN, season=True)
        assert a[1] == 40000 and a[2] == 20000 and a[3] == 10000
        assert a[5] == -10000 and a[6] == -20000 and a[7] == -40000

    def test_weekly_is_zero_sum(self):
        assert sum(amounts(ZONGGERSNIPS, self.SEVEN).values()) == 0

    def test_shrunken_league_still_balances(self):
        """At five players third place is both third-from-top and
        third-from-bottom, and must correctly net to nothing."""
        five = [(1, 90), (2, 80), (3, 70), (4, 60), (5, 50)]
        a = amounts(ZONGGERSNIPS, five)
        assert a[3] == 0
        assert sum(a.values()) == 0


class TestPnanipilots:
    """Four players after joel tay was removed from the league."""

    FOUR = [(1, 90), (2, 80), (3, 70), (4, 60)]

    def test_weekly_ladder(self):
        a = amounts(PNANIPILOTS, self.FOUR)
        assert a[1] == 4000 and a[2] == 0 and a[3] == -1000 and a[4] == -3000
        assert sum(a.values()) == 0

    def test_season_ladder(self):
        a = amounts(PNANIPILOTS, self.FOUR, season=True)
        assert a[1] == 30000 and a[2] == 0 and a[3] == -10000 and a[4] == -20000
        assert sum(a.values()) == 0

    def test_a_fifth_player_takes_nothing_and_the_pot_still_balances(self):
        five = [*self.FOUR, (5, 50)]
        a = amounts(PNANIPILOTS, five)
        assert a[5] == 0
        assert sum(a.values()) == 0


class TestTies:
    def test_tied_pair_splits_the_combined_pot(self):
        # 1st and 2nd tie: (50 + 30) / 2 = 40 each, third still takes 20.
        scores = [(1, 90), (2, 90), (3, 70), (4, 60), (5, 50), (6, 40), (7, 30)]
        a = amounts(ZONGGERSNIPS, scores)
        assert a[1] == a[2] == 4000
        assert a[3] == 2000
        assert sum(a.values()) == 0

    def test_indivisible_pot_loses_no_cents(self):
        # Three-way tie for the top: (50 + 30 + 20) / 3 = 33.33…
        scores = [(1, 90), (2, 90), (3, 90), (4, 60), (5, 50), (6, 40), (7, 30)]
        a = amounts(ZONGGERSNIPS, scores)
        assert sorted([a[1], a[2], a[3]]) == [3333, 3333, 3334]
        assert sum(a.values()) == 0

    def test_negative_pot_splits_exactly(self):
        # Tie at the bottom: (-30 + -50) / 2, and nothing may leak.
        scores = [(1, 90), (2, 80), (3, 70), (4, 60), (5, 50), (6, 30), (7, 30)]
        a = amounts(ZONGGERSNIPS, scores)
        assert a[6] + a[7] == -8000
        assert sum(a.values()) == 0

    def test_everyone_level_pays_nobody(self):
        scores = [(i, 50) for i in range(1, 8)]
        a = amounts(ZONGGERSNIPS, scores)
        assert set(a.values()) == {0}


class TestRerunsAndCorrections:
    def test_settling_twice_gives_the_same_answer(self):
        """Amounts are derived, never accumulated — so a rerun is a no-op."""
        scores = [(1, 90), (2, 80), (3, 70), (4, 60)]
        assert amounts(PNANIPILOTS, scores) == amounts(PNANIPILOTS, scores)

    def test_a_points_correction_flows_straight_through(self):
        before = amounts(PNANIPILOTS, [(1, 90), (2, 80), (3, 70), (4, 60)])
        # 90 revised down to 50 drops entry 1 below every rival, so it goes
        # from taking the week's +40 to paying last place's -30.
        after = amounts(PNANIPILOTS, [(1, 50), (2, 80), (3, 70), (4, 60)])
        assert before[1] == 4000
        assert after[1] == -3000
        assert after[2] == 4000          # the new winner
        assert sum(after.values()) == 0

    def test_running_balances_accumulate(self):
        weekly = {
            1: {1: 4000, 2: 0, 3: -1000, 4: -3000},
            2: {1: -3000, 2: 4000, 3: 0, 4: -1000},
        }
        bal = wager.running_balances(weekly)
        assert bal[1] == 1000 and bal[2] == 4000
        assert sum(bal.values()) == 0

    def test_empty_league_is_handled(self):
        assert amounts(PNANIPILOTS, []) == {}


class TestSettlement:
    def test_transfers_clear_every_balance(self):
        balances = {1: 5000, 2: 3000, 3: -2000, 4: -6000}
        moves = transfers(balances)
        net = dict.fromkeys(balances, 0)
        for payer, payee, cents in moves:
            assert cents > 0
            net[payer] -= cents
            net[payee] += cents
        assert net == balances

    def test_nobody_owes_anything_when_level(self):
        assert transfers({1: 0, 2: 0}) == []

    def test_transfer_count_stays_small(self):
        balances = {1: 5000, 2: 3000, 3: -2000, 4: -6000}
        assert len(transfers(balances)) <= len(balances) - 1


class TestMoneyFormatting:
    def test_whole_amounts_have_no_decimals(self):
        assert money(4000) == "+40"
        assert money(-3000) == "-30"

    def test_split_amounts_keep_cents(self):
        assert money(3333) == "+33.33"
        assert money(-3334) == "-33.34"

    def test_zero_is_neutral(self):
        assert money(0) == "±0"

    def test_unsigned_for_payment_lines(self):
        assert money(-6000, signed=False) == "60"


def test_only_configured_leagues_have_a_scheme():
    assert wager.scheme_for(166726) is ZONGGERSNIPS
    assert wager.scheme_for(167008) is PNANIPILOTS
    assert wager.scheme_for(999999) is None
