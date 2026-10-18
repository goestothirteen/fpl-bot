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
