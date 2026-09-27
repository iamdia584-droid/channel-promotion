"""Campaign lifecycle, validation and moderation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.errors import (
    Conflict,
    InsufficientFunds,
    NotFound,
    ValidationFailed,
)
from app.core.money import q
from app.db.base import utcnow
from app.models.enums import AdStatus, CampaignStatus, CampaignType, ReviewDecision
from app.models.ops import EventLog, ModerationReview, Notification
from app.services.audit import Actor
from app.services.campaigns import CampaignDraft, CampaignService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


def _draft(**kw) -> CampaignDraft:
    now = utcnow()
    defaults: dict = dict(  # noqa: C408 - kwargs style keeps the call readable
        name="Exam Preparation 2026",
        campaign_type=CampaignType.TEXT,
        total_budget=Decimal("10000"),
        daily_budget=Decimal("2000"),
        bid_cpm=Decimal("50"),
        starts_at=now,
        ends_at=now + timedelta(days=7),
        body_text="Join our exam prep course",
        destination_url="https://example.com/course",
        cta_text="Enrol now",
        countries=["BD"],
        categories=["education"],
        languages=["bn"],
    )
    defaults.update(kw)
    return CampaignDraft(**defaults)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_creating_a_campaign_stores_targeting_and_creative(db, make_advertiser):
    advertiser = make_advertiser()
    campaign = CampaignService(db).create(advertiser, _draft())

    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.currency == "BDT"
    assert campaign.target.countries == ["BD"]
    assert campaign.target.categories == ["education"]
    assert len(campaign.ads) == 1
    assert campaign.ads[0].status is AdStatus.DRAFT
    assert campaign.ads[0].cta_text == "Enrol now"
    assert advertiser.campaigns_created == 1


def test_campaign_created_event_is_emitted(db, make_advertiser):
    CampaignService(db).create(make_advertiser(), _draft())
    event = db.query(EventLog).filter(EventLog.name == "campaign.created").one()
    assert event.aggregate_type == "campaign"


@pytest.mark.parametrize(
    "kw,message",
    [
        ({"name": "  "}, "campaign name is required"),
        ({"total_budget": Decimal("10")}, "minimum campaign budget"),
        ({"daily_budget": Decimal("1")}, "minimum daily budget"),
        ({"daily_budget": Decimal("99999")}, "daily budget cannot exceed"),
        ({"bid_cpm": Decimal("0.5")}, "minimum CPM bid"),
        ({"bid_cpm": Decimal("999999")}, "maximum CPM bid"),
        ({"body_text": None, "destination_url": None}, "needs text, an image or a video"),
        ({"priority": 99}, "priority must be between"),
        ({"min_members": 100, "max_members": 10}, "members range is inverted"),
        ({"min_avg_views": 100, "max_avg_views": 10}, "average views range is inverted"),
    ],
)
def test_invalid_drafts_are_refused(db, make_advertiser, kw, message):
    with pytest.raises(ValidationFailed, match=message):
        CampaignService(db).create(make_advertiser(), _draft(**kw))


def test_inverted_schedule_is_refused(db, make_advertiser):
    now = utcnow()
    with pytest.raises(ValidationFailed, match="end time must be after"):
        CampaignService(db).create(
            make_advertiser(), _draft(starts_at=now, ends_at=now - timedelta(days=1))
        )


def test_past_schedule_is_refused(db, make_advertiser):
    now = utcnow()
    with pytest.raises(ValidationFailed, match="already in the past"):
        CampaignService(db).create(
            make_advertiser(),
            _draft(starts_at=now - timedelta(days=10), ends_at=now - timedelta(days=1)),
        )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/file",
        "//example.com",
        "https://example.com/a b",  # whitespace smuggling
        "https://example.com/\nhttps://evil.com",
    ],
)
def test_dangerous_destination_urls_are_refused(db, make_advertiser, url):
    """A malicious scheme or a smuggled second target must never be published."""
    with pytest.raises(ValidationFailed):
        CampaignService(db).create(make_advertiser(), _draft(destination_url=url))


@pytest.mark.parametrize(
    "url", ["https://example.com/x", "http://example.com", "https://t.me/mychannel"]
)
def test_safe_destination_urls_are_accepted(db, make_advertiser, url):
    assert CampaignService(db).create(make_advertiser(), _draft(destination_url=url)) is not None


def test_image_campaign_requires_media(db, make_advertiser):
    with pytest.raises(ValidationFailed, match="image ad needs media"):
        CampaignService(db).create(make_advertiser(), _draft(campaign_type=CampaignType.IMAGE))


def test_button_campaign_requires_a_destination(db, make_advertiser):
    with pytest.raises(ValidationFailed, match="button ad needs a destination"):
        CampaignService(db).create(
            make_advertiser(),
            _draft(campaign_type=CampaignType.BUTTON_LINK, destination_url=None),
        )


def test_suspended_advertiser_cannot_create_campaigns(db, make_advertiser):
    from app.models.enums import UserStatus

    advertiser = make_advertiser()
    advertiser.status = UserStatus.SUSPENDED
    db.flush()
    with pytest.raises(Exception, match="cannot create campaigns"):
        CampaignService(db).create(advertiser, _draft())


def test_budget_limits_are_configurable(db, make_advertiser):
    SettingsService(db).set("min_campaign_budget", "50000.000000")
    with pytest.raises(ValidationFailed, match="minimum campaign budget is 50000"):
        CampaignService(db).create(make_advertiser(), _draft())


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------


def test_submission_requires_sufficient_balance(db, make_advertiser):
    """Told now, not after a reviewer has spent time on it."""
    advertiser = make_advertiser()
    service = CampaignService(db)
    campaign = service.create(advertiser, _draft())
    with pytest.raises(InsufficientFunds, match="does not cover"):
        service.submit(campaign)


def test_submission_creates_a_moderation_review(db, make_advertiser, funded):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))

    assert campaign.status is CampaignStatus.SUBMITTED
    assert campaign.ads[0].status is AdStatus.SUBMITTED
    review = db.query(ModerationReview).one()
    assert review.decision is ReviewDecision.PENDING
    assert set(review.checklist) == {"text", "media", "url", "category", "targeting"}


def test_draft_budget_is_not_reserved_until_approval(db, make_advertiser, funded):
    """A campaign waiting in moderation must not tie up the advertiser's money."""
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    service.submit(service.create(advertiser, _draft()))
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.reserved_balance) == Decimal("0.000000")
    assert q(wallet.available_balance) == Decimal("20000.000000")


def test_auto_approve_setting_skips_moderation(db, make_advertiser, funded):
    advertiser = funded(make_advertiser(), "20000")
    SettingsService(db).set("campaign_auto_approve", "true")
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))
    assert campaign.status is CampaignStatus.RUNNING


def test_cannot_submit_twice(db, make_advertiser, funded):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))
    with pytest.raises(Conflict, match="cannot be submitted"):
        service.submit(campaign)


# --------------------------------------------------------------------------
# Moderation
# --------------------------------------------------------------------------


def test_approval_reserves_budget_and_starts_delivery(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))
    service.approve(campaign, Actor.staff(make_staff()))

    assert campaign.status is CampaignStatus.RUNNING
    assert campaign.approved_at is not None
    assert campaign.ads[0].status is AdStatus.APPROVED
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.reserved_balance) == Decimal("10000.000000")
    assert q(wallet.available_balance) == Decimal("10000.000000")
    assert all(v == 0 for v in WalletService(db).verify_against_ledger(wallet).values())


def test_approval_closes_the_review_and_notifies(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    staff = make_staff()
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))
    service.approve(campaign, Actor.staff(staff), note="looks fine")

    review = db.query(ModerationReview).one()
    assert review.decision is ReviewDecision.APPROVED
    assert review.staff_id == staff.id
    assert all(review.checklist.values())
    notes = db.query(Notification).filter(Notification.template == "campaign_approved").all()
    assert len(notes) == 1
    assert "approved" in notes[0].rendered_text


def test_approval_before_the_start_date_does_not_start_delivery(
    db, make_advertiser, funded, make_staff
):
    advertiser = funded(make_advertiser(), "20000")
    now = utcnow()
    service = CampaignService(db)
    campaign = service.create(
        advertiser, _draft(starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=9))
    )
    service.approve(service.submit(campaign), Actor.staff(make_staff()))
    assert campaign.status is CampaignStatus.APPROVED  # not RUNNING yet


def test_rejection_requires_a_reason_and_notifies(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))

    with pytest.raises(ValidationFailed, match="reason is required"):
        service.reject(campaign, Actor.staff(make_staff()), "  ")

    service.reject(campaign, Actor.staff(make_staff()), "landing page is a scam")
    assert campaign.status is CampaignStatus.REJECTED
    assert campaign.ads[0].rejection_reason == "landing page is a scam"
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert q(wallet.reserved_balance) == Decimal("0.000000")  # nothing was reserved
    note = db.query(Notification).filter(Notification.template == "campaign_rejected").one()
    assert "scam" in note.rendered_text


def test_rejected_campaign_can_be_resubmitted(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.submit(service.create(advertiser, _draft()))
    service.reject(campaign, Actor.staff(make_staff()), "needs a clearer CTA")

    service.submit(campaign)
    assert campaign.status is CampaignStatus.SUBMITTED
    assert campaign.ads[0].status is AdStatus.SUBMITTED
    assert campaign.ads[0].rejection_reason is None


def test_moderation_queue_lists_submitted_campaigns(db, make_advertiser, funded):
    service = CampaignService(db)
    for _ in range(3):
        advertiser = funded(make_advertiser(), "20000")
        service.submit(service.create(advertiser, _draft()))
    assert len(service.moderation_queue()) == 3


# --------------------------------------------------------------------------
# Running state
# --------------------------------------------------------------------------


def test_pause_and_resume(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.approve(
        service.submit(service.create(advertiser, _draft())), Actor.staff(make_staff())
    )
    service.pause(campaign, "budget review")
    assert campaign.status is CampaignStatus.PAUSED
    assert campaign.paused_reason == "budget review"

    service.resume(campaign)
    assert campaign.status is CampaignStatus.RUNNING
    assert campaign.paused_reason is None


def test_cannot_resume_an_expired_campaign(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.approve(
        service.submit(service.create(advertiser, _draft())), Actor.staff(make_staff())
    )
    service.pause(campaign)
    # Move the whole flight into the past: the ck_campaigns_schedule_ordered
    # CHECK correctly refuses an end date before the start date.
    campaign.starts_at = utcnow() - timedelta(days=3)
    campaign.ends_at = utcnow() - timedelta(hours=1)
    db.flush()
    with pytest.raises(Conflict, match="schedule has already ended"):
        service.resume(campaign)


def test_cannot_resume_an_exhausted_campaign(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.approve(
        service.submit(service.create(advertiser, _draft())), Actor.staff(make_staff())
    )
    service.pause(campaign)
    campaign.spent_amount = q(campaign.total_budget)
    db.flush()
    with pytest.raises(Conflict, match="no budget left"):
        service.resume(campaign)


def test_admin_suspension_stops_the_ads_too(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.approve(
        service.submit(service.create(advertiser, _draft())), Actor.staff(make_staff())
    )
    service.suspend(campaign, Actor.staff(make_staff()), "policy violation")
    assert campaign.status is CampaignStatus.SUSPENDED
    assert campaign.ads[0].status is AdStatus.SUSPENDED


def test_expired_campaigns_are_completed_and_notified(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.approve(
        service.submit(service.create(advertiser, _draft())), Actor.staff(make_staff())
    )
    campaign.starts_at = utcnow() - timedelta(days=2)
    campaign.ends_at = utcnow() - timedelta(minutes=1)
    db.flush()

    assert service.complete_expired() == 1
    assert campaign.status is CampaignStatus.COMPLETED
    assert db.query(Notification).filter(Notification.template == "campaign_completed").count() == 1


# --------------------------------------------------------------------------
# Reads and authorisation
# --------------------------------------------------------------------------


def test_another_advertisers_campaign_is_not_readable(db, make_advertiser):
    mine, theirs = make_advertiser(), make_advertiser()
    service = CampaignService(db)
    campaign = service.create(theirs, _draft())
    # Reported as not-found rather than forbidden: confirming existence would
    # leak that this campaign id is real.
    with pytest.raises(NotFound):
        service.get_owned(campaign.id, mine.id)
    assert service.get_owned(campaign.id, theirs.id).id == campaign.id


def test_preview_labels_impressions_as_an_estimate(db, make_advertiser):
    """Spec §3 step 7 and §37: the preview must not promise impressions."""
    campaign = CampaignService(db).create(make_advertiser(), _draft())
    preview = CampaignService(db).preview(campaign)
    assert preview["estimated_impressions"] == 200_000  # ৳10,000 at ৳50 CPM
    assert preview["estimate_is_not_a_guarantee"] is True
    assert preview["duration_days"] == 7


def test_stats_report_ctr_and_effective_cpm(db, sent_delivery):
    from app.models.enums import ImpressionKind, ImpressionSource

    from app.services.impressions import ImpressionService
    from app.services.settlement import SettlementService

    delivery, campaign, channel, advertiser = sent_delivery(
        avg_views=20_000, bid_cpm="100", commission="0.20"
    )
    impressions = ImpressionService(db)
    impressions.record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key="s1",
        quantity=10_000,
    )
    impressions.record_click(delivery, dedupe_key="c1")
    SettlementService(db).settle_delivery(delivery)

    stats = CampaignService(db).stats(campaign)
    assert stats["billable_impressions"] == 10_000
    assert stats["spent"] == Decimal("1000.000000")
    assert stats["effective_cpm"] == Decimal("100.000000")
    assert stats["clicks"] == 1
    assert stats["deliveries"] == 1
    assert stats["channels_reached"] == 1


def test_low_balance_warning_flags_nearly_spent_campaigns(db, make_advertiser, funded, make_staff):
    advertiser = funded(make_advertiser(), "20000")
    service = CampaignService(db)
    campaign = service.approve(
        service.submit(service.create(advertiser, _draft())), Actor.staff(make_staff())
    )
    assert service.low_balance_warnings() == []
    campaign.spent_amount = q("9500")  # 95% spent
    db.flush()
    assert campaign in service.low_balance_warnings()
