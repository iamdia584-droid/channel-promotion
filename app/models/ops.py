"""Fraud, moderation, reports, audit, notifications, settings, pricing, events."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, Base, Money, StrEnumType, TelegramId, Timestamped, UTCDateTime, UUIDPk
from app.models.enums import (
    FraudBand,
    FraudCaseStatus,
    FraudSubject,
    NotificationChannel,
    NotificationStatus,
    PricingRuleScope,
    ReportReason,
    ReportStatus,
    ReviewDecision,
    ReviewTarget,
    SettingType,
)

# --------------------------------------------------------------------------
# Fraud
# --------------------------------------------------------------------------


class FraudEvent(UUIDPk, Base):
    """One scored signal firing, with the evidence an admin needs to judge it."""

    __tablename__ = "fraud_events"

    subject_type: Mapped[FraudSubject] = mapped_column(
        StrEnumType(FraudSubject, 16), nullable=False, index=True
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    advertiser_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    channel_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)

    signal: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    band: Mapped[FraudBand] = mapped_column(StrEnumType(FraudBand, 16), nullable=False, index=True)
    # Never a bare verdict: the admin must be able to inspect why (spec §16).
    evidence: Mapped[dict] = mapped_column(default=dict, nullable=False)
    action_taken: Mapped[str | None] = mapped_column(String(64))
    amount_at_risk: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_fraud_events_band_time", "band", "occurred_at"),
        CheckConstraint("score BETWEEN 0 AND 100", name="fraud_event_score_range"),
    )


class FraudScore(UUIDPk, Timestamped, Base):
    """Current composite risk for an entity, with the contributing signals."""

    __tablename__ = "fraud_scores"

    subject_type: Mapped[FraudSubject] = mapped_column(
        StrEnumType(FraudSubject, 16), nullable=False
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    band: Mapped[FraudBand] = mapped_column(StrEnumType(FraudBand, 16), nullable=False)
    signals: Mapped[dict] = mapped_column(default=dict, nullable=False)
    events_counted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", name="uq_fraud_scores_subject"),
        CheckConstraint("score BETWEEN 0 AND 100", name="fraud_score_value_range"),
    )


class FraudCase(UUIDPk, Timestamped, Base):
    """An investigation. Required before any punitive action (spec §16)."""

    __tablename__ = "fraud_cases"

    subject_type: Mapped[FraudSubject] = mapped_column(
        StrEnumType(FraudSubject, 16), nullable=False
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False, index=True)
    status: Mapped[FraudCaseStatus] = mapped_column(
        StrEnumType(FraudCaseStatus, 16), default=FraudCaseStatus.OPEN, nullable=False, index=True
    )
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    band: Mapped[FraudBand] = mapped_column(StrEnumType(FraudBand, 16), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500))
    evidence: Mapped[dict] = mapped_column(default=dict, nullable=False)
    amount_held: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    amount_reversed: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    assigned_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolution: Mapped[str | None] = mapped_column(String(500))


# --------------------------------------------------------------------------
# Moderation & reports
# --------------------------------------------------------------------------


class ModerationReview(UUIDPk, Timestamped, Base):
    """A moderator/admin decision on a campaign, creative or channel (spec §19)."""

    __tablename__ = "moderation_reviews"

    target_type: Mapped[ReviewTarget] = mapped_column(
        StrEnumType(ReviewTarget, 16), nullable=False, index=True
    )
    target_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False, index=True)
    decision: Mapped[ReviewDecision] = mapped_column(
        StrEnumType(ReviewDecision, 16), default=ReviewDecision.PENDING, nullable=False, index=True
    )
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    checklist: Mapped[dict] = mapped_column(default=dict, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    escalated_to_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_moderation_reviews_queue", "target_type", "decision", "created_at"),
    )


class Report(UUIDPk, Timestamped, Base):
    """A user report against an ad or channel."""

    __tablename__ = "reports"

    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL")
    )
    reporter_telegram_id: Mapped[int | None] = mapped_column(TelegramId)
    target_type: Mapped[ReviewTarget] = mapped_column(StrEnumType(ReviewTarget, 16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False, index=True)
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(GUID)
    reason: Mapped[ReportReason] = mapped_column(
        StrEnumType(ReportReason, 16), nullable=False, index=True
    )
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(
        StrEnumType(ReportStatus, 16), default=ReportStatus.OPEN, nullable=False, index=True
    )
    resolution: Mapped[str | None] = mapped_column(String(500))
    handled_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    handled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# --------------------------------------------------------------------------
# Audit & events
# --------------------------------------------------------------------------


class AuditLog(UUIDPk, Base):
    """Append-only record of every consequential action (spec §34).

    ``old_value``/``new_value`` make a silent financial edit impossible to hide:
    a manual balance adjustment writes both the audit row and a ledger
    transaction, and neither can be removed without leaving the other dangling.
    """

    __tablename__ = "audit_logs"

    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)  # staff/user/system
    actor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor_label: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), index=True)
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    old_value: Mapped[dict] = mapped_column(default=dict, nullable=False)
    new_value: Mapped[dict] = mapped_column(default=dict, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    request_id: Mapped[str | None] = mapped_column(String(64))
    is_financial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_target", "target_type", "target_id", "created_at"),
    )


class EventLog(UUIDPk, Base):
    """Persisted domain events (spec §30). The outbox for async fan-out."""

    __tablename__ = "event_log"

    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(default=dict, nullable=False)
    aggregate_type: Mapped[str | None] = mapped_column(String(32))
    aggregate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)

    __table_args__ = (Index("ix_event_log_pending", "dispatched", "created_at"),)


class IdempotencyRecord(UUIDPk, Base):
    """Stored result of a mutating API call keyed by its Idempotency-Key (spec §26)."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_records_scope_key"),)


class Notification(UUIDPk, Base):
    """Outbound notification (spec §27). Deduplicated so a retry cannot spam."""

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(TelegramId)
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="CASCADE")
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        StrEnumType(NotificationChannel, 16), default=NotificationChannel.TELEGRAM, nullable=False
    )
    template: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(default=dict, nullable=False)
    rendered_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        StrEnumType(NotificationStatus, 16),
        default=NotificationStatus.QUEUED,
        nullable=False,
        index=True,
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(160))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notifications_dedupe_key"),
        Index("ix_notifications_queue", "status", "created_at"),
    )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class SystemSetting(UUIDPk, Timestamped, Base):
    """Admin-editable global setting. Nothing pricing-related is hard-coded (spec §6)."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[SettingType] = mapped_column(StrEnumType(SettingType, 16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    min_value: Mapped[str | None] = mapped_column(String(64))
    max_value: Mapped[str | None] = mapped_column(String(64))
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )


class PricingRule(UUIDPk, Timestamped, Base):
    """A multiplier or commission override for one scope value (spec §7, §31).

    Rules are never deleted; ``active`` is toggled and ``effective_from`` bounds
    them, so the price applied to a past delivery stays reconstructible.
    """

    __tablename__ = "pricing_rules"

    scope: Mapped[PricingRuleScope] = mapped_column(
        StrEnumType(PricingRuleScope, 24), nullable=False, index=True
    )
    scope_value: Mapped[str] = mapped_column(String(48), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("1.0000"), nullable=False
    )
    commission_rate_override: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    min_cpm: Mapped[object | None] = mapped_column(Money)
    max_cpm: Mapped[object | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime())
    note: Mapped[str | None] = mapped_column(String(300))
    updated_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("scope", "scope_value", "currency", name="uq_pricing_rules_scope_value"),
        Index("ix_pricing_rules_lookup", "scope", "active", "scope_value"),
        CheckConstraint("multiplier > 0", name="pricing_multiplier_positive"),
        CheckConstraint(
            "commission_rate_override IS NULL OR "
            "(commission_rate_override >= 0 AND commission_rate_override <= 1)",
            name="pricing_commission_range",
        ),
    )


class Category(UUIDPk, Timestamped, Base):
    """Channel/ad category taxonomy, admin-managed."""

    __tablename__ = "categories"

    slug: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
