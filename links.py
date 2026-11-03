from __future__ import annotations

"""Разбор сообщения со ссылками на Ozon. Без сети и без парсинга страниц.

Из сообщения бот берёт только ссылку, количество («3 шт») и текст —
текст становится комментарием к заказу. Название заказа — «Фамилия Имя №N».
"""

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

# со схемой и без: «https://ozon.ru/t/abc», «www.ozon.ru/product/…», «ozon.ru/t/abc»
URL_RE = re.compile(
    r"(?<![\w@./-])(?:https?://)?(?:[\w-]+\.)*ozon\.(?:ru|by|kz|com)(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)
# «3 шт», «3шт.», «10 штук», «кол-во 2», «количество: 4»
QTY_RE = re.compile(
    r"(?:(?<![\d.,])(\d{1,3})\s*(?:шт\.?|штук[иа]?|pcs)(?![а-яё\w]))"
    r"|(?:(?:кол-?во|количество)\s*[:\-]?\s*(\d{1,3})(?!\d))",
    re.IGNORECASE,
)
# подписи, которые приложение Ozon добавляет при «Поделиться»
SHARE_NOISE_RE = re.compile(
    r"^\s*(?:посмотр\w*|смотр\w*|глянь\w*|зацени\w*)[^:\n]{0,40}(?:ozon|озон)\w*\s*[:!.\-—]*\s*",
    re.IGNORECASE,
)
PRODUCT_RE = re.compile(r"/product/([^/?#]+)", re.IGNORECASE)
ID_TAIL_RE = re.compile(r"-(\d{6,})$")
TRAIL_PUNCT = ").,;:!?»\"'"

MAX_QTY = 999


@dataclass(slots=True)
class LinkItem:
    url: str
    comment: str | None
    qty: int


def normalize(url: str) -> str:
    """Схема, нижний регистр хоста и без реф-мусора в query у полных ссылок."""
    url = url.rstrip(TRAIL_PUNCT)
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    p = urlparse(url)
    host = p.netloc.lower()
    path = p.path or "/"
    if "/product/" in path:
        return urlunparse(("https", host, path, "", "", ""))
    # короткие /t/xxx и прочее оставляем как есть, только без фрагмента
    return urlunparse(("https", host, path, "", p.query, ""))


def product_id_from_url(url: str) -> str | None:
    """id товара из хвоста полной ссылки — чтобы замечать повторы."""
    m = PRODUCT_RE.search(url or "")
    if not m:
        return None
    slug = m.group(1)
    if slug.isdigit():
        return slug
    tail = ID_TAIL_RE.search(slug)
    return tail.group(1) if tail else None


def find_urls(text: str | None) -> list[str]:
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        raw = m.group(0)
        # «ozon.ru» без пути — это не товар
        if "/" not in raw.split("://", 1)[-1].rstrip("/"):
            continue
        url = normalize(raw)
        if url not in out:
            out.append(url)
    return out


def has_url(text: str | None) -> bool:
    return bool(find_urls(text))


def _clean_text(text: str) -> str:
    text = URL_RE.sub(" ", text)
    text = SHARE_NOISE_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip(" -—–:,;") for line in text.splitlines())
    return re.sub(r"\n{2,}", "\n", text).strip()


def _take_qty(text: str) -> tuple[str, int | None]:
    m = QTY_RE.search(text)
    if not m:
        return text, None
    value = int(m.group(1) or m.group(2))
    if not 1 <= value <= MAX_QTY:
        return text, None
    return (text[: m.start()] + " " + text[m.end():]).strip(), value


def _item(url: str, text: str) -> LinkItem:
    text, qty = _take_qty(_clean_text(text))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return LinkItem(url, text[:1000] if len(text) >= 2 else None, qty or 1)


def parse_message(text: str | None) -> list[LinkItem]:
    """Одна ссылка — весь текст относится к ней. Несколько — текст строки к её ссылке."""
    text = text or ""
    urls = find_urls(text)
    if not urls:
        return []
    if len(urls) == 1:
        return [_item(urls[0], text)]

    items: list[LinkItem] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line_urls = [u for u in find_urls(line) if u not in seen]
        for u in line_urls:
            seen.add(u)
            items.append(_item(u, line if len(line_urls) == 1 else ""))
    for u in urls:  # ссылки, разорванные переносом строки
        if u not in seen:
            items.append(_item(u, ""))
    return items
