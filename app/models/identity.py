"""Users and their role profiles.

Identity is keyed on ``telegram_user_id`` (BIGINT). Usernames are stored for
display only and are never used for lookup or authorisation, because a Telegram
username can be changed or transferred to someone else (spec §2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, Money, StrEnumType, TelegramId, Timestamped, UUIDPk
from app.models.enums import Role, UserStatus

if TYPE_CHECKING:
    from app.models.money import Wallet
    from app.models.telegram import PublisherChannel


class User(UUIDPk, Timestamped, Base):
    """A Telegram human. One row per Telegram account, whatever roles they hold."""

    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(
        TelegramId, unique=True, nullable=False, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64))  # display only, mutable upstream
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(2))

    is_advertiser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_publisher: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_moderator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[UserStatus] = mapped_column(
        StrEnumType(UserStatus, 16), default=UserStatus.ACTIVE, nullable=False, index=True
    )
    suspension_reason: Mapped[str | None] = mapped_column(String(500))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signup_source: Mapped[str | None] = mapped_column(String(32))
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    advertiser: Mapped["Advertiser | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    publisher: Mapped["Publisher | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_users_status_created", "status", "created_at"),)

    @property
    def roles(self) -> set[Role]:
        out: set[Role] = set()
        if self.is_advertiser:
            out.add(Role.ADVERTISER)
        if self.is_publisher:
            out.add(Role.PUBLISHER)
        if self.is_moderator:
            out.add(Role.MODERATOR)
        if self.is_admin:
            out.add(Role.ADMIN)
        return out

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        name = " ".join(filter(None, [self.first_name, self.last_name])).strip()
        return name or f"tg:{self.telegram_user_id}"


class Advertiser(UUIDPk, Timestamped, Base):
    """Advertiser profile and lifetime aggregates (aggregates are projections)."""

    __tablename__ = "advertisers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    company_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    billing_country: Mapped[str | None] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    status: Mapped[UserStatus] = mapped_column(
        StrEnumType(UserStatus, 16), default=UserStatus.ACTIVE, nullable=False, index=True
    )
    trust_tier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lifetime_deposited: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    lifetime_spent: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    campaigns_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="advertiser")
    wallet: Mapped["Wallet | None"] = relationship(
        back_populates="advertiser", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE


class Publisher(UUIDPk, Timestamped, Base):
    """Channel/group owner profile."""

    __tablename__ = "publishers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    payout_country: Mapped[str | None] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    status: Mapped[UserStatus] = mapped_column(
        StrEnumType(UserStatus, 16), default=UserStatus.ACTIVE, nullable=False, index=True
    )
    auto_advertising: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    accepted_categories: Mapped[dict] = mapped_column(default=dict, nullable=False)
    # Publisher-level frequency guard rails (spec §18).
    max_ads_per_day: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_ads_per_week: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    min_ad_interval_minutes: Mapped[int] = mapped_column(Integer, default=240, nullable=False)

    lifetime_earned: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    lifetime_withdrawn: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    fraud_strikes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="publisher")
    wallet: Mapped["Wallet | None"] = relationship(
        back_populates="publisher", uselist=False, cascade="all, delete-orphan"
    )
    channels: Mapped[list["PublisherChannel"]] = relationship(
        back_populates="publisher", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE


class StaffUser(UUIDPk, Timestamped, Base):
    """Admin/moderator credentials for the web dashboard.

    Separate from :class:`User` on purpose: dashboard access is password + TOTP,
    not Telegram identity, so a compromised Telegram account cannot reach the
    admin surface.
    """

    __tablename__ = "staff_users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(StrEnumType(Role, 16), nullable=False)

    telegram_user_id: Mapped[int | None] = mapped_column(TelegramId, unique=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recovery_code_hashes: Mapped[dict] = mapped_column(default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("email", name="uq_staff_users_email"),)

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN
