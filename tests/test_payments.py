"""Deposits: a provider callback must credit exactly once (spec §26)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.money import q
from app.models.enums import DepositStatus, TransactionType
from app.models.money import Deposit, LedgerTransaction
from app.models.ops import AuditLog
from app.services.audit import Actor
from app.services.ledger import LedgerService
from app.services.payments import DepositService, ManualProvider, ProviderCharge
from app.services.wallet import WalletService


def _charge(deposit, txn_id="BKASH-TX-001", amount=None):
    return ManualProvider().verify_callback(
        {
            "provider_transaction_id": txn_id,
            "amount": format(q(amount if amount is not None else deposit.amount), "f"),
            "currency": deposit.currency,
            "reference": "ref-1",
        }
    )


def test_initiate_creates_a_pending_deposit_without_crediting(db, make_advertiser):
    """Money must not appear before the provider confirms it."""
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, instructions = service.initiate(advertiser.id, "5000")

    assert deposit.status is DepositStatus.PENDING
    assert deposit.amount == Decimal("5000.000000")
    assert deposit.net_amount == Decimal("5000.000000")
    assert "reference" in instructions
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == Decimal("0.000000")


def test_fee_is_deducted_from_the_credited_amount(db, make_advertiser):
    advertiser = make_advertiser()
    deposit, _ = DepositService(db).initiate(advertiser.id, "5000", fee="150")
    assert deposit.fee == Decimal("150.000000")
    assert deposit.net_amount == Decimal("4850.000000")
    assert deposit.net_amount == q(deposit.amount) - q(deposit.fee)


@pytest.mark.parametrize(
    "amount,fee", [("0", "0"), ("-10", "0"), ("100", "100"), ("100", "150"), ("100", "-5")]
)
def test_invalid_amounts_are_refused(db, make_advertiser, amount, fee):
    advertiser = make_advertiser()
    with pytest.raises(ValidationFailed):
        DepositService(db).initiate(advertiser.id, amount, fee=fee)


def test_unknown_advertiser_is_refused(db):
    import uuid

    with pytest.raises(NotFound):
        DepositService(db).initiate(uuid.uuid4(), "1000")


def test_confirmation_credits_the_wallet_once(db, make_advertiser):
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "5000")
    service.confirm(_charge(deposit), deposit_id=deposit.id)

    assert deposit.status is DepositStatus.CONFIRMED
    assert deposit.confirmed_at is not None
    assert deposit.ledger_transaction_id is not None
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == Decimal("5000.000000")
    assert q(wallet.deposited_total) == Decimal("5000.000000")
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_a_replayed_provider_callback_credits_once(db, make_advertiser):
    """Spec §26: payment providers retry. Six deliveries, one credit."""
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "7500")
    charge = _charge(deposit, "BKASH-RETRY-99")

    for _ in range(6):
        service.confirm(charge, deposit_id=deposit.id)

    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == Decimal("7500.000000")
    deposits = db.query(Deposit).filter(Deposit.advertiser_id == advertiser.id).all()
    assert len(deposits) == 1
    settlements = (
        db.query(LedgerTransaction)
        .filter(LedgerTransaction.transaction_type == TransactionType.DEPOSIT)
        .count()
    )
    assert settlements == 1


def test_the_same_provider_transaction_id_cannot_fund_two_deposits(db, make_advertiser):
    """The UNIQUE constraint, not application logic, is the guarantee."""
    advertiser = make_advertiser()
    service = DepositService(db)
    first, _ = service.initiate(advertiser.id, "1000")
    second, _ = service.initiate(advertiser.id, "1000")

    service.confirm(_charge(first, "SHARED-REF"), deposit_id=first.id)
    # The second deposit tries to claim the same provider reference.
    result = service.confirm(_charge(second, "SHARED-REF"), deposit_id=second.id)

    assert result.id == first.id  # the original wins
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == Decimal("1000.000000")
    assert second.status is DepositStatus.PENDING


def test_a_callback_whose_amount_disagrees_is_refused(db, make_advertiser):
    """A provider reporting a different amount is a discrepancy, not a credit."""
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "5000")
    with pytest.raises(ValidationFailed, match="does not match the deposit amount"):
        service.confirm(_charge(deposit, amount="9999"), deposit_id=deposit.id)
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == Decimal("0.000000")


def test_a_callback_in_the_wrong_currency_is_refused(db, make_advertiser):
    advertiser = make_advertiser(currency="BDT")
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "5000")
    charge = ProviderCharge(
        provider="manual",
        provider_transaction_id="X1",
        amount=Decimal("5000"),
        currency="USD",
    )
    with pytest.raises(ValidationFailed, match="currency"):
        service.confirm(charge, deposit_id=deposit.id)


def test_a_callback_with_no_matching_deposit_is_refused(db):
    service = DepositService(db)
    charge = ProviderCharge(
        provider="manual",
        provider_transaction_id="ORPHAN",
        amount=Decimal("100"),
        currency="BDT",
    )
    with pytest.raises(NotFound, match="no deposit matches"):
        service.confirm(charge)


def test_malformed_callbacks_are_rejected_by_the_provider(db):
    provider = ManualProvider()
    with pytest.raises(ValidationFailed, match="missing fields"):
        provider.verify_callback({"amount": "100"})


def test_provider_secrets_are_stripped_before_being_stored(db, make_advertiser):
    """A provider payload often carries a signature. It must not be persisted."""
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "1000")
    charge = ManualProvider().verify_callback(
        {
            "provider_transaction_id": "SECRETY-1",
            "amount": "1000.000000",
            "currency": "BDT",
            "signature": "abc123-do-not-store-me",
            "api_key": "live_key_should_never_persist",
            "pin": "1234",
            "payer": "01712345678",
        }
    )
    service.confirm(charge, deposit_id=deposit.id)

    stored = deposit.provider_payload
    assert "signature" not in stored
    assert "api_key" not in stored
    assert "pin" not in stored
    assert stored["payer"] == "01712345678"  # ordinary fields are kept
    assert "do-not-store-me" not in str(stored)


def test_confirmation_is_audited_against_its_ledger_transaction(db, make_advertiser, make_staff):
    advertiser = make_advertiser()
    staff = make_staff()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "2000")
    service.confirm(_charge(deposit, "AUDIT-1"), deposit_id=deposit.id, actor=Actor.staff(staff))

    log = db.query(AuditLog).filter(AuditLog.action == "deposit.confirmed").one()
    assert log.is_financial is True
    assert log.ledger_transaction_id == deposit.ledger_transaction_id
    assert log.actor_id == str(staff.id)


def test_a_failed_deposit_credits_nothing(db, make_advertiser):
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "3000")
    service.fail(deposit, "payer cancelled at the gateway")

    assert deposit.status is DepositStatus.FAILED
    assert deposit.failure_reason == "payer cancelled at the gateway"
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == Decimal("0.000000")


def test_a_confirmed_deposit_cannot_be_failed(db, make_advertiser):
    """Reversing settled money is a refund, with its own ledger entries."""
    advertiser = make_advertiser()
    service = DepositService(db)
    deposit, _ = service.initiate(advertiser.id, "3000")
    service.confirm(_charge(deposit, "NOFAIL-1"), deposit_id=deposit.id)
    with pytest.raises(Conflict, match="refund it instead"):
        service.fail(deposit, "too late")


def test_deposit_history_is_listed_newest_first(db, make_advertiser):
    advertiser = make_advertiser()
    service = DepositService(db)
    for i in range(3):
        deposit, _ = service.initiate(advertiser.id, str(1000 + i))
        service.confirm(_charge(deposit, f"HIST-{i}"), deposit_id=deposit.id)

    history = service.list_for_advertiser(advertiser.id)
    assert len(history) == 3
    assert all(d.status is DepositStatus.CONFIRMED for d in history)
    assert q(WalletService(db).for_advertiser(advertiser.id).available_balance) == (
        Decimal("3003.000000")
    )
