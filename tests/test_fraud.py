"""Fraud detection must be evidence-based, graduated, and never ban on one signal."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.db.base import utcnow
from app.models.enums import FraudBand, FraudSubject
from app.models.identity import User
from app.models.ops import FraudCase, FraudEvent, FraudScore
from app.models.telegram import ChannelStatDaily
from app.services.fraud import FraudService, Signal, blend
from app.services.impressions import ImpressionService, hash_identity
from app.services.settings_service import SettingsService


# --------------------------------------------------------------------------
# The blend
# --------------------------------------------------------------------------


def test_bands_match_the_spec_thresholds():
    """Spec §16: 0-30 normal, 31-60 review, 61-80 suspicious, 81-100 high risk."""
    assert FraudBand.of(0) is FraudBand.NORMAL
    assert FraudBand.of(30) is FraudBand.NORMAL
    assert FraudBand.of(31) is FraudBand.REVIEW
    assert FraudBand.of(60) is FraudBand.REVIEW
    assert FraudBand.of(61) is FraudBand.SUSPICIOUS
    assert FraudBand.of(80) is FraudBand.SUSPICIOUS
    assert FraudBand.of(81) is FraudBand.HIGH_RISK
    assert FraudBand.of(100) is FraudBand.HIGH_RISK


def test_one_weak_signal_cannot_reach_high_risk():
    """Spec §16: never act on a single signal. Weak evidence stays weak."""
    assert blend([Signal("a", 40)]) <= 60


def test_several_weak_signals_do_not_manufacture_high_risk():
    """A plain sum would score 95 here; the blend keeps it in review territory."""
    score = blend([Signal("a", 35), Signal("b", 30), Signal("c", 30)])
    assert FraudBand.of(score) is not FraudBand.HIGH_RISK
    assert score < 61


def test_corroboration_escalates():
    alone = blend([Signal("a", 70)])
    corroborated = blend([Signal("a", 70), Signal("b", 65), Signal("c", 60)])
    assert corroborated > alone
    assert FraudBand.of(corroborated) in (FraudBand.SUSPICIOUS, FraudBand.HIGH_RISK)


def test_blend_is_bounded_and_handles_empty():
    assert blend([]) == 0
    assert blend([Signal("a", 100), Signal("b", 100), Signal("c", 100)]) <= 100


def test_signal_scores_are_clamped():
    assert Signal("a", 500).score == 100
    assert Signal("a", -20).score == 0


# --------------------------------------------------------------------------
# Impression-level signals
# --------------------------------------------------------------------------


def test_publisher_clicking_their_own_ad_is_caught(db, sent_delivery):
    """Spec §16: self-generated impressions."""
    delivery, campaign, channel, advertiser = sent_delivery()
    publisher_user = db.get(
        User, db.get(type(channel.publisher), channel.publisher_id).user_id
    )
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=publisher_user.telegram_user_id,
        user_agent_hash="ua", ip_hash="ip",
    )
    assert assessment.band is FraudBand.HIGH_RISK
    assert "self_generated" in assessment.triggered()
    evidence = assessment.evidence
    assert any(s["evidence"].get("role") == "publisher" for s in evidence["signals"])


def test_advertiser_clicking_their_own_ad_is_caught(db, sent_delivery):
    from app.models.identity import Advertiser

    delivery, campaign, channel, advertiser = sent_delivery()
    adv_user = db.get(User, db.get(Advertiser, advertiser.id).user_id)
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=adv_user.telegram_user_id, user_agent_hash="ua"
    )
    assert "self_generated" in assessment.triggered()


def test_an_ordinary_viewer_scores_clean(db, sent_delivery):
    delivery, *_ = sent_delivery()
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=987_654_321, user_agent_hash=hash_identity("Mozilla"),
        ip_hash=hash_identity("203.0.113.9"),
    )
    assert assessment.band is FraudBand.NORMAL
    assert assessment.score == 0


def test_many_users_behind_one_address_is_flagged(db, sent_delivery):
    """Click-farm signature: 'distinct users' all sharing one IP."""
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, *_ = sent_delivery()
    impressions = ImpressionService(db)
    shared_ip = hash_identity("198.51.100.7")
    for user in range(12):
        impressions.record(
            delivery, kind=ImpressionKind.MEASURED,
            source=ImpressionSource.TRACKING_LINK,
            dedupe_key=f"farm:{user}", telegram_user_id=user + 1,
            ip_hash=shared_ip, user_agent_hash="ua",
        )
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=99, ip_hash=shared_ip, user_agent_hash="ua"
    )
    assert "ip_concentration" in assessment.triggered()
    assert assessment.score >= 61


def test_missing_user_agent_is_a_weak_signal_only(db, sent_delivery):
    """Absent UA is suspicious but must not by itself block billing."""
    delivery, *_ = sent_delivery()
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=5, ip_hash=hash_identity("1.2.3.4"),
        user_agent_hash=None,
    )
    assert "missing_user_agent" in assessment.triggered()
    # Recorded, but weighted down to 15 on its own: well below the block
    # threshold, so an absent header cannot cost a publisher its earnings.
    assert assessment.score < 31
    assert assessment.band is FraudBand.NORMAL


def test_impossible_velocity_is_detected(db, sent_delivery):
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, campaign, channel, advertiser = sent_delivery(avg_views=1_000)
    now = utcnow()
    ImpressionService(db).record(
        delivery, kind=ImpressionKind.MEASURED, source=ImpressionSource.TRACKING_LINK,
        dedupe_key="burst", quantity=900, occurred_at=now,
    )
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=7, occurred_at=now, user_agent_hash="ua"
    )
    assert "impossible_velocity" in assessment.triggered()


def test_repeat_identity_inside_the_dedupe_window_is_flagged(db, sent_delivery):
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, *_ = sent_delivery()
    SettingsService(db).set("click_dedupe_window_minutes", "60")
    impressions = ImpressionService(db)
    for i in range(3):
        impressions.record(
            delivery, kind=ImpressionKind.MEASURED,
            source=ImpressionSource.TRACKING_LINK, dedupe_key=f"rep:{i}",
            telegram_user_id=4242, user_agent_hash="ua",
        )
    assessment = FraudService(db).score_impression_event(
        delivery, telegram_user_id=4242, user_agent_hash="ua"
    )
    assert "repeat_identity" in assessment.triggered()


# --------------------------------------------------------------------------
# Channel audit
# --------------------------------------------------------------------------


def test_member_inflation_is_detected(db, make_channel):
    """Spec §16 fake members: 500k members with 2k views."""
    inflated = make_channel(members=500_000, avg_views=2_000)
    assessment = FraudService(db).audit_channel(inflated)
    assert "member_inflation" in assessment.triggered()
    assert assessment.score >= 31
    evidence = next(s for s in assessment.evidence["signals"]
                    if s["name"] == "member_inflation")
    assert evidence["evidence"]["members"] == 500_000
    assert evidence["evidence"]["avg_views"] == 2_000


def test_healthy_channel_is_not_flagged(db, make_channel):
    healthy = make_channel(members=50_000, avg_views=20_000)
    assessment = FraudService(db).audit_channel(healthy)
    assert assessment.band is FraudBand.NORMAL


def test_view_spike_is_detected(db, make_channel):
    """Spec §16: abnormally fast view growth / sudden traffic spikes."""
    channel = make_channel(members=50_000, avg_views=40_000)
    base = utcnow().date()
    for day in range(10):
        db.add(ChannelStatDaily(
            channel_id=channel.id, stat_date=base - timedelta(days=10 - day),
            member_count=50_000, avg_views=2_000, median_views=2_000,
        ))
    # Yesterday's views jump 20x.
    db.add(ChannelStatDaily(
        channel_id=channel.id, stat_date=base, member_count=50_000,
        avg_views=40_000, median_views=40_000,
    ))
    db.flush()
    assessment = FraudService(db).audit_channel(channel)
    assert "view_spike" in assessment.triggered()


def test_implausible_ctr_is_detected(db, make_channel):
    channel = make_channel(members=50_000, avg_views=20_000)
    channel.total_impressions = 10_000
    channel.total_clicks = 6_000          # 60% CTR is not real
    db.flush()
    assessment = FraudService(db).audit_channel(channel)
    assert "implausible_ctr" in assessment.triggered()


def test_member_growth_spike_is_detected(db, make_channel):
    channel = make_channel(members=100_000, avg_views=30_000)
    base = utcnow().date()
    for day in range(6):
        db.add(ChannelStatDaily(
            channel_id=channel.id, stat_date=base - timedelta(days=day),
            member_count=100_000, member_delta=200, avg_views=30_000,
        ))
    # 40k members appear in one day.
    db.add(ChannelStatDaily(
        channel_id=channel.id, stat_date=base - timedelta(days=7),
        member_count=100_000, member_delta=40_000, avg_views=30_000,
    ))
    db.flush()
    assessment = FraudService(db).audit_channel(channel)
    assert "member_growth_spike" in assessment.triggered()


def test_audit_persists_the_score_for_admin_inspection(db, make_channel):
    channel = make_channel(members=500_000, avg_views=1_000)
    assessment = FraudService(db).audit_channel(channel)
    assert channel.fraud_score == assessment.score
    row = db.query(FraudScore).filter(
        FraudScore.subject_type == FraudSubject.CHANNEL,
        FraudScore.subject_id == channel.id,
    ).one()
    assert row.score == assessment.score
    assert row.signals["signals"]      # evidence is inspectable


def test_thresholds_are_configurable_not_hardcoded(db, make_channel):
    """Spec §16: admin controls the thresholds."""
    channel = make_channel(members=100_000, avg_views=3_000)  # 3% view ratio
    SettingsService(db).set("fraud_min_view_member_ratio", "0.0100")  # 1% floor
    assert "member_inflation" not in FraudService(db).audit_channel(channel).triggered()

    SettingsService(db).set("fraud_min_view_member_ratio", "0.1000")  # 10% floor
    assert "member_inflation" in FraudService(db).audit_channel(channel).triggered()


# --------------------------------------------------------------------------
# Recording and cases
# --------------------------------------------------------------------------


def test_low_scores_are_not_recorded_as_events(db, make_channel):
    channel = make_channel(members=50_000, avg_views=20_000)
    service = FraudService(db)
    assessment = service.audit_channel(channel)
    assert service.record_event(FraudSubject.CHANNEL, channel.id, assessment) is None
    assert db.query(FraudEvent).count() == 0


def test_flagged_scores_are_recorded_with_full_evidence(db, make_channel):
    channel = make_channel(members=500_000, avg_views=1_000)
    service = FraudService(db)
    assessment = service.audit_channel(channel)
    event = service.record_event(
        FraudSubject.CHANNEL, channel.id, assessment,
        publisher_id=channel.publisher_id, channel_id=channel.id,
        amount_at_risk="1234.50",
    )
    assert event is not None
    assert event.band is assessment.band
    assert event.amount_at_risk == Decimal("1234.500000")
    assert event.evidence["signals"]
    import json
    json.dumps(event.evidence)


def test_case_is_deduplicated_per_open_subject(db, make_channel):
    channel = make_channel(members=500_000, avg_views=1_000)
    service = FraudService(db)
    assessment = service.audit_channel(channel)
    first = service.open_case(FraudSubject.CHANNEL, channel.id, assessment, "inflated members")
    second = service.open_case(FraudSubject.CHANNEL, channel.id, assessment, "still inflated")
    assert first.id == second.id
    assert db.query(FraudCase).count() == 1
    assert second.summary == "still inflated"


def test_sweep_flags_suspicious_deliveries(db, sent_delivery):
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, *_ = sent_delivery(avg_views=50_000)
    impressions = ImpressionService(db)
    concentrated = hash_identity("198.51.100.200")
    for i in range(60):
        impressions.record(
            delivery, kind=ImpressionKind.MEASURED,
            source=ImpressionSource.TRACKING_LINK, dedupe_key=f"sw:{i}",
            telegram_user_id=i, ip_hash=concentrated, user_agent_hash="ua",
        )
    events = FraudService(db).sweep_deliveries()
    assert len(events) == 1
    assert "ip_concentration" in events[0].signal
    assert delivery.fraud_score >= 31


def test_clean_deliveries_are_not_flagged_by_the_sweep(db, sent_delivery):
    from app.models.enums import ImpressionKind, ImpressionSource

    delivery, *_ = sent_delivery(avg_views=50_000)
    impressions = ImpressionService(db)
    for i in range(60):
        impressions.record(
            delivery, kind=ImpressionKind.MEASURED,
            source=ImpressionSource.TRACKING_LINK, dedupe_key=f"ok:{i}",
            telegram_user_id=i, ip_hash=hash_identity(f"10.0.0.{i}"),
            user_agent_hash="ua",
        )
    assert FraudService(db).sweep_deliveries() == []
