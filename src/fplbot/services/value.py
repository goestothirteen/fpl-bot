"""Squad value: what each manager's team is worth, and whether their transfers
have paid for themselves. Pure — fed the raw history/transfers payloads, no I/O.

FPL exposes, per manager:
  * ``entry/{id}/history/`` → ``current[]`` with ``value`` (squad at selling
    prices + bank, tenths of £m), ``bank``, ``event_transfers`` and
    ``event_transfers_cost`` for every gameweek so far;
  * ``entry/{id}/transfers/`` → every move with ``element_in_cost`` — the price
    paid — so a buy's rise or fall since then is ``now_cost − element_in_cost``.

Every team starts the season at £100.0m, so profit since GW1 is just
``value − 1000``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Player

STARTING_VALUE = 1000  # tenths of £m — the budget everyone begins with


@dataclass(frozen=True, slots=True)
class Buy:
    element: int
    name: str
    paid: int            # tenths, what they paid
    now: int             # tenths, today's price

    @property
    def gain(self) -> int:
        return self.now - self.paid


@dataclass(slots=True)
class SquadValue:
    entry_id: int
    team_name: str
    manager_name: str
    value: int                  # squad + bank, tenths of £m
    bank: int                   # tenths
    week_delta: int | None      # vs the previous gameweek's value, tenths
    transfers: int              # moves made this season
    hits: int                   # points paid for extra transfers
    held_buys: list[Buy] = field(default_factory=list)   # transfers-in still in the squad

    @property
    def squad(self) -> int:
        return self.value - self.bank

    @property
    def profit(self) -> int:
        return self.value - STARTING_VALUE

    @property
    def buy_gain(self) -> int:
        """How much the players they bought (and still hold) have moved since."""
        return sum(b.gain for b in self.held_buys)

    @property
    def best_buy(self) -> Buy | None:
        risen = [b for b in self.held_buys if b.gain > 0]
        return max(risen, key=lambda b: (b.gain, -b.paid)) if risen else None

    @property
    def worst_buy(self) -> Buy | None:
        fallen = [b for b in self.held_buys if b.gain < 0]
        return min(fallen, key=lambda b: (b.gain, b.paid)) if fallen else None


def _held_from_transfers(transfers: list[dict]) -> set[int]:
    """Without picks, assume a player is still held if the last move touching
    them was a transfer in."""
    latest: dict[int, tuple[str, bool]] = {}
    for t in transfers:
        when = t.get("time") or ""
        for key, is_in in (("element_in", True), ("element_out", False)):
            el = t.get(key)
            if el is None:
                continue
            if el not in latest or when > latest[el][0]:
                latest[el] = (when, is_in)
    return {el for el, (_, is_in) in latest.items() if is_in}


def squad_value(
    entry_id: int,
    team_name: str,
    manager_name: str,
    history: dict,
    transfers: list[dict],
    players: dict[int, Player],
    *,
    event: int,
    squad: set[int] | None = None,
) -> SquadValue | None:
    """One manager. ``squad`` is the current 15 if known (from picks); otherwise
    it is inferred from the transfer list. Returns None before their first
    gameweek has a row."""
    rows = sorted(
        (h for h in history.get("current", []) if h.get("event", 0) <= event),
        key=lambda h: h["event"],
    )
    if not rows:
        return None
    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    value = int(latest.get("value", STARTING_VALUE))
    bank = int(latest.get("bank", 0))
    week_delta = value - int(prev["value"]) if prev and "value" in prev else None

    held = squad if squad else _held_from_transfers(transfers)
    # Newest first, one Buy per element — the most recent purchase is the one
    # whose price they are sitting on.
    seen: set[int] = set()
    buys: list[Buy] = []
    for t in sorted(transfers, key=lambda t: t.get("time") or "", reverse=True):
        el = t.get("element_in")
        if el is None or el in seen or el not in held or el not in players:
            continue
        seen.add(el)
        buys.append(Buy(el, players[el].web_name, int(t.get("element_in_cost", 0)),
                        players[el].now_cost))

    return SquadValue(
        entry_id=entry_id,
        team_name=team_name,
        manager_name=manager_name,
        value=value,
        bank=bank,
        week_delta=week_delta,
        transfers=len(transfers),
        hits=sum(int(h.get("event_transfers_cost", 0)) for h in rows),
        held_buys=buys,
    )


def ranked(values: list[SquadValue]) -> list[SquadValue]:
    """Richest first; ties broken by cash in the bank, then name."""
    return sorted(values, key=lambda v: (-v.value, -v.bank, v.team_name))


def money(tenths: int, *, signed: bool = False) -> str:
    """1004 → '£100.4m'; with ``signed`` 14 → '+£1.4m', −3 → '−£0.3m', 0 → '£0.0m'."""
    sign = ""
    if signed and tenths > 0:
        sign = "+"
    elif tenths < 0:
        sign = "−"
    return f"{sign}£{abs(tenths) / 10:.1f}m"
