"""Workers must be safe to redeliver, and must not lose money when they retry."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.money import q
from app.db.base import utcnow
from app.models.enums import (
    ChannelStatus,
    DeliveryStatus,
    EarningStatus,
    ImpressionKind,
    ImpressionSource,
    NotificationStatus,
)
from app.models.money import PublisherEarning
from app.models.ops import EventLog, Notification
from app.services.impressions import ImpressionService
from app.services.ledger import LedgerService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


@pytest.fixture
def worker_env(db, engine, monkeypatch, gateway_fixture):
    """Point the worker tasks at the test database and a fake Telegram."""
    from sqlalchemy.orm import sessionmaker

    import app.db.session as session_mod
    from app.services.telegram_gateway import set_gateway

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(session_mod, "SessionLocal", factory)
    set_gateway(gateway_fixture)
    yield gateway_fixture
    set_gateway(None)


# --------------------------------------------------------------------------
# Delivery worker
# --------------------------------------------------------------------------


def test_serve_channel_delivers_and_is_serialised(
    db, worker_env, make_campaign, make_channel, make_advertiser, funded
):
    from app.workers.tasks import serve_channel

    advertiser = funded(make_advertiser(), "100000")
    make_campaign(advertiser)
    channel = make_channel()
    worker_env.register_chat(channel.telegram_chat_id, username="wchan")
    db.commit()

    result = serve_channel(str(channel.id))
    assert "delivery_id" in result, result
    assert len(worker_env.sent) == 1


def test_serve_channel_on_a_missing_channel_is_a_no_op(db, worker_env):
    import uuid

    from app.workers.tasks import serve_channel

    assert serve_channel(str(uuid.uuid4())) == {"skipped": "channel not found"}


def test_serve_due_channels_only_queues_eligible_channels(
    db, worker_env, make_channel, make_campaign, make_advertiser, funded
):
    from app.workers.tasks import serve_due_channels

    advertiser = funded(make_advertiser(), "100000")
    make_campaign(advertiser)
    make_channel(status=ChannelStatus.ACTIVE)
    make_channel(status=ChannelStatus.SUSPENDED)
    make_channel(status=ChannelStatus.ACTIVE, auto_advertising=False)
    db.commit()

    # Celery's .delay is not available without a broker; call the body directly.
    from unittest.mock import patch

    with patch("app.workers.tasks.serve_channel.delay") as delay:
        result = serve_due_channels()
    assert result["queued"] == 1
    assert delay.call_count == 1


# --------------------------------------------------------------------------
# Settlement worker
# --------------------------------------------------------------------------


def test_settle_due_worker_settles_closed_windows(db, worker_env, sent_delivery):
    from app.workers.tasks import settle_due

    delivery, campaign, channel, advertiser = sent_delivery(
        avg_views=20_000, bid_cpm="100", commission="0.20"
    )
    ImpressionService(db).record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key="w1",
        quantity=5_000,
    )
    delivery.measurement_ends_at = utcnow() - timedelta(minutes=1)
    db.commit()

    result = settle_due()
    assert result["settled"] == 1
    assert result["gross"] == "500.000000"  # 5,000 at ৳100 CPM


def test_running_the_settlement_worker_twice_does_not_double_pay(db, worker_env, sent_delivery):
    """Celery redelivers on worker loss. That must be harmless (spec §26)."""
    from app.workers.tasks import settle_due

    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000)
    ImpressionService(db).record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key="w2",
        quantity=3_000,
    )
    delivery.measurement_ends_at = utcnow() - timedelta(minutes=1)
    db.commit()

    first = settle_due()
    second = settle_due()
    third = settle_due()

    assert first["settled"] == 1
    assert second["settled"] == 0
    assert third["settled"] == 0
    assert db.query(PublisherEarning).count() == 1
    db.expire_all()
    assert LedgerService(db).trial_balance("BDT")["difference"] == Decimal("0.000000")


def test_confirm_earnings_worker_is_idempotent(db, worker_env, sent_delivery):
    from app.services.settlement import SettlementService
    from app.workers.tasks import confirm_earnings

    SettingsService(db).set("earnings_validation_hours", "0")
    delivery, campaign, channel, advertiser = sent_delivery(avg_views=20_000)
    ImpressionService(db).record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key="w3",
        quantity=4_000,
    )
    SettlementService(db).settle_delivery(delivery)
    publisher_id = delivery.publisher_id
    db.commit()

    first = confirm_earnings()
    second = confirm_earnings()
    assert first["confirmed"] == 1
    assert second["confirmed"] == 0

    db.expire_all()
    wallet = WalletService(db).for_publisher(publisher_id)
    assert q(wallet.pending_balance) == Decimal("0.000000")
    assert q(wallet.confirmed_balance) == q(first["amount"])
    assert all(v == 0 for v in WalletService(db).verify_against_ledger(wallet).values())


# --------------------------------------------------------------------------
# Integrity worker
# --------------------------------------------------------------------------


def test_verify_ledger_reports_balance(db, worker_env, sent_delivery):
    from app.services.settlement import SettlementService
    from app.workers.tasks import verify_ledger

    delivery, *_ = sent_delivery(avg_views=20_000)
    ImpressionService(db).record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key="w4",
        quantity=2_000,
    )
    SettlementService(db).settle_delivery(delivery)
    db.commit()

    assert verify_ledger("BDT")["difference"] == "0.000000"


def test_verify_ledger_alerts_admins_on_an_imbalance(db, worker_env, make_staff):
    """An imbalance must escalate to a human, not just a log line."""
    from app.models.enums import AccountKind
    from app.services.ledger import LedgerService
    from app.workers.tasks import verify_ledger

    # A staff member with a Telegram id, so the alert has somewhere to go.
    make_staff(telegram_user_id=555_000_111)
    ledger = LedgerService(db)
    # Corrupt a cached balance directly, bypassing post() — exactly the class of
    # bug this job exists to catch.
    account = ledger.get_or_create_account(AccountKind.PLATFORM_REVENUE, "BDT")
    account.balance = Decimal("123.456789")
    db.commit()

    result = verify_ledger("BDT")
    assert result["difference"] != "0.000000"
    db.expire_all()
    alerts = db.query(Notification).filter(Notification.template == "admin_system_error").all()
    assert len(alerts) == 1
    assert "trial balance" in alerts[0].rendered_text


# --------------------------------------------------------------------------
# Snapshot and reporting workers
# --------------------------------------------------------------------------


def test_snapshot_worker_is_idempotent(db, worker_env, sent_delivery):
    from app.models.money import DailyFinancialSnapshot
    from app.services.settlement import SettlementService
    from app.workers.tasks import build_snapshot

    delivery, *_ = sent_delivery(avg_views=20_000, bid_cpm="100", commission="0.20")
    ImpressionService(db).record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key="w5",
        quantity=10_000,
    )
    SettlementService(db).settle_delivery(delivery)
    today = utcnow().date().isoformat()
    db.commit()

    first = build_snapshot(today)
    second = build_snapshot(today)
    assert first["gross_ad_spend"] == "1000.000000"
    assert first["publisher_payout"] == "800.000000"
    assert first["platform_revenue"] == "200.000000"
    assert second == first
    db.expire_all()
    assert db.query(DailyFinancialSnapshot).count() == 1


# --------------------------------------------------------------------------
# Fraud worker
# --------------------------------------------------------------------------


def test_channel_audit_worker_flags_and_opens_a_case(db, worker_env, make_channel):
    from app.models.ops import FraudCase, FraudEvent
    from app.workers.tasks import audit_channels

    # Every fraud signal at once: inflated members, no views.
    make_channel(members=800_000, avg_views=500, status=ChannelStatus.ACTIVE)
    make_channel(members=40_000, avg_views=20_000, status=ChannelStatus.ACTIVE)
    db.commit()

    result = audit_channels()
    assert result["audited"] == 2
    assert result["flagged"] >= 1
    db.expire_all()
    assert db.query(FraudEvent).count() >= 1
    # A case, never an automatic ban (spec §16).
    for case in db.query(FraudCase).all():
        assert case.status.value == "open"


# --------------------------------------------------------------------------
# Notification and event workers
# --------------------------------------------------------------------------


def test_notification_worker_sends_and_marks_sent(db, worker_env, make_publisher):
    from app.services.notifications import NotificationService
    from app.workers.tasks import deliver_pending

    publisher = make_publisher()
    NotificationService(db).queue_for_publisher(
        publisher.id, "earnings_confirmed", {"amount": "500", "currency": "BDT"}
    )
    db.commit()

    result = deliver_pending()
    assert result["sent"] == 1
    db.expire_all()
    assert db.query(Notification).one().status is NotificationStatus.SENT
    assert len(worker_env.sent) == 1


def test_notification_worker_suppresses_a_blocked_user(db, worker_env, make_publisher):
    """A user who blocked the bot will never succeed; retrying forever is waste."""
    from app.services.notifications import NotificationService
    from app.services.telegram_gateway import TelegramError
    from app.workers.tasks import deliver_pending

    publisher = make_publisher()
    NotificationService(db).queue_for_publisher(
        publisher.id, "earnings_confirmed", {"amount": "500", "currency": "BDT"}
    )
    db.commit()
    worker_env.fail_next_send = "Forbidden: bot was blocked by the user"

    deliver_pending()
    db.expire_all()
    assert db.query(Notification).one().status is NotificationStatus.SUPPRESSED


def test_duplicate_notifications_are_deduplicated(db, worker_env, make_publisher):
    from app.services.notifications import NotificationService

    publisher = make_publisher()
    service = NotificationService(db)
    for _ in range(4):
        service.queue_for_publisher(
            publisher.id,
            "earnings_confirmed",
            {"amount": "500", "currency": "BDT"},
            dedupe_key="same-earning",
        )
    assert db.query(Notification).count() == 1


def test_event_worker_drains_the_outbox(db, worker_env, make_publisher):
    from app.core.events import Event, emit
    from app.workers.tasks import dispatch_events

    publisher = make_publisher()
    emit(
        db,
        Event.EARNING_CREATED,
        {
            "publisher_id": str(publisher.id),
            "earning_id": "e1",
            "amount": "750",
            "currency": "BDT",
            "impressions": 9000,
        },
    )
    db.commit()

    assert dispatch_events()["dispatched"] == 1
    db.expire_all()
    assert db.query(EventLog).one().dispatched is True
    note = db.query(Notification).filter(Notification.template == "earnings_generated").one()
    assert "750" in note.rendered_text
    # Draining again must not re-notify.
    assert dispatch_events()["dispatched"] == 0
    assert db.query(Notification).count() == 1


def test_a_failing_event_handler_does_not_block_the_queue(db, worker_env):
    from app.core.events import Event, emit
    from app.workers.tasks import dispatch_events

    # A publisher id that does not exist: the handler will fail for this row.
    import uuid

    emit(db, Event.EARNING_CREATED, {"publisher_id": str(uuid.uuid4())})
    emit(db, Event.CAMPAIGN_STARTED, {"campaign_id": "x"})
    db.commit()

    result = dispatch_events()
    assert result["dispatched"] == 2
    db.expire_all()
    rows = db.query(EventLog).all()
    # Each row records its own outcome; one failure does not stall the others.
    assert any(r.dispatched for r in rows)


def test_expired_campaign_worker_completes_them(
    db, worker_env, make_advertiser, make_campaign, funded
):
    from app.workers.tasks import complete_expired_campaigns

    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser)
    campaign.starts_at = utcnow() - timedelta(days=3)
    campaign.ends_at = utcnow() - timedelta(minutes=1)
    db.commit()

    assert complete_expired_campaigns()["completed"] == 1
    db.expire_all()
    assert db.get(type(campaign), campaign.id).status.value == "completed"


def test_expired_post_removal_worker(db, worker_env, sent_delivery):
    from app.workers.tasks import remove_expired_posts

    SettingsService(db).set("ad_post_ttl_hours", "1")
    delivery, *_ = sent_delivery()
    delivery.sent_at = utcnow() - timedelta(hours=5)
    delivery.status = DeliveryStatus.SETTLED
    db.commit()

    assert remove_expired_posts()["removed"] == 1
    assert len(worker_env.deleted) == 1


def test_idempotency_purge_worker(db, worker_env):
    from app.core.idempotency import store
    from app.models.ops import IdempotencyRecord
    from app.workers.tasks import purge_idempotency

    store(db, "scope", "key-1", {"a": 1}, 201, {"ok": True})
    row = db.query(IdempotencyRecord).one()
    row.expires_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert purge_idempotency()["purged"] == 1
    db.expire_all()
    assert db.query(IdempotencyRecord).count() == 0
