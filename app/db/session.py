"""Engine, session factory and transaction helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def build_engine(url: str | None = None) -> Engine:
    """Create an engine, passing only options the chosen pool actually accepts.

    ``pool_size`` and ``max_overflow`` belong to QueuePool. SQLite uses
    SingletonThreadPool and raises TypeError if they are supplied, so a single
    unconditional call would make the app unimportable against SQLite.
    """
    url = url or settings.database_url
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    connect_args: dict = {}

    if url.startswith("postgresql"):
        # A runaway query must not hold a row lock on a wallet indefinitely.
        connect_args["options"] = f"-c statement_timeout={settings.db_statement_timeout_ms}"
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,
        )
    elif url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    built = create_engine(url, connect_args=connect_args, **kwargs)

    if built.dialect.name == "sqlite":
        # SQLite ignores foreign keys unless asked per connection. Registered on
        # *this* engine rather than on the Engine class: a class-level listener
        # fires for every engine in the process, so it would send PRAGMA to a
        # PostgreSQL connection and fail there.
        @event.listens_for(built, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return built


engine: Engine = build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def healthcheck() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover
        return False
