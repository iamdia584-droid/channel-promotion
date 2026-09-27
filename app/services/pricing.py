"""Pricing engine (spec §7, §31).

    effective_cpm = advertiser_bid
                    × country_multiplier
                    × category_multiplier
                    × quality_multiplier
                    × format_multiplier
                    × size_multiplier
    commission    = effective_cpm × commission_rate
    publisher_cpm = effective_cpm − commission

Every multiplier comes from ``pricing_rules`` and every rate from
``system_settings``; nothing is hard-coded. A quote is returned frozen and is
persisted onto the delivery row, so changing a rule tomorrow cannot retroactively
alter what an already-served delivery owes (spec §31: server-side only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.money import D, q, split_commission
from app.db.base import utcnow
from app.models.campaigns import Campaign
from app.models.enums import CampaignType, PricingRuleScope
from app.models.ops import PricingRule
from app.models.telegram import PublisherChannel
from app.services.settings_service import SettingsService

ONE = Decimal(1)


def size_band(member_count: int) -> str:
    """Coarse size buckets, so a rule can price small and large inventory apart."""
    if member_count < 1_000:
        return "micro"
    if member_count < 10_000:
        return "small"
    if member_count < 100_000:
        return "medium"
    if member_count < 1_000_000:
        return "large"
    return "mega"


def quality_tier(score: Decimal) -> str:
    score = D(score)
    if score < Decimal("0.25"):
        return "poor"
    if score < Decimal("0.50"):
        return "fair"
    if score < Decimal("0.75"):
        return "good"
    return "excellent"


@dataclass(frozen=True)
class PriceQuote:
    """An immutable, auditable price decision."""

    currency: str
    advertiser_cpm: Decimal
    effective_cpm: Decimal
    publisher_cpm: Decimal
    commission_rate: Decimal
    commission_amount_per_mille: Decimal
    multipliers: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def breakdown(self) -> dict:
        """Serialised onto ``ad_deliveries.price_breakdown`` for dispute resolution."""
        return {
            "currency": self.currency,
            "advertiser_cpm": str(self.advertiser_cpm),
            "effective_cpm": str(self.effective_cpm),
            "publisher_cpm": str(self.publisher_cpm),
            "commission_rate": str(self.commission_rate),
            "commission_per_mille": str(self.commission_amount_per_mille),
            "multipliers": self.multipliers,
            "notes": list(self.notes),
            "quoted_at": utcnow().isoformat(),
        }

    def cost_for(self, impressions: int) -> tuple[Decimal, Decimal, Decimal]:
        """(gross advertiser cost, publisher revenue, platform revenue).

        The publisher share is computed from the *same* gross that is charged, so
        the three figures always reconcile exactly.
        """
        from app.core.money import cpm_cost

        gross = cpm_cost(impressions, self.effective_cpm)
        publisher, platform = split_commission(gross, self.commission_rate)
        return gross, publisher, platform


class PricingService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = SettingsService(session)

    # -- rule lookup -------------------------------------------------------

    def _rule(
        self, scope: PricingRuleScope, value: str | None, currency: str, at: datetime
    ) -> PricingRule | None:
        if not value:
            return None
        rules = self.session.scalars(
            select(PricingRule)
            .where(
                PricingRule.scope == scope,
                PricingRule.scope_value == str(value).lower(),
                PricingRule.active.is_(True),
            )
            .order_by(PricingRule.priority.desc())
        ).all()
        for rule in rules:
            if rule.currency not in (None, currency):
                continue
            if rule.effective_from and rule.effective_from > at:
                continue
            if rule.effective_to and rule.effective_to <= at:
                continue
            return rule
        return None

    def multiplier(
        self, scope: PricingRuleScope, value: str | None, currency: str, at: datetime
    ) -> tuple[Decimal, PricingRule | None]:
        rule = self._rule(scope, value, currency, at)
        return (D(rule.multiplier) if rule else ONE), rule

    # -- quality -----------------------------------------------------------

    def quality_multiplier(self, channel: PublisherChannel) -> Decimal:
        """Map the internal quality score onto a configurable [floor, ceiling] band.

        Deliberately a smooth linear map rather than tiers, so a channel cannot
        gain a large payout jump by nudging one metric over a threshold.
        """
        floor = self.settings.decimal("quality_multiplier_floor")
        ceiling = self.settings.decimal("quality_multiplier_ceiling")
        if ceiling < floor:
            floor, ceiling = ceiling, floor
        score = D(channel.quality_score or 0)
        score = max(Decimal(0), min(ONE, score))
        with localcontext() as ctx:
            ctx.prec = 30
            return (floor + (ceiling - floor) * score).quantize(Decimal("0.0001"))

    # -- the quote ---------------------------------------------------------

    def quote(
        self,
        campaign: Campaign,
        channel: PublisherChannel,
        *,
        at: datetime | None = None,
        bid_override: Decimal | None = None,
    ) -> PriceQuote:
        at = at or utcnow()
        currency = campaign.currency
        bid = q(bid_override if bid_override is not None else campaign.bid_cpm)

        min_cpm = self.settings.money("min_cpm")
        max_cpm = self.settings.money("max_cpm")
        notes: list[str] = []
        if bid < min_cpm:
            raise ValidationFailed(f"bid {bid} is below the configured minimum CPM {min_cpm}")
        if bid > max_cpm:
            bid = max_cpm
            notes.append(f"bid clamped to max_cpm {max_cpm}")

        members = channel.chat.member_count if channel.chat else 0
        factors: list[tuple[str, Decimal, PricingRule | None]] = []
        for label, scope, value in (
            ("country", PricingRuleScope.COUNTRY, channel.country),
            ("category", PricingRuleScope.CATEGORY, channel.category),
            ("ad_format", PricingRuleScope.AD_FORMAT, _format_key(campaign.campaign_type)),
            ("channel_size", PricingRuleScope.CHANNEL_SIZE, size_band(members)),
            (
                "quality_tier",
                PricingRuleScope.QUALITY_TIER,
                quality_tier(D(channel.quality_score or 0)),
            ),
        ):
            mult, rule = self.multiplier(scope, value, currency, at)
            factors.append((label, mult, rule))

        quality_mult = self.quality_multiplier(channel)

        with localcontext() as ctx:
            ctx.prec = 40
            effective = D(bid)
            for _, mult, _ in factors:
                effective *= mult
            effective *= quality_mult
        effective = q(effective)

        # A rule may impose an explicit floor/ceiling on the resulting CPM.
        for label, _, rule in factors:
            if rule is None:
                continue
            if rule.min_cpm is not None and effective < q(rule.min_cpm):
                effective = q(rule.min_cpm)
                notes.append(f"{label} rule raised CPM to its minimum {effective}")
            if rule.max_cpm is not None and effective > q(rule.max_cpm):
                effective = q(rule.max_cpm)
                notes.append(f"{label} rule capped CPM at its maximum {effective}")

        # The publisher's own floor is respected by refusing to serve below it,
        # which the delivery engine checks — never by silently underpaying.
        if channel.min_cpm_floor is not None and effective < q(channel.min_cpm_floor):
            notes.append(f"below channel floor {q(channel.min_cpm_floor)}; channel is ineligible")

        commission_rate = self._commission_rate(factors)
        _, commission_per_mille = split_commission(effective, commission_rate)
        publisher_cpm = q(effective - commission_per_mille)

        return PriceQuote(
            currency=currency,
            advertiser_cpm=bid,
            effective_cpm=effective,
            publisher_cpm=publisher_cpm,
            commission_rate=commission_rate,
            commission_amount_per_mille=commission_per_mille,
            multipliers={
                **{label: str(mult) for label, mult, _ in factors},
                "quality": str(quality_mult),
            },
            notes=tuple(notes),
        )

    def _commission_rate(self, factors: list[tuple[str, Decimal, PricingRule | None]]) -> Decimal:
        """Most specific override wins; otherwise the global rate."""
        for label in ("category", "country"):
            for name, _, rule in factors:
                if name == label and rule is not None and rule.commission_rate_override is not None:
                    return D(rule.commission_rate_override)
        return self.settings.decimal("platform_commission_rate")

    # -- forecasting (clearly labelled as an estimate) ---------------------

    def forecast_impressions(self, campaign: Campaign) -> dict[str, object]:
        """Estimated reach for the campaign preview (spec §3).

        Returned under explicitly estimate-flavoured keys: these are projections
        from historical averages, never a promise of billable impressions
        (docs/TELEGRAM_CONSTRAINTS.md).
        """
        from app.core.money import impressions_for_budget

        budget = q(campaign.total_budget)
        nominal = impressions_for_budget(budget, campaign.bid_cpm)
        eligible = self.session.scalars(
            select(PublisherChannel).where(PublisherChannel.status.in_(["active", "verified"]))
        ).all()
        inventory = sum(c.avg_views for c in eligible)
        return {
            "basis": "historical_average_views",
            "is_estimate": True,
            "estimated_impressions_at_bid": nominal,
            "eligible_channels": len(eligible),
            "daily_inventory_estimate": inventory,
            "note": (
                "Estimated from publisher-reported average views. Billing uses only "
                "independently measured impressions, which are typically lower."
            ),
        }

    # -- admin helper ------------------------------------------------------

    def upsert_rule(
        self,
        scope: PricingRuleScope,
        scope_value: str,
        *,
        multiplier: object = "1.0",
        commission_rate_override: object | None = None,
        min_cpm: object | None = None,
        max_cpm: object | None = None,
        currency: str | None = None,
        note: str | None = None,
        staff_id=None,
    ) -> PricingRule:
        scope_value = scope_value.strip().lower()
        rule = self.session.scalars(
            select(PricingRule).where(
                PricingRule.scope == scope,
                PricingRule.scope_value == scope_value,
                PricingRule.currency == currency,
            )
        ).one_or_none()
        if rule is None:
            rule = PricingRule(scope=scope, scope_value=scope_value, currency=currency)
            self.session.add(rule)
        rule.multiplier = D(multiplier)
        rule.commission_rate_override = (
            D(commission_rate_override) if commission_rate_override is not None else None
        )
        rule.min_cpm = q(min_cpm) if min_cpm is not None else None
        rule.max_cpm = q(max_cpm) if max_cpm is not None else None
        rule.active = True
        rule.note = note
        rule.updated_by_staff_id = staff_id
        self.session.flush()
        return rule


def _format_key(campaign_type: CampaignType) -> str:
    return str(campaign_type.value if hasattr(campaign_type, "value") else campaign_type)
