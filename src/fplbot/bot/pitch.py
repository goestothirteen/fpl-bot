"""Draw a manager's gameweek XI on a football pitch as a PNG.

Telegram can't edit a text message into a photo, so the "Teams" view lives on
its own photo message (see handlers/teams.py). This module is the pure,
network-free, Telegram-free renderer: given one manager's picks plus the player
and live maps (and, optionally, pre-fetched shirt images), it returns PNG
bytes. Kept pure so it is unit-testable and so the handler layer stays thin.

Each pick is drawn the way the FPL app draws it: the club shirt, a name strip
and a points strip underneath. Shirts come in as PNG bytes keyed by
(team id, is_goalkeeper) — see bot/shirts.py for the fetch and cache — and a
pick with no shirt available gets a plain drawn one, so the view never breaks
on a missing asset.

Typography is the whole game here. Telegram shows the photo at roughly a third
of this size on a phone, so every size below is chosen for how it reads at
~350px wide. Player names are all set at ONE size: a name that doesn't fit is
wrapped onto two lines at a space, hyphen or leading initial, and a long single
word is condensed horizontally by up to a fifth before anything is allowed to
shrink. The points strip only shows a number once the player has actually
played (minutes > 0); a player yet to kick off shows a dash, not a misleading 0.

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

ShirtKey = tuple[int, bool]   # (team id, is goalkeeper)

# ---------------------------------------------------------------------------
# Geometry (pixels at render resolution; 1280 is Telegram's max photo side)
# ---------------------------------------------------------------------------
W, H = 1040, 1280
HEADER_H = 104
BENCH_H = 236
PITCH_TOP = HEADER_H
PITCH_BOTTOM = H - BENCH_H
MARGIN = 32

CARD_W = 190
CARD_GAP = 12            # minimum gap between cards in a packed (5-wide) row
SHIRT_H = 84             # XI shirt height; the bench uses BENCH_SHIRT_H
BENCH_SHIRT_H = 64
NAME_H = 78              # name strip: tall enough for two lines at NAME_SIZE
PTS_H = 38               # points strip
STRIP_RADIUS = 10
BADGE_R = 21             # captain / vice badge on the shirt's shoulder


def card_h(shirt_h: int) -> int:
    return shirt_h + NAME_H + PTS_H


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
GRASS_DARK = (24, 122, 52)
GRASS_LIGHT = (32, 138, 60)
LINE = (255, 255, 255)
NAME_BG = (250, 250, 250)          # player's fixture started/finished
NAME_BG_WAITING = (205, 212, 220)  # fixture not kicked off yet
NAME_TEXT = (16, 20, 24)
NAME_TEXT_WAITING = (70, 78, 88)
PTS_BG = (55, 0, 60)               # FPL purple
PTS_TEXT = (0, 255, 136)           # FPL green
PTS_BG_WAITING = (40, 46, 52)
PTS_TEXT_WAITING = (130, 140, 150)
CAPTAIN = (250, 196, 0)
BADGE_DARK = (12, 16, 20)
HEADER_BG = (12, 16, 20)
HEADER_TEXT = (255, 255, 255)
SUB_TEXT = (170, 180, 190)
DIVIDER = (40, 46, 52)
PLACEHOLDER_SHIRT = (228, 232, 236)
PLACEHOLDER_SHIRT_EDGE = (150, 158, 166)

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


FONT_TITLE_MAX = 38
FONT_TITLE_MIN = 26
FONT_SUB = _font(22)
FONT_SCORE = _font(60)
FONT_SCORE_UNIT = _font(24)
FONT_PTS = _font(28)
FONT_BADGE = _font(24)
FONT_BENCH_LABEL = _font(22)

# One size for every name. Long names wrap or condense rather than shrink;
# only a name that still won't fit steps down the (short) fallback ladder.
NAME_SIZE = 32
NAME_FALLBACK_SIZES = (28, 24)
NAME_MAX_W = CARD_W - 12
MAX_CONDENSE = 0.75           # never squeeze a line narrower than 75% of natural


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
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

    Everything is tried at NAME_SIZE first: one line, then a two-line split,
    then one line condensed by up to MAX_CONDENSE, then a condensed split.
    Only if none of that fits does the size step down. Lines may come back
    wider than `max_w` by up to the condense factor — _draw_line squeezes them.
    """
    limit = max_w / MAX_CONDENSE

    def width(text: str, font: ImageFont.FreeTypeFont) -> float:
        return draw.textlength(text, font=font)

    def best_split(font: ImageFont.FreeTypeFont, bound: float) -> list[str] | None:
        fits = [(a, b) for a, b in _split_candidates(name)
                if max(width(a, font), width(b, font)) <= bound]
        if not fits:
            return None
        a, b = min(fits, key=lambda ab: max(width(ab[0], font), width(ab[1], font)))
        return [a, b]

    for size in (NAME_SIZE, *NAME_FALLBACK_SIZES):
        font = _font(size)
        if width(name, font) <= max_w:
            return [name], font
        if (split := best_split(font, max_w)) is not None:
            return split, font
        if width(name, font) <= limit:
            return [name], font
        if (split := best_split(font, limit)) is not None:
            return split, font

    font = _font(NAME_FALLBACK_SIZES[-1])
    return [_clip_to_width(draw, name, font, int(limit))], font


def _clip_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                   max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 2 and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _fit_title(draw: ImageDraw.ImageDraw, text: str, max_w: int) -> tuple[str, ImageFont.FreeTypeFont]:
    for size in range(FONT_TITLE_MAX, FONT_TITLE_MIN - 1, -2):
        if draw.textlength(text, font=_font(size)) <= max_w:
            return text, _font(size)
    font = _font(FONT_TITLE_MIN)
    return _clip_to_width(draw, text, font, max_w), font


def _draw_line(img: Image.Image, cx: float, cy: float, text: str,
               font: ImageFont.FreeTypeFont, fill: tuple[int, int, int], max_w: int) -> None:
    """Draw `text` centred on (cx, cy); if it is wider than `max_w`, render it
    off-screen and squeeze it horizontally to fit (a cheap condensed face)."""
    draw = ImageDraw.Draw(img)
    natural = draw.textlength(text, font=font)
    if natural <= max_w:
        draw.text((cx, cy), text, font=font, fill=fill, anchor="mm")
        return
    ascent, descent = font.getmetrics()
    tw, th = int(natural) + 4, ascent + descent + 4
    layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((tw / 2, th / 2), text, font=font, fill=fill, anchor="mm")
    layer = layer.resize((max_w, th), Image.LANCZOS)
    img.paste(layer, (int(cx - max_w / 2), int(cy - th / 2)), layer)


# ---------------------------------------------------------------------------
# Shirts
# ---------------------------------------------------------------------------
def _placeholder_shirt(h: int) -> Image.Image:
    """A plain drawn jersey for when no club image is available."""
    w = int(h * 220 / 290)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    body = [
        (w * 0.30, 0), (w * 0.70, 0), (w * 0.78, h * 0.08), (w, h * 0.22),
        (w * 0.86, h * 0.40), (w * 0.76, h * 0.34), (w * 0.76, h), (w * 0.24, h),
        (w * 0.24, h * 0.34), (w * 0.14, h * 0.40), (0, h * 0.22), (w * 0.22, h * 0.08),
    ]
    d.polygon(body, fill=PLACEHOLDER_SHIRT, outline=PLACEHOLDER_SHIRT_EDGE, width=3)
    d.ellipse((w * 0.36, -h * 0.06, w * 0.64, h * 0.08), fill=(0, 0, 0, 0),
              outline=PLACEHOLDER_SHIRT_EDGE, width=3)
    return im


class _ShirtCache:
    """Decode + resize each shirt PNG once per render, not once per pick."""

    def __init__(self, shirts: dict[ShirtKey, bytes] | None) -> None:
        self._raw = shirts or {}
        self._done: dict[tuple[ShirtKey, int], Image.Image] = {}

    def get(self, key: ShirtKey, h: int) -> Image.Image:
        k = (key, h)
        if k not in self._done:
            png = self._raw.get(key)
            if png:
                try:
                    im = Image.open(io.BytesIO(png)).convert("RGBA")
                    w = max(1, round(im.width * h / im.height))
                    self._done[k] = im.resize((w, h), Image.LANCZOS)
                except OSError:
                    self._done[k] = _placeholder_shirt(h)
            else:
                self._done[k] = _placeholder_shirt(h)
        return self._done[k]


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _draw_card(img: Image.Image, shirts: _ShirtCache, cx: int, top: int, *,
               name: str, points: int, played: bool, captain: bool, vice: bool,
               shirt_key: ShirtKey, shirt_h: int) -> None:
    """One pick, FPL-app style, with its top edge at `top`: shirt, name strip,
    points strip. Name is drawn at the one shared size; points only if played."""
    draw = ImageDraw.Draw(img)
    x0, x1 = cx - CARD_W // 2, cx + CARD_W // 2

    shirt = shirts.get(shirt_key, shirt_h)
    img.paste(shirt, (cx - shirt.width // 2, top), shirt)

    # Name strip (rounded top), then the points strip (rounded bottom).
    ny0 = top + shirt_h
    ny1 = ny0 + NAME_H
    py1 = ny1 + PTS_H
    name_bg = NAME_BG if played else NAME_BG_WAITING
    pts_bg = PTS_BG if played else PTS_BG_WAITING
    draw.rounded_rectangle((x0, ny0, x1, py1), radius=STRIP_RADIUS, fill=pts_bg)
    draw.rounded_rectangle((x0, ny0, x1, ny1), radius=STRIP_RADIUS, fill=name_bg,
                           corners=(True, True, False, False))

    lines, font = _fit_name(draw, name)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    block_top = (ny0 + ny1) / 2 - line_h * len(lines) / 2
    colour = NAME_TEXT if played else NAME_TEXT_WAITING
    for i, line in enumerate(lines):
        _draw_line(img, cx, block_top + line_h * (i + 0.5), line, font, colour, NAME_MAX_W)

    label = str(points) if played else "–"
    draw.text((cx, (ny1 + py1) / 2), label, font=FONT_PTS,
              fill=PTS_TEXT if played else PTS_TEXT_WAITING, anchor="mm")

    marker = "C" if captain else ("V" if vice else "")
    if marker:
        bx, by = cx + shirt.width // 2, top + BADGE_R - 4
        fill, text = (CAPTAIN, BADGE_DARK) if captain else (BADGE_DARK, HEADER_TEXT)
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


def _row_top(row_index: int) -> int:
    """Top edge of formation row `row_index` (0=GK .. 3=FWD)."""
    first = PITCH_TOP + 22
    last = PITCH_BOTTOM - 22 - card_h(SHIRT_H)
    return int(first + (last - first) * row_index / (len(ELEMENT_ROWS) - 1))


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
    draw.text((W - MARGIN, 62), "pts", font=FONT_SCORE_UNIT, fill=SUB_TEXT, anchor="rs")
    draw.text((W - MARGIN - unit_w - 10, 62), score, font=FONT_SCORE, fill=PTS_TEXT, anchor="rs")
    score_w = draw.textlength(score, font=FONT_SCORE) + unit_w + 10

    details = [f"GW{event}"]
    if manager.transfer_cost:
        details.append(f"−{manager.transfer_cost} hit")
    if manager.active_chip:
        details.append(CHIP_NAMES.get(manager.active_chip, manager.active_chip))
    draw.text((W - MARGIN, 84), "  ·  ".join(details), font=FONT_SUB, fill=SUB_TEXT, anchor="rm")

    # Left: team name, then the manager's own name underneath.
    max_w = int(W - 2 * MARGIN - score_w - 40)
    title, title_font = _fit_title(draw, manager.team_name, max_w)
    draw.text((MARGIN, 40), title, font=title_font, fill=HEADER_TEXT, anchor="lm")
    if manager.manager_name:
        sub = _clip_to_width(draw, manager.manager_name, FONT_SUB, max_w)
        draw.text((MARGIN, 80), sub, font=FONT_SUB, fill=SUB_TEXT, anchor="lm")


def _card_points(pick: Pick, lv: PlayerLive | None) -> int:
    """What the card shows: the player's points × the pick's multiplier, so a
    captain's card reads 2× and the XI adds up to the header score. Bench and
    subbed-out picks (multiplier 0) show the raw points."""
    raw = lv.effective_points if lv else 0
    return raw * max(pick.effective_multiplier, 1)


def shirt_keys(manager: ManagerLive, players: dict[int, Player]) -> set[ShirtKey]:
    """Every (team id, is_gk) this manager's squad needs a shirt for."""
    out: set[ShirtKey] = set()
    for p in manager.picks:
        pl = players.get(p.element)
        if pl:
            out.add((pl.team, pl.element_type == 1))
    return out


def render_pitch(
    manager: ManagerLive,
    players: dict[int, Player],
    live: dict[int, PlayerLive],
    *,
    event: int,
    league_name: str,
    shirts: dict[ShirtKey, bytes] | None = None,
) -> bytes:
    """Render `manager`'s XI (+ bench strip) to PNG bytes.

    `shirts` maps (team id, is_goalkeeper) to PNG bytes; any pick without one
    gets a plain drawn shirt. The header shows the team and manager name, the
    gameweek (plus any hit or chip) and the running net GW points. Pure: no
    network, no Telegram.
    """
    img = Image.new("RGB", (W, H), GRASS_DARK)
    draw = ImageDraw.Draw(img)
    cache_ = _ShirtCache(shirts)

    _draw_pitch(draw)
    _draw_header(draw, manager, event)

    starters = sorted((p for p in manager.picks if p.position <= 11), key=lambda p: p.position)
    bench = sorted((p for p in manager.picks if p.position >= 12), key=lambda p: p.position)

    rows: dict[int, list[Pick]] = {et: [] for et in ELEMENT_ROWS}
    for p in starters:
        pl = players.get(p.element)
        rows.setdefault(pl.element_type if pl else 3, []).append(p)

    def card(p: Pick, cx: int, top: int, shirt_h: int, *, marks: bool) -> None:
        pl = players.get(p.element)
        lv = live.get(p.element)
        _draw_card(
            img, cache_, cx, top,
            name=pl.web_name if pl else "?",
            points=_card_points(p, lv),
            played=bool(lv and lv.minutes > 0),
            captain=marks and p.is_captain,
            vice=marks and p.is_vice_captain,
            shirt_key=(pl.team, pl.element_type == 1) if pl else (0, False),
            shirt_h=shirt_h,
        )

    for ri, et in enumerate(ELEMENT_ROWS):
        line = rows.get(et, [])
        if not line:
            continue
        top = _row_top(ri)
        for cx, p in zip(_row_centres(len(line)), line, strict=True):
            card(p, cx, top, SHIRT_H, marks=True)

    # Bench strip: a caption, then the four bench picks with a smaller shirt.
    draw.rectangle((0, PITCH_BOTTOM, W, H), fill=HEADER_BG)
    draw.line((MARGIN, PITCH_BOTTOM + 28, W - MARGIN, PITCH_BOTTOM + 28), fill=DIVIDER, width=2)
    draw.rectangle((W // 2 - 60, PITCH_BOTTOM + 14, W // 2 + 60, PITCH_BOTTOM + 42), fill=HEADER_BG)
    draw.text((W // 2, PITCH_BOTTOM + 28), "BENCH", font=FONT_BENCH_LABEL, fill=SUB_TEXT, anchor="mm")
    bench_top = PITCH_BOTTOM + 48
    for cx, p in zip(_row_centres(len(bench)), bench, strict=True):
        card(p, cx, bench_top, BENCH_SHIRT_H, marks=False)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
