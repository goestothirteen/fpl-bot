"""Draw the wager ledger as one week-by-week grid, as a PNG.

Telegram has no table markup and the bot deliberately never emits <pre> (the
code font is too small to read on a phone — see bot/formatting.py). A ledger,
though, really is a table: one row per settled gameweek, one column per
manager, each cell that week's win or loss, and a totals row underneath that
adds the columns up. So, like the Teams pitch, this view is drawn as a picture
with the bundled bold font and sent as a photo.

Pure: given a `Ledger` (services/ledger.py) it returns PNG bytes. No network,
no Telegram, so it is unit-tested directly.

Layout, at render width 1040 (phones show it at roughly a third of that, so
every size here is chosen for how it reads at ~350px wide):

    ZonggersNIPS
    wager · week by week

    GW      Aston   Mark    Teck    Ivan      <- purple header, names
    GW1     +40     -30     ±0      -10
    GW2*    ...                               <- * provisional
    ────────────────────────────────────
    Total   +120    -80     +30     -70       <- running balances
    Season  +300    -200    ±0      -100      <- only once GW38 is final
    Final   +420    -280    +30     -170

Columns are ordered by final balance, winner first.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from ..services.ledger import Ledger
from ..services.wager import money
from .pitch import _font

# ---------------------------------------------------------------------------
# Geometry (pixels at render resolution)
# ---------------------------------------------------------------------------
W = 1040
MARGIN = 32
TITLE_TOP = 28
TITLE_SIZE = 40
SUB_SIZE = 24
HEADER_H = 72
ROW_H = 58
TOTAL_ROW_H = 66
RULE_H = 4
FOOT_H = 40
BOTTOM_PAD = 24
GW_COL_W = 150

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
PAGE = (255, 255, 255)
HEADER_BG = (55, 0, 60)            # FPL purple
HEADER_TEXT = (255, 255, 255)
ZEBRA = (244, 244, 247)
TEXT = (16, 20, 24)
MUTED = (120, 128, 138)
UP = (0, 140, 70)
DOWN = (200, 40, 40)
RULE = (55, 0, 60)
TOTAL_BG = (233, 255, 243)         # a wash of FPL green under the totals
PROVISIONAL_BG = (255, 248, 225)   # amber tint: these rows can still move


@dataclass(frozen=True, slots=True)
class _Column:
    entry_id: int
    label: str


def _signed(cents: int) -> str:
    """`money()` with a real minus sign, which reads far better than a hyphen."""
    return money(cents).replace("-", "−", 1)


def _colour(cents: int) -> tuple[int, int, int]:
    if cents > 0:
        return UP
    if cents < 0:
        return DOWN
    return MUTED


def _short_labels(ledger: Ledger, order: list[int]) -> list[_Column]:
    """One short name per column: the manager's first name when it is unique,
    the full manager name otherwise, and the team name when there is no
    manager name at all. Fitting to the column width happens at draw time."""
    firsts: dict[int, str] = {}
    for entry_id in order:
        team, manager = ledger.names.get(entry_id, (str(entry_id), ""))
        firsts[entry_id] = (manager.split()[0] if manager.strip() else team).strip()
    seen = [f.lower() for f in firsts.values()]
    out = []
    for entry_id in order:
        team, manager = ledger.names.get(entry_id, (str(entry_id), ""))
        label = firsts[entry_id]
        if seen.count(label.lower()) > 1 and manager.strip():
            label = manager.strip()
        out.append(_Column(entry_id, label))
    return out


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int,
         sizes: tuple[int, ...]) -> tuple[str, ImageFont.FreeTypeFont]:
    """Largest of `sizes` at which `text` fits, else clipped at the smallest."""
    for size in sizes:
        font = _font(size)
        if draw.textlength(text, font=font) <= max_w:
            return text, font
    font = _font(sizes[-1])
    while len(text) > 2 and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…", font


def _cell_sizes(columns: int) -> tuple[int, ...]:
    """Amount font ladder: fewer managers, bigger numbers."""
    if columns <= 4:
        return (36, 32, 28)
    if columns <= 6:
        return (32, 28, 24)
    return (28, 24, 20)


def render_wager_grid(ledger: Ledger) -> bytes:
    """The whole season so far as one grid. Returns PNG bytes."""
    final = ledger.final_balances
    order = sorted(final, key=lambda e: (-final[e], e))
    columns = _short_labels(ledger, order)
    n = max(len(columns), 1)
    col_w = (W - 2 * MARGIN - GW_COL_W) // n

    events = list(ledger.events)
    total_rows = 1 + (2 if ledger.season_amounts else 0)
    notes = []
    if ledger.provisional:
        notes.append("* provisional — awaiting FPL confirmation, can still change")
    if any(r.agreed for rows in ledger.by_event.values() for r in rows):
        notes.append("† settled by hand, not by the ladder")
    if not events:
        notes.append("No gameweek has been settled yet.")

    title_block = TITLE_TOP + TITLE_SIZE + 10 + SUB_SIZE + 24
    height = (
        title_block + HEADER_H + ROW_H * len(events) + RULE_H
        + TOTAL_ROW_H * total_rows + FOOT_H * len(notes) + BOTTOM_PAD
    )
    img = Image.new("RGB", (W, height), PAGE)
    draw = ImageDraw.Draw(img)

    # Title
    title, title_font = _fit(draw, ledger.league_name, W - 2 * MARGIN, (TITLE_SIZE, 34, 28))
    draw.text((MARGIN, TITLE_TOP), title, font=title_font, fill=TEXT)
    span = ""
    if events:
        span = f" · GW{events[0]}" if len(events) == 1 else f" · GW{events[0]}–{events[-1]}"
    draw.text((MARGIN, TITLE_TOP + TITLE_SIZE + 10), f"wager · week by week{span}",
              font=_font(SUB_SIZE), fill=MUTED)

    # Header row
    y = title_block
    draw.rectangle((MARGIN, y, W - MARGIN, y + HEADER_H), fill=HEADER_BG)
    draw.text((MARGIN + 16, y + HEADER_H / 2), "GW", font=_font(28), fill=HEADER_TEXT, anchor="lm")
    for i, col in enumerate(columns):
        cx = MARGIN + GW_COL_W + col_w * i + col_w / 2
        label, font = _fit(draw, col.label, col_w - 12, (28, 24, 20))
        draw.text((cx, y + HEADER_H / 2), label, font=font, fill=HEADER_TEXT, anchor="mm")
    y += HEADER_H

    # One row per settled gameweek
    cell_sizes = _cell_sizes(n)
    for k, event in enumerate(events):
        rows = {r.entry_id: r for r in ledger.by_event.get(event, [])}
        provisional = event in ledger.provisional
        agreed = any(r.agreed for r in rows.values())
        bg = PROVISIONAL_BG if provisional else (ZEBRA if k % 2 else PAGE)
        draw.rectangle((MARGIN, y, W - MARGIN, y + ROW_H), fill=bg)
        label = f"GW{event}" + ("*" if provisional else "") + ("†" if agreed else "")
        draw.text((MARGIN + 16, y + ROW_H / 2), label, font=_font(28),
                  fill=MUTED if provisional else TEXT, anchor="lm")
        for i, col in enumerate(columns):
            cents = rows[col.entry_id].amount if col.entry_id in rows else 0
            cx = MARGIN + GW_COL_W + col_w * i + col_w / 2
            text, font = _fit(draw, _signed(cents), col_w - 8, cell_sizes)
            draw.text((cx, y + ROW_H / 2), text, font=font, fill=_colour(cents), anchor="mm")
        y += ROW_H

    # Rule, then the totals
    draw.rectangle((MARGIN, y, W - MARGIN, y + RULE_H), fill=RULE)
    y += RULE_H

    total_sizes = tuple(s + 2 for s in cell_sizes)
    blocks: list[tuple[str, dict[int, int]]] = [("Total", dict(ledger.balances))]
    if ledger.season_amounts:
        blocks = [
            ("Weekly", dict(ledger.balances)),
            ("Season", dict(ledger.season_amounts)),
            ("Final", final),
        ]
    for j, (label, amounts) in enumerate(blocks):
        last = j == len(blocks) - 1
        draw.rectangle((MARGIN, y, W - MARGIN, y + TOTAL_ROW_H), fill=TOTAL_BG if last else PAGE)
        draw.text((MARGIN + 16, y + TOTAL_ROW_H / 2), label, font=_font(30), fill=TEXT, anchor="lm")
        for i, col in enumerate(columns):
            cents = amounts.get(col.entry_id, 0)
            cx = MARGIN + GW_COL_W + col_w * i + col_w / 2
            text, font = _fit(draw, _signed(cents), col_w - 8, total_sizes)
            draw.text((cx, y + TOTAL_ROW_H / 2), text, font=font, fill=_colour(cents), anchor="mm")
        y += TOTAL_ROW_H

    # Footnotes
    y += 8
    for note in notes:
        text, font = _fit(draw, note, W - 2 * MARGIN, (22, 20, 18))
        draw.text((MARGIN, y + FOOT_H / 2), text, font=font, fill=MUTED, anchor="lm")
        y += FOOT_H

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
