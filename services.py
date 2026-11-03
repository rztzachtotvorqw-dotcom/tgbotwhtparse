from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import config
from db import PENDING, Notice, Order, Role, User
from keyboards import announce_kb, main_menu
from views import render_announce

log = logging.getLogger(__name__)


async def edit(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Повторное нажатие той же кнопки не должно ломать интерфейс.

    Telegram отвечает 'message is not modified', если текст и клавиатура не изменились —
    это не ошибка. Если сообщение отредактировать нельзя (старое, удалено, это фото),
    присылаем новое.
    """
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        log.info("edit failed: %s", exc)
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def edit_by_id(
    bot: Bot, chat_id: int, message_id: int | None, text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> bool:
    """Правит сообщение по id; False — если не вышло и нужно прислать новое."""
    if not message_id:
        return False
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=kb, disable_web_page_preview=True,
        )
        return True
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return True
        log.info("edit_by_id %s failed: %s", message_id, exc)
        return False
    except TelegramAPIError as exc:
        log.info("edit_by_id %s failed: %s", message_id, exc)
        return False


async def try_delete(message: Message) -> None:
    """Убираем ответ пользователя, чтобы в чате оставалась одна аккуратная карточка."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, disable_web_page_preview=True, **kwargs)
    except TelegramAPIError as exc:
        log.warning("send to %s failed: %s", chat_id, exc)
        return None


# ------------------------------------------------------------------ администраторы


async def staff_ids(session: AsyncSession) -> list[int]:
    """Все, кто получает служебные уведомления: администраторы и разработчики
    из базы, разработчики из .env и ADMIN_CHAT_ID. У всех одинаковые права."""
    rows = await session.scalars(
        select(User.id).where(User.role.in_((Role.admin, Role.developer)), User.is_blocked.is_(False))
    )
    ids = set(rows.all()) | set(config.developer_ids) | {config.admin_chat_id}
    return sorted(ids)


async def notify_staff(
    bot: Bot, session: AsyncSession, text: str, exclude: int | None = None, **kwargs
) -> list[Message]:
    sent = []
    for chat_id in await staff_ids(session):
        if chat_id == exclude:
            continue
        msg = await safe_send(bot, chat_id, text, **kwargs)
        if msg is not None:
            sent.append(msg)
    return sent


async def announce_order(bot: Bot, session: AsyncSession, order: Order) -> None:
    """Опубликован заказ: уведомление уходит каждому администратору и разработчику.
    Автору — нет, даже если он сам админ."""
    big = order.total is not None and order.total >= config.big_order_threshold
    sent = await notify_staff(
        bot, session, render_announce(order, big=big),
        exclude=order.user_id, reply_markup=announce_kb(order),
    )
    for msg in sent:
        order.notices.append(Notice(chat_id=msg.chat.id, message_id=msg.message_id))
    await session.commit()


def is_notice(order: Order, chat_id: int, message_id: int) -> bool:
    """Нажали кнопку прямо на уведомлении о заказе?"""
    if order.announce_chat_id == chat_id and order.announce_message_id == message_id:
        return True
    return any(n.chat_id == chat_id and n.message_id == message_id for n in order.notices)


async def refresh_announce(bot: Bot, order: Order) -> None:
    """Держим уведомления у всех админов актуальными: заказано / приехал / отменён."""
    text, kb = render_announce(order), announce_kb(order)
    targets = [(n.chat_id, n.message_id) for n in order.notices]
    if order.announce_chat_id and order.announce_message_id:  # уведомления версии 1.0
        targets.append((order.announce_chat_id, order.announce_message_id))
    for chat_id, message_id in targets:
        await edit_by_id(bot, chat_id, message_id, text, kb)


# ------------------------------------------------------------------ счётчик «В пути»


async def pending_count(session: AsyncSession, user_id: int) -> int:
    return await session.scalar(
        select(func.count(Order.id)).where(Order.user_id == user_id, Order.status.in_(PENDING))
    ) or 0


async def menu_for(session: AsyncSession, user: User) -> ReplyKeyboardMarkup:
    """Нижнее меню со свежим счётчиком «В пути · N»."""
    return main_menu(user.is_staff, await pending_count(session, user.id))


async def menu_for_id(session: AsyncSession, user_id: int) -> ReplyKeyboardMarkup:
    user = await session.get(User, user_id)
    return main_menu(bool(user and user.is_staff), await pending_count(session, user_id))
