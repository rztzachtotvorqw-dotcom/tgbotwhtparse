from __future__ import annotations

"""Заявка на оплату для бухгалтерии (.xlsx).

Первый лист — заявка в том виде, в котором она уходит письмом в бухгалтерию,
плюс состав заказа. Второй лист — рабочий: ссылки, заказчики и комментарии,
чтобы по нему же оформлять корзину на Ozon.
"""

import io
import re

from aiogram.types import BufferedInputFile, Message
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side

import offices
from db import Order
from utils import esc, fmt_date, fmt_price, today
from views import DOT

# Юрлицо Ozon — получатель платежа в заявке на оплату
EXPORT_RECIPIENT = '(OZON) ООО "Интернет Решения"'


def _rub(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " руб."


def _safe_filename(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", s).strip("._")
    return s or "mesto"


def build_xlsx(place: str | None, items: list[Order], total: float, approved_by: str) -> bytes:
    label = offices.label(place)

    wb = Workbook()
    ws = wb.active
    ws.title = "Заявка"

    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    for col, w in {"A": 22, "B": 46, "C": 9, "D": 14, "E": 14}.items():
        ws.column_dimensions[col].width = w

    def put(sheet, row: int, col: int, value, *, b: bool = False, num: bool = False):
        c = sheet.cell(row=row, column=col, value=value)
        c.border = border
        c.alignment = wrap
        if b:
            c.font = bold
        if num:
            c.number_format = "#,##0"
        return c

    # --- шапка заявки: как в письме финансового отдела
    pairs = [
        ("Компания", label),
        ("Проект / Договор", f"обеспечение офиса {label}"),
        ("Назначение", "Предоплата"),
        ("Получатель", EXPORT_RECIPIENT),
        ("Сумма и валюта", _rub(total)),
        ("Условия оплаты", "счёт оферта"),
        ("Документы", "счёт во вложении"),
        ("Согласовано с", approved_by),
    ]
    put(ws, 1, 1, "Поле", b=True)
    put(ws, 1, 2, "Значение", b=True)
    r = 2
    for name, value in pairs:
        put(ws, r, 1, name, b=True)
        put(ws, r, 2, value)
        r += 1

    # --- состав заказа
    r += 1
    ws.cell(row=r, column=1, value=f"Состав ({len(items)} поз.)").font = bold
    r += 1
    for col, title in enumerate(("№", "Название", "Кол-во", "Цена, руб.", "Сумма, руб."), 1):
        put(ws, r, col, title, b=True)
    r += 1
    for o in items:
        put(ws, r, 1, o.id)
        put(ws, r, 2, f"{o.name}\n{o.comment}" if o.comment else o.name)
        put(ws, r, 3, o.quantity, num=True)
        put(ws, r, 4, o.price if o.price is not None else "не указана", num=o.price is not None)
        put(ws, r, 5, o.total if o.total is not None else "—", num=o.total is not None)
        r += 1
    put(ws, r, 2, "Итого (позиции без цены не учтены)", b=True)
    put(ws, r, 5, total, b=True, num=True)

    # --- рабочий лист для оформления корзины
    ws2 = wb.create_sheet("Ссылки")
    for col, w in {"A": 8, "B": 46, "C": 8, "D": 20, "E": 12, "F": 30, "G": 60}.items():
        ws2.column_dimensions[col].width = w
    for col, title in enumerate(("№", "Название", "Кол-во", "Заказчик", "Срок", "Комментарий", "Ссылка"), 1):
        put(ws2, 1, col, title, b=True)
    for i, o in enumerate(items, 2):
        put(ws2, i, 1, o.id)
        put(ws2, i, 2, o.name)
        put(ws2, i, 3, o.quantity, num=True)
        put(ws2, i, 4, o.user.display if o.user else "")
        put(ws2, i, 5, fmt_date(o.deadline))
        put(ws2, i, 6, o.comment or "")
        link = put(ws2, i, 7, o.url)
        link.hyperlink = o.url
        link.font = Font(color="0563C1", underline="single")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def send_export(message: Message, orders: list[Order], approved_by: str) -> int:
    """По одному файлу на место доставки. Лаборатория всегда отдельным файлом."""
    groups: dict[str | None, list[Order]] = {}
    for o in orders:
        groups.setdefault(o.place, []).append(o)

    stamp = today().strftime("%d.%m.%Y")
    # лабораторию — последней, чтобы её было легко найти
    for place, items in sorted(groups.items(), key=lambda kv: (offices.is_lab(kv[0]), offices.label(kv[0]))):
        total = sum(o.total for o in items if o.total is not None)
        no_price = [o for o in items if o.total is None]
        data = build_xlsx(place, items, total, approved_by)
        filename = f"Заявка_{_safe_filename(offices.label(place))}_{stamp}.xlsx"

        caption = [
            f"<b>Заявка на оплату — {esc(offices.label(place))}</b>"
            + (" (отдельный заказ)" if offices.is_lab(place) else ""),
            f"{DOT} Позиций — {len(items)}, сумма — {fmt_price(total)}",
        ]
        if no_price:
            caption.append(f"{DOT} Без цены: {', '.join(f'№{o.id}' for o in no_price)} — в сумму не вошли.")
        await message.answer_document(
            BufferedInputFile(data, filename=filename), caption="\n".join(caption)
        )
    return len(groups)
