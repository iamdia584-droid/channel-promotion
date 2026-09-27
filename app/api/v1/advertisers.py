"""Advertiser endpoints: wallet, campaigns, statistics (spec §1, §25)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response

from app.api.deps import (
    CurrentAdvertiser,
    CurrentPrincipal,
    DbSession,
    IdempotencyKey,
    throttle,
)
from app.core.idempotency import lookup, store
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignOut,
    CampaignPreview,
    CampaignStats,
    PauseIn,
)
from app.schemas.common import Acknowledged, MoneyInput, Page, TransactionOut, WalletOut
from app.services.analytics import AnalyticsService
from app.services.campaigns import CampaignDraft, CampaignService
from app.services.payments import DepositService
from app.services.refunds import RefundService
from app.services.wallet import WalletService

router = APIRouter(prefix="/advertisers", tags=["advertisers"])


# --- wallet ---------------------------------------------------------------


@router.get("/me/wallet", response_model=WalletOut)
def get_wallet(db: DbSession, advertiser: CurrentAdvertiser) -> WalletOut:
    service = WalletService(db)
    return WalletOut.model_validate(service.view(service.for_advertiser(advertiser.id)))


@router.get("/me/transactions", response_model=Page[TransactionOut])
def list_transactions(
    db: DbSession,
    advertiser: CurrentAdvertiser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TransactionOut]:
    service = WalletService(db)
    wallet = service.for_advertiser(advertiser.id)
    rows = service.statement(wallet.id, limit=limit, offset=offset)
    return Page[TransactionOut](
        items=[TransactionOut.model_validate(r) for r in rows],
        total=len(rows) + offset, limit=limit, offset=offset,
    )


@router.post("/me/deposits", status_code=201,
             dependencies=[throttle("deposit", limit=10, window=3600)])
def initiate_deposit(
    db: DbSession, advertiser: CurrentAdvertiser, body: MoneyInput
) -> dict:
    """Start a deposit. Crediting happens only on a verified provider callback."""
    deposit, instructions = DepositService(db).initiate(advertiser.id, body.amount)
    return {
        "deposit_id": str(deposit.id),
        "amount": format(deposit.amount, "f"),
        "currency": deposit.currency,
        "status": deposit.status.value,
        "instructions": instructions,
    }


# --- campaigns ------------------------------------------------------------


@router.post("/me/campaigns", response_model=CampaignOut, status_code=201,
             dependencies=[throttle("campaign_create", limit=30, window=3600)])
def create_campaign(
    db: DbSession,
    advertiser: CurrentAdvertiser,
    body: CampaignCreate,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> CampaignOut:
    scope = f"campaign_create:{advertiser.id}"
    payload = body.model_dump(mode="json")
    if idempotency_key:
        cached = lookup(db, scope, idempotency_key, payload)
        if cached is not None:
            response.status_code = cached["status_code"]
            return CampaignOut.model_validate(cached["body"])

    draft = CampaignDraft(
        name=body.name,
        campaign_type=body.campaign_type,
        pricing_model=body.pricing_model,
        total_budget=body.total_budget,
        daily_budget=body.daily_budget,
        bid_cpm=body.bid_cpm,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        body_text=body.body_text,
        media_file_id=body.media_file_id,
        media_url=body.media_url,
        destination_url=body.destination_url,
        cta_text=body.cta_text,
        priority=body.priority,
        max_impressions_per_user=body.max_impressions_per_user,
        max_impressions_per_channel_per_day=body.max_impressions_per_channel_per_day,
        specific_channel_ids=list(body.specific_channel_ids),
        countries=body.targeting.countries,
        languages=body.targeting.languages,
        categories=body.targeting.categories,
        excluded_categories=body.targeting.excluded_categories,
        audience_types=body.targeting.audience_types,
        min_members=body.targeting.min_members,
        max_members=body.targeting.max_members,
        min_avg_views=body.targeting.min_avg_views,
        max_avg_views=body.targeting.max_avg_views,
    )
    campaign = CampaignService(db).create(advertiser, draft)
    out = CampaignOut.model_validate(campaign)
    if idempotency_key:
        store(db, scope, idempotency_key, payload, 201, out.model_dump(mode="json"))
    return out


@router.get("/me/campaigns", response_model=Page[CampaignOut])
def list_campaigns(
    db: DbSession,
    advertiser: CurrentAdvertiser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[CampaignOut]:
    rows = CampaignService(db).list_for_advertiser(advertiser.id, limit, offset)
    return Page[CampaignOut](
        items=[CampaignOut.model_validate(c) for c in rows],
        total=len(rows) + offset, limit=limit, offset=offset,
    )


@router.get("/me/campaigns/{campaign_id}", response_model=CampaignOut)
def get_campaign(
    db: DbSession, advertiser: CurrentAdvertiser, campaign_id: uuid.UUID
) -> CampaignOut:
    return CampaignOut.model_validate(
        CampaignService(db).get_owned(campaign_id, advertiser.id)
    )


@router.get("/me/campaigns/{campaign_id}/preview", response_model=CampaignPreview)
def preview_campaign(
    db: DbSession, advertiser: CurrentAdvertiser, campaign_id: uuid.UUID
) -> CampaignPreview:
    service = CampaignService(db)
    campaign = service.get_owned(campaign_id, advertiser.id)
    return CampaignPreview.model_validate(service.preview(campaign))


@router.post("/me/campaigns/{campaign_id}/submit", response_model=CampaignOut)
def submit_campaign(
    db: DbSession, advertiser: CurrentAdvertiser, principal: CurrentPrincipal,
    campaign_id: uuid.UUID,
) -> CampaignOut:
    service = CampaignService(db)
    campaign = service.get_owned(campaign_id, advertiser.id)
    return CampaignOut.model_validate(service.submit(campaign, principal.actor))


@router.post("/me/campaigns/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(
    db: DbSession, advertiser: CurrentAdvertiser, principal: CurrentPrincipal,
    campaign_id: uuid.UUID, body: PauseIn,
) -> CampaignOut:
    service = CampaignService(db)
    campaign = service.get_owned(campaign_id, advertiser.id)
    return CampaignOut.model_validate(
        service.pause(campaign, body.reason, principal.actor)
    )


@router.post("/me/campaigns/{campaign_id}/resume", response_model=CampaignOut)
def resume_campaign(
    db: DbSession, advertiser: CurrentAdvertiser, principal: CurrentPrincipal,
    campaign_id: uuid.UUID,
) -> CampaignOut:
    service = CampaignService(db)
    campaign = service.get_owned(campaign_id, advertiser.id)
    return CampaignOut.model_validate(service.resume(campaign, principal.actor))


@router.post("/me/campaigns/{campaign_id}/cancel")
def cancel_campaign(
    db: DbSession, advertiser: CurrentAdvertiser, principal: CurrentPrincipal,
    campaign_id: uuid.UUID,
) -> dict:
    """Cancel and refund the unspent reservation (spec §35)."""
    campaign = CampaignService(db).get_owned(campaign_id, advertiser.id)
    campaign, refund = RefundService(db).cancel_campaign(campaign, principal.actor)
    return {
        "campaign_id": str(campaign.id),
        "status": campaign.status.value,
        "refunded": format(refund.approved_amount, "f") if refund else "0",
        "currency": campaign.currency,
    }


@router.get("/me/campaigns/{campaign_id}/stats", response_model=CampaignStats)
def campaign_stats(
    db: DbSession, advertiser: CurrentAdvertiser, campaign_id: uuid.UUID
) -> CampaignStats:
    service = CampaignService(db)
    campaign = service.get_owned(campaign_id, advertiser.id)
    return CampaignStats.model_validate(service.stats(campaign))


@router.get("/me/campaigns/{campaign_id}/breakdown")
def campaign_breakdown(
    db: DbSession, advertiser: CurrentAdvertiser, campaign_id: uuid.UUID,
    by: str = Query("publisher", pattern="^(publisher|country|category)$"),
) -> dict:
    CampaignService(db).get_owned(campaign_id, advertiser.id)
    rows = AnalyticsService(db).campaign_breakdown(campaign_id, by=by)
    return {"by": by, "rows": [_stringify(r) for r in rows]}


# --- refunds --------------------------------------------------------------


@router.post("/me/campaigns/{campaign_id}/refunds", status_code=201)
def request_refund(
    db: DbSession, advertiser: CurrentAdvertiser, principal: CurrentPrincipal,
    campaign_id: uuid.UUID, idempotency_key: IdempotencyKey = None,
) -> dict:
    campaign = CampaignService(db).get_owned(campaign_id, advertiser.id)
    service = RefundService(db)
    quote = service.quote(campaign)
    refund = service.request(
        campaign, idempotency_key=idempotency_key, actor=principal.actor
    )
    return {
        "refund_id": str(refund.id),
        "status": refund.status.value,
        "requested": format(refund.requested_amount, "f"),
        "breakdown": quote.explain(),
    }


@router.get("/me/campaigns/{campaign_id}/refund-quote")
def refund_quote(
    db: DbSession, advertiser: CurrentAdvertiser, campaign_id: uuid.UUID
) -> dict:
    campaign = CampaignService(db).get_owned(campaign_id, advertiser.id)
    return RefundService(db).quote(campaign).explain()


# --- analytics ------------------------------------------------------------


@router.get("/me/overview")
def overview(db: DbSession, advertiser: CurrentAdvertiser) -> dict:
    return _stringify(AnalyticsService(db).advertiser_overview(advertiser.id))


@router.get("/me/daily")
def daily(
    db: DbSession, advertiser: CurrentAdvertiser, days: int = Query(30, ge=1, le=365)
) -> dict:
    return {"rows": [_stringify(r)
                     for r in AnalyticsService(db).advertiser_daily(advertiser.id, days)]}


@router.get("/me/report.csv", response_class=Response)
def report_csv(
    db: DbSession, advertiser: CurrentAdvertiser, days: int = Query(30, ge=1, le=365)
) -> Response:
    csv_text = AnalyticsService(db).advertiser_report_csv(advertiser.id, days)
    return Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=advertiser-report.csv"},
    )


def _stringify(row: dict) -> dict:
    """Decimals leave the API as strings, never JSON floats (spec §11)."""
    from decimal import Decimal

    out = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = format(value, "f")
        elif isinstance(value, dict):
            out[key] = _stringify(value)
        else:
            out[key] = value
    return out
