"""Publisher earnings lifecycle (spec §12).

    impression → PENDING → (fraud validation) → CONFIRMED → withdrawable
                        ↘ REVERSED

Earnings sit in ``PUBLISHER_PENDING`` for a configurable validation period before
moving to ``PUBLISHER_CONFIRMED``. Only confirmed balance can be withdrawn, so a
fraudulent impression discovered on day two costs the platform nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.core.money import ZERO, D, q
from app.db.base import utcnow
from app.models.enums import (
    AccountKind,
    EarningStatus,
    TransactionType,
    ValidationStatus,
)
from app.models.money import PublisherEarning
from app.services.ledger import LedgerService, credit, debit
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


@dataclass(frozen=True)
class ConfirmationBatch:
    confirmed: int
    held: int
    amount: Decimal


class EarningsService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ledger = LedgerService(session)
        self.wallets = WalletService(session)
        self.settings = SettingsService(session)

    # -- confirmation ------------------------------------------------------

    def confirm(self, earning: PublisherEarning) -> bool:
        """Move one earning pending → confirmed. Idempotent."""
        if earning.status is EarningStatus.CONFIRMED:
            return False
        if earning.status is not EarningStatus.PENDING:
            raise Conflict(f"cannot confirm an earning that is {earning.status.value}")
        if q(earning.net_amount) <= ZERO:
            earning.status = EarningStatus.CONFIRMED
            earning.confirmed_at = utcnow()
            self.session.flush()
            return True

        result = self.ledger.post(
            transaction_type=TransactionType.EARNING_CONFIRM,
            currency=earning.currency,
            legs=[
                debit(AccountKind.PUBLISHER_PENDING, earning.net_amount, earning.publisher_id),
                credit(AccountKind.PUBLISHER_CONFIRMED, earning.net_amount, earning.publisher_id),
            ],
            idempotency_key=f"earning-confirm:{earning.id}",
            description=(f"Confirmed earnings for {earning.billable_impressions} impressions"),
            publisher_id=earning.publisher_id,
            campaign_id=earning.campaign_id,
            delivery_id=earning.delivery_id,
            settlement_batch_id=earning.settlement_batch_id,
        )
        if not result.replayed:
            self.wallets.apply_publisher_confirm(earning.publisher_id, earning.net_amount)
        earning.status = EarningStatus.CONFIRMED
        earning.confirmed_at = utcnow()
        self.session.flush()
        return True

    def confirm_due(self, limit: int = 500, now=None) -> ConfirmationBatch:
        """Confirm every pending earning past its validation window.

        An earning whose fraud score has since risen above the hold threshold is
        left pending for a human rather than confirmed on a timer.
        """
        now = now or utcnow()
        hold_threshold = self.settings.int_("fraud_hold_earnings_threshold")
        rows = self.session.scalars(
            select(PublisherEarning)
            .where(
                PublisherEarning.status == EarningStatus.PENDING,
                PublisherEarning.confirm_after <= now,
            )
            .order_by(PublisherEarning.confirm_after)
            .limit(limit)
        ).all()

        confirmed = held = 0
        total = ZERO
        for earning in rows:
            if earning.fraud_score >= hold_threshold:
                held += 1
                continue
            if self.confirm(earning):
                confirmed += 1
                total = q(total + D(earning.net_amount))
        return ConfirmationBatch(confirmed, held, total)

    # -- reversal ----------------------------------------------------------

    def reverse(self, earning: PublisherEarning, reason: str) -> bool:
        """Claw back a pending earning found fraudulent.

        Only PENDING earnings can be reversed this way. Once confirmed, the
        publisher has a withdrawable claim; unwinding that is a separate, explicit
        admin adjustment with its own audit trail.
        """
        if earning.status is EarningStatus.REVERSED:
            return False
        if earning.status is not EarningStatus.PENDING:
            raise Conflict(
                f"only pending earnings can be reversed; this one is {earning.status.value}"
            )

        amount = q(earning.net_amount)
        if amount > ZERO:
            self.ledger.post(
                transaction_type=TransactionType.EARNING_REVERSAL,
                currency=earning.currency,
                legs=[
                    debit(AccountKind.PUBLISHER_PENDING, amount, earning.publisher_id),
                    credit(AccountKind.FRAUD_CLAWBACK, amount),
                ],
                idempotency_key=f"earning-reverse:{earning.id}",
                description=f"Reversed earnings: {reason}",
                publisher_id=earning.publisher_id,
                campaign_id=earning.campaign_id,
                delivery_id=earning.delivery_id,
                meta={"reason": reason},
                allow_negative=True,
            )
            self.wallets.apply_publisher_reversal(earning.publisher_id, amount)

        earning.status = EarningStatus.REVERSED
        earning.reversed_at = utcnow()
        earning.reversal_reason = reason[:300]

        # Retract the underlying impressions' billability so reports and the
        # ledger tell the same story.
        from app.models.delivery import Impression
        from app.services.impressions import ImpressionService

        impressions = self.session.scalars(
            select(Impression).where(Impression.settlement_batch_id == earning.settlement_batch_id)
        ).all()
        service = ImpressionService(self.session)
        for impression in impressions:
            service.invalidate(impression, ValidationStatus.FRAUDULENT, reason)

        self.session.flush()
        return True

    def hold(self, earning: PublisherEarning, extra_hours: int, reason: str) -> PublisherEarning:
        """Extend the validation window on a suspicious earning."""
        from datetime import timedelta

        earning.confirm_after = earning.confirm_after + timedelta(hours=extra_hours)
        earning.reversal_reason = f"hold: {reason}"[:300]
        self.session.flush()
        return earning

    # -- reads -------------------------------------------------------------

    def summary(self, publisher_id: uuid.UUID) -> dict[str, object]:
        def total(status: EarningStatus) -> Decimal:
            return q(
                self.session.scalar(
                    select(func.coalesce(func.sum(PublisherEarning.net_amount), 0)).where(
                        PublisherEarning.publisher_id == publisher_id,
                        PublisherEarning.status == status,
                    )
                )
                or 0
            )

        impressions = int(
            self.session.scalar(
                select(func.coalesce(func.sum(PublisherEarning.billable_impressions), 0)).where(
                    PublisherEarning.publisher_id == publisher_id
                )
            )
            or 0
        )
        pending, confirmed = total(EarningStatus.PENDING), total(EarningStatus.CONFIRMED)
        paid, reversed_ = total(EarningStatus.PAID), total(EarningStatus.REVERSED)
        earned = q(pending + confirmed + paid)
        return {
            "pending": pending,
            "confirmed": confirmed,
            "paid": paid,
            "reversed": reversed_,
            "total_earned": earned,
            "billable_impressions": impressions,
            "effective_cpm": q(earned * 1000 / D(impressions)) if impressions else ZERO,
        }

    def list_for_publisher(
        self, publisher_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[PublisherEarning]:
        return list(
            self.session.scalars(
                select(PublisherEarning)
                .where(PublisherEarning.publisher_id == publisher_id)
                .order_by(PublisherEarning.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def get(self, earning_id: uuid.UUID) -> PublisherEarning:
        earning = self.session.get(PublisherEarning, earning_id)
        if earning is None:
            raise NotFound("earning not found")
        return earning
