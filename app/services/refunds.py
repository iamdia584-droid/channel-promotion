"""Campaign cancellation and advertiser refunds (spec §35).

The refundable amount is the *unspent reservation*, never the whole budget:

    refundable = reserved − committed to deliveries still being measured

Spec §35's example: ৳10,000 budget, ৳3,500 spent → ৳6,500 refundable. Deliveries
whose measurement window is still open are excluded, because their impressions may
yet settle and the publisher has already earned that inventory.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.money import ZERO, D, q
from app.db.base import utcnow
from app.models.campaigns import Campaign
from app.models.delivery import AdDelivery
from app.models.enums import (
    AccountKind,
    CampaignStatus,
    DeliveryStatus,
    RefundStatus,
    TransactionType,
)
from app.models.money import Refund
from app.services.audit import Actor, AuditService
from app.services.ledger import LedgerService, credit, debit
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


@dataclass(frozen=True)
class RefundQuote:
    reserved: Decimal
    spent: Decimal
    in_flight: Decimal
    refundable: Decimal
    currency: str

    def explain(self) -> dict[str, str]:
        return {
            "reserved": str(self.reserved),
            "spent": str(self.spent),
            "in_flight_awaiting_measurement": str(self.in_flight),
            "refundable_now": str(self.refundable),
            "currency": self.currency,
        }


class RefundService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ledger = LedgerService(session)
        self.wallets = WalletService(session)
        self.settings = SettingsService(session)
        self.audit = AuditService(session)

    # -- quoting -----------------------------------------------------------

    def quote(self, campaign: Campaign) -> RefundQuote:
        in_flight = q(
            self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(AdDelivery.reserved_amount - AdDelivery.settled_amount), 0
                    )
                ).where(
                    AdDelivery.campaign_id == campaign.id,
                    AdDelivery.status.in_(
                        [
                            DeliveryStatus.RESERVED,
                            DeliveryStatus.SENT,
                            DeliveryStatus.MEASURING,
                        ]
                    ),
                )
            )
            or 0
        )
        reserved = q(campaign.reserved_amount)
        refundable = max(ZERO, q(reserved - in_flight))
        return RefundQuote(
            reserved=reserved,
            spent=q(campaign.spent_amount),
            in_flight=in_flight,
            refundable=refundable,
            currency=campaign.currency,
        )

    # -- requests ----------------------------------------------------------

    def request(
        self,
        campaign: Campaign,
        *,
        amount: object | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
        actor: Actor | None = None,
    ) -> Refund:
        quote = self.quote(campaign)
        requested = q(amount) if amount is not None else quote.refundable
        if requested <= ZERO:
            raise ValidationFailed(
                "there is nothing refundable on this campaign",
                **quote.explain(),
            )
        if requested > quote.refundable:
            raise ValidationFailed(
                f"only {quote.refundable} is refundable right now",
                **quote.explain(),
            )

        idempotency_key = idempotency_key or f"refund:{campaign.id}:{uuid.uuid4()}"
        existing = self.session.scalars(
            select(Refund).where(Refund.idempotency_key == idempotency_key)
        ).one_or_none()
        if existing is not None:
            return existing

        refund = Refund(
            advertiser_id=campaign.advertiser_id,
            campaign_id=campaign.id,
            status=RefundStatus.REQUESTED,
            requested_amount=requested,
            currency=campaign.currency,
            reason=(reason or "")[:500] or None,
            to_wallet=True,
            idempotency_key=idempotency_key,
        )
        self.session.add(refund)
        self.session.flush()
        self.audit.log(
            actor or Actor.system("advertiser-request"), "refund.requested",
            target_type="refund", target_id=refund.id,
            new_value={"requested": str(requested), "campaign_id": str(campaign.id)},
            reason=reason,
        )
        return refund

    def approve(
        self,
        refund: Refund,
        actor: Actor,
        *,
        amount: object | None = None,
        note: str | None = None,
    ) -> Refund:
        """Move the approved amount back from reserved to available."""
        if refund.status is not RefundStatus.REQUESTED:
            raise Conflict(f"cannot approve a {refund.status.value} refund")
        campaign = self.session.get(Campaign, refund.campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")

        approved = q(amount) if amount is not None else q(refund.requested_amount)
        quote = self.quote(campaign)
        if approved <= ZERO:
            raise ValidationFailed("the approved amount must be positive")
        if approved > quote.refundable:
            raise ValidationFailed(
                f"only {quote.refundable} is refundable right now", **quote.explain()
            )

        result = self.ledger.post(
            transaction_type=TransactionType.REFUND,
            currency=refund.currency,
            legs=[
                debit(AccountKind.ADVERTISER_RESERVED, approved, refund.advertiser_id),
                credit(AccountKind.ADVERTISER_AVAILABLE, approved, refund.advertiser_id),
            ],
            idempotency_key=f"refund:{refund.id}",
            description=f"Refund of unused campaign budget: {refund.reason or 'requested'}",
            advertiser_id=refund.advertiser_id,
            campaign_id=campaign.id,
            reference_type="refund",
            reference_id=str(refund.id),
            actor_type=actor.type,
            actor_id=actor.id,
        )
        if not result.replayed:
            wallet = self.wallets.locked(
                self.wallets.for_advertiser(refund.advertiser_id).id
            )
            wallet.reserved_balance = q(D(wallet.reserved_balance) - approved)
            wallet.available_balance = q(D(wallet.available_balance) + approved)
            wallet.refunded_total = q(D(wallet.refunded_total) + approved)
            wallet.version += 1
            campaign.reserved_amount = max(ZERO, q(D(campaign.reserved_amount) - approved))
            campaign.refunded_amount = q(D(campaign.refunded_amount) + approved)

        refund.status = RefundStatus.PROCESSED
        refund.approved_amount = approved
        refund.decided_at = utcnow()
        refund.decided_by_staff_id = _staff_id(actor)
        refund.decision_note = (note or "")[:500] or None
        refund.ledger_transaction_id = result.id
        self.session.flush()

        self.audit.financial(
            actor, "refund.approved", target_type="refund", target_id=refund.id,
            ledger_transaction_id=result.id,
            old_value={"status": "requested"},
            new_value={"status": "processed", "approved": str(approved)},
            reason=note,
        )
        return refund

    def reject(self, refund: Refund, actor: Actor, note: str) -> Refund:
        if refund.status is not RefundStatus.REQUESTED:
            raise Conflict(f"cannot reject a {refund.status.value} refund")
        if not note.strip():
            raise ValidationFailed("a rejection note is required")
        refund.status = RefundStatus.REJECTED
        refund.decided_at = utcnow()
        refund.decided_by_staff_id = _staff_id(actor)
        refund.decision_note = note[:500]
        self.session.flush()
        self.audit.log(
            actor, "refund.rejected", target_type="refund", target_id=refund.id,
            new_value={"status": "rejected"}, reason=note,
        )
        return refund

    # -- campaign cancellation --------------------------------------------

    def cancel_campaign(
        self, campaign: Campaign, actor: Actor, reason: str = "cancelled by advertiser"
    ) -> tuple[Campaign, Refund | None]:
        """Stop a campaign and return its unspent reservation automatically."""
        if campaign.status.is_terminal:
            raise Conflict(f"campaign is already {campaign.status.value}")

        old_status = campaign.status
        campaign.status = CampaignStatus.CANCELLED
        campaign.completed_at = utcnow()
        campaign.paused_reason = reason[:300]
        self.session.flush()

        quote = self.quote(campaign)
        refund: Refund | None = None
        if quote.refundable > ZERO:
            refund = self.request(
                campaign, amount=quote.refundable, reason=reason,
                idempotency_key=f"refund:cancel:{campaign.id}", actor=actor,
            )
            # Auto-approve: this is the advertiser's own unspent money.
            refund = self.approve(refund, actor, note="automatic on cancellation")

        self.audit.log(
            actor, "campaign.cancelled", target_type="campaign", target_id=campaign.id,
            old_value={"status": str(old_status)},
            new_value={
                "status": "cancelled",
                "refunded": str(refund.approved_amount) if refund else "0",
                "in_flight_retained": str(quote.in_flight),
            },
            reason=reason,
        )
        return campaign, refund

    def pending_queue(self, limit: int = 100) -> list[Refund]:
        return list(
            self.session.scalars(
                select(Refund)
                .where(Refund.status == RefundStatus.REQUESTED)
                .order_by(Refund.created_at)
                .limit(limit)
            ).all()
        )


def _staff_id(actor: Actor):
    if actor.type != "staff" or not actor.id:
        return None
    try:
        return uuid.UUID(actor.id)
    except (ValueError, TypeError):  # pragma: no cover
        return None
