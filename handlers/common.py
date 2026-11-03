from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import offices
from config import config
from db import ROLE_LABELS, User
from handlers.orders import start_drafts
from keyboards import OfficeCb, office_kb
from screens import ASK_NAME, ASK_OFFICE, HELP_TEXT
from services import edit, menu_for
from states import Reg
from utils import esc, greeting
from views import DOT

router = Router(name="common")
router.message.filter(F.chat.type == "private")

READY = "Готово. Пришлите ссылку на товар Ozon — заведу черновик."


# раньше всех: /cancel должен работать в любом диалоге
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    text = "Сейчас нечего отменять." if await state.get_state() is None else "Отменил текущее действие."
    await state.clear()
    await message.answer(text, reply_markup=await menu_for(session, user))


async def _ask_office(message: Message, state: FSMContext) -> None:
    await state.set_state(Reg.office)
    await message.answer(ASK_OFFICE, reply_markup=office_kb())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    menu = await menu_for(session, user)
    if not user.full_name:
        await state.set_state(Reg.name)
        await message.answer(
            f"{greeting()}. Я принимаю заказы с Ozon и заявки на помощь.\n\n{ASK_NAME}",
            reply_markup=menu,
        )
        return
    if not user.office:
        await message.answer(f"{greeting()}, <b>{esc(user.full_name)}</b>.", reply_markup=menu)
        await _ask_office(message, state)
        return
    await message.answer(
        f"{greeting()}, <b>{esc(user.full_name)}</b>.\n\n{HELP_TEXT}",
        reply_markup=menu,
        disable_web_page_preview=True,
    )


@router.message(Command("name"))
async def cmd_name(message: Message, state: FSMContext) -> None:
    await state.set_state(Reg.name)
    await message.answer(ASK_NAME)


@router.message(StateFilter(Reg.name), F.text, ~F.text.startswith("/"))
async def set_name(message: Message, state: FSMContext, user: User, session: AsyncSession) -> None:
    name = " ".join((message.text or "").split())
    if not 3 <= len(name) <= 64 or len(name.split()) < 2:
        await message.answer("Нужны фамилия и имя через пробел, например: <i>Петров Иван</i>.")
        return
    user.full_name = name
    await session.commit()
    await message.answer(f"Записал: <b>{esc(name)}</b>. Заказы будут называться "
                         f"«{esc(name)} №…».", reply_markup=await menu_for(session, user))
    if not user.office:
        await _ask_office(message, state)  # отложенная ссылка остаётся в данных состояния
        return
    pending = (await state.get_data()).get("pending_text")
    await state.clear()
    if pending:
        await start_drafts(message, state, session, user, pending)


@router.message(Command("office"))
async def cmd_office(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await message.answer(
        f"{DOT} Сейчас — {esc(offices.label(user.office))}\n\n{ASK_OFFICE}",
        reply_markup=office_kb(),
    )


@router.callback_query(OfficeCb.filter())
async def set_office(
    call: CallbackQuery, callback_data: OfficeCb, state: FSMContext, user: User,
    session: AsyncSession,
) -> None:
    if not offices.is_code(callback_data.code):
        await call.answer("Неизвестное место", show_alert=True)
        return
    first_time = not user.office
    user.office = callback_data.code
    await session.commit()
    pending = (await state.get_data()).get("pending_text")
    await state.clear()

    text = f"{DOT} Рабочее место — <b>{esc(offices.label(user.office))}</b>."
    if offices.is_lab(user.office):
        text += "\nЗаказы лаборатории собираются отдельным заказом."
    await edit(call.message, text)
    await call.answer("Сохранил")
    if pending:
        await start_drafts(call.message, state, session, user, pending)
        return
    if first_time:
        await call.message.answer(f"{READY}\n\nКак пользоваться — /help.",
                                  reply_markup=await menu_for(session, user))


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(HELP_TEXT, reply_markup=await menu_for(session, user),
                         disable_web_page_preview=True)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    await message.answer(
        f"{greeting()}, <b>{esc(user.display)}</b>.\n\n"
        "Меню внизу экрана. Чтобы заказать, пришлите ссылку на товар Ozon.",
        reply_markup=await menu_for(session, user),
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(
        f"Ваш идентификатор — <code>{message.from_user.id}</code>\n"
        f"Идентификатор этого чата — <code>{message.chat.id}</code>\n\n"
        "Первый нужен администратору, чтобы выдать доступ, и для REPAIR_CHAT_ID / "
        "REPORT_CHAT_ID в настройках."
    )


@router.message(Command("whoami"))
async def cmd_whoami(message: Message, user: User) -> None:
    roles = [ROLE_LABELS.get(user.role, user.role.value)]
    if user.id == config.repair_chat_id:
        roles.append("мастер по заявкам на помощь")
    if user.id == config.report_chat_id:
        roles.append("получает отчёты по заявкам на помощь")
    await message.answer(
        f"{esc(user.display)} — {', '.join(roles)}\n"
        f"{DOT} Рабочее место — {esc(offices.label(user.office))}\n"
        f"{DOT} Идентификатор — <code>{user.id}</code>"
    )
