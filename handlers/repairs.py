from __future__ import annotations

"""Заявки на починку.

Сотрудник → «Заявка на помощь» → описание (можно с фото) → место → «Отправить мастеру».
Мастер (REPAIR_CHAT_ID) получает заявку с кнопками «Взял / Сделано / Отклонить».
Когда сделано — заявитель ставит оценку 1–5 (и может вернуть «не решено»),
а отчёт с оценкой уходит в REPORT_CHAT_ID — это другой человек.
"""

import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import offices
from config import config
from db import Repair, RepairStatus, User
from keyboards import (
    BTN_REPAIR,
    BTN_REPAIR_OLD,
    REJECT_REASONS,
    RepCb,
    rate_kb,
    repair_card_kb,
    repair_draft_kb,
    repair_master_kb,
    repair_reject_kb,
    skip_comment_kb,
)
from screens import (
    can_handle_repairs,
    can_view_repairs,
    screen_repair,
    screen_repairs,
    screen_repairs_all,
)
from services import edit, edit_by_id, safe_send
from states import RepairSG
from utils import esc, now, plural, today
from views import DONE, DOT, OFF, RULER, STARS, render_repair, render_report

log = logging.getLogger(__name__)
router = Router(name="repairs")

NEW_PROMPT = (
    "<b>Новая заявка на помощь</b>\n\n"
    "Опишите одним сообщением, что случилось и где именно. Можно прислать фото "
    "с подписью — так мастеру будет понятнее.\n\n/cancel — передумали."
)


# ---------------------------------------------------------------- вход


@router.message(F.chat.type == "private", F.text.in_({BTN_REPAIR, *BTN_REPAIR_OLD}))
@router.message(F.chat.type == "private", Command("repair", "repairs", "support"))
async def btn_repair(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    text, kb = await screen_repairs(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(RepCb.filter(F.a == "my"))
async def cb_my(call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    text, kb = await screen_repairs(session, user)
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(RepCb.filter(F.a == "all"))
async def cb_all(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not can_view_repairs(user):
        await call.answer("Список всех заявок видят мастер и администраторы", show_alert=True)
        return
    text, kb = await screen_repairs_all(session)
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(RepCb.filter(F.a == "open"))
async def cb_open(call: CallbackQuery, callback_data: RepCb, session: AsyncSession, user: User) -> None:
    r = await session.get(Repair, callback_data.id)
    if r is not None and _is_worker_msg(call, r):
        # «Не отклонять» на сообщении мастера — возвращаем его кнопки
        await edit(call.message, render_repair(r), repair_master_kb(r))
    else:
        text, kb = await screen_repair(session, user, callback_data.id)
        await edit(call.message, text, kb)
    await call.answer()


# ---------------------------------------------------------------- создание


@router.callback_query(RepCb.filter(F.a == "new"))
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RepairSG.text)
    await state.update_data(photo_id=None)
    await edit(call.message, NEW_PROMPT)
    await call.answer()


@router.message(F.chat.type == "private", StateFilter(RepairSG.text), F.photo)
async def repair_photo(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    photo_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    if len(caption) < 3:
        await state.update_data(photo_id=photo_id)
        await message.answer("Фото есть. Теперь опишите словами, что сломалось и где.")
        return
    await _create(message, state, session, user, caption, photo_id)


@router.message(F.chat.type == "private", StateFilter(RepairSG.text), F.text,
                ~F.text.startswith("/"))
async def repair_text(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Опишите чуть подробнее: что сломалось и где.")
        return
    photo_id = (await state.get_data()).get("photo_id")
    await _create(message, state, session, user, text, photo_id)


async def _create(message: Message, state: FSMContext, session: AsyncSession, user: User,
                  text: str, photo_id: str | None) -> None:
    await state.clear()
    r = Repair(user_id=user.id, text=text[:2000], photo_id=photo_id, place=user.office,
               status=RepairStatus.draft)
    session.add(r)
    await session.flush()
    r.user = user
    await session.commit()
    await message.answer(
        render_repair(r) + "\n\nПроверьте место и нажмите «Отправить мастеру».",
        reply_markup=repair_draft_kb(r),
    )


async def _own_draft(call: CallbackQuery, session: AsyncSession, user: User, rid: int) -> Repair | None:
    r = await session.get(Repair, rid)
    if r is None or r.user_id != user.id or r.status is not RepairStatus.draft:
        await call.answer("Заявка уже отправлена или удалена", show_alert=True)
        return None
    return r


@router.callback_query(RepCb.filter(F.a == "place"))
async def cb_place(call: CallbackQuery, callback_data: RepCb, session: AsyncSession, user: User) -> None:
    r = await _own_draft(call, session, user, callback_data.id)
    if not r:
        return
    codes = list(offices.OFFICES)
    if 0 <= callback_data.v < len(codes):
        r.place = codes[callback_data.v]
        await session.commit()
    await edit(call.message, render_repair(r) + "\n\nПроверьте место и нажмите «Отправить мастеру».",
               repair_draft_kb(r))
    await call.answer(offices.label(r.place))


@router.callback_query(RepCb.filter(F.a == "drop"))
async def cb_drop(call: CallbackQuery, callback_data: RepCb, session: AsyncSession, user: User) -> None:
    r = await _own_draft(call, session, user, callback_data.id)
    if not r:
        return
    await session.delete(r)
    await session.commit()
    await edit(call.message, f"{OFF} Заявка удалена.")
    await call.answer()


@router.callback_query(RepCb.filter(F.a == "send"))
async def cb_send(call: CallbackQuery, callback_data: RepCb, session: AsyncSession,
                  user: User, bot: Bot) -> None:
    r = await _own_draft(call, session, user, callback_data.id)
    if not r:
        return
    r.status = RepairStatus.new
    r.sent_at = now()
    await session.commit()

    chat = config.repair_chat_id
    if r.photo_id:
        try:
            await bot.send_photo(chat, r.photo_id, caption=f"Фото к заявке {r.code}")
        except TelegramAPIError as exc:
            log.warning("фото заявки %s не ушло: %s", r.id, exc)
    msg = await safe_send(bot, chat, render_repair(r, f"○ <b>Новая заявка на помощь {r.code}</b>"),
                          reply_markup=repair_master_kb(r))
    if msg is not None:
        r.worker_chat_id, r.worker_msg_id = msg.chat.id, msg.message_id
        await session.commit()
    await edit(call.message, render_repair(r) + "\n\nЗаявка у мастера. Когда он возьмёт её "
               "в работу и закончит — пришлю сообщение.", repair_card_kb(r, user, can_handle_repairs(user)))
    await call.answer("Отправлено мастеру")


# ---------------------------------------------------------------- заявитель


@router.callback_query(RepCb.filter(F.a == "cancel"))
async def cb_cancel(call: CallbackQuery, callback_data: RepCb, session: AsyncSession,
                    user: User, bot: Bot) -> None:
    r = await session.get(Repair, callback_data.id)
    if r is None or r.user_id != user.id or not r.is_open:
        await call.answer("Отозвать можно только свою открытую заявку", show_alert=True)
        return
    r.status = RepairStatus.cancelled
    await session.commit()
    await _refresh_worker(bot, r)
    await safe_send(bot, config.repair_chat_id, f"{OFF} Заявку {r.code} отозвал заявитель — "
                    f"{esc(user.display)}.")
    await edit(call.message, render_repair(r), repair_card_kb(r, user, can_handle_repairs(user)))
    await call.answer("Заявка отозвана")


@router.callback_query(RepCb.filter(F.a == "rate"))
async def cb_rate(call: CallbackQuery, callback_data: RepCb, state: FSMContext,
                  session: AsyncSession, user: User, bot: Bot) -> None:
    r = await session.get(Repair, callback_data.id)
    if r is None or r.user_id != user.id or r.status is not RepairStatus.done:
        await call.answer("Оценить можно только свою выполненную заявку", show_alert=True)
        return
    if not 1 <= callback_data.v <= 5:
        await call.answer()
        return
    r.rating = callback_data.v
    await session.commit()
    await _update_report(bot, session, r)
    await safe_send(bot, config.repair_chat_id,
                    f"Оценка по заявке {r.code}: {STARS[r.rating]} {r.rating}/5")
    await state.set_state(RepairSG.comment)
    await state.update_data(repair_id=r.id)
    await edit(call.message,
               f"Спасибо! Оценка {STARS[r.rating]} {r.rating}/5 сохранена.\n\n"
               "Хотите добавить пару слов? Напишите одним сообщением — передам вместе с оценкой.",
               skip_comment_kb(r))
    await call.answer("Оценка сохранена")


@router.message(F.chat.type == "private", StateFilter(RepairSG.comment), F.text,
                ~F.text.startswith("/"))
async def rate_comment(message: Message, state: FSMContext, session: AsyncSession,
                       user: User, bot: Bot) -> None:
    rid = (await state.get_data()).get("repair_id")
    await state.clear()
    r = await session.get(Repair, int(rid or 0))
    if r is None or r.user_id != user.id:
        return
    r.rating_comment = (message.text or "").strip()[:1000]
    await session.commit()
    await _update_report(bot, session, r)
    await message.answer("Спасибо, отзыв передан.")


@router.callback_query(RepCb.filter(F.a == "skip"))
async def cb_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit(call.message, "Спасибо за оценку!")
    await call.answer()


@router.callback_query(RepCb.filter(F.a == "back"))
async def cb_back(call: CallbackQuery, callback_data: RepCb, session: AsyncSession,
                  user: User, bot: Bot) -> None:
    """«Не решено» — заявка снова у мастера."""
    r = await session.get(Repair, callback_data.id)
    if r is None or r.user_id != user.id or r.status is not RepairStatus.done:
        await call.answer("Вернуть можно только свою выполненную заявку", show_alert=True)
        return
    r.status = RepairStatus.in_work
    r.done_at = None
    r.rating = None
    r.rating_comment = None
    r.returned = (r.returned or 0) + 1
    await session.commit()
    await _refresh_worker(bot, r)
    msg = await safe_send(
        bot, config.repair_chat_id,
        render_repair(r, f"◐ <b>Заявка {r.code} возвращена: не решено</b>"),
        reply_markup=repair_master_kb(r),
    )
    if msg is not None:
        r.worker_chat_id, r.worker_msg_id = msg.chat.id, msg.message_id
        await session.commit()
    await _update_report(bot, session, r)
    await edit(call.message, render_repair(r) + "\n\nЗаявка снова у мастера.",
               repair_card_kb(r, user, can_handle_repairs(user)))
    await call.answer("Вернул мастеру")


# ---------------------------------------------------------------- мастер


def _is_worker_msg(call: CallbackQuery, r: Repair) -> bool:
    return (call.message is not None and r.worker_msg_id == call.message.message_id
            and r.worker_chat_id == call.message.chat.id)


def _may_handle(call: CallbackQuery, user: User) -> bool:
    # REPAIR_CHAT_ID может быть и группой — тогда кнопки жмут её участники
    in_repair_chat = call.message is not None and call.message.chat.id == config.repair_chat_id
    return can_handle_repairs(user) or in_repair_chat


async def _refresh_worker(bot: Bot, r: Repair) -> None:
    if r.worker_chat_id and r.worker_msg_id:
        await edit_by_id(bot, r.worker_chat_id, r.worker_msg_id, render_repair(r), repair_master_kb(r))


async def _after_master_action(call: CallbackQuery, bot: Bot, r: Repair, user: User) -> None:
    """Обновляем сообщение мастера и карточку, на которой нажали (если это не оно)."""
    await _refresh_worker(bot, r)
    if not _is_worker_msg(call, r):
        await edit(call.message, render_repair(r), repair_card_kb(r, user, True))


async def _handled(call: CallbackQuery, session: AsyncSession, user: User, rid: int) -> Repair | None:
    if not _may_handle(call, user):
        await call.answer("Это кнопки мастера", show_alert=True)
        return None
    r = await session.get(Repair, rid)
    if r is None or not r.is_open:
        await call.answer("Заявка уже закрыта", show_alert=True)
        return None
    return r


@router.callback_query(RepCb.filter(F.a == "take"))
async def cb_take(call: CallbackQuery, callback_data: RepCb, session: AsyncSession,
                  user: User, bot: Bot) -> None:
    r = await _handled(call, session, user, callback_data.id)
    if not r:
        return
    r.status = RepairStatus.in_work
    r.taken_at = now()
    r.taken_by = user.id
    await session.commit()
    await _after_master_action(call, bot, r, user)
    await call.answer("Взято в работу")
    await safe_send(bot, r.user_id, f"◐ <b>Заявка {r.code} в работе</b>\n"
                                    f"Мастер {esc(user.display)} взял её.")


@router.callback_query(RepCb.filter(F.a == "done"))
async def cb_done(call: CallbackQuery, callback_data: RepCb, session: AsyncSession,
                  user: User, bot: Bot) -> None:
    r = await _handled(call, session, user, callback_data.id)
    if not r:
        return
    r.status = RepairStatus.done
    r.done_at = now()
    r.taken_at = r.taken_at or r.done_at
    r.taken_by = user.id
    await session.commit()
    await _after_master_action(call, bot, r, user)
    await call.answer("Отмечено: сделано")
    await safe_send(
        bot, r.user_id,
        f"{DONE} <b>Заявка {r.code} выполнена</b>\n{esc(r.text[:200])}\n\n"
        "Оцените, пожалуйста, работу от 1 до 5. Если на самом деле не решено — "
        "верните заявку мастеру.",
        reply_markup=rate_kb(r),
    )
    await _update_report(bot, session, r)


@router.callback_query(RepCb.filter(F.a == "rej"))
async def cb_reject(call: CallbackQuery, callback_data: RepCb, session: AsyncSession, user: User) -> None:
    r = await _handled(call, session, user, callback_data.id)
    if not r:
        return
    await edit(call.message, render_repair(r) + "\n\n<b>Почему отклоняете?</b>", repair_reject_kb(r))
    await call.answer()


@router.callback_query(RepCb.filter(F.a == "rej_ok"))
async def cb_reject_ok(call: CallbackQuery, callback_data: RepCb, session: AsyncSession,
                       user: User, bot: Bot) -> None:
    r = await _handled(call, session, user, callback_data.id)
    if not r:
        return
    r.status = RepairStatus.rejected
    r.reject_reason = REJECT_REASONS[callback_data.v] if 0 <= callback_data.v < len(REJECT_REASONS) else None
    r.taken_by = user.id
    await session.commit()
    await _after_master_action(call, bot, r, user)
    await call.answer("Заявка отклонена")
    await safe_send(bot, r.user_id, f"{OFF} <b>Заявка {r.code} отклонена</b>\n"
                                    f"Причина: {esc(r.reject_reason or 'не указана')}.\n"
                                    "Если это ошибка — напишите администратору.")
    await _update_report(bot, session, r)


# ---------------------------------------------------------------- отчёты


async def _update_report(bot: Bot, session: AsyncSession, r: Repair) -> None:
    """Один отчёт на заявку в REPORT_CHAT_ID; оценка дописывается в него же."""
    master = await session.get(User, r.taken_by) if r.taken_by else None
    text = render_report(r, master.display if master else "мастер")
    if r.report_msg_id and await edit_by_id(bot, config.report_chat_id, r.report_msg_id, text):
        return
    msg = await safe_send(bot, config.report_chat_id, text)
    if msg is not None:
        r.report_msg_id = msg.message_id
        await session.commit()


@router.message(F.chat.type == "private", Command("repstats"))
async def cmd_repstats(message: Message, session: AsyncSession, user: User) -> None:
    """Сводка по починке за 30 дней: сколько, как быстро, какая оценка."""
    if not can_view_repairs(user):
        await message.answer("Сводку видят мастер, получатель отчётов и администраторы.")
        return
    await message.answer(await repair_summary(session, days=30))


async def repair_summary(session: AsyncSession, days: int) -> str:
    since = dt.datetime.combine(today() - dt.timedelta(days=days - 1), dt.time.min)
    rows = list((await session.scalars(
        select(Repair).where(Repair.sent_at.is_not(None), Repair.sent_at >= since)
    )).all())
    open_now = await session.scalar(
        select(func.count(Repair.id)).where(Repair.status.in_((RepairStatus.new, RepairStatus.in_work)))
    ) or 0
    done = [r for r in rows if r.status is RepairStatus.done]
    rated = [r.rating for r in done if r.rating]
    hours = [(r.done_at - r.sent_at).total_seconds() / 3600 for r in done if r.done_at and r.sent_at]
    lines = [
        f"<b>Заявки на помощь за {plural(days, 'день', 'дня', 'дней')}</b>",
        RULER,
        f"{DOT} Подано заявок — {len(rows)}, выполнено — {len(done)}, "
        f"отклонено — {sum(1 for r in rows if r.status is RepairStatus.rejected)}",
        f"{DOT} Открыто сейчас — {open_now}",
    ]
    if hours:
        lines.append(f"{DOT} Среднее время до выполнения — {sum(hours) / len(hours):.1f} ч")
    if rated:
        avg = sum(rated) / len(rated)
        lines.append(f"{DOT} Средняя оценка — {avg:.1f}/5 (оценок {len(rated)})")
        low = [r for r in done if r.rating and r.rating <= 2]
        if low:
            lines.append(f"{DOT} Низкие оценки: " + ", ".join(f"{r.code} ({r.rating})" for r in low[:10]))
    returned = [r for r in rows if r.returned]
    if returned:
        lines.append(f"{DOT} Возвращали «не решено»: " + ", ".join(r.code for r in returned[:10]))
    return "\n".join(lines)
