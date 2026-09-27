"""Financial tables: ledger, wallets, earnings, deposits, withdrawals, refunds.

The ledger (``ledger_accounts`` / ``ledger_transactions`` / ``ledger_entries``) is
the single authority for every balance. ``wallets`` and the ``*_amount`` columns
elsewhere are projections maintained inside the same DB transaction as the ledger
post, and ``tests/test_ledger.py`` asserts they never diverge.
"""

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
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, Money, StrEnumType, Timestamped, UUIDPk
from app.models.enums import (
    AccountKind,
    AccountOwnerType,
    DepositStatus,
    EarningStatus,
    EntryDirection,
    PayoutMethod,
    RefundStatus,
    TransactionStatus,
    TransactionType,
    WithdrawalStatus,
)

if TYPE_CHECKING:
    from app.models.identity import Advertiser, Publisher


# --------------------------------------------------------------------------
# Double-entry core
# --------------------------------------------------------------------------


class LedgerAccount(UUIDPk, Timestamped, Base):
    """One account per (owner, kind, currency).

    ``normal_side`` lets balance maintenance be a single signed add: an entry on
    the normal side increases the balance, the other side decreases it. That is
    why asset and liability accounts can share one table without sign confusion.
    """

    __tablename__ = "ledger_accounts"

    owner_type: Mapped[AccountOwnerType] = mapped_column(StrEnumType(AccountOwnerType, 16), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(GUID)  # NULL for platform accounts
    kind: Mapped[AccountKind] = mapped_column(StrEnumType(AccountKind, 32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    normal_side: Mapped[EntryDirection] = mapped_column(StrEnumType(EntryDirection, 8), nullable=False)
    balance: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        # SQL UNIQUE treats NULLs as distinct, so this constraint alone would let
        # two platform_revenue accounts (owner_id IS NULL) coexist. The partial
        # unique index below closes that hole.
        UniqueConstraint(
            "owner_type", "owner_id", "kind", "currency", name="uq_ledger_accounts_identity"
        ),
        Index(
            "uq_ledger_accounts_platform_kind",
            "kind",
            "currency",
            unique=True,
            postgresql_where=sa_text("owner_id IS NULL"),
            sqlite_where=sa_text("owner_id IS NULL"),
        ),
        Index("ix_ledger_accounts_owner", "owner_type", "owner_id"),
    )


class LedgerTransaction(UUIDPk, Base):
    """A balanced money movement. ``idempotency_key`` UNIQUE is the replay guard."""

    __tablename__ = "ledger_transactions"

    transaction_type: Mapped[TransactionType] = mapped_column(
        StrEnumType(TransactionType, 32), nullable=False, index=True
    )
    status: Mapped[TransactionStatus] = mapped_column(
        StrEnumType(TransactionStatus, 16), default=TransactionStatus.POSTED, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[object] = mapped_column(Money, nullable=False)  # the movement's magnitude

    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Soft references: a ledger row must never be deleted because a campaign was,
    # so these are deliberately not foreign keys with CASCADE.
    advertiser_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    settlement_batch_id: Mapped[uuid.UUID | None] = mapped_column(GUID, index=True)
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[str | None] = mapped_column(String(64))

    reverses_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT")
    )
    description: Mapped[str | None] = mapped_column(String(300))
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)
    actor_type: Mapped[str | None] = mapped_column(String(24))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_transactions_idempotency_key"),
        Index("ix_ledger_transactions_type_created", "transaction_type", "created_at"),
        CheckConstraint("amount >= 0", name="transaction_amount_non_negative"),
    )


class LedgerEntry(UUIDPk, Base):
    """One leg. Immutable. ``balance_after`` snapshots the account at post time."""

    __tablename__ = "ledger_entries"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    direction: Mapped[EntryDirection] = mapped_column(StrEnumType(EntryDirection, 8), nullable=False)
    amount: Mapped[object] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_after: Mapped[object] = mapped_column(Money, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    transaction: Mapped[LedgerTransaction] = relationship(back_populates="entries")
    account: Mapped[LedgerAccount] = relationship()

    __table_args__ = (
        Index("ix_ledger_entries_account_created", "account_id", "created_at"),
        CheckConstraint("amount > 0", name="entry_amount_positive"),
    )


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------


class Wallet(UUIDPk, Timestamped, Base):
    """Fast projection of a party's ledger accounts (spec §14)."""

    __tablename__ = "wallets"

    advertiser_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("advertisers.id", ondelete="CASCADE"), unique=True
    )
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="CASCADE"), unique=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Advertiser side
    available_balance: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    reserved_balance: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    spent_total: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    refunded_total: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    deposited_total: Mapped[object] = mapped_column(Money, default="0", nullable=False)

    # Publisher side
    pending_balance: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    confirmed_balance: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    withdrawn_total: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    earned_total: Mapped[object] = mapped_column(Money, default="0", nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    advertiser: Mapped["Advertiser | None"] = relationship(back_populates="wallet")
    publisher: Mapped["Publisher | None"] = relationship(back_populates="wallet")

    __table_args__ = (
        CheckConstraint(
            "(advertiser_id IS NOT NULL) <> (publisher_id IS NOT NULL)",
            name="wallet_belongs_to_exactly_one_party",
        ),
        CheckConstraint("available_balance >= 0", name="available_non_negative"),
        CheckConstraint("reserved_balance >= 0", name="reserved_non_negative"),
        CheckConstraint("pending_balance >= 0", name="pending_non_negative"),
        CheckConstraint("confirmed_balance >= 0", name="confirmed_non_negative"),
    )

    @property
    def withdrawable(self):
        return self.confirmed_balance


class WalletTransaction(UUIDPk, Base):
    """User-facing statement line. Always tied to a ledger transaction (spec §15)."""

    __tablename__ = "wallet_transactions"

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ledger_transaction_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_type: Mapped[TransactionType] = mapped_column(StrEnumType(TransactionType, 32), nullable=False)
    # Signed from the user's point of view: +credit to them, -debit from them.
    signed_amount: Mapped[object] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_after: Mapped[object] = mapped_column(Money, nullable=False)
    bucket: Mapped[str] = mapped_column(String(24), nullable=False)  # available/pending/...
    description: Mapped[str | None] = mapped_column(String(300))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    __table_args__ = (Index("ix_wallet_transactions_wallet_created", "wallet_id", "created_at"),)


class PublisherEarning(UUIDPk, Base):
    """Publisher revenue from one settlement batch, pending → confirmed (spec §12)."""

    __tablename__ = "publisher_earnings"

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publisher_channels.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ad_deliveries.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False, index=True)
    settlement_batch_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False, index=True)

    status: Mapped[EarningStatus] = mapped_column(
        StrEnumType(EarningStatus, 16), default=EarningStatus.PENDING, nullable=False, index=True
    )
    billable_impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    publisher_cpm: Mapped[object] = mapped_column(Money, nullable=False)
    gross_amount: Mapped[object] = mapped_column(Money, nullable=False)
    platform_commission: Mapped[object] = mapped_column(Money, nullable=False)
    net_amount: Mapped[object] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    confirm_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(String(300))
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "delivery_id", "settlement_batch_id", name="uq_publisher_earnings_delivery_batch"
        ),
        Index("ix_publisher_earnings_confirmation", "status", "confirm_after"),
        Index("ix_publisher_earnings_pub_status", "publisher_id", "status"),
        CheckConstraint("net_amount >= 0", name="earning_net_non_negative"),
        CheckConstraint(
            "gross_amount = net_amount + platform_commission", name="earning_split_balances"
        ),
    )


# --------------------------------------------------------------------------
# Cash in / cash out
# --------------------------------------------------------------------------


class Deposit(UUIDPk, Timestamped, Base):
    """Advertiser top-up. ``provider_transaction_id`` UNIQUE defeats replayed callbacks."""

    __tablename__ = "deposits"

    advertiser_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("advertisers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[DepositStatus] = mapped_column(
        StrEnumType(DepositStatus, 16), default=DepositStatus.INITIATED, nullable=False, index=True
    )
    amount: Mapped[object] = mapped_column(Money, nullable=False)
    fee: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    net_amount: Mapped[object] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(128))
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    provider_payload: Mapped[dict] = mapped_column(default=dict, nullable=False)

    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_transaction_id", name="uq_deposits_provider_transaction"
        ),
        CheckConstraint("amount > 0", name="deposit_amount_positive"),
        CheckConstraint("net_amount = amount - fee", name="deposit_net_balances"),
    )


class PayoutMethodRecord(UUIDPk, Timestamped, Base):
    """A publisher's payout destination.

    Only a masked form is stored in ``destination_masked`` for display; the full
    value lives in ``destination_encrypted`` and is decrypted solely at payout
    time by staff with the payout permission (spec §13, §28).
    """

    __tablename__ = "payout_methods"

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    method: Mapped[PayoutMethod] = mapped_column(StrEnumType(PayoutMethod, 16), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    account_name: Mapped[str | None] = mapped_column(String(120))
    destination_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    destination_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bank_name: Mapped[str | None] = mapped_column(String(120))
    branch: Mapped[str | None] = mapped_column(String(120))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "publisher_id",
            "method",
            "destination_fingerprint",
            name="uq_payout_methods_publisher_destination",
        ),
    )


class Withdrawal(UUIDPk, Timestamped, Base):
    __tablename__ = "withdrawals"

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("publishers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payout_method_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("payout_methods.id", ondelete="RESTRICT")
    )
    status: Mapped[WithdrawalStatus] = mapped_column(
        StrEnumType(WithdrawalStatus, 16), default=WithdrawalStatus.PENDING, nullable=False, index=True
    )
    amount: Mapped[object] = mapped_column(Money, nullable=False)
    fee: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    net_amount: Mapped[object] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    method: Mapped[PayoutMethod] = mapped_column(StrEnumType(PayoutMethod, 16), nullable=False)
    destination_masked: Mapped[str] = mapped_column(String(64), nullable=False)

    request_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT")
    )
    settle_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT")
    )

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fraud_hold: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    meta: Mapped[dict] = mapped_column(default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_withdrawals_idempotency_key"),
        Index("ix_withdrawals_status_created", "status", "created_at"),
        CheckConstraint("amount > 0", name="withdrawal_amount_positive"),
        CheckConstraint("fee >= 0", name="withdrawal_fee_non_negative"),
        CheckConstraint("net_amount = amount - fee", name="withdrawal_net_balances"),
        CheckConstraint("net_amount > 0", name="withdrawal_net_positive"),
    )


class Refund(UUIDPk, Timestamped, Base):
    """Advertiser refund request against a campaign's unspent reservation (spec §35)."""

    __tablename__ = "refunds"

    advertiser_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("advertisers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[RefundStatus] = mapped_column(
        StrEnumType(RefundStatus, 16), default=RefundStatus.REQUESTED, nullable=False, index=True
    )
    requested_amount: Mapped[object] = mapped_column(Money, nullable=False)
    approved_amount: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    to_wallet: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_transactions.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    decision_note: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_refunds_idempotency_key"),
        CheckConstraint("requested_amount > 0", name="refund_requested_positive"),
        CheckConstraint("approved_amount >= 0", name="refund_approved_non_negative"),
    )


class DailyFinancialSnapshot(UUIDPk, Base):
    """Pre-aggregated daily financial report row (spec §33). Rebuildable from the ledger."""

    __tablename__ = "daily_financial_snapshots"

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    gross_ad_spend: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    publisher_payout: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    platform_revenue: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    advertiser_deposits: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    refunds: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    withdrawals_paid: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    withdrawal_fees: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    fraud_reversed: Mapped[object] = mapped_column(Money, default="0", nullable=False)
    billable_impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_campaigns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_channels: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
