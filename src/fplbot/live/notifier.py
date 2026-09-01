"""Outbound message queue.

Telegram allows roughly 20 messages a minute to a group. A 6-goal thriller can
generate more events than that, so alerts are batched into one message per
window per chat and the sender paces itself. A bot that 429s itself during the
best 10 minutes of the season is worse than one that summarises.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from ..db import repo
from ..db.session import session_scope
from ..logging_conf import get_logger

log = get_logger(__name__)

MIN_IMPORTANCE = {"all": 1, "big-moments": 3, "digest-only": 99, "off": 99}


@dataclass(slots=True)
class Outbound:
    chat_id: int
    text: str
    key: str
    importance: int
    thread_id: int | None = None   # forum topic to post into, if the chat pins one


class Notifier:
    def __init__(self, bot: Bot, batch_window: float = 60.0, per_chat_delay: float = 1.0) -> None:
        self.bot = bot
        self._queue: asyncio.Queue[Outbound] = asyncio.Queue()
        self._batch_window = batch_window
        self._per_chat_delay = per_chat_delay
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def enqueue(self, item: Outbound) -> None:
        await self._queue.put(item)

    @staticmethod
    def in_quiet_hours(
        tz_name: str, quiet_from: int, quiet_to: int, at: datetime | None = None
    ) -> bool:
        """`at` is injectable so the wrap-past-midnight case can be tested."""
        now = (at or datetime.now(UTC)).astimezone(ZoneInfo(tz_name)).hour
        if quiet_from == quiet_to:
            return False
        if quiet_from < quiet_to:
            return quiet_from <= now < quiet_to
        return now >= quiet_from or now < quiet_to   # window crosses midnight

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            batch: list[Outbound] = [first]
            deadline = asyncio.get_running_loop().time() + self._batch_window
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except TimeoutError:
                    break

            by_chat: dict[int, list[Outbound]] = defaultdict(list)
            for item in batch:
                by_chat[item.chat_id].append(item)

            for chat_id, items in by_chat.items():
                text = "\n".join(i.text for i in items[:12])
                if len(items) > 12:
                    text += f"\n<i>…and {len(items) - 12} more</i>"
                await self._send(chat_id, text, items[0].thread_id)
                await asyncio.sleep(self._per_chat_delay)

    async def _send(self, chat_id: int, text: str, thread_id: int | None = None) -> None:
        for _ in range(3):
            try:
                await self.bot.send_message(
                    chat_id, text, disable_web_page_preview=True,
                    message_thread_id=thread_id,
                )
                return
            except TelegramRetryAfter as exc:
                log.warning("notifier.rate_limited", chat=chat_id, retry_after=exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except Exception as exc:  # noqa: BLE001 - a dead chat must not kill the loop
                log.warning("notifier.send_failed", chat=chat_id, error=str(exc))
                return

    async def dispatch(self, chat, events, *, force: bool = False) -> None:  # noqa: ANN001
        """Filter by the chat's alert profile and quiet hours, dedupe, enqueue."""
        threshold = MIN_IMPORTANCE.get(chat.alert_profile, 3)
        if self.in_quiet_hours(chat.timezone, chat.quiet_from, chat.quiet_to) and not force:
            return
        for ev in events:
            if ev.importance < threshold and not force:
                continue
            async with session_scope() as s:
                fresh = await repo.claim_alert(s, chat.id, ev.key)
            if fresh:
                await self.enqueue(
                    Outbound(
                        chat_id=chat.id, text=ev.text, key=ev.key,
                        importance=ev.importance, thread_id=repo.topic_of(chat),
                    )
                )
