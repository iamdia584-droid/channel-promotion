"""Reports are a human signal: recorded, never acted on automatically (spec §19)."""

from __future__ import annotations

import pytest

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.enums import (
    AdStatus,
    CampaignStatus,
    ChannelStatus,
    ReportReason,
    ReportStatus,
    ReviewTarget,
)
from app.models.ops import AuditLog, Notification, Report
from app.services.audit import Actor
from app.services.moderation import ModerationService


def test_filing_a_report_records_it_and_alerts_staff(db, make_campaign, make_staff, make_user):
    make_staff(telegram_user_id=770_000_111)
    campaign = make_campaign()
    reporter = make_user()

    report = ModerationService(db).file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SCAM,
        details="The landing page asks for bKash PINs",
        reporter_user_id=reporter.id,
        reporter_telegram_id=reporter.telegram_user_id,
    )
    assert report.status is ReportStatus.OPEN
    assert report.reason is ReportReason.SCAM
    assert "bKash PIN" in report.details
    # The campaign keeps running until a human decides (spec §19).
    assert campaign.status is CampaignStatus.RUNNING

    alert = db.query(Notification).filter(Notification.template == "admin_campaign_report").one()
    assert "scam" in alert.rendered_text


def test_reporting_something_that_does_not_exist_is_refused(db):
    import uuid

    with pytest.raises(NotFound):
        ModerationService(db).file_report(
            target_type=ReviewTarget.CAMPAIGN,
            target_id=uuid.uuid4(),
            reason=ReportReason.SPAM,
        )


def test_the_same_reporter_filing_twice_does_not_duplicate(db, make_campaign, make_user):
    campaign = make_campaign()
    reporter = make_user()
    service = ModerationService(db)
    first = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SPAM,
        reporter_telegram_id=reporter.telegram_user_id,
    )
    second = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SPAM,
        reporter_telegram_id=reporter.telegram_user_id,
    )
    assert first.id == second.id
    assert db.query(Report).count() == 1


def test_different_reporters_each_count(db, make_campaign, make_user):
    campaign = make_campaign()
    service = ModerationService(db)
    for _ in range(3):
        service.file_report(
            target_type=ReviewTarget.CAMPAIGN,
            target_id=campaign.id,
            reason=ReportReason.MISLEADING,
            reporter_telegram_id=make_user().telegram_user_id,
        )
    assert db.query(Report).count() == 3
    counts = service.report_counts(ReviewTarget.CAMPAIGN, campaign.id)
    assert counts == {"reports": 3, "upheld": 0}


@pytest.mark.parametrize(
    "reason",
    [
        ReportReason.SCAM,
        ReportReason.MALWARE,
        ReportReason.ADULT,
        ReportReason.ILLEGAL,
        ReportReason.IMPERSONATION,
    ],
)
def test_upholding_a_severe_report_suspends_the_campaign_at_once(
    db, make_campaign, make_staff, reason
):
    """Harm is already in front of an audience, so severe means stop now."""
    campaign = make_campaign()
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN, target_id=campaign.id, reason=reason
    )
    outcome = service.uphold(
        report, Actor.staff(make_staff()), "confirmed by reviewing the landing page"
    )
    assert outcome.campaign_suspended is True
    assert campaign.status is CampaignStatus.SUSPENDED
    assert campaign.ads[0].status is AdStatus.SUSPENDED
    assert report.status is ReportStatus.UPHELD


@pytest.mark.parametrize(
    "reason",
    [ReportReason.SPAM, ReportReason.COPYRIGHT, ReportReason.MISLEADING, ReportReason.OTHER],
)
def test_upholding_a_lesser_report_does_not_auto_suspend(db, make_campaign, make_staff, reason):
    """A copyright or spam complaint deserves a human decision, not a kill switch."""
    campaign = make_campaign()
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN, target_id=campaign.id, reason=reason
    )
    outcome = service.uphold(report, Actor.staff(make_staff()), "noted for follow-up")
    assert outcome.campaign_suspended is False
    assert campaign.status is CampaignStatus.RUNNING
    assert report.status is ReportStatus.UPHELD


def test_upholding_a_severe_channel_report_suspends_the_channel(db, make_channel, make_staff):
    channel = make_channel()
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.CHANNEL,
        target_id=channel.id,
        reason=ReportReason.ILLEGAL,
    )
    outcome = service.uphold(report, Actor.staff(make_staff()), "confirmed")
    assert outcome.channel_suspended is True
    assert channel.status is ChannelStatus.SUSPENDED


def test_reporting_an_advertisement_reaches_its_campaign(db, make_campaign, make_staff):
    campaign = make_campaign()
    ad = campaign.ads[0]
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.ADVERTISEMENT,
        target_id=ad.id,
        reason=ReportReason.MALWARE,
    )
    outcome = service.uphold(report, Actor.staff(make_staff()), "the link drops an APK")
    assert outcome.campaign_suspended is True
    assert campaign.status is CampaignStatus.SUSPENDED


def test_dismissing_a_report_changes_nothing_else(db, make_campaign, make_staff):
    campaign = make_campaign()
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SCAM,
    )
    service.dismiss(report, Actor.staff(make_staff()), "landing page is legitimate")
    assert report.status is ReportStatus.DISMISSED
    assert campaign.status is CampaignStatus.RUNNING


def test_a_resolution_note_is_required_either_way(db, make_campaign, make_staff):
    campaign = make_campaign()
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SPAM,
    )
    actor = Actor.staff(make_staff())
    with pytest.raises(ValidationFailed, match="resolution note is required"):
        service.uphold(report, actor, "   ")
    with pytest.raises(ValidationFailed, match="resolution note is required"):
        service.dismiss(report, actor, "")


def test_a_decided_report_cannot_be_decided_again(db, make_campaign, make_staff):
    campaign = make_campaign()
    service = ModerationService(db)
    actor = Actor.staff(make_staff())
    report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SPAM,
    )
    service.dismiss(report, actor, "not a problem")
    with pytest.raises(Conflict, match="already dismissed"):
        service.uphold(report, actor, "changed my mind")


def test_every_decision_is_audited(db, make_campaign, make_staff):
    campaign = make_campaign()
    staff = make_staff()
    service = ModerationService(db)
    report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SCAM,
    )
    service.uphold(report, Actor.staff(staff), "confirmed scam")

    log = db.query(AuditLog).filter(AuditLog.action == "report.upheld").one()
    assert log.actor_id == str(staff.id)
    assert log.reason == "confirmed scam"
    assert log.new_value["campaign_suspended"] is True


def test_the_queue_lists_only_undecided_reports(db, make_campaign, make_staff):
    service = ModerationService(db)
    actor = Actor.staff(make_staff())
    open_report = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=make_campaign().id,
        reason=ReportReason.SPAM,
    )
    decided = service.file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=make_campaign().id,
        reason=ReportReason.SPAM,
    )
    service.dismiss(decided, actor, "fine")

    queue = service.open_reports()
    assert [r.id for r in queue] == [open_report.id]
    assert len(service.all_reports()) == 2


def test_moderator_can_escalate_a_review_to_an_admin(
    db, make_campaign, make_staff, funded, make_advertiser
):
    """Spec §1: a moderator escalates financial and fraud questions."""
    from app.models.enums import ReviewDecision
    from app.models.ops import ModerationReview
    from app.services.campaigns import CampaignService

    advertiser = funded(make_advertiser(), "20000")
    campaign = make_campaign(advertiser, status=CampaignStatus.DRAFT)
    CampaignService(db).submit(campaign)
    review = db.query(ModerationReview).one()

    ModerationService(db).escalate(
        review, Actor.staff(make_staff()), "advertiser balance looks laundered"
    )
    assert review.decision is ReviewDecision.ESCALATED
    assert db.query(AuditLog).filter(AuditLog.action == "review.escalated").count() == 1


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_report_api_requires_authentication(client):
    import uuid

    response = client.post(
        "/api/v1/reports",
        json={
            "target_type": "campaign",
            "target_id": str(uuid.uuid4()),
            "reason": "spam",
        },
    )
    assert response.status_code == 401


def test_any_authenticated_user_can_file_a_report(client, api_token, db, make_campaign):
    token, _ = api_token(publisher=True)
    campaign = make_campaign()
    db.commit()

    response = client.post(
        "/api/v1/reports",
        json={
            "target_type": "campaign",
            "target_id": str(campaign.id),
            "reason": "misleading",
            "details": "claims are not true",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "open"


def test_only_staff_can_read_or_decide_reports(client, api_token, db, make_campaign):
    token, _ = api_token(publisher=True)
    campaign = make_campaign()
    report = ModerationService(db).file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SPAM,
    )
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/reports/queue", headers=headers).status_code == 403
    assert (
        client.post(
            f"/api/v1/reports/{report.id}/uphold",
            json={"resolution": "let me suspend this"},
            headers=headers,
        ).status_code
        == 403
    )


def test_staff_can_work_the_report_queue_over_http(client, db, make_staff, make_campaign):
    from app.core.security import hash_password

    staff = make_staff(email="modapi@example.com")
    staff.password_hash = hash_password("correct-horse-battery")
    campaign = make_campaign()
    report = ModerationService(db).file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=campaign.id,
        reason=ReportReason.SCAM,
    )
    db.commit()

    client.post(
        "/admin/login",
        data={"email": "modapi@example.com", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    csrf = client.cookies.get("adnet_csrf")

    queue = client.get("/api/v1/reports/queue")
    assert queue.status_code == 200
    assert len(queue.json()) == 1

    decided = client.post(
        f"/api/v1/reports/{report.id}/uphold",
        json={"resolution": "confirmed phishing landing page"},
        headers={"X-CSRF-Token": csrf},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["campaign_suspended"] is True


def test_reports_dashboard_page_renders(client, db, make_staff, make_campaign):
    from app.core.security import hash_password

    staff = make_staff(email="modui@example.com")
    staff.password_hash = hash_password("correct-horse-battery")
    ModerationService(db).file_report(
        target_type=ReviewTarget.CAMPAIGN,
        target_id=make_campaign().id,
        reason=ReportReason.MALWARE,
        details="drops an apk",
    )
    db.commit()
    client.post(
        "/admin/login",
        data={"email": "modui@example.com", "password": "correct-horse-battery"},
        follow_redirects=False,
    )

    page = client.get("/admin/reports")
    assert page.status_code == 200
    assert "malware" in page.text
    assert "drops an apk" in page.text
    assert "never acted on" in page.text
