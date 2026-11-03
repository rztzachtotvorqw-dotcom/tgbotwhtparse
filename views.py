from __future__ import annotations

import datetime as dt
import itertools

import offices
from db import Order, OrderStatus, Repair, RepairStatus
from utils import (
    deadline_note,
    esc,
    fmt_date,
    fmt_day,
    fmt_dt,
    fmt_price,
    fmt_short,
    plural,
    shorten,
    today,
)

# ------------------------------------------------------------------ стиль
# Единственное место с оформлением: текстовые маркеры вместо эмодзи.
DOT = "•"   # строка карточки, пункт списка
SOFT = "◦"  # второстепенное
DONE = "✓"  # приехал
OFF = "✕"   # отменён, удалён
RULER = "─" * 18

STATUS_TEXT = {
    OrderStatus.draft: "черновик",
    OrderStatus.new: "ждёт оформления",
    OrderStatus.in_progress: "заказано",
    OrderStatus.arrived: "приехал",
    OrderStatus.done: "приехал",
    OrderStatus.cancelled: "отменён",
    OrderStatus.deleted: "удалён",
}

# ○ ждёт → ◐ заказано → ✓ приехал
STATUS_MARK = {
    OrderStatus.draft: "✎",
    OrderStatus.new: "○",
    OrderStatus.in_progress: "◐",
    OrderStatus.arrived: DONE,
    OrderStatus.done: DONE,
    OrderStatus.cancelled: OFF,
    OrderStatus.deleted: OFF,
}

PART = "◑"  # приехала часть, остаток ещё едет

LEGEND = "○ ждёт оформления · ◐ заказано · ◑ приехала часть · ✓ приехал · ✕ отменён"


def mark(order: Order) -> str:
    if order.is_partial and order.is_pending:
        return PART
    return STATUS_MARK.get(order.status, DOT)


def status_text(order: Order) -> str:
    """«приехало 2 из 5» для частично приехавшего, иначе обычный статус."""
    if order.is_partial and order.status is not OrderStatus.cancelled:
        return f"приехало {order.arrived_qty} из {order.quantity}"
    return STATUS_TEXT.get(order.status, order.status.value)


def _overdue(order: Order) -> bool:
    return bool(order.deadline and order.deadline < today() and order.is_pending)


def _money_line(order: Order) -> str:
    qty = order.quantity
    if order.price is None:
        return f"{DOT} {qty} шт. · цена не указана"
    if qty == 1:
        return f"{DOT} 1 шт. · {fmt_price(order.price)}"
    return f"{DOT} {qty} шт. × {fmt_price(order.price)} = <b>{fmt_price(order.total)}</b>"


def _place_line(order: Order) -> str:
    line = f"{DOT} Куда: {esc(offices.label(order.place))}"
    if offices.is_lab(order.place):
        line += " — отдельный заказ"
    return line


def _deadline_line(order: Order) -> str:
    line = f"{DOT} Срок: до {fmt_date(order.deadline)}"
    note = deadline_note(order.deadline) if order.is_pending or order.is_draft else ""
    return f"{line} ({note})" if note else line


def _part_lines(order: Order) -> list[str]:
    if not order.is_partial:
        return []
    line = f"{PART} Приехало {order.arrived_qty} из {order.quantity} шт."
    if order.is_pending:
        return [line + f", ждём ещё {order.left_qty}"]
    lines = [line]
    if order.cancel_reason:
        lines.append(f"{OFF} {esc(order.cancel_reason)}")
    return lines


def _body(order: Order) -> list[str]:
    lines = [_money_line(order), *_part_lines(order), _place_line(order), _deadline_line(order)]
    if order.comment:
        lines.append(f"{DOT} Комментарий: <i>{esc(order.comment)}</i>")
    return lines


# Ссылка на товар — кнопкой «Ozon ↗» под сообщением, в тексте не дублируем.
# Одна линия под заголовком задаёт ширину сообщения, чтобы кнопки не сжимались.

# ------------------------------------------------------------------ черновик


def render_draft(order: Order, hint: str = "", duplicate: str = "") -> str:
    lines = [f"✎ <b>Черновик · {esc(order.name)}</b>", RULER, *_body(order)]
    tips: list[str] = []
    if not order.comment:
        tips.append("Добавьте комментарий, если важны цвет, размер или «срочно».")
    if duplicate:
        tips.append(duplicate)
    if tips:
        lines.append("")
        lines += [f"{SOFT} {t}" for t in tips]
    if hint:
        lines += ["", hint]
    return "\n".join(lines)


# ------------------------------------------------------------------ заказ


def render_order(order: Order) -> str:
    head = f"{mark(order)} <b>{esc(order.name)}</b> — {status_text(order)}"
    lines = [head, RULER, *_body(order), ""]
    lines.append(f"{SOFT} Опубликован: {fmt_dt(order.when)}")
    if order.ordered_at:
        lines.append(f"{SOFT} Заказано на Ozon: {fmt_dt(order.ordered_at)}")
    if order.arrived_at and order.status in (OrderStatus.arrived, OrderStatus.done):
        lines.append(f"{DONE} Приехал: {fmt_dt(order.arrived_at)}")
    if order.status is OrderStatus.cancelled:
        lines.append(f"{OFF} Причина отмены: {esc(order.cancel_reason or 'не указана')}")
    return "\n".join(lines)


_ANNOUNCE_HEAD = {
    OrderStatus.in_progress: "◐ <b>{name}</b> — заказано",
    OrderStatus.arrived: f"{DONE} <b>{{name}}</b> — приехал",
    OrderStatus.done: f"{DONE} <b>{{name}}</b> — приехал",
    OrderStatus.cancelled: f"{OFF} <b>{{name}}</b> — отменён",
    OrderStatus.deleted: f"{OFF} <b>{{name}}</b> — удалён",
    OrderStatus.draft: f"{SOFT} <b>{{name}}</b> — отозван автором на правку",
}


def render_announce(order: Order, big: bool = False) -> str:
    """Уведомление администраторам о заказе."""
    name = esc(order.name)
    if order.status is OrderStatus.new:
        group = offices.GROUPS[offices.group_of(order.place)]
        head = f"○ <b>Новый заказ: {name}</b> · {group}"
    elif order.is_partial and order.status is not OrderStatus.cancelled:
        head = f"{mark(order)} <b>{name}</b> — {status_text(order)}"
    else:
        head = _ANNOUNCE_HEAD.get(order.status, "<b>{name}</b>").format(name=name)
    lines = [head, RULER, *_body(order)]
    if big:
        lines.append(f"{SOFT} Крупный заказ — стоит посмотреть внимательнее.")
    if order.status is OrderStatus.cancelled:
        lines.append(f"{OFF} Причина: {esc(order.cancel_reason or 'не указана')}")
    if order.status is OrderStatus.draft:
        lines.append(f"{SOFT} После правки придёт новое уведомление.")
    return "\n".join(lines)


# ------------------------------------------------------------------ списки


def list_row(o: Order, show_status: bool = True, show_name: bool = True) -> str:
    """В своих заказах имя не повторяем — только номер: «○ №116 · 2 шт. · Т4»."""
    money = f"{o.quantity} шт."
    if o.total is not None:
        money += f", {fmt_price(o.total)}"
    details = [money]
    if show_status:
        details.append(status_text(o))
    details.append(esc(offices.label(o.place)))
    if o.is_pending or o.is_draft:
        details.append("<b>срок вышел</b>" if _overdue(o) else f"до {fmt_short(o.deadline)}")
    title = esc(o.name) if show_name else f"№{o.id}"
    row = f"{mark(o)} <b>{title}</b>\n      {' · '.join(details)}"
    if o.comment:
        row += f"\n      <i>{esc(shorten(o.comment, 70))}</i>"
    return row


def render_rows_by_day(orders: list[Order], day_of=None, show_name: bool = True) -> list[str]:
    """Заказы, сгруппированные по дням: «Сегодня», «Вчера», «3 ноября, пн»."""
    day_of = day_of or (lambda o: o.when.date())
    out: list[str] = []
    for day, group in itertools.groupby(orders, key=day_of):
        out.append(f"\n<b>{fmt_day(day)}</b>")
        out += [list_row(o, show_name=show_name) for o in group]
    return out


def render_links(orders: list[Order], title: str = "Ссылки списком") -> str:
    parts = [f"<b>{esc(title)}</b>\nМожно скопировать целиком и открывать по очереди."]
    for i, o in enumerate(orders, 1):
        head = f"{i}. {esc(o.name)} — {o.quantity} шт."
        if o.total is not None:
            head += f", {fmt_price(o.total)}"
        if o.comment:
            head += f"\n<i>{esc(shorten(o.comment, 100))}</i>"
        parts.append(f"{head}\n{esc(o.url)}")
    return "\n\n".join(parts)


def summary(orders: list[Order]) -> str:
    """«8 позиций на 32 400 ₽, без цены — 2»."""
    total = sum(o.total for o in orders if o.total is not None)
    no_price = sum(1 for o in orders if o.total is None)
    text = plural(len(orders), "позиция", "позиции", "позиций")
    if total:
        text += f" на {fmt_price(total)}"
    if no_price and total:
        text += f", без цены — {no_price}"
    return text


def short_list(orders: list[Order], limit: int = 20) -> str:
    rows = [f"{DOT} {esc(o.name)} — {o.quantity} шт." for o in orders[:limit]]
    if len(orders) > limit:
        rows.append(f"… и ещё {len(orders) - limit}")
    return "\n".join(rows)


# ------------------------------------------------------------------ починка

REPAIR_TEXT = {
    RepairStatus.draft: "не отправлена",
    RepairStatus.new: "ждёт мастера",
    RepairStatus.in_work: "в работе",
    RepairStatus.done: "выполнена",
    RepairStatus.rejected: "отклонена",
    RepairStatus.cancelled: "отозвана",
}
REPAIR_MARK = {
    RepairStatus.draft: "✎",
    RepairStatus.new: "○",
    RepairStatus.in_work: "◐",
    RepairStatus.done: DONE,
    RepairStatus.rejected: OFF,
    RepairStatus.cancelled: OFF,
}
STARS = {1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★"}


def _duration(a: dt.datetime | None, b: dt.datetime | None) -> str:
    if not a or not b:
        return "—"
    minutes = int((b - a).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} мин."
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч {minutes % 60} мин."
    return f"{hours // 24} дн. {hours % 24} ч"


def render_repair(r: Repair, head: str = "") -> str:
    lines = [
        head or f"{REPAIR_MARK[r.status]} <b>Заявка {r.code}</b> — {REPAIR_TEXT[r.status]}",
        RULER,
        esc(r.text) if r.text else "<i>без описания</i>",
        "",
        f"{DOT} Где: {esc(offices.label(r.place))}",
        f"{DOT} Заявитель: {esc(r.user.display)}",
    ]
    if r.photo_id:
        lines.append(f"{DOT} Фото приложено")
    if r.sent_at:
        lines.append(f"{DOT} Подана: {fmt_dt(r.sent_at)}")
    if r.taken_at and r.status is not RepairStatus.new:
        lines.append(f"{DOT} Взята в работу: {fmt_dt(r.taken_at)}")
    if r.done_at and r.status is RepairStatus.done:
        lines.append(f"{DONE} Выполнена: {fmt_dt(r.done_at)} (за {_duration(r.sent_at, r.done_at)})")
    if r.status is RepairStatus.rejected:
        lines.append(f"{OFF} Причина: {esc(r.reject_reason or 'не указана')}")
    if r.returned:
        lines.append(f"{SOFT} Возвращалась «не решено»: {r.returned} раз")
    if r.rating:
        line = f"{DOT} Оценка: {STARS[r.rating]} {r.rating}/5"
        if r.rating_comment:
            line += f" — <i>{esc(r.rating_comment)}</i>"
        lines.append(line)
    return "\n".join(lines)


def repair_row(r: Repair, show_user: bool) -> str:
    who = f" · {esc(r.user.display)}" if show_user else ""
    stars = f" · {r.rating}★" if r.rating else ""
    return (
        f"{REPAIR_MARK[r.status]} <b>{r.code}</b> {esc(shorten(r.text, 50))}\n"
        f"      {REPAIR_TEXT[r.status]} · {esc(offices.label(r.place))} · "
        f"{fmt_short(r.sent_at or r.created_at)}{who}{stars}"
    )


def render_report(r: Repair, master: str) -> str:
    """Отчёт для REPORT_CHAT_ID: что сделали, как быстро и как оценили."""
    head = {
        RepairStatus.done: f"{DONE} <b>Отчёт: заявка {r.code} выполнена</b>",
        RepairStatus.rejected: f"{OFF} <b>Отчёт: заявка {r.code} отклонена</b>",
        RepairStatus.in_work: f"◐ <b>Отчёт: заявка {r.code} возвращена в работу</b>",
    }.get(r.status, f"<b>Отчёт: заявка {r.code}</b>")
    lines = [
        head,
        RULER,
        esc(shorten(r.text, 600)),
        "",
        f"{DOT} Где: {esc(offices.label(r.place))}",
        f"{DOT} Заявитель: {esc(r.user.display)}",
        f"{DOT} Исполнитель: {esc(master)}",
        f"{DOT} Подана: {fmt_dt(r.sent_at)}",
    ]
    if r.status is RepairStatus.done:
        lines.append(f"{DOT} Выполнена: {fmt_dt(r.done_at)}, за {_duration(r.sent_at, r.done_at)}")
    if r.status is RepairStatus.rejected:
        lines.append(f"{DOT} Причина отказа: {esc(r.reject_reason or 'не указана')}")
    if r.returned:
        lines.append(f"{SOFT} Заявитель возвращал «не решено»: {r.returned} раз")
    if r.rating:
        lines.append(f"{DOT} Оценка заявителя: {STARS[r.rating]} {r.rating}/5")
        if r.rating_comment:
            lines.append(f"{DOT} Отзыв: <i>{esc(r.rating_comment)}</i>")
    elif r.status is RepairStatus.done:
        lines.append(f"{SOFT} Оценки пока нет — придёт сюда же.")
    return "\n".join(lines)
