"""Analytics must derive every figure from the ledger, and reconcile."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

from app.core.money import q
from app.db.base import utcnow
from app.models.enums import ImpressionKind, ImpressionSource
from app.services.analytics import AnalyticsService, DateRange
from app.services.impressions import ImpressionService
from app.services.settlement import SettlementService


def _run(db, sent_delivery, impressions=10_000, clicks=25, **kw):
    delivery, campaign, channel, advertiser = sent_delivery(
        avg_views=50_000, bid_cpm="100", commission="0.20", **kw
    )
    service = ImpressionService(db)
    service.record(
        delivery, kind=ImpressionKind.MEASURED, source=ImpressionSource.TRACKING_LINK,
        dedupe_key="a1", quantity=impressions,
    )
    for i in range(clicks):
        service.record_click(delivery, dedupe_key=f"ck:{i}", telegram_user_id=i)
    SettlementService(db).settle_delivery(delivery)
    return delivery, campaign, channel, advertiser


def test_advertiser_overview_matches_the_ledger(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    overview = AnalyticsService(db).advertiser_overview(advertiser.id)

    assert overview["billable_impressions"] == 10_000
    assert overview["spend"] == Decimal("1000.000000")     # 10,000 at ৳100 CPM
    assert overview["clicks"] == 25
    assert overview["effective_cpm"] == Decimal("100.000000")
    assert overview["ctr_percent"] == Decimal("0.2500")
    assert overview["cost_per_click"] == Decimal("40.000000")
    assert overview["deliveries"] == 1
    assert overview["campaigns_total"] == 1


def test_publisher_overview_reports_pending_separately(db, sent_delivery):
    """A publisher must see clearly what is not yet withdrawable."""
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    overview = AnalyticsService(db).publisher_overview(delivery.publisher_id)

    assert overview["billable_impressions"] == 10_000
    assert overview["pending_balance"] == Decimal("800.000000")   # 80% of ৳1,000
    assert overview["confirmed_balance"] == Decimal("0.000000")
    assert overview["total_earned"] == Decimal("800.000000")
    assert overview["effective_cpm"] == Decimal("80.000000")
    assert overview["channels_active"] == 1
    assert overview["ads_served"] == 1


def test_platform_overview_reconciles_the_three_parties(db, sent_delivery):
    """Advertiser spend must equal publisher revenue plus platform revenue."""
    _run(db, sent_delivery)
    overview = AnalyticsService(db).platform_overview()

    assert overview["gross_ad_spend"] == Decimal("1000.000000")
    assert overview["publisher_revenue"] == Decimal("800.000000")
    assert overview["platform_revenue"] == Decimal("200.000000")
    assert (
        overview["publisher_revenue"] + overview["platform_revenue"]
        == overview["gross_ad_spend"]
    )
    # And the books balance.
    assert overview["trial_balance"]["difference"] == Decimal("0.000000")


def test_platform_overview_counts_queues_for_the_admin_dashboard(db, sent_delivery,
                                                                 make_channel):
    _run(db, sent_delivery)
    from app.models.enums import ChannelStatus

    make_channel(status=ChannelStatus.PENDING)
    overview = AnalyticsService(db).platform_overview()
    assert overview["channels_awaiting_review"] == 1
    assert overview["active_channels"] >= 1
    assert overview["total_clicks"] == 25
    assert overview["impressions_today"] == 10_000


def test_campaign_breakdown_by_publisher(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    rows = AnalyticsService(db).campaign_breakdown(campaign.id, by="publisher")
    assert len(rows) == 1
    assert rows[0]["impressions"] == 10_000
    assert rows[0]["clicks"] == 25
    assert rows[0]["spend"] == Decimal("1000.000000")
    assert rows[0]["effective_cpm"] == Decimal("100.000000")


def test_campaign_breakdown_by_country_and_category(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    by_country = AnalyticsService(db).campaign_breakdown(campaign.id, by="country")
    by_category = AnalyticsService(db).campaign_breakdown(campaign.id, by="category")
    assert by_country[0]["country"] == "BD"
    assert by_category[0]["category"] == "education"


def test_channel_performance_shows_a_band_not_the_score(db, sent_delivery):
    """Spec §17: internal quality weights must not leak to publishers."""
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    rows = AnalyticsService(db).channel_performance(delivery.publisher_id)
    assert rows[0]["quality_band"] in {"poor", "fair", "good", "excellent"}
    assert "quality_score" not in rows[0]
    assert "fraud_score" not in rows[0]


def test_daily_snapshot_reconciles_and_is_idempotent(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    service = AnalyticsService(db)
    today = utcnow().date()

    first = service.build_snapshot(today)
    assert first.gross_ad_spend == Decimal("1000.000000")
    assert first.publisher_payout == Decimal("800.000000")
    assert first.platform_revenue == Decimal("200.000000")
    assert first.platform_revenue + first.publisher_payout == first.gross_ad_spend
    assert first.billable_impressions == 10_000
    assert first.clicks == 25

    # Rebuilding must not double-count.
    second = service.build_snapshot(today)
    assert second.id == first.id
    assert second.gross_ad_spend == Decimal("1000.000000")


def test_advertiser_report_csv_has_the_columns_the_spec_names(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    output = AnalyticsService(db).advertiser_report_csv(advertiser.id)
    rows = list(csv.DictReader(io.StringIO(output)))
    assert list(rows[0]) == [
        "date", "campaign", "spend", "impressions", "clicks", "ctr_percent", "cpm"
    ]
    assert rows[0]["impressions"] == "10000"
    assert rows[0]["spend"] == "1000.000000"
    assert rows[0]["cpm"] == "100.000000"


def test_publisher_report_csv_has_the_columns_the_spec_names(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    output = AnalyticsService(db).publisher_report_csv(delivery.publisher_id)
    rows = list(csv.DictReader(io.StringIO(output)))
    assert list(rows[0]) == ["date", "channel", "impressions", "ads", "cpm", "revenue"]
    assert rows[0]["revenue"] == "800.000000"
    assert rows[0]["cpm"] == "80.000000"


def test_financial_report_csv_includes_net_revenue(db, sent_delivery):
    delivery, campaign, channel, advertiser = _run(db, sent_delivery)
    service = AnalyticsService(db)
    service.build_snapshot(utcnow().date())
    output = service.financial_report_csv()
    rows = list(csv.DictReader(io.StringIO(output)))
    assert "net_revenue" in rows[0]
    assert rows[0]["gross_ad_spend"] == "1000.000000"
    assert rows[0]["publisher_payout"] == "800.000000"


def test_csv_never_uses_scientific_notation(db):
    """A report showing 1E+3 instead of 1000 is unusable for accounting."""
    output = AnalyticsService.to_csv(
        [{"amount": Decimal("1000"), "tiny": Decimal("0.000001")}]
    )
    assert "E" not in output and "e+" not in output
    assert "1000" in output and "0.000001" in output


def test_empty_csv_is_empty_not_a_crash(db):
    assert AnalyticsService.to_csv([]) == ""


def test_reversed_earnings_are_excluded_from_publisher_totals(db, sent_delivery):
    from app.models.money import PublisherEarning
    from app.services.earnings import EarningsService

    delivery, campaign, channel, advertiser = _run(db, sent_delivery, clicks=0)
    EarningsService(db).reverse(
        db.query(PublisherEarning).one(), "confirmed bot traffic"
    )
    overview = AnalyticsService(db).publisher_overview(delivery.publisher_id)
    assert overview["total_earned"] == Decimal("0.000000")
    assert overview["pending_balance"] == Decimal("0.000000")


def test_date_range_helpers():
    window = DateRange.last_days(7)
    assert (window.end - window.start).days == 7
    today = DateRange.today()
    assert today.start.hour == 0 and today.start.minute == 0
