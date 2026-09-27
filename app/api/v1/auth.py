"""Authentication: Telegram identity in, session token out (spec §2, §25)."""

from __future__ import annotations

from fastapi import APIRouter, Header
from pydantic import Field
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, issue_session_token
from app.core.errors import PermissionDenied, Unauthenticated
from app.core.security import verify_telegram_login
from app.db.base import utcnow
from app.models.identity import Advertiser, Publisher, User
from app.schemas.common import Schema
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService

router = APIRouter(prefix="/auth", tags=["auth"])


class SessionOut(Schema):
    token: str
    user_id: str
    telegram_user_id: int
    display_name: str
    roles: list[str]


class RoleIn(Schema):
    role: str = Field(..., pattern="^(advertiser|publisher)$")


@router.post("/telegram", response_model=SessionOut)
def login_with_telegram(
    db: DbSession, x_telegram_init_data: str = Header(..., alias="X-Telegram-Init-Data")
) -> SessionOut:
    """Exchange a signed Telegram WebApp ``initData`` payload for a session.

    The signature is verified against the bot token, so a caller cannot claim to
    be another Telegram user.
    """
    import json

    data = verify_telegram_login(x_telegram_init_data)
    raw = json.loads(data.get("user", "{}"))
    telegram_id = int(raw.get("id", 0))
    if not telegram_id:
        raise Unauthenticated("initData carries no user id")

    user = get_or_create_user(
        db, telegram_id,
        username=raw.get("username"),
        first_name=raw.get("first_name"),
        last_name=raw.get("last_name"),
        language_code=raw.get("language_code"),
    )
    if not user.is_active:
        raise PermissionDenied(f"this account is {user.status.value}")
    return SessionOut(
        token=issue_session_token(user),
        user_id=str(user.id),
        telegram_user_id=user.telegram_user_id,
        display_name=user.display_name,
        roles=sorted(r.value for r in user.roles),
    )


@router.get("/me", response_model=SessionOut)
def whoami(principal: CurrentPrincipal) -> SessionOut:
    user = principal.user
    if user is None:
        raise Unauthenticated("no user on this session")
    return SessionOut(
        token="", user_id=str(user.id), telegram_user_id=user.telegram_user_id,
        display_name=user.display_name, roles=sorted(r.value for r in user.roles),
    )


@router.post("/roles", response_model=SessionOut)
def enable_role(db: DbSession, principal: CurrentPrincipal, body: RoleIn) -> SessionOut:
    """Opt into the advertiser or publisher side. A user may be both."""
    user = principal.user
    if user is None:
        raise Unauthenticated("no user on this session")
    if not SettingsService(db).bool_("registration_open"):
        raise PermissionDenied("registration is currently closed")

    if body.role == "advertiser":
        ensure_advertiser(db, user)
    else:
        ensure_publisher(db, user)
    return SessionOut(
        token="", user_id=str(user.id), telegram_user_id=user.telegram_user_id,
        display_name=user.display_name, roles=sorted(r.value for r in user.roles),
    )


# --------------------------------------------------------------------------
# Shared helpers, also used by the bot
# --------------------------------------------------------------------------


def get_or_create_user(
    db, telegram_user_id: int, *, username=None, first_name=None,
    last_name=None, language_code=None, signup_source: str = "telegram",
) -> User:
    """Look a user up by Telegram id, never by username (spec §2).

    A username can be changed or transferred; the numeric id is permanent, so it
    is the only safe internal identity.
    """
    user = db.scalars(
        select(User).where(User.telegram_user_id == telegram_user_id)
    ).one_or_none()
    if user is None:
        user = User(
            telegram_user_id=telegram_user_id, username=username,
            first_name=first_name, last_name=last_name,
            language_code=language_code, signup_source=signup_source,
        )
        db.add(user)
    else:
        # Refresh the display fields; they are cosmetic and do change upstream.
        user.username = username or user.username
        user.first_name = first_name or user.first_name
        user.last_name = last_name or user.last_name
        user.language_code = language_code or user.language_code
    user.last_seen_at = utcnow()
    db.flush()
    return user


def ensure_advertiser(db, user: User) -> Advertiser:
    from app.core.config import settings

    advertiser = db.scalars(
        select(Advertiser).where(Advertiser.user_id == user.id)
    ).one_or_none()
    if advertiser is None:
        advertiser = Advertiser(user_id=user.id, currency=settings.default_currency)
        db.add(advertiser)
        db.flush()
        WalletService(db).for_advertiser(advertiser.id)
    user.is_advertiser = True
    db.flush()
    return advertiser


def ensure_publisher(db, user: User) -> Publisher:
    from app.core.config import settings

    publisher = db.scalars(
        select(Publisher).where(Publisher.user_id == user.id)
    ).one_or_none()
    if publisher is None:
        publisher = Publisher(user_id=user.id, currency=settings.default_currency)
        db.add(publisher)
        db.flush()
        WalletService(db).for_publisher(publisher.id)
    user.is_publisher = True
    db.flush()
    return publisher
