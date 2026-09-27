"""Telegram notifications (spec §27).

Every notification is a row first, delivered by a worker second, and deduplicated
on ``dedupe_key`` so a retried worker or a replayed event cannot spam a user.
Templates are plain functions, so a missing key fails loudly in tests rather than
sending a half-rendered message.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.money import fmt
from app.db.base import utcnow
from app.models.enums import NotificationChannel, NotificationStatus
from app.models.identity import Publisher, StaffUser, User
from app.models.ops import Notification

log = get_logger(__name__)

Renderer = Callable[[dict], str]


def _money(payload: dict, key: str) -> str:
    return fmt(payload.get(key, "0"), payload.get("currency", "BDT"))


#: template name -> renderer. Advertiser, publisher and admin sets per spec §27.
TEMPLATES: dict[str, Renderer] = {
    # --- advertiser ---
    "campaign_approved": lambda p: (
        f"✅ Your campaign <b>{p['campaign_name']}</b> has been approved and is now live."
    ),
    "campaign_rejected": lambda p: (
        f"❌ Your campaign <b>{p['campaign_name']}</b> was not approved.\n"
        f"Reason: {p.get('reason', 'not specified')}\n\n"
        "You can edit it and submit it again."
    ),
    "campaign_started": lambda p: (
        f"🚀 Campaign <b>{p['campaign_name']}</b> has started delivering."
    ),
    "campaign_paused": lambda p: (
        f"⏸ Campaign <b>{p['campaign_name']}</b> is paused.\n"
        f"Reason: {p.get('reason', 'not specified')}"
    ),
    "campaign_completed": lambda p: (
        f"🏁 Campaign <b>{p['campaign_name']}</b> has finished.\n"
        f"Impressions: {p.get('impressions', 0):,}\n"
        f"Spent: {_money(p, 'spent')}"
    ),
    "low_balance": lambda p: (
        f"⚠️ Low wallet balance: {_money(p, 'available')}.\n"
        "Top up to keep your campaigns delivering."
    ),
    "deposit_confirmed": lambda p: (
        f"💰 Deposit received: {_money(p, 'amount')}.\nAvailable balance: {_money(p, 'available')}"
    ),
    "refund_processed": lambda p: (
        f"↩️ Refund of {_money(p, 'amount')} has been returned to your wallet."
    ),
    # --- publisher ---
    "channel_approved": lambda p: (
        f"✅ <b>{p['channel_title']}</b> is verified and can now receive ads."
    ),
    "channel_rejected": lambda p: (
        f"❌ <b>{p['channel_title']}</b> was not approved.\n"
        f"Reason: {p.get('reason', 'not specified')}"
    ),
    "channel_suspended": lambda p: (
        f"🚫 <b>{p['channel_title']}</b> has been suspended.\n"
        f"Reason: {p.get('reason', 'contact support')}"
    ),
    "ad_served": lambda p: (
        f"📢 An ad was posted to <b>{p['channel_title']}</b>.\n"
        f"Rate: {_money(p, 'publisher_cpm')} CPM"
    ),
    "earnings_generated": lambda p: (
        f"📈 You earned {_money(p, 'amount')} from {p.get('impressions', 0):,} "
        f"billable impressions.\nThis is pending until validation completes."
    ),
    "earnings_confirmed": lambda p: (
        f"✅ {_money(p, 'amount')} of earnings is confirmed and available to withdraw."
    ),
    "withdrawal_submitted": lambda p: (
        f"📤 Withdrawal request received.\n"
        f"Amount: {_money(p, 'amount')}\nFee: {_money(p, 'fee')}\n"
        f"You will receive: {_money(p, 'net')}\nStatus: pending review"
    ),
    "withdrawal_paid": lambda p: (
        f"✅ Withdrawal paid: {_money(p, 'net')} sent to {p.get('destination', 'your account')}.\n"
        f"Reference: {p.get('reference', '-')}"
    ),
    "withdrawal_rejected": lambda p: (
        f"❌ Your withdrawal of {_money(p, 'amount')} was rejected and the full "
        f"amount returned to your balance.\nReason: {p.get('reason', 'not specified')}"
    ),
    # --- admin ---
    "admin_large_withdrawal": lambda p: (
        f"🔔 Large withdrawal: {_money(p, 'amount')} by publisher {p.get('publisher_id')}.\n"
        f"Fraud score: {p.get('fraud_score', 0)}"
    ),
    "admin_fraud_alert": lambda p: (
        f"🚨 Fraud alert [{p.get('band', 'unknown')}] score {p.get('score', 0)}\n"
        f"Subject: {p.get('subject_type')} {p.get('subject_id')}\n"
        f"Signals: {p.get('signals', '-')}"
    ),
    "admin_campaign_report": lambda p: (
        f"⚠️ Campaign reported for {p.get('reason')}.\nCampaign: {p.get('campaign_name')}"
    ),
    "admin_payment_failure": lambda p: (
        f"❗ Payment failure on deposit {p.get('deposit_id')}: {p.get('reason')}"
    ),
    "admin_system_error": lambda p: f"💥 System error in {p.get('component')}: {p.get('error')}",
}


@dataclass(frozen=True)
class Recipient:
    user_id: uuid.UUID | None = None
    telegram_user_id: int | None = None
    staff_id: uuid.UUID | None = None


class NotificationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- queueing ----------------------------------------------------------

    def queue(
        self,
        template: str,
        recipient: Recipient,
        payload: dict,
        *,
        dedupe_key: str | None = None,
        channel: NotificationChannel = NotificationChannel.TELEGRAM,
    ) -> Notification | None:
        """Queue a notification. Returns None when deduplicated."""
        renderer = TEMPLATES.get(template)
        if renderer is None:
            raise KeyError(f"unknown notification template {template!r}")
        try:
            rendered = renderer(payload)
        except KeyError as exc:
            # Fail loudly rather than sending a half-rendered message.
            raise KeyError(f"template {template!r} needs payload key {exc}") from exc

        if recipient.telegram_user_id is None and recipient.user_id is not None:
            user = self.session.get(User, recipient.user_id)
            if user is not None:
                recipient = Recipient(user_id=user.id, telegram_user_id=user.telegram_user_id)

        row = Notification(
            user_id=recipient.user_id,
            telegram_user_id=recipient.telegram_user_id,
            staff_id=recipient.staff_id,
            channel=channel,
            template=template,
            payload=payload,
            rendered_text=rendered,
            status=NotificationStatus.QUEUED,
            dedupe_key=dedupe_key,
            created_at=utcnow(),
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            return None  # already queued for this dedupe key
        return row

    def queue_for_publisher(
        self, publisher_id: uuid.UUID, template: str, payload: dict, **kw
    ) -> Notification | None:
        publisher = self.session.get(Publisher, publisher_id)
        if publisher is None:
            return None
        return self.queue(template, Recipient(user_id=publisher.user_id), payload, **kw)

    def queue_for_advertiser(
        self, advertiser_id: uuid.UUID, template: str, payload: dict, **kw
    ) -> Notification | None:
        from app.models.identity import Advertiser

        advertiser = self.session.get(Advertiser, advertiser_id)
        if advertiser is None:
            return None
        return self.queue(template, Recipient(user_id=advertiser.user_id), payload, **kw)

    def queue_for_admins(self, template: str, payload: dict, **kw) -> list[Notification]:
        staff = self.session.scalars(
            select(StaffUser).where(
                StaffUser.is_active.is_(True), StaffUser.telegram_user_id.is_not(None)
            )
        ).all()
        out = []
        base_key = kw.pop("dedupe_key", None)
        for member in staff:
            row = self.queue(
                template,
                Recipient(staff_id=member.id, telegram_user_id=member.telegram_user_id),
                payload,
                dedupe_key=f"{base_key}:{member.id}" if base_key else None,
                **kw,
            )
            if row is not None:
                out.append(row)
        return out

    # -- delivery ----------------------------------------------------------

    def deliver_pending(self, gateway=None, limit: int = 100) -> tuple[int, int]:
        """Send queued notifications. Returns ``(sent, failed)``."""
        from app.services.telegram_gateway import TelegramError, get_gateway

        gateway = gateway or get_gateway()
        rows = self.session.scalars(
            select(Notification)
            .where(Notification.status == NotificationStatus.QUEUED)
            .order_by(Notification.created_at)
            .limit(limit)
        ).all()
        sent = failed = 0
        for row in rows:
            if not row.telegram_user_id:
                row.status = NotificationStatus.SUPPRESSED
                row.last_error = "no telegram id on record"
                continue
            row.attempts += 1
            try:
                gateway.send_text(row.telegram_user_id, row.rendered_text or "")
                row.status = NotificationStatus.SENT
                row.sent_at = utcnow()
                sent += 1
            except TelegramError as exc:
                message = str(exc)
                row.last_error = message[:500]
                # A user who blocked the bot will never succeed; stop retrying.
                if "blocked" in message.lower() or "chat not found" in message.lower():
                    row.status = NotificationStatus.SUPPRESSED
                elif row.attempts >= 5:
                    row.status = NotificationStatus.FAILED
                failed += 1
        self.session.flush()
        return sent, failed

    def pending_count(self) -> int:
        from sqlalchemy import func

        return int(
            self.session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.status == NotificationStatus.QUEUED
                )
            )
            or 0
        )
