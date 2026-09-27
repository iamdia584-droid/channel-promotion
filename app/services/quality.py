"""Channel quality score (spec §17).

A single number in [0, 1] blending several observable signals. Two rules shaped
this design:

* **Member count is not reach.** The dominant term is the views/member ratio, so
  a 100k-member channel with 3k average views scores far below a 50k-member
  channel with 25k views (spec §5).
* **The formula is internal.** Publishers see a coarse band, never the weights or
  the fraud inputs, because exposing them is a recipe for gaming (spec §17).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext
from statistics import fmean, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import D, pct
from app.db.base import utcnow
from app.models.telegram import ChannelStatDaily, PublisherChannel

ONE = Decimal(1)
ZERO = Decimal(0)

#: Signal weights. They sum to 1.
WEIGHTS: dict[str, Decimal] = {
    "view_ratio": Decimal("0.32"),
    "consistency": Decimal("0.18"),
    "engagement": Decimal("0.15"),
    "ad_performance": Decimal("0.15"),
    "longevity": Decimal("0.10"),
    "growth_health": Decimal("0.10"),
}

#: A views/member ratio at or above this scores full marks on that signal.
EXCELLENT_VIEW_RATIO = Decimal("0.40")
#: CTR at or above this scores full marks — higher is treated as suspicious, not better.
EXCELLENT_CTR = Decimal("3.0")


@dataclass(frozen=True)
class QualityBreakdown:
    score: Decimal
    signals: dict[str, str]
    band: str
    #: Fraud penalty already applied to ``score``.
    fraud_penalty: Decimal

    def public_view(self) -> dict[str, str]:
        """What a publisher may see: the band only, never the weights."""
        return {"quality_band": self.band}


class QualityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def compute(self, channel: PublisherChannel) -> QualityBreakdown:
        signals: dict[str, Decimal] = {}
        members = channel.chat.member_count if channel.chat else 0

        # 1. Views per member — the anti-inflation signal.
        if members > 0:
            ratio = D(channel.avg_views) / D(members)
            signals["view_ratio"] = _clamp(ratio / EXCELLENT_VIEW_RATIO)
        else:
            signals["view_ratio"] = ZERO

        history = self._history(channel, days=30)

        # 2. Consistency — steady view counts beat a spiky graph of the same mean.
        views = [row.avg_views for row in history if row.avg_views > 0]
        if len(views) >= 3:
            mean = fmean(views)
            spread = pstdev(views)
            cv = Decimal(str(spread / mean)) if mean else ONE
            signals["consistency"] = _clamp(ONE - cv)
        else:
            signals["consistency"] = Decimal("0.5")  # unknown, not bad

        # 3. Engagement — median vs mean views. A median close to the mean means
        #    steady genuine readership rather than a few viral outliers.
        if channel.avg_views > 0 and channel.median_views > 0:
            signals["engagement"] = _clamp(D(channel.median_views) / D(channel.avg_views))
        else:
            signals["engagement"] = Decimal("0.4")

        # 4. Ad performance — CTR, rewarded up to a plausible ceiling only.
        if channel.total_impressions >= 100:
            ctr = pct(channel.total_clicks, channel.total_impressions)
            signals["ad_performance"] = _clamp(ctr / EXCELLENT_CTR)
        else:
            signals["ad_performance"] = Decimal("0.4")  # insufficient data

        # 5. Longevity — full marks at 180 days of registration history.
        age_days = max(0, (utcnow() - channel.created_at).days) if channel.created_at else 0
        signals["longevity"] = _clamp(D(age_days) / Decimal(180))

        # 6. Growth health — organic growth scores well; a flat line or an
        #    implausible spike both score poorly.
        signals["growth_health"] = self._growth_health(history, members)

        with localcontext() as ctx:
            ctx.prec = 30
            raw = sum((WEIGHTS[k] * v for k, v in signals.items()), ZERO)

        penalty = _clamp(D(channel.fraud_score) / Decimal(100))
        score = _clamp(raw * (ONE - penalty)).quantize(Decimal("0.0001"))

        return QualityBreakdown(
            score=score,
            signals={k: str(v.quantize(Decimal("0.0001"))) for k, v in signals.items()},
            band=_band(score),
            fraud_penalty=penalty.quantize(Decimal("0.0001")),
        )

    def refresh(self, channel: PublisherChannel) -> QualityBreakdown:
        breakdown = self.compute(channel)
        channel.quality_score = breakdown.score
        self.session.flush()
        return breakdown

    def _history(self, channel: PublisherChannel, days: int) -> list[ChannelStatDaily]:
        since = (utcnow() - timedelta(days=days)).date()
        return list(
            self.session.scalars(
                select(ChannelStatDaily)
                .where(
                    ChannelStatDaily.channel_id == channel.id,
                    ChannelStatDaily.stat_date >= since,
                )
                .order_by(ChannelStatDaily.stat_date)
            ).all()
        )

    def _growth_health(self, history: list[ChannelStatDaily], members: int) -> Decimal:
        if len(history) < 5 or members <= 0:
            return Decimal("0.5")
        deltas = [row.member_delta for row in history]
        total_growth = sum(deltas)
        # Sudden single-day spikes suggest purchased members.
        worst_spike = max((abs(d) for d in deltas), default=0)
        spike_ratio = D(worst_spike) / D(members)
        spike_penalty = _clamp(spike_ratio / Decimal("0.05"))
        # Modest steady growth is healthiest; explosive growth is not rewarded.
        growth_rate = D(total_growth) / D(members)
        shape = _clamp(growth_rate / Decimal("0.10")) if growth_rate > 0 else Decimal("0.35")
        return _clamp(shape * (ONE - spike_penalty))


def _clamp(value: Decimal) -> Decimal:
    if value < ZERO:
        return ZERO
    return ONE if value > ONE else value


def _band(score: Decimal) -> str:
    if score < Decimal("0.25"):
        return "poor"
    if score < Decimal("0.50"):
        return "fair"
    if score < Decimal("0.75"):
        return "good"
    return "excellent"
