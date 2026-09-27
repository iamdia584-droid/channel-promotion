"""Alembic environment. Imports the models so autogenerate sees every table."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app.core.config import settings
from app.db.session import build_engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", settings.database_url)


def render_item(type_, obj, autogen_context):
    """Render the project's custom column types as plain SQLAlchemy DDL.

    A migration describes a schema; it should not depend on application classes
    that may be renamed or change signature later. ``MoneyType`` is really
    ``NUMERIC(24,6)``, ``StrEnumType`` is a ``VARCHAR(n)`` (the enum conversion is
    Python-side only), and ``UTCDateTime`` is ``TIMESTAMP WITH TIME ZONE``, so the
    migration says exactly that.
    """
    if type_ != "type":
        return False

    from app.db.base import GUID, JSONBType, MoneyType, StrEnumType, UTCDateTime

    if isinstance(obj, MoneyType):
        return "sa.Numeric(precision=24, scale=6)"
    if isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.length})"
    if isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if isinstance(obj, JSONBType):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "postgresql.JSONB(astext_type=sa.Text())"
    if isinstance(obj, GUID):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "postgresql.UUID(as_uuid=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = build_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
            # SQLite cannot ALTER most things; batch mode rebuilds the table.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
