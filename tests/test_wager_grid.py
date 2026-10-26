"""The week-by-week wager grid — pure PNG renderer, no network, no Telegram."""
from __future__ import annotations

import io

from PIL import Image

from fplbot.bot import wager_grid
from fplbot.bot.wager_grid import render_wager_grid
from fplbot.services.ledger import Ledger, WeekRow
from fplbot.services.wager import ZONGGERSNIPS, settle

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

NAMES = {
    1: ("Ten Hag's Lads", "Aston Tan"),
    2: ("Sonny Delight", "Mark Wee"),
    3: ("Klopp Fiction TC", "Teck Ann Tan"),
    4: ("Mark's XI", "Ivan Neo"),
    5: ("REECE'S SET PIECES", "Mark Lee"),
}


def _ledger(weeks: int, managers: int = 5, *, provisional: set[int] = frozenset(),
            season: bool = False, agreed: set[int] = frozenset()) -> Ledger:
    """A deterministic ledger: manager e scores (100 - e*3 + week) each week,
    so the order is stable and every amount comes from the real ladder."""
    ids = list(range(1, managers + 1))
    by_event: dict[int, list[WeekRow]] = {}
    balances = dict.fromkeys(ids, 0)
    events = list(range(1, weeks + 1))
    for event in events:
        scores = [(e, 100 - e * 3 + (event % 3) * (e % 2)) for e in ids]
        amounts = settle(ZONGGERSNIPS, scores, season=False)
        ranked = sorted(scores, key=lambda t: -t[1])
        rows = []
        for pos, (e, pts) in enumerate(ranked, 1):
            balances[e] += amounts[e]
            team, manager = NAMES.get(e, (f"Team {e}", f"Manager {e}"))
            rows.append(WeekRow(
                entry_id=e, team_name=team, manager_name=manager, points=pts,
                position=pos, amount=amounts[e], balance=balances[e],
                agreed=event in agreed,
            ))
        by_event[event] = rows
    season_amounts = None
    if season:
        season_amounts = settle(ZONGGERSNIPS, [(e, balances[e]) for e in ids], season=True)
    return Ledger(
        league_id=166726, league_name="ZonggersNIPS", scheme=ZONGGERSNIPS,
        events=events, by_event=by_event, balances=balances,
        names={e: NAMES.get(e, (f"Team {e}", f"Manager {e}")) for e in ids},
        season_amounts=season_amounts, provisional=frozenset(provisional),
    )


def _image(png: bytes) -> Image.Image:
    assert png.startswith(PNG_MAGIC)
    return Image.open(io.BytesIO(png))


class TestRenderWagerGrid:
    def test_returns_a_png_at_render_width(self):
        img = _image(render_wager_grid(_ledger(9)))
        assert img.width == wager_grid.W
        assert img.height > wager_grid.HEADER_H + 9 * wager_grid.ROW_H

    def test_one_row_per_settled_gameweek(self):
        a = _image(render_wager_grid(_ledger(3)))
        b = _image(render_wager_grid(_ledger(4)))
        assert b.height - a.height == wager_grid.ROW_H

    def test_season_adjustment_adds_two_total_rows(self):
        plain = _image(render_wager_grid(_ledger(38)))
        full = _image(render_wager_grid(_ledger(38, season=True)))
        assert full.height - plain.height == 2 * wager_grid.TOTAL_ROW_H

    def test_provisional_and_agreed_rows_add_footnotes(self):
        plain = _image(render_wager_grid(_ledger(5)))
        noted = _image(render_wager_grid(_ledger(5, provisional={5}, agreed={1})))
        assert noted.height - plain.height == 2 * wager_grid.FOOT_H

    def test_full_season_fits_telegram_photo_limits(self):
        # sendPhoto: width + height <= 10000 and the aspect ratio at most 20:1.
        img = _image(render_wager_grid(_ledger(38, managers=7, season=True)))
        assert img.width + img.height <= 10000
        assert img.height / img.width <= 20

    def test_empty_ledger_still_renders(self):
        led = _ledger(0)
        img = _image(render_wager_grid(led))
        assert img.width == wager_grid.W

    def test_many_managers_do_not_overflow(self):
        img = _image(render_wager_grid(_ledger(10, managers=9)))
        assert img.width == wager_grid.W


class TestLabels:
    def test_first_names_when_unique(self):
        led = _ledger(1, managers=4)
        labels = [c.label for c in wager_grid._short_labels(led, [1, 2, 3, 4])]
        assert labels == ["Aston", "Mark", "Teck", "Ivan"]

    def test_duplicate_first_names_fall_back_to_full_name(self):
        led = _ledger(1, managers=5)
        labels = [c.label for c in wager_grid._short_labels(led, [1, 2, 3, 4, 5])]
        assert labels[1] == "Mark Wee"
        assert labels[4] == "Mark Lee"

    def test_team_name_when_no_manager_name(self):
        led = _ledger(1, managers=2)
        led = Ledger(
            league_id=led.league_id, league_name=led.league_name, scheme=led.scheme,
            events=led.events, by_event=led.by_event, balances=led.balances,
            names={1: ("Nameless FC", ""), 2: ("Other", "Bob")},
        )
        labels = [c.label for c in wager_grid._short_labels(led, [1, 2])]
        assert labels == ["Nameless FC", "Bob"]

    def test_signed_uses_a_real_minus(self):
        assert wager_grid._signed(-1050) == "−10.50"
        assert wager_grid._signed(4000) == "+40"
        assert wager_grid._signed(0) == "±0"
