"""Double-entry ledger (spec §15).

``LedgerService.post`` is the **only** function in this codebase permitted to
change an account balance. Everything financial — deposits, reservations,
settlement, earnings, withdrawals, refunds, manual adjustments — goes through it.

Three guarantees it enforces, in order:

1. **Balance.** ``sum(debits) == sum(credits)`` or the post is refused.
2. **Idempotency.** ``ledger_transactions.idempotency_key`` is UNIQUE, so a
   replayed payment callback is caught by the database, not by a check that a
   concurrent request can lose.
3. **Deadlock freedom.** Accounts are locked ``FOR UPDATE`` in ascending id
   order, so two transfers touching the same pair cannot deadlock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    CurrencyMismatch,
    DuplicateTransaction,
    InsufficientFunds,
    LedgerError,
    NotFound,
    UnbalancedTransaction,
)
from app.core.money import ZERO, D, q
from app.db.base import utcnow
from app.models.enums import (
    AccountKind,
    AccountOwnerType,
    EntryDirection,
    TransactionStatus,
    TransactionType,
)
from app.models.money import LedgerAccount, LedgerEntry, LedgerTransaction

# Which side increases each account. Assets and expenses are debit-normal;
# liabilities and revenue are credit-normal.
NORMAL_SIDE: dict[AccountKind, EntryDirection] = {
    AccountKind.ADVERTISER_AVAILABLE: EntryDirection.CREDIT,
    AccountKind.ADVERTISER_RESERVED: EntryDirection.CREDIT,
    AccountKind.PUBLISHER_PENDING: EntryDirection.CREDIT,
    AccountKind.PUBLISHER_CONFIRMED: EntryDirection.CREDIT,
    AccountKind.PLATFORM_REVENUE: EntryDirection.CREDIT,
    AccountKind.PLATFORM_FEES: EntryDirection.CREDIT,
    # The platform's own cash position: debited when an advertiser deposits,
    # credited when a publisher is paid.
    AccountKind.GATEWAY_CLEARING: EntryDirection.DEBIT,
    AccountKind.PAYOUT_CLEARING: EntryDirection.CREDIT,
    AccountKind.FRAUD_CLAWBACK: EntryDirection.CREDIT,
}

# Accounts that must never go negative: a negative balance here would mean we
# have promised money we do not hold.
NON_NEGATIVE: frozenset[AccountKind] = frozenset(
    {
        AccountKind.ADVERTISER_AVAILABLE,
        AccountKind.ADVERTISER_RESERVED,
        AccountKind.PUBLISHER_PENDING,
        AccountKind.PUBLISHER_CONFIRMED,
        # Paying out more than was committed would mean sending money we never
        # took from a balance.
        AccountKind.PAYOUT_CLEARING,
    }
)

OWNER_FOR_KIND: dict[AccountKind, AccountOwnerType] = {
    AccountKind.ADVERTISER_AVAILABLE: AccountOwnerType.ADVERTISER,
    AccountKind.ADVERTISER_RESERVED: AccountOwnerType.ADVERTISER,
    AccountKind.PUBLISHER_PENDING: AccountOwnerType.PUBLISHER,
    AccountKind.PUBLISHER_CONFIRMED: AccountOwnerType.PUBLISHER,
    AccountKind.PLATFORM_REVENUE: AccountOwnerType.PLATFORM,
    AccountKind.PLATFORM_FEES: AccountOwnerType.PLATFORM,
    AccountKind.GATEWAY_CLEARING: AccountOwnerType.PLATFORM,
    AccountKind.PAYOUT_CLEARING: AccountOwnerType.PLATFORM,
    AccountKind.FRAUD_CLAWBACK: AccountOwnerType.PLATFORM,
}


@dataclass(frozen=True)
class Leg:
    """One side of a movement, expressed against an account *kind* and owner."""

    kind: AccountKind
    direction: EntryDirection
    amount: Decimal
    owner_id: uuid.UUID | None = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        amount = q(self.amount)
        object.__setattr__(self, "amount", amount)
        if amount <= ZERO:
            raise LedgerError(f"leg amount must be positive, got {amount}")
        expected_owner = OWNER_FOR_KIND[self.kind]
        if expected_owner is AccountOwnerType.PLATFORM:
            if self.owner_id is not None:
                raise LedgerError(f"{self.kind} is a platform account and takes no owner_id")
        elif self.owner_id is None:
            raise LedgerError(f"{self.kind} requires an owner_id")


def debit(kind: AccountKind, amount: object, owner_id: uuid.UUID | None = None, **meta) -> Leg:
    return Leg(kind, EntryDirection.DEBIT, D(amount), owner_id, meta)


def credit(kind: AccountKind, amount: object, owner_id: uuid.UUID | None = None, **meta) -> Leg:
    return Leg(kind, EntryDirection.CREDIT, D(amount), owner_id, meta)


@dataclass(frozen=True)
class PostResult:
    transaction: LedgerTransaction
    entries: list[LedgerEntry]
    replayed: bool  # True when an existing transaction was returned for this key

    @property
    def id(self) -> uuid.UUID:
        return self.transaction.id


class LedgerService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- accounts ----------------------------------------------------------

    def get_or_create_account(
        self, kind: AccountKind, currency: str, owner_id: uuid.UUID | None = None
    ) -> LedgerAccount:
        owner_type = OWNER_FOR_KIND[kind]
        if owner_type is AccountOwnerType.PLATFORM:
            owner_id = None
        stmt = select(LedgerAccount).where(
            LedgerAccount.kind == kind,
            LedgerAccount.currency == currency,
            LedgerAccount.owner_type == owner_type,
            LedgerAccount.owner_id == owner_id,
        )
        account = self.session.scalars(stmt).one_or_none()
        if account is not None:
            return account
        account = LedgerAccount(
            owner_type=owner_type,
            owner_id=owner_id,
            kind=kind,
            currency=currency,
            normal_side=NORMAL_SIDE[kind],
            balance=ZERO,
            label=f"{kind}:{owner_id or 'platform'}",
        )
        try:
            # Flush inside a SAVEPOINT so a concurrent creator collides here, and
            # losing that race costs only this insert rather than the caller's
            # whole transaction.
            with self.session.begin_nested():
                self.session.add(account)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalars(stmt).one_or_none()
            if existing is None:  # pragma: no cover - only on a genuine DB fault
                raise
            return existing
        return account

    def balance(
        self, kind: AccountKind, currency: str, owner_id: uuid.UUID | None = None
    ) -> Decimal:
        account = self.session.scalars(
            select(LedgerAccount).where(
                LedgerAccount.kind == kind,
                LedgerAccount.currency == currency,
                LedgerAccount.owner_type == OWNER_FOR_KIND[kind],
                LedgerAccount.owner_id == (
                    None if OWNER_FOR_KIND[kind] is AccountOwnerType.PLATFORM else owner_id
                ),
            )
        ).one_or_none()
        return q(account.balance) if account else ZERO

    # -- posting -----------------------------------------------------------

    def find_by_key(self, idempotency_key: str) -> LedgerTransaction | None:
        return self.session.scalars(
            select(LedgerTransaction).where(
                LedgerTransaction.idempotency_key == idempotency_key
            )
        ).one_or_none()

    def post(
        self,
        *,
        transaction_type: TransactionType,
        currency: str,
        legs: Sequence[Leg],
        idempotency_key: str,
        description: str | None = None,
        advertiser_id: uuid.UUID | None = None,
        publisher_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        delivery_id: uuid.UUID | None = None,
        settlement_batch_id: uuid.UUID | None = None,
        reference_type: str | None = None,
        reference_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        reverses_transaction_id: uuid.UUID | None = None,
        meta: dict | None = None,
        allow_negative: bool = False,
    ) -> PostResult:
        """Post a balanced transaction. Idempotent on ``idempotency_key``."""
        if not idempotency_key:
            raise LedgerError("idempotency_key is required for every posting")
        if len(legs) < 2:
            raise UnbalancedTransaction("a transaction needs at least two legs")

        # Replay guard: return the original rather than double-crediting.
        existing = self.find_by_key(idempotency_key)
        if existing is not None:
            if existing.transaction_type != transaction_type:
                raise DuplicateTransaction(
                    "idempotency key reused for a different transaction type",
                    key=idempotency_key,
                    existing_type=str(existing.transaction_type),
                )
            return PostResult(existing, list(existing.entries), replayed=True)

        self._assert_balanced(legs)
        magnitude = q(
            sum((leg.amount for leg in legs if leg.direction is EntryDirection.DEBIT), ZERO)
        )
        now = utcnow()

        txn = LedgerTransaction(
            transaction_type=transaction_type,
            status=TransactionStatus.POSTED,
            currency=currency,
            amount=magnitude,
            idempotency_key=idempotency_key,
            advertiser_id=advertiser_id,
            publisher_id=publisher_id,
            campaign_id=campaign_id,
            delivery_id=delivery_id,
            settlement_batch_id=settlement_batch_id,
            reference_type=reference_type,
            reference_id=reference_id,
            reverses_transaction_id=reverses_transaction_id,
            description=description,
            meta=meta or {},
            actor_type=actor_type,
            actor_id=actor_id,
            created_at=now,
        )
        self.session.add(txn)

        # Resolve every account first, then lock them in a deterministic order.
        resolved = [(leg, self.get_or_create_account(leg.kind, currency, leg.owner_id))
                    for leg in legs]
        self._lock_accounts(account for _, account in resolved)

        entries: list[LedgerEntry] = []
        for leg, account in resolved:
            if account.currency != currency:
                raise CurrencyMismatch(
                    f"account {account.kind} is {account.currency}, posting is {currency}"
                )
            signed = leg.amount if leg.direction is account.normal_side else -leg.amount
            new_balance = q(D(account.balance) + signed)
            if not allow_negative and account.kind in NON_NEGATIVE and new_balance < ZERO:
                raise InsufficientFunds(
                    f"{account.kind} would go negative",
                    account_kind=str(account.kind),
                    owner_id=str(account.owner_id) if account.owner_id else None,
                    balance=str(q(account.balance)),
                    requested=str(leg.amount),
                )
            account.balance = new_balance
            entry = LedgerEntry(
                transaction=txn,
                account_id=account.id,
                direction=leg.direction,
                amount=leg.amount,
                currency=currency,
                balance_after=new_balance,
                created_at=now,
                meta=leg.meta,
            )
            self.session.add(entry)
            entries.append(entry)

        try:
            self.session.flush()
        except IntegrityError:
            # Lost the race on the unique idempotency key: the other writer's post
            # stands and ours must leave no trace. Undo it to the savepoint the
            # caller opened, then return the winner.
            self.session.rollback()
            winner = self.find_by_key(idempotency_key)
            if winner is None:  # pragma: no cover - a genuine constraint fault
                raise
            return PostResult(winner, list(winner.entries), replayed=True)
        return PostResult(txn, entries, replayed=False)

    def reverse(
        self,
        transaction: LedgerTransaction,
        *,
        idempotency_key: str,
        reason: str,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> PostResult:
        """Post the mirror image of ``transaction``.

        Reversal is by counter-entry, never by deleting or editing the original:
        the history of a mistake is part of the audit trail (spec §34).
        """
        if transaction.status is TransactionStatus.REVERSED:
            raise LedgerError("transaction is already reversed", txn=str(transaction.id))
        legs: list[Leg] = []
        for entry in transaction.entries:
            account = self.session.get(LedgerAccount, entry.account_id)
            if account is None:  # pragma: no cover
                raise NotFound("ledger account vanished")
            legs.append(
                Leg(
                    kind=account.kind,
                    direction=entry.direction.opposite,
                    amount=q(entry.amount),
                    owner_id=account.owner_id,
                    meta={"reverses_entry": str(entry.id)},
                )
            )
        result = self.post(
            transaction_type=transaction.transaction_type,
            currency=transaction.currency,
            legs=legs,
            idempotency_key=idempotency_key,
            description=f"Reversal: {reason}",
            advertiser_id=transaction.advertiser_id,
            publisher_id=transaction.publisher_id,
            campaign_id=transaction.campaign_id,
            delivery_id=transaction.delivery_id,
            reverses_transaction_id=transaction.id,
            actor_type=actor_type,
            actor_id=actor_id,
            meta={"reason": reason},
            allow_negative=True,  # unwinding must always be possible
        )
        if not result.replayed:
            transaction.status = TransactionStatus.REVERSED
        return result

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _assert_balanced(legs: Sequence[Leg]) -> None:
        debits = q(sum((l.amount for l in legs if l.direction is EntryDirection.DEBIT), ZERO))
        credits = q(sum((l.amount for l in legs if l.direction is EntryDirection.CREDIT), ZERO))
        if debits != credits:
            raise UnbalancedTransaction(
                f"debits {debits} != credits {credits}",
                debits=str(debits),
                credits=str(credits),
            )
        if debits == ZERO:
            raise UnbalancedTransaction("a zero-value transaction is not a movement")

    def _lock_accounts(self, accounts: Iterable[LedgerAccount]) -> None:
        """Lock rows FOR UPDATE in ascending id order to preclude deadlock."""
        ids = sorted({a.id for a in accounts}, key=str)
        if not ids:
            return
        dialect = self.session.get_bind().dialect.name
        if dialect == "sqlite":
            return  # SQLite serialises writes already; FOR UPDATE is unsupported
        self.session.execute(
            select(LedgerAccount.id)
            .where(LedgerAccount.id.in_(ids))
            .order_by(LedgerAccount.id)
            .with_for_update()
        ).all()

    # -- verification ------------------------------------------------------

    def trial_balance(self, currency: str) -> dict[str, Decimal]:
        """Sum every account by normal side. Debits must equal credits globally."""
        accounts = self.session.scalars(
            select(LedgerAccount).where(LedgerAccount.currency == currency)
        ).all()
        debits = q(
            sum(
                (D(a.balance) for a in accounts if a.normal_side is EntryDirection.DEBIT),
                ZERO,
            )
        )
        credits = q(
            sum(
                (D(a.balance) for a in accounts if a.normal_side is EntryDirection.CREDIT),
                ZERO,
            )
        )
        return {"debits": debits, "credits": credits, "difference": q(debits - credits)}

    def recompute_account_balance(self, account_id: uuid.UUID) -> Decimal:
        """Replay a single account's entries. Used by the nightly integrity job."""
        account = self.session.get(LedgerAccount, account_id)
        if account is None:
            raise NotFound("ledger account not found")
        entries = self.session.scalars(
            select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        ).all()
        total = ZERO
        for entry in entries:
            total += (
                D(entry.amount)
                if entry.direction is account.normal_side
                else -D(entry.amount)
            )
        return q(total)
