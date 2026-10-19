"""The pitch PNG renderer — pure, no network, no Telegram."""
from __future__ import annotations

from conftest import live, player

from fplbot.bot.pitch import render_pitch
from fplbot.services.models import ManagerLive, Pick

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _pick(element: int, position: int, multiplier: int,
          captain: bool = False, vice: bool = False) -> Pick:
    return Pick(
        element=element, position=position, multiplier=multiplier,
        is_captain=captain, is_vice_captain=vice,
    )


def _manager(picks: list[Pick]) -> ManagerLive:
    return ManagerLive(
        entry_id=1, manager_name="Gaffer", team_name="Test FC",
        picks=picks, active_chip=None, transfer_cost=0, season_total_before=100,
        gw_points=55, captain_element=10,
    )


def _full_squad() -> list[Pick]:
    picks = [_pick(1, 1, 1)]                                  # GK
    picks += [_pick(i, i, 1) for i in range(2, 6)]           # DEF 2-5
    picks += [_pick(i, i, 1) for i in range(6, 10)]          # MID 6-9
    picks += [_pick(10, 10, 2, captain=True), _pick(11, 11, 1, vice=True)]  # FWD
    picks += [_pick(i, i, 0) for i in range(12, 16)]         # bench
    return picks


def _players():  # noqa: ANN202
    types = {1: 1, 2: 2, 3: 2, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3, 9: 3, 10: 4, 11: 4,
             12: 1, 13: 2, 14: 3, 15: 4}
    return {pid: player(pid, team=pid, etype=t) for pid, t in types.items()}


def _live():  # noqa: ANN202
    # Odd ids "played" (90'), even ids waiting (0').
    return {pid: live(pid, minutes=90 if pid % 2 else 0, points=pid) for pid in range(1, 16)}


class TestRenderPitch:
    def test_returns_valid_png_for_a_full_xi(self):
        png = render_pitch(
            _manager(_full_squad()), _players(), _live(),
            event=7, league_name="Test League",
        )
        assert isinstance(png, bytes)
        assert png.startswith(PNG_MAGIC)
        assert len(png) > 1000   # a real image, not a stub

    def test_empty_picks_does_not_raise(self):
        png = render_pitch(
            _manager([]), {}, {},
            event=7, league_name="Test League",
        )
        assert png.startswith(PNG_MAGIC)

    def test_missing_player_and_live_data_is_tolerated(self):
        # Picks reference elements with no Player/live entry — must not raise.
        png = render_pitch(
            _manager(_full_squad()), {}, {},
            event=7, league_name="Test League",
        )
        assert png.startswith(PNG_MAGIC)

    def test_long_player_name_is_handled(self):
        # A very long web_name must auto-shrink to fit, not crash.
        from dataclasses import replace
        players = _players()
        players[6] = replace(players[6], web_name="Alexander-Arnoldssen")
        png = render_pitch(
            _manager(_full_squad()), players, _live(),
            event=7, league_name="Test League",
        )
        assert png.startswith(PNG_MAGIC)


class TestTypography:
    """The names must be drawn with a real scalable font. The production image
    (python:*-slim) has no system fonts, and Pillow's silent fallback is a 10px
    bitmap font — which is exactly how the first release shipped."""

    def test_font_is_scalable_at_the_requested_size(self):
        from PIL import ImageFont

        from fplbot.bot.pitch import _font

        f = _font(42)
        assert isinstance(f, ImageFont.FreeTypeFont)
        assert f.size == 42

    def test_bundled_font_file_ships_with_the_package(self):
        from fplbot.bot.pitch import _BUNDLED_BOLD

        assert _BUNDLED_BOLD.is_file()


def _draw():  # noqa: ANN202
    from PIL import Image, ImageDraw

    return ImageDraw.Draw(Image.new("RGB", (10, 10)))


class TestNameFit:
    """Every name renders at the one shared size; long ones wrap or condense."""

    def test_short_name_is_one_line_at_the_shared_size(self):
        from fplbot.bot.pitch import NAME_SIZE, _fit_name

        lines, font = _fit_name(_draw(), "Raya")
        assert lines == ["Raya"]
        assert font.size == NAME_SIZE

    def test_long_hyphenated_name_wraps_at_the_hyphen(self):
        from fplbot.bot.pitch import NAME_SIZE, _fit_name

        lines, font = _fit_name(_draw(), "Alexander-Arnold")
        assert lines == ["Alexander-", "Arnold"]
        assert font.size == NAME_SIZE

    def test_initial_prefixed_name_wraps_after_the_dot(self):
        from fplbot.bot.pitch import NAME_SIZE, _fit_name

        lines, font = _fit_name(_draw(), "N.Williams")
        assert lines == ["N.", "Williams"]
        assert font.size == NAME_SIZE

    def test_two_word_name_wraps_at_the_space(self):
        from fplbot.bot.pitch import NAME_SIZE, _fit_name

        lines, font = _fit_name(_draw(), "João Pedro")
        assert lines == ["João", "Pedro"]
        assert font.size == NAME_SIZE

    def test_mild_overflow_condenses_one_line_rather_than_wrapping_awkwardly(self):
        # "Fernandes" alone is already too wide for a clean split, so the whole
        # name stays on one line, condensed, at the shared size.
        from fplbot.bot.pitch import NAME_SIZE, _fit_name

        lines, font = _fit_name(_draw(), "B.Fernandes")
        assert lines == ["B.Fernandes"]
        assert font.size == NAME_SIZE

    def test_long_single_word_is_condensed_not_shrunk(self):
        from fplbot.bot.pitch import MAX_CONDENSE, NAME_MAX_W, NAME_SIZE, _fit_name

        draw = _draw()
        for name in ("Szoboszlai", "Richarlison", "Branthwaite"):
            lines, font = _fit_name(draw, name)
            assert lines == [name], name
            assert font.size == NAME_SIZE, name
            assert draw.textlength(name, font=font) <= NAME_MAX_W / MAX_CONDENSE, name

    def test_hopeless_name_is_clipped_not_overflowing(self):
        from fplbot.bot.pitch import MAX_CONDENSE, NAME_MAX_W, _fit_name

        draw = _draw()
        lines, font = _fit_name(draw, "Abcdefghijklmnopqrstuvwxyz")
        assert len(lines) == 1 and lines[0].endswith("…")
        assert draw.textlength(lines[0], font=font) <= NAME_MAX_W / MAX_CONDENSE


class TestLayout:
    def test_five_wide_row_does_not_overlap(self):
        from fplbot.bot.pitch import CARD_W, W, _row_centres

        xs = _row_centres(5)
        assert all(b - a >= CARD_W for a, b in zip(xs, xs[1:], strict=False))
        assert xs[0] - CARD_W // 2 >= 0 and xs[-1] + CARD_W // 2 <= W

    def test_rows_fit_inside_the_pitch(self):
        from fplbot.bot.pitch import PITCH_BOTTOM, PITCH_TOP, SHIRT_H, _row_top, card_h

        tops = [_row_top(i) for i in range(4)]
        assert tops[0] >= PITCH_TOP
        assert tops[-1] + card_h(SHIRT_H) <= PITCH_BOTTOM
        assert all(b - a >= card_h(SHIRT_H) for a, b in zip(tops, tops[1:], strict=False))

    def test_captain_card_shows_doubled_points(self):
        from fplbot.bot.pitch import _card_points

        assert _card_points(_pick(10, 10, 2, captain=True), live(10, minutes=90, points=12)) == 24
        assert _card_points(_pick(12, 12, 0), live(12, minutes=90, points=3)) == 3   # bench: raw
        assert _card_points(_pick(1, 1, 1), None) == 0


def _shirt_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (220, 290), (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


class TestShirts:
    def test_shirt_keys_cover_the_whole_squad_by_team_and_kit(self):
        from fplbot.bot.pitch import shirt_keys

        keys = shirt_keys(_manager(_full_squad()), _players())
        assert (1, True) in keys          # the GK, goalkeeper kit
        assert (12, True) in keys         # bench GK too
        assert (2, False) in keys
        assert len(keys) == 15            # demo data: one team per player

    def test_renders_with_real_missing_and_corrupt_shirts(self):
        shirts = {(1, True): _shirt_png(), (2, False): b"not a png"}
        png = render_pitch(
            _manager(_full_squad()), _players(), _live(),
            event=7, league_name="Test League", shirts=shirts,
        )
        assert png.startswith(PNG_MAGIC)
