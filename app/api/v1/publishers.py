"""Publisher endpoints: channels, earnings, withdrawals (spec §1, §25)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response

from app.api.deps import (
    CurrentPrincipal,
    CurrentPublisher,
    DbSession,
    IdempotencyKey,
    throttle,
)
from app.core.errors import NotFound, ValidationFailed
from app.core.money import D
from app.schemas.common import Acknowledged, Page, WalletOut
from app.schemas.publishers import (
    ChannelOut,
    ChannelRegisterIn,
    ChannelSettingsIn,
    EarningOut,
    EarningsSummary,
    FeeQuoteOut,
    PayoutMethodIn,
    PayoutMethodOut,
    WithdrawalIn,
    WithdrawalOut,
)
from app.services.analytics import AnalyticsService
from app.services.channels import ChannelService
from app.services.earnings import EarningsService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService
from app.services.withdrawals import WithdrawalService

router = APIRouter(prefix="/publishers", tags=["publishers"])


def _channel_out(channel) -> ChannelOut:
    from decimal import Decimal

    score = D(channel.quality_score or 0)
    band = (
        "poor"
        if score < Decimal("0.25")
        else "fair"
        if score < Decimal("0.50")
        else "good"
        if score < Decimal("0.75")
        else "excellent"
    )
    return ChannelOut(
        id=channel.id,
        telegram_chat_id=channel.telegram_chat_id,
        title=channel.chat.title if channel.chat else None,
        username=channel.chat.username if channel.chat else None,
        status=channel.status,
        verification_status=channel.verification_status.value,
        category=channel.category,
        language=channel.language,
        country=channel.country,
        members=channel.chat.member_count if channel.chat else 0,
        avg_views=channel.avg_views,
        avg_ad_views=channel.avg_ad_views,
        total_impressions=channel.total_impressions,
        total_clicks=channel.total_clicks,
        total_ads_served=channel.total_ads_served,
        earned=channel.lifetime_earned,
        quality_band=band,
        auto_advertising=channel.auto_advertising,
        created_at=channel.created_at,
    )


def _owned_channel(db, publisher, channel_id: uuid.UUID):
    from app.models.telegram import PublisherChannel

    channel = db.get(PublisherChannel, channel_id)
    if channel is None or channel.publisher_id != publisher.id:
        # Not "forbidden": confirming existence would leak another publisher's ids.
        raise NotFound("channel not found")
    return channel


# --- channels -------------------------------------------------------------


@router.post(
    "/me/channels",
    response_model=ChannelOut,
    status_code=201,
    dependencies=[throttle("channel_register", limit=20, window=3600)],
)
def register_channel(
    db: DbSession, publisher: CurrentPublisher, body: ChannelRegisterIn
) -> ChannelOut:
    """Register a chat. Ownership is proven against Telegram, not assumed (spec §4)."""
    result = ChannelService(db).register(
        publisher,
        body.identifier,
        category=body.category,
        language=body.language,
        country=body.country,
    )
    if not result.ok:
        raise ValidationFailed(result.user_message(), verification_status=result.status.value)
    return _channel_out(result.channel)


@router.get("/me/channels", response_model=list[ChannelOut])
def list_channels(db: DbSession, publisher: CurrentPublisher) -> list[ChannelOut]:
    return [_channel_out(c) for c in ChannelService(db).list_for_publisher(publisher.id)]


@router.get("/me/channels/{channel_id}", response_model=ChannelOut)
def get_channel(db: DbSession, publisher: CurrentPublisher, channel_id: uuid.UUID) -> ChannelOut:
    return _channel_out(_owned_channel(db, publisher, channel_id))


@router.patch("/me/channels/{channel_id}", response_model=ChannelOut)
def update_channel(
    db: DbSession,
    publisher: CurrentPublisher,
    channel_id: uuid.UUID,
    body: ChannelSettingsIn,
) -> ChannelOut:
    channel = _owned_channel(db, publisher, channel_id)
    if body.auto_advertising is not None:
        channel.auto_advertising = body.auto_advertising
    if body.accepted_categories is not None:
        channel.accepted_categories = [c.lower() for c in body.accepted_categories]
    for field in ("max_ads_per_day", "max_ads_per_week", "min_ad_interval_minutes"):
        value = getattr(body, field)
        if value is not None:
            setattr(channel, field, value)
    if body.min_cpm_floor is not None:
        channel.min_cpm_floor = body.min_cpm_floor
    db.flush()
    return _channel_out(channel)


@router.post("/me/channels/{channel_id}/revalidate", response_model=ChannelOut)
def revalidate_channel(
    db: DbSession, publisher: CurrentPublisher, channel_id: uuid.UUID
) -> ChannelOut:
    """Re-check the bot's admin rights — publishers do remove the bot."""
    channel = _owned_channel(db, publisher, channel_id)
    ChannelService(db).revalidate(channel)
    return _channel_out(channel)


@router.get("/me/channels/performance")
def channel_performance(db: DbSession, publisher: CurrentPublisher) -> dict:
    from app.api.v1.advertisers import _stringify

    return {"rows": [_stringify(r) for r in AnalyticsService(db).channel_performance(publisher.id)]}


# --- earnings -------------------------------------------------------------


@router.get("/me/wallet", response_model=WalletOut)
def get_wallet(db: DbSession, publisher: CurrentPublisher) -> WalletOut:
    service = WalletService(db)
    return WalletOut.model_validate(service.view(service.for_publisher(publisher.id)))


@router.get("/me/earnings/summary", response_model=EarningsSummary)
def earnings_summary(db: DbSession, publisher: CurrentPublisher) -> EarningsSummary:
    return EarningsSummary.model_validate(EarningsService(db).summary(publisher.id))


@router.get("/me/earnings", response_model=Page[EarningOut])
def list_earnings(
    db: DbSession,
    publisher: CurrentPublisher,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EarningOut]:
    rows = EarningsService(db).list_for_publisher(publisher.id, limit, offset)
    return Page[EarningOut](
        items=[EarningOut.model_validate(r) for r in rows],
        total=len(rows) + offset,
        limit=limit,
        offset=offset,
    )


@router.get("/me/daily")
def daily(db: DbSession, publisher: CurrentPublisher, days: int = Query(30, ge=1, le=365)) -> dict:
    from app.api.v1.advertisers import _stringify

    return {
        "rows": [_stringify(r) for r in AnalyticsService(db).publisher_daily(publisher.id, days)]
    }


@router.get("/me/overview")
def overview(db: DbSession, publisher: CurrentPublisher) -> dict:
    from app.api.v1.advertisers import _stringify

    return _stringify(AnalyticsService(db).publisher_overview(publisher.id))


@router.get("/me/report.csv", response_class=Response)
def report_csv(
    db: DbSession, publisher: CurrentPublisher, days: int = Query(30, ge=1, le=365)
) -> Response:
    csv_text = AnalyticsService(db).publisher_report_csv(publisher.id, days)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=publisher-report.csv"},
    )


# --- payout methods and withdrawals --------------------------------------


@router.post("/me/payout-methods", response_model=PayoutMethodOut, status_code=201)
def add_payout_method(
    db: DbSession, publisher: CurrentPublisher, body: PayoutMethodIn
) -> PayoutMethodOut:
    record = WithdrawalService(db).add_payout_method(
        publisher.id,
        body.method,
        body.destination,
        account_name=body.account_name,
        bank_name=body.bank_name,
        branch=body.branch,
        label=body.label,
        make_default=body.make_default,
    )
    return PayoutMethodOut.model_validate(record)


@router.get("/me/payout-methods", response_model=list[PayoutMethodOut])
def list_payout_methods(db: DbSession, publisher: CurrentPublisher) -> list[PayoutMethodOut]:
    return [
        PayoutMethodOut.model_validate(m)
        for m in WithdrawalService(db).list_payout_methods(publisher.id)
    ]


@router.delete("/me/payout-methods/{method_id}", response_model=Acknowledged)
def remove_payout_method(
    db: DbSession, publisher: CurrentPublisher, method_id: uuid.UUID
) -> Acknowledged:
    from app.models.money import PayoutMethodRecord

    record = db.get(PayoutMethodRecord, method_id)
    if record is None or record.publisher_id != publisher.id:
        raise NotFound("payout method not found")
    record.is_active = False
    record.is_default = False
    db.flush()
    return Acknowledged(message="payout method removed")


@router.get("/me/withdrawals/quote", response_model=FeeQuoteOut)
def withdrawal_quote(
    db: DbSession, publisher: CurrentPublisher, amount: str = Query(...)
) -> FeeQuoteOut:
    service = WithdrawalService(db)
    breakdown = service.quote_fee(amount)
    return FeeQuoteOut(
        amount=breakdown.amount,
        fee=breakdown.fee,
        net=breakdown.net,
        minimum=SettingsService(db).money("min_withdrawal"),
        currency=publisher.currency,
    )


@router.post(
    "/me/withdrawals",
    response_model=WithdrawalOut,
    status_code=201,
    dependencies=[throttle("withdrawal", limit=10, window=3600)],
)
def request_withdrawal(
    db: DbSession,
    publisher: CurrentPublisher,
    principal: CurrentPrincipal,
    body: WithdrawalIn,
    idempotency_key: IdempotencyKey = None,
) -> WithdrawalOut:
    withdrawal = WithdrawalService(db).request(
        publisher.id,
        body.amount,
        body.payout_method_id,
        idempotency_key=idempotency_key,
        actor=principal.actor,
    )
    return WithdrawalOut.model_validate(withdrawal)


@router.get("/me/withdrawals", response_model=list[WithdrawalOut])
def list_withdrawals(db: DbSession, publisher: CurrentPublisher) -> list[WithdrawalOut]:
    return [
        WithdrawalOut.model_validate(w)
        for w in WithdrawalService(db).list_for_publisher(publisher.id)
    ]


@router.post("/me/withdrawals/{withdrawal_id}/cancel", response_model=WithdrawalOut)
def cancel_withdrawal(
    db: DbSession,
    publisher: CurrentPublisher,
    principal: CurrentPrincipal,
    withdrawal_id: uuid.UUID,
) -> WithdrawalOut:
    from app.models.money import Withdrawal

    withdrawal = db.get(Withdrawal, withdrawal_id)
    if withdrawal is None or withdrawal.publisher_id != publisher.id:
        raise NotFound("withdrawal not found")
    return WithdrawalOut.model_validate(WithdrawalService(db).cancel(withdrawal, principal.actor))
