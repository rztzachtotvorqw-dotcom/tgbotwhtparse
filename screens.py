from __future__ import annotations

import datetime as dt
import itertools
import math

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import offices
from config import config
from db import PENDING, REPAIR_OPEN, Order, OrderStatus, Repair, RepairStatus, User
from keyboards import (
    PERIODS,
    TABS,
    Ctx,
    back_kb,
    batch_day_kb,
    batch_kb,
    draft_kb,
    list_kb,
    more_kb,
    order_kb,
    period_kb,
    repair_card_kb,
    repairs_all_kb,
    repairs_kb,
)
from utils import esc, fmt_day, fmt_price, fmt_short, greeting, plural, shorten, today
from views import (
    LEGEND,
    RULER,
    list_row,
    mark,
    render_draft,
    render_order,
    render_repair,
    render_rows_by_day,
    repair_row,
    summary,
)

Screen = tuple[str, InlineKeyboardMarkup | None]

PAGE = 6  # ровно две строки кнопок-номеров по 3
BATCH_DAYS_PAGE = 6
ROWS_PER_DAY = 6

ASK_NAME = (
    "Напишите <b>фамилию и имя</b> — например, <i>Петров Иван</i>. Спрошу один раз: "
    "так будут называться все ваши заказы — «Петров Иван №45»."
)
ASK_OFFICE = (
    "<b>Где вы работаете?</b>\n\n"
    "Это место станет местом доставки по умолчанию. У каждого заказа его можно поменять."
)

HELP_TEXT = (
    "<b>Как заказать</b>\n"
    "Пришлите ссылку на товар Ozon — появится черновик. Проверьте количество, место и срок, "
    "нажмите «✓ Опубликовать». Всё.\n\n"
    "Заказ называется по вам: <i>Фамилия Имя №номер</i>.\n\n"
    "<b>Удобные мелочи</b>\n"
    "• К ссылке можно дописать текст: <i>3 шт чёрный</i> — бот сам поставит количество, "
    "остальное станет комментарием.\n"
    "• Несколько ссылок в одном сообщении — несколько черновиков сразу.\n"
    "• Пришлите номер заказа, например <code>45</code>, — откроется его карточка.\n"
    "• «Повторить» на карточке — новый черновик с теми же данными.\n"
    "• Пока заказ ждёт оформления, его можно «Изменить» или «Отменить».\n"
    "• На кнопке «В пути» — сколько ваших заказов ещё едет. Приехал заказ — число уменьшится.\n\n"
    "<b>Статусы</b>\n"
    f"{LEGEND}\n"
    "Статусы ставят администраторы, вам придёт сообщение.\n\n"
    "<b>Лаборатория</b> закупается отдельным заказом.\n\n"
    "<b>Заявка на помощь</b>\n"
    "Что-то сломалось или не работает — «Заявка на помощь» → «Новая заявка». Опишите проблему, можно с фото. "
    "Мастер получит заявку, а когда сделает — вы сможете оценить работу от 1 до 5.\n\n"
    "<b>Бот работает с 10 до 18, пн–пт</b>\n"
    "Для срочной связи: https://t.me/LdDsqBrSbqz"
)

# какие статусы попадают во вкладку
TAB_STATUSES: dict[str, tuple[OrderStatus, ...]] = {
    "all": (OrderStatus.new, OrderStatus.in_progress, OrderStatus.arrived,
            OrderStatus.done, OrderStatus.cancelled),
    "act": PENDING,
    "ord": (OrderStatus.in_progress,),
    "draft": (OrderStatus.draft,),
    "arr": (OrderStatus.arrived, OrderStatus.done),
    "cnl": (OrderStatus.cancelled,),
}


# ------------------------------------------------------------------ период


def ymd(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def from_ymd(v: int) -> dt.date | None:
    try:
        return dt.date(v // 10000, v // 100 % 100, v % 100) if v else None
    except ValueError:
        return None


def range_ctx(ctx: Ctx, start: dt.date, end: dt.date) -> Ctx:
    return ctx.to(per="r", p=0, d1=ymd(start), d2=ymd(end))


def period_bounds(ctx: Ctx) -> tuple[dt.date | None, dt.date | None]:
    t = today()
    if ctx.per == "t":
        return t, t
    if ctx.per == "w":
        return t - dt.timedelta(days=6), t
    if ctx.per == "m":
        return t - dt.timedelta(days=29), t
    if ctx.per == "q":
        return t - dt.timedelta(days=89), t
    if ctx.per == "r":
        return from_ymd(ctx.d1), from_ymd(ctx.d2)
    return None, None


def period_label(ctx: Ctx) -> str:
    if ctx.per == "r":
        a, b = period_bounds(ctx)
        if a and b and a == b:
            return fmt_short(a)
        return f"{fmt_short(a)}–{fmt_short(b)}"
    return PERIODS.get(ctx.per, "Всё время").lower()


# ------------------------------------------------------------------ списки


def _when():
    return func.coalesce(Order.published_at, Order.created_at)


def _scoped(stmt, user: User, ctx: Ctx):
    stmt = stmt.where(Order.status != OrderStatus.deleted)
    if ctx.kind == "a" and user.is_staff:
        stmt = stmt.where(Order.status != OrderStatus.draft)  # чужие черновики — личное
    else:
        stmt = stmt.where(Order.user_id == user.id)
    start, end = period_bounds(ctx)
    if start:
        stmt = stmt.where(_when() >= dt.datetime.combine(start, dt.time.min))
    if end:
        stmt = stmt.where(_when() < dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min))
    return stmt


async def _tab_counts(session: AsyncSession, user: User, ctx: Ctx) -> dict[str, int]:
    rows = (
        await session.execute(
            _scoped(select(Order.status, func.count(Order.id)), user, ctx).group_by(Order.status)
        )
    ).all()
    by_status = {status: n for status, n in rows}
    return {tab: sum(by_status.get(s, 0) for s in statuses) for tab, statuses in TAB_STATUSES.items()}


async def screen_list(session: AsyncSession, user: User, ctx: Ctx) -> Screen:
    if ctx.kind == "a" and not user.is_staff:
        ctx = ctx.to(kind="m")
    if ctx.st not in TABS or (ctx.kind == "a" and ctx.st == "draft"):
        ctx = ctx.to(st="all")

    counts = await _tab_counts(session, user, ctx)
    total = counts.get(ctx.st, 0)
    pages = max(1, math.ceil(total / PAGE))
    ctx = ctx.to(p=min(max(ctx.p, 0), pages - 1))

    base = _scoped(select(Order), user, ctx).where(Order.status.in_(TAB_STATUSES[ctx.st]))
    orders = list(
        (
            await session.scalars(
                base.order_by(_when().desc(), Order.id.desc()).offset(ctx.p * PAGE).limit(PAGE)
            )
        ).all()
    )
    amount = await session.scalar(
        _scoped(select(func.coalesce(func.sum(Order.price * func.coalesce(Order.qty, 1)), 0.0)), user, ctx)
        .where(Order.status.in_(TAB_STATUSES[ctx.st]))
        .where(Order.status != OrderStatus.cancelled)
    )

    title = "Все заказы" if ctx.kind == "a" else "Мои заказы"
    head = [f"<b>{title}</b> · {TABS[ctx.st].lower()} · {period_label(ctx)}"]
    if total:
        line = plural(total, "заказ", "заказа", "заказов")
        if amount:
            line += f" на {fmt_price(amount)}"
        head.append(line)
    # счётчики вкладок — строкой в тексте, а не на кнопках: кнопки остаются короткими
    others = [f"{TABS[t].lower()} {counts[t]}" for t in TABS
              if t not in ("all", ctx.st) and counts.get(t) and not (ctx.kind == "a" and t == "draft")]
    if others:
        head.append(f"<i>{' · '.join(others)}</i>")

    if not orders:
        empty = {
            "draft": "Черновиков нет. Пришлите ссылку на товар Ozon — появится черновик.",
            "act": "Сейчас ничего не ждём.",
            "ord": "Заказанного и ещё не приехавшего нет.",
        }.get(ctx.st, "Здесь пусто.")
        if ctx.per != "a":
            empty += " Попробуйте другой период."
        text = "\n".join(head) + f"\n{RULER}\n{empty}"
    else:
        text = "\n".join(head) + f"\n{RULER}" + "\n".join(
            render_rows_by_day(orders, show_name=ctx.kind != "m"))
        if ctx.st == "draft":
            text += "\n\nЧерновики видны только вам. Откройте, проверьте и опубликуйте."
    return text, list_kb(orders, ctx, counts, pages, period_label(ctx))


def screen_period(ctx: Ctx) -> Screen:
    return (
        f"<b>Период</b>\nСейчас: {period_label(ctx)}.\n\n"
        "Фильтр идёт по дате публикации заказа (у черновиков — по дате создания).",
        period_kb(ctx),
    )


# ------------------------------------------------------------------ карточка


async def screen_order(session: AsyncSession, user: User, order_id: int, ctx: Ctx) -> Screen:
    order = await session.get(Order, order_id)
    if not order or order.status is OrderStatus.deleted:
        return (f"Заказ №{order_id} не найден: возможно, он удалён.", back_kb(ctx))
    if order.is_draft:
        if order.user_id != user.id:
            return ("Это чужой черновик — он виден только автору.", back_kb(ctx))
        return render_draft(order), draft_kb(order)
    if order.user_id != user.id and not user.is_staff:
        return ("Это заказ коллеги — его видят только автор и администраторы.", back_kb(ctx))
    return render_order(order), order_kb(order, user, ctx)


# ------------------------------------------------------------------ сборка

STAGES = {"new": (OrderStatus.new,), "ord": (OrderStatus.in_progress,)}
STAGE_TEXT = {"new": "ждут оформления", "ord": "заказано, едет"}


def batch_day(o: Order, stage: str) -> dt.date:
    """Ждущие группируем по дню публикации, заказанные — по дню заказа на Ozon:
    так «пришла часть заказа от 3 ноября» — это одна дата."""
    if stage == "ord":
        return (o.ordered_at or o.when).date()
    return o.when.date()


async def batch_orders(
    session: AsyncSession, group: str, stage: str, max_id: int = 0, day: int = 0
) -> list[Order]:
    stmt = select(Order).where(Order.status.in_(STAGES.get(stage, STAGES["new"])))
    if max_id:
        stmt = stmt.where(Order.id <= max_id)
    rows = (await session.scalars(stmt.order_by(Order.id))).all()
    items = [o for o in rows if offices.group_of(o.place) == group]
    if day:
        items = [o for o in items if ymd(batch_day(o, stage)) == day]
    # свежие даты сверху, внутри даты — по местам доставки
    items.sort(key=lambda o: (-ymd(batch_day(o, stage)), offices.label(o.place), o.id))
    return items


async def batch_counts(session: AsyncSession) -> dict[tuple[str, str], int]:
    rows = (
        await session.execute(
            select(Order.place, Order.status, func.count(Order.id))
            .where(Order.status.in_(PENDING))
            .group_by(Order.place, Order.status)
        )
    ).all()
    out: dict[tuple[str, str], int] = {}
    for place, status, n in rows:
        stage = "new" if status is OrderStatus.new else "ord"
        key = (offices.group_of(place), stage)
        out[key] = out.get(key, 0) + n
    return out


def _batch_row(o: Order) -> str:
    money = f"{o.quantity} шт."
    if o.price is not None:
        money += f" × {fmt_price(o.price)}" if o.quantity > 1 else f", {fmt_price(o.price)}"
    if o.is_partial:  # часть уже приехала — показываем, сколько ещё ждём
        money = f"ждём {o.left_qty} из {o.quantity} шт."
    overdue = o.deadline and o.deadline < today()
    details = [money, esc(offices.label(o.place)),
               f"<b>срок вышел {fmt_short(o.deadline)}</b>" if overdue else f"до {fmt_short(o.deadline)}"]
    row = f"{mark(o)} <b>{esc(o.name)}</b>\n      {' · '.join(details)}"
    if o.comment:
        row += f"\n      <i>{esc(shorten(o.comment, 90))}</i>"
    return row


def group_by_day(items: list[Order], stage: str) -> list[tuple[dt.date, list[Order]]]:
    return [(d, list(g)) for d, g in itertools.groupby(items, key=lambda o: batch_day(o, stage))]


async def screen_batch(session: AsyncSession, ctx: Ctx) -> Screen:
    g = ctx.st if ctx.st in offices.GROUPS else offices.GROUP_MAIN
    s = ctx.per if ctx.per in STAGES else "new"
    items = await batch_orders(session, g, s)
    counts = await batch_counts(session)
    days = group_by_day(items, s)
    pages = max(1, math.ceil(len(days) / BATCH_DAYS_PAGE))
    ctx = Ctx("b", g, s, min(max(ctx.p, 0), pages - 1))
    page_days = days[ctx.p * BATCH_DAYS_PAGE:(ctx.p + 1) * BATCH_DAYS_PAGE]

    lines = [
        f"<b>▤ {offices.GROUPS[g]}</b>"
        + (" — отдельный заказ на Ozon" if g == offices.GROUP_LAB else f" — {offices.group_places(g)}"),
        f"{STAGE_TEXT[s].capitalize()}: " + (summary(items) if items else "пусто"),
        RULER,
    ]
    if not items:
        lines.append("Новых позиций нет." if s == "new" else "Всё заказанное уже приехало.")
    for day, day_items in page_days:
        lines.append(f"\n<b>{fmt_day(day)}</b> — {len(day_items)} поз.")
        lines += [_batch_row(o) for o in day_items[:ROWS_PER_DAY]]
        if len(day_items) > ROWS_PER_DAY:
            lines.append(f"      … и ещё {len(day_items) - ROWS_PER_DAY} — откройте дату")
    if items:
        hint = ("Оформили на Ozon — откройте дату и нажмите «◐ Всё» "
                "(или отметьте галочками часть)." if s == "new" else
                "Пришла часть заказа — нажмите на дату заказа и отметьте, что приехало. "
                "Пришла не вся позиция (2 из 5 шт.) — пришлите номер заказа и нажмите «◑ Частично».")
        lines += ["", hint]
    max_id = max((o.id for o in items), default=0)
    return "\n".join(lines), batch_kb(ctx, page_days, counts, pages, max_id, bool(items))


async def screen_batch_day(
    session: AsyncSession, g: str, s: str, d: int, m: int, selected: set[int]
) -> Screen:
    items = (await batch_orders(session, g, s, m, d))[:50]
    day = from_ymd(d)
    selected = {i for i in selected if any(o.id == i for o in items)}
    head = (f"<b>▤ {offices.GROUPS[g]} · {STAGE_TEXT[s]}</b>\n"
            f"<b>{fmt_day(day) if day else d}</b> — " + (summary(items) if items else "пусто"))
    lines = [head, RULER]
    if not items:
        lines.append("За эту дату ничего не осталось — всё отмечено.")
    lines += [_batch_row(o) for o in items]
    verb = "заказано" if s == "new" else "приехало"
    if items:
        lines += ["", f"Отметьте галочками, что {verb}, или нажмите «Всё». "
                      "Заказчики получат уведомления."]
    return "\n".join(lines), batch_day_kb(g, s, d, m, items, selected, Ctx("b", g, s))


# ------------------------------------------------------------------ починка


def can_handle_repairs(user: User) -> bool:
    """Мастер из REPAIR_CHAT_ID, администраторы и разработчики."""
    return user.is_staff or user.id == config.repair_chat_id


def can_view_repairs(user: User) -> bool:
    return can_handle_repairs(user) or user.id == config.report_chat_id


async def open_repairs(session: AsyncSession) -> list[Repair]:
    return list((await session.scalars(
        select(Repair).where(Repair.status.in_(REPAIR_OPEN)).order_by(Repair.id)
    )).all())


async def screen_repairs(session: AsyncSession, user: User) -> Screen:
    mine = list((await session.scalars(
        select(Repair).where(Repair.user_id == user.id, Repair.status != RepairStatus.cancelled)
        .order_by(Repair.id.desc()).limit(10)
    )).all())
    lines = ["<b>Заявки на помощь</b>", "Что-то сломалось или не работает в офисе или лаборатории — оставьте заявку, мастер "
             "получит её сразу.", RULER]
    if mine:
        lines.append("<b>Ваши заявки</b>")
        lines += [repair_row(r, show_user=False) for r in mine]
    else:
        lines.append("Заявок пока нет.")
    opened = await open_repairs(session) if can_view_repairs(user) else []
    return "\n".join(lines), repairs_kb(mine, can_view_repairs(user), len(opened))


async def screen_repairs_all(session: AsyncSession) -> Screen:
    items = await open_repairs(session)
    lines = [f"<b>Открытые заявки на помощь: {len(items)}</b>", RULER]
    lines += [repair_row(r, show_user=True) for r in items] or ["Открытых заявок нет."]
    return "\n".join(lines), repairs_all_kb(items)


async def screen_repair(session: AsyncSession, user: User, repair_id: int) -> Screen:
    r = await session.get(Repair, repair_id)
    if r is None or (r.user_id != user.id and not can_view_repairs(user)):
        return ("Заявка не найдена.", None)
    return render_repair(r), repair_card_kb(r, user, can_handle_repairs(user))


# ------------------------------------------------------------------ прочее


async def screen_more(user: User) -> Screen:
    text = (
        f"{greeting()}, <b>{esc(user.display)}</b>.\n\n"
        f"• Рабочее место — {esc(offices.label(user.office))}\n"
        f"• Срок по умолчанию — {config.default_deadline_days} дн.\n\n"
        "Чтобы заказать, просто пришлите ссылку на товар Ozon."
    )
    return text, more_kb(user.is_staff)


async def resolve_ctx(session: AsyncSession, user: User, ctx: Ctx) -> Screen:
    if ctx.kind == "b":
        if not user.is_staff:
            return ("Раздел доступен только администраторам.", None)
        return await screen_batch(session, ctx)
    return await screen_list(session, user, ctx)


__all__ = ["list_row"]
