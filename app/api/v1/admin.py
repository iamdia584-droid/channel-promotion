"""Admin and moderator endpoints (spec §1 admin/moderator, §23, §25)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field
from sqlalchemy import select

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.api.v1.advertisers import _stringify
from app.core.errors import NotFound, ValidationFailed
from app.models.campaigns import Campaign
from app.models.enums import (
    ChannelStatus,
    PricingRuleScope,
    UserStatus,
)
from app.models.identity import Advertiser, Publisher, User
from app.models.money import Withdrawal
from app.models.ops import FraudCase, FraudEvent, PricingRule
from app.models.telegram import PublisherChannel, TelegramChat
from app.schemas.campaigns import CampaignOut, RejectIn
from app.schemas.common import Acknowledged, MoneyInput, Schema
from app.schemas.publishers import WithdrawalOut
from app.services.analytics import AnalyticsService
from app.services.audit import Actor, AuditService
from app.services.campaigns import CampaignService
from app.services.fraud import FraudService
from app.services.ledger import LedgerService
from app.services.payments import DepositService, ManualProvider
from app.services.pricing import PricingService
from app.services.refunds import RefundService
from app.services.settings_service import SettingsService
from app.services.withdrawals import WithdrawalService

router = APIRouter(prefix="/admin", tags=["admin"])


def _actor(request: Request, staff) -> Actor:
    return Actor.staff(
        staff,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=request.headers.get("x-request-id"),
    )


# --- dashboard ------------------------------------------------------------


@router.get("/overview")
def overview(db: DbSession, staff: RequireStaff) -> dict:
    """The admin dashboard cards (spec §23)."""
    return _stringify(AnalyticsService(db).platform_overview())


@router.get("/ledger/trial-balance")
def trial_balance(db: DbSession, staff: RequireAdmin, currency: str = "BDT") -> dict:
    """Debits must equal credits. A non-zero difference is a bug, and visible."""
    return _stringify(LedgerService(db).trial_balance(currency))


# --- campaign moderation --------------------------------------------------


@router.get("/campaigns/queue", response_model=list[CampaignOut])
def moderation_queue(db: DbSession, staff: RequireStaff) -> list[CampaignOut]:
    return [CampaignOut.model_validate(c) for c in CampaignService(db).moderation_queue()]


@router.post("/campaigns/{campaign_id}/approve", response_model=CampaignOut)
def approve_campaign(
    db: DbSession,
    staff: RequireStaff,
    request: Request,
    campaign_id: uuid.UUID,
    note: str | None = None,
) -> CampaignOut:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    return CampaignOut.model_validate(
        CampaignService(db).approve(campaign, _actor(request, staff), note)
    )


@router.post("/campaigns/{campaign_id}/reject", response_model=CampaignOut)
def reject_campaign(
    db: DbSession,
    staff: RequireStaff,
    request: Request,
    campaign_id: uuid.UUID,
    body: RejectIn,
) -> CampaignOut:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    return CampaignOut.model_validate(
        CampaignService(db).reject(campaign, _actor(request, staff), body.reason)
    )


@router.post("/campaigns/{campaign_id}/suspend", response_model=CampaignOut)
def suspend_campaign(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    campaign_id: uuid.UUID,
    body: RejectIn,
) -> CampaignOut:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    return CampaignOut.model_validate(
        CampaignService(db).suspend(campaign, _actor(request, staff), body.reason)
    )


# --- channels -------------------------------------------------------------


class ChannelDecisionIn(Schema):
    status: ChannelStatus
    reason: str | None = Field(None, max_length=500)


@router.get("/channels")
def list_channels(
    db: DbSession,
    staff: RequireStaff,
    status: ChannelStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    stmt = select(PublisherChannel).order_by(PublisherChannel.created_at.desc())
    if status is not None:
        stmt = stmt.where(PublisherChannel.status == status)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return {
        "items": [
            {
                "id": str(c.id),
                "telegram_chat_id": c.telegram_chat_id,
                "title": c.chat.title if c.chat else None,
                "status": c.status.value,
                "verification_status": c.verification_status.value,
                "members": c.chat.member_count if c.chat else 0,
                "avg_views": c.avg_views,
                "quality_score": str(c.quality_score),
                "fraud_score": c.fraud_score,
                "publisher_id": str(c.publisher_id),
            }
            for c in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post("/channels/{channel_id}/status", response_model=Acknowledged)
def set_channel_status(
    db: DbSession,
    staff: RequireStaff,
    request: Request,
    channel_id: uuid.UUID,
    body: ChannelDecisionIn,
) -> Acknowledged:
    channel = db.get(PublisherChannel, channel_id)
    if channel is None:
        raise NotFound("channel not found")
    old = channel.status
    channel.status = body.status
    channel.rejection_reason = body.reason
    db.flush()
    AuditService(db).log(
        _actor(request, staff),
        "channel.status_changed",
        target_type="channel",
        target_id=channel.id,
        old_value={"status": str(old)},
        new_value={"status": str(body.status)},
        reason=body.reason,
    )
    from app.services.notifications import NotificationService

    if body.status is ChannelStatus.SUSPENDED:
        NotificationService(db).queue_for_publisher(
            channel.publisher_id,
            "channel_suspended",
            {
                "channel_title": channel.chat.title if channel.chat else "your channel",
                "reason": body.reason or "contact support",
            },
        )
    return Acknowledged(message=f"channel is now {body.status.value}")


@router.post("/channels/{channel_id}/blacklist", response_model=Acknowledged)
def blacklist_chat(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    channel_id: uuid.UUID,
    body: RejectIn,
) -> Acknowledged:
    channel = db.get(PublisherChannel, channel_id)
    if channel is None:
        raise NotFound("channel not found")
    chat = db.get(TelegramChat, channel.telegram_chat_id_ref)
    chat.is_blacklisted = True
    chat.blacklist_reason = body.reason
    channel.status = ChannelStatus.SUSPENDED
    db.flush()
    AuditService(db).log(
        _actor(request, staff),
        "chat.blacklisted",
        target_type="telegram_chat",
        target_id=chat.id,
        new_value={"is_blacklisted": True},
        reason=body.reason,
    )
    return Acknowledged(message="chat blacklisted")


# --- users ----------------------------------------------------------------


@router.get("/users")
def list_users(
    db: DbSession,
    staff: RequireStaff,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = db.scalars(
        select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "items": [
            {
                "id": str(u.id),
                "telegram_user_id": u.telegram_user_id,
                "display_name": u.display_name,
                "status": u.status.value,
                "roles": sorted(r.value for r in u.roles),
                "created_at": u.created_at.isoformat(),
            }
            for u in rows
        ],
        "limit": limit,
        "offset": offset,
    }


class SuspendIn(Schema):
    reason: str = Field(..., min_length=3, max_length=500)


@router.post("/advertisers/{advertiser_id}/suspend", response_model=Acknowledged)
def suspend_advertiser(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    advertiser_id: uuid.UUID,
    body: SuspendIn,
) -> Acknowledged:
    advertiser = db.get(Advertiser, advertiser_id)
    if advertiser is None:
        raise NotFound("advertiser not found")
    advertiser.status = UserStatus.SUSPENDED
    db.flush()
    AuditService(db).log(
        _actor(request, staff),
        "advertiser.suspended",
        target_type="advertiser",
        target_id=advertiser.id,
        new_value={"status": "suspended"},
        reason=body.reason,
    )
    return Acknowledged(message="advertiser suspended")


@router.post("/publishers/{publisher_id}/suspend", response_model=Acknowledged)
def suspend_publisher(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    publisher_id: uuid.UUID,
    body: SuspendIn,
) -> Acknowledged:
    publisher = db.get(Publisher, publisher_id)
    if publisher is None:
        raise NotFound("publisher not found")
    publisher.status = UserStatus.SUSPENDED
    db.flush()
    AuditService(db).log(
        _actor(request, staff),
        "publisher.suspended",
        target_type="publisher",
        target_id=publisher.id,
        new_value={"status": "suspended"},
        reason=body.reason,
    )
    return Acknowledged(message="publisher suspended")


@router.post("/{party}/{party_id}/reinstate", response_model=Acknowledged)
def reinstate(
    db: DbSession, staff: RequireAdmin, request: Request, party: str, party_id: uuid.UUID
) -> Acknowledged:
    model = {"advertisers": Advertiser, "publishers": Publisher}.get(party)
    if model is None:
        raise ValidationFailed("party must be 'advertisers' or 'publishers'")
    row = db.get(model, party_id)
    if row is None:
        raise NotFound(f"{party[:-1]} not found")
    row.status = UserStatus.ACTIVE
    db.flush()
    AuditService(db).log(
        _actor(request, staff),
        f"{party[:-1]}.reinstated",
        target_type=party[:-1],
        target_id=row.id,
        new_value={"status": "active"},
    )
    return Acknowledged(message="reinstated")


# --- deposits -------------------------------------------------------------


class ManualDepositIn(MoneyInput):
    advertiser_id: uuid.UUID
    provider_transaction_id: str = Field(..., min_length=3, max_length=128)
    reference: str | None = Field(None, max_length=128)


@router.post("/deposits/confirm")
def confirm_deposit(
    db: DbSession, staff: RequireAdmin, request: Request, body: ManualDepositIn
) -> dict:
    """Record a deposit received out of band (bank transfer, manual bKash).

    Idempotent on ``provider_transaction_id``: submitting the same reference twice
    credits once (spec §26).
    """
    service = DepositService(db, ManualProvider())
    deposit, _ = service.initiate(body.advertiser_id, body.amount)
    charge = ManualProvider().verify_callback(
        {
            "provider_transaction_id": body.provider_transaction_id,
            "amount": format(body.amount, "f"),
            "currency": deposit.currency,
            "reference": body.reference,
        }
    )
    deposit = service.confirm(charge, deposit_id=deposit.id, actor=_actor(request, staff))
    return {
        "deposit_id": str(deposit.id),
        "status": deposit.status.value,
        "net_amount": format(deposit.net_amount, "f"),
        "currency": deposit.currency,
    }


# --- withdrawals ----------------------------------------------------------


@router.get("/withdrawals/queue", response_model=list[WithdrawalOut])
def withdrawal_queue(db: DbSession, staff: RequireStaff) -> list[WithdrawalOut]:
    return [WithdrawalOut.model_validate(w) for w in WithdrawalService(db).pending_queue()]


class PayoutIn(Schema):
    provider_reference: str = Field(..., min_length=2, max_length=128)


@router.post("/withdrawals/{withdrawal_id}/processing", response_model=WithdrawalOut)
def start_processing(
    db: DbSession, staff: RequireAdmin, request: Request, withdrawal_id: uuid.UUID
) -> WithdrawalOut:
    withdrawal = _withdrawal(db, withdrawal_id)
    return WithdrawalOut.model_validate(
        WithdrawalService(db).mark_processing(withdrawal, _actor(request, staff))
    )


@router.post("/withdrawals/{withdrawal_id}/paid", response_model=WithdrawalOut)
def mark_paid(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    withdrawal_id: uuid.UUID,
    body: PayoutIn,
) -> WithdrawalOut:
    withdrawal = _withdrawal(db, withdrawal_id)
    return WithdrawalOut.model_validate(
        WithdrawalService(db).mark_paid(withdrawal, _actor(request, staff), body.provider_reference)
    )


@router.post("/withdrawals/{withdrawal_id}/reject", response_model=WithdrawalOut)
def reject_withdrawal(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    withdrawal_id: uuid.UUID,
    body: RejectIn,
) -> WithdrawalOut:
    withdrawal = _withdrawal(db, withdrawal_id)
    return WithdrawalOut.model_validate(
        WithdrawalService(db).reject(withdrawal, _actor(request, staff), body.reason)
    )


@router.get("/withdrawals/{withdrawal_id}/destination")
def reveal_destination(
    db: DbSession, staff: RequireAdmin, request: Request, withdrawal_id: uuid.UUID
) -> dict:
    """Decrypt the payout destination for an operator. Always audited (spec §13)."""
    from app.models.money import PayoutMethodRecord

    withdrawal = _withdrawal(db, withdrawal_id)
    record = db.get(PayoutMethodRecord, withdrawal.payout_method_id)
    if record is None:
        raise NotFound("payout method not found")
    destination = WithdrawalService(db).reveal_destination(record, _actor(request, staff))
    return {
        "withdrawal_id": str(withdrawal.id),
        "method": withdrawal.method.value,
        "destination": destination,
        "account_name": record.account_name,
        "bank_name": record.bank_name,
        "net_amount": format(withdrawal.net_amount, "f"),
    }


def _withdrawal(db, withdrawal_id: uuid.UUID) -> Withdrawal:
    withdrawal = db.get(Withdrawal, withdrawal_id)
    if withdrawal is None:
        raise NotFound("withdrawal not found")
    return withdrawal


# --- refunds --------------------------------------------------------------


@router.get("/refunds/queue")
def refund_queue(db: DbSession, staff: RequireStaff) -> dict:
    return {
        "items": [
            {
                "id": str(r.id),
                "advertiser_id": str(r.advertiser_id),
                "campaign_id": str(r.campaign_id) if r.campaign_id else None,
                "requested": format(r.requested_amount, "f"),
                "currency": r.currency,
                "reason": r.reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in RefundService(db).pending_queue()
        ]
    }


@router.post("/refunds/{refund_id}/approve")
def approve_refund(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    refund_id: uuid.UUID,
    amount: str | None = None,
    note: str | None = None,
) -> dict:
    from app.models.money import Refund

    refund = db.get(Refund, refund_id)
    if refund is None:
        raise NotFound("refund not found")
    refund = RefundService(db).approve(refund, _actor(request, staff), amount=amount, note=note)
    return {
        "refund_id": str(refund.id),
        "status": refund.status.value,
        "approved": format(refund.approved_amount, "f"),
    }


@router.post("/refunds/{refund_id}/reject")
def reject_refund(
    db: DbSession,
    staff: RequireAdmin,
    request: Request,
    refund_id: uuid.UUID,
    body: RejectIn,
) -> dict:
    from app.models.money import Refund

    refund = db.get(Refund, refund_id)
    if refund is None:
        raise NotFound("refund not found")
    refund = RefundService(db).reject(refund, _actor(request, staff), body.reason)
    return {"refund_id": str(refund.id), "status": refund.status.value}


# --- manual balance adjustment (spec §1: with audit logs) ----------------


class AdjustmentIn(Schema):
    party: str = Field(..., pattern="^(advertiser|publisher)$")
    party_id: uuid.UUID
    amount: str = Field(..., description="Signed amount, e.g. '500' or '-500'")
    reason: str = Field(..., min_length=10, max_length=500)


@router.post("/adjustments")
def manual_adjustment(
    db: DbSession, staff: RequireAdmin, request: Request, body: AdjustmentIn
) -> dict:
    """Adjust a balance by hand. Always double-entry, always audited (spec §34).

    A reason of at least 10 characters is required: an unexplained manual
    adjustment is exactly what the audit trail exists to prevent.
    """
    from decimal import Decimal

    from app.core.money import D, q
    from app.models.enums import AccountKind, TransactionType
    from app.services.ledger import credit, debit
    from app.services.wallet import WalletService

    amount = q(D(body.amount))
    if amount == Decimal(0):
        raise ValidationFailed("a zero adjustment changes nothing")

    wallets = WalletService(db)
    ledger = LedgerService(db)
    key = f"manual-adjust:{uuid.uuid4()}"
    magnitude = abs(amount)

    if body.party == "advertiser":
        if db.get(Advertiser, body.party_id) is None:
            raise NotFound("advertiser not found")
        wallet = wallets.locked(wallets.for_advertiser(body.party_id).id)
        account = AccountKind.ADVERTISER_AVAILABLE
    else:
        if db.get(Publisher, body.party_id) is None:
            raise NotFound("publisher not found")
        wallet = wallets.locked(wallets.for_publisher(body.party_id).id)
        account = AccountKind.PUBLISHER_CONFIRMED

    # A credit to the user is funded from (or returned to) the platform's cash
    # position, so the books stay balanced either way.
    legs = (
        [debit(AccountKind.GATEWAY_CLEARING, magnitude), credit(account, magnitude, body.party_id)]
        if amount > 0
        else [
            debit(account, magnitude, body.party_id),
            credit(AccountKind.GATEWAY_CLEARING, magnitude),
        ]
    )
    result = ledger.post(
        transaction_type=TransactionType.MANUAL_ADJUSTMENT,
        currency=wallet.currency,
        legs=legs,
        idempotency_key=key,
        description=f"Manual adjustment: {body.reason}",
        advertiser_id=body.party_id if body.party == "advertiser" else None,
        publisher_id=body.party_id if body.party == "publisher" else None,
        actor_type="staff",
        actor_id=str(staff.id),
    )
    if body.party == "advertiser":
        old = q(wallet.available_balance)
        wallet.available_balance = q(D(wallet.available_balance) + amount)
        new = q(wallet.available_balance)
    else:
        old = q(wallet.confirmed_balance)
        wallet.confirmed_balance = q(D(wallet.confirmed_balance) + amount)
        new = q(wallet.confirmed_balance)
    wallet.version += 1
    db.flush()

    AuditService(db).financial(
        _actor(request, staff),
        "balance.adjusted",
        target_type=body.party,
        target_id=body.party_id,
        ledger_transaction_id=result.id,
        old_value={"balance": str(old)},
        new_value={"balance": str(new)},
        reason=body.reason,
    )
    return {
        "party": body.party,
        "party_id": str(body.party_id),
        "old_balance": format(old, "f"),
        "new_balance": format(new, "f"),
        "ledger_transaction_id": str(result.id),
    }


# --- fraud ----------------------------------------------------------------


@router.get("/fraud/events")
def fraud_events(
    db: DbSession,
    staff: RequireStaff,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = db.scalars(
        select(FraudEvent).order_by(FraudEvent.occurred_at.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "items": [
            {
                "id": str(e.id),
                "subject_type": e.subject_type.value,
                "subject_id": str(e.subject_id) if e.subject_id else None,
                "signal": e.signal,
                "score": e.score,
                "band": e.band.value,
                "amount_at_risk": format(e.amount_at_risk, "f"),
                "evidence": e.evidence,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for e in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/fraud/cases")
def fraud_cases(db: DbSession, staff: RequireStaff) -> dict:
    rows = db.scalars(select(FraudCase).order_by(FraudCase.created_at.desc()).limit(100)).all()
    return {
        "items": [
            {
                "id": str(c.id),
                "subject_type": c.subject_type.value,
                "subject_id": str(c.subject_id),
                "status": c.status.value,
                "score": c.score,
                "band": c.band.value,
                "summary": c.summary,
                "amount_held": format(c.amount_held, "f"),
                "evidence": c.evidence,
            }
            for c in rows
        ]
    }


@router.post("/fraud/channels/{channel_id}/audit")
def audit_channel(db: DbSession, staff: RequireStaff, channel_id: uuid.UUID) -> dict:
    channel = db.get(PublisherChannel, channel_id)
    if channel is None:
        raise NotFound("channel not found")
    assessment = FraudService(db).audit_channel(channel)
    return {
        "score": assessment.score,
        "band": assessment.band.value,
        "evidence": assessment.evidence,
    }


# --- pricing and settings -------------------------------------------------


@router.get("/settings")
def get_settings(db: DbSession, staff: RequireAdmin) -> dict:
    return SettingsService(db).all_by_category()


class SettingIn(Schema):
    value: str = Field(..., max_length=2000)


@router.put("/settings/{key}", response_model=Acknowledged)
def set_setting(
    db: DbSession, staff: RequireAdmin, request: Request, key: str, body: SettingIn
) -> Acknowledged:
    old, new = SettingsService(db).set(key, body.value, staff_id=staff.id)
    AuditService(db).log(
        _actor(request, staff),
        "setting.changed",
        target_type="system_setting",
        target_id=key,
        old_value={"value": old},
        new_value={"value": new},
    )
    return Acknowledged(message=f"{key} set to {new}")


class PricingRuleIn(Schema):
    scope: PricingRuleScope
    scope_value: str = Field(..., min_length=1, max_length=48)
    multiplier: str = "1.0"
    commission_rate_override: str | None = None
    min_cpm: str | None = None
    max_cpm: str | None = None
    currency: str | None = Field(None, min_length=3, max_length=3)
    note: str | None = Field(None, max_length=300)


@router.get("/pricing/rules")
def list_pricing_rules(db: DbSession, staff: RequireAdmin) -> dict:
    rows = db.scalars(
        select(PricingRule).order_by(PricingRule.scope, PricingRule.scope_value)
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "scope": r.scope.value,
                "scope_value": r.scope_value,
                "multiplier": str(r.multiplier),
                "commission_rate_override": str(r.commission_rate_override)
                if r.commission_rate_override is not None
                else None,
                "min_cpm": format(r.min_cpm, "f") if r.min_cpm is not None else None,
                "max_cpm": format(r.max_cpm, "f") if r.max_cpm is not None else None,
                "currency": r.currency,
                "active": r.active,
                "note": r.note,
            }
            for r in rows
        ]
    }


@router.put("/pricing/rules", response_model=Acknowledged)
def upsert_pricing_rule(
    db: DbSession, staff: RequireAdmin, request: Request, body: PricingRuleIn
) -> Acknowledged:
    rule = PricingService(db).upsert_rule(
        body.scope,
        body.scope_value,
        multiplier=body.multiplier,
        commission_rate_override=body.commission_rate_override,
        min_cpm=body.min_cpm,
        max_cpm=body.max_cpm,
        currency=body.currency,
        note=body.note,
        staff_id=staff.id,
    )
    AuditService(db).log(
        _actor(request, staff),
        "pricing_rule.upserted",
        target_type="pricing_rule",
        target_id=rule.id,
        new_value={
            "scope": body.scope.value,
            "scope_value": body.scope_value,
            "multiplier": body.multiplier,
        },
    )
    return Acknowledged(message="pricing rule saved")


@router.delete("/pricing/rules/{rule_id}", response_model=Acknowledged)
def deactivate_pricing_rule(
    db: DbSession, staff: RequireAdmin, request: Request, rule_id: uuid.UUID
) -> Acknowledged:
    rule = db.get(PricingRule, rule_id)
    if rule is None:
        raise NotFound("pricing rule not found")
    # Deactivated, never deleted: the price applied to a past delivery must stay
    # reconstructible.
    rule.active = False
    db.flush()
    AuditService(db).log(
        _actor(request, staff),
        "pricing_rule.deactivated",
        target_type="pricing_rule",
        target_id=rule.id,
        old_value={"active": True},
        new_value={"active": False},
    )
    return Acknowledged(message="pricing rule deactivated")


# --- audit and reports ----------------------------------------------------


@router.get("/audit")
def audit_log(
    db: DbSession,
    staff: RequireAdmin,
    financial_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = AuditService(db).recent(limit, offset, financial_only)
    return {
        "items": [
            {
                "id": str(a.id),
                "actor_type": a.actor_type,
                "actor": a.actor_label,
                "action": a.action,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "old_value": a.old_value,
                "new_value": a.new_value,
                "reason": a.reason,
                "is_financial": a.is_financial,
                "ledger_transaction_id": str(a.ledger_transaction_id)
                if a.ledger_transaction_id
                else None,
                "created_at": a.created_at.isoformat(),
            }
            for a in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/reports/financial.csv", response_class=Response)
def financial_report(
    db: DbSession, staff: RequireAdmin, days: int = Query(30, ge=1, le=365)
) -> Response:
    return Response(
        content=AnalyticsService(db).financial_report_csv(days),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=financial-report.csv"},
    )


@router.get("/snapshots")
def snapshots(db: DbSession, staff: RequireStaff, days: int = Query(30, ge=1, le=365)) -> dict:
    return {
        "items": [
            {
                "date": str(s.snapshot_date),
                "gross_ad_spend": format(s.gross_ad_spend, "f"),
                "publisher_payout": format(s.publisher_payout, "f"),
                "platform_revenue": format(s.platform_revenue, "f"),
                "refunds": format(s.refunds, "f"),
                "withdrawals_paid": format(s.withdrawals_paid, "f"),
                "billable_impressions": s.billable_impressions,
                "clicks": s.clicks,
            }
            for s in AnalyticsService(db).snapshots(days)
        ]
    }
