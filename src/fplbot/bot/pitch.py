"""Draw a manager's gameweek XI on a football pitch as a PNG.

Telegram can't edit a text message into a photo, so the "Teams" view lives on
its own photo message (see handlers/teams.py). This module is the pure,
network-free, Telegram-free renderer: given one manager's picks plus the player
and live maps, it returns PNG bytes. Kept pure so it is unit-testable and so the
handler layer stays thin.

Uses Pillow's built-in bitmap font (ImageFont.load_default) rather than a
TrueType file, because the container ships no font files and we don't want to
add any. The default font is small, so cards are sized around it.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from ..services.models import ManagerLive, Player, PlayerLive

# Pitch geometry (pixels).
W, H = 760, 1040
BENCH_H = 150
PITCH_H = H - BENCH_H

GRASS_DARK = (24, 122, 52)
GRASS_LIGHT = (32, 138, 60)
LINE = (255, 255, 255)
CARD_PLAYED = (250, 250, 250)      # player's fixture started/finished
CARD_WAITING = (210, 216, 222)     # fixture not kicked off yet
CARD_TEXT = (16, 20, 24)
PTS_BG = (55, 0, 60)               # FPL purple
PTS_TEXT = (0, 255, 136)
CAPTAIN_RING = (250, 196, 0)
HEADER_BG = (12, 16, 20)
HEADER_TEXT = (255, 255, 255)

CARD_W, CARD_H = 112, 66
HEADER_H = 64

ELEMENT_ROWS = (1, 2, 3, 4)   # GK, DEF, MID, FWD

_font = ImageFont.load_default()


def _text_centered(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str,
                    fill: tuple[int, int, int]) -> None:
    bbox = draw.textbbox((0, 0), text, font=_font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w // 2, y), text, font=_font, fill=fill)


def _clip(name: str, width: int = 14) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


def _draw_card(draw: ImageDraw.ImageDraw, cx: int, cy: int, *,
               name: str, points: int, played: bool, captain: bool,
               vice: bool, mode: str) -> None:
    """One player card centred on (cx, cy)."""
    x0, y0 = cx - CARD_W // 2, cy - CARD_H // 2
    x1, y1 = cx + CARD_W // 2, cy + CARD_H // 2
    body = CARD_PLAYED if played else CARD_WAITING

    if captain:
        draw.rounded_rectangle((x0 - 3, y0 - 3, x1 + 3, y1 + 3), radius=10,
                               fill=CAPTAIN_RING)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=body)

    # Name strip.
    _text_centered(draw, cx, y0 + 6, _clip(name), CARD_TEXT)

    # Points strip — emphasised in GW-score mode.
    if mode == "score":
        pts_h = 30
        draw.rounded_rectangle((x0, y1 - pts_h, x1, y1), radius=8, fill=PTS_BG)
        _text_centered(draw, cx, y1 - pts_h + 6, f"{points}", PTS_TEXT)
    else:
        tag = ""
        if captain:
            tag = " (C)"
        elif vice:
            tag = " (V)"
        draw.rounded_rectangle((x0 + 18, y1 - 24, x1 - 18, y1 - 2), radius=6,
                               fill=PTS_BG)
        _text_centered(draw, cx, y1 - 23, f"{points}{tag}", PTS_TEXT)


def _row_y(row_index: int, total_rows: int) -> int:
    """Vertical centre for formation row `row_index` (0=GK .. 3=FWD)."""
    top = HEADER_H + 40
    usable = PITCH_H - top - 30
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
    draw.rectangle((10, HEADER_H + 10, W - 10, PITCH_H - 10), outline=LINE, width=3)
    draw.line((10, PITCH_H // 2 + HEADER_H // 2, W - 10, PITCH_H // 2 + HEADER_H // 2),
              fill=LINE, width=2)
    draw.ellipse((W // 2 - 70, (PITCH_H + HEADER_H) // 2 - 70,
                  W // 2 + 70, (PITCH_H + HEADER_H) // 2 + 70), outline=LINE, width=2)
    # Penalty boxes.
    draw.rectangle((W // 2 - 130, HEADER_H + 10, W // 2 + 130, HEADER_H + 90),
                   outline=LINE, width=2)
    draw.rectangle((W // 2 - 130, PITCH_H - 90, W // 2 + 130, PITCH_H - 10),
                   outline=LINE, width=2)

    # Header.
    draw.rectangle((0, 0, W, HEADER_H), fill=HEADER_BG)
    title = f"{_clip(manager.team_name, 24)} · GW{event}"
    _text_centered(draw, W // 2, 10, title, HEADER_TEXT)
    if mode == "score":
        sub = f"{manager.net_points} pts"
    else:
        sub = _clip(manager.manager_name, 28)
    _text_centered(draw, W // 2, 36, sub, (170, 180, 190))

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
    _text_centered(draw, 48, PITCH_H + BENCH_H // 2 - 8, "BENCH", (170, 180, 190))
    if bench:
        gap = (W - 110) // (len(bench) + 1)
        for ci, p in enumerate(bench):
            cx = 110 + gap * (ci + 1)
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
