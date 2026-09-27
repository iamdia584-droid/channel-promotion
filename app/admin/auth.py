"""Admin dashboard authentication: password + TOTP + CSRF (spec §28)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import PermissionDenied, Unauthenticated, ValidationFailed
from app.core.security import (
    constant_time_equals,
    hash_password,
    needs_rehash,
    sign,
    unsign,
    verify_password,
    verify_totp,
)
from app.db.base import utcnow
from app.models.enums import Role
from app.models.identity import StaffUser

SESSION_COOKIE = "adnet_admin"
CSRF_COOKIE = "adnet_csrf"
SESSION_SALT = "admin-session"
MAX_FAILED = 5
LOCKOUT_MINUTES = 15


@dataclass(frozen=True)
class LoginResult:
    staff: StaffUser
    token: str
    csrf: str


def authenticate(db: Session, email: str, password: str, totp_code: str | None) -> LoginResult:
    staff = db.scalars(
        select(StaffUser).where(StaffUser.email == email.strip().lower())
    ).one_or_none()

    # Same message whichever part failed: distinguishing them would let an
    # attacker enumerate valid staff emails.
    generic = Unauthenticated("email, password or code is incorrect")

    if staff is None or not staff.is_active:
        raise generic
    if staff.locked_until and staff.locked_until > utcnow():
        raise PermissionDenied(
            "this account is temporarily locked after repeated failed sign-ins"
        )
    if not verify_password(password, staff.password_hash):
        staff.failed_logins += 1
        if staff.failed_logins >= MAX_FAILED:
            staff.locked_until = utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            staff.failed_logins = 0
        db.flush()
        raise generic

    if staff.totp_enabled:
        if not totp_code or not verify_totp(staff.totp_secret or "", totp_code):
            staff.failed_logins += 1
            db.flush()
            raise generic
    elif settings.admin_require_2fa and settings.is_production:
        raise PermissionDenied(
            "two-factor authentication must be enabled on this account before "
            "signing in to production"
        )

    if needs_rehash(staff.password_hash):
        staff.password_hash = hash_password(password)
    staff.failed_logins = 0
    staff.locked_until = None
    staff.last_login_at = utcnow()
    db.flush()

    csrf = secrets.token_urlsafe(24)
    token = sign({"sid": str(staff.id), "csrf": csrf}, SESSION_SALT)
    return LoginResult(staff=staff, token=token, csrf=csrf)


def current_staff(request: Request, db: Session) -> StaffUser:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Unauthenticated("sign in required")
    payload = unsign(token, SESSION_SALT, settings.admin_session_max_age)
    import uuid

    staff = db.get(StaffUser, uuid.UUID(payload["sid"]))
    if staff is None or not staff.is_active:
        raise Unauthenticated("this session is no longer valid")
    return staff


def verify_csrf(request: Request, submitted: str | None) -> None:
    """Double-submit cookie check on every state-changing form (spec §28)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Unauthenticated("sign in required")
    payload = unsign(token, SESSION_SALT, settings.admin_session_max_age)
    expected = payload.get("csrf", "")
    if not submitted or not constant_time_equals(submitted, expected):
        raise PermissionDenied("this form expired; reload the page and try again")


def require_role(staff: StaffUser, *roles: Role) -> None:
    if roles and staff.role not in roles:
        raise PermissionDenied(
            f"this action requires: {', '.join(r.value for r in roles)}"
        )


def bootstrap_admin(db: Session) -> StaffUser | None:
    """Create the first admin from env vars, once. Never overwrites an existing one."""
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = settings.bootstrap_admin_password
    if not email or not password:
        return None
    existing = db.scalars(select(StaffUser).where(StaffUser.email == email)).one_or_none()
    if existing is not None:
        return existing
    if len(password) < 12:
        raise ValidationFailed("BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters")
    staff = StaffUser(
        email=email, password_hash=hash_password(password),
        full_name="Bootstrap Admin", role=Role.ADMIN, is_active=True,
    )
    db.add(staff)
    db.flush()
    return staff
