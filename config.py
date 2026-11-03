from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> frozenset[int]:
    out: set[int] = set()
    for chunk in raw.replace(",", " ").split():
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return frozenset(out)


def _int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _hour(raw: str | None, default: int) -> int | None:
    """DIGEST_HOUR: час ежедневной сводки. off / -1 — выключить."""
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    if raw in {"off", "no", "-1", "false"}:
        return None
    value = _int(raw)
    return value if value is not None and 0 <= value <= 23 else default


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    tg_proxy: str | None
    developer_ids: frozenset[int]
    admin_chat_id: int  # единственный получатель уведомлений о заказах
    work_chat_id: int | None  # участники этого чата получают доступ автоматически
    access_cache_ttl: int
    big_order_threshold: float
    export_approved_by: str | None  # кто согласует заявку на оплату в /export
    default_deadline_days: int
    digest_hour: int | None  # утренняя сводка админу и напоминания о черновиках
    repair_chat_id: int  # кто получает заявки на помощь (мастер)
    report_chat_id: int  # куда уходят отчёты по заявкам и оценки
    db_url: str
    tz: str

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN не задан в .env")
        devs = _ids(os.getenv("DEVELOPER_IDS", ""))
        if not devs:
            raise RuntimeError("DEVELOPER_IDS не задан в .env — без него некому раздавать админку")

        # ADMIN_CHAT_ID можно не задавать: тогда уведомления идут первому разработчику
        admin_chat = _int(os.getenv("ADMIN_CHAT_ID")) or min(devs)

        return cls(
            bot_token=token,
            tg_proxy=(os.getenv("TG_PROXY") or "").strip() or None,
            developer_ids=devs,
            admin_chat_id=admin_chat,
            work_chat_id=_int(os.getenv("WORK_CHAT_ID")),
            access_cache_ttl=int(os.getenv("ACCESS_CACHE_TTL", "600")),
            big_order_threshold=float(os.getenv("BIG_ORDER_THRESHOLD", "50000")),
            # пусто -> в заявку попадёт имя того, кто делает выгрузку
            export_approved_by=(os.getenv("APPROVED_BY") or "").strip() or None,
            default_deadline_days=int(os.getenv("DEFAULT_DEADLINE_DAYS", "7")),
            digest_hour=_hour(os.getenv("DIGEST_HOUR"), 10),
            # не заданы — заявки и отчёты идут администратору
            repair_chat_id=_int(os.getenv("REPAIR_CHAT_ID")) or admin_chat,
            report_chat_id=_int(os.getenv("REPORT_CHAT_ID")) or admin_chat,
            db_url=os.getenv("DB_URL", "sqlite+aiosqlite:///data/bot.db"),
            tz=os.getenv("TZ", "Europe/Moscow"),
        )


config = Config.load()
