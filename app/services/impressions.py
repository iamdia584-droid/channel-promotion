"""Impression recording and validation (spec §8).

Four defences against inflated billing, in the order they apply:

1. **Structural dedupe.** ``impressions.dedupe_key`` is UNIQUE in the database.
   Refresh abuse, replayed tracking links and retried workers all collide on it.
   An application-level "have I seen this?" check can lose a race; a unique index
   cannot.
2. **Window.** An impression outside the delivery's measurement window does not
   bill.
3. **Ratchet cap.** A delivery cannot bill more than
   ``min(channel.avg_views × cap_multiplier, funded impressions)``. A channel
   cannot bill more reach than it has historically demonstrated.
4. **Fraud score.** Above the configured block threshold, the row is recorded as
   evidence but is not billable.

Rejected impressions are *recorded*, never dropped: the evidence is what lets an
admin adjudicate a dispute, and the counters feed fraud detection.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.money import ZERO, cpm_cost, q, split_commission
from app.db.base import utcnow
from app.models.delivery import AdDelivery, Click, Impression
from app.models.enums import (
    DeliveryStatus,
    ImpressionKind,
    ImpressionSource,
    MeasurementMode,
    ValidationStatus,
)
from app.models.telegram import PublisherChannel
from app.services.settings_service import SettingsService


def hash_identity(*parts: object) -> str:
    """Salted one-way hash. We never store a raw IP or user agent (spec §28)."""
    from app.core.config import settings as app_settings

    material = "|".join(str(p) for p in parts)
    return hashlib.sha256(f"{app_settings.secret_key}|{material}".encode()).hexdigest()[:64]


@dataclass(frozen=True)
class RecordResult:
    impression: Impression | None
    accepted: bool
    status: ValidationStatus
    reason: str = ""

    @property
    def billable(self) -> bool:
        return self.accepted and self.status.is_billable


class ImpressionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = SettingsService(session)

    # -- caps --------------------------------------------------------------

    def compute_cap(self, delivery: AdDelivery, channel: PublisherChannel) -> int:
        """The most this delivery may ever bill.

        Two ceilings, whichever is lower: demonstrated reach, and funded reach.
        Without the first, a compromised or colluding publisher could bill
        unlimited impressions against a large budget.
        """
        multiplier = self.settings.decimal("impression_cap_multiplier")
        demonstrated = int((q(channel.avg_views) * multiplier).to_integral_value())
        funded = int(delivery.impression_allowance or 0)
        candidates = [c for c in (demonstrated, funded) if c > 0]
        return min(candidates) if candidates else 0

    # -- recording ---------------------------------------------------------

    def record(
        self,
        delivery: AdDelivery,
        *,
        kind: ImpressionKind,
        source: ImpressionSource,
        dedupe_key: str,
        quantity: int = 1,
        telegram_user_id: int | None = None,
        session_hash: str | None = None,
        ip_hash: str | None = None,
        user_agent_hash: str | None = None,
        occurred_at=None,
        meta: dict | None = None,
        fraud_score: int = 0,
        fraud_reasons: list | None = None,
    ) -> RecordResult:
        """Record one impression event and decide whether it bills."""
        if quantity < 1:
            raise ValidationFailed("impression quantity must be at least 1")
        occurred_at = occurred_at or utcnow()
        channel = self.session.get(PublisherChannel, delivery.channel_id)
        if channel is None:  # pragma: no cover
            raise NotFound("channel not found")

        status, reason = self._validate(delivery, channel, kind, quantity, occurred_at, fraud_score)

        unit_cost = cpm_cost(1, delivery.effective_cpm)
        unit_publisher, _ = split_commission(unit_cost, delivery.commission_rate)

        impression = Impression(
            delivery_id=delivery.id,
            campaign_id=delivery.campaign_id,
            advertisement_id=delivery.advertisement_id,
            publisher_id=delivery.publisher_id,
            channel_id=delivery.channel_id,
            telegram_chat_id=delivery.telegram_chat_id,
            telegram_message_id=delivery.telegram_message_id,
            telegram_user_id=telegram_user_id,
            session_hash=session_hash,
            kind=kind,
            source=source,
            quantity=quantity,
            validation_status=status,
            billable=status.is_billable,
            fraud_score=fraud_score,
            fraud_reasons=fraud_reasons or [],
            unit_advertiser_cost=unit_cost if status.is_billable else ZERO,
            unit_publisher_revenue=unit_publisher if status.is_billable else ZERO,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
            recorded_at=utcnow(),
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            meta=meta or {},
        )
        try:
            # A SAVEPOINT, not a plain flush: hitting the unique index is the
            # expected path for a refreshed tracking link or a retried worker, and
            # a bare session.rollback() here would discard the caller's entire
            # unit of work along with it.
            with self.session.begin_nested():
                self.session.add(impression)
                self.session.flush()
        except IntegrityError:
            return RecordResult(
                None, False, ValidationStatus.DUPLICATE, "dedupe_key already recorded"
            )

        self._apply_counters(delivery, channel, impression)
        self.session.flush()
        return RecordResult(impression, True, status, reason)

    def _validate(
        self,
        delivery: AdDelivery,
        channel: PublisherChannel,
        kind: ImpressionKind,
        quantity: int,
        occurred_at,
        fraud_score: int,
    ) -> tuple[ValidationStatus, str]:
        if not kind.billable_by_default:
            return ValidationStatus.INVALIDATED, f"{kind.value} impressions are never billable"

        # A view-counter impression only bills where the channel is configured
        # for it — otherwise we'd be billing a number we cannot corroborate.
        if kind is ImpressionKind.TELEGRAM_REPORTED and channel.measurement_mode not in (
            MeasurementMode.VIEW_COUNTER,
            MeasurementMode.HYBRID,
        ):
            return (
                ValidationStatus.INVALIDATED,
                "channel measurement mode does not permit view-counter billing",
            )

        if delivery.status in (
            DeliveryStatus.CANCELLED,
            DeliveryStatus.REVERSED,
            DeliveryStatus.FAILED,
        ):
            return ValidationStatus.INVALIDATED, f"delivery is {delivery.status.value}"

        if delivery.sent_at is None:
            return ValidationStatus.OUT_OF_WINDOW, "delivery has not been sent"
        if occurred_at < delivery.sent_at:
            return ValidationStatus.OUT_OF_WINDOW, "impression predates the delivery"
        window_end = delivery.measurement_ends_at or (
            delivery.sent_at + timedelta(hours=self.settings.int_("impression_window_hours"))
        )
        if occurred_at > window_end:
            return ValidationStatus.OUT_OF_WINDOW, "outside the measurement window"

        if fraud_score >= self.settings.int_("fraud_block_threshold"):
            return ValidationStatus.FRAUDULENT, f"fraud score {fraud_score} exceeds block threshold"

        cap = delivery.impression_cap or self.compute_cap(delivery, channel)
        if cap and delivery.billable_impressions + quantity > cap:
            return (
                ValidationStatus.CAPPED,
                f"delivery cap of {cap} billable impressions reached",
            )
        return ValidationStatus.VALID, ""

    def _apply_counters(
        self, delivery: AdDelivery, channel: PublisherChannel, impression: Impression
    ) -> None:
        qty = impression.quantity
        if impression.kind is ImpressionKind.MEASURED:
            delivery.measured_impressions += qty
        elif impression.kind is ImpressionKind.TELEGRAM_REPORTED:
            delivery.reported_impressions += qty
        elif impression.kind is ImpressionKind.ESTIMATED:
            delivery.estimated_impressions += qty

        if impression.billable:
            delivery.billable_impressions += qty
            channel.total_impressions += qty
        else:
            delivery.invalid_impressions += qty

    # -- view-counter ingestion -------------------------------------------

    def ingest_view_observation(
        self, delivery: AdDelivery, cumulative_views: int, source_name: str
    ) -> RecordResult:
        """Bill only the positive delta above the high-water mark.

        Cumulative counters are not idempotent by nature: polling the same post
        twice reports the same total. Ratcheting on
        ``reported_views_high_water`` makes repeated polling free and makes a
        counter that drops (or is manipulated downward and back up) unable to
        bill the same views twice.
        """
        previous = int(delivery.reported_views_high_water or 0)
        delta = int(cumulative_views) - previous
        if delta <= 0:
            return RecordResult(
                None, False, ValidationStatus.DUPLICATE, "no new views above the high-water mark"
            )
        delivery.reported_views_high_water = int(cumulative_views)
        result = self.record(
            delivery,
            kind=ImpressionKind.TELEGRAM_REPORTED,
            source=ImpressionSource.VIEW_COUNTER,
            dedupe_key=f"view:{delivery.id}:{cumulative_views}",
            quantity=delta,
            meta={
                "source": source_name,
                "cumulative": int(cumulative_views),
                "previous_high_water": previous,
            },
        )
        if not result.accepted:
            # Roll the ratchet back so a genuine later poll is not silently lost.
            delivery.reported_views_high_water = previous
        self.session.flush()
        return result

    # -- estimation (never billable) --------------------------------------

    def record_estimate(self, delivery: AdDelivery, estimated: int) -> RecordResult:
        """Record an estimate for reporting. Explicitly non-billable (spec §37)."""
        return self.record(
            delivery,
            kind=ImpressionKind.ESTIMATED,
            source=ImpressionSource.ESTIMATOR,
            dedupe_key=f"est:{delivery.id}:{utcnow().date().isoformat()}",
            quantity=max(1, estimated),
            meta={"basis": "channel_avg_views", "billable": False},
        )

    # -- clicks ------------------------------------------------------------

    def record_click(
        self,
        delivery: AdDelivery,
        *,
        dedupe_key: str,
        telegram_user_id: int | None = None,
        ip_hash: str | None = None,
        user_agent_hash: str | None = None,
        referrer: str | None = None,
        fraud_score: int = 0,
        fraud_reasons: list | None = None,
        occurred_at=None,
    ) -> Click | None:
        """Record a click. Returns None when it was a duplicate."""
        occurred_at = occurred_at or utcnow()
        valid = fraud_score < self.settings.int_("fraud_block_threshold")
        click = Click(
            delivery_id=delivery.id,
            campaign_id=delivery.campaign_id,
            publisher_id=delivery.publisher_id,
            telegram_user_id=telegram_user_id,
            dedupe_key=dedupe_key,
            valid=valid,
            fraud_score=fraud_score,
            fraud_reasons=fraud_reasons or [],
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            referrer=(referrer or "")[:2000] or None,
            occurred_at=occurred_at,
        )
        try:
            with self.session.begin_nested():
                self.session.add(click)
                self.session.flush()
        except IntegrityError:
            return None  # duplicate click; already counted
        if valid:
            delivery.clicks += 1
            channel = self.session.get(PublisherChannel, delivery.channel_id)
            if channel is not None:
                channel.total_clicks += 1
        self.session.flush()
        return click

    # -- queries -----------------------------------------------------------

    def billable_count(self, delivery_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(Impression.quantity), 0)).where(
                    Impression.delivery_id == delivery_id,
                    Impression.billable.is_(True),
                )
            )
            or 0
        )

    def unsettled_billable(self, delivery_id: uuid.UUID) -> tuple[int, list[uuid.UUID]]:
        """Billable impressions not yet attached to a settlement batch."""
        rows = self.session.scalars(
            select(Impression).where(
                Impression.delivery_id == delivery_id,
                Impression.billable.is_(True),
                Impression.settlement_batch_id.is_(None),
            )
        ).all()
        return sum(r.quantity for r in rows), [r.id for r in rows]

    def invalidate(
        self, impression: Impression, status: ValidationStatus, reason: str
    ) -> Impression:
        """Retract an impression's billability without erasing the evidence."""
        if not impression.billable:
            return impression
        impression.billable = False
        impression.validation_status = status
        impression.fraud_reasons = [*(impression.fraud_reasons or []), reason]
        delivery = self.session.get(AdDelivery, impression.delivery_id)
        if delivery is not None:
            delivery.billable_impressions = max(
                0, delivery.billable_impressions - impression.quantity
            )
            delivery.invalid_impressions += impression.quantity
        self.session.flush()
        return impression

    # -- honest reporting --------------------------------------------------

    def delivery_report(self, delivery: AdDelivery) -> dict[str, object]:
        """Report the four impression kinds separately (spec §37)."""
        return {
            "billable_impressions": delivery.billable_impressions,
            "measured_impressions": delivery.measured_impressions,
            "telegram_reported_impressions": delivery.reported_impressions,
            "estimated_impressions": delivery.estimated_impressions,
            "invalidated_impressions": delivery.invalid_impressions,
            "clicks": delivery.clicks,
            "impression_cap": delivery.impression_cap,
            "note": (
                "Only billable impressions are charged. Estimated impressions are "
                "projections and are never billed."
            ),
        }
