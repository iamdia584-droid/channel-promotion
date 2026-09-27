"""Wallet operations (spec §14).

The wallet is a *projection* of the ledger, maintained inside the same database
transaction as the ledger post. Nothing here changes a balance directly: every
method builds legs and hands them to :class:`LedgerService`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.errors import InsufficientFunds, NotFound, ValidationFailed
from app.core.money import ZERO, D, q
from app.db.base import utcnow
from app.models.enums import AccountKind, TransactionStatus, TransactionType
from app.models.identity import Advertiser, Publisher
from app.models.money import LedgerTransaction, Wallet, WalletTransaction
from app.services.ledger import LedgerService, credit, debit


@dataclass(frozen=True)
class WalletView:
    """Read model for the bot and API."""

    currency: str
    available: Decimal
    reserved: Decimal
    spent: Decimal
    deposited: Decimal
    refunded: Decimal
    pending: Decimal
    confirmed: Decimal
    earned: Decimal
    withdrawn: Decimal

    @property
    def total_held(self) -> Decimal:
        return q(self.available + self.reserved)

    @property
    def withdrawable(self) -> Decimal:
        return self.confirmed


class WalletService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ledger = LedgerService(session)

    # -- lookup ------------------------------------------------------------

    def for_advertiser(self, advertiser_id: uuid.UUID, *, create: bool = True) -> Wallet:
        wallet = self.session.scalars(
            select(Wallet).where(Wallet.advertiser_id == advertiser_id)
        ).one_or_none()
        if wallet is not None:
            return wallet
        if not create:
            raise NotFound("advertiser wallet not found")
        advertiser = self.session.get(Advertiser, advertiser_id)
        if advertiser is None:
            raise NotFound("advertiser not found")
        wallet = Wallet(advertiser_id=advertiser_id, currency=advertiser.currency)
        self.session.add(wallet)
        self.session.flush()
        return wallet

    def for_publisher(self, publisher_id: uuid.UUID, *, create: bool = True) -> Wallet:
        wallet = self.session.scalars(
            select(Wallet).where(Wallet.publisher_id == publisher_id)
        ).one_or_none()
        if wallet is not None:
            return wallet
        if not create:
            raise NotFound("publisher wallet not found")
        publisher = self.session.get(Publisher, publisher_id)
        if publisher is None:
            raise NotFound("publisher not found")
        wallet = Wallet(publisher_id=publisher_id, currency=publisher.currency)
        self.session.add(wallet)
        self.session.flush()
        return wallet

    def locked(self, wallet_id: uuid.UUID) -> Wallet:
        """Re-read a wallet under a row lock. Call before any balance change."""
        stmt = select(Wallet).where(Wallet.id == wallet_id)
        if self.session.get_bind().dialect.name != "sqlite":
            stmt = stmt.with_for_update()
        wallet = self.session.scalars(stmt).one_or_none()
        if wallet is None:
            raise NotFound("wallet not found")
        return wallet

    def view(self, wallet: Wallet) -> WalletView:
        return WalletView(
            currency=wallet.currency,
            available=q(wallet.available_balance),
            reserved=q(wallet.reserved_balance),
            spent=q(wallet.spent_total),
            deposited=q(wallet.deposited_total),
            refunded=q(wallet.refunded_total),
            pending=q(wallet.pending_balance),
            confirmed=q(wallet.confirmed_balance),
            earned=q(wallet.earned_total),
            withdrawn=q(wallet.withdrawn_total),
        )

    # -- advertiser money in ----------------------------------------------

    def credit_deposit(
        self,
        advertiser_id: uuid.UUID,
        amount: object,
        *,
        idempotency_key: str,
        description: str = "Wallet deposit",
        reference_type: str | None = None,
        reference_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> LedgerTransaction:
        """Money arrives from a payment provider and becomes spendable."""
        amount = q(amount)
        if amount <= ZERO:
            raise ValidationFailed("deposit amount must be positive")
        wallet = self.locked(self.for_advertiser(advertiser_id).id)

        result = self.ledger.post(
            transaction_type=TransactionType.DEPOSIT,
            currency=wallet.currency,
            legs=[
                debit(AccountKind.GATEWAY_CLEARING, amount),
                credit(AccountKind.ADVERTISER_AVAILABLE, amount, advertiser_id),
            ],
            idempotency_key=idempotency_key,
            description=description,
            advertiser_id=advertiser_id,
            reference_type=reference_type,
            reference_id=reference_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        if result.replayed:
            # A duplicate callback. The first post already moved the money.
            return result.transaction

        wallet.available_balance = q(D(wallet.available_balance) + amount)
        wallet.deposited_total = q(D(wallet.deposited_total) + amount)
        wallet.version += 1
        advertiser = self.session.get(Advertiser, advertiser_id)
        if advertiser is not None:
            advertiser.lifetime_deposited = q(D(advertiser.lifetime_deposited) + amount)

        self._statement(
            wallet, result.transaction, amount, "available", description
        )
        self.session.flush()
        return result.transaction

    # -- campaign budget --------------------------------------------------

    def reserve_budget(
        self,
        advertiser_id: uuid.UUID,
        campaign_id: uuid.UUID,
        amount: object,
        *,
        idempotency_key: str,
        description: str = "Campaign budget reserved",
    ) -> LedgerTransaction:
        """Move available → reserved. Refuses to over-commit (spec §14)."""
        amount = q(amount)
        if amount <= ZERO:
            raise ValidationFailed("reservation must be positive")
        wallet = self.locked(self.for_advertiser(advertiser_id).id)
        if q(wallet.available_balance) < amount:
            raise InsufficientFunds(
                "insufficient available balance to reserve this budget",
                available=str(q(wallet.available_balance)),
                required=str(amount),
                currency=wallet.currency,
            )

        result = self.ledger.post(
            transaction_type=TransactionType.BUDGET_RESERVE,
            currency=wallet.currency,
            legs=[
                debit(AccountKind.ADVERTISER_AVAILABLE, amount, advertiser_id),
                credit(AccountKind.ADVERTISER_RESERVED, amount, advertiser_id),
            ],
            idempotency_key=idempotency_key,
            description=description,
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
        )
        if result.replayed:
            return result.transaction

        wallet.available_balance = q(D(wallet.available_balance) - amount)
        wallet.reserved_balance = q(D(wallet.reserved_balance) + amount)
        wallet.version += 1
        self._statement(
            wallet, result.transaction, -amount, "available", description,
            campaign_id=campaign_id,
        )
        self.session.flush()
        return result.transaction

    def release_budget(
        self,
        advertiser_id: uuid.UUID,
        campaign_id: uuid.UUID,
        amount: object,
        *,
        idempotency_key: str,
        description: str = "Campaign budget released",
    ) -> LedgerTransaction:
        """Move reserved → available (pause, cancel, or unspent tail)."""
        amount = q(amount)
        if amount <= ZERO:
            raise ValidationFailed("release must be positive")
        wallet = self.locked(self.for_advertiser(advertiser_id).id)
        if q(wallet.reserved_balance) < amount:
            raise InsufficientFunds(
                "cannot release more than is reserved",
                reserved=str(q(wallet.reserved_balance)),
                requested=str(amount),
            )

        result = self.ledger.post(
            transaction_type=TransactionType.BUDGET_RELEASE,
            currency=wallet.currency,
            legs=[
                debit(AccountKind.ADVERTISER_RESERVED, amount, advertiser_id),
                credit(AccountKind.ADVERTISER_AVAILABLE, amount, advertiser_id),
            ],
            idempotency_key=idempotency_key,
            description=description,
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
        )
        if result.replayed:
            return result.transaction

        wallet.reserved_balance = q(D(wallet.reserved_balance) - amount)
        wallet.available_balance = q(D(wallet.available_balance) + amount)
        wallet.version += 1
        self._statement(
            wallet, result.transaction, amount, "available", description,
            campaign_id=campaign_id,
        )
        self.session.flush()
        return result.transaction

    # -- projections used by settlement -----------------------------------

    def apply_spend(self, advertiser_id: uuid.UUID, amount: object) -> None:
        """Update the advertiser projection after a settlement post.

        Called by :class:`~app.services.settlement.SettlementService` *inside* the
        same transaction as the four-leg settlement posting; it does not post to
        the ledger itself.
        """
        amount = q(amount)
        wallet = self.locked(self.for_advertiser(advertiser_id).id)
        wallet.reserved_balance = q(D(wallet.reserved_balance) - amount)
        wallet.spent_total = q(D(wallet.spent_total) + amount)
        wallet.version += 1
        advertiser = self.session.get(Advertiser, advertiser_id)
        if advertiser is not None:
            advertiser.lifetime_spent = q(D(advertiser.lifetime_spent) + amount)

    def apply_publisher_pending(self, publisher_id: uuid.UUID, amount: object) -> None:
        amount = q(amount)
        wallet = self.locked(self.for_publisher(publisher_id).id)
        wallet.pending_balance = q(D(wallet.pending_balance) + amount)
        wallet.earned_total = q(D(wallet.earned_total) + amount)
        wallet.version += 1
        publisher = self.session.get(Publisher, publisher_id)
        if publisher is not None:
            publisher.lifetime_earned = q(D(publisher.lifetime_earned) + amount)

    def apply_publisher_confirm(self, publisher_id: uuid.UUID, amount: object) -> None:
        amount = q(amount)
        wallet = self.locked(self.for_publisher(publisher_id).id)
        wallet.pending_balance = q(D(wallet.pending_balance) - amount)
        wallet.confirmed_balance = q(D(wallet.confirmed_balance) + amount)
        wallet.version += 1

    def apply_publisher_reversal(self, publisher_id: uuid.UUID, amount: object) -> None:
        amount = q(amount)
        wallet = self.locked(self.for_publisher(publisher_id).id)
        wallet.pending_balance = q(D(wallet.pending_balance) - amount)
        wallet.earned_total = q(D(wallet.earned_total) - amount)
        wallet.version += 1
        publisher = self.session.get(Publisher, publisher_id)
        if publisher is not None:
            publisher.lifetime_earned = q(D(publisher.lifetime_earned) - amount)

    # -- statement ---------------------------------------------------------

    def _statement(
        self,
        wallet: Wallet,
        txn: LedgerTransaction,
        signed_amount: Decimal,
        bucket: str,
        description: str,
        campaign_id: uuid.UUID | None = None,
    ) -> WalletTransaction:
        balance_map = {
            "available": wallet.available_balance,
            "reserved": wallet.reserved_balance,
            "pending": wallet.pending_balance,
            "confirmed": wallet.confirmed_balance,
        }
        row = WalletTransaction(
            wallet_id=wallet.id,
            ledger_transaction_id=txn.id,
            transaction_type=txn.transaction_type,
            signed_amount=q(signed_amount),
            currency=wallet.currency,
            balance_after=q(balance_map.get(bucket, ZERO)),
            bucket=bucket,
            description=description,
            campaign_id=campaign_id,
            created_at=utcnow(),
        )
        self.session.add(row)
        return row

    def statement(
        self, wallet_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[WalletTransaction]:
        return list(
            self.session.scalars(
                select(WalletTransaction)
                .where(WalletTransaction.wallet_id == wallet_id)
                .order_by(WalletTransaction.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    # -- reconciliation ----------------------------------------------------

    def verify_against_ledger(self, wallet: Wallet) -> dict[str, Decimal]:
        """Compare the projection to the ledger. All diffs must be zero."""
        out: dict[str, Decimal] = {}
        if wallet.advertiser_id:
            owner = wallet.advertiser_id
            out["available"] = q(
                D(wallet.available_balance)
                - self.ledger.balance(AccountKind.ADVERTISER_AVAILABLE, wallet.currency, owner)
            )
            out["reserved"] = q(
                D(wallet.reserved_balance)
                - self.ledger.balance(AccountKind.ADVERTISER_RESERVED, wallet.currency, owner)
            )
            # There is no ADVERTISER_SPENT account: spend is recognised by
            # debiting the advertiser's reservation. Cumulative spend is therefore
            # the sum of their settlement postings.
            settled = self.session.scalar(
                select(func.coalesce(func.sum(LedgerTransaction.amount), 0)).where(
                    LedgerTransaction.advertiser_id == owner,
                    LedgerTransaction.transaction_type == TransactionType.SETTLEMENT,
                    LedgerTransaction.status == TransactionStatus.POSTED,
                    LedgerTransaction.currency == wallet.currency,
                )
            )
            out["spent"] = q(D(wallet.spent_total) - D(settled or 0))
        if wallet.publisher_id:
            owner = wallet.publisher_id
            out["pending"] = q(
                D(wallet.pending_balance)
                - self.ledger.balance(AccountKind.PUBLISHER_PENDING, wallet.currency, owner)
            )
            out["confirmed"] = q(
                D(wallet.confirmed_balance)
                - self.ledger.balance(AccountKind.PUBLISHER_CONFIRMED, wallet.currency, owner)
            )
        return out


def default_currency() -> str:
    return app_settings.default_currency
