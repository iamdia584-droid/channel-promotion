"""FastAPI dependencies: auth, RBAC, rate limiting, idempotency (spec §25)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.cache import rate_limit
from app.core.config import settings
from app.core.errors import PermissionDenied, SuspendedAccount, Unauthenticated
from app.core.security import (
    constant_time_equals,
    unsign,
    verify_telegram_login,
)
from app.db.session import get_session
from app.models.enums import Role
from app.models.identity import Advertiser, Publisher, StaffUser, User
from app.services.audit import Actor

SESSION_SALT = "api-session"
SESSION_MAX_AGE = 7 * 24 * 3600

DbSession = Annotated[Session, Depends(get_session)]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller and what they are allowed to do."""

    user: User | None = None
    staff: StaffUser | None = None

    @property
    def roles(self) -> set[Role]:
        if self.staff is not None:
            return {Role.ADMIN} if self.staff.is_admin else {Role.MODERATOR}
        return self.user.roles if self.user else set()

    @property
    def actor(self) -> Actor:
        if self.staff is not None:
            return Actor.staff(self.staff)
        if self.user is not None:
            return Actor.user(self.user)
        return Actor.system("anonymous")

    def require(self, *roles: Role) -> None:
        if not roles:
            return
        if not (set(roles) & self.roles):
            raise PermissionDenied(
                f"this endpoint requires one of: {', '.join(sorted(r.value for r in roles))}"
            )


def issue_session_token(user: User) -> str:
    """Sign a session for a Telegram-authenticated user."""
    from app.core.security import sign

    return sign({"uid": str(user.id), "tg": user.telegram_user_id}, SESSION_SALT)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _staff_from_cookie(request: Request, db: Session) -> StaffUser | None:
    """Recognise an admin dashboard session on the REST API.

    The dashboard and the API are two front doors to the same service. Without
    this bridge nothing could satisfy the staff dependencies at all, and the
    dashboard's own export links would 401.
    """
    from app.admin.auth import SESSION_COOKIE, SESSION_SALT

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = unsign(token, SESSION_SALT, settings.admin_session_max_age)
    staff = db.get(StaffUser, uuid.UUID(payload["sid"]))
    if staff is None or not staff.is_active:
        raise Unauthenticated("this staff session is no longer valid")

    # A cookie travels automatically, so a state-changing request authenticated by
    # one needs the same CSRF proof the dashboard forms carry.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        submitted = request.headers.get("x-csrf-token", "")
        expected = payload.get("csrf", "")
        if not expected or not submitted or not constant_time_equals(submitted, expected):
            raise PermissionDenied(
                "an X-CSRF-Token header matching your session is required for this request"
            )
    return staff


def current_principal(
    request: Request,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> Principal:
    """Authenticate by bearer token, Telegram initData, or a staff cookie."""
    token = _bearer(authorization)
    if token:
        payload = unsign(token, SESSION_SALT, SESSION_MAX_AGE)
        user = db.get(User, uuid.UUID(payload["uid"]))
        if user is None:
            raise Unauthenticated("session refers to an unknown user")
        if not user.is_active:
            raise SuspendedAccount(f"this account is {user.status.value}")
        return Principal(user=user)

    if x_telegram_init_data:
        data = verify_telegram_login(x_telegram_init_data)
        import json

        raw = json.loads(data.get("user", "{}"))
        telegram_id = int(raw.get("id", 0))
        if not telegram_id:
            raise Unauthenticated("initData carries no user id")
        user = db.scalars(select(User).where(User.telegram_user_id == telegram_id)).one_or_none()
        if user is None:
            raise Unauthenticated("no account for this Telegram user yet")
        if not user.is_active:
            raise SuspendedAccount(f"this account is {user.status.value}")
        return Principal(user=user)

    staff = _staff_from_cookie(request, db)
    if staff is not None:
        return Principal(staff=staff)

    raise Unauthenticated("authentication required")


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def current_advertiser(db: DbSession, principal: CurrentPrincipal) -> Advertiser:
    principal.require(Role.ADVERTISER)
    advertiser = db.scalars(
        select(Advertiser).where(Advertiser.user_id == principal.user.id)
    ).one_or_none()
    if advertiser is None:
        raise PermissionDenied("no advertiser profile on this account")
    if not advertiser.is_active:
        raise SuspendedAccount(f"this advertiser account is {advertiser.status.value}")
    return advertiser


def current_publisher(db: DbSession, principal: CurrentPrincipal) -> Publisher:
    principal.require(Role.PUBLISHER)
    publisher = db.scalars(
        select(Publisher).where(Publisher.user_id == principal.user.id)
    ).one_or_none()
    if publisher is None:
        raise PermissionDenied("no publisher profile on this account")
    if not publisher.is_active:
        raise SuspendedAccount(f"this publisher account is {publisher.status.value}")
    return publisher


def require_staff(principal: CurrentPrincipal) -> StaffUser:
    if principal.staff is None:
        raise PermissionDenied("staff authentication required")
    return principal.staff


def require_admin(principal: CurrentPrincipal) -> StaffUser:
    staff = require_staff(principal)
    if not staff.is_admin:
        raise PermissionDenied("this endpoint requires an administrator")
    return staff


CurrentAdvertiser = Annotated[Advertiser, Depends(current_advertiser)]
CurrentPublisher = Annotated[Publisher, Depends(current_publisher)]
RequireStaff = Annotated[StaffUser, Depends(require_staff)]
RequireAdmin = Annotated[StaffUser, Depends(require_admin)]


def throttle(bucket: str, limit: int, window: int = 60):
    """Per-principal rate limit for an endpoint (spec §25)."""

    def _dep(request: Request, principal: CurrentPrincipal) -> None:
        identity = (
            str(principal.user.id)
            if principal.user
            else str(principal.staff.id)
            if principal.staff
            else (request.client.host if request.client else "anonymous")
        )
        rate_limit(f"{bucket}:{identity}", limit, window)

    return Depends(_dep)


IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
