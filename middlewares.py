from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser

from config import config
from db import Session, User, get_or_create_user

log = logging.getLogger(__name__)

DENY_TEXT = (
    "<b>Доступ закрыт</b>\n\n"
    "Бот работает только для сотрудников, состоящих в рабочем чате. Если вы там есть, "
    "но доступа нет, попросите администратора выдать его вручную: ему понадобится ваш "
    "идентификатор, его покажет команда /id."
)
# алерт callback-запроса: максимум 200 символов и без HTML,
# длинный DENY_TEXT туда не влезает — Telegram вернёт Bad Request
DENY_ALERT = (
    "Доступ закрыт: бот только для сотрудников. Отправьте боту /id и передайте "
    "идентификатор администратору."
)
ALLOWED_STATUSES = {"creator", "administrator", "member", "restricted"}


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with Session() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                # упавший хендлер не должен дописывать в базу половину изменений
                if session.in_transaction():
                    await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()
            return result


class AccessMiddleware(BaseMiddleware):
    """Пускает разработчиков, админов, одобренных и участников рабочего чата."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple[float, bool]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None

        session = data["session"]
        user: User = await get_or_create_user(session, tg_user)
        data["user"] = user

        if user.is_blocked:
            return None

        # мастер и получатель отчётов по починке заходят без отдельного одобрения;
        # если REPAIR_CHAT_ID — группа, кнопки заявки могут жать её участники
        chat = data.get("event_chat")
        allowed = (
            user.is_staff
            or user.is_approved
            or user.id in (config.repair_chat_id, config.report_chat_id)
            or (chat is not None and chat.id == config.repair_chat_id)
        )
        if not allowed:
            allowed = await self._in_work_chat(data["bot"], tg_user.id)
            if allowed:
                user.is_approved = True

        if not allowed:
            await session.commit()
            if isinstance(event, Message):
                await event.answer(DENY_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(DENY_ALERT, show_alert=True)
            return None

        return await handler(event, data)

    async def _in_work_chat(self, bot: Bot, user_id: int) -> bool:
        if config.work_chat_id is None:
            return False
        cached = self._cache.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            member = await bot.get_chat_member(config.work_chat_id, user_id)
            ok = member.status in ALLOWED_STATUSES
        except Exception as exc:
            log.warning("get_chat_member failed for %s: %s", user_id, exc)
            ok = False
        self._cache[user_id] = (time.monotonic() + config.access_cache_ttl, ok)
        return ok

    def invalidate(self, user_id: int) -> None:
        self._cache.pop(user_id, None)
