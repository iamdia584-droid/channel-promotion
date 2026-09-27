"""Settlement and earnings: the money must reconcile exactly, every time."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.errors import Conflict
from app.core.money import cpm_cost, q
from app.db.base import utcnow
from app.models.enums import (
    AccountKind,
    DeliveryStatus,
    EarningStatus,
    TransactionType,
)
from app.models.money import LedgerTransaction, PublisherEarning
from app.services.earnings import EarningsService
from app.services.impressions import ImpressionService
from app.services.ledger import LedgerService
from app.services.settlement import SettlementService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


def _impressions(db, delivery, n, prefix="s", rows=None):
    """Record ``n`` measured impressions; return how many were billable.

    Large volumes are recorded as a handful of rows carrying ``quantity`` rather
    than one row per impression. Settlement arithmetic, the ratchet cap and the
    counters all operate on quantities, so this exercises the same code paths
    without making the suite insert tens of thousands of rows. Tests specifically
    about per-event dedupe pass ``rows=n`` to get one row each.
    """
    from app.models.enums import ImpressionKind, ImpressionSource

    service = ImpressionService(db)
    rows = rows if rows is not None else min(n, 20)
    base, remainder = divmod(n, rows)
    billed = 0
    for i in range(rows):
        quantity = base + (1 if i < remainder else 0)
        if quantity <= 0:
            continue
        if service.record(
            delivery,
            kind=ImpressionKind.MEASURED,
            source=ImpressionSource.TRACKING_LINK,
            dedupe_key=f"{prefix}:{delivery.id}:{i}",
            quantity=quantity,
            telegram_user_id=1000 + i,
        ).billable:
            billed += quantity
    return billed


# --------------------------------------------------------------------------
# The spec's own arithmetic (§11)
# --------------------------------------------------------------------------


def test_spec_section_11_end_to_end(db, sent_delivery):
    """50,000 impressions at ৳100 CPM, 20% commission → ৳4,000 / ৳1,000."""
    SettingsService(db).set(
        "hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]"
    )
    delivery, campaign, channel, advertiser = sent_delivery(
        bid_cpm="100", avg_views=100_000, budget="400000", commission="0.20"
    )
    assert delivery.effective_cpm == Decimal("100.000000")
    billed = _impressions(db, delivery, 50_000)
    assert billed == 50_000

    result = SettlementService(db).settle_delivery(delivery)
    assert result.settled is True
    assert result.impressions == 50_000
    assert result.gross == Decimal("5000.000000")
    assert result.publisher_amount == Decimal("4000.000000")
    assert result.platform_amount == Decimal("1000.000000")
    assert result.publisher_amount + result.platform_amount == result.gross


def test_settlement_keeps_the_ledger_balanced(db, sent_delivery):
    delivery, campaign, channel, advertiser = sent_delivery(bid_cpm="80", avg_views=30_000)
    _impressions(db, delivery, 10_000)
    SettlementService(db).settle_delivery(delivery)
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_publisher_earnings_and_platform_revenue_match_advertiser_spend(db, sent_delivery):
    SettingsService(db).set(
        "hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]"
    )
    delivery, campaign, channel, advertiser = sent_delivery(
        bid_cpm="123.456789", avg_views=40_000, budget="400000", commission="0.3333"
    )
    _impressions(db, delivery, 7_777)
    result = SettlementService(db).settle_delivery(delivery)

    ledger = LedgerService(db)
    pending = ledger.balance(AccountKind.PUBLISHER_PENDING, "BDT", delivery.publisher_id)
    revenue = ledger.balance(AccountKind.PLATFORM_REVENUE, "BDT")
    assert pending == result.publisher_amount
    assert revenue == result.platform_amount
    assert pending + revenue == result.gross


def test_wallet_projections_agree_with_the_ledger_after_settlement(db, sent_delivery):
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=25_000)
    _impressions(db, delivery, 5_000)
    SettlementService(db).settle_delivery(delivery)

    wallets = WalletService(db)
    adv_wallet = wallets.for_advertiser(advertiser.id)
    pub_wallet = wallets.for_publisher(delivery.publisher_id)
    assert all(v == 0 for v in wallets.verify_against_ledger(adv_wallet).values())
    assert all(v == 0 for v in wallets.verify_against_ledger(pub_wallet).values())


# --------------------------------------------------------------------------
# Idempotency (spec §26)
# --------------------------------------------------------------------------


def test_settling_twice_does_not_pay_twice(db, sent_delivery):
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 4_000)
    service = SettlementService(db)

    first = service.settle_delivery(delivery)
    assert first.settled is True
    paid_once = q(delivery.settled_amount)

    for _ in range(3):
        again = service.settle_delivery(delivery)
        assert again.settled is False

    assert q(delivery.settled_amount) == paid_once
    assert db.query(PublisherEarning).count() == 1
    settlements = (
        db.query(LedgerTransaction)
        .filter(LedgerTransaction.transaction_type == TransactionType.SETTLEMENT)
        .count()
    )
    assert settlements == 1


def test_impressions_arriving_mid_window_settle_in_a_second_batch(db, sent_delivery):
    """Settling early must not strand a publisher's later earnings.

    While the measurement window is open the reservation is deliberately kept, so
    impressions that arrive after an early settlement are still payable.
    """
    SettingsService(db).set(
        "hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]"
    )
    delivery, campaign, channel, advertiser = sent_delivery(
        avg_views=50_000, bid_cpm="100", budget="400000"
    )
    _impressions(db, delivery, 1_000, prefix="a")
    service = SettlementService(db)

    first = service.settle_delivery(delivery)
    assert first.impressions == 1_000
    assert delivery.status is DeliveryStatus.MEASURING   # window still open
    assert q(delivery.reserved_amount) > q(delivery.settled_amount)

    _impressions(db, delivery, 500, prefix="b")
    second = service.settle_delivery(delivery)
    assert second.settled is True
    assert second.impressions == 500
    assert second.batch_id != first.batch_id
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_reservation_is_released_once_the_window_closes(db, sent_delivery):
    SettingsService(db).set(
        "hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]"
    )
    delivery, campaign, channel, advertiser = sent_delivery(
        avg_views=50_000, bid_cpm="100", budget="400000"
    )
    _impressions(db, delivery, 1_000)
    service = SettlementService(db)
    service.settle_delivery(delivery)

    # Close the window, then settle again: the unused reservation goes back.
    delivery.measurement_ends_at = utcnow() - timedelta(minutes=1)
    db.flush()
    service.settle_delivery(delivery)

    assert delivery.status is DeliveryStatus.SETTLED
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.reserved_balance) == Decimal("0.000000")
    assert all(
        v == 0 for v in WalletService(db).verify_against_ledger(wallet).values()
    )


# --------------------------------------------------------------------------
# No measurable impressions
# --------------------------------------------------------------------------


def test_unmeasured_delivery_is_refunded_not_charged(db, sent_delivery):
    """A post nobody provably saw must cost the advertiser nothing."""
    delivery, campaign, channel, advertiser = sent_delivery()
    wallets = WalletService(db)
    before = q(wallets.for_advertiser(advertiser.id).available_balance)
    reserved = q(delivery.reserved_amount)
    assert reserved > 0

    result = SettlementService(db).settle_delivery(delivery)
    assert result.settled is False
    assert result.gross == Decimal("0.000000")

    wallet = wallets.for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == before + reserved
    assert q(wallet.reserved_balance) == Decimal("0.000000")
    assert q(wallet.spent_total) == Decimal("0.000000")
    assert all(v == 0 for v in wallets.verify_against_ledger(wallet).values())


def test_unused_reservation_is_returned_to_the_advertiser(db, sent_delivery):
    """Reserve for 20,000 expected impressions, measure 2,000, refund the rest."""
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000, bid_cpm="100")
    reserved = q(delivery.reserved_amount)
    _impressions(db, delivery, 2_000)

    # Settlement runs when the window closes, which is how settle_due drives it.
    delivery.measurement_ends_at = utcnow() - timedelta(minutes=1)
    db.flush()
    result = SettlementService(db).settle_delivery(delivery)
    assert result.gross == cpm_cost(2_000, "100")
    assert result.gross < reserved

    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.reserved_balance) == Decimal("0.000000")
    assert q(wallet.spent_total) == result.gross
    assert all(
        v == 0 for v in WalletService(db).verify_against_ledger(wallet).values()
    )


def test_settlement_never_exceeds_the_reservation(db, sent_delivery):
    """Even if impressions somehow exceed the funded amount, exposure is capped."""
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=10_000, bid_cpm="100")
    reserved = q(delivery.reserved_amount)
    # Lift the cap artificially to simulate runaway measurement.
    delivery.impression_cap = 10_000_000
    db.flush()
    _impressions(db, delivery, 30_000)

    result = SettlementService(db).settle_delivery(delivery)
    assert result.gross <= reserved
    assert q(delivery.settled_amount) <= reserved


def test_cancelled_delivery_is_not_settled(db, sent_delivery):
    delivery, *_ = sent_delivery()
    delivery.status = DeliveryStatus.CANCELLED
    db.flush()
    result = SettlementService(db).settle_delivery(delivery)
    assert result.settled is False
    assert "cancelled" in result.reason


def test_settle_due_only_picks_closed_windows(db, sent_delivery):
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 1_000)
    service = SettlementService(db)

    assert service.settle_due(now=utcnow()) == []          # window still open
    results = service.settle_due(now=delivery.measurement_ends_at + timedelta(minutes=1))
    assert len(results) == 1 and results[0].settled is True


# --------------------------------------------------------------------------
# Earnings lifecycle (spec §12)
# --------------------------------------------------------------------------


def test_earnings_start_pending_and_are_not_withdrawable(db, sent_delivery):
    SettingsService(db).set("earnings_validation_hours", "72")
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 5_000)
    result = SettlementService(db).settle_delivery(delivery)

    earning = db.query(PublisherEarning).one()
    assert earning.status is EarningStatus.PENDING
    assert earning.net_amount == result.publisher_amount
    assert earning.gross_amount == earning.net_amount + earning.platform_commission

    wallet = WalletService(db).for_publisher(delivery.publisher_id)
    assert q(wallet.pending_balance) == result.publisher_amount
    assert q(wallet.confirmed_balance) == Decimal("0.000000")
    assert q(wallet.withdrawable) == Decimal("0.000000")   # cannot be withdrawn yet


def test_earnings_confirm_after_the_validation_window(db, sent_delivery):
    SettingsService(db).set("earnings_validation_hours", "72")
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 5_000)
    SettlementService(db).settle_delivery(delivery)
    earnings = EarningsService(db)

    # Too early.
    assert earnings.confirm_due(now=utcnow()).confirmed == 0
    batch = earnings.confirm_due(now=utcnow() + timedelta(hours=73))
    assert batch.confirmed == 1

    wallet = WalletService(db).for_publisher(delivery.publisher_id)
    assert q(wallet.pending_balance) == Decimal("0.000000")
    assert q(wallet.confirmed_balance) == batch.amount
    assert q(wallet.withdrawable) == batch.amount
    assert all(
        v == 0 for v in WalletService(db).verify_against_ledger(wallet).values()
    )


def test_confirmation_is_idempotent(db, sent_delivery):
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 2_000)
    SettlementService(db).settle_delivery(delivery)
    earnings = EarningsService(db)
    earning = db.query(PublisherEarning).one()

    assert earnings.confirm(earning) is True
    assert earnings.confirm(earning) is False
    wallet = WalletService(db).for_publisher(delivery.publisher_id)
    assert q(wallet.confirmed_balance) == q(earning.net_amount)


def test_high_fraud_score_holds_earnings_past_the_window(db, sent_delivery):
    """Spec §12/§16: a suspicious earning waits for a human, not a timer."""
    s = SettingsService(db)
    s.set("earnings_validation_hours", "24")
    s.set("fraud_hold_earnings_threshold", "61")
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 3_000)
    delivery.fraud_score = 75
    db.flush()
    SettlementService(db).settle_delivery(delivery)

    batch = EarningsService(db).confirm_due(now=utcnow() + timedelta(hours=48))
    assert batch.confirmed == 0
    assert batch.held == 1
    assert db.query(PublisherEarning).one().status is EarningStatus.PENDING


def test_pending_earnings_can_be_reversed_and_the_ledger_stays_balanced(db, sent_delivery):
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 4_000)
    SettlementService(db).settle_delivery(delivery)
    earning = db.query(PublisherEarning).one()
    amount = q(earning.net_amount)

    assert EarningsService(db).reverse(earning, "confirmed click farm") is True
    assert earning.status is EarningStatus.REVERSED

    wallets = WalletService(db)
    wallet = wallets.for_publisher(delivery.publisher_id)
    assert q(wallet.pending_balance) == Decimal("0.000000")
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")
    assert LedgerService(db).balance(AccountKind.FRAUD_CLAWBACK, "BDT") == amount
    assert all(v == 0 for v in wallets.verify_against_ledger(wallet).values())


def test_reversal_retracts_the_underlying_impressions(db, sent_delivery):
    from app.models.delivery import Impression

    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 100)
    SettlementService(db).settle_delivery(delivery)
    EarningsService(db).reverse(db.query(PublisherEarning).one(), "bot traffic")

    assert db.query(Impression).filter(Impression.billable.is_(True)).count() == 0
    assert delivery.billable_impressions == 0


def test_confirmed_earnings_cannot_be_silently_reversed(db, sent_delivery):
    """Once withdrawable, unwinding needs an explicit admin adjustment."""
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 1_000)
    SettlementService(db).settle_delivery(delivery)
    earnings = EarningsService(db)
    earning = db.query(PublisherEarning).one()
    earnings.confirm(earning)

    with pytest.raises(Conflict, match="only pending earnings can be reversed"):
        earnings.reverse(earning, "too late")


def test_reversal_is_idempotent(db, sent_delivery):
    delivery, *_ = sent_delivery(avg_views=20_000)
    _impressions(db, delivery, 500)
    SettlementService(db).settle_delivery(delivery)
    earnings = EarningsService(db)
    earning = db.query(PublisherEarning).one()
    assert earnings.reverse(earning, "fraud") is True
    assert earnings.reverse(earning, "fraud") is False
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_earnings_summary_reports_honest_effective_cpm(db, sent_delivery):
    delivery, *_ = sent_delivery(avg_views=20_000, bid_cpm="100", commission="0.20")
    _impressions(db, delivery, 10_000)
    SettlementService(db).settle_delivery(delivery)
    summary = EarningsService(db).summary(delivery.publisher_id)

    assert summary["billable_impressions"] == 10_000
    assert summary["pending"] == Decimal("800.000000")     # 10,000 × ৳80 / 1000
    assert summary["effective_cpm"] == Decimal("80.000000")
    assert summary["confirmed"] == Decimal("0.000000")
