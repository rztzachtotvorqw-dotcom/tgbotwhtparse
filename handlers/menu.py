from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

import offices
from db import User
from keyboards import (
    ALL_ORDERS,
    BATCH_LAB,
    BATCH_MAIN,
    BTN_ACTIVE,
    BTN_ALL,
    BTN_BATCH,
    BTN_DRAFTS,
    BTN_MORE,
    BTN_MY,
    GROUP_BY_BTN,
    MY_ACTIVE,
    MY_ALL,
    MY_DRAFTS,
    Ctx,
    LCb,
    NavCb,
    OCb,
    back_kb,
    office_kb,
)
from links import has_url
from screens import (
    HELP_TEXT,
    Screen,
    range_ctx,
    resolve_ctx,
    screen_more,
    screen_order,
    screen_period,
)
from services import edit, edit_by_id, menu_for, try_delete
from states import FeedbackSG, FilterSG, Reg
from utils import esc, parse_range
from views import DOT, OFF

router = Router(name="menu")
router.message.filter(F.chat.type == "private")

ADMIN_HELP = (
    "<b>Администрирование</b>\n\n"
    "У администраторов и разработчиков одинаковые права: все получают уведомления "
    "о новых заказах и могут менять статусы.\n\n"
    "<b>Каждый день</b>\n"
    "• «▤ Общий заказ» (Т4, ЦМНТ, ЦРПП) и «▤ Лаборатория» — две отдельные кнопки в "
    "главном меню, это два разных заказа на Ozon. Внутри — «Ждут оформления» и «Заказано», "
    "сгруппировано по датам: нажмите на дату и отметьте всё за неё "
    "«заказано» или «приехало» — или часть галочками.\n"
    "• «Все заказы» — заказы всех сотрудников с фильтром по статусу и датам.\n"
    "• Уведомление о новом заказе приходит каждому админу, статус можно менять прямо с него.\n"
    "• Приехала не вся позиция (если штук больше одной) — «◑ Частично» на карточке или "
    "уведомлении: сколько штук приехало, а остаток ждём дальше или отменяем "
    "(«Отменён Ozon» / «Не нужен»). «Отменить» → «Отменён Ozon» — если Ozon отменил весь заказ.\n\n"
    "<b>Команды</b>\n"
    "/main — общий заказ, /lab — лаборатория\n"
    "/all — все заказы\n"
    "/export — заявки на оплату по всему, что в работе (/export црпп — одно место)\n"
    "/stats — сводка по заказам (/stats 30 — за 30 дней)\n"
    "/repstats — сводка по заявкам на помощь за 30 дней\n"
    "/users — люди и их идентификаторы\n"
    "/find текст — поиск по всем заказам\n"
    "/approve id, /block id — доступ\n"
    "/reply id текст — ответить сотруднику\n"
    "/grant id, /revoke id — права администратора"
)


async def _send(message: Message, screen: Screen) -> None:
    text, kb = screen
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def _open_list(message: Message, state: FSMContext, session: AsyncSession, user: User,
                     ctx: Ctx) -> None:
    await state.clear()
    await _send(message, await resolve_ctx(session, user, ctx))


async def _staff_gate(message: Message, session: AsyncSession, user: User) -> bool:
    if user.is_staff:
        return True
    await message.answer("Раздел доступен только администраторам.",
                         reply_markup=await menu_for(session, user))
    return False


# ---------------------------------------------------------------- reply-кнопки
# Работают в любом состоянии: это аварийный выход из любого диалога.


@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def btn_my(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _open_list(message, state, session, user, MY_ALL)


@router.message(F.text.startswith(BTN_ACTIVE))  # «В пути · 3»
@router.message(Command("pending"))
async def btn_active(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _open_list(message, state, session, user, MY_ACTIVE)


@router.message(F.text == BTN_DRAFTS)
@router.message(Command("drafts"))
async def btn_drafts(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _open_list(message, state, session, user, MY_DRAFTS)


@router.message(F.text.in_(GROUP_BY_BTN) | (F.text == BTN_BATCH))
async def btn_group(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """«▤ Общий заказ» / «▤ Лаборатория» — у админов прямо в главном меню.
    Сотрудник мог просто написать такой текст (например, комментарием к черновику) —
    тогда пропускаем сообщение дальше, к обычным обработчикам."""
    if not user.is_staff:
        raise SkipHandler
    g = GROUP_BY_BTN.get(message.text or "", offices.GROUP_MAIN)
    await _open_list(message, state, session, user, BATCH_LAB if g == offices.GROUP_LAB else BATCH_MAIN)
    if message.text == BTN_BATCH:  # старое меню — заменим на новое с двумя кнопками
        await message.answer("Меню обновлено: общий заказ и лаборатория — отдельные кнопки.",
                             reply_markup=await menu_for(session, user))


@router.message(Command("batch", "main"))
async def cmd_batch(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    if await _staff_gate(message, session, user):
        await _open_list(message, state, session, user, BATCH_MAIN)


@router.message(Command("lab"))
async def cmd_lab(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    if await _staff_gate(message, session, user):
        await _open_list(message, state, session, user, BATCH_LAB)


@router.message(F.text == BTN_ALL)
@router.message(Command("all", "allorders"))
async def btn_all(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    if await _staff_gate(message, session, user):
        await _open_list(message, state, session, user, ALL_ORDERS)


@router.message(F.text == BTN_MORE)
async def btn_more(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await _send(message, await screen_more(user))


# ---------------------------------------------------------------- списки и карточки


@router.callback_query(LCb.filter())
async def cb_list(call: CallbackQuery, callback_data: LCb, state: FSMContext,
                  session: AsyncSession, user: User) -> None:
    await state.clear()
    text, kb = await resolve_ctx(session, user, Ctx.unpack(callback_data.c))
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(OCb.filter())
async def cb_open(call: CallbackQuery, callback_data: OCb, state: FSMContext,
                  session: AsyncSession, user: User) -> None:
    await state.clear()
    text, kb = await screen_order(session, user, callback_data.id, Ctx.unpack(callback_data.c))
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "noop"))
async def nav_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "period"))
async def nav_period(call: CallbackQuery, callback_data: NavCb, state: FSMContext) -> None:
    await state.clear()
    text, kb = screen_period(Ctx.unpack(callback_data.c))
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "range"))
async def nav_range(call: CallbackQuery, callback_data: NavCb, state: FSMContext) -> None:
    ctx = Ctx.unpack(callback_data.c)
    await state.set_state(FilterSG.range)
    await state.update_data(c=ctx.pack(), prompt_id=call.message.message_id)
    await edit(
        call.message,
        "<b>Свои даты</b>\n\nПришлите период одним сообщением:\n"
        "• <code>01.10-15.10</code> — с 1 по 15 октября\n"
        "• <code>01.09.2026 - 30.09.2026</code>\n"
        "• <code>03.11</code> — один день",
        back_kb(ctx),
    )
    await call.answer()


@router.message(StateFilter(FilterSG.range), F.text, ~F.text.startswith("/"), ~F.text.func(has_url))
async def range_input(message: Message, state: FSMContext, session: AsyncSession, user: User,
                      bot: Bot) -> None:
    data = await state.get_data()
    rng = parse_range(message.text or "")
    await try_delete(message)
    ctx = Ctx.unpack(data.get("c"))
    if rng is None:
        hint = f"{OFF} Не понял даты. Пример: <code>01.10-15.10</code> или <code>03.11</code>."
        if not await edit_by_id(bot, message.chat.id, data.get("prompt_id"), hint, back_kb(ctx)):
            await message.answer(hint)
        return
    await state.clear()
    text, kb = await resolve_ctx(session, user, range_ctx(ctx, *rng))
    if not await edit_by_id(bot, message.chat.id, data.get("prompt_id"), text, kb):
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ---------------------------------------------------------------- «Ещё»


def _back_more() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="‹ Назад", callback_data=NavCb(to="more"))
    return kb.as_markup()


@router.callback_query(NavCb.filter(F.to == "more"))
async def nav_more(call: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    text, kb = await screen_more(user)
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "help"))
async def nav_help(call: CallbackQuery) -> None:
    await edit(call.message, HELP_TEXT, _back_more())
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "feedback"))
async def nav_feedback(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FeedbackSG.text)
    await state.update_data(order_id=0)
    await edit(
        call.message,
        "Напишите сообщение одним текстом — передам администраторам вместе с вашим именем. "
        "/cancel — не отправлять.",
    )
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "name"))
async def nav_name(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Reg.name)
    await edit(call.message, "Напишите фамилию и имя — так будут называться ваши заказы.\n"
                             "/cancel — оставить как есть.")
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "office"))
async def nav_office(call: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    await edit(
        call.message,
        f"{DOT} Сейчас — {esc(offices.label(user.office))}\n\n"
        "Выберите рабочее место — оно подставляется в новые заказы:",
        office_kb(),
    )
    await call.answer()


@router.callback_query(NavCb.filter(F.to == "admin"))
async def nav_admin(call: CallbackQuery, user: User) -> None:
    if not user.is_staff:
        await call.answer("Раздел доступен только администраторам", show_alert=True)
        return
    await edit(call.message, ADMIN_HELP, _back_more())
    await call.answer()
