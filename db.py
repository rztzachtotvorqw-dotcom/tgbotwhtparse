from __future__ import annotations

import datetime as dt
import enum
import os
from urllib.parse import urlparse

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    inspect,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import config
from utils import now

# С версии 1.0 отзывы, голосования и парсер убраны. Их таблицы (reviews, polls,
# votes) и колонки (orders.seller, orders.parse_error) остаются в базе нетронутыми:
# данные не теряются, бот их просто больше не читает.


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    worker = "worker"
    admin = "admin"
    developer = "developer"


class OrderStatus(str, enum.Enum):
    draft = "draft"  # черновик: виден только автору, здесь можно править всё
    new = "new"  # опубликован, ждёт оформления на Ozon
    in_progress = "in_progress"  # оформлен на Ozon, едет
    arrived = "arrived"
    done = "done"  # legacy, остаётся ради старых записей
    cancelled = "cancelled"
    deleted = "deleted"


class RepairStatus(str, enum.Enum):
    draft = "draft"  # заявитель ещё не отправил
    new = "new"  # отправлена мастеру
    in_work = "in_work"  # мастер взял
    done = "done"
    rejected = "rejected"  # мастер отклонил
    cancelled = "cancelled"  # заявитель отозвал


REPAIR_OPEN = (RepairStatus.new, RepairStatus.in_work)

# статусы, по которым заказ ещё ждут
PENDING = (OrderStatus.new, OrderStatus.in_progress)
# завершённые
FINISHED = (OrderStatus.arrived, OrderStatus.done, OrderStatus.cancelled)
# статусы, которые не показываем в общих списках
HIDDEN = (OrderStatus.draft, OrderStatus.deleted)

ROLE_LABELS = {
    Role.worker: "сотрудник",
    Role.admin: "администратор",
    Role.developer: "разработчик",
}


def _enum(col: type[enum.Enum]):
    # native_enum=False -> хранится как VARCHAR, дружелюбно к SQLite и миграциям
    return Enum(col, native_enum=False, length=20, values_callable=lambda e: [i.value for i in e])


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(128))  # имя, которое бот спросил один раз
    office: Mapped[str | None] = mapped_column(String(32))  # код рабочего места из offices.py
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.worker, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    @property
    def display(self) -> str:
        return self.full_name or (f"@{self.username}" if self.username else f"id{self.id}")

    @property
    def is_staff(self) -> bool:
        return self.role in (Role.admin, Role.developer)

    @property
    def is_registered(self) -> bool:
        return bool(self.full_name and self.office)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(32), index=True)  # id из хвоста ссылки
    title: Mapped[str] = mapped_column(String(512), default="Без названия")
    qty: Mapped[int | None] = mapped_column(Integer, default=1)
    price: Mapped[float | None] = mapped_column(Float)
    place: Mapped[str | None] = mapped_column(String(64))  # код офиса или свой текст
    deadline: Mapped[dt.date | None] = mapped_column(Date)  # желаемый срок доставки
    comment: Mapped[str | None] = mapped_column(Text)  # цвет, размер, «срочно» и т.п.
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.draft)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    ordered_at: Mapped[dt.datetime | None] = mapped_column(DateTime)  # оформлен на Ozon
    arrived_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(512))
    # частичное прибытие: сколько штук уже приехало (None — ничего / не отмечали частями).
    # Если остаток отменили — статус «приехал», а в cancel_reason — что стало с остатком.
    qty_arrived: Mapped[int | None] = mapped_column(Integer)
    # где висит уведомление у админа — чтобы обновлять его при смене статуса
    announce_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    announce_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(lazy="joined")
    # уведомления о заказе у каждого администратора — чтобы обновлять их все
    notices: Mapped[list["Notice"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def name(self) -> str:
        """Заказ называется по заказчику: «Петров Иван №45»."""
        return f"{self.user.display} №{self.id}"

    @property
    def quantity(self) -> int:
        return self.qty or 1

    @property
    def arrived_qty(self) -> int:
        return min(self.qty_arrived or 0, self.quantity)

    @property
    def left_qty(self) -> int:
        """Сколько штук ещё не приехало."""
        return self.quantity - self.arrived_qty

    @property
    def is_partial(self) -> bool:
        """Приехала только часть: остаток ещё едет или был отменён."""
        return 0 < self.arrived_qty < self.quantity

    @property
    def can_split(self) -> bool:
        """«Приехала часть» имеет смысл, пока в пути больше одной штуки."""
        return self.status in PENDING and self.left_qty > 1

    @property
    def total(self) -> float | None:
        """Сумма по позиции с учётом количества."""
        return None if self.price is None else self.price * self.quantity

    @property
    def is_draft(self) -> bool:
        return self.status is OrderStatus.draft

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING

    @property
    def when(self) -> dt.datetime:
        """Дата, по которой заказ сортируется и фильтруется."""
        return self.published_at or self.created_at


class Notice(Base):
    """Сообщение-уведомление о заказе у одного из администраторов."""

    __tablename__ = "order_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)


class Repair(Base):
    """Заявка на помощь."""

    __tablename__ = "repairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    place: Mapped[str | None] = mapped_column(String(64))
    photo_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[RepairStatus] = mapped_column(_enum(RepairStatus), default=RepairStatus.draft)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    taken_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    taken_by: Mapped[int | None] = mapped_column(BigInteger)  # кто взял / выполнил
    reject_reason: Mapped[str | None] = mapped_column(String(256))
    rating: Mapped[int | None] = mapped_column(Integer)  # 1..5 от заявителя
    rating_comment: Mapped[str | None] = mapped_column(Text)
    returned: Mapped[int | None] = mapped_column(Integer)  # сколько раз «не решено»
    worker_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    worker_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    report_msg_id: Mapped[int | None] = mapped_column(BigInteger)

    user: Mapped[User] = relationship(lazy="joined")

    @property
    def code(self) -> str:
        return f"Р-{self.id}"

    @property
    def is_open(self) -> bool:
        return self.status in REPAIR_OPEN


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer)  # если сообщение по конкретному заказу
    text: Mapped[str] = mapped_column(Text)
    is_answered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


engine = create_async_engine(config.db_url, echo=False, pool_pre_ping=True)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    # для sqlite создаём директорию под файл
    if config.db_url.startswith("sqlite"):
        path = urlparse(config.db_url.replace("sqlite+aiosqlite:///", "file:///")).path
        directory = os.path.dirname(path.lstrip("/"))
        if directory:
            os.makedirs(directory, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:
    """Мини-миграция вместо Alembic: доливает новые nullable-колонки в живую базу."""
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing or not column.nullable:
                continue
            ddl = column.type.compile(conn.dialect)
            conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}")


async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    user = await session.get(User, tg_user.id)
    if user is None:
        user = User(id=tg_user.id, username=tg_user.username)
        session.add(user)
        await session.flush()
    elif user.username != tg_user.username:
        user.username = tg_user.username

    # разработчики поднимаются автоматически из .env
    if user.id in config.developer_ids and user.role is not Role.developer:
        user.role = Role.developer
        user.is_approved = True
    return user
