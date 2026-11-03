from __future__ import annotations

"""Точки доставки внутри организации.

Ключ хранится в базе (users.office, orders.place), значение показывается людям.
Место доставки заказа — либо один из этих кодов, либо произвольный текст,
который человек ввёл руками. Поэтому label() возвращает сам текст, если ключа нет.

Лаборатория закупается ОТДЕЛЬНЫМ заказом: её позиции не смешиваются с общей
корзиной ни в «Сборке», ни в заявке на оплату.
"""

OFFICES: dict[str, str] = {
    "floor2": "Т4",
    "cmnt": "ЦМНТ",
    "lab": "Лаборатория",
    "crpp": "ЦРПП",
}

CODES = tuple(OFFICES)
LAB = "lab"

# группы сборки: в какой заказ на Ozon попадёт позиция
GROUP_MAIN = "main"
GROUP_LAB = "lab"
GROUPS: dict[str, str] = {
    GROUP_MAIN: "Общий заказ",
    GROUP_LAB: "Лаборатория",
}


def label(code: str | None) -> str:
    if not code:
        return "не указано"
    return OFFICES.get(code, code)


def is_code(value: str | None) -> bool:
    return value in OFFICES


def is_lab(place: str | None) -> bool:
    """Код lab или своё место, где написано «лаборатория»."""
    if not place:
        return False
    return place == LAB or "лаборат" in place.casefold()


def group_of(place: str | None) -> str:
    return GROUP_LAB if is_lab(place) else GROUP_MAIN


def group_places(group: str) -> str:
    """Подпись группы для заголовка: «Т4, ЦМНТ, ЦРПП»."""
    if group == GROUP_LAB:
        return OFFICES[LAB]
    return ", ".join(name for code, name in OFFICES.items() if code != LAB)
