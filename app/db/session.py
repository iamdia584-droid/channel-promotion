"""Engine, session factory and transaction helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

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

    return create_engine(url, connect_args=connect_args, **kwargs)


engine: Engine = build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """SQLite ignores foreign keys unless asked. Tests must enforce them too."""
    if engine.dialect.name != "sqlite":
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


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
