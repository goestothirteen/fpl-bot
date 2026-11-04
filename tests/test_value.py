from __future__ import annotations

from dataclasses import replace

from conftest import player

from fplbot.bot.formatting import render_squad_values
from fplbot.services import value as value_svc
from fplbot.services.value import money, squad_value


def history(*rows: tuple[int, int, int, int]) -> dict:
    """(event, value, bank, hit_cost)"""
    return {"current": [
        {"event": e, "value": v, "bank": b, "event_transfers": 1, "event_transfers_cost": c}
        for e, v, b, c in rows
    ]}


def transfer(el_in: int, cost_in: int, el_out: int, event: int, time: str) -> dict:
    return {"element_in": el_in, "element_in_cost": cost_in, "element_out": el_out,
            "element_out_cost": 40, "event": event, "time": time}


def _players() -> dict:
    p = {i: player(i, team=1, etype=3) for i in range(1, 6)}
    # now_cost defaults to 50 — bump a couple so buys have moved.
    p[2] = replace(p[2], now_cost=56)
    p[3] = replace(p[3], now_cost=44)
    return p


def test_value_profit_week_delta_and_hits():
    v = squad_value(
        7, "Team", "Mark", history((1, 1000, 5, 0), (2, 1008, 3, 4), (3, 1014, 7, 0)),
        [], _players(), event=3,
    )
    assert v is not None
    assert v.value == 1014 and v.bank == 7 and v.squad == 1007
    assert v.profit == 14
    assert v.week_delta == 6
    assert v.hits == 4
    assert v.transfers == 0
    assert v.held_buys == [] and v.buy_gain == 0


def test_rows_after_the_requested_event_are_ignored():
    v = squad_value(7, "T", "M", history((1, 1000, 0, 0), (2, 1020, 0, 0)), [], {}, event=1)
    assert v is not None and v.value == 1000 and v.week_delta is None


def test_no_history_yet_returns_none():
    assert squad_value(7, "T", "M", {"current": []}, [], {}, event=1) is None


def test_held_buys_use_latest_purchase_and_current_squad():
    players = _players()
    transfers = [
        transfer(2, 50, 9, 2, "2026-09-01T10:00:00Z"),   # bought at 5.0, now 5.6
        transfer(3, 48, 9, 2, "2026-09-01T10:01:00Z"),   # bought at 4.8, now 4.4
        transfer(4, 60, 9, 3, "2026-09-08T10:00:00Z"),   # bought but since sold
        transfer(2, 53, 9, 4, "2026-09-15T10:00:00Z"),   # re-bought at 5.3 — this one counts
    ]
    v = squad_value(
        7, "T", "M", history((4, 1010, 2, 0)), transfers, players, event=4, squad={1, 2, 3, 5},
    )
    assert v is not None
    assert v.transfers == 4
    assert {b.element: b.paid for b in v.held_buys} == {2: 53, 3: 48}
    assert v.buy_gain == (56 - 53) + (44 - 48)
    assert v.best_buy and v.best_buy.element == 2 and v.best_buy.gain == 3
    assert v.worst_buy and v.worst_buy.element == 3 and v.worst_buy.gain == -4


def test_without_picks_held_is_inferred_from_the_transfer_list():
    players = _players()
    transfers = [
        transfer(2, 50, 9, 2, "2026-09-01T10:00:00Z"),
        transfer(5, 50, 2, 3, "2026-09-08T10:00:00Z"),   # 2 went back out
    ]
    v = squad_value(7, "T", "M", history((3, 1000, 0, 0)), transfers, players, event=3)
    assert v is not None
    assert [b.element for b in v.held_buys] == [5]


def test_ranked_richest_first_then_bank():
    a = squad_value(1, "A", "", history((1, 1010, 1, 0)), [], {}, event=1)
    b = squad_value(2, "B", "", history((1, 1010, 4, 0)), [], {}, event=1)
    c = squad_value(3, "C", "", history((1, 1020, 0, 0)), [], {}, event=1)
    assert [v.entry_id for v in value_svc.ranked([a, b, c])] == [3, 2, 1]


def test_money_formatting():
    assert money(1004) == "£100.4m"
    assert money(14, signed=True) == "+£1.4m"
    assert money(-3, signed=True) == "−£0.3m"
    assert money(0, signed=True) == "£0.0m"


def test_render_mentions_everything_that_matters():
    players = _players()
    v = squad_value(
        7, "Mbeumo My Hole", "Teck Ann", history((8, 1009, 3, 0), (9, 1016, 1, 8)),
        [transfer(2, 50, 9, 9, "2026-10-01T10:00:00Z")], players, event=9, squad={2},
    )
    text = render_squad_values("FPL-SEER", 9, [v])
    assert "💷 <b>FPL-SEER</b> · team value after GW9" in text
    assert "🥇 <b>Mbeumo My Hole</b> — <b>£101.6m</b>" in text
    assert "squad £101.5m · bank £0.1m · +£1.6m since GW1 · ▲ £0.7m this week" in text
    assert "1 transfer · −8 in hits · buys +£0.6m · best P2 +£0.6m" in text
    assert "league average £101.6m" in text


def test_render_empty_and_unreachable():
    assert "No value data yet" in render_squad_values("L", 1, [])
    v = squad_value(1, "A", "", history((1, 1000, 0, 0)), [], {}, event=1)
    assert "2 manager(s) couldn't be reached" in render_squad_values("L", 1, [v], unreachable=2)
