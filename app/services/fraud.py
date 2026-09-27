"""Anti-fraud engine (spec §16).

Each signal is an independent function returning a 0-100 score plus the evidence
behind it. The composite is a **weighted max-blend**, not a sum: the strongest
signal dominates and the others can only lift it part of the way. That matters
because a sum lets three weak, correlated signals manufacture a high-risk verdict,
while a bare max ignores corroboration entirely.

Nothing here bans anyone. A score flags, throttles, holds earnings or opens a case
with the full evidence for a human (spec §16: never act on one signal alone).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import D, pct, q
from app.db.base import utcnow
from app.models.campaigns import Campaign
from app.models.delivery import AdDelivery, Click, Impression
from app.models.enums import (
    FraudBand,
    FraudCaseStatus,
    FraudSubject,
    ValidationStatus,
)
from app.models.identity import Publisher, User
from app.models.money import Withdrawal
from app.models.ops import FraudCase, FraudEvent, FraudScore
from app.models.telegram import ChannelStatDaily, PublisherChannel
from app.services.settings_service import SettingsService


@dataclass
class Signal:
    name: str
    score: int
    evidence: dict = field(default_factory=dict)
    weight: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        self.score = max(0, min(100, int(self.score)))


@dataclass
class Assessment:
    score: int
    band: FraudBand
    signals: list[Signal]

    @property
    def evidence(self) -> dict:
        return {
            "composite_score": self.score,
            "band": self.band.value,
            "signals": [
                {"name": s.name, "score": s.score, "weight": str(s.weight),
                 "evidence": s.evidence}
                for s in self.signals
                if s.score > 0
            ],
        }

    def triggered(self) -> list[str]:
        return [s.name for s in self.signals if s.score > 0]


def blend(signals: list[Signal]) -> int:
    """Weighted max-blend.

    The top signal sets the floor; every further signal adds a diminishing share
    of the remaining headroom. One noisy signal therefore cannot reach 100 alone,
    and genuine corroboration still escalates.
    """
    active = sorted(
        (s for s in signals if s.score > 0), key=lambda s: s.score * s.weight, reverse=True
    )
    if not active:
        return 0
    top = active[0]
    score = D(top.score) * top.weight
    for signal in active[1:]:
        headroom = Decimal(100) - score
        score += headroom * (D(signal.score) / Decimal(100)) * signal.weight * Decimal("0.35")
    return int(max(0, min(100, int(score.to_integral_value()))))


class FraudService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = SettingsService(session)

    # ------------------------------------------------------------------
    # Impression / click scoring (the hot path)
    # ------------------------------------------------------------------

    def score_impression_event(
        self,
        delivery: AdDelivery,
        *,
        telegram_user_id: int | None = None,
        ip_hash: str | None = None,
        user_agent_hash: str | None = None,
        occurred_at=None,
    ) -> Assessment:
        occurred_at = occurred_at or utcnow()
        signals: list[Signal] = [
            self._self_generated(delivery, telegram_user_id),
            self._ip_concentration(delivery, ip_hash),
            self._velocity(delivery, occurred_at),
            self._missing_user_agent(user_agent_hash),
            self._repeat_identity(delivery, telegram_user_id, occurred_at),
            self._channel_history(delivery),
        ]
        score = blend(signals)
        return Assessment(score, FraudBand.of(score), signals)

    def _self_generated(
        self, delivery: AdDelivery, telegram_user_id: int | None
    ) -> Signal:
        """The publisher (or the advertiser) clicking their own ad."""
        if telegram_user_id is None:
            return Signal("self_generated", 0)
        publisher = self.session.get(Publisher, delivery.publisher_id)
        campaign = self.session.get(Campaign, delivery.campaign_id)
        owners: dict[str, int] = {}
        if publisher is not None:
            user = self.session.get(User, publisher.user_id)
            if user is not None:
                owners["publisher"] = user.telegram_user_id
        if campaign is not None:
            from app.models.identity import Advertiser

            advertiser = self.session.get(Advertiser, campaign.advertiser_id)
            if advertiser is not None:
                user = self.session.get(User, advertiser.user_id)
                if user is not None:
                    owners["advertiser"] = user.telegram_user_id
        for role, owner_id in owners.items():
            if owner_id == telegram_user_id:
                return Signal(
                    "self_generated", 95,
                    {"role": role, "telegram_user_id": telegram_user_id},
                    weight=Decimal("1.0"),
                )
        return Signal("self_generated", 0)

    def _ip_concentration(self, delivery: AdDelivery, ip_hash: str | None) -> Signal:
        """Many 'distinct users' behind one address is a click farm signature."""
        if not ip_hash:
            return Signal("ip_concentration", 0)
        distinct_users = int(
            self.session.scalar(
                select(func.count(func.distinct(Impression.telegram_user_id))).where(
                    Impression.delivery_id == delivery.id,
                    Impression.ip_hash == ip_hash,
                )
            )
            or 0
        )
        if distinct_users >= 20:
            score = 90
        elif distinct_users >= 10:
            score = 70
        elif distinct_users >= 5:
            score = 45
        elif distinct_users >= 3:
            score = 25
        else:
            score = 0
        return Signal(
            "ip_concentration", score,
            {"distinct_users_on_ip": distinct_users} if score else {},
            weight=Decimal("0.9"),
        )

    def _velocity(self, delivery: AdDelivery, occurred_at) -> Signal:
        """More impressions per minute than the channel could plausibly produce."""
        window_start = occurred_at - timedelta(minutes=1)
        recent = int(
            self.session.scalar(
                select(func.coalesce(func.sum(Impression.quantity), 0)).where(
                    Impression.delivery_id == delivery.id,
                    Impression.occurred_at >= window_start,
                    Impression.occurred_at <= occurred_at,
                )
            )
            or 0
        )
        channel = self.session.get(PublisherChannel, delivery.channel_id)
        # A generous ceiling: a tenth of the channel's whole daily reach inside one
        # minute is already implausible.
        plausible = max(20, int((channel.avg_views if channel else 0) * 0.1))
        if recent <= plausible:
            return Signal("impossible_velocity", 0)
        ratio = recent / plausible
        score = min(95, int(40 + ratio * 10))
        return Signal(
            "impossible_velocity", score,
            {"impressions_in_last_minute": recent, "plausible_ceiling": plausible},
            weight=Decimal("0.95"),
        )

    def _missing_user_agent(self, user_agent_hash: str | None) -> Signal:
        """A tracking-link hit with no user agent is usually a bot or a scanner."""
        if user_agent_hash:
            return Signal("missing_user_agent", 0)
        return Signal("missing_user_agent", 30, {"user_agent": "absent"},
                      weight=Decimal("0.5"))

    def _repeat_identity(
        self, delivery: AdDelivery, telegram_user_id: int | None, occurred_at
    ) -> Signal:
        """The same identity hitting the same ad repeatedly (refresh abuse)."""
        if telegram_user_id is None:
            return Signal("repeat_identity", 0)
        window = self.settings.int_("click_dedupe_window_minutes")
        since = occurred_at - timedelta(minutes=window)
        repeats = int(
            self.session.scalar(
                select(func.count(Impression.id)).where(
                    Impression.delivery_id == delivery.id,
                    Impression.telegram_user_id == telegram_user_id,
                    Impression.occurred_at >= since,
                )
            )
            or 0
        )
        if repeats <= 1:
            return Signal("repeat_identity", 0)
        return Signal(
            "repeat_identity", min(80, 20 * repeats),
            {"hits_in_window": repeats, "window_minutes": window},
            weight=Decimal("0.7"),
        )

    def _channel_history(self, delivery: AdDelivery) -> Signal:
        """A channel with prior confirmed fraud gets a standing penalty."""
        channel = self.session.get(PublisherChannel, delivery.channel_id)
        if channel is None or channel.fraud_score <= 30:
            return Signal("channel_history", 0)
        return Signal(
            "channel_history", channel.fraud_score,
            {"channel_fraud_score": channel.fraud_score}, weight=Decimal("0.6"),
        )

    # ------------------------------------------------------------------
    # Channel-level audit (runs periodically, not per impression)
    # ------------------------------------------------------------------

    def audit_channel(self, channel: PublisherChannel) -> Assessment:
        signals = [
            self._member_inflation(channel),
            self._view_spike(channel),
            self._implausible_ctr(channel),
            self._growth_spike(channel),
            self._invalid_impression_ratio(channel),
        ]
        score = blend(signals)
        assessment = Assessment(score, FraudBand.of(score), signals)
        channel.fraud_score = score
        self._upsert_score(FraudSubject.CHANNEL, channel.id, assessment)
        self.session.flush()
        return assessment

    def _member_inflation(self, channel: PublisherChannel) -> Signal:
        """Spec §16: fake members. Views far below members is the tell."""
        members = channel.chat.member_count if channel.chat else 0
        if members < 1_000 or channel.avg_views <= 0:
            return Signal("member_inflation", 0)
        ratio = D(channel.avg_views) / D(members)
        floor = self.settings.decimal("fraud_min_view_member_ratio")
        if ratio >= floor:
            return Signal("member_inflation", 0)
        shortfall = (floor - ratio) / floor  # 0 → 1 as views approach zero
        return Signal(
            "member_inflation", int(min(Decimal(85), shortfall * Decimal(85))),
            {
                "members": members,
                "avg_views": channel.avg_views,
                "view_member_ratio": str(ratio.quantize(Decimal("0.0001"))),
                "configured_floor": str(floor),
            },
            weight=Decimal("0.85"),
        )

    def _view_spike(self, channel: PublisherChannel) -> Signal:
        """Spec §16: abnormally fast view growth / sudden traffic spikes."""
        history = self._history(channel, 14)
        if len(history) < 4:
            return Signal("view_spike", 0)
        views = [r.avg_views for r in history if r.avg_views > 0]
        if len(views) < 4:
            return Signal("view_spike", 0)
        latest, trailing = views[-1], views[:-1]
        baseline = sum(trailing) / len(trailing)
        if baseline <= 0:
            return Signal("view_spike", 0)
        ratio = D(latest) / D(str(baseline))
        limit = self.settings.decimal("fraud_max_view_growth_ratio")
        if ratio <= limit:
            return Signal("view_spike", 0)
        return Signal(
            "view_spike", int(min(Decimal(90), Decimal(40) + (ratio - limit) * Decimal(15))),
            {
                "latest_avg_views": latest,
                "trailing_baseline": int(baseline),
                "ratio": str(ratio.quantize(Decimal("0.01"))),
                "configured_limit": str(limit),
            },
            weight=Decimal("0.9"),
        )

    def _implausible_ctr(self, channel: PublisherChannel) -> Signal:
        """A CTR no genuine display placement reaches means synthetic clicks."""
        if channel.total_impressions < 500:
            return Signal("implausible_ctr", 0)
        ctr = pct(channel.total_clicks, channel.total_impressions) / Decimal(100)
        limit = self.settings.decimal("fraud_max_ctr")
        if ctr <= limit:
            return Signal("implausible_ctr", 0)
        return Signal(
            "implausible_ctr", int(min(Decimal(88), Decimal(45) + (ctr - limit) * Decimal(150))),
            {"ctr": str(ctr.quantize(Decimal("0.0001"))), "configured_limit": str(limit)},
            weight=Decimal("0.85"),
        )

    def _growth_spike(self, channel: PublisherChannel) -> Signal:
        history = self._history(channel, 14)
        members = channel.chat.member_count if channel.chat else 0
        if len(history) < 4 or members <= 0:
            return Signal("member_growth_spike", 0)
        worst = max((abs(r.member_delta) for r in history), default=0)
        share = D(worst) / D(members)
        if share < Decimal("0.05"):
            return Signal("member_growth_spike", 0)
        return Signal(
            "member_growth_spike", int(min(Decimal(75), share * Decimal(600))),
            {"largest_daily_member_change": worst, "members": members},
            weight=Decimal("0.7"),
        )

    def _invalid_impression_ratio(self, channel: PublisherChannel) -> Signal:
        totals = self.session.execute(
            select(
                func.coalesce(func.sum(AdDelivery.billable_impressions), 0),
                func.coalesce(func.sum(AdDelivery.invalid_impressions), 0),
            ).where(AdDelivery.channel_id == channel.id)
        ).one()
        billable, invalid = int(totals[0] or 0), int(totals[1] or 0)
        total = billable + invalid
        if total < 200:
            return Signal("invalid_impression_ratio", 0)
        ratio = D(invalid) / D(total)
        if ratio < Decimal("0.3"):
            return Signal("invalid_impression_ratio", 0)
        return Signal(
            "invalid_impression_ratio", int(min(Decimal(80), ratio * Decimal(100))),
            {"invalid": invalid, "billable": billable,
             "ratio": str(ratio.quantize(Decimal("0.0001")))},
            weight=Decimal("0.75"),
        )

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

    # ------------------------------------------------------------------
    # Withdrawal screening (spec §16)
    # ------------------------------------------------------------------

    def score_withdrawal(self, withdrawal: Withdrawal) -> Assessment:
        publisher = self.session.get(Publisher, withdrawal.publisher_id)
        signals: list[Signal] = []

        # Cashing out almost everything immediately after earning it.
        recent_earnings = q(
            self.session.scalar(
                select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                    Withdrawal.publisher_id == withdrawal.publisher_id,
                    Withdrawal.created_at >= utcnow() - timedelta(hours=24),
                )
            )
            or 0
        )
        daily_cap = self.settings.money("max_withdrawal_per_day")
        if daily_cap > 0 and recent_earnings > daily_cap * Decimal("0.8"):
            signals.append(
                Signal("rapid_cashout", 55,
                       {"withdrawn_last_24h": str(recent_earnings),
                        "daily_cap": str(daily_cap)}, weight=Decimal("0.7"))
            )

        # A brand-new account withdrawing is worth a look.
        if publisher is not None and publisher.created_at:
            age_days = (utcnow() - publisher.created_at).days
            if age_days < 7:
                signals.append(
                    Signal("new_account_withdrawal", 45,
                           {"account_age_days": age_days}, weight=Decimal("0.6"))
                )

        if publisher is not None and publisher.fraud_strikes > 0:
            signals.append(
                Signal("prior_fraud_strikes", min(85, 30 * publisher.fraud_strikes),
                       {"strikes": publisher.fraud_strikes}, weight=Decimal("0.9"))
            )

        # The same payout destination used by several publishers.
        from app.models.money import PayoutMethodRecord

        if withdrawal.payout_method_id:
            method = self.session.get(PayoutMethodRecord, withdrawal.payout_method_id)
            if method is not None:
                shared = int(
                    self.session.scalar(
                        select(
                            func.count(func.distinct(PayoutMethodRecord.publisher_id))
                        ).where(
                            PayoutMethodRecord.destination_fingerprint
                            == method.destination_fingerprint
                        )
                    )
                    or 0
                )
                if shared > 1:
                    signals.append(
                        Signal("shared_payout_destination", min(90, 35 * shared),
                               {"publishers_sharing_destination": shared},
                               weight=Decimal("0.9"))
                    )

        # Channels behind this publisher currently scoring badly.
        worst = int(
            self.session.scalar(
                select(func.coalesce(func.max(PublisherChannel.fraud_score), 0)).where(
                    PublisherChannel.publisher_id == withdrawal.publisher_id
                )
            )
            or 0
        )
        if worst > 30:
            signals.append(
                Signal("channel_fraud_history", worst,
                       {"worst_channel_fraud_score": worst}, weight=Decimal("0.8"))
            )

        if not signals:
            signals.append(Signal("clean", 0))
        score = blend(signals)
        assessment = Assessment(score, FraudBand.of(score), signals)
        withdrawal.fraud_score = score
        withdrawal.fraud_hold = score >= self.settings.int_("fraud_hold_earnings_threshold")
        self._upsert_score(FraudSubject.WITHDRAWAL, withdrawal.id, assessment)
        self.session.flush()
        return assessment

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_event(
        self,
        subject_type: FraudSubject,
        subject_id: uuid.UUID | None,
        assessment: Assessment,
        *,
        publisher_id: uuid.UUID | None = None,
        advertiser_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        channel_id: uuid.UUID | None = None,
        delivery_id: uuid.UUID | None = None,
        amount_at_risk: object = "0",
        action_taken: str | None = None,
    ) -> FraudEvent | None:
        """Persist an event once it is worth a human's attention."""
        if assessment.score < self.settings.int_("fraud_review_threshold"):
            return None
        event = FraudEvent(
            subject_type=subject_type,
            subject_id=subject_id,
            publisher_id=publisher_id,
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
            channel_id=channel_id,
            delivery_id=delivery_id,
            signal=",".join(assessment.triggered())[:64] or "composite",
            score=assessment.score,
            band=assessment.band,
            evidence=assessment.evidence,
            action_taken=action_taken,
            amount_at_risk=q(amount_at_risk),
            occurred_at=utcnow(),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _upsert_score(
        self, subject_type: FraudSubject, subject_id: uuid.UUID, assessment: Assessment
    ) -> FraudScore:
        row = self.session.scalars(
            select(FraudScore).where(
                FraudScore.subject_type == subject_type,
                FraudScore.subject_id == subject_id,
            )
        ).one_or_none()
        if row is None:
            row = FraudScore(subject_type=subject_type, subject_id=subject_id)
            self.session.add(row)
        row.score = assessment.score
        row.band = assessment.band
        row.signals = assessment.evidence
        row.events_counted = (row.events_counted or 0) + 1
        row.last_event_at = utcnow()
        self.session.flush()
        return row

    def open_case(
        self,
        subject_type: FraudSubject,
        subject_id: uuid.UUID,
        assessment: Assessment,
        summary: str,
        amount_held: object = "0",
    ) -> FraudCase:
        """Open (or refresh) an investigation. Deduplicated per open subject."""
        case = self.session.scalars(
            select(FraudCase).where(
                FraudCase.subject_type == subject_type,
                FraudCase.subject_id == subject_id,
                FraudCase.status.in_([FraudCaseStatus.OPEN, FraudCaseStatus.INVESTIGATING]),
            )
        ).one_or_none()
        if case is None:
            case = FraudCase(subject_type=subject_type, subject_id=subject_id)
            self.session.add(case)
        case.score = assessment.score
        case.band = assessment.band
        case.summary = summary[:500]
        case.evidence = assessment.evidence
        case.amount_held = q(amount_held)
        self.session.flush()
        return case

    # ------------------------------------------------------------------
    # Sweep: find deliveries whose measured traffic looks wrong
    # ------------------------------------------------------------------

    def sweep_deliveries(self, limit: int = 200) -> list[FraudEvent]:
        """Re-examine recent deliveries and flag the suspicious ones."""
        cutoff = utcnow() - timedelta(days=2)
        rows = self.session.scalars(
            select(AdDelivery)
            .where(AdDelivery.sent_at.is_not(None), AdDelivery.sent_at >= cutoff)
            .order_by(AdDelivery.sent_at.desc())
            .limit(limit)
        ).all()
        events: list[FraudEvent] = []
        for delivery in rows:
            assessment = self.audit_delivery(delivery)
            delivery.fraud_score = assessment.score
            event = self.record_event(
                FraudSubject.DELIVERY, delivery.id, assessment,
                publisher_id=delivery.publisher_id, campaign_id=delivery.campaign_id,
                channel_id=delivery.channel_id, delivery_id=delivery.id,
                amount_at_risk=delivery.settled_amount or delivery.reserved_amount,
            )
            if event is not None:
                events.append(event)
        self.session.flush()
        return events

    def audit_delivery(self, delivery: AdDelivery) -> Assessment:
        signals: list[Signal] = []

        # Impressions concentrated on very few addresses.
        rows = self.session.execute(
            select(Impression.ip_hash, func.count(Impression.id))
            .where(Impression.delivery_id == delivery.id, Impression.ip_hash.is_not(None))
            .group_by(Impression.ip_hash)
            .order_by(func.count(Impression.id).desc())
            .limit(5)
        ).all()
        total = int(
            self.session.scalar(
                select(func.count(Impression.id)).where(
                    Impression.delivery_id == delivery.id
                )
            )
            or 0
        )
        if total >= 50 and rows:
            top_share = D(int(rows[0][1])) / D(total)
            if top_share > Decimal("0.25"):
                signals.append(
                    Signal("ip_concentration", int(min(Decimal(90), top_share * Decimal(140))),
                           {"top_ip_share": str(top_share.quantize(Decimal("0.0001"))),
                            "total_impressions": total}, weight=Decimal("0.9"))
                )

        # Duplicate/capped/fraudulent ratio.
        invalid = int(
            self.session.scalar(
                select(func.count(Impression.id)).where(
                    Impression.delivery_id == delivery.id,
                    Impression.validation_status.in_(
                        [ValidationStatus.FRAUDULENT, ValidationStatus.DUPLICATE]
                    ),
                )
            )
            or 0
        )
        if total >= 50 and invalid / total > 0.3:
            signals.append(
                Signal("invalid_traffic_ratio", int(min(85, (invalid / total) * 100)),
                       {"invalid": invalid, "total": total}, weight=Decimal("0.8"))
            )

        # CTR far above plausibility for this delivery.
        if delivery.billable_impressions >= 200:
            ctr = pct(delivery.clicks, delivery.billable_impressions) / Decimal(100)
            limit = self.settings.decimal("fraud_max_ctr")
            if ctr > limit:
                signals.append(
                    Signal("implausible_ctr", int(min(Decimal(85), Decimal(45) +
                                                      (ctr - limit) * Decimal(150))),
                           {"ctr": str(ctr.quantize(Decimal("0.0001")))},
                           weight=Decimal("0.85"))
                )

        invalid_clicks = int(
            self.session.scalar(
                select(func.count(Click.id)).where(
                    Click.delivery_id == delivery.id, Click.valid.is_(False)
                )
            )
            or 0
        )
        if invalid_clicks >= 5:
            signals.append(
                Signal("invalid_clicks", min(70, 10 * invalid_clicks),
                       {"invalid_clicks": invalid_clicks}, weight=Decimal("0.6"))
            )

        if not signals:
            signals.append(Signal("clean", 0))
        score = blend(signals)
        return Assessment(score, FraudBand.of(score), signals)
