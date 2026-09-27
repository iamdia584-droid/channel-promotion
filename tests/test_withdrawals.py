"""Withdrawals: only confirmed money leaves, exactly once, fully audited."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.errors import (
    Conflict,
    InsufficientFunds,
    ValidationFailed,
    WithdrawalError,
)
from app.core.money import q
from app.db.base import utcnow
from app.models.enums import (
    AccountKind,
    EarningStatus,
    PayoutMethod,
    WithdrawalStatus,
)
from app.models.money import PublisherEarning
from app.models.ops import AuditLog
from app.services.audit import Actor
from app.services.earnings import EarningsService
from app.services.impressions import ImpressionService
from app.services.ledger import LedgerService
from app.services.settlement import SettlementService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService
from app.services.withdrawals import (
    WithdrawalService,
    decrypt_destination,
    encrypt_destination,
    mask_destination,
)


@pytest.fixture
def earning_publisher(db, sent_delivery):
    """A publisher with confirmed, withdrawable balance."""

    def _make(impressions=20_000, bid_cpm="100", commission="0.20"):
        from app.models.enums import ImpressionKind, ImpressionSource

        SettingsService(db).set(
            "hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]"
        )
        delivery, campaign, channel, advertiser = sent_delivery(
            bid_cpm=bid_cpm, avg_views=100_000, budget="400000", commission=commission
        )
        service = ImpressionService(db)
        rows = 20
        base = impressions // rows
        for i in range(rows):
            service.record(
                delivery, kind=ImpressionKind.MEASURED,
                source=ImpressionSource.TRACKING_LINK,
                dedupe_key=f"w:{delivery.id}:{i}", quantity=base,
                telegram_user_id=5000 + i,
            )
        SettlementService(db).settle_delivery(delivery)
        earnings = EarningsService(db)
        for earning in db.query(PublisherEarning).all():
            earnings.confirm(earning)
        return delivery.publisher_id, WalletService(db).for_publisher(delivery.publisher_id)

    return _make


def _method(db, publisher_id, dest="01712345678", method=PayoutMethod.BKASH):
    return WithdrawalService(db).add_payout_method(publisher_id, method, dest)


# --------------------------------------------------------------------------
# Payout destinations (spec §13: never expose sensitive data)
# --------------------------------------------------------------------------


def test_destination_is_encrypted_and_only_shown_masked(db, make_publisher):
    publisher = make_publisher()
    record = _method(db, publisher.id, "01712345678")

    assert record.destination_masked == "01******678"
    assert "01712345678" not in record.destination_encrypted
    assert decrypt_destination(record.destination_encrypted) == "01712345678"


def test_revealing_a_destination_is_audited(db, make_publisher, make_staff):
    publisher, staff = make_publisher(), make_staff()
    record = _method(db, publisher.id)
    service = WithdrawalService(db)

    assert service.reveal_destination(record, Actor.staff(staff)) == "01712345678"
    log = db.query(AuditLog).filter(AuditLog.action == "payout_method.revealed").one()
    assert log.actor_id == str(staff.id)
    assert log.target_id == str(record.id)


def test_tampered_destination_is_detected(db, make_publisher):
    publisher = make_publisher()
    record = _method(db, publisher.id)
    record.destination_encrypted = record.destination_encrypted[:-6] + "AAAAAA"
    with pytest.raises(WithdrawalError, match="integrity check"):
        decrypt_destination(record.destination_encrypted)


@pytest.mark.parametrize(
    "method,dest,ok",
    [
        (PayoutMethod.BKASH, "01712345678", True),
        (PayoutMethod.BKASH, "+8801712345678", True),
        (PayoutMethod.BKASH, "1712345678", False),      # missing leading 0
        (PayoutMethod.NAGAD, "0171234567", False),      # 10 digits
        (PayoutMethod.ROCKET, "02712345678", False),    # not 01
        (PayoutMethod.BANK, "12345678901", True),
        (PayoutMethod.BANK, "123", False),
    ],
)
def test_destination_validation(db, make_publisher, method, dest, ok):
    publisher = make_publisher()
    if ok:
        assert _method(db, publisher.id, dest, method) is not None
    else:
        with pytest.raises(ValidationFailed):
            _method(db, publisher.id, dest, method)


def test_re_adding_the_same_destination_updates_rather_than_duplicates(db, make_publisher):
    publisher = make_publisher()
    first = _method(db, publisher.id, "01712345678")
    second = _method(db, publisher.id, "01712345678")
    assert first.id == second.id
    assert len(WithdrawalService(db).list_payout_methods(publisher.id)) == 1


def test_masking_handles_short_values():
    assert mask_destination("12") == "**"
    assert mask_destination("123456") == "***456"


# --------------------------------------------------------------------------
# Requesting
# --------------------------------------------------------------------------


def test_pending_earnings_cannot_be_withdrawn(db, sent_delivery):
    """Spec §12: pending balance is not withdrawable."""
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000)
    ImpressionService(db).record(
        delivery, kind=ImpressionKind.MEASURED, source=ImpressionSource.TRACKING_LINK,
        dedupe_key="p1", quantity=5_000,
    )
    SettlementService(db).settle_delivery(delivery)
    wallet = WalletService(db).for_publisher(delivery.publisher_id)
    assert q(wallet.pending_balance) > 0
    assert q(wallet.confirmed_balance) == Decimal("0.000000")

    SettingsService(db).set("min_withdrawal", "1.000000")
    method = _method(db, delivery.publisher_id)
    with pytest.raises(InsufficientFunds, match="not yet"):
        WithdrawalService(db).request(delivery.publisher_id, "100", method.id)


def test_withdrawal_deducts_confirmed_balance_immediately(db, earning_publisher):
    publisher_id, wallet = earning_publisher()
    s = SettingsService(db)
    s.set("min_withdrawal", "100.000000")
    s.set("withdrawal_fee_flat", "10.000000")
    s.set("withdrawal_fee_percent", "0.0100")
    before = q(wallet.confirmed_balance)
    assert before >= Decimal("1000")

    method = _method(db, publisher_id)
    withdrawal = WithdrawalService(db).request(publisher_id, "1000", method.id)

    assert withdrawal.amount == Decimal("1000.000000")
    assert withdrawal.fee == Decimal("20.000000")      # 10 flat + 1% of 1000
    assert withdrawal.net_amount == Decimal("980.000000")
    assert withdrawal.status is WithdrawalStatus.PENDING
    assert withdrawal.destination_masked == "01******678"

    wallet = WalletService(db).for_publisher(publisher_id)
    assert q(wallet.confirmed_balance) == before - Decimal("1000.000000")
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_the_same_balance_cannot_be_withdrawn_twice(db, earning_publisher):
    publisher_id, wallet = earning_publisher()
    SettingsService(db).set("min_withdrawal", "100.000000")
    available = q(wallet.confirmed_balance)
    method = _method(db, publisher_id)
    service = WithdrawalService(db)

    service.request(publisher_id, available, method.id)
    with pytest.raises(InsufficientFunds):
        service.request(publisher_id, available, method.id)


def test_withdrawal_request_is_idempotent(db, earning_publisher):
    publisher_id, wallet = earning_publisher()
    SettingsService(db).set("min_withdrawal", "100.000000")
    before = q(wallet.confirmed_balance)
    method = _method(db, publisher_id)
    service = WithdrawalService(db)

    for _ in range(3):
        service.request(publisher_id, "500", method.id, idempotency_key="idem-1")

    assert db.query(type(_first_withdrawal(db))).count() == 1
    wallet = WalletService(db).for_publisher(publisher_id)
    assert q(wallet.confirmed_balance) == before - Decimal("500.000000")


def _first_withdrawal(db):
    from app.models.money import Withdrawal

    return db.query(Withdrawal).first()


def test_below_minimum_is_refused(db, earning_publisher):
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "500.000000")
    method = _method(db, publisher_id)
    with pytest.raises(ValidationFailed, match="minimum withdrawal is"):
        WithdrawalService(db).request(publisher_id, "499", method.id)


def test_daily_limit_is_enforced(db, earning_publisher):
    publisher_id, _ = earning_publisher()
    s = SettingsService(db)
    s.set("min_withdrawal", "10.000000")
    s.set("max_withdrawal_per_day", "1000.000000")
    method = _method(db, publisher_id)
    service = WithdrawalService(db)

    service.request(publisher_id, "800", method.id)
    with pytest.raises(ValidationFailed, match="daily withdrawal limit"):
        service.request(publisher_id, "300", method.id)


def test_fee_cannot_consume_the_whole_amount(db, earning_publisher):
    publisher_id, _ = earning_publisher()
    s = SettingsService(db)
    s.set("min_withdrawal", "1.000000")
    s.set("withdrawal_fee_flat", "100.000000")
    method = _method(db, publisher_id)
    with pytest.raises(ValidationFailed, match="consume the whole amount"):
        WithdrawalService(db).request(publisher_id, "50", method.id)


# --------------------------------------------------------------------------
# Operator transitions
# --------------------------------------------------------------------------


def test_full_paid_lifecycle_balances(db, earning_publisher, make_staff):
    publisher_id, wallet = earning_publisher()
    SettingsService(db).set("min_withdrawal", "100.000000")
    SettingsService(db).set("withdrawal_fee_flat", "10.000000")
    SettingsService(db).set("withdrawal_fee_percent", "0")
    staff = make_staff()
    actor = Actor.staff(staff)
    method = _method(db, publisher_id)
    service = WithdrawalService(db)

    withdrawal = service.request(publisher_id, "1000", method.id)
    service.mark_processing(withdrawal, actor)
    assert withdrawal.status is WithdrawalStatus.PROCESSING
    service.mark_paid(withdrawal, actor, provider_reference="BKASH-TX-7781")

    assert withdrawal.status is WithdrawalStatus.PAID
    assert withdrawal.provider_reference == "BKASH-TX-7781"
    assert withdrawal.processed_by_staff_id == staff.id

    ledger = LedgerService(db)
    assert ledger.trial_balance("BDT")["difference"] == Decimal("0.000000")
    assert ledger.balance(AccountKind.PAYOUT_CLEARING, "BDT") == Decimal("0.000000")
    assert ledger.balance(AccountKind.PLATFORM_FEES, "BDT") == Decimal("10.000000")

    wallet = WalletService(db).for_publisher(publisher_id)
    assert q(wallet.withdrawn_total) == Decimal("990.000000")


def test_paying_requires_a_provider_reference(db, earning_publisher, make_staff):
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "500", method.id)
    with pytest.raises(ValidationFailed, match="provider reference"):
        service.mark_paid(withdrawal, Actor.staff(make_staff()), provider_reference="  ")


def test_paying_twice_does_not_double_post(db, earning_publisher, make_staff):
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    actor = Actor.staff(make_staff())
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "500", method.id)
    service.mark_paid(withdrawal, actor, provider_reference="REF-1")

    with pytest.raises(Conflict, match="cannot pay a paid withdrawal"):
        service.mark_paid(withdrawal, actor, provider_reference="REF-1")
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_rejection_returns_the_full_amount_including_the_fee(db, earning_publisher, make_staff):
    publisher_id, wallet = earning_publisher()
    s = SettingsService(db)
    s.set("min_withdrawal", "10.000000")
    s.set("withdrawal_fee_flat", "25.000000")
    s.set("withdrawal_fee_percent", "0")
    before = q(wallet.confirmed_balance)
    actor = Actor.staff(make_staff())
    method = _method(db, publisher_id)
    service = WithdrawalService(db)

    withdrawal = service.request(publisher_id, "600", method.id)
    service.reject(withdrawal, actor, "payout number does not match the account name")

    assert withdrawal.status is WithdrawalStatus.REJECTED
    wallet = WalletService(db).for_publisher(publisher_id)
    # The publisher is made whole: we never keep a fee for a payout we did not make.
    assert q(wallet.confirmed_balance) == before
    ledger = LedgerService(db)
    assert ledger.balance(AccountKind.PLATFORM_FEES, "BDT") == Decimal("0.000000")
    assert ledger.balance(AccountKind.PAYOUT_CLEARING, "BDT") == Decimal("0.000000")
    assert ledger.trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_rejection_requires_a_reason(db, earning_publisher, make_staff):
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "500", method.id)
    with pytest.raises(ValidationFailed, match="reason is required"):
        service.reject(withdrawal, Actor.staff(make_staff()), "   ")


def test_publisher_can_cancel_while_pending(db, earning_publisher, make_staff):
    publisher_id, wallet = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    before = q(wallet.confirmed_balance)
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "400", method.id)

    service.cancel(withdrawal, Actor.staff(make_staff()))
    assert withdrawal.status is WithdrawalStatus.CANCELLED
    assert q(WalletService(db).for_publisher(publisher_id).confirmed_balance) == before


def test_cannot_cancel_after_payout(db, earning_publisher, make_staff):
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    actor = Actor.staff(make_staff())
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "400", method.id)
    service.mark_paid(withdrawal, actor, provider_reference="R1")
    with pytest.raises(Conflict):
        service.cancel(withdrawal, actor)


def test_every_financial_transition_is_audited_with_its_ledger_txn(
    db, earning_publisher, make_staff
):
    """Spec §34: an admin must not be able to move money without a trace."""
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    actor = Actor.staff(make_staff())
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "500", method.id)
    service.mark_paid(withdrawal, actor, provider_reference="REF-9")

    financial = db.query(AuditLog).filter(AuditLog.is_financial.is_(True)).all()
    actions = {log.action for log in financial}
    assert {"withdrawal.requested", "withdrawal.paid"} <= actions
    for log in financial:
        assert log.ledger_transaction_id is not None, log.action


# --------------------------------------------------------------------------
# Fraud interaction
# --------------------------------------------------------------------------


def test_fraud_hold_blocks_processing_until_reviewed(db, earning_publisher, make_staff):
    publisher_id, _ = earning_publisher()
    s = SettingsService(db)
    s.set("min_withdrawal", "10.000000")
    s.set("fraud_hold_earnings_threshold", "1")     # force a hold
    method = _method(db, publisher_id)
    service = WithdrawalService(db)
    withdrawal = service.request(publisher_id, "500", method.id)
    assert withdrawal.fraud_hold is True

    with pytest.raises(WithdrawalError, match="fraud hold"):
        service.mark_processing(withdrawal, Actor.staff(make_staff()))


def test_shared_payout_destination_raises_the_fraud_score(db, earning_publisher, make_publisher):
    """One bKash number behind several publishers is a collusion signal."""
    publisher_id, _ = earning_publisher()
    SettingsService(db).set("min_withdrawal", "10.000000")
    other = make_publisher()
    service = WithdrawalService(db)
    service.add_payout_method(other.id, PayoutMethod.BKASH, "01712345678")
    method = _method(db, publisher_id, "01712345678")

    withdrawal = service.request(publisher_id, "500", method.id)
    assert withdrawal.fraud_score > 30
    signals = str(db.query(__import__("app.models.ops", fromlist=["FraudScore"])
                           .FraudScore).all())
    assert signals is not None


def test_pending_queue_surfaces_held_withdrawals_first(db, earning_publisher, make_publisher):
    publisher_id, _ = earning_publisher()
    s = SettingsService(db)
    s.set("min_withdrawal", "10.000000")
    service = WithdrawalService(db)
    method = _method(db, publisher_id)

    clean = service.request(publisher_id, "100", method.id, idempotency_key="k1")
    s.set("fraud_hold_earnings_threshold", "1")
    held = service.request(publisher_id, "100", method.id, idempotency_key="k2")

    queue = service.pending_queue()
    assert queue[0].id == held.id
    assert {w.id for w in queue} == {clean.id, held.id}
