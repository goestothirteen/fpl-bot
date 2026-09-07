"""When a gameweek's numbers are safe to write into the wager.

FPL's `event.data_checked` is the flag that says "this gameweek is final", and
waiting for it is unimpeachably safe — but it is also slow. In GW3 2026/27 every
fixture had finished, bonus was awarded and folded into player totals by
15:30Z on the Sunday, and `data_checked` was still false fourteen hours later.
The wager for the week simply did not exist until FPL got round to it.

So settlement happens in two stages:

    provisional — every fixture complete, bonus awarded *and* folded into the
                  players' running totals, plus a safety buffer. The week's
                  amounts are written and shown, flagged as not-yet-confirmed.
    confirmed   — `data_checked` arrives, the figures are rewritten from final
                  data and the flag clears. The week stops moving.

Between the two the poller keeps re-upserting, so a late bonus or stat
correction overwrites the provisional figure on the next cycle rather than
being baked in. Nothing here can move real money: the season-end freeze in
`repo.record_settlement` still requires every gameweek to be confirmed.

Pure functions — no network, no clock of their own. See tests/test_settlement.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Fixture, PlayerLive

# Kickoff to final whistle, generously: 90 minutes, half time, and stoppage.
MATCH_LENGTH = timedelta(hours=2)

# Insurance against settling into an early bonus or stat correction. FPL has
# been seen adjusting BPS-derived bonus shortly after a late fixture ends.
SETTLE_BUFFER = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a gameweek may be provisionally settled, and why not if not."""

    ready: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ready


def bonus_is_folded(fixtures: list[Fixture], live: dict[int, PlayerLive]) -> bool:
    """True when every awarded bonus point is also in the player's total.

    There is a window where FPL has published the 3/2/1 in the fixture's stats
    but has not yet added it to `total_points`. Settling inside that window
    would bank scores that are about to go up, so both halves must agree.
    """
    for fixture in fixtures:
        if not fixture.bonus:
            return False
        for element, awarded in fixture.bonus.items():
            player = live.get(element)
            if player is None or player.bonus != awarded:
                return False
    return True


def settlement_ready(
    fixtures: list[Fixture],
    live: dict[int, PlayerLive],
    *,
    now: datetime,
    buffer: timedelta = SETTLE_BUFFER,
) -> Readiness:
    """Whether a gameweek is done enough to write provisional wager amounts.

    Deliberately derived from the fixtures themselves rather than from
    `event.data_checked` or `event-status.bonus_added`, both of which lag —
    `bonus_added` was still false for all three GW3 match days while the bonus
    was demonstrably awarded and folded.
    """
    if not fixtures:
        return Readiness(False, "no fixtures for this gameweek")

    # A postponed or rearranged fixture keeps the week open, exactly as
    # data_checked would have. This also covers double gameweeks: *every*
    # fixture in the event has to be done, not just one per team.
    if not all(f.complete for f in fixtures):
        outstanding = sum(1 for f in fixtures if not f.complete)
        return Readiness(False, f"{outstanding} fixture(s) not finished")

    if not bonus_is_folded(fixtures, live):
        return Readiness(False, "bonus not yet awarded and folded into totals")

    kickoffs = [f.kickoff for f in fixtures if f.kickoff is not None]
    if not kickoffs:
        return Readiness(False, "no kickoff times to measure the buffer from")

    ready_at = max(kickoffs) + MATCH_LENGTH + buffer
    if now < ready_at:
        return Readiness(False, f"safety buffer until {ready_at:%Y-%m-%d %H:%M}Z")

    return Readiness(True, "all fixtures complete, bonus folded, buffer elapsed")
