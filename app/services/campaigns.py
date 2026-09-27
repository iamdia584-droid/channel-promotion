"""Campaign lifecycle: draft → submitted → approved → running → completed.

Budget is reserved at approval, not at creation, so a campaign waiting in
moderation does not tie up an advertiser's money. Every transition is validated
against the current status rather than assumed, so a double-click cannot skip a
state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    Conflict,
    InsufficientFunds,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from app.core.events import Event, emit
from app.core.money import ZERO, D, impressions_for_budget, q
from app.db.base import utcnow
from app.models.campaigns import (
    Advertisement,
    Campaign,
    CampaignPublisher,
    CampaignTarget,
)
from app.models.enums import (
    AdStatus,
    CampaignStatus,
    CampaignType,
    PricingModel,
    ReviewDecision,
    ReviewTarget,
)
from app.models.identity import Advertiser
from app.models.ops import ModerationReview
from app.services.audit import Actor, AuditService
from app.services.notifications import NotificationService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService

#: URL schemes we will publish. Anything else is a phishing or malware vector.
ALLOWED_SCHEMES = ("https://", "http://", "https://t.me/", "tg://")


@dataclass
class CampaignDraft:
    """Validated input for creating a campaign (the bot wizard's output)."""

    name: str
    campaign_type: CampaignType
    total_budget: Decimal
    daily_budget: Decimal
    bid_cpm: Decimal
    starts_at: datetime
    ends_at: datetime
    body_text: str | None = None
    media_file_id: str | None = None
    media_url: str | None = None
    destination_url: str | None = None
    cta_text: str | None = None
    pricing_model: PricingModel = PricingModel.CPM
    countries: list[str] | None = None
    languages: list[str] | None = None
    categories: list[str] | None = None
    excluded_categories: list[str] | None = None
    audience_types: list[str] | None = None
    min_members: int | None = None
    max_members: int | None = None
    min_avg_views: int | None = None
    max_avg_views: int | None = None
    priority: int = 5
    max_impressions_per_user: int | None = None
    max_impressions_per_channel_per_day: int | None = None
    specific_channel_ids: list[uuid.UUID] | None = None


class CampaignService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = SettingsService(session)
        self.wallets = WalletService(session)
        self.audit = AuditService(session)
        self.notify = NotificationService(session)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create(self, advertiser: Advertiser, draft: CampaignDraft) -> Campaign:
        if not advertiser.is_active:
            raise PermissionDenied("this advertiser account cannot create campaigns")
        self._validate(draft)

        campaign = Campaign(
            advertiser_id=advertiser.id,
            name=draft.name.strip()[:200],
            campaign_type=draft.campaign_type,
            pricing_model=draft.pricing_model,
            currency=advertiser.currency,
            status=CampaignStatus.DRAFT,
            total_budget=q(draft.total_budget),
            daily_budget=q(draft.daily_budget),
            bid_cpm=q(draft.bid_cpm),
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            priority=draft.priority,
            max_impressions_per_user=draft.max_impressions_per_user,
            max_impressions_per_channel_per_day=draft.max_impressions_per_channel_per_day,
            allow_specific_channels=bool(draft.specific_channel_ids),
        )
        self.session.add(campaign)
        self.session.flush()

        self.session.add(
            CampaignTarget(
                campaign_id=campaign.id,
                countries=[c.upper() for c in (draft.countries or [])],
                languages=[x.lower() for x in (draft.languages or [])],
                categories=[x.lower() for x in (draft.categories or [])],
                excluded_categories=[x.lower() for x in (draft.excluded_categories or [])],
                audience_types=[x.lower() for x in (draft.audience_types or [])],
                min_members=draft.min_members,
                max_members=draft.max_members,
                min_avg_views=draft.min_avg_views,
                max_avg_views=draft.max_avg_views,
            )
        )
        self.session.add(
            Advertisement(
                campaign_id=campaign.id,
                status=AdStatus.DRAFT,
                ad_format=draft.campaign_type,
                body_text=draft.body_text,
                media_file_id=draft.media_file_id,
                media_url=draft.media_url,
                destination_url=draft.destination_url,
                cta_text=(draft.cta_text or "").strip()[:64] or None,
            )
        )
        for channel_id in draft.specific_channel_ids or []:
            self.session.add(
                CampaignPublisher(campaign_id=campaign.id, channel_id=channel_id, allowed=True)
            )
        advertiser.campaigns_created += 1
        self.session.flush()

        emit(
            self.session,
            Event.CAMPAIGN_CREATED,
            {
                "campaign_id": campaign.id,
                "advertiser_id": advertiser.id,
                "name": campaign.name,
                "total_budget": campaign.total_budget,
            },
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )
        return campaign

    def _validate(self, draft: CampaignDraft) -> None:
        if not (draft.name or "").strip():
            raise ValidationFailed("a campaign name is required")

        total = q(draft.total_budget)
        daily = q(draft.daily_budget)
        bid = q(draft.bid_cpm)

        min_total = self.settings.money("min_campaign_budget")
        min_daily = self.settings.money("min_daily_budget")
        if total < min_total:
            raise ValidationFailed(f"the minimum campaign budget is {min_total}")
        if daily < min_daily:
            raise ValidationFailed(f"the minimum daily budget is {min_daily}")
        if daily > total:
            raise ValidationFailed("the daily budget cannot exceed the total budget")

        min_cpm = self.settings.money("min_cpm")
        max_cpm = self.settings.money("max_cpm")
        if bid < min_cpm:
            raise ValidationFailed(f"the minimum CPM bid is {min_cpm}")
        if bid > max_cpm:
            raise ValidationFailed(f"the maximum CPM bid is {max_cpm}")

        if draft.ends_at <= draft.starts_at:
            raise ValidationFailed("the end time must be after the start time")
        if draft.ends_at <= utcnow():
            raise ValidationFailed("the end time is already in the past")
        if not (1 <= draft.priority <= 10):
            raise ValidationFailed("priority must be between 1 and 10")

        has_text = bool((draft.body_text or "").strip())
        has_media = bool(draft.media_file_id or draft.media_url)
        if not has_text and not has_media:
            raise ValidationFailed("the ad needs text, an image or a video")
        if draft.campaign_type in (CampaignType.IMAGE, CampaignType.VIDEO) and not has_media:
            raise ValidationFailed(f"a {draft.campaign_type.value} ad needs media")
        if draft.campaign_type is CampaignType.BUTTON_LINK and not draft.destination_url:
            raise ValidationFailed("a button ad needs a destination URL")

        if draft.destination_url:
            self._validate_url(draft.destination_url)
        if draft.body_text and len(draft.body_text) > 3500:
            raise ValidationFailed("the ad text is too long (3500 characters maximum)")

        for label, low, high in (
            ("members", draft.min_members, draft.max_members),
            ("average views", draft.min_avg_views, draft.max_avg_views),
        ):
            if low is not None and high is not None and low > high:
                raise ValidationFailed(f"the {label} range is inverted")
            for value in (low, high):
                if value is not None and value < 0:
                    raise ValidationFailed(f"{label} cannot be negative")

    @staticmethod
    def _validate_url(url: str) -> None:
        cleaned = url.strip()
        if not cleaned.lower().startswith(ALLOWED_SCHEMES):
            raise ValidationFailed("the destination URL must start with https:// or http://")
        if len(cleaned) > 2000:
            raise ValidationFailed("the destination URL is too long")
        # A URL containing whitespace or control characters is malformed and is a
        # classic way to smuggle a second target past a naive check.
        if any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
            raise ValidationFailed("the destination URL contains invalid characters")

    # ------------------------------------------------------------------
    # Submission and moderation
    # ------------------------------------------------------------------

    def submit(self, campaign: Campaign, actor: Actor | None = None) -> Campaign:
        """Send a draft for review, or auto-approve when configured."""
        if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.REJECTED):
            raise Conflict(f"a {campaign.status.value} campaign cannot be submitted")

        # Check funding up front so the advertiser learns now, not after review.
        wallet = self.wallets.for_advertiser(campaign.advertiser_id)
        if q(wallet.available_balance) < q(campaign.total_budget):
            raise InsufficientFunds(
                "your available balance does not cover this campaign budget",
                available=str(q(wallet.available_balance)),
                required=str(q(campaign.total_budget)),
            )

        campaign.status = CampaignStatus.SUBMITTED
        for ad in campaign.ads:
            if ad.status in (AdStatus.DRAFT, AdStatus.REJECTED):
                ad.status = AdStatus.SUBMITTED
                ad.rejection_reason = None
        self.session.flush()

        self.session.add(
            ModerationReview(
                target_type=ReviewTarget.CAMPAIGN,
                target_id=campaign.id,
                decision=ReviewDecision.PENDING,
                checklist={
                    "text": False,
                    "media": False,
                    "url": False,
                    "category": False,
                    "targeting": False,
                },
            )
        )
        emit(
            self.session,
            Event.AD_SUBMITTED,
            {"campaign_id": campaign.id},
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )

        if self.settings.bool_("campaign_auto_approve"):
            return self.approve(
                campaign,
                actor or Actor.system("auto-approve"),
                note="auto-approved by configuration",
            )
        return campaign

    def approve(self, campaign: Campaign, actor: Actor, note: str | None = None) -> Campaign:
        """Approve, reserve the budget, and start delivering if in window."""
        if campaign.status not in (CampaignStatus.SUBMITTED, CampaignStatus.UNDER_REVIEW):
            raise Conflict(f"a {campaign.status.value} campaign cannot be approved")

        old = campaign.status
        # Reserve now: from here on the money is committed to this campaign.
        self.wallets.reserve_budget(
            campaign.advertiser_id,
            campaign.id,
            campaign.total_budget,
            idempotency_key=f"campaign-reserve:{campaign.id}",
            description=f"Budget reserved for campaign {campaign.name}",
        )
        campaign.reserved_amount = q(campaign.total_budget)
        campaign.status = CampaignStatus.APPROVED
        campaign.approved_at = utcnow()
        campaign.paused_reason = None
        for ad in campaign.ads:
            if ad.status is AdStatus.SUBMITTED:
                ad.status = AdStatus.APPROVED
                ad.reviewed_at = utcnow()
                ad.reviewed_by = _staff_id(actor)
        self._close_review(campaign.id, ReviewDecision.APPROVED, actor, note)
        self.session.flush()

        self.audit.log(
            actor,
            "campaign.approved",
            target_type="campaign",
            target_id=campaign.id,
            old_value={"status": str(old)},
            new_value={"status": "approved"},
            reason=note,
        )
        emit(
            self.session,
            Event.CAMPAIGN_APPROVED,
            {"campaign_id": campaign.id, "name": campaign.name},
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )
        self.notify.queue_for_advertiser(
            campaign.advertiser_id,
            "campaign_approved",
            {"campaign_name": campaign.name},
            dedupe_key=f"campaign_approved:{campaign.id}",
        )

        now = utcnow()
        if campaign.starts_at <= now < campaign.ends_at:
            self.start(campaign)
        return campaign

    def reject(self, campaign: Campaign, actor: Actor, reason: str) -> Campaign:
        if campaign.status not in (CampaignStatus.SUBMITTED, CampaignStatus.UNDER_REVIEW):
            raise Conflict(f"a {campaign.status.value} campaign cannot be rejected")
        if not reason.strip():
            raise ValidationFailed("a rejection reason is required")

        old = campaign.status
        campaign.status = CampaignStatus.REJECTED
        for ad in campaign.ads:
            ad.status = AdStatus.REJECTED
            ad.rejection_reason = reason[:500]
            ad.reviewed_at = utcnow()
            ad.reviewed_by = _staff_id(actor)
        self._close_review(campaign.id, ReviewDecision.REJECTED, actor, reason)
        self.session.flush()

        self.audit.log(
            actor,
            "campaign.rejected",
            target_type="campaign",
            target_id=campaign.id,
            old_value={"status": str(old)},
            new_value={"status": "rejected"},
            reason=reason,
        )
        emit(
            self.session,
            Event.CAMPAIGN_REJECTED,
            {"campaign_id": campaign.id, "reason": reason},
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )
        self.notify.queue_for_advertiser(
            campaign.advertiser_id,
            "campaign_rejected",
            {"campaign_name": campaign.name, "reason": reason},
            dedupe_key=f"campaign_rejected:{campaign.id}",
        )
        return campaign

    def _close_review(
        self, campaign_id: uuid.UUID, decision: ReviewDecision, actor: Actor, note: str | None
    ) -> None:
        review = self.session.scalars(
            select(ModerationReview).where(
                ModerationReview.target_type == ReviewTarget.CAMPAIGN,
                ModerationReview.target_id == campaign_id,
                ModerationReview.decision == ReviewDecision.PENDING,
            )
        ).first()
        if review is None:
            return
        review.decision = decision
        review.staff_id = _staff_id(actor)
        review.reason = (note or "")[:500] or None
        review.decided_at = utcnow()
        review.checklist = dict.fromkeys(review.checklist or {}, True)

    # ------------------------------------------------------------------
    # Running state
    # ------------------------------------------------------------------

    def start(self, campaign: Campaign) -> Campaign:
        if campaign.status not in (CampaignStatus.APPROVED, CampaignStatus.PAUSED):
            raise Conflict(f"a {campaign.status.value} campaign cannot start")
        campaign.status = CampaignStatus.RUNNING
        campaign.paused_reason = None
        self.session.flush()
        emit(
            self.session,
            Event.CAMPAIGN_STARTED,
            {"campaign_id": campaign.id},
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )
        self.notify.queue_for_advertiser(
            campaign.advertiser_id,
            "campaign_started",
            {"campaign_name": campaign.name},
            dedupe_key=f"campaign_started:{campaign.id}",
        )
        return campaign

    def pause(
        self,
        campaign: Campaign,
        reason: str = "paused by advertiser",
        actor: Actor | None = None,
    ) -> Campaign:
        if campaign.status is not CampaignStatus.RUNNING:
            raise Conflict(f"a {campaign.status.value} campaign cannot be paused")
        campaign.status = CampaignStatus.PAUSED
        campaign.paused_reason = reason[:300]
        self.session.flush()
        self.audit.log(
            actor or Actor.system(),
            "campaign.paused",
            target_type="campaign",
            target_id=campaign.id,
            new_value={"status": "paused"},
            reason=reason,
        )
        emit(
            self.session,
            Event.CAMPAIGN_PAUSED,
            {"campaign_id": campaign.id, "reason": reason},
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )
        self.notify.queue_for_advertiser(
            campaign.advertiser_id,
            "campaign_paused",
            {"campaign_name": campaign.name, "reason": reason},
        )
        return campaign

    def resume(self, campaign: Campaign, actor: Actor | None = None) -> Campaign:
        if campaign.status is not CampaignStatus.PAUSED:
            raise Conflict(f"a {campaign.status.value} campaign cannot be resumed")
        if campaign.ends_at <= utcnow():
            raise Conflict("this campaign's schedule has already ended")
        if q(campaign.remaining_budget) <= ZERO:
            raise Conflict("this campaign has no budget left")
        campaign.status = CampaignStatus.RUNNING
        campaign.paused_reason = None
        self.session.flush()
        emit(
            self.session,
            Event.CAMPAIGN_RESUMED,
            {"campaign_id": campaign.id},
            aggregate_type="campaign",
            aggregate_id=campaign.id,
        )
        return campaign

    def suspend(self, campaign: Campaign, actor: Actor, reason: str) -> Campaign:
        """Admin stop — for policy violations, distinct from an advertiser pause."""
        if campaign.status.is_terminal:
            raise Conflict(f"campaign is already {campaign.status.value}")
        old = campaign.status
        campaign.status = CampaignStatus.SUSPENDED
        campaign.paused_reason = reason[:300]
        for ad in campaign.ads:
            ad.status = AdStatus.SUSPENDED
        self.session.flush()
        self.audit.log(
            actor,
            "campaign.suspended",
            target_type="campaign",
            target_id=campaign.id,
            old_value={"status": str(old)},
            new_value={"status": "suspended"},
            reason=reason,
        )
        return campaign

    def complete_expired(self, limit: int = 200) -> int:
        """Close campaigns whose schedule has ended or budget is spent."""
        now = utcnow()
        rows = self.session.scalars(
            select(Campaign)
            .where(Campaign.status.in_([CampaignStatus.RUNNING, CampaignStatus.APPROVED]))
            .limit(limit)
        ).all()
        closed = 0
        for campaign in rows:
            if campaign.ends_at > now and q(campaign.remaining_budget) > ZERO:
                continue
            campaign.status = CampaignStatus.COMPLETED
            campaign.completed_at = now
            closed += 1
            emit(
                self.session,
                Event.CAMPAIGN_COMPLETED,
                {
                    "campaign_id": campaign.id,
                    "impressions": campaign.billable_impressions,
                    "spent": campaign.spent_amount,
                    "currency": campaign.currency,
                },
                aggregate_type="campaign",
                aggregate_id=campaign.id,
            )
            self.notify.queue_for_advertiser(
                campaign.advertiser_id,
                "campaign_completed",
                {
                    "campaign_name": campaign.name,
                    "impressions": campaign.billable_impressions,
                    "spent": str(q(campaign.spent_amount)),
                    "currency": campaign.currency,
                },
                dedupe_key=f"campaign_completed:{campaign.id}",
            )
        self.session.flush()
        return closed

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def preview(self, campaign: Campaign) -> dict[str, object]:
        """The confirmation screen of the bot wizard (spec §3 step 7)."""
        estimated = impressions_for_budget(campaign.total_budget, campaign.bid_cpm)
        target = campaign.target
        days = max(1, (campaign.ends_at - campaign.starts_at).days)
        return {
            "name": campaign.name,
            "type": campaign.campaign_type.value,
            "currency": campaign.currency,
            "total_budget": q(campaign.total_budget),
            "daily_budget": q(campaign.daily_budget),
            "bid_cpm": q(campaign.bid_cpm),
            # Explicitly an estimate: actual billing depends on measured
            # impressions (docs/TELEGRAM_CONSTRAINTS.md).
            "estimated_impressions": estimated,
            "estimate_is_not_a_guarantee": True,
            "countries": (target.countries if target else []) or ["any"],
            "languages": (target.languages if target else []) or ["any"],
            "categories": (target.categories if target else []) or ["any"],
            "starts_at": campaign.starts_at,
            "ends_at": campaign.ends_at,
            "duration_days": days,
        }

    def list_for_advertiser(
        self, advertiser_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[Campaign]:
        return list(
            self.session.scalars(
                select(Campaign)
                .where(Campaign.advertiser_id == advertiser_id)
                .order_by(Campaign.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def moderation_queue(self, limit: int = 50) -> list[Campaign]:
        return list(
            self.session.scalars(
                select(Campaign)
                .where(Campaign.status.in_([CampaignStatus.SUBMITTED, CampaignStatus.UNDER_REVIEW]))
                .order_by(Campaign.created_at)
                .limit(limit)
            ).all()
        )

    def get_owned(self, campaign_id: uuid.UUID, advertiser_id: uuid.UUID) -> Campaign:
        """Fetch with an ownership check — never trust a client-supplied id alone."""
        campaign = self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        if campaign.advertiser_id != advertiser_id:
            # Deliberately the same error as "not found": confirming existence
            # would leak other advertisers' campaign ids.
            raise NotFound("campaign not found")
        return campaign

    def low_balance_warnings(self, threshold_ratio: str = "0.1") -> list[Campaign]:
        """Campaigns close to exhausting their budget, for a heads-up."""
        rows = self.session.scalars(
            select(Campaign).where(Campaign.status == CampaignStatus.RUNNING)
        ).all()
        ratio = D(threshold_ratio)
        return [
            c
            for c in rows
            if q(c.total_budget) > ZERO and q(c.remaining_budget) / q(c.total_budget) <= ratio
        ]

    def stats(self, campaign: Campaign) -> dict[str, object]:
        from app.core.money import pct
        from app.models.delivery import AdDelivery

        deliveries = int(
            self.session.scalar(
                select(func.count(AdDelivery.id)).where(
                    AdDelivery.campaign_id == campaign.id,
                    AdDelivery.sent_at.is_not(None),
                )
            )
            or 0
        )
        channels = int(
            self.session.scalar(
                select(func.count(func.distinct(AdDelivery.channel_id))).where(
                    AdDelivery.campaign_id == campaign.id,
                    AdDelivery.sent_at.is_not(None),
                )
            )
            or 0
        )
        impressions = campaign.billable_impressions
        spent = q(campaign.spent_amount)
        return {
            "status": campaign.status.value,
            "spent": spent,
            "remaining_budget": q(campaign.remaining_budget),
            "billable_impressions": impressions,
            "clicks": campaign.clicks,
            "ctr_percent": pct(campaign.clicks, impressions),
            "effective_cpm": q(spent * 1000 / D(impressions)) if impressions else ZERO,
            "cost_per_click": q(spent / D(campaign.clicks)) if campaign.clicks else ZERO,
            "deliveries": deliveries,
            "channels_reached": channels,
            "currency": campaign.currency,
        }


def _staff_id(actor: Actor):
    if actor.type != "staff" or not actor.id:
        return None
    try:
        return uuid.UUID(actor.id)
    except (ValueError, TypeError):  # pragma: no cover
        return None


def default_schedule(days: int = 7) -> tuple[datetime, datetime]:
    now = utcnow()
    return now, now + timedelta(days=days)
