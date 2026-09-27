"""Celery tasks. Thin wrappers around services, each safe to redeliver."""

from __future__ import annotations

import uuid
from datetime import timedelta

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.core.cache import lock
from app.core.errors import AdNetError, Conflict
from app.core.money import ZERO
from app.db.base import utcnow
from app.db.session import session_scope
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


@celery_app.task(name="adnet.delivery.serve_due_channels", ignore_result=True)
def serve_due_channels(limit: int = 200) -> dict:
    """Find channels eligible for an ad and enqueue one delivery each."""
    from app.models.enums import ChannelStatus
    from app.models.telegram import PublisherChannel

    with session_scope() as db:
        channels = db.scalars(
            select(PublisherChannel)
            .where(
                PublisherChannel.status.in_([ChannelStatus.ACTIVE, ChannelStatus.VERIFIED]),
                PublisherChannel.auto_advertising.is_(True),
            )
            .order_by(PublisherChannel.last_ad_at.asc().nullsfirst())
            .limit(limit)
        ).all()
        channel_ids = [str(c.id) for c in channels]

    for channel_id in channel_ids:
        serve_channel.delay(channel_id)
    return {"queued": len(channel_ids)}


@celery_app.task(
    name="adnet.delivery.serve_channel",
    ignore_result=True,
    autoretry_for=(Conflict,),
    retry_backoff=True,
    max_retries=3,
)
def serve_channel(channel_id: str) -> dict:
    """Plan and dispatch one delivery into one channel.

    Serialised per channel by a Redis lock: two concurrent workers must not both
    reserve budget and post to the same chat.
    """
    from app.models.telegram import PublisherChannel
    from app.services.delivery import DeliveryService

    with lock(f"serve:{channel_id}", ttl=60), session_scope() as db:
        channel = db.get(PublisherChannel, uuid.UUID(channel_id))
        if channel is None:
            return {"skipped": "channel not found"}
        try:
            result = DeliveryService(db).deliver_to(channel)
        except AdNetError as exc:
            log.info("delivery_skipped channel=%s reason=%s", channel_id, exc.message)
            return {"skipped": exc.message}
        if result.delivery is None:
            return {"skipped": result.reason}
        return {
            "delivery_id": str(result.delivery.id),
            "campaign_id": str(result.delivery.campaign_id),
            "eligible": result.eligible,
        }


@celery_app.task(name="adnet.delivery.remove_expired_posts", ignore_result=True)
def remove_expired_posts(limit: int = 200) -> dict:
    from app.services.delivery import DeliveryService

    with session_scope() as db:
        return {"removed": DeliveryService(db).remove_expired_posts(limit)}


@celery_app.task(name="adnet.delivery.refresh_channel_stats", ignore_result=True)
def refresh_channel_stats(limit: int = 500) -> dict:
    """Refresh member counts, bot rights and the daily stats row."""
    from app.models.enums import ChannelStatus
    from app.models.telegram import ChannelStatDaily, PublisherChannel
    from app.services.channels import ChannelService
    from app.services.quality import QualityService

    refreshed = 0
    with session_scope() as db:
        channels = db.scalars(
            select(PublisherChannel)
            .where(
                PublisherChannel.status.in_(
                    [ChannelStatus.ACTIVE, ChannelStatus.VERIFIED, ChannelStatus.PAUSED]
                )
            )
            .limit(limit)
        ).all()
        service = ChannelService(db)
        quality = QualityService(db)
        today = utcnow().date()

        for channel in channels:
            previous = channel.chat.member_count if channel.chat else 0
            count = service.refresh_member_count(channel)
            service.refresh_bot_rights(channel)
            quality.refresh(channel)

            row = db.scalars(
                select(ChannelStatDaily).where(
                    ChannelStatDaily.channel_id == channel.id,
                    ChannelStatDaily.stat_date == today,
                )
            ).one_or_none()
            if row is None:
                row = ChannelStatDaily(channel_id=channel.id, stat_date=today, created_at=utcnow())
                db.add(row)
            row.member_count = count
            row.member_delta = count - previous
            row.avg_views = channel.avg_views
            row.median_views = channel.median_views
            row.ad_impressions = channel.total_impressions
            row.ad_clicks = channel.total_clicks
            row.ads_served = channel.total_ads_served
            refreshed += 1
    return {"refreshed": refreshed}


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


@celery_app.task(name="adnet.money.settle_due", ignore_result=True)
def settle_due(limit: int = 200) -> dict:
    """Settle deliveries whose measurement window has closed (spec §9 steps 10-13)."""
    from app.services.settlement import SettlementService

    with session_scope() as db:
        results = SettlementService(db).settle_due(limit)
        settled = [r for r in results if r.settled]
        return {
            "examined": len(results),
            "settled": len(settled),
            "gross": str(sum((r.gross for r in settled), ZERO)),
        }


@celery_app.task(name="adnet.money.confirm_earnings", ignore_result=True)
def confirm_earnings(limit: int = 500) -> dict:
    """Move pending earnings to confirmed once validated (spec §12)."""
    from app.services.earnings import EarningsService
    from app.services.notifications import NotificationService

    with session_scope() as db:
        service = EarningsService(db)
        batch = service.confirm_due(limit)
        if batch.confirmed:
            notify = NotificationService(db)
            from app.models.enums import EarningStatus
            from app.models.money import PublisherEarning

            recent = db.scalars(
                select(PublisherEarning).where(
                    PublisherEarning.status == EarningStatus.CONFIRMED,
                    PublisherEarning.confirmed_at >= utcnow() - timedelta(minutes=90),
                )
            ).all()
            for earning in recent:
                notify.queue_for_publisher(
                    earning.publisher_id,
                    "earnings_confirmed",
                    {"amount": str(earning.net_amount), "currency": earning.currency},
                    dedupe_key=f"earnings_confirmed:{earning.id}",
                )
        return {"confirmed": batch.confirmed, "held": batch.held, "amount": str(batch.amount)}


@celery_app.task(name="adnet.money.complete_expired_campaigns", ignore_result=True)
def complete_expired_campaigns() -> dict:
    from app.services.campaigns import CampaignService

    with session_scope() as db:
        return {"completed": CampaignService(db).complete_expired()}


@celery_app.task(name="adnet.money.verify_ledger", ignore_result=True)
def verify_ledger(currency: str | None = None) -> dict:
    """Prove the books balance, and shout if they do not.

    A silent imbalance is the worst possible failure for a payments system, so this
    runs hourly and notifies admins rather than only logging.
    """
    from app.core.config import settings
    from app.services.ledger import LedgerService
    from app.services.notifications import NotificationService

    currency = currency or settings.default_currency
    with session_scope() as db:
        balance = LedgerService(db).trial_balance(currency)
        if balance["difference"] != ZERO:
            log.error("LEDGER IMBALANCE %s: %s", currency, balance)
            NotificationService(db).queue_for_admins(
                "admin_system_error",
                {
                    "component": "ledger",
                    "error": f"trial balance difference {balance['difference']} {currency}",
                },
                dedupe_key=f"ledger-imbalance:{utcnow():%Y-%m-%dT%H}",
            )
        return {k: str(v) for k, v in balance.items()}


@celery_app.task(name="adnet.money.build_snapshot", ignore_result=True)
def build_snapshot(day: str | None = None) -> dict:
    """Aggregate yesterday's finances (spec §33). Idempotent."""
    from datetime import date

    from app.services.analytics import AnalyticsService

    target = date.fromisoformat(day) if day else (utcnow().date() - timedelta(days=1))
    with session_scope() as db:
        row = AnalyticsService(db).build_snapshot(target)
        return {
            "date": str(row.snapshot_date),
            "gross_ad_spend": str(row.gross_ad_spend),
            "publisher_payout": str(row.publisher_payout),
            "platform_revenue": str(row.platform_revenue),
        }


@celery_app.task(name="adnet.money.purge_idempotency", ignore_result=True)
def purge_idempotency() -> dict:
    from app.core.idempotency import purge_expired

    with session_scope() as db:
        return {"purged": purge_expired(db)}


@celery_app.task(name="adnet.money.low_balance_warnings", ignore_result=True)
def low_balance_warnings() -> dict:
    from app.services.campaigns import CampaignService
    from app.services.notifications import NotificationService
    from app.services.wallet import WalletService

    with session_scope() as db:
        campaigns = CampaignService(db).low_balance_warnings()
        notify = NotificationService(db)
        wallets = WalletService(db)
        for campaign in campaigns:
            wallet = wallets.for_advertiser(campaign.advertiser_id)
            notify.queue_for_advertiser(
                campaign.advertiser_id,
                "low_balance",
                {"available": str(wallet.available_balance), "currency": wallet.currency},
                dedupe_key=f"low_balance:{campaign.id}:{utcnow():%Y-%m-%d}",
            )
        return {"warned": len(campaigns)}


# --------------------------------------------------------------------------
# Fraud
# --------------------------------------------------------------------------


@celery_app.task(name="adnet.fraud.sweep_deliveries", ignore_result=True)
def sweep_deliveries(limit: int = 300) -> dict:
    from app.services.fraud import FraudService
    from app.services.notifications import NotificationService

    with session_scope() as db:
        events = FraudService(db).sweep_deliveries(limit)
        notify = NotificationService(db)
        for event in events:
            if event.band.value in {"suspicious", "high_risk"}:
                notify.queue_for_admins(
                    "admin_fraud_alert",
                    {
                        "band": event.band.value,
                        "score": event.score,
                        "subject_type": event.subject_type.value,
                        "subject_id": str(event.subject_id),
                        "signals": event.signal,
                    },
                    dedupe_key=f"fraud_alert:{event.id}",
                )
        return {"flagged": len(events)}


@celery_app.task(name="adnet.fraud.audit_channels", ignore_result=True)
def audit_channels(limit: int = 500) -> dict:
    from app.models.enums import ChannelStatus, FraudSubject
    from app.models.telegram import PublisherChannel
    from app.services.fraud import FraudService

    flagged = 0
    with session_scope() as db:
        channels = db.scalars(
            select(PublisherChannel)
            .where(PublisherChannel.status.in_([ChannelStatus.ACTIVE, ChannelStatus.VERIFIED]))
            .limit(limit)
        ).all()
        service = FraudService(db)
        for channel in channels:
            assessment = service.audit_channel(channel)
            event = service.record_event(
                FraudSubject.CHANNEL,
                channel.id,
                assessment,
                publisher_id=channel.publisher_id,
                channel_id=channel.id,
                amount_at_risk=channel.lifetime_earned,
            )
            if event is not None:
                flagged += 1
                if assessment.band.value == "high_risk":
                    # A case, not a ban: an admin decides (spec §16).
                    service.open_case(
                        FraudSubject.CHANNEL,
                        channel.id,
                        assessment,
                        f"Automated audit flagged {channel.telegram_chat_id}",
                        amount_held=channel.lifetime_earned,
                    )
        return {"audited": len(channels), "flagged": flagged}


@celery_app.task(name="adnet.fraud.reverse_earning", ignore_result=True)
def reverse_earning(earning_id: str, reason: str) -> dict:
    from app.services.earnings import EarningsService

    with session_scope() as db:
        service = EarningsService(db)
        earning = service.get(uuid.UUID(earning_id))
        reversed_ = service.reverse(earning, reason)
        return {"reversed": reversed_, "amount": str(earning.net_amount)}


# --------------------------------------------------------------------------
# Notifications and events
# --------------------------------------------------------------------------


@celery_app.task(name="adnet.notify.deliver_pending", ignore_result=True)
def deliver_pending(limit: int = 100) -> dict:
    """Send queued Telegram notifications (spec §27)."""
    from app.services.notifications import NotificationService

    with session_scope() as db:
        service = NotificationService(db)
        if service.pending_count() == 0:
            return {"sent": 0, "failed": 0}
        sent, failed = service.deliver_pending(gateway=_sync_gateway(), limit=limit)
        return {"sent": sent, "failed": failed}


def _sync_gateway():
    """The notification sender uses the synchronous HTTP gateway, not aiogram."""
    from app.services.telegram_gateway import get_gateway

    return get_gateway()


@celery_app.task(name="adnet.notify.dispatch_events", ignore_result=True)
def dispatch_events(limit: int = 500) -> dict:
    """Drain the event outbox (spec §30)."""
    from app.core import events as event_module

    with session_scope() as db:
        rows = event_module.pending(db, limit)
        for row in rows:
            try:
                _handle_event(db, row.name, row.payload)
                event_module.mark_dispatched(db, row)
            except Exception as exc:  # keep draining; the row is retried later
                log.exception("event_dispatch_failed name=%s", row.name)
                event_module.mark_dispatched(db, row, error=str(exc))
        return {"dispatched": len(rows)}


def _handle_event(db, name: str, payload: dict) -> None:
    """Fan an event out to the notifications that depend on it."""
    from app.core.events import Event
    from app.services.notifications import NotificationService

    notify = NotificationService(db)
    if name == Event.EARNING_CREATED:
        notify.queue_for_publisher(
            uuid.UUID(payload["publisher_id"]),
            "earnings_generated",
            {
                "amount": payload.get("amount", "0"),
                "currency": payload.get("currency", "BDT"),
                "impressions": payload.get("impressions", 0),
            },
            dedupe_key=f"earnings_generated:{payload.get('earning_id')}",
        )
    elif name == Event.AD_DELIVERED:
        notify.queue_for_publisher(
            uuid.UUID(payload["publisher_id"]),
            "ad_served",
            {
                "channel_title": payload.get("channel_title", "your channel"),
                "publisher_cpm": payload.get("publisher_cpm", "0"),
                "currency": payload.get("currency", "BDT"),
            },
            dedupe_key=f"ad_served:{payload.get('delivery_id')}",
        )
    elif name == Event.WITHDRAWAL_CREATED:
        from app.core.config import settings
        from app.core.money import D
        from app.services.settings_service import SettingsService

        threshold = SettingsService(db).money("large_withdrawal_alert")
        if D(payload.get("amount", "0")) >= threshold:
            notify.queue_for_admins(
                "admin_large_withdrawal",
                {
                    "amount": payload.get("amount", "0"),
                    "currency": payload.get("currency", settings.default_currency),
                    "publisher_id": payload.get("publisher_id"),
                    "fraud_score": payload.get("fraud_score", 0),
                },
                dedupe_key=f"large_withdrawal:{payload.get('withdrawal_id')}",
            )
