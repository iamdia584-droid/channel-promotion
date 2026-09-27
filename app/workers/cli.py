"""Operational CLI: run a worker task by hand, bootstrap, seed, health-check.

python -m app.workers.cli bootstrap
python -m app.workers.cli serve
python -m app.workers.cli settle
python -m app.workers.cli snapshot [YYYY-MM-DD]
python -m app.workers.cli verify-ledger
python -m app.workers.cli set-webhook
python -m app.workers.cli seed-demo
"""

from __future__ import annotations

import sys

from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


def bootstrap() -> int:
    """Create setting rows and the first admin from the environment."""
    from app.admin.auth import bootstrap_admin
    from app.db.session import session_scope
    from app.services.settings_service import SettingsService

    with session_scope() as db:
        created = SettingsService(db).seed_defaults()
        staff = bootstrap_admin(db)
    print(f"settings created: {created}")
    if staff is None:
        print(
            "no admin created: set BOOTSTRAP_ADMIN_EMAIL and "
            "BOOTSTRAP_ADMIN_PASSWORD, then run again"
        )
    else:
        print(f"admin ready: {staff.email} (enable 2FA before production use)")
    return 0


def serve() -> int:
    from app.workers.tasks import serve_due_channels

    print(serve_due_channels())
    return 0


def settle() -> int:
    from app.workers.tasks import settle_due

    print(settle_due())
    return 0


def confirm() -> int:
    from app.workers.tasks import confirm_earnings

    print(confirm_earnings())
    return 0


def snapshot(day: str | None = None) -> int:
    from app.workers.tasks import build_snapshot

    print(build_snapshot(day))
    return 0


def verify_ledger() -> int:
    from app.workers.tasks import verify_ledger as task

    result = task()
    print(result)
    return 0 if result.get("difference") in {"0.000000", "0E-6", "0"} else 1


def fraud_sweep() -> int:
    from app.workers.tasks import audit_channels, sweep_deliveries

    print(sweep_deliveries())
    print(audit_channels())
    return 0


def notify() -> int:
    from app.workers.tasks import deliver_pending, dispatch_events

    print(dispatch_events())
    print(deliver_pending())
    return 0


def set_webhook() -> int:
    import asyncio

    from app.bot.dispatcher import set_commands
    from app.bot.dispatcher import set_webhook as register

    async def run():
        url = await register()
        await set_commands()
        return url

    print(f"webhook set to {asyncio.run(run())}")
    return 0


def seed_demo() -> int:
    """Create a small, internally consistent dataset for a local walkthrough.

    Deliberately not called by any migration or startup path: it writes real rows
    through the real services, so it must only ever be run deliberately.
    """
    from datetime import timedelta

    from app.api.v1.auth import ensure_advertiser, ensure_publisher, get_or_create_user
    from app.core.config import settings
    from app.core.money import D
    from app.db.base import utcnow
    from app.db.session import session_scope
    from app.models.enums import CampaignType, ChannelStatus, ChatType, VerificationStatus
    from app.models.telegram import PublisherChannel, TelegramChat
    from app.services.audit import Actor
    from app.services.campaigns import CampaignDraft, CampaignService
    from app.services.settings_service import SettingsService
    from app.services.wallet import WalletService

    if settings.is_production:
        print("refusing to seed demo data in production")
        return 1

    with session_scope() as db:
        SettingsService(db).seed_defaults()

        adv_user = get_or_create_user(
            db, 900_000_001, username="demo_advertiser", first_name="Demo"
        )
        advertiser = ensure_advertiser(db, adv_user)
        WalletService(db).credit_deposit(
            advertiser.id,
            "50000",
            idempotency_key="seed:deposit:1",
            description="Seed deposit",
        )

        pub_user = get_or_create_user(db, 900_000_002, username="demo_publisher", first_name="Demo")
        publisher = ensure_publisher(db, pub_user)

        chat = TelegramChat(
            telegram_chat_id=-100_900_000_003,
            chat_type=ChatType.CHANNEL,
            username="demo_exam_channel",
            title="Demo Exam Channel",
            member_count=48_000,
            bot_is_admin=True,
            bot_can_post=True,
            first_seen_at=utcnow(),
        )
        db.add(chat)
        db.flush()
        db.add(
            PublisherChannel(
                publisher_id=publisher.id,
                telegram_chat_id_ref=chat.id,
                telegram_chat_id=chat.telegram_chat_id,
                category="education",
                language="bn",
                country="BD",
                status=ChannelStatus.ACTIVE,
                verification_status=VerificationStatus.VERIFIED,
                verified_at=utcnow(),
                avg_views=19_500,
                median_views=18_800,
                quality_score=D("0.72"),
            )
        )
        db.flush()

        now = utcnow()
        service = CampaignService(db)
        campaign = service.create(
            advertiser,
            CampaignDraft(
                name="Exam Preparation 2026",
                campaign_type=CampaignType.TEXT,
                total_budget=D("10000"),
                daily_budget=D("2000"),
                bid_cpm=D("50"),
                starts_at=now,
                ends_at=now + timedelta(days=7),
                body_text="Exam Preparation 2026 — enrol before the deadline.",
                destination_url="https://example.com/exam-2026",
                cta_text="Enrol now",
                countries=["BD"],
                categories=["education"],
                languages=["bn"],
            ),
        )
        service.submit(campaign)
        service.approve(campaign, Actor.system("seed"), note="seeded")

    print("seeded: 1 advertiser (৳50,000), 1 publisher, 1 channel, 1 live campaign")
    print("run 'python -m app.workers.cli serve' to deliver an ad")
    return 0


COMMANDS = {
    "bootstrap": bootstrap,
    "serve": serve,
    "settle": settle,
    "confirm": confirm,
    "snapshot": snapshot,
    "verify-ledger": verify_ledger,
    "fraud-sweep": fraud_sweep,
    "notify": notify,
    "set-webhook": set_webhook,
    "seed-demo": seed_demo,
}


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        print("commands: " + ", ".join(sorted(COMMANDS)))
        return 0
    command, *args = argv
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"unknown command {command!r}\ncommands: {', '.join(sorted(COMMANDS))}")
        return 2
    return handler(*args)


if __name__ == "__main__":
    raise SystemExit(main())
