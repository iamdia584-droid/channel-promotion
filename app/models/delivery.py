"""Ad deliveries and the append-only impression ledger (spec §8).

``impressions`` is treated as immutable evidence: rows are inserted, never
rewritten to hide a mistake. A retracted impression gets ``validation_status``
moved off ``VALID`` and a reversal posted in the financial ledger — the original
row and its fraud evidence stay readable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import GUID, Base, Money, StrEnumType, TelegramId, Timestamped, UTCDateTime, UUIDPk
from app.models.enums import (
    DeliveryStatus,
    ImpressionKind,
    ImpressionSource,
    SelectionMode,
    ValidationStatus,
)

if TYPE_CHECKING:
    from app.models.campaigns import Advertisement, Campaign
    from app.models.telegram import PublisherChannel


class AdDelivery(UUIDPk, Timestamped, Base):
    """One placement of one creative into one channel.

    The price quote is *frozen* onto this row at planning time. Admin changing a
    pricing rule tomorrow must not alter what this delivery already owes.
    """

    __tablename__ = "ad_deliveries"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    advertisement_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("advertisements.id", ondelete="RESTRICT"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publisher_channels.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status: Mapped[DeliveryStatus] = mapped_column(
        StrEnumType(DeliveryStatus, 16), default=DeliveryStatus.PLANNED, nullable=False, index=True
    )

    telegram_chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    message_link: Mapped[str | None] = mapped_column(String(300))

    # --- frozen price quote ---
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    advertiser_cpm: Mapped[object] = mapped_column(Money, nullable=False)
    effective_cpm: Mapped[object] = mapped_column(Money, nullable=False)
    publisher_cpm: Mapped[object] = mapped_column(Money, nullable=False)
    commission_rate: Mapped[object] = mapped_column(Numeric(6, 4), nullable=False)
    price_breakdown: Mapped[dict] = mapped_column(default=dict, nullable=False)

    selection_mode: Mapped[SelectionMode] = mapped_column(
        StrEnumType(SelectionMode, 16), nullable=False
    )
    selection_score: Mapped[object] = mapped_column(Numeric(12, 6), default="0", nullable=False)
    selection_debug: Mapped[dict] = mapped_column(default=dict, nullable=False)

    # --- budget reservation ---
    reserved_amount: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    settled_amount: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    impression_allowance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- measurement ---
    billable_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    measured_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reported_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Monotonic ratchet: a view counter that jumps then drops cannot double-bill.
    reported_views_high_water: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    impression_cap: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    tracking_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    measurement_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    campaign: Mapped[Campaign] = relationship()
    advertisement: Mapped[Advertisement] = relationship()
    channel: Mapped[PublisherChannel] = relationship()

    __table_args__ = (
        UniqueConstraint("tracking_nonce", name="uq_ad_deliveries_tracking_nonce"),
        Index("ix_ad_deliveries_settlement", "status", "measurement_ends_at"),
        Index("ix_ad_deliveries_channel_sent", "channel_id", "sent_at"),
        Index("ix_ad_deliveries_campaign_channel", "campaign_id", "channel_id", "sent_at"),
        CheckConstraint("billable_impressions >= 0", name="billable_non_negative"),
        CheckConstraint(
            "billable_impressions <= impression_cap OR impression_cap = 0",
            name="billable_within_cap",
        ),
        CheckConstraint("settled_amount >= 0", name="settled_non_negative"),
        CheckConstraint("settled_amount <= reserved_amount", name="settled_within_reserved"),
        CheckConstraint("publisher_cpm <= effective_cpm", name="publisher_share_within_gross"),
    )


class Impression(UUIDPk, Base):
    """Append-only impression evidence. Never updated to erase history."""

    __tablename__ = "impressions"

    delivery_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ad_deliveries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    advertisement_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("advertisements.id", ondelete="RESTRICT"), nullable=False
    )
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publisher_channels.id", ondelete="RESTRICT"), nullable=False
    )

    telegram_chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    # Present only for interacting users; NULL for counter-derived impressions,
    # because Telegram never identifies a passive viewer.
    telegram_user_id: Mapped[int | None] = mapped_column(TelegramId, index=True)
    session_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    kind: Mapped[ImpressionKind] = mapped_column(
        StrEnumType(ImpressionKind, 24), nullable=False, index=True
    )
    source: Mapped[ImpressionSource] = mapped_column(
        StrEnumType(ImpressionSource, 24), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    validation_status: Mapped[ValidationStatus] = mapped_column(
        StrEnumType(ValidationStatus, 16),
        default=ValidationStatus.PENDING,
        nullable=False,
        index=True,
    )
    billable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fraud_reasons: Mapped[dict] = mapped_column(default=list, nullable=False)

    unit_advertiser_cost: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    unit_publisher_revenue: Mapped[object] = mapped_column(Money, default="0", nullable=False)

    # The structural defence against double counting (spec §8, §26). Uniqueness is
    # enforced by the database, not by an application check that a race can lose.
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    settlement_batch_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_impressions_dedupe_key"),
        Index("ix_impressions_delivery_status", "delivery_id", "validation_status"),
        Index("ix_impressions_billing", "delivery_id", "billable", "settlement_batch_id"),
        Index("ix_impressions_campaign_time", "campaign_id", "occurred_at"),
        Index("ix_impressions_publisher_time", "publisher_id", "occurred_at"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("fraud_score BETWEEN 0 AND 100", name="impression_fraud_score_range"),
        CheckConstraint(
            "NOT billable OR validation_status = 'valid'", name="billable_implies_valid"
        ),
    )


class Click(UUIDPk, Base):
    """A validated click. Also the primary MEASURED impression source."""

    __tablename__ = "clicks"

    delivery_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ad_deliveries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    impression_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("impressions.id", ondelete="SET NULL")
    )
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(TelegramId, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fraud_reasons: Mapped[dict] = mapped_column(default=list, nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    referrer: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_clicks_dedupe_key"),)


class ConversionEvent(UUIDPk, Base):
    """Advertiser-reported postback conversion (phase 2, table present for §24)."""

    __tablename__ = "conversion_events"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ad_deliveries.id", ondelete="SET NULL")
    )
    click_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("clicks.id", ondelete="SET NULL")
    )
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    external_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "external_id", name="uq_conversion_events_external"),
    )
