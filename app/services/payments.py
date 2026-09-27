"""Deposits and payment providers (spec §26 idempotency, §14 wallet).

Bangladesh-relevant providers (bKash, Nagad, Rocket, bank transfer) all follow the
same shape: we create a pending deposit, the provider later confirms it with its
own transaction id, and we credit the wallet exactly once.

``deposits(provider, provider_transaction_id)`` is UNIQUE, so a callback replayed
by the provider — which they do, routinely — cannot credit twice. That constraint,
not application logic, is the guarantee.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.money import ZERO, q
from app.db.base import utcnow
from app.models.enums import DepositStatus
from app.models.identity import Advertiser
from app.models.money import Deposit
from app.services.audit import Actor, AuditService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


@dataclass(frozen=True)
class ProviderCharge:
    """What a provider tells us when it takes money."""

    provider: str
    provider_transaction_id: str
    amount: Decimal
    currency: str
    reference: str | None = None
    payload: dict | None = None


class PaymentProvider(Protocol):
    name: str

    def initiate(self, deposit: Deposit) -> dict: ...
    def verify_callback(self, payload: dict) -> ProviderCharge: ...


class ManualProvider:
    """Admin-recorded deposits (bank transfer, cash, manual bKash confirmation).

    The provider actually shipping today. A real gateway adapter implements the
    same interface; nothing downstream changes.
    """

    name = "manual"

    def initiate(self, deposit: Deposit) -> dict:
        return {
            "instructions": (
                "Send the amount to the platform account, then submit the "
                "transaction id for admin confirmation."
            ),
            "reference": str(deposit.id),
        }

    def verify_callback(self, payload: dict) -> ProviderCharge:
        required = {"provider_transaction_id", "amount", "currency"}
        missing = required - set(payload)
        if missing:
            raise ValidationFailed(f"missing fields: {sorted(missing)}")
        return ProviderCharge(
            provider=self.name,
            provider_transaction_id=str(payload["provider_transaction_id"]),
            amount=q(payload["amount"]),
            currency=str(payload["currency"]).upper(),
            reference=payload.get("reference"),
            payload=payload,
        )


class DepositService:
    def __init__(self, session: Session, provider: PaymentProvider | None = None) -> None:
        self.session = session
        self.provider = provider or ManualProvider()
        self.wallets = WalletService(session)
        self.settings = SettingsService(session)
        self.audit = AuditService(session)

    # -- create ------------------------------------------------------------

    def initiate(
        self, advertiser_id: uuid.UUID, amount: object, *, fee: object = "0"
    ) -> tuple[Deposit, dict]:
        advertiser = self.session.get(Advertiser, advertiser_id)
        if advertiser is None:
            raise NotFound("advertiser not found")
        amount = q(amount)
        fee = q(fee)
        if amount <= ZERO:
            raise ValidationFailed("deposit amount must be positive")
        if fee < ZERO or fee >= amount:
            raise ValidationFailed("fee must be non-negative and below the amount")

        deposit = Deposit(
            advertiser_id=advertiser_id,
            status=DepositStatus.PENDING,
            amount=amount,
            fee=fee,
            net_amount=q(amount - fee),
            currency=advertiser.currency,
            provider=self.provider.name,
        )
        self.session.add(deposit)
        self.session.flush()
        return deposit, self.provider.initiate(deposit)

    # -- confirm -----------------------------------------------------------

    def confirm(
        self,
        charge: ProviderCharge,
        *,
        deposit_id: uuid.UUID | None = None,
        actor: Actor | None = None,
    ) -> Deposit:
        """Credit the wallet exactly once for this provider transaction."""
        actor = actor or Actor.system("payment-callback")

        # Already processed? Return the original rather than crediting again.
        existing = self.session.scalars(
            select(Deposit).where(
                Deposit.provider == charge.provider,
                Deposit.provider_transaction_id == charge.provider_transaction_id,
            )
        ).one_or_none()
        if existing is not None and existing.status is DepositStatus.CONFIRMED:
            return existing

        deposit = existing
        if deposit is None and deposit_id is not None:
            deposit = self.session.get(Deposit, deposit_id)
        if deposit is None:
            raise NotFound("no deposit matches this callback")

        if deposit.currency != charge.currency:
            raise ValidationFailed(
                f"callback currency {charge.currency} does not match deposit "
                f"currency {deposit.currency}"
            )
        if q(charge.amount) != q(deposit.amount):
            raise ValidationFailed(
                f"callback amount {charge.amount} does not match the deposit "
                f"amount {q(deposit.amount)}"
            )

        old_status = deposit.status
        try:
            with self.session.begin_nested():
                deposit.provider_transaction_id = charge.provider_transaction_id
                deposit.provider_reference = charge.reference
                deposit.provider_payload = _safe_payload(charge.payload)
                self.session.flush()
        except IntegrityError:
            # Another deposit already owns this provider transaction id.
            winner = self.session.scalars(
                select(Deposit).where(
                    Deposit.provider == charge.provider,
                    Deposit.provider_transaction_id == charge.provider_transaction_id,
                )
            ).one_or_none()
            if winner is not None:
                return winner
            raise Conflict("provider transaction id already used") from None

        txn = self.wallets.credit_deposit(
            deposit.advertiser_id,
            deposit.net_amount,
            # Derived from the provider's own id, so the ledger refuses a replay
            # even if this method is somehow entered twice.
            idempotency_key=f"deposit:{charge.provider}:{charge.provider_transaction_id}",
            description=f"Deposit via {charge.provider}",
            reference_type="deposit",
            reference_id=str(deposit.id),
            actor_type=actor.type,
            actor_id=actor.id,
        )
        deposit.status = DepositStatus.CONFIRMED
        deposit.confirmed_at = utcnow()
        deposit.ledger_transaction_id = txn.id
        self.session.flush()

        self.audit.financial(
            actor,
            "deposit.confirmed",
            target_type="deposit",
            target_id=deposit.id,
            ledger_transaction_id=txn.id,
            old_value={"status": str(old_status)},
            new_value={"status": str(deposit.status), "net_amount": str(deposit.net_amount)},
        )
        return deposit

    def fail(self, deposit: Deposit, reason: str, actor: Actor | None = None) -> Deposit:
        if deposit.status is DepositStatus.CONFIRMED:
            raise Conflict("a confirmed deposit cannot be failed; refund it instead")
        deposit.status = DepositStatus.FAILED
        deposit.failure_reason = reason[:300]
        self.session.flush()
        self.audit.log(
            actor or Actor.system(),
            "deposit.failed",
            target_type="deposit",
            target_id=deposit.id,
            new_value={"reason": reason},
        )
        return deposit

    def list_for_advertiser(self, advertiser_id: uuid.UUID, limit: int = 50) -> list[Deposit]:
        return list(
            self.session.scalars(
                select(Deposit)
                .where(Deposit.advertiser_id == advertiser_id)
                .order_by(Deposit.created_at.desc())
                .limit(limit)
            ).all()
        )


def _safe_payload(payload: dict | None) -> dict:
    """Strip anything that looks like a provider secret before persisting."""
    if not payload:
        return {}
    blocked = {"signature", "token", "secret", "password", "api_key", "apikey", "pin"}
    return {
        key: (str(value)[:500] if not isinstance(value, (int, bool)) else value)
        for key, value in payload.items()
        if key.lower() not in blocked
    }
