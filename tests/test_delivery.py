"""The delivery engine must select deliberately, pace spending, and never
charge for a post it failed to send."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.money import q
from app.db.base import utcnow
from app.models.campaigns import CampaignPublisher
from app.models.enums import (
    AdStatus,
    CampaignStatus,
    ChannelStatus,
    DeliveryStatus,
    SelectionMode,
)
from app.services.settings_service import SettingsService
from app.services.telegram_gateway import TelegramError
from app.services.wallet import WalletService


# --------------------------------------------------------------------------
# Eligibility and targeting (spec §9 steps 3-4)
# --------------------------------------------------------------------------


def test_delivery_posts_once_and_records_the_message(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id, username="examchan")

    result = delivery_engine.deliver_to(channel)
    delivery = result.delivery
    assert delivery is not None, result.reason
    assert delivery.status is DeliveryStatus.SENT
    assert delivery.telegram_message_id is not None
    assert delivery.sent_at is not None
    assert delivery.measurement_ends_at > delivery.sent_at
    assert len(gateway_fixture.sent) == 1


def test_ad_post_discloses_that_it_is_an_ad(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    delivery_engine.deliver_to(channel)
    assert "Ad ·" in gateway_fixture.sent[0]["text"]


def test_click_button_routes_through_our_tracking_redirect(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    """A raw advertiser URL would be unmeasurable, so we must not use one."""
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.deliver_to(channel)
    button = gateway_fixture.sent[0]["buttons"][0]
    assert "/t/" in button["url"]
    assert str(result.delivery.id) in button["url"]
    assert "example.com/course" not in button["url"]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"countries": ["IN"]}, "country mismatch"),
        ({"languages": ["en"]}, "language mismatch"),
        ({"categories": ["crypto"]}, "category mismatch"),
    ],
)
def test_targeting_mismatches_block_delivery(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel,
    make_advertiser, funded, kwargs, expected,
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser, **kwargs)
    channel = make_channel(country="BD", language="bn", category="education")
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert result.debug["rejections"].get(expected) == 1


def test_excluded_category_blocks_delivery(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser, categories=[], excluded_categories=["education"])
    channel = make_channel(category="education")
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert result.debug["rejections"].get("category excluded") == 1


def test_publisher_category_refusal_overrides_advertiser_targeting(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    """A publisher who accepts only 'technology' must not receive gambling ads."""
    advertiser = funded(make_advertiser())
    make_campaign(advertiser, categories=["education"])
    channel = make_channel(category="education", accepted_categories=["technology"])
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert "publisher does not accept this category" in str(result.debug["rejections"])


def test_campaign_without_approved_creative_is_skipped(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    campaign = make_campaign(advertiser)
    campaign.ads[0].status = AdStatus.UNDER_REVIEW
    db.flush()
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert result.debug["rejections"].get("no approved creative") == 1


def test_unfunded_advertiser_cannot_deliver(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser
):
    """No wallet balance, no delivery — we never extend credit."""
    make_campaign(make_advertiser())  # deliberately not funded
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert "insufficient available balance" in str(result.debug["rejections"])


@pytest.mark.parametrize("status", [ChannelStatus.PENDING, ChannelStatus.SUSPENDED,
                                    ChannelStatus.REJECTED, ChannelStatus.PAUSED])
def test_non_serving_channel_statuses_are_skipped(
    db, delivery_engine, make_campaign, make_channel, make_advertiser, funded, status
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel(status=status)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert status.value in result.reason


def test_publisher_opt_out_is_respected(
    db, delivery_engine, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel(auto_advertising=False)
    assert delivery_engine.plan(channel).delivery is None


def test_blacklisted_channel_is_skipped(
    db, delivery_engine, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel()
    channel.chat.is_blacklisted = True
    db.flush()
    assert "blacklisted" in delivery_engine.plan(channel).reason


def test_channel_below_minimum_average_views_is_skipped(
    db, delivery_engine, make_campaign, make_channel, make_advertiser, funded
):
    SettingsService(db).set("min_avg_views_to_serve", "5000")
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel(avg_views=100)
    assert "below the configured minimum" in delivery_engine.plan(channel).reason


def test_advertiser_channel_exclusion_is_honoured(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    campaign = make_campaign(advertiser)
    channel = make_channel()
    db.add(CampaignPublisher(campaign_id=campaign.id, channel_id=channel.id, allowed=False))
    db.flush()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    result = delivery_engine.plan(channel)
    assert result.delivery is None
    assert "excluded by advertiser" in str(result.debug["rejections"])


def test_specific_channel_campaign_only_serves_listed_channels(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    campaign = make_campaign(advertiser, allow_specific_channels=True)
    listed, unlisted = make_channel(), make_channel()
    db.add(CampaignPublisher(campaign_id=campaign.id, channel_id=listed.id, allowed=True))
    db.flush()
    for ch in (listed, unlisted):
        gateway_fixture.register_chat(ch.telegram_chat_id)

    assert delivery_engine.plan(unlisted).delivery is None
    assert delivery_engine.plan(listed).delivery is not None


# --------------------------------------------------------------------------
# Frequency capping (spec §18)
# --------------------------------------------------------------------------


def test_minimum_interval_between_ads_is_enforced(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel(min_ad_interval_minutes=240, max_ads_per_day=10)
    gateway_fixture.register_chat(channel.telegram_chat_id)

    assert delivery_engine.deliver_to(channel).delivery is not None
    blocked = delivery_engine.plan(channel)
    assert blocked.delivery is None
    assert "minimum ad interval" in blocked.reason
    # Four hours later it is allowed again.
    later = utcnow() + timedelta(minutes=241)
    assert delivery_engine.plan(channel, at=later).delivery is not None


def test_daily_ad_cap_stops_a_channel_becoming_a_spam_feed(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    s = SettingsService(db)
    s.set("max_advertiser_inventory_share", "1.0000")   # isolate the daily cap
    s.set("hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]")
    advertiser = funded(make_advertiser(), "500000")
    make_campaign(advertiser, total_budget="400000", daily_budget="400000")
    channel = make_channel(max_ads_per_day=2, min_ad_interval_minutes=0)
    gateway_fixture.register_chat(channel.telegram_chat_id)

    at = utcnow()
    assert delivery_engine.deliver_to(channel, at=at).delivery is not None
    assert delivery_engine.deliver_to(channel, at=at).delivery is not None
    third = delivery_engine.plan(channel, at=at)
    assert third.delivery is None
    assert "daily ad cap of 2" in third.reason


def test_one_advertiser_cannot_monopolise_a_channel(
    db, delivery_engine, gateway_fixture, make_campaign, make_channel, make_advertiser, funded
):
    """Spec §32: no single advertiser takes all of a publisher's inventory."""
    s = SettingsService(db)
    s.set("max_advertiser_inventory_share", "0.5000")
    # Uniform hour weights so the test does not depend on the hour it runs at.
    s.set("hour_weights", "[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]")
    hog = funded(make_advertiser(), "900000")
    make_campaign(hog, total_budget="800000", daily_budget="800000", name="Hog")
    channel = make_channel(max_ads_per_day=4, min_ad_interval_minutes=0)
    gateway_fixture.register_chat(channel.telegram_chat_id)

    at = utcnow()
    # 50% of 4 slots = 2 deliveries for this advertiser, then it is capped out.
    assert delivery_engine.deliver_to(channel, at=at).delivery is not None
    assert delivery_engine.deliver_to(channel, at=at).delivery is not None
    blocked = delivery_engine.plan(channel, at=at)
    assert blocked.delivery is None
    assert "inventory share cap" in str(blocked.debug["rejections"])

    # A different advertiser can still buy the remaining inventory.
    rival = funded(make_advertiser(), "900000")
    make_campaign(rival, total_budget="800000", daily_budget="800000", name="Rival")
    assert delivery_engine.deliver_to(channel, at=at).delivery is not None


# --------------------------------------------------------------------------
# Selection (spec §9, §32)
# --------------------------------------------------------------------------


def test_selection_is_not_random_highest_value_wins(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    SettingsService(db).set("selection_mode", SelectionMode.FIXED_CPM.value)
    low = make_campaign(funded(make_advertiser()), bid_cpm="20", name="Low bid")
    high = make_campaign(funded(make_advertiser()), bid_cpm="200", name="High bid")
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)

    result = delivery_engine.plan(channel)
    assert result.eligible == 2
    assert result.delivery.campaign_id == high.id


def test_auction_mode_charges_second_price(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    """The winner pays just above the runner-up, not its own bid (spec §32)."""
    s = SettingsService(db)
    s.set("selection_mode", SelectionMode.AUCTION.value)
    s.set("auction_second_price_increment", "0.0100")
    s.set("quality_multiplier_floor", "1.0000")
    s.set("quality_multiplier_ceiling", "1.0000")
    make_campaign(funded(make_advertiser()), bid_cpm="100", name="Runner up")
    winner = make_campaign(funded(make_advertiser()), bid_cpm="300", name="Winner")
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)

    delivery = delivery_engine.plan(channel).delivery
    assert delivery.campaign_id == winner.id
    assert delivery.advertiser_cpm == Decimal("300.000000")
    assert delivery.effective_cpm == Decimal("101.000000")  # 100 × 1.01


def test_second_price_discount_is_shared_with_the_publisher(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    """The publisher's cut must be a share of what was actually charged."""
    s = SettingsService(db)
    s.set("selection_mode", SelectionMode.AUCTION.value)
    s.set("platform_commission_rate", "0.2000")
    s.set("quality_multiplier_floor", "1.0000")
    s.set("quality_multiplier_ceiling", "1.0000")
    make_campaign(funded(make_advertiser()), bid_cpm="100", name="Runner up")
    make_campaign(funded(make_advertiser()), bid_cpm="300", name="Winner")
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)

    delivery = delivery_engine.plan(channel).delivery
    assert delivery.effective_cpm == Decimal("101.000000")
    assert delivery.publisher_cpm == Decimal("80.800000")  # 80% of the charged CPM
    assert delivery.publisher_cpm < delivery.effective_cpm


def test_sole_bidder_pays_its_own_bid(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    s = SettingsService(db)
    s.set("selection_mode", SelectionMode.AUCTION.value)
    s.set("quality_multiplier_floor", "1.0000")
    s.set("quality_multiplier_ceiling", "1.0000")
    make_campaign(funded(make_advertiser()), bid_cpm="75", name="Only")
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    assert delivery_engine.plan(channel).delivery.effective_cpm == Decimal("75.000000")


def test_weighted_mode_is_deterministic_under_a_seed(db, delivery_engine):
    """Seeded selection must be reproducible, or the algorithm is untestable."""
    from app.services.delivery import Candidate

    SettingsService(db).set("selection_mode", SelectionMode.WEIGHTED.value)
    pool = [
        Candidate(campaign=None, advertisement=None, quote=None, score=Decimal(s))
        for s in ("400", "300", "200", "100")
    ]
    first = delivery_engine._select(pool, seed=1234)
    assert delivery_engine._select(pool, seed=1234) is first


def test_weighted_mode_favours_higher_scores_without_starving_others(db, delivery_engine):
    """Weighted delivery must spread inventory, but in proportion to value."""
    from collections import Counter

    from app.services.delivery import Candidate

    SettingsService(db).set("selection_mode", SelectionMode.WEIGHTED.value)
    pool = [
        Candidate(campaign=None, advertisement=None, quote=None, score=Decimal(s))
        for s in ("400", "100")
    ]
    picks = Counter(id(delivery_engine._select(pool, seed=n)) for n in range(400))
    top, bottom = picks[id(pool[0])], picks[id(pool[1])]
    assert top > bottom          # value wins on average
    assert bottom > 0            # but the lower bidder is not starved entirely


def test_selection_debug_explains_the_decision(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    make_campaign(funded(make_advertiser()))
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    delivery = delivery_engine.plan(channel).delivery
    factors = delivery.selection_debug["winner"]
    for key in ("effective_cpm", "relevance", "quality", "pacing_factor", "fatigue"):
        assert key in factors
    import json
    json.dumps(delivery.selection_debug)


# --------------------------------------------------------------------------
# Budget safety
# --------------------------------------------------------------------------


def test_reservation_is_taken_before_the_post_is_sent(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    advertiser = funded(make_advertiser(), "100000")
    campaign = make_campaign(advertiser)
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)

    delivery = delivery_engine.plan(channel).delivery
    wallet = WalletService(db).for_advertiser(advertiser.id)
    assert delivery.status is DeliveryStatus.RESERVED
    assert q(wallet.reserved_balance) == q(delivery.reserved_amount)
    assert q(wallet.reserved_balance) > 0


def test_failed_send_releases_the_whole_reservation(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    """A Telegram failure must cost the advertiser nothing."""
    advertiser = funded(make_advertiser(), "100000")
    make_campaign(advertiser)
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    wallets = WalletService(db)
    before = q(wallets.for_advertiser(advertiser.id).available_balance)

    gateway_fixture.fail_next_send = "Bad Request: chat not found"
    with pytest.raises(TelegramError):
        delivery_engine.deliver_to(channel)

    wallet = wallets.for_advertiser(advertiser.id)
    assert q(wallet.available_balance) == before
    assert q(wallet.reserved_balance) == Decimal("0.000000")
    assert all(v == 0 for v in wallets.verify_against_ledger(wallet).values())


def test_dispatch_is_not_repeated_for_an_already_sent_delivery(
    db, delivery_engine, gateway_fixture, make_channel, make_campaign, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser)
    channel = make_channel()
    gateway_fixture.register_chat(channel.telegram_chat_id)
    delivery = delivery_engine.deliver_to(channel).delivery
    delivery_engine.dispatch(delivery)  # must be a no-op
    assert len(gateway_fixture.sent) == 1


def test_expired_campaign_does_not_serve(
    db, delivery_engine, make_channel, make_campaign, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    now = utcnow()
    make_campaign(advertiser, starts_at=now - timedelta(days=10),
                  ends_at=now - timedelta(days=1))
    assert delivery_engine.plan(make_channel()).delivery is None


def test_future_campaign_does_not_serve(
    db, delivery_engine, make_channel, make_campaign, make_advertiser, funded
):
    advertiser = funded(make_advertiser())
    now = utcnow()
    make_campaign(advertiser, starts_at=now + timedelta(days=1),
                  ends_at=now + timedelta(days=10))
    assert delivery_engine.plan(make_channel()).delivery is None


@pytest.mark.parametrize("status", [CampaignStatus.PAUSED, CampaignStatus.DRAFT,
                                    CampaignStatus.SUBMITTED, CampaignStatus.REJECTED,
                                    CampaignStatus.COMPLETED, CampaignStatus.SUSPENDED])
def test_only_running_campaigns_serve(
    db, delivery_engine, make_channel, make_campaign, make_advertiser, funded, status
):
    advertiser = funded(make_advertiser())
    make_campaign(advertiser, status=status)
    assert delivery_engine.plan(make_channel()).delivery is None
