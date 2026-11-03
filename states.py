from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Reg(StatesGroup):
    name = State()  # фамилию и имя спрашиваем один раз
    office = State()  # и рабочее место следом


class Draft(StatesGroup):
    """Ввод текстом при правке черновика. order_id и prompt_id лежат в data."""

    price = State()
    qty = State()
    deadline = State()
    place = State()
    comment = State()
    link = State()


class OrderSG(StatesGroup):
    cancel_reason = State()
    part_qty = State()  # сколько штук приехало — своим числом


class FilterSG(StatesGroup):
    range = State()  # свои даты для фильтра списка


class FeedbackSG(StatesGroup):
    text = State()


class RepairSG(StatesGroup):
    text = State()  # описание поломки (можно с фото)
    comment = State()  # отзыв к оценке
