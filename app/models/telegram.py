"""Telegram chats, publisher channel registrations and their analytics history."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import GUID, Base, Money, StrEnumType, TelegramId, Timestamped, UTCDateTime, UUIDPk
from app.models.enums import ChannelStatus, ChatType, MeasurementMode, VerificationStatus

if TYPE_CHECKING:
    from app.models.identity import Publisher


class TelegramChat(UUIDPk, Timestamped, Base):
    """A Telegram chat as Telegram describes it, independent of who claims it.

    Kept separate from :class:`PublisherChannel` so that a chat's identity and
    observed facts survive a publisher deregistering, and so two publishers
    cannot create divergent records of the same chat.
    """

    __tablename__ = "telegram_chats"

    telegram_chat_id: Mapped[int] = mapped_column(
        TelegramId, unique=True, nullable=False, index=True
    )
    chat_type: Mapped[ChatType] = mapped_column(StrEnumType(ChatType, 16), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(1000))
    invite_link: Mapped[str | None] = mapped_column(String(300))

    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    member_count_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    bot_is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bot_can_post: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bot_can_delete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bot_rights_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    blacklist_reason: Mapped[str | None] = mapped_column(String(500))
    first_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)


class PublisherChannel(UUIDPk, Timestamped, Base):
    """A publisher's registered inventory slot for one Telegram chat."""

    __tablename__ = "publisher_channels"

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_chat_id_ref: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("telegram_chats.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False, index=True)

    # Publisher-declared classification. A *claim* until corroborated — Telegram
    # exposes none of this (docs/TELEGRAM_CONSTRAINTS.md).
    category: Mapped[str | None] = mapped_column(String(48), index=True)
    language: Mapped[str | None] = mapped_column(String(8), index=True)
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    audience_type: Mapped[str | None] = mapped_column(String(32))
    accepted_categories: Mapped[dict] = mapped_column(default=dict, nullable=False)

    status: Mapped[ChannelStatus] = mapped_column(
        StrEnumType(ChannelStatus, 16), default=ChannelStatus.PENDING, nullable=False, index=True
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        StrEnumType(VerificationStatus, 24), default=VerificationStatus.UNVERIFIED, nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    verification_evidence: Mapped[dict] = mapped_column(default=dict, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))

    measurement_mode: Mapped[MeasurementMode] = mapped_column(
        StrEnumType(MeasurementMode, 16), default=MeasurementMode.CLICK_ONLY, nullable=False
    )

    # Observed performance. avg_views — not member_count — is the billing basis.
    avg_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    median_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_ad_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_ads_served: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_score: Mapped[object] = mapped_column(Numeric(6, 4), default="0.5000", nullable=False)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    lifetime_earned: Mapped[object] = mapped_column(Money, default="0", nullable=False)

    # Per-channel frequency overrides; NULL falls back to the publisher's values.
    max_ads_per_day: Mapped[int | None] = mapped_column(Integer)
    max_ads_per_week: Mapped[int | None] = mapped_column(Integer)
    min_ad_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    min_cpm_floor: Mapped[object | None] = mapped_column(Money)

    auto_advertising: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_ad_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)

    publisher: Mapped[Publisher] = relationship(back_populates="channels")
    chat: Mapped[TelegramChat] = relationship()

    __table_args__ = (
        # One chat can only be monetised by one publisher registration.
        UniqueConstraint("telegram_chat_id", name="uq_publisher_channels_telegram_chat_id"),
        Index("ix_publisher_channels_serving", "status", "auto_advertising", "category"),
        Index("ix_publisher_channels_pub_status", "publisher_id", "status"),
        CheckConstraint("avg_views >= 0", name="avg_views_non_negative"),
        CheckConstraint("quality_score >= 0 AND quality_score <= 1", name="quality_score_range"),
        CheckConstraint("fraud_score >= 0 AND fraud_score <= 100", name="fraud_score_range"),
    )

    @property
    def can_serve(self) -> bool:
        return self.status.can_serve and self.auto_advertising and not self.chat.is_blacklisted

    @property
    def view_member_ratio(self) -> float:
        """Engagement proxy. A 100k-member channel with 3k views scores 0.03."""
        members = self.chat.member_count if self.chat else 0
        return (self.avg_views / members) if members else 0.0


class ChannelStatDaily(UUIDPk, Base):
    """One row per channel per day. The basis for growth and consistency signals."""

    __tablename__ = "channel_stats_daily"

    channel_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publisher_channels.id", ondelete="CASCADE"), nullable=False
    )
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)

    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    member_delta: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    median_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ad_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ad_clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ads_served: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    earnings: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now()
    )

    __table_args__ = (
        UniqueConstraint("channel_id", "stat_date", name="uq_channel_stats_daily_channel_date"),
        Index("ix_channel_stats_daily_date", "stat_date"),
    )


class ChannelVerificationAttempt(UUIDPk, Timestamped, Base):
    """Audit trail of every ownership check, successful or not."""

    __tablename__ = "channel_verification_attempts"

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("publisher_channels.id", ondelete="SET NULL")
    )
    submitted_identifier: Mapped[str] = mapped_column(String(300), nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(TelegramId)
    result: Mapped[VerificationStatus] = mapped_column(
        StrEnumType(VerificationStatus, 24), nullable=False
    )
    evidence: Mapped[dict] = mapped_column(default=dict, nullable=False)
