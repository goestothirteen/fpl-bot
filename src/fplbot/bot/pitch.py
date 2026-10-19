"""Draw a manager's gameweek XI on a football pitch as a PNG.

Telegram can't edit a text message into a photo, so the "Teams" view lives on
its own photo message (see handlers/teams.py). This module is the pure,
network-free, Telegram-free renderer: given one manager's picks plus the player
and live maps, it returns PNG bytes. Kept pure so it is unit-testable and so the
handler layer stays thin.

Typography is the whole game here. Telegram shows the photo at roughly a third
of this size on a phone, so every size below is chosen for how it reads at
~350px wide, not at full resolution. The player NAME dominates each card and is
auto-fitted: shrunk a little for long names, then wrapped onto two lines at a
space or hyphen before it is allowed to get small. The points chip hangs off
the bottom edge of the card and only appears once the player has actually
played (minutes > 0) — a player yet to kick off shows a grey card and no number
rather than a misleading 0.

The bold font is bundled with the package (fonts/DejaVuSans-Bold.ttf, Bitstream
Vera licence alongside). The Docker image is python:*-slim and ships no system
fonts, so asking Pillow for "DejaVuSans-Bold.ttf" by name used to fail there
and silently fall back to the 10px bitmap font — which is why the cards once
rendered with unreadably small names. If the bundled file is somehow missing we
still fall back to Pillow's own embedded *scalable* font at the requested size,
never to the bitmap one.
"""
from __future__ import annotations

import io
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..services.models import ManagerLive, Pick, Player, PlayerLive

# ---------------------------------------------------------------------------
# Geometry (pixels at render resolution)
# ---------------------------------------------------------------------------
W, H = 1040, 1240
HEADER_H = 120
BENCH_H = 240
PITCH_TOP = HEADER_H
PITCH_BOTTOM = H - BENCH_H
MARGIN = 32

CARD_W, CARD_H = 184, 128
CARD_GAP = 16            # minimum gap between cards in a packed (5-wide) row
CARD_RADIUS = 14
CHIP_W, CHIP_H = 80, 44  # points chip, straddles the card's bottom edge
BADGE_R = 19             # captain / vice badge, straddles the top-right corner
RING = 5                 # captain ring thickness around the card

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
GRASS_DARK = (24, 122, 52)
GRASS_LIGHT = (32, 138, 60)
LINE = (255, 255, 255)
CARD_PLAYED = (250, 250, 250)      # player's fixture started/finished
CARD_WAITING = (200, 208, 216)     # fixture not kicked off yet
CARD_TEXT = (16, 20, 24)
CARD_TEXT_WAITING = (70, 78, 88)
PTS_BG = (55, 0, 60)               # FPL purple
PTS_TEXT = (0, 255, 136)           # FPL green
CAPTAIN_RING = (250, 196, 0)
BADGE_DARK = (12, 16, 20)
HEADER_BG = (12, 16, 20)
HEADER_TEXT = (255, 255, 255)
SUB_TEXT = (170, 180, 190)
DIVIDER = (40, 46, 52)

ELEMENT_ROWS = (1, 2, 3, 4)   # GK, DEF, MID, FWD

CHIP_NAMES = {
    "3xc": "Triple Captain",
    "bboost": "Bench Boost",
    "freehit": "Free Hit",
    "wildcard": "Wildcard",
    "manager": "Assistant Manager",
}

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
_BUNDLED_BOLD = Path(__file__).with_name("fonts") / "DejaVuSans-Bold.ttf"


@cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    """Bold font at `size` px. Always a scalable FreeType font, never the
    fixed 10px bitmap default — see the module docstring for why that matters."""
    for source in (str(_BUNDLED_BOLD), "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(source, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


FONT_TITLE_MAX = 40
FONT_TITLE_MIN = 26
FONT_SUB = _font(24)
FONT_SCORE = _font(64)
FONT_SCORE_UNIT = _font(26)
FONT_PTS = _font(30)
FONT_BADGE = _font(24)
FONT_BENCH_LABEL = _font(22)

# Name fit ladder. A single line is preferred down to NAME_SINGLE_MIN; below
# that a two-line split is tried and wins if it lets the name render larger.
NAME_SIZES = (42, 40, 38, 36, 34, 32, 30, 28, 26, 24)
NAME_SINGLE_MIN = 34
NAME_MAX_W = CARD_W - 16


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _largest_fitting(draw: ImageDraw.ImageDraw, text: str, max_w: int,
                     sizes: tuple[int, ...]) -> int | None:
    for size in sizes:
        if draw.textlength(text, font=_font(size)) <= max_w:
            return size
    return None


def _split_candidates(name: str) -> list[tuple[str, str]]:
    """Two-line splits at a space (dropped), a hyphen (kept on line one), or
    the dot after a leading initial ("B.Fernandes" -> "B." / "Fernandes")."""
    out: list[tuple[str, str]] = []
    for i, ch in enumerate(name):
        if ch == " ":
            a, b = name[:i], name[i + 1:]
        elif (ch == "-" and 0 < i < len(name) - 1) or (ch == "." and 0 < i <= 2):
            a, b = name[: i + 1], name[i + 1:]
        else:
            continue
        if a and b:
            out.append((a, b))
    return out


def _fit_name(draw: ImageDraw.ImageDraw, name: str,
              max_w: int = NAME_MAX_W) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Return the lines to draw and the font to draw them in.

    Prefers one line at a comfortable size; a long name is wrapped onto two
    lines when that lets it render bigger; a name that fits no other way is
    clipped with an ellipsis at the smallest size rather than spilling over.
    """
    single = _largest_fitting(draw, name, max_w, NAME_SIZES)
    if single is not None and single >= NAME_SINGLE_MIN:
        return [name], _font(single)

    best: tuple[int, list[str]] | None = None
    for a, b in _split_candidates(name):
        size = next(
            (s for s in NAME_SIZES
             if max(draw.textlength(a, font=_font(s)), draw.textlength(b, font=_font(s))) <= max_w),
            None,
        )
        if size is not None and (best is None or size > best[0]):
            best = (size, [a, b])

    if best is not None and (single is None or best[0] > single):
        return best[1], _font(best[0])
    if single is not None:
        return [name], _font(single)

    font = _font(NAME_SIZES[-1])
    return [_clip_to_width(draw, name, font, max_w)], font


def _clip_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                   max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 2 and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _fit_title(draw: ImageDraw.ImageDraw, text: str, max_w: int) -> tuple[str, ImageFont.FreeTypeFont]:
    sizes = tuple(range(FONT_TITLE_MAX, FONT_TITLE_MIN - 1, -2))
    size = _largest_fitting(draw, text, max_w, sizes)
    if size is not None:
        return text, _font(size)
    font = _font(FONT_TITLE_MIN)
    return _clip_to_width(draw, text, font, max_w), font


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _draw_card(draw: ImageDraw.ImageDraw, cx: int, cy: int, *,
               name: str, points: int, played: bool, captain: bool,
               vice: bool) -> None:
    """One player card centred on (cx, cy). Name dominates; points only if played."""
    x0, y0 = cx - CARD_W // 2, cy - CARD_H // 2
    x1, y1 = x0 + CARD_W, y0 + CARD_H

    if captain:
        draw.rounded_rectangle((x0 - RING, y0 - RING, x1 + RING, y1 + RING),
                               radius=CARD_RADIUS + RING, fill=CAPTAIN_RING)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=CARD_RADIUS,
                           fill=CARD_PLAYED if played else CARD_WAITING)

    # Name block, vertically centred in the card body. When the points chip is
    # showing it eats into the bottom edge, so the name sits a touch higher.
    lines, font = _fit_name(draw, name)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    block_h = line_h * len(lines)
    block_cy = cy - (CHIP_H // 4 if played else 0)
    colour = CARD_TEXT if played else CARD_TEXT_WAITING
    for i, line in enumerate(lines):
        ly = block_cy - block_h / 2 + line_h * (i + 0.5)
        draw.text((cx, ly), line, font=font, fill=colour, anchor="mm")

    if played:
        draw.rounded_rectangle(
            (cx - CHIP_W // 2, y1 - CHIP_H // 2, cx + CHIP_W // 2, y1 + CHIP_H // 2),
            radius=12, fill=PTS_BG,
        )
        draw.text((cx, y1), str(points), font=FONT_PTS, fill=PTS_TEXT, anchor="mm")

    marker = "C" if captain else ("V" if vice else "")
    if marker:
        bx, by = x1 - 6, y0 + 6
        fill, text = (CAPTAIN_RING, BADGE_DARK) if captain else (BADGE_DARK, HEADER_TEXT)
        draw.ellipse((bx - BADGE_R - 3, by - BADGE_R - 3, bx + BADGE_R + 3, by + BADGE_R + 3),
                     fill=LINE)
        draw.ellipse((bx - BADGE_R, by - BADGE_R, bx + BADGE_R, by + BADGE_R), fill=fill)
        draw.text((bx, by + 1), marker, font=FONT_BADGE, fill=text, anchor="mm")


def _row_centres(n: int) -> list[int]:
    """Horizontal centres for `n` cards in one row.

    Spread evenly across the pitch when there's room; a 5-wide row packs the
    cards with CARD_GAP between them instead so nothing overlaps.
    """
    if n <= 0:
        return []
    spacing = max(W // (n + 1), CARD_W + CARD_GAP)
    first = W / 2 - spacing * (n - 1) / 2
    return [int(first + spacing * i) for i in range(n)]


def _row_y(row_index: int) -> int:
    """Vertical centre for formation row `row_index` (0=GK .. 3=FWD)."""
    top = PITCH_TOP + 28 + CARD_H // 2
    bottom = PITCH_BOTTOM - 28 - CHIP_H // 2 - CARD_H // 2
    return int(top + (bottom - top) * row_index / (len(ELEMENT_ROWS) - 1))


def _draw_pitch(draw: ImageDraw.ImageDraw) -> None:
    stripes = 8
    stripe_h = (PITCH_BOTTOM - PITCH_TOP) / stripes
    for i in range(stripes):
        y0 = PITCH_TOP + stripe_h * i
        draw.rectangle((0, y0, W, y0 + stripe_h),
                       fill=GRASS_LIGHT if i % 2 == 0 else GRASS_DARK)

    inset = 14
    top, bottom = PITCH_TOP + inset, PITCH_BOTTOM - inset
    mid_y = (top + bottom) // 2
    draw.rectangle((inset, top, W - inset, bottom), outline=LINE, width=3)
    draw.line((inset, mid_y, W - inset, mid_y), fill=LINE, width=3)
    draw.ellipse((W // 2 - 90, mid_y - 90, W // 2 + 90, mid_y + 90), outline=LINE, width=3)
    draw.ellipse((W // 2 - 5, mid_y - 5, W // 2 + 5, mid_y + 5), fill=LINE)
    for box_top, box_bottom in ((top, top + 120), (bottom - 120, bottom)):
        draw.rectangle((W // 2 - 200, box_top, W // 2 + 200, box_bottom), outline=LINE, width=3)
    draw.rectangle((W // 2 - 90, top, W // 2 + 90, top + 44), outline=LINE, width=3)
    draw.rectangle((W // 2 - 90, bottom - 44, W // 2 + 90, bottom), outline=LINE, width=3)


def _draw_header(draw: ImageDraw.ImageDraw, manager: ManagerLive, event: int) -> None:
    draw.rectangle((0, 0, W, HEADER_H), fill=HEADER_BG)

    # Right: the running net GW score, big.
    unit_w = draw.textlength("pts", font=FONT_SCORE_UNIT)
    score = str(manager.net_points)
    draw.text((W - MARGIN, 70), "pts", font=FONT_SCORE_UNIT, fill=SUB_TEXT, anchor="rs")
    draw.text((W - MARGIN - unit_w - 10, 70), score, font=FONT_SCORE, fill=PTS_TEXT, anchor="rs")
    score_w = draw.textlength(score, font=FONT_SCORE) + unit_w + 10

    details = [f"GW{event}"]
    if manager.transfer_cost:
        details.append(f"−{manager.transfer_cost} hit")
    if manager.active_chip:
        details.append(CHIP_NAMES.get(manager.active_chip, manager.active_chip))
    draw.text((W - MARGIN, 94), "  ·  ".join(details), font=FONT_SUB, fill=SUB_TEXT, anchor="rm")

    # Left: team name, then the manager's own name underneath.
    max_w = int(W - 2 * MARGIN - score_w - 40)
    title, title_font = _fit_title(draw, manager.team_name, max_w)
    draw.text((MARGIN, 44), title, font=title_font, fill=HEADER_TEXT, anchor="lm")
    if manager.manager_name:
        sub = _clip_to_width(draw, manager.manager_name, FONT_SUB, max_w)
        draw.text((MARGIN, 90), sub, font=FONT_SUB, fill=SUB_TEXT, anchor="lm")


def _card_points(pick: Pick, lv: PlayerLive | None) -> int:
    """What the card shows: the player's points × the pick's multiplier, so a
    captain's card reads 2× and the XI adds up to the header score. Bench and
    subbed-out picks (multiplier 0) show the raw points."""
    raw = lv.effective_points if lv else 0
    return raw * max(pick.effective_multiplier, 1)


def render_pitch(
    manager: ManagerLive,
    players: dict[int, Player],
    live: dict[int, PlayerLive],
    *,
    event: int,
    league_name: str,
) -> bytes:
    """Render `manager`'s XI (+ bench strip) to PNG bytes.

    One view: the formation, each card dominated by the player's name, with a
    points chip shown only for players who have played. The header shows the
    team and manager name, the gameweek (plus any hit or chip) and the running
    net GW points. Pure: no network, no Telegram.
    """
    img = Image.new("RGB", (W, H), GRASS_DARK)
    draw = ImageDraw.Draw(img)

    _draw_pitch(draw)
    _draw_header(draw, manager, event)

    starters = sorted((p for p in manager.picks if p.position <= 11), key=lambda p: p.position)
    bench = sorted((p for p in manager.picks if p.position >= 12), key=lambda p: p.position)

    rows: dict[int, list[Pick]] = {et: [] for et in ELEMENT_ROWS}
    for p in starters:
        pl = players.get(p.element)
        rows.setdefault(pl.element_type if pl else 3, []).append(p)

    for ri, et in enumerate(ELEMENT_ROWS):
        line = rows.get(et, [])
        if not line:
            continue
        y = _row_y(ri)
        for cx, p in zip(_row_centres(len(line)), line, strict=True):
            pl = players.get(p.element)
            lv = live.get(p.element)
            _draw_card(
                draw, cx, y,
                name=pl.web_name if pl else "?",
                points=_card_points(p, lv),
                played=bool(lv and lv.minutes > 0),
                captain=p.is_captain,
                vice=p.is_vice_captain,
            )

    # Bench strip: a caption, then the four bench cards at the same size as the XI.
    draw.rectangle((0, PITCH_BOTTOM, W, H), fill=HEADER_BG)
    draw.line((MARGIN, PITCH_BOTTOM + 40, W - MARGIN, PITCH_BOTTOM + 40), fill=DIVIDER, width=2)
    draw.rectangle((W // 2 - 60, PITCH_BOTTOM + 26, W // 2 + 60, PITCH_BOTTOM + 54), fill=HEADER_BG)
    draw.text((W // 2, PITCH_BOTTOM + 40), "BENCH", font=FONT_BENCH_LABEL, fill=SUB_TEXT, anchor="mm")
    bench_y = PITCH_BOTTOM + 58 + CARD_H // 2
    for cx, p in zip(_row_centres(len(bench)), bench, strict=True):
        pl = players.get(p.element)
        lv = live.get(p.element)
        _draw_card(
            draw, cx, bench_y,
            name=pl.web_name if pl else "?",
            points=_card_points(p, lv),
            played=bool(lv and lv.minutes > 0),
            captain=False, vice=False,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
