"""The ledger must balance, resist replay, and never let a projection drift."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import (
    DuplicateTransaction,
    InsufficientFunds,
    LedgerError,
    UnbalancedTransaction,
)
from app.core.money import q
from app.models.enums import AccountKind, EntryDirection, TransactionStatus, TransactionType
from app.services.ledger import NORMAL_SIDE, LedgerService, credit, debit
from app.services.wallet import WalletService


def test_unbalanced_transaction_is_refused(db, make_advertiser):
    adv = make_advertiser()
    ledger = LedgerService(db)
    with pytest.raises(UnbalancedTransaction, match=r"debits .* != credits"):
        ledger.post(
            transaction_type=TransactionType.DEPOSIT,
            currency="BDT",
            legs=[
                debit(AccountKind.GATEWAY_CLEARING, "100"),
                credit(AccountKind.ADVERTISER_AVAILABLE, "90", adv.id),
            ],
            idempotency_key="unbalanced-1",
        )


def test_single_leg_is_refused(db, make_advertiser):
    adv = make_advertiser()
    with pytest.raises(UnbalancedTransaction):
        LedgerService(db).post(
            transaction_type=TransactionType.DEPOSIT,
            currency="BDT",
            legs=[credit(AccountKind.ADVERTISER_AVAILABLE, "100", adv.id)],
            idempotency_key="one-leg",
        )


def test_zero_value_leg_is_refused(db):
    with pytest.raises(LedgerError, match="must be positive"):
        debit(AccountKind.GATEWAY_CLEARING, "0")


def test_platform_account_rejects_owner_and_vice_versa():
    with pytest.raises(LedgerError, match="takes no owner_id"):
        debit(AccountKind.PLATFORM_REVENUE, "10", owner_id="not-none")
    with pytest.raises(LedgerError, match="requires an owner_id"):
        debit(AccountKind.ADVERTISER_AVAILABLE, "10")


def test_normal_side_drives_balance_direction(db, make_advertiser):
    """A credit-normal liability grows on credit; a debit-normal asset grows on debit."""
    adv = make_advertiser()
    ledger = LedgerService(db)
    ledger.post(
        transaction_type=TransactionType.DEPOSIT,
        currency="BDT",
        legs=[
            debit(AccountKind.GATEWAY_CLEARING, "1000"),
            credit(AccountKind.ADVERTISER_AVAILABLE, "1000", adv.id),
        ],
        idempotency_key="dep-1",
    )
    assert ledger.balance(AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id) == Decimal("1000.000000")
    assert ledger.balance(AccountKind.GATEWAY_CLEARING, "BDT") == Decimal("1000.000000")
    assert NORMAL_SIDE[AccountKind.ADVERTISER_AVAILABLE] is EntryDirection.CREDIT
    assert NORMAL_SIDE[AccountKind.GATEWAY_CLEARING] is EntryDirection.DEBIT


def test_trial_balance_is_zero_after_every_post(db, make_advertiser, make_publisher):
    """Global debits must equal global credits — the defining ledger invariant."""
    adv = make_advertiser()
    pub = make_publisher()
    ledger = LedgerService(db)
    movements = [
        (
            TransactionType.DEPOSIT,
            [
                debit(AccountKind.GATEWAY_CLEARING, "10000"),
                credit(AccountKind.ADVERTISER_AVAILABLE, "10000", adv.id),
            ],
        ),
        (
            TransactionType.BUDGET_RESERVE,
            [
                debit(AccountKind.ADVERTISER_AVAILABLE, "6000", adv.id),
                credit(AccountKind.ADVERTISER_RESERVED, "6000", adv.id),
            ],
        ),
        (
            TransactionType.SETTLEMENT,
            [
                debit(AccountKind.ADVERTISER_RESERVED, "1000", adv.id),
                credit(AccountKind.PUBLISHER_PENDING, "700", pub.id),
                credit(AccountKind.PLATFORM_REVENUE, "300"),
            ],
        ),
        (
            TransactionType.EARNING_CONFIRM,
            [
                debit(AccountKind.PUBLISHER_PENDING, "700", pub.id),
                credit(AccountKind.PUBLISHER_CONFIRMED, "700", pub.id),
            ],
        ),
    ]
    for i, (kind, legs) in enumerate(movements):
        ledger.post(transaction_type=kind, currency="BDT", legs=legs, idempotency_key=f"mv-{i}")
        tb = ledger.trial_balance("BDT")
        assert tb["difference"] == Decimal("0.000000"), (kind, tb)


def test_replayed_idempotency_key_does_not_double_credit(db, make_advertiser):
    """Spec §26: a payment callback arriving twice must credit exactly once."""
    adv = make_advertiser()
    ledger = LedgerService(db)
    legs = [
        debit(AccountKind.GATEWAY_CLEARING, "5000"),
        credit(AccountKind.ADVERTISER_AVAILABLE, "5000", adv.id),
    ]

    first = ledger.post(
        transaction_type=TransactionType.DEPOSIT,
        currency="BDT",
        legs=legs,
        idempotency_key="provider-txn-abc123",
    )
    second = ledger.post(
        transaction_type=TransactionType.DEPOSIT,
        currency="BDT",
        legs=legs,
        idempotency_key="provider-txn-abc123",
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.id == first.id
    assert ledger.balance(AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id) == Decimal("5000.000000")


def test_idempotency_key_reuse_across_types_is_an_error(db, make_advertiser):
    adv = make_advertiser()
    ledger = LedgerService(db)
    ledger.post(
        transaction_type=TransactionType.DEPOSIT,
        currency="BDT",
        legs=[
            debit(AccountKind.GATEWAY_CLEARING, "100"),
            credit(AccountKind.ADVERTISER_AVAILABLE, "100", adv.id),
        ],
        idempotency_key="shared-key",
    )
    with pytest.raises(DuplicateTransaction):
        ledger.post(
            transaction_type=TransactionType.REFUND,
            currency="BDT",
            legs=[
                debit(AccountKind.ADVERTISER_AVAILABLE, "100", adv.id),
                credit(AccountKind.GATEWAY_CLEARING, "100"),
            ],
            idempotency_key="shared-key",
        )


def test_protected_accounts_cannot_go_negative(db, make_advertiser):
    adv = make_advertiser()
    ledger = LedgerService(db)
    with pytest.raises(InsufficientFunds):
        ledger.post(
            transaction_type=TransactionType.BUDGET_RESERVE,
            currency="BDT",
            legs=[
                debit(AccountKind.ADVERTISER_AVAILABLE, "1", adv.id),
                credit(AccountKind.ADVERTISER_RESERVED, "1", adv.id),
            ],
            idempotency_key="overdraw",
        )


def test_reversal_is_a_counter_entry_not_a_deletion(db, make_advertiser, make_publisher):
    adv, pub = make_advertiser(), make_publisher()
    ledger = LedgerService(db)
    original = ledger.post(
        transaction_type=TransactionType.SETTLEMENT,
        currency="BDT",
        legs=[
            debit(AccountKind.ADVERTISER_RESERVED, "1000", adv.id),
            credit(AccountKind.PUBLISHER_PENDING, "700", pub.id),
            credit(AccountKind.PLATFORM_REVENUE, "300"),
        ],
        idempotency_key="settle-1",
        allow_negative=True,
    )
    ledger.reverse(original.transaction, idempotency_key="settle-1-rev", reason="fraud confirmed")

    # Balances unwound...
    assert ledger.balance(AccountKind.PUBLISHER_PENDING, "BDT", pub.id) == Decimal("0.000000")
    assert ledger.balance(AccountKind.PLATFORM_REVENUE, "BDT") == Decimal("0.000000")
    # ...but the original transaction and its entries are still on record.
    assert original.transaction.status is TransactionStatus.REVERSED
    assert len(original.transaction.entries) == 3
    assert ledger.trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_double_reversal_is_refused(db, make_advertiser):
    adv = make_advertiser()
    ledger = LedgerService(db)
    txn = ledger.post(
        transaction_type=TransactionType.DEPOSIT,
        currency="BDT",
        legs=[
            debit(AccountKind.GATEWAY_CLEARING, "100"),
            credit(AccountKind.ADVERTISER_AVAILABLE, "100", adv.id),
        ],
        idempotency_key="rev-once",
    )
    ledger.reverse(txn.transaction, idempotency_key="rev-once-r1", reason="mistake")
    with pytest.raises(LedgerError, match="already reversed"):
        ledger.reverse(txn.transaction, idempotency_key="rev-once-r2", reason="again")


def test_entry_records_balance_after_snapshot(db, make_advertiser):
    adv = make_advertiser()
    ledger = LedgerService(db)
    for i, amount in enumerate(["100", "250", "50"]):
        result = ledger.post(
            transaction_type=TransactionType.DEPOSIT,
            currency="BDT",
            legs=[
                debit(AccountKind.GATEWAY_CLEARING, amount),
                credit(AccountKind.ADVERTISER_AVAILABLE, amount, adv.id),
            ],
            idempotency_key=f"snap-{i}",
        )
        entry = next(e for e in result.entries if e.direction is EntryDirection.CREDIT)
        assert entry.balance_after == ledger.balance(
            AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id
        )


def test_recompute_matches_cached_balance(db, make_advertiser):
    """The cached balance must equal a full replay of the account's entries."""
    adv = make_advertiser()
    ledger = LedgerService(db)
    for i in range(12):
        ledger.post(
            transaction_type=TransactionType.DEPOSIT,
            currency="BDT",
            legs=[
                debit(AccountKind.GATEWAY_CLEARING, "13.370000"),
                credit(AccountKind.ADVERTISER_AVAILABLE, "13.370000", adv.id),
            ],
            idempotency_key=f"recompute-{i}",
        )
    account = ledger.get_or_create_account(AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id)
    assert ledger.recompute_account_balance(account.id) == q(account.balance)
    assert q(account.balance) == Decimal("160.440000")


def test_currency_mismatch_is_refused(db, make_advertiser):
    from app.core.errors import CurrencyMismatch

    adv = make_advertiser(currency="BDT")
    ledger = LedgerService(db)
    ledger.get_or_create_account(AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id)
    # Posting USD legs against this advertiser resolves a *separate* USD account,
    # so mixing is structurally impossible rather than merely discouraged.
    ledger.post(
        transaction_type=TransactionType.DEPOSIT,
        currency="USD",
        legs=[
            debit(AccountKind.GATEWAY_CLEARING, "10"),
            credit(AccountKind.ADVERTISER_AVAILABLE, "10", adv.id),
        ],
        idempotency_key="usd-1",
    )
    assert ledger.balance(AccountKind.ADVERTISER_AVAILABLE, "BDT", adv.id) == Decimal("0.000000")
    assert ledger.balance(AccountKind.ADVERTISER_AVAILABLE, "USD", adv.id) == Decimal("10.000000")
    assert ledger.trial_balance("USD")["difference"] == Decimal("0.000000")


# --------------------------------------------------------------------------
# Wallet projection
# --------------------------------------------------------------------------


def test_deposit_updates_projection_and_statement(db, make_advertiser):
    adv = make_advertiser()
    wallets = WalletService(db)
    wallets.credit_deposit(adv.id, "10000", idempotency_key="dep-a")
    wallet = wallets.for_advertiser(adv.id)

    view = wallets.view(wallet)
    assert view.available == Decimal("10000.000000")
    assert view.deposited == Decimal("10000.000000")
    assert wallets.verify_against_ledger(wallet) == {
        "available": Decimal("0.000000"),
        "reserved": Decimal("0.000000"),
        "spent": Decimal("0.000000"),
    }
    statement = wallets.statement(wallet.id)
    assert len(statement) == 1
    assert statement[0].signed_amount == Decimal("10000.000000")
    assert statement[0].balance_after == Decimal("10000.000000")


def test_duplicate_deposit_callback_credits_once(db, make_advertiser):
    """Spec §26 end to end: the wallet projection must not double-count either."""
    adv = make_advertiser()
    wallets = WalletService(db)
    for _ in range(4):
        wallets.credit_deposit(adv.id, "2500", idempotency_key="bkash-TRX99")
    wallet = wallets.for_advertiser(adv.id)
    assert wallets.view(wallet).available == Decimal("2500.000000")
    assert len(wallets.statement(wallet.id)) == 1
    assert wallets.verify_against_ledger(wallet)["available"] == Decimal("0.000000")


def test_reserve_then_release_round_trips(db, make_advertiser, make_campaign):
    adv = make_advertiser()
    campaign = make_campaign(adv)
    wallets = WalletService(db)
    wallets.credit_deposit(adv.id, "10000", idempotency_key="d1")

    wallets.reserve_budget(adv.id, campaign.id, "6000", idempotency_key="r1")
    wallet = wallets.for_advertiser(adv.id)
    view = wallets.view(wallet)
    assert (view.available, view.reserved) == (Decimal("4000.000000"), Decimal("6000.000000"))
    assert view.total_held == Decimal("10000.000000")

    wallets.release_budget(adv.id, campaign.id, "6000", idempotency_key="rel1")
    view = wallets.view(wallets.for_advertiser(adv.id))
    assert (view.available, view.reserved) == (Decimal("10000.000000"), Decimal("0.000000"))
    assert all(v == 0 for v in wallets.verify_against_ledger(wallet).values())


def test_cannot_reserve_more_than_available(db, make_advertiser, make_campaign):
    adv = make_advertiser()
    campaign = make_campaign(adv)
    wallets = WalletService(db)
    wallets.credit_deposit(adv.id, "1000", idempotency_key="d2")
    with pytest.raises(InsufficientFunds):
        wallets.reserve_budget(adv.id, campaign.id, "1000.000001", idempotency_key="r2")


def test_cannot_release_more_than_reserved(db, make_advertiser, make_campaign):
    adv = make_advertiser()
    campaign = make_campaign(adv)
    wallets = WalletService(db)
    wallets.credit_deposit(adv.id, "1000", idempotency_key="d3")
    wallets.reserve_budget(adv.id, campaign.id, "400", idempotency_key="r3")
    with pytest.raises(InsufficientFunds):
        wallets.release_budget(adv.id, campaign.id, "500", idempotency_key="rel3")


def test_replayed_reservation_reserves_once(db, make_advertiser, make_campaign):
    adv = make_advertiser()
    campaign = make_campaign(adv)
    wallets = WalletService(db)
    wallets.credit_deposit(adv.id, "10000", idempotency_key="d4")
    for _ in range(3):
        wallets.reserve_budget(adv.id, campaign.id, "2000", idempotency_key="reserve-once")
    view = wallets.view(wallets.for_advertiser(adv.id))
    assert view.reserved == Decimal("2000.000000")
    assert view.available == Decimal("8000.000000")
