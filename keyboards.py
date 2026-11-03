from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import offices
from db import Order, OrderStatus, Repair, RepairStatus, User
from utils import fmt_day, shorten
from views import DONE, DOT, OFF, REPAIR_MARK, mark

# ------------------------------------------------------------------ постоянное меню

BTN_MY = "Мои заказы"
BTN_ACTIVE = "В пути"  # на кнопке ещё счётчик: «В пути · 3»
BTN_DRAFTS = "Черновики"
BTN_REPAIR = "Заявка на помощь"
BTN_REPAIR_OLD = ("Помощь", "Починка")  # кнопки прежних версий меню
BTN_MORE = "Ещё"
BTN_BATCH = "Сборка"  # старая кнопка 1.1 — ещё может висеть у админов, ведёт в общий заказ
BTN_ALL = "Все заказы"
# две отдельные кнопки у админов: общий заказ (Т4, ЦМНТ, ЦРПП) и лаборатория
BTN_GROUP = {g: f"▤ {name}" for g, name in offices.GROUPS.items()}
GROUP_BY_BTN = {text: g for g, text in BTN_GROUP.items()}


def main_menu(is_staff: bool = False, pending: int | None = None) -> ReplyKeyboardMarkup:
    """Ровная сетка 2 × N. Черновики — вкладка в «Моих заказах» и пункт в «Ещё»."""
    if is_staff:
        # «В пути» у админа — вкладка в «Моих заказах», зато сетка ровная 2 × 3
        grid = [
            (BTN_GROUP[offices.GROUP_MAIN], BTN_GROUP[offices.GROUP_LAB]),
            (BTN_MY, BTN_ALL),
            (BTN_REPAIR, BTN_MORE),
        ]
    else:
        active = f"{BTN_ACTIVE} · {pending}" if pending else BTN_ACTIVE
        grid = [
            (BTN_MY, active),
            (BTN_REPAIR, BTN_MORE),
        ]
    rows = [[KeyboardButton(text=t) for t in row] for row in grid]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пришлите ссылку на Ozon…",
    )


# ------------------------------------------------------------------ контекст списка
# Контекст едет в callback_data, чтобы «Назад» из карточки возвращал
# ровно в тот список, страницу и фильтр, откуда её открыли.

TABS: dict[str, str] = {
    "all": "Все",
    "act": "В пути",
    "ord": "Заказано",
    "arr": "Приехали",
    "cnl": "Отменены",
    "draft": "Черновики",
}
PERIODS: dict[str, str] = {
    "t": "Сегодня",
    "w": "Неделя",
    "m": "Месяц",
    "q": "3 месяца",
    "a": "Всё время",
}


@dataclass(slots=True)
class Ctx:
    kind: str = "m"  # m — мои заказы, a — все заказы (админ), b — сборка, "" — нет списка
    st: str = "all"  # вкладка статуса; для сборки — группа main | lab
    per: str = "a"  # период t|w|m|q|a|r; для сборки — этап new | ord
    p: int = 0  # страница
    d1: int = 0  # свой период: yyyymmdd
    d2: int = 0

    def pack(self) -> str:
        return f"{self.kind}.{self.st}.{self.per}.{self.p}.{self.d1}.{self.d2}"

    @classmethod
    def unpack(cls, raw: str | None) -> "Ctx":
        if not raw:
            return cls(kind="")
        parts = raw.split(".")
        try:
            return cls(parts[0], parts[1], parts[2], int(parts[3]), int(parts[4]), int(parts[5]))
        except (IndexError, ValueError):
            return cls()

    def to(self, **kw) -> "Ctx":
        return dataclasses.replace(self, **kw)


MY_DRAFTS = Ctx("m", "draft", "a")
MY_ACTIVE = Ctx("m", "act", "a")
MY_ALL = Ctx("m", "all", "a")
ALL_ORDERS = Ctx("a", "all", "m")
BATCH_MAIN = Ctx("b", offices.GROUP_MAIN, "new")
BATCH_LAB = Ctx("b", offices.GROUP_LAB, "new")


# ------------------------------------------------------------------ callback data


class NavCb(CallbackData, prefix="nav"):
    to: str  # more | help | office | name | feedback | admin | period | range | noop
    c: str = ""


class LCb(CallbackData, prefix="l"):
    """Открыть список с контекстом."""

    c: str


class OCb(CallbackData, prefix="o"):
    """Открыть карточку заказа; c — куда вернуться."""

    id: int
    c: str = ""


class OrderCb(CallbackData, prefix="ord"):
    action: str
    order_id: int = 0
    c: str = ""


class QtyCb(CallbackData, prefix="qty"):
    order_id: int
    value: int = 1  # 0 = ввести своё число


class DeadlineCb(CallbackData, prefix="dl"):
    order_id: int
    kind: str = "default"  # default | plus | custom
    days: int = 0


class PlaceCb(CallbackData, prefix="plc"):
    order_id: int
    code: str  # код офиса | custom


class CancelCb(CallbackData, prefix="cr"):
    order_id: int
    r: int  # индекс в CANCEL_REASONS, -1 — своя причина
    c: str = ""


class PartCb(CallbackData, prefix="pt"):
    """Приехала часть заказа: k — сколько штук; r — что с остатком:
    "" — спросить, w — ждём остаток, oz — отменён Ozon, no — не нужен, n — ввести число."""

    id: int
    k: int = 0
    r: str = ""
    c: str = ""


class OfficeCb(CallbackData, prefix="ofc"):
    code: str


class BatchCb(CallbackData, prefix="b"):
    # links | xlsx | mark | mark_ok | arr | arr_ok — вся группа
    # day — открыть дату; t — галочка; day_all | day_sel — отметить дату / выбранные
    # dlinks | dxlsx — ссылки и заявка за дату
    a: str
    g: str = offices.GROUP_MAIN
    s: str = "new"
    m: int = 0  # максимальный id на момент показа — новые заказы не зацепит
    d: int = 0  # дата yyyymmdd
    i: int = 0  # id заказа для галочки


class FbCb(CallbackData, prefix="fb"):
    order_id: int = 0


class RepCb(CallbackData, prefix="rp"):
    # my | new | open | place | send | drop | cancel | take | done | rej | rej_ok
    # rate | skip | back | all
    a: str
    id: int = 0
    v: int = 0


CANCEL_REASONS = (
    "Передумали, уже не нужно",
    "Нет в наличии",
    "Купили в другом месте",
    "Ошибка в заказе",
    "Отменён Ozon",  # только для админов, у них — первой строкой
)
OZON_REASON = len(CANCEL_REASONS) - 1
PART_REST = {"oz": "отменён Ozon", "no": "не нужен, отменён"}
REJECT_REASONS = (
    "Не по нашей части",
    "Нужна закупка запчастей",
    "Повторная заявка",
    "Не удалось найти проблему",
)


def _btn(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def _sel(text: str, selected: bool) -> str:
    return f"{DOT} {text}" if selected else text


# Раскладка: главная кнопка — сверху на всю ширину, остальные — ровной сеткой
# по 2 в ряд с короткими подписями, «Назад» — внизу. Короткие подписи не обрезаются
# Telegram'ом в «…», а одинаковые ряды выглядят соразмерно с текстом сообщения.


def _grid(kb: InlineKeyboardBuilder, buttons: list[InlineKeyboardButton], cols: int = 2) -> None:
    buttons = [b for b in buttons if b is not None]
    for i in range(0, len(buttons), cols):
        kb.row(*buttons[i:i + cols])


def _ozon(url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Ozon ↗", url=url)


def _pager(kb: InlineKeyboardBuilder, page: int, pages: int, make) -> None:
    if pages <= 1:
        return
    kb.row(
        _btn("‹", make(page - 1)) if page > 0 else _btn("·", NavCb(to="noop")),
        _btn(f"{page + 1} из {pages}", NavCb(to="noop")),
        _btn("›", make(page + 1)) if page + 1 < pages else _btn("·", NavCb(to="noop")),
    )


def back_to_draft(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("‹ Назад", OCb(id=order_id)))
    return kb.as_markup()


def back_kb(ctx: Ctx, text: str = "‹ Назад") -> InlineKeyboardMarkup | None:
    if not ctx.kind:
        return None
    kb = InlineKeyboardBuilder()
    kb.row(_btn(text, LCb(c=ctx.pack())))
    return kb.as_markup()


# ------------------------------------------------------------------ рабочее место


def office_kb() -> InlineKeyboardMarkup:
    """Спрашиваем один раз при первом запуске."""
    kb = InlineKeyboardBuilder()
    for code, name in offices.OFFICES.items():
        kb.button(text=name, callback_data=OfficeCb(code=code))
    kb.adjust(2)
    return kb.as_markup()


def place_kb(order_id: int, current: str | None) -> InlineKeyboardMarkup:
    """Куда везти конкретный заказ: офисы + свой вариант."""
    kb = InlineKeyboardBuilder()
    for code, name in offices.OFFICES.items():
        text = f"{DONE} {name}" if code == current else name
        kb.button(text=text, callback_data=PlaceCb(order_id=order_id, code=code))
    kb.adjust(2)
    kb.row(_btn("Другое…", PlaceCb(order_id=order_id, code="custom")), _btn("‹ Назад", OCb(id=order_id)))
    return kb.as_markup()


# ------------------------------------------------------------------ черновик


def draft_kb(order: Order) -> InlineKeyboardMarkup:
    """Значения (кол-во, место, срок) видны в тексте — на кнопках только действия."""
    oid = order.id

    def act(text: str, action: str) -> InlineKeyboardButton:
        return _btn(text, OrderCb(action=action, order_id=oid))

    kb = InlineKeyboardBuilder()
    kb.row(act(f"{DONE} Опубликовать", "publish"))
    _grid(kb, [
        act("Количество", "qty"), act("Цена", "price"),
        act("Срок", "deadline"), act("Место", "place"),
        act("Комментарий", "comment"), act("Ссылка", "link"),
        _ozon(order.url), act("Удалить", "drop"),
    ])
    return kb.as_markup()


def qty_kb(order_id: int, current: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("−1", QtyCb(order_id=order_id, value=max(current - 1, 1))),
        _btn("+1", QtyCb(order_id=order_id, value=current + 1)),
    )
    _grid(kb, [
        _btn(f"{DONE} {n}" if n == current else str(n), QtyCb(order_id=order_id, value=n))
        for n in (1, 2, 3, 5, 10, 20)
    ], cols=3)
    kb.row(_btn("Другое…", QtyCb(order_id=order_id, value=0)), _btn("‹ Назад", OCb(id=order_id)))
    return kb.as_markup()


def deadline_kb(order_id: int, default_days: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"Обычный · {default_days} дн.", DeadlineCb(order_id=order_id, kind="default")))
    _grid(kb, [
        _btn(f"+{days} дн.", DeadlineCb(order_id=order_id, kind="plus", days=days)) for days in (1, 3, 7)
    ], cols=3)
    kb.row(_btn("Своя дата…", DeadlineCb(order_id=order_id, kind="custom")), _btn("‹ Назад", OCb(id=order_id)))
    return kb.as_markup()


def _draft_label(o: Order) -> str:
    """«✎ №115 · перчатки M» — свои черновики, имя не повторяем."""
    tail = shorten(o.comment, 14) if o.comment else f"{o.quantity} шт."
    return f"✎ №{o.id} · {tail}"


def created_kb(orders: list[Order]) -> InlineKeyboardMarkup:
    """После пачки ссылок: открыть каждый черновик или опубликовать все разом."""
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"{DONE} Опубликовать все", OrderCb(action="pub_all")))
    _grid(kb, [_btn(_draft_label(o), OCb(id=o.id, c=MY_DRAFTS.pack())) for o in orders])
    return kb.as_markup()


def pub_all_kb(count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn(f"{DONE} Да, все {count}", OrderCb(action="pub_all_ok")),
        _btn("Отмена", LCb(c=MY_DRAFTS.pack())),
    )
    return kb.as_markup()


# ------------------------------------------------------------------ опубликованный заказ


def order_kb(order: Order, viewer: User, ctx: Ctx) -> InlineKeyboardMarkup:
    """Статусы «заказано», «приехал», «вернуть в работу» ставят только админы и разработчики."""
    oid, c = order.id, ctx.pack()
    owner = order.user_id == viewer.id
    staff = viewer.is_staff
    manage = owner or staff

    def act(text: str, action: str) -> InlineKeyboardButton:
        return _btn(text, OrderCb(action=action, order_id=oid, c=c))

    status = order.status
    buttons: list[InlineKeyboardButton] = []
    if staff:
        if status is OrderStatus.new:
            buttons += [act("◐ Заказано", "ordered"), act(f"{DONE} Приехал", "arrived")]
        elif status is OrderStatus.in_progress:
            buttons.append(act(f"{DONE} Приехал", "arrived"))
        elif status in (OrderStatus.arrived, OrderStatus.done, OrderStatus.cancelled):
            buttons.append(act("↺ В работу", "reopen"))
        if order.can_split:
            buttons.append(_btn("◑ Частично", PartCb(id=oid, c=c)))
    buttons.append(_ozon(order.url))
    if owner and status is OrderStatus.new:
        buttons.append(act("Изменить", "edit"))
    if manage and order.is_pending:
        buttons.append(act("Отменить", "cancel"))
    buttons.append(act("Повторить", "repeat"))
    if owner and not staff:
        buttons.append(_btn("Написать", FbCb(order_id=oid)))
    if manage:
        buttons.append(act("Удалить", "del"))

    kb = InlineKeyboardBuilder()
    _grid(kb, buttons)
    if ctx.kind:
        kb.row(_btn("‹ Назад", LCb(c=c)))
    return kb.as_markup()


def announce_kb(order: Order) -> InlineKeyboardMarkup:
    """Клавиатура уведомления администратору: сменить статус, не заходя в меню."""
    oid = order.id

    def act(text: str, action: str) -> InlineKeyboardButton:
        return _btn(text, OrderCb(action=action, order_id=oid))

    part = _btn("◑ Частично", PartCb(id=oid)) if order.can_split else None
    kb = InlineKeyboardBuilder()
    if order.status is OrderStatus.new:
        _grid(kb, [act("◐ Заказано", "ordered"), act(f"{DONE} Приехал", "arrived"),
                   part, act("Отменить", "cancel"), _ozon(order.url)])
    elif order.status is OrderStatus.in_progress:
        _grid(kb, [act(f"{DONE} Приехал", "arrived"), part, act("Отменить", "cancel"), _ozon(order.url)])
    else:
        kb.row(_ozon(order.url))
    return kb.as_markup()


def part_qty_kb(order: Order, ctx: Ctx) -> InlineKeyboardMarkup:
    """Сколько штук приехало: от 1 до «всё, кроме одной». Много штук — ввести числом."""
    c, oid, top = ctx.pack(), order.id, order.left_qty - 1
    if top <= 12:
        values = list(range(1, top + 1))
    else:
        values = sorted({v for v in (1, 2, 3, 5, 10, 20, 50, order.left_qty // 2) if 0 < v <= top})
    kb = InlineKeyboardBuilder()
    _grid(kb, [_btn(str(v), PartCb(id=oid, k=v, c=c)) for v in values], cols=4)
    kb.row(_btn("Другое число…", PartCb(id=oid, r="n", c=c)), _btn("‹ Назад", OCb(id=oid, c=c)))
    return kb.as_markup()


def part_rest_kb(order: Order, k: int, ctx: Ctx) -> InlineKeyboardMarkup:
    """Что с остатком после частичного прибытия."""
    c, oid = ctx.pack(), order.id
    kb = InlineKeyboardBuilder()
    kb.row(_btn("◐ Ждём остаток", PartCb(id=oid, k=k, r="w", c=c)))
    kb.row(_btn(f"{OFF} Отменён Ozon", PartCb(id=oid, k=k, r="oz", c=c)),
           _btn(f"{OFF} Не нужен", PartCb(id=oid, k=k, r="no", c=c)))
    kb.row(_btn("‹ Назад", PartCb(id=oid, c=c)))
    return kb.as_markup()


def confirm_delete_kb(order_id: int, ctx: Ctx) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("Да, удалить", OrderCb(action="del_yes", order_id=order_id, c=ctx.pack())),
        _btn("Отмена", OCb(id=order_id, c=ctx.pack())),
    )
    return kb.as_markup()


def cancel_reasons_kb(order_id: int, ctx: Ctx, staff: bool = False) -> InlineKeyboardMarkup:
    c = ctx.pack()
    kb = InlineKeyboardBuilder()
    if staff:
        kb.row(_btn(f"{OFF} Отменён Ozon", CancelCb(order_id=order_id, r=OZON_REASON, c=c)))
    for i, reason in enumerate(CANCEL_REASONS):
        if i != OZON_REASON:
            kb.row(_btn(reason, CancelCb(order_id=order_id, r=i, c=c)))
    kb.row(_btn("Своя причина…", CancelCb(order_id=order_id, r=-1, c=c)),
           _btn("‹ Не отменять", OCb(id=order_id, c=c)))
    return kb.as_markup()


# ------------------------------------------------------------------ списки


def _order_btn(o: Order, c: str = "") -> InlineKeyboardButton:
    """Компактная кнопка «○ №116»: подробности — в тексте списка над кнопками."""
    return _btn(f"{mark(o)} №{o.id}", OCb(id=o.id, c=c))


def list_kb(
    orders: list[Order], ctx: Ctx, counts: dict[str, int], pages: int, period_label: str
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    c = ctx.pack()
    if ctx.st == "draft" and orders:
        kb.row(_btn(f"{DONE} Опубликовать все", OrderCb(action="pub_all")))
    _grid(kb, [_order_btn(o, c) for o in orders], cols=3)
    _pager(kb, ctx.p, pages, lambda p: LCb(c=ctx.to(p=p).pack()))

    # вкладки без счётчиков: число выбранной — в заголовке, так подписи не обрезаются
    tabs = [t for t in TABS if not (ctx.kind == "a" and t == "draft")]
    _grid(kb, [_btn(_sel(TABS[t], t == ctx.st), LCb(c=ctx.to(st=t, p=0).pack())) for t in tabs])

    row = [_btn(f"{period_label[:1].upper()}{period_label[1:]} ▾", NavCb(to="period", c=c))]
    if orders:
        row.append(_btn("Ссылки", OrderCb(action="links", c=c)))
    kb.row(*row)
    return kb.as_markup()


def period_kb(ctx: Ctx) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    buttons = [
        _btn(_sel(name, key == ctx.per), LCb(c=ctx.to(per=key, p=0, d1=0, d2=0).pack()))
        for key, name in PERIODS.items()
    ]
    buttons.append(_btn(_sel("Свои даты…", ctx.per == "r"), NavCb(to="range", c=ctx.pack())))
    _grid(kb, buttons)
    kb.row(_btn("‹ Назад", LCb(c=ctx.pack())))
    return kb.as_markup()


def found_kb(orders: list[Order]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _grid(kb, [_order_btn(o) for o in orders], cols=3)
    return kb.as_markup()


# ------------------------------------------------------------------ сборка (админ)


def ymd(d) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def batch_kb(
    ctx: Ctx, days: list[tuple], counts: dict[tuple[str, str], int], pages: int,
    max_id: int, has_items: bool,
) -> InlineKeyboardMarkup:
    """days — [(дата, заказы)] для текущей страницы."""
    g, s = ctx.st, ctx.per
    kb = InlineKeyboardBuilder()
    # группы не смешиваем: общий заказ и лаборатория открываются разными кнопками меню
    kb.row(
        _btn(_sel(f"Ждут · {counts.get((g, 'new'), 0)}", s == "new"), LCb(c=Ctx("b", g, "new").pack())),
        _btn(_sel(f"Заказано · {counts.get((g, 'ord'), 0)}", s == "ord"), LCb(c=Ctx("b", g, "ord").pack())),
    )
    for day, items in days:
        kb.row(_btn(f"{fmt_day(day)} · {len(items)} ›",
                    BatchCb(a="day", g=g, s=s, m=max_id, d=ymd(day))))
    _pager(kb, ctx.p, pages, lambda p: LCb(c=ctx.to(p=p).pack()))
    buttons = []
    if has_items:
        buttons += [_btn("Ссылки", BatchCb(a="links", g=g, s=s)),
                    _btn("Заявка .xlsx", BatchCb(a="xlsx", g=g, s=s))]
        if s == "new":
            buttons.append(_btn("◐ Всё заказано", BatchCb(a="mark", g=g, s=s, m=max_id)))
        else:
            buttons.append(_btn(f"{DONE} Всё приехало", BatchCb(a="arr", g=g, s=s, m=max_id)))
    buttons.append(_btn("↻ Обновить", LCb(c=ctx.pack())))
    _grid(kb, buttons)
    return kb.as_markup()


def batch_day_kb(
    g: str, s: str, d: int, m: int, items: list[Order], selected: set[int], back: Ctx
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for o in items:
        box = "☑" if o.id in selected else "☐"
        kb.row(_btn(f"{box} {o.name} · {o.left_qty} шт.", BatchCb(a="t", g=g, s=s, m=m, d=d, i=o.id)))
    sign = "◐" if s == "new" else DONE
    if items:
        row = [_btn(f"{sign} Всё · {len(items)}", BatchCb(a="day_all", g=g, s=s, m=m, d=d))]
        if selected:
            row.insert(0, _btn(f"{sign} Выбранное · {len(selected)}", BatchCb(a="day_sel", g=g, s=s, m=m, d=d)))
        kb.row(*row)
        kb.row(
            _btn("Ссылки", BatchCb(a="dlinks", g=g, s=s, m=m, d=d)),
            _btn("Заявка .xlsx", BatchCb(a="dxlsx", g=g, s=s, m=m, d=d)),
        )
    kb.row(_btn("‹ К датам", LCb(c=back.pack())))
    return kb.as_markup()


def batch_confirm_kb(action: str, g: str, s: str, max_id: int, count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn(f"Да, все {count}", BatchCb(a=f"{action}_ok", g=g, s=s, m=max_id)),
        _btn("Отмена", LCb(c=Ctx("b", g, s).pack())),
    )
    return kb.as_markup()


# ------------------------------------------------------------------ починка


def repairs_kb(items: list[Repair], can_handle: bool, open_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("+ Новая заявка", RepCb(a="new")))
    _grid(kb, [_repair_btn(r) for r in items], cols=3)
    if can_handle:
        kb.row(_btn(f"Открытые у всех · {open_count}", RepCb(a="all")))
    return kb.as_markup()


def _repair_btn(r: Repair) -> InlineKeyboardButton:
    """«◐ Р-12»: описание заявки — в тексте над кнопками."""
    return _btn(f"{REPAIR_MARK[r.status]} {r.code}", RepCb(a="open", id=r.id))


def repairs_all_kb(items: list[Repair]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _grid(kb, [_repair_btn(r) for r in items], cols=3)
    kb.row(_btn("‹ Мои заявки", RepCb(a="my")))
    return kb.as_markup()


def repair_draft_kb(r: Repair) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"{DONE} Отправить мастеру", RepCb(a="send", id=r.id)))
    places = [
        _btn(f"{DONE} {name}" if code == r.place else name, RepCb(a="place", id=r.id, v=i))
        for i, (code, name) in enumerate(offices.OFFICES.items())
    ]
    _grid(kb, places)
    kb.row(_btn("Удалить", RepCb(a="drop", id=r.id)))
    return kb.as_markup()


def repair_master_kb(r: Repair) -> InlineKeyboardMarkup | None:
    """Кнопки у мастера (REPAIR_CHAT_ID)."""
    kb = InlineKeyboardBuilder()
    if r.status is RepairStatus.new:
        kb.row(_btn("◐ Взял", RepCb(a="take", id=r.id)), _btn(f"{DONE} Сделано", RepCb(a="done", id=r.id)))
        kb.row(_btn("Отклонить", RepCb(a="rej", id=r.id)))
    elif r.status is RepairStatus.in_work:
        kb.row(_btn(f"{DONE} Сделано", RepCb(a="done", id=r.id)), _btn("Отклонить", RepCb(a="rej", id=r.id)))
    else:
        return None
    return kb.as_markup()


def repair_reject_kb(r: Repair) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, reason in enumerate(REJECT_REASONS):
        kb.row(_btn(reason, RepCb(a="rej_ok", id=r.id, v=i)))
    kb.row(_btn("‹ Не отклонять", RepCb(a="open", id=r.id)))
    return kb.as_markup()


def repair_card_kb(r: Repair, viewer: User, can_handle: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    mine = r.user_id == viewer.id
    if mine and r.status is RepairStatus.done and not r.rating:
        kb.row(*[_btn(f"{v}★", RepCb(a="rate", id=r.id, v=v)) for v in range(1, 6)])
    if can_handle:
        master = repair_master_kb(r)
        if master:
            for row in master.inline_keyboard:
                kb.row(*row)
    last = [_btn("‹ Заявки", RepCb(a="my"))]
    if mine and r.status is RepairStatus.done:
        last.append(_btn("↺ Не решено", RepCb(a="back", id=r.id)))
    elif mine and r.is_open:
        last.append(_btn("Отозвать", RepCb(a="cancel", id=r.id)))
    kb.row(*last)
    return kb.as_markup()


def rate_kb(r: Repair) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(*[_btn(f"{v}★", RepCb(a="rate", id=r.id, v=v)) for v in range(1, 6)])
    kb.row(_btn("↺ Не решено — вернуть", RepCb(a="back", id=r.id)))
    return kb.as_markup()


def skip_comment_kb(r: Repair) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("Без отзыва", RepCb(a="skip", id=r.id)))
    return kb.as_markup()


# ------------------------------------------------------------------ прочее


def more_kb(is_staff: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    buttons = [
        _btn("Черновики", LCb(c=MY_DRAFTS.pack())), _btn("Справка", NavCb(to="help")),
        _btn("Сменить имя", NavCb(to="name")), _btn("Место", NavCb(to="office")),
    ]
    if is_staff:
        buttons += [_btn("В пути", LCb(c=MY_ACTIVE.pack())), _btn("Команды", NavCb(to="admin"))]
    else:
        buttons.append(_btn("Написать админу", NavCb(to="feedback")))
    _grid(kb, buttons)
    return kb.as_markup()


def batch_digest_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(BTN_GROUP[offices.GROUP_MAIN], LCb(c=BATCH_MAIN.pack())),
           _btn(BTN_GROUP[offices.GROUP_LAB], LCb(c=BATCH_LAB.pack())))
    return kb.as_markup()


def drafts_digest_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("Открыть черновики", LCb(c=MY_DRAFTS.pack())))
    return kb.as_markup()


def repairs_digest_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("Открытые заявки", RepCb(a="all")))
    return kb.as_markup()
