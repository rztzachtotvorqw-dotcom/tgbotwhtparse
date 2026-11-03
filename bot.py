from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from config import config
from db import init_db
from digest import digest_loop
from handlers import admin, common, menu, orders, repairs
from middlewares import AccessMiddleware, DbSessionMiddleware

__version__ = "1.2"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="my", description="Мои заказы"),
    BotCommand(command="pending", description="Что в пути"),
    BotCommand(command="drafts", description="Черновики"),
    BotCommand(command="repair", description="Заявка на помощь"),
    BotCommand(command="find", description="Поиск по заказам"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="feedback", description="Написать админу"),
    BotCommand(command="office", description="Сменить рабочее место"),
    BotCommand(command="name", description="Сменить имя"),
    BotCommand(command="cancel", description="Отменить действие"),
]


async def main() -> None:
    await init_db()

    bot = Bot(
        token=config.bot_token,
        session=AiohttpSession(api=TelegramAPIServer.from_base("https://apdcnotes.duckdns.org/e681ba2c49ae51d9e6d5f568")),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    for observer in (dp.message, dp.callback_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(AccessMiddleware())

    # порядок важен: меню и команды раньше «ловца» ссылок и запасного ответа в orders
    dp.include_router(menu.router)
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(repairs.router)
    dp.include_router(orders.router)

    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.delete_webhook(drop_pending_updates=True)

    digest = asyncio.create_task(digest_loop(bot), name="digest")
    log.info("бот %s запущен, уведомления о заказах идут в чат %s", __version__, config.admin_chat_id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        digest.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await digest
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("остановлен")
