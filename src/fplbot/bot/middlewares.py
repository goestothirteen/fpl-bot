"""Dispatcher middlewares: rate limiting and chat context."""
from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..config import get_settings
from ..db import repo
from ..db.session import session_scope


class ThrottleMiddleware(BaseMiddleware):
    """3 commands per 10 s per user. Silently drops beyond that — replying
    'slow down' to a spammer just doubles the traffic."""

    def __init__(self, limit: int = 3, window: float = 10.0) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[int, list[float]] = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            hits = [t for t in self._hits[user.id] if now - t < self._window]
            if len(hits) >= self._limit:
                self._hits[user.id] = hits
                return None
            hits.append(now)
            self._hits[user.id] = hits
        return await handler(event, data)


class ChatContextMiddleware(BaseMiddleware):
    """Upserts the chat row and injects the chat's default league."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat

        if chat is not None:
            settings = get_settings()
            async with session_scope() as s:
                row = await repo.upsert_chat(
                    s, chat.id, chat.type, chat.title or chat.full_name or None,
                    settings.default_timezone,
                )
                data["chat_row"] = row
                data["default_league"] = await repo.default_league(s, chat.id)
        return await handler(event, data)
