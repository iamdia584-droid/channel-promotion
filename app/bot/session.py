"""Database access for bot handlers.

Handlers are async, the ORM session is sync. Each handler opens a short
transaction, extracts what it needs, and closes it before awaiting Telegram —
never holding a database transaction open across a network call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.core.cache import rate_limit
from app.core.errors import RateLimited, SuspendedAccount
from app.db.session import session_scope
from app.models.identity import User


@contextmanager
def bot_session() -> Iterator[Session]:
    with session_scope() as db:
        yield db


def resolve_user(db: Session, from_user) -> User:
    """Identify by Telegram numeric id and refresh the display fields (spec §2)."""
    from app.api.v1.auth import get_or_create_user

    user = get_or_create_user(
        db,
        from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
        language_code=getattr(from_user, "language_code", None),
        signup_source="bot",
    )
    if not user.is_active:
        raise SuspendedAccount(f"this account is {user.status.value}")
    return user


def throttle_user(telegram_user_id: int, bucket: str, limit: int, window: int = 60) -> None:
    """Keep one user from hammering an expensive handler."""
    try:
        rate_limit(f"bot:{bucket}:{telegram_user_id}", limit, window)
    except RateLimited:
        raise
