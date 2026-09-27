"""Analytics and reporting (spec §21, §33).

Every figure here is derived from the ledger or the impression table, never from a
client-supplied number. Impression counts are reported by kind so a reader can
tell measured reach from an estimate (spec §37).
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.money import ZERO, D, pct, q
from app.db.base import utcnow
from app.models.campaigns import Campaign
from app.models.delivery import AdDelivery, Click, Impression
from app.models.enums import (
    AccountKind,
    CampaignStatus,
    ChannelStatus,
    DepositStatus,
    EarningStatus,
    TransactionStatus,
    TransactionType,
    WithdrawalStatus,
)
from app.models.identity import Advertiser, Publisher
from app.models.money import (
    DailyFinancialSnapshot,
    Deposit,
    LedgerTransaction,
    PublisherEarning,
    Refund,
    Withdrawal,
)
from app.models.telegram import PublisherChannel


def _sum(session: Session, column, *where) -> Decimal:
    return q(session.scalar(select(func.coalesce(func.sum(column), 0)).where(*where)) or 0)


def _count(session: Session, column, *where) -> int:
    return int(session.scalar(select(func.count(column)).where(*where)) or 0)


@dataclass(frozen=True)
class DateRange:
    start: datetime
    end: datetime

    @classmethod
    def last_days(cls, days: int = 30) -> "DateRange":
        end = utcnow()
        return cls(end - timedelta(days=days), end)

    @classmethod
    def today(cls) -> "DateRange":
        now = utcnow()
        return cls(now.replace(hour=0, minute=0, second=0, microsecond=0), now)


class AnalyticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Advertiser dashboard (spec §21)
    # ------------------------------------------------------------------

    def advertiser_overview(
        self, advertiser_id: uuid.UUID, window: DateRange | None = None
    ) -> dict[str, Any]:
        window = window or DateRange.last_days(30)
        campaigns = self.session.scalars(
            select(Campaign).where(Campaign.advertiser_id == advertiser_id)
        ).all()
        campaign_ids = [c.id for c in campaigns]

        spend = q(sum((q(c.spent_amount) for c in campaigns), ZERO))
        impressions = sum(c.billable_impressions for c in campaigns)
        clicks = sum(c.clicks for c in campaigns)

        return {
            "campaigns_total": len(campaigns),
            "campaigns_active": sum(
                1 for c in campaigns if c.status is CampaignStatus.RUNNING
            ),
            "spend": spend,
            "billable_impressions": impressions,
            "clicks": clicks,
            "ctr_percent": pct(clicks, impressions),
            "effective_cpm": q(spend * 1000 / D(impressions)) if impressions else ZERO,
            "cost_per_click": q(spend / D(clicks)) if clicks else ZERO,
            "remaining_budget": q(
                sum((q(c.remaining_budget) for c in campaigns), ZERO)
            ),
            "deliveries": _count(
                self.session, AdDelivery.id,
                AdDelivery.campaign_id.in_(campaign_ids or [uuid.uuid4()]),
                AdDelivery.sent_at.is_not(None),
            ),
            "window": {"start": window.start, "end": window.end},
        }

    def advertiser_daily(
        self, advertiser_id: uuid.UUID, days: int = 30
    ) -> list[dict[str, Any]]:
        """Daily spend/impressions/clicks for the performance chart."""
        since = utcnow() - timedelta(days=days)
        rows = self.session.execute(
            select(
                func.date(Impression.occurred_at).label("day"),
                func.coalesce(func.sum(Impression.quantity), 0),
                func.coalesce(func.sum(Impression.unit_advertiser_cost * Impression.quantity), 0),
            )
            .join(Campaign, Campaign.id == Impression.campaign_id)
            .where(
                Campaign.advertiser_id == advertiser_id,
                Impression.billable.is_(True),
                Impression.occurred_at >= since,
            )
            .group_by(func.date(Impression.occurred_at))
            .order_by(func.date(Impression.occurred_at))
        ).all()
        return [
            {"date": str(day), "impressions": int(impressions or 0), "spend": q(spend or 0)}
            for day, impressions, spend in rows
        ]

    def campaign_breakdown(
        self, campaign_id: uuid.UUID, by: str = "publisher"
    ) -> list[dict[str, Any]]:
        """Per-publisher, per-country or per-category performance (spec §21)."""
        group = {
            "publisher": PublisherChannel.id,
            "country": PublisherChannel.country,
            "category": PublisherChannel.category,
        }.get(by)
        if group is None:
            raise ValueError(f"unsupported breakdown {by!r}")

        rows = self.session.execute(
            select(
                group,
                func.coalesce(func.sum(AdDelivery.billable_impressions), 0),
                func.coalesce(func.sum(AdDelivery.clicks), 0),
                func.coalesce(func.sum(AdDelivery.settled_amount), 0),
                func.count(AdDelivery.id),
            )
            .join(PublisherChannel, PublisherChannel.id == AdDelivery.channel_id)
            .where(AdDelivery.campaign_id == campaign_id)
            .group_by(group)
            .order_by(func.coalesce(func.sum(AdDelivery.billable_impressions), 0).desc())
        ).all()
        out = []
        for key, impressions, clicks, spend, deliveries in rows:
            impressions, clicks = int(impressions or 0), int(clicks or 0)
            out.append(
                {
                    by: str(key) if key is not None else "unknown",
                    "impressions": impressions,
                    "clicks": clicks,
                    "ctr_percent": pct(clicks, impressions),
                    "spend": q(spend or 0),
                    "deliveries": int(deliveries or 0),
                    "effective_cpm": q(q(spend or 0) * 1000 / D(impressions))
                    if impressions else ZERO,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Publisher dashboard (spec §21)
    # ------------------------------------------------------------------

    def publisher_overview(self, publisher_id: uuid.UUID) -> dict[str, Any]:
        channels = self.session.scalars(
            select(PublisherChannel).where(PublisherChannel.publisher_id == publisher_id)
        ).all()

        def earnings(status: EarningStatus) -> Decimal:
            return _sum(
                self.session, PublisherEarning.net_amount,
                PublisherEarning.publisher_id == publisher_id,
                PublisherEarning.status == status,
            )

        impressions = _sum(
            self.session, PublisherEarning.billable_impressions,
            PublisherEarning.publisher_id == publisher_id,
        )
        impressions = int(impressions)
        clicks = sum(c.total_clicks for c in channels)
        pending, confirmed = earnings(EarningStatus.PENDING), earnings(EarningStatus.CONFIRMED)
        paid = earnings(EarningStatus.PAID)
        total = q(pending + confirmed + paid)

        return {
            "channels_total": len(channels),
            "channels_active": sum(1 for c in channels if c.status.can_serve),
            "ads_served": sum(c.total_ads_served for c in channels),
            "billable_impressions": impressions,
            "clicks": clicks,
            "ctr_percent": pct(clicks, impressions),
            "pending_balance": pending,
            "confirmed_balance": confirmed,
            "paid_total": paid,
            "total_earned": total,
            "effective_cpm": q(total * 1000 / D(impressions)) if impressions else ZERO,
            "withdrawals_pending": _count(
                self.session, Withdrawal.id,
                Withdrawal.publisher_id == publisher_id,
                Withdrawal.status.in_(
                    [WithdrawalStatus.PENDING, WithdrawalStatus.PROCESSING]
                ),
            ),
        }

    def publisher_daily(self, publisher_id: uuid.UUID, days: int = 30) -> list[dict[str, Any]]:
        since = utcnow() - timedelta(days=days)
        rows = self.session.execute(
            select(
                func.date(PublisherEarning.created_at),
                func.coalesce(func.sum(PublisherEarning.billable_impressions), 0),
                func.coalesce(func.sum(PublisherEarning.net_amount), 0),
            )
            .where(
                PublisherEarning.publisher_id == publisher_id,
                PublisherEarning.created_at >= since,
                PublisherEarning.status != EarningStatus.REVERSED,
            )
            .group_by(func.date(PublisherEarning.created_at))
            .order_by(func.date(PublisherEarning.created_at))
        ).all()
        return [
            {"date": str(day), "impressions": int(impressions or 0), "earnings": q(amount or 0)}
            for day, impressions, amount in rows
        ]

    def channel_performance(self, publisher_id: uuid.UUID) -> list[dict[str, Any]]:
        channels = self.session.scalars(
            select(PublisherChannel).where(PublisherChannel.publisher_id == publisher_id)
        ).all()
        out = []
        for channel in channels:
            out.append(
                {
                    "channel_id": str(channel.id),
                    "title": channel.chat.title if channel.chat else None,
                    "username": channel.chat.username if channel.chat else None,
                    "status": channel.status.value,
                    "members": channel.chat.member_count if channel.chat else 0,
                    "avg_views": channel.avg_views,
                    "avg_ad_views": channel.avg_ad_views,
                    "impressions": channel.total_impressions,
                    "clicks": channel.total_clicks,
                    "ctr_percent": pct(channel.total_clicks, channel.total_impressions),
                    "ads_served": channel.total_ads_served,
                    "earned": q(channel.lifetime_earned),
                    # A band, never the internal weights (spec §17).
                    "quality_band": _band(D(channel.quality_score or 0)),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Admin dashboard (spec §21, §23)
    # ------------------------------------------------------------------

    def platform_overview(self, currency: str = "BDT") -> dict[str, Any]:
        today = DateRange.today()

        gross_spend = _sum(
            self.session, LedgerTransaction.amount,
            LedgerTransaction.transaction_type == TransactionType.SETTLEMENT,
            LedgerTransaction.status == TransactionStatus.POSTED,
            LedgerTransaction.currency == currency,
        )
        publisher_revenue = _sum(
            self.session, PublisherEarning.net_amount,
            PublisherEarning.status != EarningStatus.REVERSED,
            PublisherEarning.currency == currency,
        )

        from app.services.ledger import LedgerService

        ledger = LedgerService(self.session)
        platform_revenue = ledger.balance(AccountKind.PLATFORM_REVENUE, currency)
        platform_fees = ledger.balance(AccountKind.PLATFORM_FEES, currency)
        clawed_back = ledger.balance(AccountKind.FRAUD_CLAWBACK, currency)

        return {
            "currency": currency,
            "gross_ad_spend": gross_spend,
            "platform_revenue": platform_revenue,
            "platform_fees": platform_fees,
            "publisher_revenue": publisher_revenue,
            "fraud_reversed": clawed_back,
            "advertiser_deposits": _sum(
                self.session, Deposit.net_amount,
                Deposit.status == DepositStatus.CONFIRMED,
                Deposit.currency == currency,
            ),
            "refunds": _sum(
                self.session, Refund.approved_amount, Refund.currency == currency
            ),
            "withdrawals_paid": _sum(
                self.session, Withdrawal.net_amount,
                Withdrawal.status == WithdrawalStatus.PAID,
                Withdrawal.currency == currency,
            ),
            "pending_withdrawals_count": _count(
                self.session, Withdrawal.id,
                Withdrawal.status.in_(
                    [WithdrawalStatus.PENDING, WithdrawalStatus.PROCESSING]
                ),
            ),
            "pending_withdrawals_amount": _sum(
                self.session, Withdrawal.net_amount,
                Withdrawal.status.in_(
                    [WithdrawalStatus.PENDING, WithdrawalStatus.PROCESSING]
                ),
            ),
            "active_campaigns": _count(
                self.session, Campaign.id, Campaign.status == CampaignStatus.RUNNING
            ),
            "campaigns_awaiting_review": _count(
                self.session, Campaign.id,
                Campaign.status.in_(
                    [CampaignStatus.SUBMITTED, CampaignStatus.UNDER_REVIEW]
                ),
            ),
            "active_publishers": _count(
                self.session, func.distinct(PublisherChannel.publisher_id),
                PublisherChannel.status.in_(
                    [ChannelStatus.ACTIVE, ChannelStatus.VERIFIED]
                ),
            ),
            "active_channels": _count(
                self.session, PublisherChannel.id,
                PublisherChannel.status.in_(
                    [ChannelStatus.ACTIVE, ChannelStatus.VERIFIED]
                ),
            ),
            "channels_awaiting_review": _count(
                self.session, PublisherChannel.id,
                PublisherChannel.status == ChannelStatus.PENDING,
            ),
            "advertisers_total": _count(self.session, Advertiser.id),
            "publishers_total": _count(self.session, Publisher.id),
            "total_billable_impressions": int(
                _sum(self.session, Impression.quantity, Impression.billable.is_(True))
            ),
            "total_clicks": _count(self.session, Click.id, Click.valid.is_(True)),
            "impressions_today": int(
                _sum(
                    self.session, Impression.quantity,
                    Impression.billable.is_(True),
                    Impression.occurred_at >= today.start,
                )
            ),
            "clicks_today": _count(
                self.session, Click.id,
                Click.valid.is_(True), Click.occurred_at >= today.start,
            ),
            "trial_balance": ledger.trial_balance(currency),
        }

    # ------------------------------------------------------------------
    # Daily financial snapshot (spec §33)
    # ------------------------------------------------------------------

    def build_snapshot(self, day: date | None = None, currency: str = "BDT") -> DailyFinancialSnapshot:
        """Aggregate one day's finances. Idempotent: rebuilds in place."""
        day = day or (utcnow().date() - timedelta(days=1))
        start = datetime.combine(day, datetime.min.time()).replace(
            tzinfo=utcnow().tzinfo
        )
        end = start + timedelta(days=1)

        def between(column):
            return (column >= start, column < end)

        gross = _sum(
            self.session, LedgerTransaction.amount,
            LedgerTransaction.transaction_type == TransactionType.SETTLEMENT,
            LedgerTransaction.status == TransactionStatus.POSTED,
            LedgerTransaction.currency == currency,
            *between(LedgerTransaction.created_at),
        )
        publisher_payout = _sum(
            self.session, PublisherEarning.net_amount,
            PublisherEarning.currency == currency,
            PublisherEarning.status != EarningStatus.REVERSED,
            *between(PublisherEarning.created_at),
        )

        row = self.session.scalars(
            select(DailyFinancialSnapshot).where(
                DailyFinancialSnapshot.snapshot_date == day
            )
        ).one_or_none()
        if row is None:
            row = DailyFinancialSnapshot(
                snapshot_date=day, currency=currency, created_at=utcnow()
            )
            self.session.add(row)

        row.gross_ad_spend = gross
        row.publisher_payout = publisher_payout
        # Platform revenue is the residual of what was charged and what was owed:
        # derived, so the three figures always reconcile.
        row.platform_revenue = q(gross - publisher_payout)
        row.advertiser_deposits = _sum(
            self.session, Deposit.net_amount,
            Deposit.status == DepositStatus.CONFIRMED, Deposit.currency == currency,
            *between(Deposit.confirmed_at),
        )
        row.refunds = _sum(
            self.session, Refund.approved_amount, Refund.currency == currency,
            *between(Refund.decided_at),
        )
        row.withdrawals_paid = _sum(
            self.session, Withdrawal.net_amount,
            Withdrawal.status == WithdrawalStatus.PAID, Withdrawal.currency == currency,
            *between(Withdrawal.processed_at),
        )
        row.withdrawal_fees = _sum(
            self.session, Withdrawal.fee,
            Withdrawal.status == WithdrawalStatus.PAID, Withdrawal.currency == currency,
            *between(Withdrawal.processed_at),
        )
        row.fraud_reversed = _sum(
            self.session, PublisherEarning.net_amount,
            PublisherEarning.status == EarningStatus.REVERSED,
            *between(PublisherEarning.reversed_at),
        )
        row.billable_impressions = int(
            _sum(
                self.session, Impression.quantity, Impression.billable.is_(True),
                *between(Impression.occurred_at),
            )
        )
        row.clicks = _count(
            self.session, Click.id, Click.valid.is_(True), *between(Click.occurred_at)
        )
        row.active_campaigns = _count(
            self.session, Campaign.id, Campaign.status == CampaignStatus.RUNNING
        )
        row.active_channels = _count(
            self.session, PublisherChannel.id,
            PublisherChannel.status.in_([ChannelStatus.ACTIVE, ChannelStatus.VERIFIED]),
        )
        self.session.flush()
        return row

    def snapshots(self, days: int = 30) -> list[DailyFinancialSnapshot]:
        since = utcnow().date() - timedelta(days=days)
        return list(
            self.session.scalars(
                select(DailyFinancialSnapshot)
                .where(DailyFinancialSnapshot.snapshot_date >= since)
                .order_by(DailyFinancialSnapshot.snapshot_date.desc())
            ).all()
        )

    # ------------------------------------------------------------------
    # CSV export (spec §33)
    # ------------------------------------------------------------------

    @staticmethod
    def to_csv(rows: Iterable[dict[str, Any]], columns: list[str] | None = None) -> str:
        rows = list(rows)
        if not rows:
            return ""
        columns = columns or list(rows[0].keys())
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(row.get(k)) for k in columns})
        return buffer.getvalue()

    def advertiser_report_csv(self, advertiser_id: uuid.UUID, days: int = 30) -> str:
        """Spec §33: date, campaign, spend, impressions, clicks, CTR, CPM."""
        since = utcnow() - timedelta(days=days)
        rows = self.session.execute(
            select(
                func.date(Impression.occurred_at),
                Campaign.name,
                func.coalesce(func.sum(Impression.quantity), 0),
                func.coalesce(
                    func.sum(Impression.unit_advertiser_cost * Impression.quantity), 0
                ),
            )
            .join(Campaign, Campaign.id == Impression.campaign_id)
            .where(
                Campaign.advertiser_id == advertiser_id,
                Impression.billable.is_(True),
                Impression.occurred_at >= since,
            )
            .group_by(func.date(Impression.occurred_at), Campaign.name)
            .order_by(func.date(Impression.occurred_at))
        ).all()
        out = []
        for day, name, impressions, spend in rows:
            impressions = int(impressions or 0)
            spend = q(spend or 0)
            clicks = _count(
                self.session, Click.id,
                Click.campaign_id.in_(
                    select(Campaign.id).where(
                        Campaign.advertiser_id == advertiser_id, Campaign.name == name
                    )
                ),
                Click.valid.is_(True),
                func.date(Click.occurred_at) == day,
            )
            out.append(
                {
                    "date": str(day), "campaign": name, "spend": spend,
                    "impressions": impressions, "clicks": clicks,
                    "ctr_percent": pct(clicks, impressions),
                    "cpm": q(spend * 1000 / D(impressions)) if impressions else ZERO,
                }
            )
        return self.to_csv(
            out, ["date", "campaign", "spend", "impressions", "clicks",
                  "ctr_percent", "cpm"]
        )

    def publisher_report_csv(self, publisher_id: uuid.UUID, days: int = 30) -> str:
        """Spec §33: date, channel, impressions, ads, CPM, revenue."""
        since = utcnow() - timedelta(days=days)
        rows = self.session.execute(
            select(
                func.date(PublisherEarning.created_at),
                PublisherEarning.channel_id,
                func.coalesce(func.sum(PublisherEarning.billable_impressions), 0),
                func.coalesce(func.sum(PublisherEarning.net_amount), 0),
                func.count(PublisherEarning.id),
            )
            .where(
                PublisherEarning.publisher_id == publisher_id,
                PublisherEarning.created_at >= since,
                PublisherEarning.status != EarningStatus.REVERSED,
            )
            .group_by(func.date(PublisherEarning.created_at), PublisherEarning.channel_id)
            .order_by(func.date(PublisherEarning.created_at))
        ).all()
        out = []
        for day, channel_id, impressions, revenue, ads in rows:
            channel = self.session.get(PublisherChannel, channel_id)
            impressions = int(impressions or 0)
            revenue = q(revenue or 0)
            out.append(
                {
                    "date": str(day),
                    "channel": (channel.chat.title if channel and channel.chat else "-"),
                    "impressions": impressions,
                    "ads": int(ads or 0),
                    "cpm": q(revenue * 1000 / D(impressions)) if impressions else ZERO,
                    "revenue": revenue,
                }
            )
        return self.to_csv(out, ["date", "channel", "impressions", "ads", "cpm", "revenue"])

    def financial_report_csv(self, days: int = 30) -> str:
        """Spec §33 admin report, including net revenue."""
        rows = []
        for snapshot in self.snapshots(days):
            net = q(
                D(snapshot.platform_revenue)
                + D(snapshot.withdrawal_fees)
                - D(snapshot.refunds)
            )
            rows.append(
                {
                    "date": str(snapshot.snapshot_date),
                    "gross_ad_spend": q(snapshot.gross_ad_spend),
                    "publisher_payout": q(snapshot.publisher_payout),
                    "platform_revenue": q(snapshot.platform_revenue),
                    "refunds": q(snapshot.refunds),
                    "withdrawal_fees": q(snapshot.withdrawal_fees),
                    "fraud_reversed": q(snapshot.fraud_reversed),
                    "net_revenue": net,
                }
            )
        return self.to_csv(
            rows,
            ["date", "gross_ad_spend", "publisher_payout", "platform_revenue",
             "refunds", "withdrawal_fees", "fraud_reversed", "net_revenue"],
        )


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")   # never scientific notation in a report
    return str(value)


def _band(score: Decimal) -> str:
    if score < Decimal("0.25"):
        return "poor"
    if score < Decimal("0.50"):
        return "fair"
    if score < Decimal("0.75"):
        return "good"
    return "excellent"
