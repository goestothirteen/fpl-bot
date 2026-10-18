"""Draw a manager's gameweek XI on a football pitch as a PNG.

Telegram can't edit a text message into a photo, so the "Teams" view lives on
its own photo message (see handlers/teams.py). This module is the pure,
network-free, Telegram-free renderer: given one manager's picks plus the player
and live maps, it returns PNG bytes. Kept pure so it is unit-testable and so the
handler layer stays thin.

Text is drawn with the DejaVu TrueType fonts that ship inside Pillow, so names
and points can be set at a real point size (the old built-in bitmap font could
not be enlarged, which left the cards tiny in a big green canvas). If DejaVu is
somehow unavailable we fall back to the bitmap font rather than crash.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from ..services.models import ManagerLive, Player, PlayerLive

# Pitch geometry (pixels). Drawn once at this resolution with scalable fonts —
# no post-scaling, so the text is genuinely large rather than an upscaled blur.
W, H = 960, 1180
BENCH_H = 190
PITCH_H = H - BENCH_H

GRASS_DARK = (24, 122, 52)
GRASS_LIGHT = (32, 138, 60)
LINE = (255, 255, 255)
CARD_PLAYED = (250, 250, 250)      # player's fixture started/finished
CARD_WAITING = (205, 212, 220)     # fixture not kicked off yet
CARD_TEXT = (16, 20, 24)
PTS_BG = (55, 0, 60)               # FPL purple
PTS_TEXT = (0, 255, 136)
CAPTAIN_RING = (250, 196, 0)
HEADER_BG = (12, 16, 20)
HEADER_TEXT = (255, 255, 255)
SUB_TEXT = (170, 180, 190)

CARD_W, CARD_H = 160, 104
HEADER_H = 92


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


FONT_TITLE = _load_font(36, bold=True)
FONT_SUB = _load_font(26)
FONT_NAME = _load_font(30, bold=True)
FONT_PTS = _load_font(40, bold=True)
FONT_PTS_SMALL = _load_font(28, bold=True)
FONT_BENCH = _load_font(24, bold=True)

ELEMENT_ROWS = (1, 2, 3, 4)   # GK, DEF, MID, FWD


def _text_centered(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str,
                   fill: tuple[int, int, int], font) -> None:  # noqa: ANN001
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w // 2, y), text, font=font, fill=fill)


def _clip(name: str, width: int = 11) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


def _draw_card(draw: ImageDraw.ImageDraw, cx: int, cy: int, *,
               name: str, points: int, played: bool, captain: bool,
               vice: bool, mode: str) -> None:
    """One player card centred on (cx, cy)."""
    x0, y0 = cx - CARD_W // 2, cy - CARD_H // 2
    x1, y1 = cx + CARD_W // 2, cy + CARD_H // 2
    body = CARD_PLAYED if played else CARD_WAITING

    if captain:
        draw.rounded_rectangle((x0 - 4, y0 - 4, x1 + 4, y1 + 4), radius=14,
                               fill=CAPTAIN_RING)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=12, fill=body)

    # Name strip.
    _text_centered(draw, cx, y0 + 10, _clip(name), CARD_TEXT, FONT_NAME)

    # Points strip — only shown once the player has actually played. For a
    # player still waiting on his fixture, showing "0" reads as a blank/duck
    # rather than "not started yet", so we omit the chip entirely and just mark
    # the captain/vice.
    if mode == "score":
        if played:
            pts_h = 50
            draw.rounded_rectangle((x0, y1 - pts_h, x1, y1), radius=12, fill=PTS_BG)
            _text_centered(draw, cx, y1 - pts_h + 5, f"{points}", PTS_TEXT, FONT_PTS)
        elif captain or vice:
            _text_centered(draw, cx, y1 - 38, "(C)" if captain else "(V)",
                           CARD_TEXT, FONT_PTS_SMALL)
    else:
        tag = ""
        if captain:
            tag = " (C)"
        elif vice:
            tag = " (V)"
        if played:
            pts_h = 34
            draw.rounded_rectangle((x0 + 22, y1 - pts_h, x1 - 22, y1 - 4), radius=9,
                                   fill=PTS_BG)
            _text_centered(draw, cx, y1 - pts_h + 2, f"{points}{tag}",
                           PTS_TEXT, FONT_PTS_SMALL)
        elif tag:
            _text_centered(draw, cx, y1 - 32, tag.strip(), CARD_TEXT, FONT_PTS_SMALL)


def _row_y(row_index: int, total_rows: int) -> int:
    """Vertical centre for formation row `row_index` (0=GK .. 3=FWD)."""
    top = HEADER_H + 36
    usable = PITCH_H - top - 24
    step = usable / total_rows
    return int(top + step * row_index + step / 2)


def render_pitch(
    manager: ManagerLive,
    players: dict[int, Player],
    live: dict[int, PlayerLive],
    *,
    mode: str,
    event: int,
    league_name: str,
) -> bytes:
    """Render `manager`'s XI (+ bench strip) to PNG bytes.

    `mode` is "pitch" (formation, points small) or "score" (points emphasised
    with a running GW total in the header). Pure: no network, no Telegram.
    """
    img = Image.new("RGB", (W, H), GRASS_DARK)
    draw = ImageDraw.Draw(img)

    # Mowed stripes.
    stripe_h = PITCH_H // 7
    for i in range(7):
        if i % 2 == 0:
            draw.rectangle((0, i * stripe_h, W, (i + 1) * stripe_h), fill=GRASS_LIGHT)

    # Pitch markings.
    mid_y = HEADER_H + (PITCH_H - HEADER_H) // 2
    draw.rectangle((10, HEADER_H + 10, W - 10, PITCH_H - 10), outline=LINE, width=3)
    draw.line((10, mid_y, W - 10, mid_y), fill=LINE, width=2)
    draw.ellipse((W // 2 - 80, mid_y - 80, W // 2 + 80, mid_y + 80), outline=LINE, width=2)
    # Penalty boxes.
    draw.rectangle((W // 2 - 150, HEADER_H + 10, W // 2 + 150, HEADER_H + 100),
                   outline=LINE, width=2)
    draw.rectangle((W // 2 - 150, PITCH_H - 100, W // 2 + 150, PITCH_H - 10),
                   outline=LINE, width=2)

    # Header.
    draw.rectangle((0, 0, W, HEADER_H), fill=HEADER_BG)
    title = f"{_clip(manager.team_name, 22)} · GW{event}"
    _text_centered(draw, W // 2, 14, title, HEADER_TEXT, FONT_TITLE)
    if mode == "score":
        sub = f"{manager.net_points} pts"
    else:
        sub = _clip(manager.manager_name, 26)
    _text_centered(draw, W // 2, 56, sub, SUB_TEXT, FONT_SUB)

    # Split picks: starters (1-11) by element_type into rows, bench (12-15).
    starters = sorted(
        (p for p in manager.picks if p.position <= 11),
        key=lambda p: p.position,
    )
    bench = sorted(
        (p for p in manager.picks if p.position >= 12),
        key=lambda p: p.position,
    )

    rows: dict[int, list] = {et: [] for et in ELEMENT_ROWS}
    for p in starters:
        pl = players.get(p.element)
        et = pl.element_type if pl else 3
        rows.setdefault(et, []).append(p)

    for ri, et in enumerate(ELEMENT_ROWS):
        line = rows.get(et, [])
        if not line:
            continue
        y = _row_y(ri, len(ELEMENT_ROWS))
        gap = W // (len(line) + 1)
        for ci, p in enumerate(line):
            cx = gap * (ci + 1)
            pl = players.get(p.element)
            lv = live.get(p.element)
            _draw_card(
                draw, cx, y,
                name=pl.web_name if pl else "?",
                points=lv.effective_points if lv else 0,
                played=bool(lv and lv.minutes > 0),
                captain=p.is_captain,
                vice=p.is_vice_captain,
                mode=mode,
            )

    # Bench strip.
    draw.rectangle((0, PITCH_H, W, H), fill=HEADER_BG)
    _text_centered(draw, 60, PITCH_H + BENCH_H // 2 - 10, "BENCH", SUB_TEXT, FONT_BENCH)
    if bench:
        gap = (W - 130) // (len(bench) + 1)
        for ci, p in enumerate(bench):
            cx = 130 + gap * (ci + 1)
            pl = players.get(p.element)
            lv = live.get(p.element)
            _draw_card(
                draw, cx, PITCH_H + BENCH_H // 2,
                name=pl.web_name if pl else "?",
                points=lv.effective_points if lv else 0,
                played=bool(lv and lv.minutes > 0),
                captain=False, vice=False, mode=mode,
            )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
