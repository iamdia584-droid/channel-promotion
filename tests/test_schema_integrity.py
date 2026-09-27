"""Schema-level guards against whole classes of bug, not individual instances."""

from __future__ import annotations

import enum
import typing

import pytest
from sqlalchemy import Float, String, inspect
from sqlalchemy.exc import StatementError

from app.db.base import StrEnumType
from app.models import Base
from app.models.enums import EntryDirection

MODEL_CLASSES = [m.class_ for m in Base.registry.mappers]


def _strenum_annotations(cls) -> dict[str, type]:
    """Field name -> StrEnum class, for every StrEnum-annotated mapped attribute."""
    try:
        hints = typing.get_type_hints(cls, include_extras=False)
    except Exception:  # pragma: no cover - unresolvable forward ref
        return {}
    out = {}
    for name, hint in hints.items():
        args = typing.get_args(hint)
        if not args:
            continue
        inner = args[0]  # Mapped[X] -> X
        candidates = [a for a in ([inner] + list(typing.get_args(inner)))
                      if isinstance(a, type) and issubclass(a, enum.StrEnum)]
        if candidates:
            out[name] = candidates[0]
    return out


def test_every_enum_column_uses_strenumtype():
    """A StrEnum column declared as plain String reads back as ``str``.

    That silently breaks every ``is`` comparison against an enum member — which
    is how a ledger entry's sign gets inverted. Catch it in the schema instead.
    """
    offenders = []
    for cls in MODEL_CLASSES:
        mapper = inspect(cls)
        for field, enum_cls in _strenum_annotations(cls).items():
            attr = mapper.attrs.get(field)
            if attr is None or not hasattr(attr, "columns"):
                continue
            col_type = attr.columns[0].type
            if not isinstance(col_type, StrEnumType):
                offenders.append(f"{cls.__name__}.{field} ({enum_cls.__name__}) -> {col_type!r}")
            elif col_type.enum_class is not enum_cls:
                offenders.append(
                    f"{cls.__name__}.{field} declares {enum_cls.__name__} "
                    f"but stores {col_type.enum_class.__name__}"
                )
    assert offenders == [], "enum columns not using StrEnumType:\n  " + "\n  ".join(offenders)


def test_enum_columns_round_trip_as_enums(db, make_advertiser):
    """Read-back identity, end to end through SQLite."""
    from app.models.enums import AccountKind, UserStatus
    from app.services.ledger import LedgerService

    adv = make_advertiser()
    account = LedgerService(db).get_or_create_account(AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id)
    account_id = account.id
    db.commit()
    db.expire_all()  # force a real reload from the database

    from app.models.money import LedgerAccount

    reloaded = db.get(LedgerAccount, account_id)
    assert reloaded.normal_side is EntryDirection.CREDIT
    assert reloaded.kind is AccountKind.ADVERTISER_AVAILABLE
    assert db.get(type(adv), adv.id).status is UserStatus.ACTIVE


def test_invalid_enum_value_is_rejected_on_write(db, make_advertiser):
    """StrEnumType validates, so an unknown status cannot reach the database."""
    from app.models.identity import Advertiser

    adv = make_advertiser()
    adv.status = "not-a-real-status"
    # SQLAlchemy wraps the type's ValueError as a StatementError on flush.
    with pytest.raises(StatementError, match="not a valid UserStatus"):
        db.flush()
    db.rollback()


def test_no_float_columns():
    offenders = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, Float)
    ]
    assert offenders == []


def test_telegram_ids_are_bigint():
    """Telegram IDs exceed int32; a 32-bit column would corrupt identity (spec §24)."""
    from sqlalchemy import BigInteger

    expected = {
        ("users", "telegram_user_id"),
        ("telegram_chats", "telegram_chat_id"),
        ("publisher_channels", "telegram_chat_id"),
        ("ad_deliveries", "telegram_chat_id"),
        ("impressions", "telegram_chat_id"),
        ("impressions", "telegram_user_id"),
        ("clicks", "telegram_user_id"),
        ("staff_users", "telegram_user_id"),
    }
    for table_name, col_name in expected:
        col = Base.metadata.tables[table_name].columns[col_name]
        assert isinstance(col.type, BigInteger), f"{table_name}.{col_name} is {col.type!r}"


def test_money_columns_have_six_decimal_places():
    """Sub-paisa precision is required to accrue one impression at a time."""
    from app.db.base import MoneyType

    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, MoneyType):
                assert col.type.scale == 6, f"{table.name}.{col.name}"
                assert col.type.asdecimal is True


@pytest.mark.parametrize(
    "table,constraint",
    [
        ("impressions", "uq_impressions_dedupe_key"),
        ("ledger_transactions", "uq_ledger_transactions_idempotency_key"),
        ("deposits", "uq_deposits_provider_transaction"),
        ("withdrawals", "uq_withdrawals_idempotency_key"),
        ("clicks", "uq_clicks_dedupe_key"),
        ("publisher_channels", "uq_publisher_channels_telegram_chat_id"),
        ("publisher_earnings", "uq_publisher_earnings_delivery_batch"),
    ],
)
def test_critical_uniqueness_is_enforced_by_the_database(table, constraint):
    """These constraints are what make replay and double-billing impossible.

    An application-level check can lose a race; a UNIQUE index cannot.
    """
    names = {c.name for c in Base.metadata.tables[table].constraints}
    names |= {i.name for i in Base.metadata.tables[table].indexes}
    assert constraint in names, f"{table} is missing {constraint}; have {sorted(names)}"


def test_string_columns_all_have_a_length():
    """An unbounded VARCHAR is a denial-of-service vector on a public webhook."""
    offenders = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, String)
        and type(c.type) is String
        and c.type.length is None
    ]
    assert offenders == []
