from __future__ import annotations

"""Утренняя сводка.

По будням в DIGEST_HOUR (по умолчанию 10:00, TZ из .env):
• всем администраторам и разработчикам — что ждёт оформления (общий заказ и
  лаборатория отдельно) и что просрочено;
• мастеру (REPAIR_CHAT_ID) — открытые заявки на помощь;
• получателю отчётов (REPORT_CHAT_ID) по понедельникам — сводка по заявкам на помощь за неделю;
• сотрудникам — напоминание о черновиках, которые висят больше суток
  (каждый черновик напоминается один раз).
Если сообщать нечего — бот молчит.
"""

import asyncio
import datetime as dt
import logging
from collections import defaultdict

from aiogram import Bot
from sqlalchemy import select

import offices
from config import config
from db import PENDING, Order, OrderStatus, Session
from handlers.repairs import repair_summary
from keyboards import batch_digest_kb, drafts_digest_kb, repairs_digest_kb
from screens import open_repairs
from services import notify_staff, safe_send
from utils import TZ, now, plural, today
from views import DOT, repair_row, short_list, summary

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60


async def build_admin_digest(session) -> str | None:
    pending = list((await session.scalars(select(Order).where(Order.status.in_(PENDING)))).all())
    if not pending:
        return None
    lines = [f"<b>Сводка на {today().strftime('%d.%m')}</b>"]
    for group, name in offices.GROUPS.items():
        waiting = [o for o in pending if o.status is OrderStatus.new and offices.group_of(o.place) == group]
        if waiting:
            lines.append(f"{DOT} {name}: ждут оформления — {summary(waiting)}")
    ordered = [o for o in pending if o.status is OrderStatus.in_progress]
    if ordered:
        lines.append(f"{DOT} Заказано, едет — {len(ordered)}")
    overdue = sorted((o for o in pending if o.deadline and o.deadline < today()), key=lambda o: o.deadline)
    if overdue:
        lines.append(f"\n<b>Срок вышел: {len(overdue)}</b>")
        lines.append(short_list(overdue, 10))
    return "\n".join(lines)


async def remind_drafts(bot: Bot, session, span_hours: int = 48) -> None:
    """Черновики старше суток, которые ещё не попадали в прошлую сводку: напоминаем один раз.

    span_hours — окно назад: обычно 48 ч, в понедельник 96 ч (выходные сводку не шлём).
    """
    t = now()
    drafts = (await session.scalars(
        select(Order).where(
            Order.status == OrderStatus.draft,
            Order.created_at <= t - dt.timedelta(hours=24),
            Order.created_at > t - dt.timedelta(hours=span_hours),
        )
    )).all()
    per_user: dict[int, list[Order]] = defaultdict(list)
    for o in drafts:
        per_user[o.user_id].append(o)
    for uid, orders in per_user.items():
        await safe_send(
            bot, uid,
            f"<b>Не опубликовано: {plural(len(orders), 'черновик', 'черновика', 'черновиков')}</b>\n"
            f"{short_list(orders)}\n\nПока заказ в черновиках, администраторы его не видят.",
            reply_markup=drafts_digest_kb(),
        )


async def send_repairs_digest(bot: Bot, session, monday: bool) -> None:
    items = await open_repairs(session)
    if items:
        rows = "\n".join(repair_row(r, show_user=True) for r in items[:15])
        await safe_send(bot, config.repair_chat_id,
                        f"<b>Открытые заявки на помощь: {len(items)}</b>\n{rows}",
                        reply_markup=repairs_digest_kb())
    if monday:
        await safe_send(bot, config.report_chat_id, await repair_summary(session, days=7))


async def run_digest(bot: Bot, weekday: int) -> None:
    async with Session() as session:
        text = await build_admin_digest(session)
        if text:
            await notify_staff(bot, session, text, reply_markup=batch_digest_kb())
        await send_repairs_digest(bot, session, monday=weekday == 0)
        await remind_drafts(bot, session, 96 if weekday == 0 else 48)


async def digest_loop(bot: Bot) -> None:
    if config.digest_hour is None:
        log.info("утренняя сводка выключена (DIGEST_HOUR=off)")
        return
    last_sent: dt.date | None = None
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            local = dt.datetime.now(TZ)
            if local.weekday() >= 5 or local.hour != config.digest_hour:
                continue
            if last_sent == local.date():
                continue
            last_sent = local.date()
            await run_digest(bot, local.weekday())
            log.info("утренняя сводка отправлена")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сбой в утренней сводке — продолжаю работать")


__all__ = ["digest_loop", "run_digest"]
