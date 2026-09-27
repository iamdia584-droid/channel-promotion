"""Click/impression tracking redirect — the primary measurable signal (spec §8).

An ad's inline button points here, not at the advertiser's URL. Resolving this
endpoint is a first-party observation we can deduplicate, score for fraud and bill
on. Without it there is nothing about a Telegram channel post the Bot API lets us
measure (docs/TELEGRAM_CONSTRAINTS.md).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.api.deps import DbSession
from app.core.cache import rate_limit
from app.core.errors import NotFound, RateLimited
from app.core.logging import get_logger
from app.core.security import verify_tracking_token
from app.db.base import utcnow
from app.models.campaigns import Advertisement
from app.models.delivery import AdDelivery
from app.models.enums import FraudSubject, ImpressionKind, ImpressionSource
from app.services.fraud import FraudService
from app.services.impressions import ImpressionService, hash_identity

log = get_logger(__name__)

router = APIRouter(tags=["tracking"])


@router.get("/t/{delivery_id}/{token}")
def track(
    db: DbSession, request: Request, delivery_id: uuid.UUID, token: str
) -> Response:
    """Record the interaction, then redirect to the advertiser's destination."""
    delivery = db.get(AdDelivery, delivery_id)
    if delivery is None:
        raise NotFound("unknown link")

    # The token is an HMAC over (delivery_id, nonce), so a publisher cannot mint
    # extra tracking URLs to inflate their own impressions.
    if not verify_tracking_token(str(delivery.id), delivery.tracking_nonce, token):
        log.warning("tracking_token_invalid", delivery_id=str(delivery_id))
        raise NotFound("unknown link")

    ad = db.get(Advertisement, delivery.advertisement_id)
    destination = (ad.destination_url if ad else None) or "https://t.me"

    ip = _client_ip(request)
    ip_hash = hash_identity(ip) if ip else None
    user_agent = request.headers.get("user-agent")
    ua_hash = hash_identity(user_agent) if user_agent else None
    telegram_user_id = _telegram_user_id(request)

    # A cheap per-address limit in front of the database, so a flood cannot turn
    # into a write-amplification attack on the impression table.
    try:
        rate_limit(f"track:{delivery_id}:{ip_hash or 'anon'}", limit=30, window=60)
    except RateLimited:
        return RedirectResponse(destination, status_code=302)

    fraud = FraudService(db)
    assessment = fraud.score_impression_event(
        delivery, telegram_user_id=telegram_user_id, ip_hash=ip_hash,
        user_agent_hash=ua_hash,
    )

    impressions = ImpressionService(db)
    # The dedupe key is what makes refresh abuse structurally free: the same
    # viewer reloading the link collides on the unique index.
    identity = str(telegram_user_id) if telegram_user_id else (ip_hash or "anon")
    dedupe = f"click:{delivery.id}:{identity}"

    result = impressions.record(
        delivery,
        kind=ImpressionKind.MEASURED,
        source=ImpressionSource.TRACKING_LINK,
        dedupe_key=dedupe,
        telegram_user_id=telegram_user_id,
        ip_hash=ip_hash,
        user_agent_hash=ua_hash,
        fraud_score=assessment.score,
        fraud_reasons=assessment.triggered(),
        occurred_at=utcnow(),
        meta={"referrer": (request.headers.get("referer") or "")[:300]},
    )
    click = impressions.record_click(
        delivery,
        dedupe_key=f"clk:{delivery.id}:{identity}",
        telegram_user_id=telegram_user_id,
        ip_hash=ip_hash,
        user_agent_hash=ua_hash,
        referrer=request.headers.get("referer"),
        fraud_score=assessment.score,
        fraud_reasons=assessment.triggered(),
    )
    if click is not None and result.impression is not None:
        click.impression_id = result.impression.id

    if assessment.score >= 31:
        fraud.record_event(
            FraudSubject.CLICK, click.id if click else None, assessment,
            publisher_id=delivery.publisher_id, campaign_id=delivery.campaign_id,
            channel_id=delivery.channel_id, delivery_id=delivery.id,
            action_taken="not_billed" if not result.billable else "billed_flagged",
        )

    # The viewer is redirected regardless of how we classified the event: a fraud
    # verdict is ours to act on internally, not a reason to break their click.
    return RedirectResponse(destination, status_code=302)


def _client_ip(request: Request) -> str | None:
    """Trust ``X-Forwarded-For`` only for its left-most entry behind our proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _telegram_user_id(request: Request) -> int | None:
    """Present only when the click came through the bot's own deep link."""
    raw = request.query_params.get("tg")
    if not raw or not raw.isdigit():
        return None
    try:
        return int(raw)
    except ValueError:  # pragma: no cover
        return None
