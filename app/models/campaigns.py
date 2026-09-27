"""Campaigns, creatives, targeting and spend pacing state."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, Money, StrEnumType, Timestamped, UTCDateTime, UUIDPk
from app.models.enums import (
    AdStatus,
    CampaignStatus,
    CampaignType,
    PricingModel,
)

if TYPE_CHECKING:
    from app.models.identity import Advertiser


class Campaign(UUIDPk, Timestamped, Base):
    __tablename__ = "campaigns"

    advertiser_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("advertisers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_type: Mapped[CampaignType] = mapped_column(StrEnumType(CampaignType, 24), nullable=False)
    pricing_model: Mapped[PricingModel] = mapped_column(
        StrEnumType(PricingModel, 8), default=PricingModel.CPM, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    status: Mapped[CampaignStatus] = mapped_column(
        StrEnumType(CampaignStatus, 16), default=CampaignStatus.DRAFT, nullable=False, index=True
    )

    total_budget: Mapped[object] = mapped_column(Money, nullable=False)
    daily_budget: Mapped[object] = mapped_column(Money, nullable=False)
    bid_cpm: Mapped[object] = mapped_column(Money, nullable=False)

    # Projections of the ledger. The ledger is authoritative; these exist so
    # pacing decisions do not need to aggregate the whole ledger on every request.
    reserved_amount: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    spent_amount: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    refunded_amount: Mapped[object] = mapped_column(Money, default="0", nullable=False)

    billable_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    # Frequency capping (spec §18). Per-user capping only binds on users who
    # interact — Telegram exposes no passive-viewer identity.
    max_impressions_per_user: Mapped[int | None] = mapped_column(Integer)
    max_impressions_per_channel_per_day: Mapped[int | None] = mapped_column(Integer)

    allow_specific_channels: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    paused_reason: Mapped[str | None] = mapped_column(String(300))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    advertiser: Mapped["Advertiser"] = relationship()
    ads: Mapped[list["Advertisement"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    target: Mapped["CampaignTarget | None"] = relationship(
        back_populates="campaign", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_campaigns_serving", "status", "starts_at", "ends_at"),
        Index("ix_campaigns_advertiser_status", "advertiser_id", "status"),
        CheckConstraint("total_budget > 0", name="total_budget_positive"),
        CheckConstraint("daily_budget > 0", name="daily_budget_positive"),
        CheckConstraint("daily_budget <= total_budget", name="daily_budget_within_total"),
        CheckConstraint("bid_cpm > 0", name="bid_cpm_positive"),
        CheckConstraint("ends_at > starts_at", name="schedule_ordered"),
        CheckConstraint("spent_amount >= 0", name="spent_non_negative"),
        CheckConstraint("reserved_amount >= 0", name="reserved_non_negative"),
        CheckConstraint("spent_amount <= total_budget", name="spend_within_budget"),
        CheckConstraint("priority BETWEEN 1 AND 10", name="priority_range"),
    )

    @property
    def remaining_budget(self):
        from app.core.money import q

        return q(self.total_budget) - q(self.spent_amount) - q(self.refunded_amount)

    @property
    def is_schedulable(self) -> bool:
        from app.db.base import utcnow

        now = utcnow()
        return (
            self.status is CampaignStatus.RUNNING
            and self.starts_at <= now < self.ends_at
            and self.remaining_budget > 0
        )


class Advertisement(UUIDPk, Timestamped, Base):
    """A creative belonging to a campaign."""

    __tablename__ = "advertisements"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[AdStatus] = mapped_column(
        StrEnumType(AdStatus, 16), default=AdStatus.DRAFT, nullable=False, index=True
    )
    ad_format: Mapped[CampaignType] = mapped_column(StrEnumType(CampaignType, 24), nullable=False)

    body_text: Mapped[str | None] = mapped_column(Text)
    # Telegram file_id: opaque, bot-scoped. We keep the original URL separately
    # because file_ids are not portable between bots.
    media_file_id: Mapped[str | None] = mapped_column(String(300))
    media_url: Mapped[str | None] = mapped_column(String(1000))
    media_mime: Mapped[str | None] = mapped_column(String(64))

    destination_url: Mapped[str | None] = mapped_column(String(2000))
    cta_text: Mapped[str | None] = mapped_column(String(64))
    disable_preview: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    campaign: Mapped[Campaign] = relationship(back_populates="ads")

    __table_args__ = (
        CheckConstraint(
            "body_text IS NOT NULL OR media_file_id IS NOT NULL OR media_url IS NOT NULL",
            name="creative_has_content",
        ),
    )

    @property
    def is_servable(self) -> bool:
        return self.status in {AdStatus.APPROVED, AdStatus.RUNNING}


class CampaignTarget(UUIDPk, Timestamped, Base):
    """Targeting criteria. One row per campaign; lists live in JSONB.

    JSONB rather than join tables because these are read as a whole on every
    delivery decision and are never queried by individual element.
    """

    __tablename__ = "campaign_targets"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    countries: Mapped[dict] = mapped_column(default=list, nullable=False)
    languages: Mapped[dict] = mapped_column(default=list, nullable=False)
    categories: Mapped[dict] = mapped_column(default=list, nullable=False)
    excluded_categories: Mapped[dict] = mapped_column(default=list, nullable=False)
    audience_types: Mapped[dict] = mapped_column(default=list, nullable=False)

    min_members: Mapped[int | None] = mapped_column(Integer)
    max_members: Mapped[int | None] = mapped_column(Integer)
    min_avg_views: Mapped[int | None] = mapped_column(Integer)
    max_avg_views: Mapped[int | None] = mapped_column(Integer)
    min_quality_score: Mapped[object | None] = mapped_column(Money)

    campaign: Mapped[Campaign] = relationship(back_populates="target")

    __table_args__ = (
        CheckConstraint(
            "max_members IS NULL OR min_members IS NULL OR max_members >= min_members",
            name="member_range_ordered",
        ),
        CheckConstraint(
            "max_avg_views IS NULL OR min_avg_views IS NULL OR max_avg_views >= min_avg_views",
            name="view_range_ordered",
        ),
    )


class CampaignPublisher(UUIDPk, Timestamped, Base):
    """Explicit allow/deny of a campaign against a specific channel (spec §1)."""

    __tablename__ = "campaign_publishers"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publisher_channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("campaign_id", "channel_id", name="uq_campaign_publishers_pair"),
    )


class CampaignDailySpend(UUIDPk, Base):
    """Authoritative daily pacing counter (spec §10).

    Redis holds a fast mirror, but this row — with its CHECK against the
    campaign's daily budget — is what makes overspend impossible.
    """

    __tablename__ = "campaign_daily_spend"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    spend_date: Mapped[date] = mapped_column(Date, nullable=False)
    reserved: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    spent: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deliveries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "spend_date", name="uq_campaign_daily_spend_pair"),
        Index("ix_campaign_daily_spend_date", "spend_date"),
        CheckConstraint("spent >= 0 AND reserved >= 0", name="daily_amounts_non_negative"),
    )
