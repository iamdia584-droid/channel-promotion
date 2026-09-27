"""Refunds return unspent reservation only — never money already earned (spec §35)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import Conflict, ValidationFailed
from app.core.money import q
from app.db.base import utcnow
from app.models.enums import CampaignStatus, RefundStatus
from app.models.money import Refund
from app.models.ops import AuditLog
from app.services.audit import Actor
from app.services.impressions import ImpressionService
from app.services.ledger import LedgerService
from app.services.refunds import RefundService
from app.services.settlement import SettlementService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


def test_spec_section_35_example(db, make_advertiser, make_campaign, funded):
    """Budget ৳10,000, spent ৳3,500 → ৳6,500 refundable."""
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="10000", daily_budget="10000")
    wallets = WalletService(db)
    wallets.reserve_budget(advertiser.id, campaign.id, "10000", idempotency_key="r")
    campaign.reserved_amount = q("10000")
    # Simulate ৳3,500 already settled.
    campaign.spent_amount = q("3500")
    campaign.reserved_amount = q("6500")
    db.flush()

    quote = RefundService(db).quote(campaign)
    assert quote.spent == Decimal("3500.000000")
    assert quote.refundable == Decimal("6500.000000")


def test_refund_returns_money_to_available_balance(db, make_advertiser, make_campaign,
                                                   funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="10000", daily_budget="10000")
    wallets = WalletService(db)
    wallets.reserve_budget(advertiser.id, campaign.id, "10000", idempotency_key="r1")
    campaign.reserved_amount = q("10000")
    db.flush()
    before = q(wallets.for_advertiser(advertiser.id).available_balance)

    service = RefundService(db)
    refund = service.request(campaign, reason="changed plans")
    refund = service.approve(refund, Actor.staff(make_staff()))

    assert refund.status is RefundStatus.PROCESSED
    assert refund.approved_amount == Decimal("10000.000000")
    wallet = wallets.for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == before + Decimal("10000.000000")
    assert q(wallet.reserved_balance) == Decimal("0.000000")
    assert q(wallet.refunded_total) == Decimal("10000.000000")
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")
    assert all(v == 0 for v in wallets.verify_against_ledger(wallet).values())


def test_in_flight_deliveries_are_not_refunded(db, sent_delivery):
    """A post already published may still produce billable impressions.

    Refunding its reservation would let an advertiser take back money the
    publisher has already earned the inventory for.
    """
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000)
    quote = RefundService(db).quote(campaign)

    assert quote.in_flight == q(delivery.reserved_amount)
    assert quote.refundable == Decimal("0.000000")
    with pytest.raises(ValidationFailed, match="nothing refundable"):
        RefundService(db).request(campaign)


def test_refund_becomes_available_once_the_delivery_settles(db, sent_delivery, make_staff):
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000, bid_cpm="100")
    ImpressionService(db).record(
        delivery, kind=ImpressionKind.MEASURED, source=ImpressionSource.TRACKING_LINK,
        dedupe_key="x1", quantity=1_000,
    )
    delivery.measurement_ends_at = utcnow()
    db.flush()
    SettlementService(db).settle_delivery(delivery)

    # After settlement the unused reservation is already back in available, so
    # the campaign has nothing reserved left to refund.
    quote = RefundService(db).quote(campaign)
    assert quote.in_flight == Decimal("0.000000")
    assert q(campaign.spent_amount) > 0


def test_cancelling_a_campaign_auto_refunds_the_unspent_reservation(
    db, make_advertiser, make_campaign, funded, make_staff
):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="8000", daily_budget="8000")
    wallets = WalletService(db)
    wallets.reserve_budget(advertiser.id, campaign.id, "8000", idempotency_key="r2")
    campaign.reserved_amount = q("8000")
    db.flush()
    before = q(wallets.for_advertiser(advertiser.id).available_balance)

    campaign, refund = RefundService(db).cancel_campaign(
        campaign, Actor.staff(make_staff()), "no longer needed"
    )
    assert campaign.status is CampaignStatus.CANCELLED
    assert refund is not None
    assert refund.approved_amount == Decimal("8000.000000")
    assert q(wallets.for_advertiser(advertiser.id).available_balance) == before + q("8000")


def test_cancelling_a_campaign_with_nothing_reserved_makes_no_refund(
    db, make_advertiser, make_campaign, funded, make_staff
):
    advertiser = funded(make_advertiser())
    campaign = make_campaign(advertiser)
    campaign, refund = RefundService(db).cancel_campaign(campaign, Actor.staff(make_staff()))
    assert refund is None
    assert campaign.status is CampaignStatus.CANCELLED


def test_cancelling_twice_is_refused(db, make_advertiser, make_campaign, funded, make_staff):
    advertiser = funded(make_advertiser())
    campaign = make_campaign(advertiser)
    actor = Actor.staff(make_staff())
    RefundService(db).cancel_campaign(campaign, actor)
    with pytest.raises(Conflict, match="already cancelled"):
        RefundService(db).cancel_campaign(campaign, actor)


def test_cannot_refund_more_than_is_refundable(db, make_advertiser, make_campaign, funded):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="5000", daily_budget="5000")
    WalletService(db).reserve_budget(advertiser.id, campaign.id, "5000", idempotency_key="r3")
    campaign.reserved_amount = q("5000")
    db.flush()
    with pytest.raises(ValidationFailed, match="is refundable right now"):
        RefundService(db).request(campaign, amount="5000.000001")


def test_refund_approval_is_idempotent(db, make_advertiser, make_campaign, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="4000", daily_budget="4000")
    wallets = WalletService(db)
    wallets.reserve_budget(advertiser.id, campaign.id, "4000", idempotency_key="r4")
    campaign.reserved_amount = q("4000")
    db.flush()
    before = q(wallets.for_advertiser(advertiser.id).available_balance)

    service = RefundService(db)
    refund = service.request(campaign, idempotency_key="idem-refund")
    service.approve(refund, Actor.staff(make_staff()))
    # A repeated approve must not credit the wallet a second time.
    with pytest.raises(Conflict):
        service.approve(refund, Actor.staff(make_staff()))
    assert q(wallets.for_advertiser(advertiser.id).available_balance) == before + q("4000")


def test_duplicate_refund_request_returns_the_same_row(db, make_advertiser, make_campaign,
                                                       funded):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="4000", daily_budget="4000")
    WalletService(db).reserve_budget(advertiser.id, campaign.id, "4000", idempotency_key="r5")
    campaign.reserved_amount = q("4000")
    db.flush()
    service = RefundService(db)
    first = service.request(campaign, idempotency_key="same")
    second = service.request(campaign, idempotency_key="same")
    assert first.id == second.id
    assert db.query(Refund).count() == 1


def test_rejected_refund_moves_no_money(db, make_advertiser, make_campaign, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="4000", daily_budget="4000")
    wallets = WalletService(db)
    wallets.reserve_budget(advertiser.id, campaign.id, "4000", idempotency_key="r6")
    campaign.reserved_amount = q("4000")
    db.flush()
    reserved_before = q(wallets.for_advertiser(advertiser.id).reserved_balance)

    service = RefundService(db)
    refund = service.request(campaign)
    service.reject(refund, Actor.staff(make_staff()), "campaign already delivered")

    assert refund.status is RefundStatus.REJECTED
    assert q(wallets.for_advertiser(advertiser.id).reserved_balance) == reserved_before


def test_rejection_requires_a_note(db, make_advertiser, make_campaign, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="4000", daily_budget="4000")
    WalletService(db).reserve_budget(advertiser.id, campaign.id, "4000", idempotency_key="r7")
    campaign.reserved_amount = q("4000")
    db.flush()
    refund = RefundService(db).request(campaign)
    with pytest.raises(ValidationFailed, match="note is required"):
        RefundService(db).reject(refund, Actor.staff(make_staff()), "  ")


def test_refund_is_audited_with_its_ledger_transaction(db, make_advertiser, make_campaign,
                                                       funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, total_budget="4000", daily_budget="4000")
    WalletService(db).reserve_budget(advertiser.id, campaign.id, "4000", idempotency_key="r8")
    campaign.reserved_amount = q("4000")
    db.flush()
    service = RefundService(db)
    refund = service.approve(service.request(campaign), Actor.staff(make_staff()))

    log = db.query(AuditLog).filter(AuditLog.action == "refund.approved").one()
    assert log.is_financial is True
    assert log.ledger_transaction_id == refund.ledger_transaction_id
    assert log.new_value["approved"] == "4000.000000"
