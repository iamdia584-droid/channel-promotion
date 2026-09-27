"""Celery application and beat schedule (spec §29).

Nothing heavy runs in the bot or API request path. Delivery, settlement, fraud
analysis, notifications and reporting all happen here, on named queues so a slow
analytics job cannot delay a payout.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery("adnet", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    # A task that dies mid-flight is redelivered. Every financial task is
    # idempotent on a ledger key, so redelivery is safe rather than dangerous.
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=600,
    task_soft_time_limit=540,
    result_expires=3600,
    task_default_queue="default",
    task_routes={
        "adnet.delivery.*": {"queue": "delivery"},
        "adnet.money.*": {"queue": "money"},
        "adnet.fraud.*": {"queue": "fraud"},
        "adnet.notify.*": {"queue": "notify"},
    },
    beat_schedule={
        # --- delivery ---
        "serve-due-channels": {
            "task": "adnet.delivery.serve_due_channels",
            "schedule": crontab(minute="*/5"),
        },
        "remove-expired-posts": {
            "task": "adnet.delivery.remove_expired_posts",
            "schedule": crontab(minute=17, hour="*/6"),
        },
        # --- money ---
        "settle-measured-deliveries": {
            "task": "adnet.money.settle_due",
            "schedule": crontab(minute="*/10"),
        },
        "confirm-due-earnings": {
            "task": "adnet.money.confirm_earnings",
            "schedule": crontab(minute=5, hour="*"),
        },
        "complete-expired-campaigns": {
            "task": "adnet.money.complete_expired_campaigns",
            "schedule": crontab(minute=25, hour="*"),
        },
        "verify-ledger-integrity": {
            "task": "adnet.money.verify_ledger",
            "schedule": crontab(minute=45, hour="*"),
        },
        "build-daily-snapshot": {
            "task": "adnet.money.build_snapshot",
            "schedule": crontab(minute=20, hour=1),
        },
        # --- trust ---
        "sweep-delivery-fraud": {
            "task": "adnet.fraud.sweep_deliveries",
            "schedule": crontab(minute=35, hour="*"),
        },
        "audit-channels": {
            "task": "adnet.fraud.audit_channels",
            "schedule": crontab(minute=50, hour=3),
        },
        # --- housekeeping ---
        "refresh-channel-stats": {
            "task": "adnet.delivery.refresh_channel_stats",
            "schedule": crontab(minute=10, hour=2),
        },
        "deliver-notifications": {
            "task": "adnet.notify.deliver_pending",
            "schedule": crontab(minute="*/2"),
        },
        "dispatch-events": {
            "task": "adnet.notify.dispatch_events",
            "schedule": crontab(minute="*/5"),
        },
        "low-balance-warnings": {
            "task": "adnet.money.low_balance_warnings",
            "schedule": crontab(minute=55, hour="*/4"),
        },
        "purge-idempotency-records": {
            "task": "adnet.money.purge_idempotency",
            "schedule": crontab(minute=40, hour=4),
        },
    },
)

configure_logging()

# Importing the task modules registers them with this app.
celery_app.autodiscover_tasks(["app.workers"], related_name="tasks", force=True)
from app.workers import tasks  # noqa: F401
