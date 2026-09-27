"""Impression validation is the platform's main fraud surface (spec §8)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.db.base import utcnow
from app.models.delivery import Impression
from app.models.enums import (
    ImpressionKind,
    ImpressionSource,
    MeasurementMode,
    ValidationStatus,
)
from app.services.impressions import ImpressionService, hash_identity
from app.services.measurement import (
    NullViewSource,
    StaticViewSource,
    get_view_source,
    set_view_source,
)
from app.services.settings_service import SettingsService


def _record(service, delivery, key, **kw):
    return service.record(
        delivery,
        kind=kw.pop("kind", ImpressionKind.MEASURED),
        source=kw.pop("source", ImpressionSource.TRACKING_LINK),
        dedupe_key=key,
        **kw,
    )


# --------------------------------------------------------------------------
# Duplicate suppression
# --------------------------------------------------------------------------


def test_duplicate_dedupe_key_is_rejected_by_the_database(db, sent_delivery):
    """Refresh abuse: the same tracking link opened repeatedly bills once."""
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)

    first = _record(service, delivery, "imp:user-42")
    assert first.accepted and first.billable

    for _ in range(9):
        again = _record(service, delivery, "imp:user-42")
        assert again.accepted is False
        assert again.status is ValidationStatus.DUPLICATE

    assert delivery.billable_impressions == 1
    assert db.query(Impression).filter(Impression.delivery_id == delivery.id).count() == 1


def test_dedupe_is_global_not_per_delivery(db, sent_delivery):
    """A dedupe key must not be replayable against a second delivery."""
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    assert _record(service, delivery, "shared-key").accepted
    # Same key, same delivery-independent uniqueness → refused.
    assert _record(service, delivery, "shared-key").accepted is False


def test_distinct_users_each_bill(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    for user in range(25):
        assert _record(service, delivery, f"imp:{user}", telegram_user_id=user).billable
    assert delivery.billable_impressions == 25


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


def test_impression_before_the_post_does_not_bill(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    result = _record(
        service, delivery, "early", occurred_at=delivery.sent_at - timedelta(minutes=1)
    )
    assert result.status is ValidationStatus.OUT_OF_WINDOW
    assert result.billable is False
    assert delivery.billable_impressions == 0
    # Still recorded as evidence.
    assert db.query(Impression).count() == 1


def test_impression_after_the_window_does_not_bill(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    late = delivery.measurement_ends_at + timedelta(hours=1)
    result = _record(service, delivery, "late", occurred_at=late)
    assert result.status is ValidationStatus.OUT_OF_WINDOW
    assert delivery.billable_impressions == 0


# --------------------------------------------------------------------------
# The ratchet cap
# --------------------------------------------------------------------------


def test_delivery_cannot_bill_beyond_demonstrated_reach(db, sent_delivery):
    """A channel averaging 1,000 views cannot bill 10,000 impressions."""
    SettingsService(db).set("impression_cap_multiplier", "1.5000")
    delivery, _, channel, _ = sent_delivery(avg_views=1_000, bid_cpm="100")
    assert delivery.impression_cap <= 1_500

    service = ImpressionService(db)
    accepted = sum(1 for i in range(3_000) if _record(service, delivery, f"cap:{i}").billable)
    assert accepted == delivery.impression_cap
    assert delivery.billable_impressions == delivery.impression_cap
    # The excess is recorded as capped evidence, not silently dropped.
    capped = (
        db.query(Impression).filter(Impression.validation_status == ValidationStatus.CAPPED).count()
    )
    assert capped > 0


def test_cap_is_the_lower_of_demonstrated_and_funded_reach(db, sent_delivery):
    """A tiny budget caps impressions even on a huge channel."""
    delivery, *_ = sent_delivery(avg_views=1_000_000, bid_cpm="100", budget="1000")
    # 1000 budget at 100 CPM funds 10,000 impressions; the channel would allow more.
    assert delivery.impression_cap == delivery.impression_allowance
    assert delivery.impression_cap <= 10_000


# --------------------------------------------------------------------------
# Fraud gating
# --------------------------------------------------------------------------


def test_high_fraud_score_blocks_billing_but_keeps_evidence(db, sent_delivery):
    delivery, *_ = sent_delivery()
    SettingsService(db).set("fraud_block_threshold", "81")
    service = ImpressionService(db)

    clean = _record(service, delivery, "clean", fraud_score=10)
    dirty = _record(
        service,
        delivery,
        "dirty",
        fraud_score=95,
        fraud_reasons=["datacenter_ip", "impossible_velocity"],
    )

    assert clean.billable is True
    assert dirty.billable is False
    assert dirty.status is ValidationStatus.FRAUDULENT
    assert delivery.billable_impressions == 1
    assert delivery.invalid_impressions == 1
    stored = db.query(Impression).filter(Impression.dedupe_key == "dirty").one()
    assert "datacenter_ip" in stored.fraud_reasons


# --------------------------------------------------------------------------
# The four impression kinds (spec §37)
# --------------------------------------------------------------------------


def test_estimated_impressions_are_never_billable(db, sent_delivery):
    """Spec §37: never fake precision. Estimates must not earn money."""
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    result = service.record_estimate(delivery, 5_000)
    assert result.accepted is True
    assert result.billable is False
    assert delivery.estimated_impressions == 5_000
    assert delivery.billable_impressions == 0


def test_view_counter_impressions_need_the_right_measurement_mode(db, sent_delivery):
    """A view number we cannot corroborate must not bill."""
    delivery, _, channel, _ = sent_delivery()
    assert channel.measurement_mode is MeasurementMode.CLICK_ONLY
    service = ImpressionService(db)

    blocked = _record(
        service,
        delivery,
        "v1",
        kind=ImpressionKind.TELEGRAM_REPORTED,
        source=ImpressionSource.VIEW_COUNTER,
    )
    assert blocked.billable is False
    assert blocked.status is ValidationStatus.INVALIDATED

    channel.measurement_mode = MeasurementMode.VIEW_COUNTER
    db.flush()
    allowed = _record(
        service,
        delivery,
        "v2",
        kind=ImpressionKind.TELEGRAM_REPORTED,
        source=ImpressionSource.VIEW_COUNTER,
    )
    assert allowed.billable is True


def test_view_counter_ratchet_bills_only_the_delta(db, sent_delivery):
    """Polling a cumulative counter repeatedly must not re-bill the same views."""
    delivery, _, channel, _ = sent_delivery(avg_views=100_000)
    channel.measurement_mode = MeasurementMode.VIEW_COUNTER
    db.flush()
    service = ImpressionService(db)

    assert service.ingest_view_observation(delivery, 1_000, "static").accepted
    assert delivery.billable_impressions == 1_000

    # Same total again → nothing new.
    repeat = service.ingest_view_observation(delivery, 1_000, "static")
    assert repeat.accepted is False
    assert delivery.billable_impressions == 1_000

    # Grown to 1,500 → only 500 more bill.
    assert service.ingest_view_observation(delivery, 1_500, "static").accepted
    assert delivery.billable_impressions == 1_500
    assert delivery.reported_views_high_water == 1_500


def test_view_counter_going_backwards_cannot_re_bill(db, sent_delivery):
    """A counter manipulated down and back up must not bill the same views twice."""
    delivery, _, channel, _ = sent_delivery(avg_views=100_000)
    channel.measurement_mode = MeasurementMode.VIEW_COUNTER
    db.flush()
    service = ImpressionService(db)

    service.ingest_view_observation(delivery, 5_000, "static")
    assert service.ingest_view_observation(delivery, 500, "static").accepted is False
    assert service.ingest_view_observation(delivery, 5_000, "static").accepted is False
    assert delivery.billable_impressions == 5_000


def test_delivery_report_separates_the_four_kinds(db, sent_delivery):
    delivery, _, channel, _ = sent_delivery(avg_views=50_000)
    channel.measurement_mode = MeasurementMode.HYBRID
    db.flush()
    service = ImpressionService(db)
    _record(service, delivery, "m1")
    service.record_estimate(delivery, 9_000)
    service.ingest_view_observation(delivery, 400, "static")

    report = service.delivery_report(delivery)
    assert report["measured_impressions"] == 1
    assert report["estimated_impressions"] == 9_000
    assert report["telegram_reported_impressions"] == 400
    assert report["billable_impressions"] == 401  # estimates excluded
    assert "never billed" in report["note"]


# --------------------------------------------------------------------------
# Clicks
# --------------------------------------------------------------------------


def test_duplicate_clicks_are_suppressed(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    assert service.record_click(delivery, dedupe_key="c:1") is not None
    assert service.record_click(delivery, dedupe_key="c:1") is None
    assert delivery.clicks == 1


def test_fraudulent_click_is_recorded_but_not_counted(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    click = service.record_click(
        delivery, dedupe_key="c:bad", fraud_score=99, fraud_reasons=["self_click"]
    )
    assert click is not None
    assert click.valid is False
    assert delivery.clicks == 0


# --------------------------------------------------------------------------
# Privacy and measurement sources
# --------------------------------------------------------------------------


def test_identities_are_hashed_never_stored_raw(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    ip = "203.0.113.45"
    _record(service, delivery, "h1", ip_hash=hash_identity(ip))
    stored = db.query(Impression).one()
    assert stored.ip_hash != ip
    assert ip not in str(stored.ip_hash)
    assert len(stored.ip_hash) == 64
    # Deterministic, so repeat visitors are still detectable.
    assert hash_identity(ip) == hash_identity(ip)
    assert hash_identity(ip) != hash_identity("203.0.113.46")


def test_default_view_source_reports_nothing(db):
    """A fresh deployment must bill only what it measured itself."""
    set_view_source(None)
    source = get_view_source()
    assert isinstance(source, NullViewSource)
    assert source.available is False
    assert source.fetch(-100, [1, 2, 3]) == []


def test_static_view_source_is_usable_for_backfill(db):
    source = StaticViewSource()
    source.set(-100, 7, 2_500)
    set_view_source(source)
    try:
        observations = get_view_source().fetch(-100, [7, 8])
        assert len(observations) == 1
        assert observations[0].views == 2_500
    finally:
        set_view_source(None)


def test_mtproto_source_refuses_to_invent_numbers(db):
    from app.services.measurement import MTProtoViewSource

    source = MTProtoViewSource()
    assert source.available is False
    with pytest.raises(NotImplementedError, match="not wired up"):
        source.fetch(-100, [1])


def test_invalidate_retracts_billing_without_deleting_evidence(db, sent_delivery):
    delivery, *_ = sent_delivery()
    service = ImpressionService(db)
    result = _record(service, delivery, "keepme")
    assert delivery.billable_impressions == 1

    service.invalidate(result.impression, ValidationStatus.FRAUDULENT, "confirmed bot farm")
    assert delivery.billable_impressions == 0
    assert delivery.invalid_impressions == 1
    stored = db.query(Impression).filter(Impression.dedupe_key == "keepme").one()
    assert stored.billable is False
    assert "confirmed bot farm" in stored.fraud_reasons
    assert stored.id is not None  # the row itself survives


def test_duplicate_does_not_discard_the_callers_transaction(db, sent_delivery):
    """A rejected duplicate must cost only itself.

    Regression: handling the unique-index violation with session.rollback()
    discarded every uncommitted change in the session, so one duplicate
    impression wiped out the delivery, channel and wallet work around it.
    """
    delivery, campaign, channel, _ = sent_delivery()
    service = ImpressionService(db)
    _record(service, delivery, "dup-key")

    # Uncommitted work alongside the duplicate.
    channel.median_views = 4_321
    db.flush()

    assert _record(service, delivery, "dup-key").status is ValidationStatus.DUPLICATE

    # Everything else is still here and still usable.
    assert channel.median_views == 4_321
    assert db.get(type(delivery), delivery.id) is not None
    assert db.get(type(campaign), campaign.id) is not None
    assert _record(service, delivery, "after-dup").billable is True
    assert delivery.billable_impressions == 2
