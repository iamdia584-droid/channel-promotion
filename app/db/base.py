"""Declarative base, shared column types and mixins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, MetaData, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from app.core.money import SCALE, D, q

# Explicit naming so Alembic autogenerate produces stable, reviewable migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class JSONBType(TypeDecorator):
    """JSONB on Postgres, JSON on SQLite (so tests need no Postgres)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class UTCDateTime(TypeDecorator):
    """A timestamp that is *always* timezone-aware UTC in Python.

    Postgres hands back aware datetimes for ``TIMESTAMPTZ``; SQLite hands back
    naive ones, and so do server defaults like ``CURRENT_TIMESTAMP``. Mixing the
    two raises ``TypeError`` on subtraction, which means a naive value reaching
    business logic turns a pacing or expiry check into a crash. Normalising in
    the type removes the entire class of bug rather than patching call sites.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # A naive value is assumed UTC: this system never works in local time.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class StrEnumType(TypeDecorator):
    """A ``StrEnum`` column that round-trips as the enum, not as ``str``.

    Declaring ``Mapped[SomeEnum]`` against a plain ``String`` stores fine but
    reads back a bare ``str``, which makes every ``is`` comparison against an
    enum member silently false — and a sign test like
    ``leg.direction is account.normal_side`` then flips the sign of a ledger
    entry. Converting in the type is the only place that cannot be forgotten.
    It also validates on write, so an unknown status can never be persisted.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class, length: int = 32) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        return self.enum_class(str(value)).value  # raises ValueError on a bad value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum_class(value)


class GUID(TypeDecorator):
    """UUID on Postgres, 36-char string elsewhere."""

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class MoneyType(TypeDecorator):
    """Exact fixed-point money, on PostgreSQL and SQLite alike.

    On PostgreSQL this is ``NUMERIC(24, 6)`` and arithmetic is exact.

    SQLite has no real NUMERIC type: it stores DECIMAL columns as IEEE floats, so
    a CHECK constraint like ``gross_amount = net_amount + platform_commission``
    is evaluated on inexact float arithmetic and fails for ordinary commission
    rates. Since those CHECKs are a core money guarantee, testing against float
    storage would be false confidence. So on SQLite we store the value as a
    64-bit integer count of micro-units, which makes both storage and in-database
    arithmetic exact. Python always sees a quantized ``Decimal`` either way.

    64-bit micro-units cap out around 9.2 x 10^12 currency units, far above any
    balance this system will hold.
    """

    impl = Numeric
    cache_ok = True
    #: Micro-units: 10 ** SCALE.
    MICRO = 10**SCALE

    def __init__(self) -> None:
        super().__init__(precision=24, scale=SCALE, asdecimal=True)

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Numeric(24, SCALE, asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        quantized = q(value)
        if dialect.name == "sqlite":
            return int(quantized.scaleb(SCALE).to_integral_value())
        return quantized

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            # int -> Decimal is exact; never route this through float.
            return q(Decimal(int(value)).scaleb(-SCALE))
        return q(D(value))


Money = MoneyType
TelegramId = BigInteger  # spec §24: Telegram IDs are BIGINT, they exceed int32


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict: JSONBType, uuid.UUID: GUID}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} {pk}>"


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=new_uuid)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
