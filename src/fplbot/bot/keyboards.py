from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def live_views(league_id: int, event: int, active: str = "live") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    views = [("GW", "live"), ("Season", "season"), ("Left", "left"),
             ("🔮 Edge", "edge"), ("Captains", "caps"), ("Diffs", "diff"),
             ("Bench", "bench"), ("💰 Wager", "wager")]
    for label, key in views:
        text = f"• {label}" if key == active else label
        b.button(text=text, callback_data=f"v:{key}:{league_id}:{event}")
    b.button(text="🔄 Refresh", callback_data=f"v:{active}:{league_id}:{event}:r")
    b.adjust(3, 3, 2, 1)
    return b.as_markup()


def league_picker(leagues: list[tuple[int, str]], action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for lid, name in leagues:
        b.button(text=name[:28], callback_data=f"{action}:{lid}")
    b.adjust(1)
    return b.as_markup()


def confirm(action: str, payload: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes", callback_data=f"{action}:yes:{payload}"),
            InlineKeyboardButton(text="✖️ No", callback_data=f"{action}:no:{payload}"),
        ]]
    )
