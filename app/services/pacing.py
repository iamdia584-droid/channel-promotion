"""Campaign pacing (spec §10).

A campaign must not burn its whole budget in the first hour. Two limits apply:

* **Daily** — ``campaign_daily_spend`` per (campaign, date), capped at
  ``daily_budget``. This row in Postgres is authoritative; Redis only mirrors it
  for fast pre-checks.
* **Hourly** — the day's budget is spread across 24 hours by configurable
  weights, and a campaign may run at most ``pacing_burst_ratio`` ahead of the
  cumulative target for the current hour.

Budget is *reserved* before an ad is sent and released if sending fails, so a
crash between "reserve" and "post" can never overspend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.money import ZERO, D, q
from app.db.base import utcnow
from app.models.campaigns import Campaign, CampaignDailySpend
from app.services.settings_service import SettingsService


@dataclass(frozen=True)
class PacingDecision:
    allowed: bool
    headroom: Decimal
    reason: str = ""
    daily_remaining: Decimal = ZERO
    hourly_target: Decimal = ZERO
    spent_today: Decimal = ZERO

    def __bool__(self) -> bool:
        return self.allowed


class PacingService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = SettingsService(session)

    # -- daily counter -----------------------------------------------------

    def daily_row(self, campaign_id: uuid.UUID, day: date | None = None) -> CampaignDailySpend:
        day = day or utcnow().date()
        row = self.session.scalars(
            select(CampaignDailySpend).where(
                CampaignDailySpend.campaign_id == campaign_id,
                CampaignDailySpend.spend_date == day,
            )
        ).one_or_none()
        if row is not None:
            return row
        try:
            with self.session.begin_nested():
                row = CampaignDailySpend(campaign_id=campaign_id, spend_date=day)
                self.session.add(row)
                self.session.flush()
        except IntegrityError:  # a concurrent creator won the race
            row = self.session.scalars(
                select(CampaignDailySpend).where(
                    CampaignDailySpend.campaign_id == campaign_id,
                    CampaignDailySpend.spend_date == day,
                )
            ).one()
        return row

    # -- hourly shape ------------------------------------------------------

    def hour_weights(self) -> list[Decimal]:
        raw = self.settings.get("hour_weights")
        if not isinstance(raw, list) or len(raw) != 24:
            return [Decimal(1)] * 24
        weights = [D(str(w)) for w in raw]
        return weights if sum(weights) > 0 else [Decimal(1)] * 24

    def hourly_target(self, daily_budget: Decimal, at: datetime) -> Decimal:
        """Cumulative spend the campaign *should* have reached by now.

        Includes the current hour in full, so a campaign is allowed to spend
        within the hour it has entered rather than only after it ends.
        """
        weights = self.hour_weights()
        total = sum(weights, ZERO)
        elapsed = sum(weights[: at.hour + 1], ZERO)
        return q(D(daily_budget) * elapsed / total)

    # -- the decision ------------------------------------------------------

    def check(
        self, campaign: Campaign, amount: object, at: datetime | None = None
    ) -> PacingDecision:
        at = at or utcnow()
        amount = q(amount)
        row = self.daily_row(campaign.id, at.date())
        committed = q(D(row.spent) + D(row.reserved))

        campaign_remaining = q(campaign.remaining_budget)
        if campaign_remaining <= ZERO:
            return PacingDecision(False, ZERO, "campaign budget is exhausted")

        daily_remaining = q(D(campaign.daily_budget) - committed)
        if daily_remaining <= ZERO:
            return PacingDecision(
                False, ZERO, "daily budget is exhausted",
                daily_remaining=ZERO, spent_today=committed,
            )

        burst = self.settings.decimal("pacing_burst_ratio")
        target = self.hourly_target(q(campaign.daily_budget), at)
        allowance = q(target * burst)
        hourly_remaining = q(allowance - committed)
        if hourly_remaining <= ZERO:
            return PacingDecision(
                False, ZERO,
                f"ahead of pace: {committed} committed against an allowance of {allowance}",
                daily_remaining=daily_remaining, hourly_target=target, spent_today=committed,
            )

        headroom = min(daily_remaining, hourly_remaining, campaign_remaining)
        if headroom < amount:
            return PacingDecision(
                False, headroom,
                f"requested {amount} exceeds pacing headroom {headroom}",
                daily_remaining=daily_remaining, hourly_target=target, spent_today=committed,
            )
        return PacingDecision(
            True, headroom, "", daily_remaining=daily_remaining,
            hourly_target=target, spent_today=committed,
        )

    # -- counters ----------------------------------------------------------

    def reserve(self, campaign: Campaign, amount: object, at: datetime | None = None) -> None:
        at = at or utcnow()
        row = self.daily_row(campaign.id, at.date())
        row.reserved = q(D(row.reserved) + q(amount))
        row.deliveries += 1
        self.session.flush()

    def release(self, campaign: Campaign, amount: object, at: datetime | None = None) -> None:
        at = at or utcnow()
        row = self.daily_row(campaign.id, at.date())
        row.reserved = max(ZERO, q(D(row.reserved) - q(amount)))
        self.session.flush()

    def commit_spend(
        self,
        campaign: Campaign,
        reserved: object,
        spent: object,
        impressions: int = 0,
        clicks: int = 0,
        at: datetime | None = None,
    ) -> None:
        """Move a delivery's reservation into realised spend."""
        at = at or utcnow()
        row = self.daily_row(campaign.id, at.date())
        row.reserved = max(ZERO, q(D(row.reserved) - q(reserved)))
        row.spent = q(D(row.spent) + q(spent))
        row.impressions += impressions
        row.clicks += clicks
        self.session.flush()

    def spend_today(self, campaign_id: uuid.UUID, at: datetime | None = None) -> Decimal:
        row = self.daily_row(campaign_id, (at or utcnow()).date())
        return q(row.spent)
