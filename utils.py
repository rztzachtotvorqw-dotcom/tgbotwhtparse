from __future__ import annotations

import datetime as dt
import html
import re
from zoneinfo import ZoneInfo

from config import config

TZ = ZoneInfo(config.tz)

MONTHS_GEN = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

_USER_DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$")
_DATE_TOKEN_RE = re.compile(r"(?<!\d)\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?(?!\d)")


def today() -> dt.date:
    return dt.datetime.now(TZ).date()


def now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None)


def _split_date(text: str) -> tuple[int, int, int | None] | None:
    m = _USER_DATE_RE.match((text or "").strip())
    if not m:
        return None
    year = None
    if m.group(3):
        year = int(m.group(3))
        if year < 100:
            year += 2000
    return int(m.group(1)), int(m.group(2)), year


def parse_user_date(text: str) -> dt.date | None:
    """Срок доставки: 25.12.2026 / 25.12 / 25-12. Без года — ближайшая будущая дата."""
    parts = _split_date(text)
    if not parts:
        return None
    day, month, year = parts
    try:
        d = dt.date(year or today().year, month, day)
    except ValueError:
        return None
    if year is None and d < today():
        try:
            d = dt.date(d.year + 1, month, day)
        except ValueError:
            return None
    return d


def parse_past_date(text: str) -> dt.date | None:
    """Дата для фильтра: без года — ближайшая прошедшая (или сегодняшняя)."""
    parts = _split_date(text)
    if not parts:
        return None
    day, month, year = parts
    try:
        d = dt.date(year or today().year, month, day)
    except ValueError:
        return None
    if year is None and d > today():
        try:
            d = dt.date(d.year - 1, month, day)
        except ValueError:
            return None
    return d


def parse_range(text: str) -> tuple[dt.date, dt.date] | None:
    """«01.10-15.10», «с 01.10 по 15.10», «01.10.2026 — 15.10.2026» или одна дата."""
    chunks = _DATE_TOKEN_RE.findall(text or "")
    if len(chunks) == 1:
        d = parse_past_date(chunks[0])
        return (d, d) if d else None
    if len(chunks) != 2:
        return None
    a, b = parse_past_date(chunks[0]), parse_past_date(chunks[1])
    if not a or not b:
        return None
    return (a, b) if a <= b else (b, a)


def greeting() -> str:
    """Приветствие по местному времени из TZ в .env."""
    hour = dt.datetime.now(TZ).hour
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    if 18 <= hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def fmt_date(d: dt.date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "не указан"


def fmt_short(d: dt.date | dt.datetime | None) -> str:
    """03.11 для текущего года, 03.11.25 — для прошлых."""
    if not d:
        return "—"
    return d.strftime("%d.%m") if d.year == today().year else d.strftime("%d.%m.%y")


def fmt_dt(d: dt.datetime | None) -> str:
    return d.strftime("%d.%m.%Y в %H:%M") if d else "не указано"


def fmt_day(d: dt.date) -> str:
    """Заголовок дня в списке: «Сегодня», «Вчера», «3 ноября, пн»."""
    t = today()
    if d == t:
        return "Сегодня"
    if d == t - dt.timedelta(days=1):
        return "Вчера"
    year = "" if d.year == t.year else f" {d.year}"
    return f"{d.day} {MONTHS_GEN[d.month]}{year}, {WEEKDAYS[d.weekday()]}"


def fmt_price(p: float | None) -> str:
    if p is None:
        return "не указана"
    return f"{p:,.0f}".replace(",", " ") + " ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def deadline_note(d: dt.date | None) -> str:
    """«через 3 дн.», «сегодня», «просрочен на 2 дн.»."""
    if not d:
        return ""
    delta = (d - today()).days
    if delta > 1:
        return f"через {delta} дн."
    if delta == 1:
        return "завтра"
    if delta == 0:
        return "сегодня"
    return f"просрочен на {-delta} дн."


def parse_price(text: str | None) -> float | None:
    """«12 490 ₽», «12490.50», «1 299₽ 1 500₽» → первое число."""
    if not text:
        return None
    cleaned = text.replace(" ", " ").replace("\xa0", " ")
    m = re.search(r"\d[\d\s]*(?:[.,]\d{1,2})?", cleaned)
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def shorten(s: str, limit: int = 60) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
