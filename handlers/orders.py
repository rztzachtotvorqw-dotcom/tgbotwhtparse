from __future__ import annotations

import datetime as dt
import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import offices
from config import config
from db import PENDING, Order, OrderStatus, User
from keyboards import (
    CANCEL_REASONS,
    MY_ACTIVE,
    MY_DRAFTS,
    OZON_REASON,
    PART_REST,
    CancelCb,
    Ctx,
    DeadlineCb,
    LCb,
    OrderCb,
    PartCb,
    PlaceCb,
    QtyCb,
    back_kb,
    back_to_draft,
    cancel_reasons_kb,
    confirm_delete_kb,
    created_kb,
    deadline_kb,
    draft_kb,
    found_kb,
    office_kb,
    order_kb,
    part_qty_kb,
    part_rest_kb,
    place_kb,
    pub_all_kb,
    qty_kb,
)
from links import MAX_QTY, LinkItem, find_urls, has_url, parse_message, product_id_from_url
from screens import ASK_NAME, ASK_OFFICE, TAB_STATUSES, _scoped, _when, screen_order
from services import (
    announce_order,
    edit,
    edit_by_id,
    is_notice,
    menu_for,
    menu_for_id,
    notify_staff,
    pending_count,
    refresh_announce,
    safe_send,
    try_delete,
)
from states import Draft, FilterSG, OrderSG, Reg
from utils import esc, fmt_date, now, parse_price, parse_user_date, plural, shorten, today
from views import DONE, OFF, PART, RULER, STATUS_TEXT, render_draft, render_links, render_order

log = logging.getLogger(__name__)
router = Router(name="orders")
router.message.filter(F.chat.type == "private")

ORDER_NO_RE = re.compile(r"^\s*(?:№|#|n|no\.?)?\s*(\d{1,7})\s*$", re.IGNORECASE)
NOT_COMMAND = ~F.text.startswith("/")

# в каких состояниях новая ссылка важнее начатого ввода
LINK_BREAKS_STATES = (
    None,
    Draft.price, Draft.qty, Draft.deadline, Draft.place, Draft.comment,
    FilterSG.range,
)


# ------------------------------------------------------------------ создание черновиков


def _default_deadline() -> dt.date:
    return today() + dt.timedelta(days=config.default_deadline_days)


async def make_draft(session: AsyncSession, user: User, item: LinkItem) -> Order:
    order = Order(
        user_id=user.id,
        url=item.url,
        product_id=product_id_from_url(item.url),
        title=user.display,
        qty=item.qty,
        comment=item.comment,
        place=user.office,
        deadline=_default_deadline(),
        status=OrderStatus.draft,
    )
    session.add(order)
    await session.flush()
    order.user = user
    order.title = order.name  # «Фамилия Имя №N» — так же хранится и в базе
    return order


async def duplicate_note(session: AsyncSession, order: Order) -> str:
    """Тот же товар уже ждут — свой или коллеги. Просто напоминание."""
    cond = [Order.url == order.url]
    if order.product_id:
        cond.append(Order.product_id == order.product_id)
    other = await session.scalar(
        select(Order)
        .where(or_(*cond), Order.id != order.id, Order.status.in_(PENDING))
        .order_by((Order.user_id == order.user_id).desc(), Order.id.desc())
        .limit(1)
    )
    if other is None:
        return ""
    if other.user_id == order.user_id:
        return f"Этот товар у вас уже в заказе — {esc(other.name)}, {STATUS_TEXT[other.status]}."
    return (
        f"Этот товар уже заказал коллега — {esc(other.name)} "
        f"({esc(offices.label(other.place))}). Возможно, хватит одного."
    )


async def start_drafts(
    message: Message, state: FSMContext, session: AsyncSession, user: User, text: str
) -> None:
    items = parse_message(text)
    if not items:
        await message.answer("Не нашёл ссылку на Ozon в сообщении.")
        return
    orders = [await make_draft(session, user, it) for it in items]
    await session.commit()

    if len(orders) == 1:
        order = orders[0]
        await message.answer(
            render_draft(order, duplicate=await duplicate_note(session, order)),
            reply_markup=draft_kb(order),
            disable_web_page_preview=True,
        )
        return

    lines = [f"<b>Создано черновиков: {len(orders)}</b>", ""]
    for o in orders:
        extra = f" — <i>{esc(shorten(o.comment, 40))}</i>" if o.comment else ""
        lines.append(f"✎ {esc(o.name)}, {o.quantity} шт.{extra}")
    lines += ["", "Откройте каждый, чтобы поправить количество или комментарий, "
                  "или опубликуйте все разом."]
    await message.answer("\n".join(lines), reply_markup=created_kb(orders),
                         disable_web_page_preview=True)


@router.message(StateFilter(*LINK_BREAKS_STATES), F.text.func(has_url))
async def catch_link(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    if not user.is_registered:
        # ссылку не теряем: дозадаём вопросы и вернёмся к ней сразу после ответа
        await state.update_data(pending_text=message.text)
        if not user.full_name:
            await state.set_state(Reg.name)
            await message.answer(f"Сначала познакомимся. {ASK_NAME}")
        else:
            await state.set_state(Reg.office)
            await message.answer(ASK_OFFICE, reply_markup=office_kb())
        return
    await start_drafts(message, state, session, user, message.text or "")


# ------------------------------------------------------------------ доступ к черновику


async def _draft_for(
    call: CallbackQuery, session: AsyncSession, user: User, order_id: int,
    state: FSMContext | None = None,
) -> Order | None:
    order = await session.get(Order, order_id)
    if order is None or not order.is_draft:
        await call.answer("Черновик не найден: он уже опубликован или удалён", show_alert=True)
        return None
    if order.user_id != user.id:
        await call.answer("Это чужой черновик", show_alert=True)
        return None
    if state is not None:
        await state.clear()  # нажали кнопку — начатый ввод текста больше не ждём
    return order


async def _show_draft(call: CallbackQuery, session: AsyncSession, order: Order) -> None:
    await edit(call.message, render_draft(order, duplicate=await duplicate_note(session, order)),
               draft_kb(order))


async def _ask(
    call: CallbackQuery, state: FSMContext, order: Order, st, text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> None:
    """Превращаем карточку в вопрос; ответ вернёт её обратно."""
    await state.set_state(st)
    await state.update_data(order_id=order.id, prompt_id=call.message.message_id, prompt=text)
    await edit(call.message, text, kb or back_to_draft(order.id))
    await call.answer()


PROMPTS = {
    "price": (Draft.price, "Цена за одну штуку — просто числом: <code>12490</code> или "
                           "<code>12 490 ₽</code>.\nДефис — убрать цену."),
    "comment": (Draft.comment, "Комментарий для закупщика: что это, цвет, размер, «срочно»…\n"
                               "Дефис — убрать комментарий."),
    "link": (Draft.link, "Пришлите новую ссылку на товар Ozon."),
}


@router.callback_query(OrderCb.filter(F.action.in_(PROMPTS)))
async def cb_prompt(call: CallbackQuery, callback_data: OrderCb, state: FSMContext,
                    session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    st, text = PROMPTS[callback_data.action]
    if callback_data.action == "comment" and order.comment:
        text += f"\n\nСейчас: <code>{esc(order.comment)}</code>"
    await _ask(call, state, order, st, text)


# ------------------------------------------------------------------ ввод текстом


def _apply(field: str, order: Order, raw: str) -> str | None:
    """Применяет ввод к черновику. Возвращает текст ошибки или None (тогда ничего не меняет)."""
    if field == "price":
        if raw == "-":
            order.price = None
            return None
        price = parse_price(raw)
        if price is None:
            return "Не понял цену. Пришлите число, например 12490."
        order.price = price
    elif field == "qty":
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_QTY:
            return f"Нужно целое число от 1 до {MAX_QTY}."
        order.qty = int(raw)
    elif field == "deadline":
        date = parse_user_date(raw)
        if date is None:
            return "Не понял дату. Формат: 25.12 или 25.12.2026."
        if date < today():
            return "Дата в прошлом. Нужна сегодняшняя или позже."
        order.deadline = date
    elif field == "place":
        if not 2 <= len(raw) <= 64:
            return "Место — от 2 до 64 символов."
        order.place = raw
    elif field == "comment":
        order.comment = None if raw == "-" else raw[:1000]
    elif field == "link":
        urls = find_urls(raw)
        if not urls:
            return "Это не похоже на ссылку Ozon. Пришлите ссылку целиком."
        order.url = urls[0]
        order.product_id = product_id_from_url(order.url)
    return None


@router.message(StateFilter(Draft), F.text, NOT_COMMAND)
async def draft_input(message: Message, state: FSMContext, session: AsyncSession,
                      user: User, bot: Bot) -> None:
    data = await state.get_data()
    order = await session.get(Order, int(data.get("order_id") or 0))
    if order is None or not order.is_draft or order.user_id != user.id:
        await state.clear()
        await message.answer("Черновик недоступен: он опубликован или удалён.")
        return

    field = (await state.get_state() or "").split(":")[-1]
    raw = (message.text or "").strip()
    prompt_id = data.get("prompt_id")
    error = _apply(field, order, raw)
    await try_delete(message)

    if error:
        text = f"{OFF} {error}\n\n{data.get('prompt') or ''}"
        if not await edit_by_id(bot, message.chat.id, prompt_id, text, back_to_draft(order.id)):
            await message.answer(error)
        return

    await session.commit()
    await state.clear()
    card = render_draft(order, duplicate=await duplicate_note(session, order))
    if not await edit_by_id(bot, message.chat.id, prompt_id, card, draft_kb(order)):
        await message.answer(card, reply_markup=draft_kb(order), disable_web_page_preview=True)


# ------------------------------------------------------------------ кнопки черновика


@router.callback_query(OrderCb.filter(F.action == "qty"))
async def cb_qty(call: CallbackQuery, callback_data: OrderCb, state: FSMContext,
                 session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    await edit(call.message, f"Сколько штук берём? Сейчас — <b>{order.quantity}</b>.",
               qty_kb(order.id, order.quantity))
    await call.answer()


@router.callback_query(QtyCb.filter())
async def cb_qty_value(call: CallbackQuery, callback_data: QtyCb, state: FSMContext,
                       session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    if callback_data.value == 0:
        await _ask(call, state, order, Draft.qty, f"Пришлите количество числом (1–{MAX_QTY}).")
        return
    order.qty = max(1, min(MAX_QTY, callback_data.value))
    await session.commit()
    await _show_draft(call, session, order)
    await call.answer(f"{order.quantity} шт.")


@router.callback_query(OrderCb.filter(F.action == "deadline"))
async def cb_deadline(call: CallbackQuery, callback_data: OrderCb, state: FSMContext,
                      session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    await edit(
        call.message,
        f"Желаемый срок доставки. Сейчас — <b>{fmt_date(order.deadline)}</b>.\n"
        "«+N дн.» прибавляет к текущему сроку.",
        deadline_kb(order.id, config.default_deadline_days),
    )
    await call.answer()


@router.callback_query(DeadlineCb.filter())
async def cb_deadline_value(call: CallbackQuery, callback_data: DeadlineCb, state: FSMContext,
                            session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    if callback_data.kind == "custom":
        await _ask(call, state, order, Draft.deadline, "Пришлите дату: <code>25.12</code> или "
                                                       "<code>25.12.2026</code>.")
        return
    if callback_data.kind == "plus":
        base = max(order.deadline or today(), today())
        order.deadline = base + dt.timedelta(days=callback_data.days)
    else:
        order.deadline = _default_deadline()
    await session.commit()
    await _show_draft(call, session, order)
    await call.answer(fmt_date(order.deadline))


@router.callback_query(OrderCb.filter(F.action == "place"))
async def cb_place(call: CallbackQuery, callback_data: OrderCb, state: FSMContext,
                   session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    await edit(
        call.message,
        f"Куда везти заказ? Сейчас — <b>{esc(offices.label(order.place))}</b>.\n"
        "Лаборатория закупается отдельным заказом.",
        place_kb(order.id, order.place),
    )
    await call.answer()


@router.callback_query(PlaceCb.filter())
async def cb_place_value(call: CallbackQuery, callback_data: PlaceCb, state: FSMContext,
                         session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    if callback_data.code == "custom":
        await _ask(call, state, order, Draft.place, "Напишите место доставки своими словами "
                                                    "(до 64 символов).")
        return
    if not offices.is_code(callback_data.code):
        await call.answer("Неизвестное место", show_alert=True)
        return
    order.place = callback_data.code
    await session.commit()
    await _show_draft(call, session, order)
    await call.answer(offices.label(order.place))


# ------------------------------------------------------------------ публикация


def _publish(order: Order, user: User) -> None:
    order.status = OrderStatus.new
    order.published_at = now()
    order.place = order.place or user.office
    order.deadline = order.deadline or _default_deadline()


async def _confirm_published(message: Message, session: AsyncSession, user: User, text: str) -> None:
    """Короткое подтверждение со свежим счётчиком на кнопке «В пути»."""
    menu = await menu_for(session, user)
    await message.answer(text, reply_markup=menu)


@router.callback_query(OrderCb.filter(F.action == "publish"))
async def cb_publish(call: CallbackQuery, callback_data: OrderCb, state: FSMContext,
                     session: AsyncSession, user: User, bot: Bot) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    if not (order.place or user.office):
        await call.answer("Сначала укажите, куда доставить", show_alert=True)
        return
    _publish(order, user)
    await session.commit()
    await edit(call.message, render_order(order), order_kb(order, user, MY_ACTIVE))
    await call.answer("Заказ опубликован")
    await announce_order(bot, session, order)
    await _confirm_published(call.message, session, user,
                             f"{DONE} {esc(order.name)} опубликован, администраторы получили заказ.")


async def _my_drafts(session: AsyncSession, user: User) -> list[Order]:
    return list((await session.scalars(
        select(Order).where(Order.user_id == user.id, Order.status == OrderStatus.draft)
        .order_by(Order.id)
    )).all())


@router.callback_query(OrderCb.filter(F.action == "pub_all"))
async def cb_pub_all(call: CallbackQuery, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    await state.clear()
    drafts = await _my_drafts(session, user)
    if not drafts:
        await call.answer("Черновиков нет", show_alert=True)
        return
    lines = [f"<b>Опубликовать {plural(len(drafts), 'черновик', 'черновика', 'черновиков')}?</b>", ""]
    for o in drafts:
        extra = f" — <i>{esc(shorten(o.comment, 40))}</i>" if o.comment else ""
        lines.append(f"✎ {esc(o.name)}, {o.quantity} шт., "
                     f"{esc(offices.label(o.place or user.office))}{extra}")
    await edit(call.message, "\n".join(lines), pub_all_kb(len(drafts)))
    await call.answer()


@router.callback_query(OrderCb.filter(F.action == "pub_all_ok"))
async def cb_pub_all_ok(call: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    drafts = await _my_drafts(session, user)
    if not drafts:
        await call.answer("Черновиков нет", show_alert=True)
        return
    for o in drafts:
        _publish(o, user)
    await session.commit()
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть «В пути»", callback_data=LCb(c=MY_ACTIVE.pack()))
    await edit(call.message,
               f"{DONE} Опубликовано {plural(len(drafts), 'заказ', 'заказа', 'заказов')}: "
               + ", ".join(f"№{o.id}" for o in drafts) + ".",
               kb.as_markup())
    await call.answer("Готово")
    for o in drafts:
        await announce_order(bot, session, o)
    await _confirm_published(call.message, session, user, "Администраторы получили заказы.")


@router.callback_query(OrderCb.filter(F.action == "drop"))
async def cb_drop(call: CallbackQuery, callback_data: OrderCb, state: FSMContext,
                  session: AsyncSession, user: User) -> None:
    order = await _draft_for(call, session, user, callback_data.order_id, state)
    if not order:
        return
    name = order.name
    await session.delete(order)
    await session.commit()
    await edit(call.message, f"{OFF} Черновик {esc(name)} удалён.")
    await call.answer()


# ------------------------------------------------------------------ опубликованный заказ


async def _managed(
    call: CallbackQuery, session: AsyncSession, user: User, order_id: int, staff_only: bool = False
) -> Order | None:
    order = await session.get(Order, order_id)
    if order is None or order.status is OrderStatus.deleted:
        await call.answer("Заказ не найден", show_alert=True)
        return None
    if order.is_draft:
        await call.answer("Это уже черновик — откройте его в «Черновиках»", show_alert=True)
        return None
    if staff_only and not user.is_staff:
        await call.answer("Статусы меняют только администраторы", show_alert=True)
        return None
    if not (order.user_id == user.id or user.is_staff):
        await call.answer("Менять чужой заказ может только администратор", show_alert=True)
        return None
    return order


def _from_notice(call: CallbackQuery, order: Order) -> bool:
    return call.message is not None and is_notice(order, call.message.chat.id, call.message.message_id)


async def _sync_cards(call: CallbackQuery, bot: Bot, order: Order, user: User, ctx: Ctx) -> None:
    """Обновляет карточку, откуда нажали, и уведомления у всех админов."""
    if not _from_notice(call, order) and call.message is not None:
        await edit(call.message, render_order(order), order_kb(order, user, ctx))
    await refresh_announce(bot, order)


async def notify_owner(bot: Bot, session: AsyncSession, order: Order, actor: User, text: str) -> None:
    """Заказчику пишем, только если статус поменял кто-то другой.
    К сообщению прикладываем меню — так обновляется счётчик «В пути»."""
    if order.user_id == actor.id:
        return
    await safe_send(bot, order.user_id, text, reply_markup=await menu_for_id(session, order.user_id))


async def left_note(session: AsyncSession, user_id: int) -> str:
    n = await pending_count(session, user_id)
    return f"В пути у вас осталось: {n}." if n else "Больше ничего не ждём — всё приехало."


def deadline_verdict(order: Order) -> str:
    if not order.deadline:
        return ""
    arrived = (order.arrived_at or now()).date()
    if arrived <= order.deadline:
        return "Приехал вовремя.\n"
    return f"Ожидали к {fmt_date(order.deadline)}, опоздание {(arrived - order.deadline).days} дн.\n"


def ordered_text(order: Order) -> str:
    return (f"◐ <b>{esc(order.name)} — заказано на Ozon</b>\n"
            + (f"<i>{esc(shorten(order.comment, 60))}</i>\n" if order.comment else "")
            + f"Ожидаем к {fmt_date(order.deadline)}. Когда приедет — пришлю сообщение.")


async def arrived_text(session: AsyncSession, order: Order) -> str:
    return (f"{DONE} <b>{esc(order.name)} приехал</b>\n"
            + (f"<i>{esc(shorten(order.comment, 60))}</i>\n" if order.comment else "")
            + f"{deadline_verdict(order)}Забрать: {esc(offices.label(order.place))}.\n"
            + await left_note(session, order.user_id))


@router.callback_query(OrderCb.filter(F.action == "ordered"))
async def cb_ordered(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                     user: User, bot: Bot) -> None:
    order = await _managed(call, session, user, callback_data.order_id, staff_only=True)
    if not order:
        return
    if order.status is not OrderStatus.new:
        await call.answer(f"Статус уже — {STATUS_TEXT[order.status]}", show_alert=True)
        return
    order.status = OrderStatus.in_progress
    order.ordered_at = now()
    await session.commit()
    await _sync_cards(call, bot, order, user, Ctx.unpack(callback_data.c))
    await call.answer("Отмечено: заказано")
    await notify_owner(bot, session, order, user, ordered_text(order))


@router.callback_query(OrderCb.filter(F.action == "arrived"))
async def cb_arrived(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                     user: User, bot: Bot) -> None:
    order = await _managed(call, session, user, callback_data.order_id, staff_only=True)
    if not order:
        return
    if order.status not in PENDING:
        await call.answer(f"Статус уже — {STATUS_TEXT[order.status]}", show_alert=True)
        return
    order.status = OrderStatus.arrived
    order.arrived_at = now()
    order.ordered_at = order.ordered_at or order.arrived_at
    if order.qty_arrived:  # до этого приезжал частями — теперь приехал весь остаток
        order.qty_arrived = order.quantity
    await session.commit()
    await _sync_cards(call, bot, order, user, Ctx.unpack(callback_data.c))
    await call.answer("Отмечено: приехал")
    await notify_owner(bot, session, order, user, await arrived_text(session, order))


@router.callback_query(OrderCb.filter(F.action == "reopen"))
async def cb_reopen(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                    user: User, bot: Bot) -> None:
    order = await _managed(call, session, user, callback_data.order_id, staff_only=True)
    if not order:
        return
    order.status = OrderStatus.in_progress if order.ordered_at else OrderStatus.new
    order.arrived_at = None
    order.cancel_reason = None
    if order.arrived_qty >= order.quantity:
        order.qty_arrived = None  # приехавшее целиком — снова ждём всё; частичное — ждём остаток
    await session.commit()
    await _sync_cards(call, bot, order, user, Ctx.unpack(callback_data.c))
    await call.answer("Заказ снова в работе")


@router.callback_query(OrderCb.filter(F.action == "edit"))
async def cb_edit(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                  user: User, bot: Bot) -> None:
    """Пока заказ не оформлен, автор может вернуть его в черновики и поправить."""
    order = await _managed(call, session, user, callback_data.order_id)
    if not order:
        return
    if order.user_id != user.id or order.status is not OrderStatus.new:
        await call.answer("Изменить можно только свой заказ, пока он ждёт оформления",
                          show_alert=True)
        return
    order.status = OrderStatus.draft
    order.published_at = None
    await refresh_announce(bot, order)  # у админов: «отозван на правку»
    order.notices.clear()
    order.announce_chat_id = None
    order.announce_message_id = None
    await session.commit()
    await edit(call.message, render_draft(order, "Заказ вернулся в черновики. Поправьте и "
                                                 "опубликуйте снова."), draft_kb(order))
    await call.answer("Вернул в черновики")


@router.callback_query(OrderCb.filter(F.action == "repeat"))
async def cb_repeat(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                    user: User) -> None:
    src = await session.get(Order, callback_data.order_id)
    if src is None or src.status is OrderStatus.deleted:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if src.user_id != user.id and not user.is_staff:
        await call.answer("Чужой заказ повторить нельзя", show_alert=True)
        return
    item = LinkItem(url=src.url, comment=src.comment, qty=src.quantity)
    order = await make_draft(session, user, item)
    order.price = src.price
    if src.user_id == user.id:
        order.place = src.place
    await session.commit()
    await call.message.answer(
        render_draft(order, f"Новый черновик по образцу заказа №{src.id}. Проверьте и опубликуйте."),
        reply_markup=draft_kb(order),
        disable_web_page_preview=True,
    )
    await call.answer("Черновик создан")


# ------------------------------------------------------------------ отмена


@router.callback_query(OrderCb.filter(F.action == "cancel"))
async def cb_cancel(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                    user: User) -> None:
    order = await _managed(call, session, user, callback_data.order_id)
    if not order:
        return
    ctx = Ctx.unpack(callback_data.c)
    what = (f"остаток {esc(order.name)} ({order.left_qty} шт.)" if order.arrived_qty
            else esc(order.name))
    text = (f"<b>Отменить {what}?</b>\n\n"
            "Выберите причину — её увидят автор заказа и администраторы.")
    if _from_notice(call, order):
        # уведомление админа не превращаем в меню — вопрос отдельным сообщением
        await call.message.answer(text, reply_markup=cancel_reasons_kb(order.id, ctx))
    else:
        await edit(call.message, text, cancel_reasons_kb(order.id, ctx, user.is_staff))
    await call.answer()


async def _do_cancel(bot: Bot, session: AsyncSession, order: Order, user: User, reason: str) -> None:
    if order.arrived_qty:
        # часть уже приехала — отменяется только остаток, заказ считается приехавшим
        await _finish_partial(bot, session, order, user, reason)
        return
    was_ordered = order.status is OrderStatus.in_progress
    order.status = OrderStatus.cancelled
    order.cancel_reason = reason[:500]
    await session.commit()
    await refresh_announce(bot, order)
    if not user.is_staff:
        # сотрудник отменил сам — админам важно знать, особенно если уже заказано
        note = (
            "\nЗаказ уже был оформлен на Ozon — возможно, нужно оформить возврат."
            if was_ordered else ""
        )
        await notify_staff(
            bot, session,
            f"{OFF} <b>{esc(user.display)} отменил {esc(order.name)}</b>\n"
            f"Причина: {esc(order.cancel_reason)}{note}",
        )
    await notify_owner(
        bot, session, order, user,
        f"{OFF} <b>{esc(order.name)} отменён</b>\n"
        f"Причина: {esc(order.cancel_reason)}\nОтменил — {esc(user.display)}.",
    )


@router.callback_query(CancelCb.filter())
async def cb_cancel_reason(call: CallbackQuery, callback_data: CancelCb, state: FSMContext,
                           session: AsyncSession, user: User, bot: Bot) -> None:
    order = await _managed(call, session, user, callback_data.order_id)
    if not order:
        return
    if order.status not in PENDING:
        await call.answer(f"Статус уже — {STATUS_TEXT[order.status]}", show_alert=True)
        return
    if callback_data.r == OZON_REASON and not user.is_staff:
        await call.answer("Этот вариант — для администраторов", show_alert=True)
        return
    if callback_data.r < 0 or callback_data.r >= len(CANCEL_REASONS):
        await state.set_state(OrderSG.cancel_reason)
        await state.update_data(order_id=order.id, prompt_id=call.message.message_id,
                                c=callback_data.c)
        await edit(call.message, f"Напишите причину отмены {esc(order.name)} одним сообщением.\n"
                                 "/cancel — передумали отменять.")
        await call.answer()
        return
    partial = bool(order.arrived_qty)
    await _do_cancel(bot, session, order, user, CANCEL_REASONS[callback_data.r])
    ctx = Ctx.unpack(callback_data.c)
    await edit(call.message, render_order(order), order_kb(order, user, ctx))
    await call.answer("Остаток отменён" if partial else "Заказ отменён")


@router.message(StateFilter(OrderSG.cancel_reason), F.text, NOT_COMMAND)
async def cancel_reason_text(message: Message, state: FSMContext, session: AsyncSession,
                             user: User, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    order = await session.get(Order, int(data.get("order_id") or 0))
    if order is None or order.status not in PENDING:
        await message.answer("Заказ не найден или уже не в работе.")
        return
    if not (order.user_id == user.id or user.is_staff):
        await message.answer("Менять чужой заказ может только администратор.")
        return
    await _do_cancel(bot, session, order, user, (message.text or "").strip() or "не указана")
    await try_delete(message)
    card, kb = render_order(order), order_kb(order, user, Ctx.unpack(data.get("c")))
    if not await edit_by_id(bot, message.chat.id, data.get("prompt_id"), card, kb):
        await message.answer(card, reply_markup=kb, disable_web_page_preview=True)


# ------------------------------------------------------------------ приехала часть
# Только для админов и только если в пути больше одной штуки. Остаток либо ждём
# дальше, либо отменяем (Ozon отменил / не нужен) — тогда заказ считается приехавшим.


def _part_prompt(order: Order) -> str:
    got = f", уже приехало {order.arrived_qty}" if order.arrived_qty else ""
    return (f"{PART} <b>{esc(order.name)} — приехала часть</b>\n{RULER}\n"
            f"Заказано {order.quantity} шт.{got}, ждём {order.left_qty}.\n"
            "Сколько штук приехало сейчас?")


def _rest_prompt(order: Order, k: int) -> str:
    return (f"{PART} <b>{esc(order.name)} — приехало {k} шт.</b>\n{RULER}\n"
            f"Не приехало ещё {order.left_qty - k} шт. Что с остатком?")


def _part_back_kb(order: Order, ctx: Ctx) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="‹ Назад", callback_data=PartCb(id=order.id, c=ctx.pack()))
    return kb.as_markup()


def _comment_line(order: Order) -> str:
    return f"<i>{esc(shorten(order.comment, 60))}</i>\n" if order.comment else ""


async def _show(call: CallbackQuery, order: Order, text: str, kb: InlineKeyboardMarkup) -> int:
    """С уведомления админа — отдельным сообщением, чтобы не ломать само уведомление."""
    if _from_notice(call, order):
        sent = await call.message.answer(text, reply_markup=kb)
        return sent.message_id
    await edit(call.message, text, kb)
    return call.message.message_id


async def _finish_partial(bot: Bot, session: AsyncSession, order: Order, user: User, reason: str) -> None:
    """Часть приехала, остаток отменён — заказ закрыт как приехавший, счётчик уменьшается."""
    rest = order.left_qty
    order.status = OrderStatus.arrived
    order.arrived_at = now()
    order.ordered_at = order.ordered_at or order.arrived_at
    order.cancel_reason = f"Остаток {rest} шт. — {reason}"[:500]
    await session.commit()
    await refresh_announce(bot, order)
    if not user.is_staff:
        await notify_staff(
            bot, session,
            f"{OFF} <b>{esc(user.display)} отменил остаток {esc(order.name)}</b> ({rest} шт.)\n"
            f"Причина: {esc(reason)}",
        )
    await notify_owner(
        bot, session, order, user,
        f"{DONE} <b>{esc(order.name)}: приехало {order.arrived_qty} из {order.quantity} шт.</b>\n"
        + _comment_line(order)
        + f"Остаток {rest} шт. не приедет: {esc(reason)}.\n"
        f"Забрать: {esc(offices.label(order.place))}.\n" + await left_note(session, order.user_id),
    )


async def _apply_part(bot: Bot, session: AsyncSession, order: Order, user: User, k: int, rest: str) -> str:
    order.qty_arrived = order.arrived_qty + k
    order.ordered_at = order.ordered_at or now()
    if rest in PART_REST:
        await _finish_partial(bot, session, order, user, PART_REST[rest])
        return "Часть приехала, остаток отменён"
    order.status = OrderStatus.in_progress
    await session.commit()
    await refresh_announce(bot, order)
    await notify_owner(
        bot, session, order, user,
        f"{PART} <b>{esc(order.name)}: приехала часть</b>\n" + _comment_line(order)
        + f"Приехало {order.arrived_qty} из {order.quantity} шт., ещё едет {order.left_qty}.\n"
        f"Забрать: {esc(offices.label(order.place))}.",
    )
    return "Отмечено: приехала часть"


@router.callback_query(PartCb.filter())
async def cb_part(call: CallbackQuery, callback_data: PartCb, state: FSMContext,
                  session: AsyncSession, user: User, bot: Bot) -> None:
    order = await _managed(call, session, user, callback_data.id, staff_only=True)
    if not order:
        return
    if not order.can_split:
        await call.answer("Частями отмечают заказ в пути, где ждём больше одной штуки", show_alert=True)
        return
    ctx, k, rest = Ctx.unpack(callback_data.c), callback_data.k, callback_data.r
    if rest == "n":
        prompt = await _show(call, order, f"{_part_prompt(order)}\n\nНапишите число от 1 до "
                                          f"{order.left_qty - 1}. /cancel — передумали.",
                             _part_back_kb(order, ctx))
        await state.set_state(OrderSG.part_qty)
        await state.update_data(order_id=order.id, c=callback_data.c, prompt_id=prompt)
        await call.answer()
        return
    await state.clear()
    if not k:
        await _show(call, order, _part_prompt(order), part_qty_kb(order, ctx))
    elif not 0 < k < order.left_qty:
        await call.answer("Столько уже не ждём — откройте карточку заново", show_alert=True)
        return
    elif not rest:
        await _show(call, order, _rest_prompt(order, k), part_rest_kb(order, k, ctx))
    else:
        note = await _apply_part(bot, session, order, user, k, rest)
        await _show(call, order, render_order(order), order_kb(order, user, ctx))
        await call.answer(note)
        return
    await call.answer()


@router.message(StateFilter(OrderSG.part_qty), F.text, NOT_COMMAND)
async def part_qty_text(message: Message, state: FSMContext, session: AsyncSession,
                        user: User, bot: Bot) -> None:
    data = await state.get_data()
    order = await session.get(Order, int(data.get("order_id") or 0))
    ctx, prompt = Ctx.unpack(data.get("c")), data.get("prompt_id")
    await try_delete(message)
    if order is None or not order.can_split or not user.is_staff:
        await state.clear()
        await message.answer("Заказ уже не в пути — отмечать нечего.")
        return
    raw = (message.text or "").strip()
    k = int(raw) if raw.isdigit() else 0
    if not 0 < k < order.left_qty:
        text = (f"{_part_prompt(order)}\n\n{OFF} Нужно число от 1 до {order.left_qty - 1}. "
                "Если приехало всё — нажмите «✓ Приехал».")
        kb = _part_back_kb(order, ctx)
    else:
        await state.clear()
        text, kb = _rest_prompt(order, k), part_rest_kb(order, k, ctx)
    if not await edit_by_id(bot, message.chat.id, prompt, text, kb):
        await message.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ удаление


@router.callback_query(OrderCb.filter(F.action == "del"))
async def cb_del(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                 user: User) -> None:
    order = await _managed(call, session, user, callback_data.order_id)
    if not order:
        return
    await edit(
        call.message,
        f"Удалить {esc(order.name)} из списков?\n"
        "Запись останется в базе, но пропадёт из всех списков. "
        "Если заказ просто не нужен — лучше «Отменить».",
        confirm_delete_kb(order.id, Ctx.unpack(callback_data.c)),
    )
    await call.answer()


@router.callback_query(OrderCb.filter(F.action == "del_yes"))
async def cb_del_yes(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                     user: User, bot: Bot) -> None:
    order = await _managed(call, session, user, callback_data.order_id)
    if not order:
        return
    order.status = OrderStatus.deleted
    await session.commit()
    await refresh_announce(bot, order)
    await edit(call.message, f"{OFF} {esc(order.name)} удалён из списков.",
               back_kb(Ctx.unpack(callback_data.c), "‹ К списку"))
    await call.answer()
    await notify_owner(bot, session, order, user,
                       f"{OFF} <b>{esc(order.name)} удалён</b>\nУдалил — {esc(user.display)}.")


# ------------------------------------------------------------------ ссылки списком


async def send_long(message: Message, parts: list[str], sep: str = "\n\n") -> None:
    """Режет длинный текст на сообщения по ~3800 символов, не разрывая пункты."""
    chunk = ""
    for part in parts:
        candidate = f"{chunk}{sep}{part}" if chunk else part
        if len(candidate) > 3800 and chunk:
            await message.answer(chunk, disable_web_page_preview=True)
            chunk = part
        else:
            chunk = candidate
    if chunk:
        await message.answer(chunk, disable_web_page_preview=True)


@router.callback_query(OrderCb.filter(F.action == "links"))
async def cb_links(call: CallbackQuery, callback_data: OrderCb, session: AsyncSession,
                   user: User) -> None:
    ctx = Ctx.unpack(callback_data.c)
    if ctx.kind == "a" and not user.is_staff:
        ctx = ctx.to(kind="m")
    statuses = TAB_STATUSES.get(ctx.st, TAB_STATUSES["all"])
    orders = list((await session.scalars(
        _scoped(select(Order), user, ctx).where(Order.status.in_(statuses))
        .order_by(_when().desc(), Order.id.desc()).limit(60)
    )).all())
    if not orders:
        await call.answer("Нечего выгружать", show_alert=True)
        return
    await send_long(call.message, render_links(orders).split("\n\n"))
    await call.answer()


# ------------------------------------------------------------------ поиск и номер заказа


async def _open_order_message(message: Message, session: AsyncSession, user: User, order_id: int) -> None:
    text, kb = await screen_order(session, user, order_id, Ctx(kind=""))
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("o", "order"))
async def cmd_order(message: Message, command: CommandObject, session: AsyncSession, user: User) -> None:
    raw = (command.args or "").strip().lstrip("№#")
    if not raw.isdigit():
        await message.answer("Формат: <code>/o 45</code> — или просто пришлите номер заказа.")
        return
    await _open_order_message(message, session, user, int(raw))


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, session: AsyncSession, user: User) -> None:
    query = (command.args or "").strip().casefold()
    if len(query) < 2:
        await message.answer("Формат: <code>/find кресло</code> — ищу по комментарию, "
                             "фамилии заказчика и ссылке.")
        return
    stmt = select(Order).where(Order.status != OrderStatus.deleted)
    if user.is_staff:
        stmt = stmt.where(or_(Order.status != OrderStatus.draft, Order.user_id == user.id))
    else:
        stmt = stmt.where(Order.user_id == user.id)
    rows = (await session.scalars(stmt.order_by(Order.id.desc()).limit(1000))).all()
    # в SQLite lower() не знает кириллицу — фильтруем в Python
    words = query.split()
    found = [
        o for o in rows
        if all(w in f"{o.name} {o.comment or ''} {o.url}".casefold() for w in words)
    ][:15]
    if not found:
        await message.answer(f"По запросу «{esc(query)}» ничего не нашёл.")
        return
    scope = "по всем заказам" if user.is_staff else "по вашим заказам"
    await message.answer(
        f"<b>Найдено {scope}: {len(found)}</b>" + (" (показаны первые 15)" if len(found) == 15 else ""),
        reply_markup=found_kb(found),
    )


@router.message(StateFilter(None), F.text.regexp(ORDER_NO_RE))
async def number_lookup(message: Message, session: AsyncSession, user: User) -> None:
    m = ORDER_NO_RE.match(message.text or "")
    await _open_order_message(message, session, user, int(m.group(1)))


# ------------------------------------------------------------------ прочий текст


@router.message(StateFilter(None))
async def fallback(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(
        "Не нашёл ссылку на Ozon. Пришлите ссылку на товар — заведу черновик. "
        "Номер заказа (например, <code>45</code>) откроет его карточку. Справка — /help.",
        reply_markup=await menu_for(session, user),
    )
