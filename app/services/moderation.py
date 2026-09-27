"""Reports and moderation queues (spec §19, §1 moderator duties).

A report is a signal from a person, so it is treated differently from a fraud
score: it is never acted on automatically. A moderator upholds or dismisses it, and
upholding a report on a running ad suspends the campaign immediately — the point of
a report is that harm is already in front of an audience.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.base import utcnow
from app.models.campaigns import Advertisement, Campaign
from app.models.enums import (
    ReportReason,
    ReportStatus,
    ReviewDecision,
    ReviewTarget,
)
from app.models.ops import ModerationReview, Report
from app.models.telegram import PublisherChannel
from app.services.audit import Actor, AuditService

#: Reasons severe enough that upholding one suspends the campaign at once rather
#: than leaving it running pending further review.
SEVERE = frozenset(
    {
        ReportReason.SCAM,
        ReportReason.MALWARE,
        ReportReason.ADULT,
        ReportReason.ILLEGAL,
        ReportReason.IMPERSONATION,
    }
)


@dataclass(frozen=True)
class ReportOutcome:
    report: Report
    campaign_suspended: bool = False
    channel_suspended: bool = False


class ModerationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    # -- filing -------------------------------------------------------------

    def file_report(
        self,
        *,
        target_type: ReviewTarget,
        target_id: uuid.UUID,
        reason: ReportReason,
        details: str | None = None,
        reporter_user_id: uuid.UUID | None = None,
        reporter_telegram_id: int | None = None,
        delivery_id: uuid.UUID | None = None,
    ) -> Report:
        """Record a report. Anyone who saw the ad may file one."""
        self._assert_target_exists(target_type, target_id)

        # One open report per reporter per target: a repeat submission is the same
        # complaint, not extra evidence.
        if reporter_telegram_id or reporter_user_id:
            existing = self.session.scalars(
                select(Report).where(
                    Report.target_type == target_type,
                    Report.target_id == target_id,
                    Report.status.in_([ReportStatus.OPEN, ReportStatus.REVIEWING]),
                    (Report.reporter_telegram_id == reporter_telegram_id)
                    if reporter_telegram_id
                    else (Report.reporter_user_id == reporter_user_id),
                )
            ).first()
            if existing is not None:
                return existing

        report = Report(
            reporter_user_id=reporter_user_id,
            reporter_telegram_id=reporter_telegram_id,
            target_type=target_type,
            target_id=target_id,
            delivery_id=delivery_id,
            reason=reason,
            details=(details or "").strip()[:2000] or None,
            status=ReportStatus.OPEN,
        )
        self.session.add(report)
        self.session.flush()

        self._notify_admins(report)
        return report

    def _assert_target_exists(self, target_type: ReviewTarget, target_id: uuid.UUID) -> None:
        model = {
            ReviewTarget.CAMPAIGN: Campaign,
            ReviewTarget.ADVERTISEMENT: Advertisement,
            ReviewTarget.CHANNEL: PublisherChannel,
        }[target_type]
        if self.session.get(model, target_id) is None:
            raise NotFound(f"{target_type.value} not found")

    def _notify_admins(self, report: Report) -> None:
        from app.services.notifications import NotificationService

        name = "unknown"
        if report.target_type is ReviewTarget.CAMPAIGN:
            campaign = self.session.get(Campaign, report.target_id)
            name = campaign.name if campaign else name
        elif report.target_type is ReviewTarget.ADVERTISEMENT:
            ad = self.session.get(Advertisement, report.target_id)
            campaign = self.session.get(Campaign, ad.campaign_id) if ad else None
            name = campaign.name if campaign else name
        elif report.target_type is ReviewTarget.CHANNEL:
            channel = self.session.get(PublisherChannel, report.target_id)
            name = (channel.chat.title if channel and channel.chat else name) or name

        NotificationService(self.session).queue_for_admins(
            "admin_campaign_report",
            {"reason": report.reason.value, "campaign_name": name},
            dedupe_key=f"report:{report.id}",
        )

    # -- handling -----------------------------------------------------------

    def start_review(self, report: Report, actor: Actor) -> Report:
        if report.status is not ReportStatus.OPEN:
            raise Conflict(f"a {report.status.value} report cannot be picked up")
        report.status = ReportStatus.REVIEWING
        report.handled_by_staff_id = _staff_id(actor)
        self.session.flush()
        return report

    def uphold(self, report: Report, actor: Actor, resolution: str) -> ReportOutcome:
        """Agree with the reporter and act on it."""
        if report.status.name in {"UPHELD", "DISMISSED"}:
            raise Conflict(f"this report is already {report.status.value}")
        if not resolution.strip():
            raise ValidationFailed("a resolution note is required")

        campaign_suspended = channel_suspended = False
        severe = report.reason in SEVERE

        if report.target_type in (ReviewTarget.CAMPAIGN, ReviewTarget.ADVERTISEMENT):
            campaign = self._campaign_for(report)
            if campaign is not None and severe and not campaign.status.is_terminal:
                from app.services.campaigns import CampaignService

                CampaignService(self.session).suspend(
                    campaign, actor, f"upheld report: {report.reason.value}"
                )
                campaign_suspended = True
        elif report.target_type is ReviewTarget.CHANNEL and severe:
            from app.models.enums import ChannelStatus

            channel = self.session.get(PublisherChannel, report.target_id)
            if channel is not None and channel.status is not ChannelStatus.SUSPENDED:
                channel.status = ChannelStatus.SUSPENDED
                channel.rejection_reason = f"upheld report: {report.reason.value}"
                channel_suspended = True

        report.status = ReportStatus.UPHELD
        report.resolution = resolution[:500]
        report.handled_by_staff_id = _staff_id(actor)
        report.handled_at = utcnow()
        self.session.flush()

        self.audit.log(
            actor,
            "report.upheld",
            target_type="report",
            target_id=report.id,
            old_value={"status": "open"},
            new_value={
                "status": "upheld",
                "campaign_suspended": campaign_suspended,
                "channel_suspended": channel_suspended,
            },
            reason=resolution,
        )
        return ReportOutcome(report, campaign_suspended, channel_suspended)

    def dismiss(self, report: Report, actor: Actor, resolution: str) -> Report:
        if report.status.name in {"UPHELD", "DISMISSED"}:
            raise Conflict(f"this report is already {report.status.value}")
        if not resolution.strip():
            raise ValidationFailed("a resolution note is required")
        report.status = ReportStatus.DISMISSED
        report.resolution = resolution[:500]
        report.handled_by_staff_id = _staff_id(actor)
        report.handled_at = utcnow()
        self.session.flush()
        self.audit.log(
            actor,
            "report.dismissed",
            target_type="report",
            target_id=report.id,
            new_value={"status": "dismissed"},
            reason=resolution,
        )
        return report

    def escalate(self, review: ModerationReview, actor: Actor, note: str) -> ModerationReview:
        """A moderator hands a financial or fraud question to an admin (spec §1)."""
        review.decision = ReviewDecision.ESCALATED
        review.notes = note[:2000]
        review.staff_id = _staff_id(actor)
        review.decided_at = utcnow()
        self.session.flush()
        self.audit.log(
            actor,
            "review.escalated",
            target_type=review.target_type.value,
            target_id=review.target_id,
            new_value={"decision": "escalated"},
            reason=note,
        )
        return review

    # -- queries ------------------------------------------------------------

    def _campaign_for(self, report: Report) -> Campaign | None:
        if report.target_type is ReviewTarget.CAMPAIGN:
            return self.session.get(Campaign, report.target_id)
        ad = self.session.get(Advertisement, report.target_id)
        return self.session.get(Campaign, ad.campaign_id) if ad else None

    def open_reports(self, limit: int = 100) -> list[Report]:
        return list(
            self.session.scalars(
                select(Report)
                .where(Report.status.in_([ReportStatus.OPEN, ReportStatus.REVIEWING]))
                .order_by(Report.created_at)
                .limit(limit)
            ).all()
        )

    def all_reports(self, limit: int = 100, offset: int = 0) -> list[Report]:
        return list(
            self.session.scalars(
                select(Report).order_by(Report.created_at.desc()).limit(limit).offset(offset)
            ).all()
        )

    def report_counts(self, target_type: ReviewTarget, target_id: uuid.UUID) -> dict:
        """How often this target has been reported, and upheld. Feeds moderation."""
        total = int(
            self.session.scalar(
                select(func.count(Report.id)).where(
                    Report.target_type == target_type, Report.target_id == target_id
                )
            )
            or 0
        )
        upheld = int(
            self.session.scalar(
                select(func.count(Report.id)).where(
                    Report.target_type == target_type,
                    Report.target_id == target_id,
                    Report.status == ReportStatus.UPHELD,
                )
            )
            or 0
        )
        return {"reports": total, "upheld": upheld}


def _staff_id(actor: Actor):
    if actor.type != "staff" or not actor.id:
        return None
    try:
        return uuid.UUID(actor.id)
    except (ValueError, TypeError):  # pragma: no cover
        return None
