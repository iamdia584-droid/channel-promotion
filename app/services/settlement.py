"""Settlement: turning measured impressions into money (spec §9 steps 10-13, §11).

One atomic ledger posting per batch, with four legs:

    debit  ADVERTISER_RESERVED   gross
    credit ADVERTISER_SPENT      gross      (recognised spend)
    debit  ADVERTISER_SPENT      gross      (immediately distributed)
    credit PUBLISHER_PENDING     publisher share
    credit PLATFORM_REVENUE      platform share

Because it is one posting, publisher earnings and platform revenue can never
disagree with what the advertiser was charged. The batch id is derived from the
delivery and the impression set, so a retried worker settles the same
impressions exactly once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.money import ZERO, D, cpm_cost, q, split_commission
from app.db.base import utcnow
from app.models.campaigns import Campaign
from app.models.delivery import AdDelivery, Impression
from app.models.enums import (
    AccountKind,
    CampaignStatus,
    DeliveryStatus,
    EarningStatus,
    TransactionType,
)
from app.models.money import PublisherEarning
from app.models.telegram import PublisherChannel
from app.services.ledger import LedgerService, credit, debit
from app.services.pacing import PacingService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


@dataclass(frozen=True)
class SettlementResult:
    settled: bool
    batch_id: uuid.UUID | None
    impressions: int
    gross: Decimal
    publisher_amount: Decimal
    platform_amount: Decimal
    reason: str = ""


class SettlementService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ledger = LedgerService(session)
        self.wallets = WalletService(session)
        self.pacing = PacingService(session)
        self.settings = SettingsService(session)

    # ------------------------------------------------------------------

    def settle_delivery(self, delivery: AdDelivery) -> SettlementResult:
        """Settle every unsettled billable impression on this delivery."""
        if delivery.status in (DeliveryStatus.CANCELLED, DeliveryStatus.FAILED):
            return SettlementResult(False, None, 0, ZERO, ZERO, ZERO,
                                    f"delivery is {delivery.status.value}")

        rows = self.session.scalars(
            select(Impression).where(
                Impression.delivery_id == delivery.id,
                Impression.billable.is_(True),
                Impression.settlement_batch_id.is_(None),
            ).order_by(Impression.occurred_at)
        ).all()
        impressions = sum(r.quantity for r in rows)
        if impressions <= 0:
            # Nothing measured. Release the reservation rather than charging for
            # a post nobody provably saw.
            return self._close_unbilled(delivery)

        campaign = self.session.get(Campaign, delivery.campaign_id)
        channel = self.session.get(PublisherChannel, delivery.channel_id)
        if campaign is None or channel is None:  # pragma: no cover
            raise ValidationFailed("delivery references a missing campaign or channel")

        gross = cpm_cost(impressions, delivery.effective_cpm)
        # Never settle beyond what was reserved: the reservation is the advertiser's
        # exposure ceiling for this delivery.
        available = q(D(delivery.reserved_amount) - D(delivery.settled_amount))
        if gross > available:
            gross = available
        if gross <= ZERO:
            return SettlementResult(False, None, 0, ZERO, ZERO, ZERO,
                                    "no reserved budget left on this delivery")

        publisher_amount, platform_amount = split_commission(
            gross, delivery.commission_rate
        )
        # Deterministic batch id: the same impression set always yields the same
        # batch, so a retry is caught by the ledger's idempotency key.
        batch_id = uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"settle:{delivery.id}:" + ",".join(sorted(str(r.id) for r in rows)),
        )

        # The advertiser's reserved prepayment is earned: our liability to them
        # falls by the gross, and that exact gross becomes a liability to the
        # publisher plus our revenue. One posting, so the three figures cannot
        # disagree.
        legs = [debit(AccountKind.ADVERTISER_RESERVED, gross, campaign.advertiser_id)]
        if publisher_amount > ZERO:
            legs.append(
                credit(AccountKind.PUBLISHER_PENDING, publisher_amount, delivery.publisher_id)
            )
        if platform_amount > ZERO:
            legs.append(credit(AccountKind.PLATFORM_REVENUE, platform_amount))

        result = self.ledger.post(
            transaction_type=TransactionType.SETTLEMENT,
            currency=delivery.currency,
            legs=legs,
            idempotency_key=f"settlement:{batch_id}",
            description=(
                f"Settled {impressions} billable impressions at "
                f"{delivery.effective_cpm} CPM"
            ),
            advertiser_id=campaign.advertiser_id,
            publisher_id=delivery.publisher_id,
            campaign_id=campaign.id,
            delivery_id=delivery.id,
            settlement_batch_id=batch_id,
        )
        if result.replayed:
            return SettlementResult(
                False, batch_id, impressions, gross, publisher_amount, platform_amount,
                "already settled",
            )

        # Stamp the impressions so they cannot be settled again.
        self.session.execute(
            update(Impression)
            .where(Impression.id.in_([r.id for r in rows]))
            .values(settlement_batch_id=batch_id)
        )

        # Projections, all inside this same DB transaction.
        self.wallets.apply_spend(campaign.advertiser_id, gross)
        if publisher_amount > ZERO:
            self.wallets.apply_publisher_pending(delivery.publisher_id, publisher_amount)

        delivery.settled_amount = q(D(delivery.settled_amount) + gross)
        delivery.settled_at = utcnow()
        # Still inside the measurement window: more impressions may yet arrive, so
        # the delivery stays in MEASURING and will settle again.
        delivery.status = (
            DeliveryStatus.SETTLED
            if (
                delivery.measurement_ends_at is None
                or utcnow() >= delivery.measurement_ends_at
            )
            else DeliveryStatus.MEASURING
        )

        campaign.spent_amount = q(D(campaign.spent_amount) + gross)
        campaign.reserved_amount = max(ZERO, q(D(campaign.reserved_amount) - gross))
        campaign.billable_impressions += impressions
        campaign.clicks += delivery.clicks

        channel.lifetime_earned = q(D(channel.lifetime_earned) + publisher_amount)
        self._update_ad_view_average(channel, delivery, impressions)

        self.pacing.commit_spend(
            campaign, delivery.reserved_amount, gross,
            impressions=impressions, clicks=delivery.clicks,
            at=delivery.sent_at or utcnow(),
        )

        earning = PublisherEarning(
            publisher_id=delivery.publisher_id,
            channel_id=delivery.channel_id,
            delivery_id=delivery.id,
            campaign_id=campaign.id,
            settlement_batch_id=batch_id,
            status=EarningStatus.PENDING,
            billable_impressions=impressions,
            publisher_cpm=q(delivery.publisher_cpm),
            gross_amount=gross,
            platform_commission=platform_amount,
            net_amount=publisher_amount,
            currency=delivery.currency,
            confirm_after=utcnow()
            + timedelta(hours=self.settings.int_("earnings_validation_hours")),
            fraud_score=delivery.fraud_score,
            created_at=utcnow(),
        )
        self.session.add(earning)

        # Release any part of the reservation the delivery did not consume — but
        # only once the measurement window has closed. Releasing it while the
        # window is still open would leave later impressions unfundable, so the
        # publisher would lose earnings they had genuinely generated.
        window_closed = (
            delivery.measurement_ends_at is None
            or utcnow() >= delivery.measurement_ends_at
        )
        unused = q(D(delivery.reserved_amount) - D(delivery.settled_amount))
        if unused > ZERO and window_closed:
            self.wallets.release_budget(
                campaign.advertiser_id, campaign.id, unused,
                idempotency_key=f"delivery-unused:{delivery.id}",
                description="Unused delivery reservation returned",
            )
            campaign.reserved_amount = max(ZERO, q(D(campaign.reserved_amount) - unused))
            delivery.reserved_amount = q(delivery.settled_amount)

        self._complete_if_exhausted(campaign)
        self.session.flush()
        return SettlementResult(
            True, batch_id, impressions, gross, publisher_amount, platform_amount
        )

    # ------------------------------------------------------------------

    def _close_unbilled(self, delivery: AdDelivery) -> SettlementResult:
        """No measurable impressions: refund the reservation in full."""
        amount = q(D(delivery.reserved_amount) - D(delivery.settled_amount))
        campaign = self.session.get(Campaign, delivery.campaign_id)
        if campaign is not None and amount > ZERO:
            self.wallets.release_budget(
                campaign.advertiser_id, campaign.id, amount,
                idempotency_key=f"delivery-unmeasured:{delivery.id}",
                description="No measurable impressions; reservation returned",
            )
            campaign.reserved_amount = max(ZERO, q(D(campaign.reserved_amount) - amount))
            self.pacing.release(campaign, amount, delivery.sent_at or utcnow())
            delivery.reserved_amount = q(delivery.settled_amount)
        delivery.status = DeliveryStatus.SETTLED
        delivery.settled_at = utcnow()
        self.session.flush()
        return SettlementResult(
            False, None, 0, ZERO, ZERO, ZERO, "no billable impressions to settle"
        )

    def _update_ad_view_average(
        self, channel: PublisherChannel, delivery: AdDelivery, impressions: int
    ) -> None:
        """Rolling average of *ad* reach — the honest basis for future pricing."""
        served = max(1, channel.total_ads_served)
        previous = channel.avg_ad_views
        channel.avg_ad_views = int((previous * (served - 1) + impressions) / served)

    def _complete_if_exhausted(self, campaign: Campaign) -> None:
        now = utcnow()
        if q(campaign.remaining_budget) <= ZERO or campaign.ends_at <= now:
            if campaign.status is CampaignStatus.RUNNING:
                campaign.status = CampaignStatus.COMPLETED
                campaign.completed_at = now

    # ------------------------------------------------------------------

    def settle_due(self, limit: int = 200, now=None) -> list[SettlementResult]:
        """Settle deliveries whose measurement window has closed."""
        now = now or utcnow()
        rows = self.session.scalars(
            select(AdDelivery)
            .where(
                AdDelivery.status.in_([DeliveryStatus.SENT, DeliveryStatus.MEASURING]),
                AdDelivery.measurement_ends_at.is_not(None),
                AdDelivery.measurement_ends_at <= now,
            )
            .order_by(AdDelivery.measurement_ends_at)
            .limit(limit)
        ).all()
        return [self.settle_delivery(d) for d in rows]
