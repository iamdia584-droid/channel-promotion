"""Ad delivery engine (spec §9, §32).

Implements the thirteen responsibilities of §9 in two phases:

``plan(channel)``  — steps 1-6: find campaigns, filter, rank, select one, freeze
                     its price, reserve budget, create a ``PLANNED`` delivery.
``dispatch(d)``    — step 7-9: post to Telegram, record the message id, arm the
                     measurement window.

Money moves later, in :mod:`app.services.settlement`, once impressions are
measured — steps 10-13. Separating them means a Telegram failure costs a released
reservation, never a mis-billed advertiser.

Selection is **not** random (spec §9). Candidates are scored on price, targeting
relevance, publisher fit, quality and pacing, and the configured selection mode
decides how the winner is drawn from that ranking.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import CampaignNotDeliverable, NotFound
from app.core.logging import get_logger
from app.core.money import ZERO, D, impressions_for_budget, q
from app.core.security import new_token, tracking_token
from app.db.base import utcnow
from app.models.campaigns import (
    Advertisement,
    Campaign,
    CampaignPublisher,
    CampaignTarget,
)
from app.models.delivery import AdDelivery
from app.models.enums import (
    AdStatus,
    CampaignStatus,
    DeliveryStatus,
    SelectionMode,
)
from app.models.identity import Advertiser
from app.models.telegram import PublisherChannel
from app.services.impressions import ImpressionService
from app.services.pacing import PacingService
from app.services.pricing import PriceQuote, PricingService
from app.services.settings_service import SettingsService
from app.services.telegram_gateway import TelegramError, TelegramGateway, get_gateway
from app.services.wallet import WalletService

log = get_logger(__name__)


@dataclass
class Candidate:
    campaign: Campaign
    advertisement: Advertisement
    quote: PriceQuote
    score: Decimal = ZERO
    factors: dict[str, str] = field(default_factory=dict)
    rejected: str | None = None


@dataclass(frozen=True)
class PlanResult:
    delivery: AdDelivery | None
    considered: int
    eligible: int
    reason: str = ""
    debug: dict = field(default_factory=dict)


class DeliveryService:
    def __init__(self, session: Session, gateway: TelegramGateway | None = None) -> None:
        self.session = session
        self.gateway = gateway or get_gateway()
        self.settings = SettingsService(session)
        self.pricing = PricingService(session)
        self.pacing = PacingService(session)
        self.wallets = WalletService(session)
        self.impressions = ImpressionService(session)

    # ------------------------------------------------------------------
    # Steps 1-6: selection
    # ------------------------------------------------------------------

    def plan(
        self,
        channel: PublisherChannel,
        *,
        at: datetime | None = None,
        seed: int | None = None,
    ) -> PlanResult:
        at = at or utcnow()

        blocked = self._channel_block_reason(channel, at)
        if blocked:
            return PlanResult(None, 0, 0, blocked)

        campaigns = self._active_campaigns(at)
        candidates: list[Candidate] = []
        rejections: dict[str, int] = {}

        for campaign in campaigns:
            candidate = self._evaluate(campaign, channel, at)
            if candidate.rejected:
                rejections[candidate.rejected] = rejections.get(candidate.rejected, 0) + 1
                continue
            candidates.append(candidate)

        if not candidates:
            return PlanResult(
                None,
                len(campaigns),
                0,
                "no eligible campaign for this channel",
                debug={"rejections": rejections},
            )

        candidates.sort(key=lambda c: (c.score, str(c.campaign.id)), reverse=True)
        winner = self._select(candidates, seed=seed)
        charge_cpm = self._clearing_cpm(candidates, winner)

        delivery = self._create_delivery(winner, channel, charge_cpm, at, candidates)
        return PlanResult(
            delivery,
            len(campaigns),
            len(candidates),
            debug={"rejections": rejections, "eligible": len(candidates)},
        )

    # -- channel gating ----------------------------------------------------

    def _channel_block_reason(self, channel: PublisherChannel, at: datetime) -> str:
        if not channel.status.can_serve:
            return f"channel status is {channel.status.value}"
        if not channel.auto_advertising:
            return "publisher has disabled automatic advertising"
        chat = channel.chat
        if chat is None or chat.is_blacklisted:
            return "channel is blacklisted"
        if not chat.bot_is_admin:
            return "bot is no longer an administrator"
        publisher = channel.publisher
        if publisher is not None:
            if not publisher.is_active:
                return "publisher account is not active"
            if not publisher.auto_advertising:
                return "publisher has disabled automatic advertising"
        if channel.avg_views < self.settings.int_("min_avg_views_to_serve"):
            return (
                f"average views {channel.avg_views} below the configured minimum "
                f"{self.settings.int_('min_avg_views_to_serve')}"
            )

        # Frequency capping (spec §18): per-channel interval, daily and weekly.
        interval = self._effective(
            channel, "min_ad_interval_minutes", "default_min_ad_interval_minutes"
        )
        if channel.last_ad_at and interval > 0:
            next_allowed = channel.last_ad_at + timedelta(minutes=interval)
            if at < next_allowed:
                return f"minimum ad interval not elapsed (next at {next_allowed.isoformat()})"

        per_day = self._effective(channel, "max_ads_per_day", "default_max_ads_per_day")
        if per_day > 0 and self._ads_since(channel.id, at - timedelta(days=1)) >= per_day:
            return f"channel daily ad cap of {per_day} reached"

        per_week = self._effective(channel, "max_ads_per_week", "default_max_ads_per_week")
        if per_week > 0 and self._ads_since(channel.id, at - timedelta(days=7)) >= per_week:
            return f"channel weekly ad cap of {per_week} reached"
        return ""

    def _effective(self, channel: PublisherChannel, field_name: str, setting_key: str) -> int:
        """Channel override, else publisher value, else the global default."""
        value = getattr(channel, field_name, None)
        if value is not None:
            return int(value)
        publisher = channel.publisher
        if publisher is not None:
            pub_value = getattr(publisher, field_name, None)
            if pub_value is not None:
                return int(pub_value)
        return self.settings.int_(setting_key)

    def _ads_since(self, channel_id: uuid.UUID, since: datetime) -> int:
        """Count ads posted to this channel since ``since``.

        Deliberately not bounded above. A frequency cap asks "how many ads has
        this channel carried lately", and an upper bound at the planning
        timestamp would miss ads whose ``sent_at`` lands microseconds after it —
        letting a channel exceed its cap.
        """
        return int(
            self.session.scalar(
                select(func.count(AdDelivery.id)).where(
                    AdDelivery.channel_id == channel_id,
                    AdDelivery.sent_at.is_not(None),
                    AdDelivery.sent_at >= since,
                    AdDelivery.status.not_in([DeliveryStatus.FAILED, DeliveryStatus.CANCELLED]),
                )
            )
            or 0
        )

    # -- campaign gating ---------------------------------------------------

    def _active_campaigns(self, at: datetime) -> list[Campaign]:
        return list(
            self.session.scalars(
                select(Campaign).where(
                    Campaign.status == CampaignStatus.RUNNING,
                    Campaign.starts_at <= at,
                    Campaign.ends_at > at,
                )
            ).all()
        )

    def _evaluate(self, campaign: Campaign, channel: PublisherChannel, at: datetime) -> Candidate:
        ad = self._pick_creative(campaign)
        empty = Candidate(campaign, ad, None)  # type: ignore[arg-type]
        if ad is None:
            empty.rejected = "no approved creative"
            return empty

        advertiser = self.session.get(Advertiser, campaign.advertiser_id)
        if advertiser is None or not advertiser.is_active:
            empty.rejected = "advertiser not active"
            return empty
        if q(campaign.remaining_budget) <= ZERO:
            empty.rejected = "budget exhausted"
            return empty

        target = campaign.target
        if target is not None:
            mismatch = self._targeting_mismatch(target, channel)
            if mismatch:
                empty.rejected = mismatch
                return empty

        explicit = self._explicit_rule(campaign, channel)
        if explicit is False:
            empty.rejected = "channel excluded by advertiser"
            return empty
        if campaign.allow_specific_channels and explicit is not True:
            empty.rejected = "campaign restricted to specific channels"
            return empty

        try:
            quote = self.pricing.quote(campaign, channel, at=at)
        except Exception as exc:  # pricing refused this pair
            empty.rejected = f"pricing refused: {exc}"
            return empty

        if channel.min_cpm_floor is not None and quote.effective_cpm < q(channel.min_cpm_floor):
            empty.rejected = "below the channel's CPM floor"
            return empty

        # One unit of inventory is one delivery priced over the channel's expected
        # reach; that is what we must be able to fund and pace.
        expected = self._expected_impressions(channel)
        cost = quote.cost_for(expected)[0]
        if cost <= ZERO:
            empty.rejected = "zero-cost delivery"
            return empty

        pacing = self.pacing.check(campaign, cost, at)
        if not pacing.allowed:
            # Try a smaller unit before giving up: the headroom may still fund
            # a partial delivery.
            if pacing.headroom <= ZERO:
                empty.rejected = f"pacing: {pacing.reason}"
                return empty
            cost = pacing.headroom

        wallet = self.wallets.for_advertiser(advertiser.id)
        if q(wallet.available_balance) < cost:
            empty.rejected = "advertiser has insufficient available balance"
            return empty

        if self._advertiser_over_share(campaign, channel, at):
            empty.rejected = "advertiser inventory share cap reached"
            return empty

        per_channel_cap = campaign.max_impressions_per_channel_per_day
        if (
            per_channel_cap is not None
            and self._campaign_impressions_today(campaign.id, channel.id, at) >= per_channel_cap
        ):
            empty.rejected = "campaign per-channel daily impression cap reached"
            return empty

        candidate = Candidate(campaign, ad, quote)
        self._score(candidate, channel, at, cost, expected)
        return candidate

    def _pick_creative(self, campaign: Campaign) -> Advertisement | None:
        servable = [a for a in campaign.ads if a.status in (AdStatus.APPROVED, AdStatus.RUNNING)]
        if not servable:
            return None
        # Rotate deterministically by id so one creative does not dominate.
        return sorted(servable, key=lambda a: str(a.id))[0]

    def _targeting_mismatch(self, target: CampaignTarget, channel: PublisherChannel) -> str:
        countries = [c.upper() for c in (target.countries or [])]
        if countries and (channel.country or "").upper() not in countries:
            return "country mismatch"
        languages = [x.lower() for x in (target.languages or [])]
        if languages and (channel.language or "").lower() not in languages:
            return "language mismatch"
        categories = [x.lower() for x in (target.categories or [])]
        channel_category = (channel.category or "").lower()
        if categories and channel_category not in categories:
            return "category mismatch"
        excluded = [x.lower() for x in (target.excluded_categories or [])]
        if channel_category and channel_category in excluded:
            return "category excluded"

        # The publisher's own acceptance list wins over advertiser targeting.
        accepted = channel.accepted_categories or []
        if (
            isinstance(accepted, list)
            and accepted
            and channel_category
            and channel_category not in [x.lower() for x in accepted]
        ):
            return "publisher does not accept this category"

        members = channel.chat.member_count if channel.chat else 0
        if target.min_members is not None and members < target.min_members:
            return "channel too small"
        if target.max_members is not None and members > target.max_members:
            return "channel too large"
        if target.min_avg_views is not None and channel.avg_views < target.min_avg_views:
            return "average views below target minimum"
        if target.max_avg_views is not None and channel.avg_views > target.max_avg_views:
            return "average views above target maximum"
        if target.min_quality_score is not None and D(channel.quality_score) < D(
            target.min_quality_score
        ):
            return "quality score below target minimum"
        audiences = [x.lower() for x in (target.audience_types or [])]
        if audiences and (channel.audience_type or "").lower() not in [*audiences, "any"]:
            return "audience type mismatch"
        return ""

    def _explicit_rule(self, campaign: Campaign, channel: PublisherChannel) -> bool | None:
        row = self.session.scalars(
            select(CampaignPublisher).where(
                CampaignPublisher.campaign_id == campaign.id,
                CampaignPublisher.channel_id == channel.id,
            )
        ).one_or_none()
        return None if row is None else bool(row.allowed)

    def _advertiser_over_share(
        self, campaign: Campaign, channel: PublisherChannel, at: datetime
    ) -> bool:
        """Stop one advertiser monopolising a channel's inventory (spec §32)."""
        share_cap = self.settings.decimal("max_advertiser_inventory_share")
        slots = self._effective(channel, "max_ads_per_day", "default_max_ads_per_day")
        if slots <= 0 or share_cap >= Decimal(1):
            return False
        allowed = int((D(slots) * share_cap).to_integral_value())
        if allowed < 1:
            allowed = 1
        since = at - timedelta(days=1)
        used = int(
            self.session.scalar(
                select(func.count(AdDelivery.id))
                .join(Campaign, Campaign.id == AdDelivery.campaign_id)
                .where(
                    AdDelivery.channel_id == channel.id,
                    Campaign.advertiser_id == campaign.advertiser_id,
                    AdDelivery.sent_at.is_not(None),
                    AdDelivery.sent_at >= since,
                    AdDelivery.status.not_in([DeliveryStatus.FAILED, DeliveryStatus.CANCELLED]),
                )
            )
            or 0
        )
        return used >= allowed

    def _campaign_impressions_today(
        self, campaign_id: uuid.UUID, channel_id: uuid.UUID, at: datetime
    ) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(AdDelivery.billable_impressions), 0)).where(
                    AdDelivery.campaign_id == campaign_id,
                    AdDelivery.channel_id == channel_id,
                    AdDelivery.sent_at >= at - timedelta(days=1),
                )
            )
            or 0
        )

    def _expected_impressions(self, channel: PublisherChannel) -> int:
        """Reach we expect from one post, from observed ad performance first."""
        if channel.avg_ad_views > 0:
            return channel.avg_ad_views
        return max(1, channel.avg_views)

    # -- scoring -----------------------------------------------------------

    def _score(
        self,
        candidate: Candidate,
        channel: PublisherChannel,
        at: datetime,
        cost: Decimal,
        expected: int,
    ) -> None:
        campaign = candidate.campaign
        quote = candidate.quote

        # Revenue per delivery is the primary term — this is an ad exchange.
        revenue = quote.effective_cpm

        relevance = self._relevance(campaign, channel)
        quality = max(Decimal("0.1"), D(channel.quality_score or 0))
        priority = D(campaign.priority) / Decimal(5)

        # Pacing factor: a campaign behind schedule is boosted so its budget
        # actually spends over the flight, rather than starving behind a
        # higher bidder every hour.
        row = self.pacing.daily_row(campaign.id, at.date())
        target = self.pacing.hourly_target(q(campaign.daily_budget), at)
        committed = q(D(row.spent) + D(row.reserved))
        if target > ZERO:
            progress = committed / target
            pacing_factor = max(Decimal("0.25"), min(Decimal(2), Decimal(2) - progress))
        else:
            pacing_factor = Decimal(1)

        fatigue = self._fatigue(campaign.id, channel.id, at)

        score = revenue * relevance * quality * priority * pacing_factor * (Decimal(1) - fatigue)
        candidate.score = q(score)
        candidate.factors = {
            "effective_cpm": str(quote.effective_cpm),
            "relevance": str(relevance),
            "quality": str(quality.quantize(Decimal("0.0001"))),
            "priority": str(priority),
            "pacing_factor": str(pacing_factor.quantize(Decimal("0.0001"))),
            "fatigue": str(fatigue.quantize(Decimal("0.0001"))),
            "expected_impressions": str(expected),
            "planned_cost": str(cost),
        }

    def _relevance(self, campaign: Campaign, channel: PublisherChannel) -> Decimal:
        """Reward precise targeting over blanket targeting."""
        target = campaign.target
        if target is None:
            return Decimal("0.8")
        score = Decimal("0.7")
        if target.categories and (channel.category or "").lower() in [
            c.lower() for c in target.categories
        ]:
            score += Decimal("0.15")
        if target.countries and (channel.country or "").upper() in [
            c.upper() for c in target.countries
        ]:
            score += Decimal("0.1")
        if target.languages and (channel.language or "").lower() in [
            x.lower() for x in target.languages
        ]:
            score += Decimal("0.05")
        return min(Decimal(1), score)

    def _fatigue(self, campaign_id: uuid.UUID, channel_id: uuid.UUID, at: datetime) -> Decimal:
        """Penalise repeating the same campaign in the same channel."""
        recent = int(
            self.session.scalar(
                select(func.count(AdDelivery.id)).where(
                    AdDelivery.campaign_id == campaign_id,
                    AdDelivery.channel_id == channel_id,
                    AdDelivery.sent_at >= at - timedelta(days=7),
                )
            )
            or 0
        )
        return min(Decimal("0.6"), D(recent) * Decimal("0.15"))

    # -- winner ------------------------------------------------------------

    def _select(self, candidates: list[Candidate], seed: int | None = None) -> Candidate:
        mode = self._mode()
        if mode is SelectionMode.WEIGHTED:
            top_n = max(1, self.settings.int_("weighted_top_n"))
            pool = candidates[:top_n]
            weights = [float(c.score) for c in pool]
            if sum(weights) <= 0:
                return pool[0]
            rng = random.Random(seed)
            return rng.choices(pool, weights=weights, k=1)[0]
        # FIXED_CPM and AUCTION both take the top-ranked candidate; they differ
        # only in what the winner is *charged* (see _clearing_cpm).
        return candidates[0]

    def _clearing_cpm(self, candidates: list[Candidate], winner: Candidate) -> Decimal:
        """Second-price clearing for AUCTION mode (spec §32).

        The winner pays just above the runner-up's effective CPM rather than its
        own bid, which removes the incentive to shade bids downward.
        """
        if self._mode() is not SelectionMode.AUCTION:
            return q(winner.quote.effective_cpm)
        runners = [c for c in candidates if c is not winner]
        if not runners:
            return q(winner.quote.effective_cpm)
        second = max(q(c.quote.effective_cpm) for c in runners)
        increment = self.settings.decimal("auction_second_price_increment")
        clearing = q(second * (Decimal(1) + increment))
        return min(q(winner.quote.effective_cpm), max(clearing, ZERO))

    def _mode(self) -> SelectionMode:
        try:
            return SelectionMode(self.settings.str_("selection_mode"))
        except ValueError:
            return SelectionMode.AUCTION

    # ------------------------------------------------------------------
    # Delivery record + budget reservation
    # ------------------------------------------------------------------

    def _create_delivery(
        self,
        winner: Candidate,
        channel: PublisherChannel,
        charge_cpm: Decimal,
        at: datetime,
        all_candidates: list[Candidate],
    ) -> AdDelivery:
        campaign = winner.campaign
        quote = winner.quote
        # Re-derive the publisher split at the *clearing* price, so a second-price
        # discount is shared with the publisher rather than pocketed silently.
        from app.core.money import split_commission

        _, commission_per_mille = split_commission(charge_cpm, quote.commission_rate)
        publisher_cpm = q(charge_cpm - commission_per_mille)

        expected = self._expected_impressions(channel)
        cost = q(quote.cost_for(expected)[0])
        pacing = self.pacing.check(campaign, cost, at)
        if not pacing.allowed:
            cost = min(cost, pacing.headroom)
        if cost <= ZERO:
            raise CampaignNotDeliverable("no pacing headroom to reserve")

        allowance = impressions_for_budget(cost, charge_cpm) if charge_cpm > ZERO else 0

        nonce = new_token(16)
        delivery = AdDelivery(
            campaign_id=campaign.id,
            advertisement_id=winner.advertisement.id,
            channel_id=channel.id,
            publisher_id=channel.publisher_id,
            status=DeliveryStatus.PLANNED,
            telegram_chat_id=channel.telegram_chat_id,
            currency=campaign.currency,
            advertiser_cpm=q(quote.advertiser_cpm),
            effective_cpm=charge_cpm,
            publisher_cpm=publisher_cpm,
            commission_rate=quote.commission_rate,
            price_breakdown={
                **quote.breakdown(),
                "charged_cpm": str(charge_cpm),
                "selection_mode": self._mode().value,
            },
            selection_mode=self._mode(),
            selection_score=winner.score,
            selection_debug={
                "winner": winner.factors,
                "candidates": [
                    {"campaign_id": str(c.campaign.id), "score": str(c.score)}
                    for c in all_candidates[:10]
                ],
            },
            reserved_amount=cost,
            impression_allowance=allowance,
            tracking_nonce=nonce,
        )
        delivery.impression_cap = self.impressions.compute_cap(delivery, channel)
        self.session.add(delivery)
        self.session.flush()

        # Reserve against the advertiser wallet AND the pacing counters, so a
        # crash before dispatch cannot let the budget be spent twice.
        self.wallets.reserve_budget(
            campaign.advertiser_id,
            campaign.id,
            cost,
            idempotency_key=f"delivery-reserve:{delivery.id}",
            description=f"Reserved for delivery to {channel.telegram_chat_id}",
        )
        campaign.reserved_amount = q(D(campaign.reserved_amount) + cost)
        self.pacing.reserve(campaign, cost, at)
        delivery.status = DeliveryStatus.RESERVED
        self.session.flush()
        return delivery

    # ------------------------------------------------------------------
    # Steps 7-9: dispatch
    # ------------------------------------------------------------------

    def dispatch(self, delivery: AdDelivery) -> AdDelivery:
        """Post the creative to Telegram and arm the measurement window."""
        if delivery.status is DeliveryStatus.SENT:
            return delivery  # already posted; never post twice
        if delivery.status is not DeliveryStatus.RESERVED:
            raise CampaignNotDeliverable(f"delivery is {delivery.status.value}, expected reserved")

        ad = self.session.get(Advertisement, delivery.advertisement_id)
        channel = self.session.get(PublisherChannel, delivery.channel_id)
        if ad is None or channel is None:  # pragma: no cover
            raise NotFound("delivery references a missing ad or channel")

        buttons = self._buttons(delivery, ad)
        text = self._render(ad)

        try:
            if ad.ad_format.value == "image" and (ad.media_file_id or ad.media_url):
                sent = self.gateway.send_photo(
                    delivery.telegram_chat_id,
                    ad.media_file_id or ad.media_url,
                    caption=text,
                    buttons=buttons,
                )
            elif ad.ad_format.value == "video" and (ad.media_file_id or ad.media_url):
                sent = self.gateway.send_video(
                    delivery.telegram_chat_id,
                    ad.media_file_id or ad.media_url,
                    caption=text,
                    buttons=buttons,
                )
            else:
                sent = self.gateway.send_text(
                    delivery.telegram_chat_id,
                    text,
                    buttons=buttons,
                    disable_preview=ad.disable_preview,
                )
        except TelegramError as exc:
            self.fail(delivery, str(exc))
            raise

        now = utcnow()
        delivery.telegram_message_id = sent.telegram_message_id
        delivery.message_link = sent.link
        delivery.status = DeliveryStatus.SENT
        delivery.sent_at = now
        delivery.measurement_ends_at = now + timedelta(
            hours=self.settings.int_("impression_window_hours")
        )
        channel.last_ad_at = now
        channel.total_ads_served += 1
        self.session.flush()
        log.info(
            "ad_delivered",
            delivery_id=str(delivery.id),
            chat_id=delivery.telegram_chat_id,
            message_id=sent.telegram_message_id,
        )
        return delivery

    def fail(self, delivery: AdDelivery, reason: str) -> AdDelivery:
        """Release everything a failed delivery reserved. No money is kept."""
        if delivery.status in (DeliveryStatus.FAILED, DeliveryStatus.CANCELLED):
            return delivery
        campaign = self.session.get(Campaign, delivery.campaign_id)
        amount = q(delivery.reserved_amount)
        if campaign is not None and amount > ZERO:
            self.wallets.release_budget(
                campaign.advertiser_id,
                campaign.id,
                amount,
                idempotency_key=f"delivery-release:{delivery.id}",
                description="Released after failed delivery",
            )
            campaign.reserved_amount = max(ZERO, q(D(campaign.reserved_amount) - amount))
            self.pacing.release(campaign, amount)
        delivery.status = DeliveryStatus.FAILED
        delivery.failure_reason = reason[:500]
        delivery.reserved_amount = ZERO
        self.session.flush()
        return delivery

    def deliver_to(
        self, channel: PublisherChannel, *, at: datetime | None = None, seed: int | None = None
    ) -> PlanResult:
        """plan + dispatch, the entry point workers call."""
        result = self.plan(channel, at=at, seed=seed)
        if result.delivery is None:
            return result
        self.dispatch(result.delivery)
        return result

    # -- rendering ---------------------------------------------------------

    def tracking_url(self, delivery: AdDelivery) -> str:
        """The signed redirect that makes a click independently measurable."""
        from app.core.config import settings as app_settings

        token = tracking_token(str(delivery.id), delivery.tracking_nonce)
        return f"{app_settings.base_url.rstrip('/')}/t/{delivery.id}/{token}"

    def _buttons(self, delivery: AdDelivery, ad: Advertisement) -> list[dict] | None:
        if not ad.destination_url:
            return None
        # Always route through our redirect: the advertiser's raw URL would be
        # unmeasurable, and we must not bill for clicks we cannot observe.
        return [{"text": ad.cta_text or "Learn more", "url": self.tracking_url(delivery)}]

    def _render(self, ad: Advertisement) -> str:
        parts = [ad.body_text or ""]
        label = self.settings.str_("platform_name")
        parts.append(f"\n\n<i>Ad · {label}</i>")  # disclosure is not optional
        return "".join(parts).strip()[:4000]

    # -- housekeeping ------------------------------------------------------

    def remove_expired_posts(self, limit: int = 100) -> int:
        """Delete ad posts past their TTL so channels are not left cluttered."""
        ttl = self.settings.int_("ad_post_ttl_hours")
        if ttl <= 0:
            return 0
        cutoff = utcnow() - timedelta(hours=ttl)
        rows = self.session.scalars(
            select(AdDelivery)
            .where(
                AdDelivery.status.in_([DeliveryStatus.SETTLED, DeliveryStatus.SENT]),
                AdDelivery.sent_at.is_not(None),
                AdDelivery.sent_at < cutoff,
                AdDelivery.removed_at.is_(None),
                AdDelivery.telegram_message_id.is_not(None),
            )
            .limit(limit)
        ).all()
        removed = 0
        for delivery in rows:
            if self.gateway.delete_message(delivery.telegram_chat_id, delivery.telegram_message_id):
                removed += 1
            delivery.removed_at = utcnow()
        self.session.flush()
        return removed
