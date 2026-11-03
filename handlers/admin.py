from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import offices
from config import config
from db import PENDING, Feedback, Order, OrderStatus, ROLE_LABELS, Role, User
from export import send_export
from handlers.orders import arrived_text, left_note, ordered_text, send_long
from keyboards import BatchCb, Ctx, FbCb, batch_confirm_kb, main_menu
from screens import batch_orders, screen_batch, screen_batch_day
from services import edit, menu_for_id, notify_staff, refresh_announce, safe_send
from states import FeedbackSG
from utils import esc, fmt_price, now, plural, shorten, today
from views import DONE, DOT, render_links, short_list, summary

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private")

NOT_COMMAND = ~F.text.startswith("/")


def _deny(user: User) -> str | None:
    """У администраторов и разработчиков одинаковые права."""
    if not user.is_staff:
        return "Команда доступна только администраторам и разработчикам."
    return None


async def _target(session: AsyncSession, command: CommandObject) -> User | None:
    raw = (command.args or "").split()
    if not raw:
        return None
    try:
        uid = int(raw[0])
    except ValueError:
        return None
    return await session.get(User, uid)


# ---------------------------------------------------------------- сообщение админу


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext) -> None:
    await state.set_state(FeedbackSG.text)
    await state.update_data(order_id=0)
    await message.answer(
        "Напишите сообщение одним текстом — передам администраторам вместе с вашим именем. "
        "/cancel — не отправлять."
    )


@router.callback_query(FbCb.filter())
async def cb_feedback_order(call: CallbackQuery, callback_data: FbCb, state: FSMContext) -> None:
    await state.set_state(FeedbackSG.text)
    await state.update_data(order_id=callback_data.order_id)
    await call.message.answer(
        f"Напишите сообщение по заказу №{callback_data.order_id} — передам администраторам "
        "вместе с номером заказа.\n/cancel — не отправлять."
    )
    await call.answer()


@router.message(StateFilter(FeedbackSG.text), F.text, NOT_COMMAND)
async def feedback_save(
    message: Message, state: FSMContext, session: AsyncSession, user: User, bot: Bot
) -> None:
    text = (message.text or "").strip()[:4000]
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0) or None
    session.add(Feedback(user_id=user.id, order_id=order_id, text=text))
    await session.commit()
    await state.clear()
    await message.answer("Сообщение отправлено. Ответ придёт сюда же.")

    head = f"<b>Сообщение от сотрудника</b>\n{esc(user.display)}, идентификатор <code>{user.id}</code>"
    if order_id:
        order = await session.get(Order, order_id)
        if order:
            head += f"\nПо заказу {esc(order.name)}"
    await notify_staff(
        bot, session, f"{head}\n\n{esc(text)}\n\nОтветить: <code>/reply {user.id} текст</code>",
        exclude=user.id,
    )


@router.message(Command("reply"))
async def cmd_reply(message: Message, command: CommandObject, user: User, bot: Bot) -> None:
    if err := _deny(user):
        await message.answer(err)
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/reply идентификатор текст ответа</code>")
        return
    try:
        uid = int(parts[0])
    except ValueError:
        await message.answer("Идентификатор должен быть числом.")
        return
    sent = await safe_send(bot, uid, f"<b>Ответ администратора</b>\n\n{esc(parts[1])}")
    await message.answer("Ответ отправлен." if sent else "Не получилось отправить: "
                         "возможно, человек остановил бота.")


# ---------------------------------------------------------------- роли и доступ


@router.message(Command("grant"))
async def cmd_grant(
    message: Message, command: CommandObject, session: AsyncSession, user: User, bot: Bot
) -> None:
    if err := _deny(user):
        await message.answer(err)
        return
    target = await _target(session, command)
    if not target:
        await message.answer(
            "Формат: <code>/grant идентификатор</code>. Человек должен хотя бы раз написать боту."
        )
        return
    if target.role is not Role.developer:
        target.role = Role.admin
    target.is_approved = True
    await session.commit()
    await message.answer(f"{esc(target.display)} теперь администратор: получает уведомления "
                         "о заказах и видит общий заказ и лабораторию в меню.")
    await safe_send(
        bot, target.id,
        "Вам выданы права администратора: уведомления о новых заказах, в главном меню — "
        "«▤ Общий заказ», «▤ Лаборатория» и «Все заказы».",
        reply_markup=main_menu(True),
    )


@router.message(Command("revoke"))
async def cmd_revoke(
    message: Message, command: CommandObject, session: AsyncSession, user: User, bot: Bot
) -> None:
    if err := _deny(user):
        await message.answer(err)
        return
    target = await _target(session, command)
    if not target:
        await message.answer("Формат: <code>/revoke идентификатор</code>")
        return
    if target.id in config.developer_ids:
        await message.answer("Разработчика из настроек снять нельзя.")
        return
    target.role = Role.worker
    await session.commit()
    await message.answer(f"{esc(target.display)} снова обычный сотрудник.")
    await safe_send(bot, target.id, "Права администратора сняты.",
                    reply_markup=await menu_for_id(session, target.id))


@router.message(Command("approve"))
async def cmd_approve(
    message: Message, command: CommandObject, session: AsyncSession, user: User, bot: Bot
) -> None:
    if err := _deny(user):
        await message.answer(err)
        return
    raw = (command.args or "").split()
    if not raw or not raw[0].lstrip("-").isdigit():
        await message.answer("Формат: <code>/approve идентификатор</code>")
        return
    target = await session.get(User, int(raw[0]))
    if target is None:
        # доступ можно выдать заранее — человек просто нажмёт /start
        target = User(id=int(raw[0]))
        session.add(target)
    target.is_approved = True
    target.is_blocked = False
    await session.commit()
    await message.answer(f"Доступ выдан: {esc(target.display)}")
    await safe_send(bot, target.id, "Доступ к боту открыт. Нажмите /start.")


@router.message(Command("block"))
async def cmd_block(
    message: Message, command: CommandObject, session: AsyncSession, user: User
) -> None:
    if err := _deny(user):
        await message.answer(err)
        return
    target = await _target(session, command)
    if not target:
        await message.answer("Формат: <code>/block идентификатор</code>")
        return
    if target.role is Role.developer or target.id in config.developer_ids:
        await message.answer("Разработчика блокировать нельзя.")
        return
    target.is_blocked = True
    target.is_approved = False
    await session.commit()
    await message.answer(f"Доступ закрыт: {esc(target.display)}")


# ---------------------------------------------------------------- отчёты


@router.message(Command("users"))
async def cmd_users(message: Message, session: AsyncSession, user: User) -> None:
    if err := _deny(user):
        await message.answer(err)
        return
    rows = (await session.scalars(select(User).order_by(User.created_at.desc()).limit(100))).all()
    lines = []
    for u in rows:
        state = "заблокирован" if u.is_blocked else ("доступ есть" if u.is_approved else "ждёт доступа")
        extra = ""
        if u.id == config.repair_chat_id:
            extra += ", мастер (заявки на помощь)"
        if u.id == config.report_chat_id:
            extra += ", получает отчёты по заявкам на помощь"
        lines.append(
            f"<code>{u.id}</code> {esc(u.display)} — {ROLE_LABELS.get(u.role, u.role.value)}, "
            f"{state}, {esc(offices.label(u.office))}{extra}"
        )
    await send_long(
        message,
        ["<b>Пользователи</b>\nИдентификатор нужен для /approve, /block, /grant.", *lines],
        sep="\n",
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, session: AsyncSession, user: User) -> None:
    """/stats — за всё время, /stats 30 — за последние 30 дней."""
    if err := _deny(user):
        await message.answer(err)
        return
    raw = (command.args or "").strip()
    days = int(raw) if raw.isdigit() and int(raw) > 0 else 0
    when = func.coalesce(Order.published_at, Order.created_at)

    def scoped(stmt):
        stmt = stmt.where(Order.status.not_in((OrderStatus.draft, OrderStatus.deleted)))
        if days:
            start = dt.datetime.combine(today() - dt.timedelta(days=days - 1), dt.time.min)
            stmt = stmt.where(when >= start)
        return stmt

    by_status = dict((await session.execute(
        scoped(select(Order.status, func.count(Order.id))).group_by(Order.status)
    )).all())
    amount = await session.scalar(scoped(
        select(func.coalesce(func.sum(Order.price * func.coalesce(Order.qty, 1)), 0.0))
    ).where(Order.status != OrderStatus.cancelled))
    by_place = (await session.execute(
        scoped(select(Order.place, func.count(Order.id),
                      func.coalesce(func.sum(Order.price * func.coalesce(Order.qty, 1)), 0.0)))
        .where(Order.status != OrderStatus.cancelled)
        .group_by(Order.place).order_by(func.count(Order.id).desc())
    )).all()
    top_users = (await session.execute(
        scoped(select(User.full_name, User.username, User.id, func.count(Order.id))
               .select_from(Order).join(User, User.id == Order.user_id))
        .group_by(User.id).order_by(func.count(Order.id).desc()).limit(5)
    )).all()
    drafts = await session.scalar(select(func.count(Order.id)).where(Order.status == OrderStatus.draft))

    arrived = (await session.execute(
        scoped(select(Order.published_at, Order.arrived_at))
        .where(Order.status == OrderStatus.arrived, Order.arrived_at.is_not(None),
               Order.published_at.is_not(None))
    )).all()
    avg_days = (
        sum((a - p).total_seconds() for p, a in arrived) / len(arrived) / 86400 if arrived else None
    )

    total = sum(by_status.values())
    title = f"за {plural(days, 'день', 'дня', 'дней')}" if days else "за всё время"
    lines = [
        f"<b>Статистика {title}</b>",
        f"{DOT} Заказов — {total}: ждут оформления {by_status.get(OrderStatus.new, 0)}, "
        f"заказано {by_status.get(OrderStatus.in_progress, 0)}, "
        f"приехали {by_status.get(OrderStatus.arrived, 0) + by_status.get(OrderStatus.done, 0)}, "
        f"отменены {by_status.get(OrderStatus.cancelled, 0)}",
        f"{DOT} Сумма (без отменённых) — {fmt_price(amount)}",
        f"{DOT} Черновиков сейчас — {drafts}",
    ]
    if avg_days is not None:
        lines.append(f"{DOT} В среднем от заказа до приезда — {avg_days:.1f} дн.")
    if by_place:
        lines.append("\n<b>По местам доставки</b>")
        for place, count, s in by_place:
            mark = " (отдельный заказ)" if offices.is_lab(place) else ""
            lines.append(f"{DOT} {esc(offices.label(place))}{mark} — {count}, {fmt_price(s)}")
    if top_users:
        lines.append("\n<b>Чаще всех заказывают</b>")
        for name, username, uid, count in top_users:
            who = name or (f"@{username}" if username else f"id{uid}")
            lines.append(f"{DOT} {esc(who)} — {count}")
    lines.append("\nПо заявкам на помощь — /repstats")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------- экспорт


@router.message(Command("export"))
async def cmd_export(
    message: Message, command: CommandObject, session: AsyncSession, user: User
) -> None:
    """Заявки на оплату по заказам в работе, по местам доставки.

    /export — по всем местам; /export црпп (или crpp) — только одно место.
    """
    if err := _deny(user):
        await message.answer(err)
        return
    orders = list((await session.scalars(
        select(Order).where(Order.status.in_(PENDING)).order_by(Order.place, Order.id)
    )).all())
    arg = (command.args or "").strip().casefold()
    if arg:
        orders = [
            o for o in orders
            if arg in (o.place or "").casefold() or arg in offices.label(o.place).casefold()
        ]
    if not orders:
        await message.answer(
            "Нечего выгружать: подходящих заказов в работе нет."
            + ("\nПроверьте название места: /export црпп" if arg else "")
        )
        return
    await send_export(message, orders, config.export_approved_by or user.display)


# ---------------------------------------------------------------- сборка: отметить пачкой


async def apply_batch(bot: Bot, session: AsyncSession, items: list[Order], arrived: bool,
                      actor: User) -> None:
    """Ставит статус пачке заказов. Каждому заказчику — одно сообщение со списком,
    к нему прикладывается меню со свежим счётчиком «В пути»."""
    stamp = now()
    for o in items:
        if arrived:
            o.status = OrderStatus.arrived
            if o.qty_arrived:  # до этого приезжал частями — теперь приехал весь остаток
                o.qty_arrived = o.quantity
            o.arrived_at = stamp
            o.ordered_at = o.ordered_at or stamp
        else:
            o.status = OrderStatus.in_progress
            o.ordered_at = stamp
    await session.commit()

    per_user: dict[int, list[Order]] = defaultdict(list)
    for o in items:
        await refresh_announce(bot, o)
        if o.user_id != actor.id:
            per_user[o.user_id].append(o)
    for uid, orders in per_user.items():
        if len(orders) == 1:
            o = orders[0]
            text = await arrived_text(session, o) if arrived else ordered_text(o)
        elif arrived:
            places = sorted({offices.label(o.place) for o in orders})
            text = (f"{DONE} <b>Приехало: {plural(len(orders), 'заказ', 'заказа', 'заказов')}</b>\n"
                    f"{short_list(orders)}\n\nЗабрать: {esc(', '.join(places))}.\n"
                    + await left_note(session, uid))
        else:
            text = (f"◐ <b>Заказано на Ozon: {plural(len(orders), 'заказ', 'заказа', 'заказов')}</b>\n"
                    f"{short_list(orders)}\n\nКогда приедет — пришлю сообщение.")
        await safe_send(bot, uid, text, reply_markup=await menu_for_id(session, uid))
    log.info("%s отметил %d поз. как %s", actor.id, len(items), "приехавшие" if arrived else "заказанные")


async def _staff_only(call: CallbackQuery, user: User) -> bool:
    if not user.is_staff:
        await call.answer("Только для администраторов", show_alert=True)
        return False
    return True


@router.callback_query(BatchCb.filter(F.a.in_({"links", "dlinks"})))
async def batch_links(call: CallbackQuery, callback_data: BatchCb, session: AsyncSession,
                      user: User) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    items = await batch_orders(session, cb.g, cb.s, cb.m, cb.d if cb.a == "dlinks" else 0)
    if not items:
        await call.answer("Список пуст", show_alert=True)
        return
    title = f"{offices.GROUPS[cb.g]} — {summary(items)}"
    await send_long(call.message, render_links(items, title).split("\n\n"))
    await call.answer()


@router.callback_query(BatchCb.filter(F.a.in_({"xlsx", "dxlsx"})))
async def batch_xlsx(call: CallbackQuery, callback_data: BatchCb, session: AsyncSession,
                     user: User) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    items = await batch_orders(session, cb.g, cb.s, cb.m, cb.d if cb.a == "dxlsx" else 0)
    if not items:
        await call.answer("Список пуст", show_alert=True)
        return
    await call.answer("Готовлю файл…")
    await send_export(call.message, items, config.export_approved_by or user.display)


_CONFIRM = {
    "mark": "Отметить <b>заказанным</b> всё ({n}) в «{g}»?\n\n"
            "Каждый заказчик получит уведомление, что его заказ оформлен.",
    "arr": "Отметить <b>приехавшим</b> всё ({n}) в «{g}»?\n\n"
           "Каждый заказчик получит уведомление, где забрать.",
}


@router.callback_query(BatchCb.filter(F.a.in_(_CONFIRM)))
async def batch_confirm(call: CallbackQuery, callback_data: BatchCb, session: AsyncSession,
                        user: User) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    items = await batch_orders(session, cb.g, cb.s, cb.m)
    if not items:
        await call.answer("Список пуст", show_alert=True)
        return
    text = _CONFIRM[cb.a].format(n=len(items), g=offices.GROUPS[cb.g]) + "\n\n" + short_list(items, 25)
    await edit(call.message, text, batch_confirm_kb(cb.a, cb.g, cb.s, cb.m, len(items)))
    await call.answer()


@router.callback_query(BatchCb.filter(F.a.in_({"mark_ok", "arr_ok"})))
async def batch_apply_all(call: CallbackQuery, callback_data: BatchCb, session: AsyncSession,
                          user: User, bot: Bot) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    items = await batch_orders(session, cb.g, cb.s, cb.m)
    if not items:
        await call.answer("Нечего отмечать — список уже пуст", show_alert=True)
        return
    await apply_batch(bot, session, items, cb.a == "arr_ok", user)
    await call.answer(f"Отмечено: {len(items)}")
    text, kb = await screen_batch(session, Ctx("b", cb.g, cb.s))
    await edit(call.message, text, kb)


# ---------------------------------------------------------------- сборка: по датам


def _sel_key(cb: BatchCb) -> str:
    return f"{cb.g}.{cb.s}.{cb.d}"


async def _selected(state: FSMContext, cb: BatchCb) -> set[int]:
    data = await state.get_data()
    sel = data.get("batch_sel") or {}
    return set(sel.get("ids", [])) if sel.get("k") == _sel_key(cb) else set()


@router.callback_query(BatchCb.filter(F.a == "day"))
async def batch_day(call: CallbackQuery, callback_data: BatchCb, state: FSMContext,
                    session: AsyncSession, user: User) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    await state.update_data(batch_sel={"k": _sel_key(cb), "ids": []})
    text, kb = await screen_batch_day(session, cb.g, cb.s, cb.d, cb.m, set())
    await edit(call.message, text, kb)
    await call.answer()


@router.callback_query(BatchCb.filter(F.a == "t"))
async def batch_toggle(call: CallbackQuery, callback_data: BatchCb, state: FSMContext,
                       session: AsyncSession, user: User) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    selected = await _selected(state, cb)
    selected ^= {cb.i}
    await state.update_data(batch_sel={"k": _sel_key(cb), "ids": sorted(selected)})
    text, kb = await screen_batch_day(session, cb.g, cb.s, cb.d, cb.m, selected)
    await edit(call.message, text, kb)
    await call.answer(f"Выбрано: {len(selected)}")


@router.callback_query(BatchCb.filter(F.a.in_({"day_all", "day_sel"})))
async def batch_day_apply(call: CallbackQuery, callback_data: BatchCb, state: FSMContext,
                          session: AsyncSession, user: User, bot: Bot) -> None:
    if not await _staff_only(call, user):
        return
    cb = callback_data
    items = await batch_orders(session, cb.g, cb.s, cb.m, cb.d)
    if cb.a == "day_sel":
        selected = await _selected(state, cb)
        items = [o for o in items if o.id in selected]
    if not items:
        await call.answer("Нечего отмечать", show_alert=True)
        return
    await apply_batch(bot, session, items, arrived=cb.s == "ord", actor=user)
    await state.update_data(batch_sel=None)
    verb = "приехало" if cb.s == "ord" else "заказано"
    await call.answer(f"Отмечено «{verb}»: {len(items)}")
    # за датой ещё что-то осталось — остаёмся на дате, иначе возвращаемся к списку дат
    rest = await batch_orders(session, cb.g, cb.s, cb.m, cb.d)
    if rest:
        text, kb = await screen_batch_day(session, cb.g, cb.s, cb.d, cb.m, set())
    else:
        text, kb = await screen_batch(session, Ctx("b", cb.g, cb.s))
    await edit(call.message, text, kb)
