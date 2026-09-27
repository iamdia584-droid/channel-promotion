"""Pricing must be server-side, configurable, and split revenue exactly."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import ValidationFailed
from app.core.money import q
from app.models.enums import PricingRuleScope
from app.services.pricing import PricingService, quality_tier, size_band
from app.services.quality import QualityService
from app.services.settings_service import SettingsService


def test_spec_section_6_advertiser_publisher_platform_split(db, make_campaign, make_channel):
    """৳50 advertiser CPM at 30% commission → ৳35 publisher, ৳15 platform margin."""
    campaign = make_campaign(bid_cpm="50")
    channel = make_channel(quality="0.5")
    settings = SettingsService(db)
    settings.set("platform_commission_rate", "0.3000")
    # Neutralise the quality band so the arithmetic is the spec's own example.
    settings.set("quality_multiplier_floor", "1.0000")
    settings.set("quality_multiplier_ceiling", "1.0000")

    quote = PricingService(db).quote(campaign, channel)
    assert quote.advertiser_cpm == Decimal("50.000000")
    assert quote.effective_cpm == Decimal("50.000000")
    assert quote.publisher_cpm == Decimal("35.000000")
    assert quote.commission_amount_per_mille == Decimal("15.000000")


def test_cost_for_impressions_reconciles_exactly(db, make_campaign, make_channel):
    """Spec §11: 50,000 impressions at ৳100 CPM, 20% commission."""
    campaign = make_campaign(bid_cpm="100")
    channel = make_channel()
    s = SettingsService(db)
    s.set("platform_commission_rate", "0.2000")
    s.set("quality_multiplier_floor", "1.0000")
    s.set("quality_multiplier_ceiling", "1.0000")

    quote = PricingService(db).quote(campaign, channel)
    gross, publisher, platform = quote.cost_for(50_000)
    assert gross == Decimal("5000.000000")
    assert publisher == Decimal("4000.000000")
    assert platform == Decimal("1000.000000")
    # The defining invariant: the split must reconstruct the charge exactly.
    assert publisher + platform == gross


@pytest.mark.parametrize("impressions", [1, 7, 333, 1001, 99_999, 1_000_000])
def test_split_always_reconciles_at_any_volume(db, make_campaign, make_channel, impressions):
    campaign = make_campaign(bid_cpm="37.77")
    channel = make_channel(quality="0.63")
    quote = PricingService(db).quote(campaign, channel)
    gross, publisher, platform = quote.cost_for(impressions)
    assert publisher + platform == gross
    assert publisher >= 0 and platform >= 0


def test_country_and_category_multipliers_compound(db, make_campaign, make_channel):
    """Spec §7: different CPM by country and by category, both admin-configured."""
    campaign = make_campaign(bid_cpm="100")
    channel = make_channel(country="BD", category="education", quality="0.5")
    s = SettingsService(db)
    s.set("quality_multiplier_floor", "1.0000")
    s.set("quality_multiplier_ceiling", "1.0000")
    pricing = PricingService(db)

    baseline = pricing.quote(campaign, channel).effective_cpm

    pricing.upsert_rule(PricingRuleScope.COUNTRY, "BD", multiplier="0.8")
    pricing.upsert_rule(PricingRuleScope.CATEGORY, "education", multiplier="1.25")
    combined = pricing.quote(campaign, channel).effective_cpm

    assert baseline == Decimal("100.000000")
    assert combined == Decimal("100.000000")  # 100 × 0.8 × 1.25
    pricing.upsert_rule(PricingRuleScope.CATEGORY, "education", multiplier="1.5")
    assert pricing.quote(campaign, channel).effective_cpm == Decimal("120.000000")


def test_category_commission_override_beats_global_rate(db, make_campaign, make_channel):
    campaign = make_campaign(bid_cpm="100")
    channel = make_channel(category="finance")
    s = SettingsService(db)
    s.set("platform_commission_rate", "0.3000")
    s.set("quality_multiplier_floor", "1.0000")
    s.set("quality_multiplier_ceiling", "1.0000")
    pricing = PricingService(db)
    pricing.upsert_rule(PricingRuleScope.CATEGORY, "finance", commission_rate_override="0.5000")

    quote = pricing.quote(campaign, channel)
    assert quote.commission_rate == Decimal("0.5000")
    assert quote.publisher_cpm == Decimal("50.000000")


def test_quality_multiplier_maps_score_onto_configured_band(db, make_campaign, make_channel):
    campaign = make_campaign(bid_cpm="100")
    s = SettingsService(db)
    s.set("quality_multiplier_floor", "0.5000")
    s.set("quality_multiplier_ceiling", "1.5000")
    pricing = PricingService(db)

    worst = pricing.quote(campaign, make_channel(quality="0")).effective_cpm
    middle = pricing.quote(campaign, make_channel(quality="0.5")).effective_cpm
    best = pricing.quote(campaign, make_channel(quality="1")).effective_cpm

    assert worst == Decimal("50.000000")
    assert middle == Decimal("100.000000")
    assert best == Decimal("150.000000")


def test_bid_below_configured_minimum_is_refused(db, make_campaign, make_channel):
    SettingsService(db).set("min_cpm", "20.000000")
    campaign = make_campaign(bid_cpm="10")
    with pytest.raises(ValidationFailed, match="below the configured minimum"):
        PricingService(db).quote(campaign, make_channel())


def test_bid_above_maximum_is_clamped_not_refused(db, make_campaign, make_channel):
    SettingsService(db).set("max_cpm", "200.000000")
    campaign = make_campaign(bid_cpm="5000")
    quote = PricingService(db).quote(campaign, make_channel(quality="0.5"))
    assert quote.effective_cpm <= Decimal("200.000000")
    assert any("clamped" in n for n in quote.notes)


def test_quote_breakdown_is_persistable_and_explains_itself(db, make_campaign, make_channel):
    """A frozen breakdown is what lets a payout dispute be settled later."""
    campaign = make_campaign(bid_cpm="60")
    channel = make_channel(country="BD", category="education")
    PricingService(db).upsert_rule(PricingRuleScope.COUNTRY, "BD", multiplier="0.9")
    quote = PricingService(db).quote(campaign, channel)
    data = quote.breakdown()
    assert data["multipliers"]["country"] == "0.9000"
    assert "quality" in data["multipliers"]
    assert data["advertiser_cpm"] == "60.000000"
    import json

    json.dumps(data)  # must be JSON-serialisable for the JSONB column


def test_publisher_cpm_never_exceeds_effective_cpm(db, make_campaign, make_channel):
    for rate in ["0", "0.01", "0.5", "0.99", "1"]:
        SettingsService(db).set("platform_commission_rate", rate)
        quote = PricingService(db).quote(make_campaign(bid_cpm="80"), make_channel())
        assert quote.publisher_cpm <= quote.effective_cpm
        assert quote.publisher_cpm >= 0


def test_size_and_quality_bands():
    assert size_band(500) == "micro"
    assert size_band(5_000) == "small"
    assert size_band(50_000) == "medium"
    assert size_band(500_000) == "large"
    assert size_band(5_000_000) == "mega"
    assert quality_tier(Decimal("0.1")) == "poor"
    assert quality_tier(Decimal("0.9")) == "excellent"


def test_forecast_is_explicitly_labelled_an_estimate(db, make_campaign, make_channel):
    """Spec §37: never fake precision — forecasts must be marked as estimates."""
    make_channel()
    campaign = make_campaign(total_budget="10000", bid_cpm="50")
    forecast = PricingService(db).forecast_impressions(campaign)
    assert forecast["is_estimate"] is True
    assert forecast["estimated_impressions_at_bid"] == 200_000
    assert "estimate" in forecast["note"].lower() or "measured" in forecast["note"].lower()


# --------------------------------------------------------------------------
# Quality scoring (spec §5, §17)
# --------------------------------------------------------------------------


def test_member_count_alone_does_not_buy_quality(db, make_channel):
    """Spec §5's exact scenario: 100k members/3k views must not beat 50k/25k."""
    inflated = make_channel(members=100_000, avg_views=3_000)
    genuine = make_channel(members=50_000, avg_views=25_000)
    quality = QualityService(db)
    inflated_score = quality.compute(inflated).score
    genuine_score = quality.compute(genuine).score
    assert genuine_score > inflated_score, (genuine_score, inflated_score)


def test_quality_score_stays_in_unit_range(db, make_channel):
    quality = QualityService(db)
    for members, views in [(0, 0), (10, 1_000_000), (1_000_000, 0), (100, 100)]:
        score = quality.compute(make_channel(members=members, avg_views=views)).score
        assert Decimal(0) <= score <= Decimal(1), (members, views, score)


def test_fraud_score_penalises_quality(db, make_channel):
    clean = make_channel(members=50_000, avg_views=25_000, fraud_score=0)
    dirty = make_channel(members=50_000, avg_views=25_000, fraud_score=90)
    quality = QualityService(db)
    assert quality.compute(dirty).score < quality.compute(clean).score


def test_publishers_see_only_a_band_not_the_weights(db, make_channel):
    """Spec §17: internal fraud logic must not leak to publishers."""
    breakdown = QualityService(db).compute(make_channel())
    public = breakdown.public_view()
    assert set(public) == {"quality_band"}
    assert "fraud" not in str(public).lower()


def test_refresh_persists_the_score(db, make_channel):
    channel = make_channel(members=40_000, avg_views=20_000)
    breakdown = QualityService(db).refresh(channel)
    assert q(channel.quality_score) == q(breakdown.score)
