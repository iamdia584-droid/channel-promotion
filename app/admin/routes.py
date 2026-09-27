"""Web admin dashboard (spec §23). Server-rendered, CSRF-protected, audited."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.admin.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    authenticate,
    current_staff,
    verify_csrf,
)
from app.core.config import settings
from app.core.errors import AdNetError, Unauthenticated
from app.core.money import fmt, q
from app.db.session import session_scope
from app.models.campaigns import Campaign
from app.models.enums import (
    ChannelStatus,
    PricingRuleScope,
    Role,
)
from app.models.identity import Advertiser, Publisher, User
from app.models.money import Refund, Withdrawal
from app.models.ops import FraudEvent, PricingRule
from app.models.telegram import PublisherChannel
from app.services.analytics import AnalyticsService
from app.services.audit import Actor, AuditService
from app.services.campaigns import CampaignService
from app.services.pricing import PricingService
from app.services.refunds import RefundService
from app.services.settings_service import SettingsService
from app.services.withdrawals import WithdrawalService

router = APIRouter(prefix="/admin", tags=["admin-ui"], include_in_schema=False)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals["money"] = lambda v: fmt(v, settings.default_currency)


def _ctx(request: Request, db, staff, **extra) -> dict:
    payload = {
        "request": request,
        "staff": staff,
        "platform": SettingsService(db).str_("platform_name"),
        "csrf": _csrf(request),
        "flash": request.query_params.get("flash"),
        "flash_kind": request.query_params.get("kind", "ok"),
    }
    payload.update(extra)
    return payload


def _csrf(request: Request) -> str:
    from app.admin.auth import SESSION_SALT
    from app.core.security import unsign

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return ""
    try:
        return unsign(token, SESSION_SALT, settings.admin_session_max_age).get("csrf", "")
    except Unauthenticated:
        return ""


def _redirect(path: str, message: str, kind: str = "ok") -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse(
        f"{path}?{urlencode({'flash': message, 'kind': kind})}", status_code=303
    )


def _actor(request: Request, staff) -> Actor:
    return Actor.staff(
        staff,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    with session_scope() as db:
        platform = SettingsService(db).str_("platform_name")
    return TEMPLATES.TemplateResponse(request, "login.html", {"platform": platform, "error": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    totp: str = Form(""),
):
    with session_scope() as db:
        platform = SettingsService(db).str_("platform_name")
        try:
            result = authenticate(db, email, password, totp or None)
        except AdNetError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"platform": platform, "error": exc.message},
                status_code=401,
            )
        AuditService(db).log(
            _actor(request, result.staff),
            "staff.signed_in",
            target_type="staff",
            target_id=result.staff.id,
        )
        token, csrf = result.token, result.csrf

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.admin_session_max_age,
    )
    # Readable by the page so forms can echo it back — that is the point of a
    # double-submit CSRF token.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.admin_session_max_age,
    )
    return response


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return response


def _guard(request: Request, db):
    """Return the signed-in staff member, or raise to send them to /admin/login."""
    return current_staff(request, db)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        analytics = AnalyticsService(db)
        overview = analytics.platform_overview()
        overview["trial_balance"] = {
            k: format(v, "f") for k, v in overview["trial_balance"].items()
        }
        snapshots = analytics.snapshots(14)
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(request, db, staff, active="dashboard", o=overview, snapshots=snapshots),
        )


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, status: str | None = None):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        stmt = select(Campaign).order_by(Campaign.created_at.desc()).limit(100)
        if status:
            stmt = stmt.where(Campaign.status == status)
        campaigns = db.scalars(stmt).all()
        rows = []
        for c in campaigns:
            ad = c.ads[0] if c.ads else None
            rows.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "status": c.status.value,
                    "status_kind": _status_kind(c.status.value),
                    "budget": fmt(c.total_budget, c.currency),
                    "spent": fmt(c.spent_amount, c.currency),
                    "cpm": fmt(c.bid_cpm, c.currency),
                    "impressions": f"{c.billable_impressions:,}",
                    "clicks": f"{c.clicks:,}",
                    "creative": ((ad.body_text or "(media)")[:60] if ad else "—"),
                    "link": (ad.destination_url or "—") if ad else "—",
                    "pending": c.status.value in {"submitted", "under_review"},
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="campaigns",
                heading="Campaigns",
                intro="Approving a campaign reserves its full budget from the "
                "advertiser's available balance.",
                columns=[
                    {"key": "name", "label": "Campaign"},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "budget", "label": "Budget", "num": True},
                    {"key": "spent", "label": "Spent", "num": True},
                    {"key": "cpm", "label": "Bid CPM", "num": True},
                    {"key": "impressions", "label": "Impressions", "num": True},
                    {"key": "clicks", "label": "Clicks", "num": True},
                    {"key": "creative", "label": "Creative"},
                ],
                actions=[
                    {
                        "label": "Approve",
                        "url": "/admin/campaigns/{id}/approve",
                        "style": "",
                        "when": "pending",
                    },
                    {
                        "label": "Reject",
                        "url": "/admin/campaigns/{id}/reject",
                        "style": "danger",
                        "when": "pending",
                        "reason": "reason",
                    },
                ],
                rows=rows,
            ),
        )


@router.post("/campaigns/{campaign_id}/approve")
def approve_campaign(request: Request, campaign_id: uuid.UUID, csrf: str = Form("")):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return _redirect("/admin/campaigns", "Campaign not found", "err")
        try:
            CampaignService(db).approve(campaign, _actor(request, staff))
        except AdNetError as exc:
            return _redirect("/admin/campaigns", exc.message, "err")
        return _redirect("/admin/campaigns", f"Approved “{campaign.name}”")


@router.post("/campaigns/{campaign_id}/reject")
def reject_campaign(
    request: Request, campaign_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return _redirect("/admin/campaigns", "Campaign not found", "err")
        try:
            CampaignService(db).reject(campaign, _actor(request, staff), reason)
        except AdNetError as exc:
            return _redirect("/admin/campaigns", exc.message, "err")
        return _redirect("/admin/campaigns", f"Rejected “{campaign.name}”")


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


@router.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        channels = db.scalars(
            select(PublisherChannel).order_by(PublisherChannel.created_at.desc()).limit(100)
        ).all()
        rows = []
        for c in channels:
            members = c.chat.member_count if c.chat else 0
            rows.append(
                {
                    "id": c.id,
                    "title": (c.chat.title if c.chat else None) or str(c.telegram_chat_id),
                    "username": ("@" + c.chat.username) if c.chat and c.chat.username else "—",
                    "status": c.status.value,
                    "status_kind": _status_kind(c.status.value),
                    "verification": c.verification_status.value,
                    "members": f"{members:,}",
                    "avg_views": f"{c.avg_views:,}",
                    # The ratio that matters: members alone are not reach (spec §5).
                    "view_ratio": f"{(c.avg_views / members * 100):.1f}%" if members else "—",
                    "quality": f"{q(c.quality_score):.2f}",
                    "fraud": c.fraud_score,
                    "earned": fmt(c.lifetime_earned, settings.default_currency),
                    "pending": c.status.value in {"pending", "verified"},
                    "live": c.status.value in {"active", "verified"},
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="channels",
                heading="Channels",
                intro="Views per member is the signal that matters — a large "
                "member count with few views is not reach.",
                columns=[
                    {"key": "title", "label": "Channel"},
                    {"key": "username", "label": "Username", "mono": True},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "verification", "label": "Verified"},
                    {"key": "members", "label": "Members", "num": True},
                    {"key": "avg_views", "label": "Avg views", "num": True},
                    {"key": "view_ratio", "label": "Views/member", "num": True},
                    {"key": "quality", "label": "Quality", "num": True},
                    {"key": "fraud", "label": "Fraud", "num": True},
                    {"key": "earned", "label": "Earned", "num": True},
                ],
                actions=[
                    {
                        "label": "Activate",
                        "url": "/admin/channels/{id}/activate",
                        "style": "",
                        "when": "pending",
                    },
                    {
                        "label": "Suspend",
                        "url": "/admin/channels/{id}/suspend",
                        "style": "danger",
                        "when": "live",
                        "reason": "reason",
                    },
                ],
                rows=rows,
            ),
        )


@router.post("/channels/{channel_id}/activate")
def activate_channel(request: Request, channel_id: uuid.UUID, csrf: str = Form("")):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        channel = db.get(PublisherChannel, channel_id)
        if channel is None:
            return _redirect("/admin/channels", "Channel not found", "err")
        old = channel.status
        channel.status = ChannelStatus.ACTIVE
        db.flush()
        AuditService(db).log(
            _actor(request, staff),
            "channel.activated",
            target_type="channel",
            target_id=channel.id,
            old_value={"status": str(old)},
            new_value={"status": "active"},
        )
        return _redirect("/admin/channels", "Channel activated")


@router.post("/channels/{channel_id}/suspend")
def suspend_channel(
    request: Request, channel_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        channel = db.get(PublisherChannel, channel_id)
        if channel is None:
            return _redirect("/admin/channels", "Channel not found", "err")
        old = channel.status
        channel.status = ChannelStatus.SUSPENDED
        channel.rejection_reason = reason[:500]
        db.flush()
        AuditService(db).log(
            _actor(request, staff),
            "channel.suspended",
            target_type="channel",
            target_id=channel.id,
            old_value={"status": str(old)},
            new_value={"status": "suspended"},
            reason=reason,
        )
        from app.services.notifications import NotificationService

        NotificationService(db).queue_for_publisher(
            channel.publisher_id,
            "channel_suspended",
            {
                "channel_title": (channel.chat.title if channel.chat else "your channel"),
                "reason": reason,
            },
        )
        return _redirect("/admin/channels", "Channel suspended")


# --------------------------------------------------------------------------
# Withdrawals
# --------------------------------------------------------------------------


@router.get("/withdrawals", response_class=HTMLResponse)
def withdrawals_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        rows = []
        for w in db.scalars(
            select(Withdrawal).order_by(Withdrawal.created_at.desc()).limit(100)
        ).all():
            rows.append(
                {
                    "id": w.id,
                    "created": w.created_at.strftime("%d %b %H:%M"),
                    "amount": fmt(w.amount, w.currency),
                    "fee": fmt(w.fee, w.currency),
                    "net": fmt(w.net_amount, w.currency),
                    "method": w.method.value,
                    "destination": w.destination_masked,
                    "status": w.status.value,
                    "status_kind": _status_kind(w.status.value),
                    "fraud": f"{w.fraud_score}{' 🚩' if w.fraud_hold else ''}",
                    "actionable": not w.status.is_terminal,
                    "detail_url": f"/admin/withdrawals/{w.id}",
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="withdrawals",
                heading="Withdrawals",
                intro="Rejecting returns the fee as well as the amount. Open the "
                "details page to reveal the payout destination — that reveal "
                "is audited.",
                columns=[
                    {"key": "created", "label": "Requested"},
                    {"key": "amount", "label": "Amount", "num": True},
                    {"key": "fee", "label": "Fee", "num": True},
                    {"key": "net", "label": "Net", "num": True},
                    {"key": "method", "label": "Method"},
                    {"key": "destination", "label": "Destination", "mono": True},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "fraud", "label": "Fraud", "num": True},
                ],
                actions=[
                    {
                        "label": "Mark paid",
                        "url": "/admin/withdrawals/{id}/paid",
                        "style": "",
                        "when": "actionable",
                        "field": "provider_reference",
                        "placeholder": "provider ref",
                    },
                    {
                        "label": "Reject",
                        "url": "/admin/withdrawals/{id}/reject",
                        "style": "danger",
                        "when": "actionable",
                        "reason": "reason",
                    },
                ],
                rows=rows,
            ),
        )


@router.get("/withdrawals/{withdrawal_id}", response_class=HTMLResponse)
def withdrawal_detail(request: Request, withdrawal_id: uuid.UUID):
    """Reveals the full payout destination. Admin only, and audited (spec §13)."""
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        withdrawal = db.get(Withdrawal, withdrawal_id)
        if withdrawal is None:
            return _redirect("/admin/withdrawals", "Withdrawal not found", "err")
        from app.models.money import PayoutMethodRecord

        record = db.get(PayoutMethodRecord, withdrawal.payout_method_id)
        destination = "—"
        if record is not None:
            destination = WithdrawalService(db).reveal_destination(record, _actor(request, staff))
        rows = [
            {
                "id": withdrawal.id,
                "field": "Destination",
                "value": destination,
            },
            {
                "id": withdrawal.id,
                "field": "Account name",
                "value": (record.account_name if record else None) or "—",
            },
            {
                "id": withdrawal.id,
                "field": "Bank",
                "value": (record.bank_name if record else None) or "—",
            },
            {
                "id": withdrawal.id,
                "field": "Net to send",
                "value": fmt(withdrawal.net_amount, withdrawal.currency),
            },
            {
                "id": withdrawal.id,
                "field": "Status",
                "value": withdrawal.status.value,
            },
            {
                "id": withdrawal.id,
                "field": "Fraud score",
                "value": f"{withdrawal.fraud_score}{' — on hold' if withdrawal.fraud_hold else ''}",
            },
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="withdrawals",
                heading="Withdrawal details",
                intro="This view decrypted the payout destination. That has been "
                "recorded in the audit log against your account.",
                columns=[
                    {"key": "field", "label": "Field"},
                    {"key": "value", "label": "Value", "mono": True},
                ],
                actions=None,
                rows=rows,
            ),
        )


@router.post("/withdrawals/{withdrawal_id}/paid")
def pay_withdrawal(
    request: Request,
    withdrawal_id: uuid.UUID,
    provider_reference: str = Form(...),
    csrf: str = Form(""),
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        withdrawal = db.get(Withdrawal, withdrawal_id)
        if withdrawal is None:
            return _redirect("/admin/withdrawals", "Withdrawal not found", "err")
        try:
            WithdrawalService(db).mark_paid(withdrawal, _actor(request, staff), provider_reference)
        except AdNetError as exc:
            return _redirect("/admin/withdrawals", exc.message, "err")
        return _redirect("/admin/withdrawals", "Withdrawal marked paid")


@router.post("/withdrawals/{withdrawal_id}/reject")
def reject_withdrawal(
    request: Request, withdrawal_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        withdrawal = db.get(Withdrawal, withdrawal_id)
        if withdrawal is None:
            return _redirect("/admin/withdrawals", "Withdrawal not found", "err")
        try:
            WithdrawalService(db).reject(withdrawal, _actor(request, staff), reason)
        except AdNetError as exc:
            return _redirect("/admin/withdrawals", exc.message, "err")
        return _redirect("/admin/withdrawals", "Withdrawal rejected and funds returned")


# --------------------------------------------------------------------------
# Refunds, fraud, audit, ledger
# --------------------------------------------------------------------------


@router.get("/refunds", response_class=HTMLResponse)
def refunds_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        rows = [
            {
                "id": r.id,
                "created": r.created_at.strftime("%d %b %H:%M"),
                "requested": fmt(r.requested_amount, r.currency),
                "approved": fmt(r.approved_amount, r.currency),
                "status": r.status.value,
                "status_kind": _status_kind(r.status.value),
                "reason": (r.reason or "—")[:60],
                "actionable": r.status.value == "requested",
            }
            for r in db.scalars(select(Refund).order_by(Refund.created_at.desc()).limit(100)).all()
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="refunds",
                heading="Refunds",
                intro="Only the unspent reservation is refundable. Deliveries still "
                "being measured are excluded — the publisher has already "
                "earned that inventory.",
                columns=[
                    {"key": "created", "label": "Requested"},
                    {"key": "requested", "label": "Amount", "num": True},
                    {"key": "approved", "label": "Approved", "num": True},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "reason", "label": "Reason"},
                ],
                actions=[
                    {
                        "label": "Approve",
                        "url": "/admin/refunds/{id}/approve",
                        "style": "",
                        "when": "actionable",
                    },
                    {
                        "label": "Reject",
                        "url": "/admin/refunds/{id}/reject",
                        "style": "danger",
                        "when": "actionable",
                        "reason": "reason",
                    },
                ],
                rows=rows,
            ),
        )


@router.post("/refunds/{refund_id}/approve")
def approve_refund(request: Request, refund_id: uuid.UUID, csrf: str = Form("")):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        refund = db.get(Refund, refund_id)
        if refund is None:
            return _redirect("/admin/refunds", "Refund not found", "err")
        try:
            RefundService(db).approve(refund, _actor(request, staff))
        except AdNetError as exc:
            return _redirect("/admin/refunds", exc.message, "err")
        return _redirect("/admin/refunds", "Refund approved")


@router.post("/refunds/{refund_id}/reject")
def reject_refund_ui(
    request: Request, refund_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        refund = db.get(Refund, refund_id)
        if refund is None:
            return _redirect("/admin/refunds", "Refund not found", "err")
        try:
            RefundService(db).reject(refund, _actor(request, staff), reason)
        except AdNetError as exc:
            return _redirect("/admin/refunds", exc.message, "err")
        return _redirect("/admin/refunds", "Refund rejected")


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        from app.services.moderation import ModerationService

        service = ModerationService(db)
        rows = []
        for r in service.all_reports(100):
            rows.append(
                {
                    "id": r.id,
                    "when": r.created_at.strftime("%d %b %H:%M"),
                    "target": r.target_type.value,
                    "target_id": str(r.target_id)[:8],
                    "reason": r.reason.value,
                    "reason_kind": "bad"
                    if r.reason.value in {"scam", "malware", "adult", "illegal", "impersonation"}
                    else "warn",
                    "details": (r.details or "—")[:70],
                    "status": r.status.value,
                    "status_kind": _status_kind(r.status.value),
                    "resolution": (r.resolution or "—")[:50],
                    "actionable": r.status.value in {"open", "reviewing"},
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="reports",
                heading="Reports",
                intro="A report is a signal from a person, never acted on "
                "automatically. Upholding a severe reason suspends the "
                "campaign or channel immediately.",
                columns=[
                    {"key": "when", "label": "Filed"},
                    {"key": "target", "label": "Target"},
                    {"key": "target_id", "label": "ID", "mono": True},
                    {"key": "reason", "label": "Reason", "pill": True},
                    {"key": "details", "label": "Details"},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "resolution", "label": "Resolution"},
                ],
                actions=[
                    {
                        "label": "Uphold",
                        "url": "/admin/reports/{id}/uphold",
                        "style": "danger",
                        "when": "actionable",
                        "reason": "resolution",
                    },
                    {
                        "label": "Dismiss",
                        "url": "/admin/reports/{id}/dismiss",
                        "style": "ghost",
                        "when": "actionable",
                        "reason": "resolution",
                    },
                ],
                rows=rows,
            ),
        )


@router.post("/reports/{report_id}/uphold")
def uphold_report_ui(
    request: Request, report_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.models.ops import Report
        from app.services.moderation import ModerationService

        report = db.get(Report, report_id)
        if report is None:
            return _redirect("/admin/reports", "Report not found", "err")
        try:
            outcome = ModerationService(db).uphold(report, _actor(request, staff), reason)
        except AdNetError as exc:
            return _redirect("/admin/reports", exc.message, "err")
        note = "Report upheld"
        if outcome.campaign_suspended:
            note += " — campaign suspended"
        if outcome.channel_suspended:
            note += " — channel suspended"
        return _redirect("/admin/reports", note)


@router.post("/reports/{report_id}/dismiss")
def dismiss_report_ui(
    request: Request, report_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.models.ops import Report
        from app.services.moderation import ModerationService

        report = db.get(Report, report_id)
        if report is None:
            return _redirect("/admin/reports", "Report not found", "err")
        try:
            ModerationService(db).dismiss(report, _actor(request, staff), reason)
        except AdNetError as exc:
            return _redirect("/admin/reports", exc.message, "err")
        return _redirect("/admin/reports", "Report dismissed")


@router.get("/fraud", response_class=HTMLResponse)
def fraud_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        import json

        events = db.scalars(
            select(FraudEvent).order_by(FraudEvent.occurred_at.desc()).limit(60)
        ).all()
        rows, evidence = [], []
        for e in events:
            rows.append(
                {
                    "id": e.id,
                    "when": e.occurred_at.strftime("%d %b %H:%M"),
                    "subject": e.subject_type.value,
                    "signal": e.signal,
                    "score": e.score,
                    "band": e.band.value,
                    "band_kind": {
                        "normal": "good",
                        "review": "warn",
                        "suspicious": "warn",
                        "high_risk": "bad",
                    }[e.band.value],
                    "at_risk": fmt(e.amount_at_risk, settings.default_currency),
                    "action": e.action_taken or "—",
                }
            )
            evidence.append(
                {
                    "summary": f"{e.occurred_at:%d %b %H:%M} · {e.subject_type.value} · "
                    f"{e.signal} · score {e.score}",
                    "evidence": json.dumps(e.evidence, indent=2),
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="fraud",
                heading="Fraud",
                intro="A score never bans on its own. Expand the evidence below to "
                "see which signals fired and why.",
                columns=[
                    {"key": "when", "label": "When"},
                    {"key": "subject", "label": "Subject"},
                    {"key": "signal", "label": "Signals"},
                    {"key": "score", "label": "Score", "num": True},
                    {"key": "band", "label": "Band", "pill": True},
                    {"key": "at_risk", "label": "At risk", "num": True},
                    {"key": "action", "label": "Action"},
                ],
                actions=None,
                rows=rows,
                evidence_rows=evidence,
            ),
        )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, financial: int = 0):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        import json

        logs = AuditService(db).recent(200, 0, bool(financial))
        rows, evidence = [], []
        for a in logs:
            rows.append(
                {
                    "id": a.id,
                    "when": a.created_at.strftime("%d %b %H:%M:%S"),
                    "actor": a.actor_label or a.actor_type,
                    "action": a.action,
                    "target": f"{a.target_type or '-'} {(a.target_id or '')[:8]}",
                    "financial": "yes" if a.is_financial else "",
                    "txn": str(a.ledger_transaction_id)[:8] if a.ledger_transaction_id else "—",
                    "reason": (a.reason or "—")[:50],
                }
            )
            if a.old_value or a.new_value:
                evidence.append(
                    {
                        "summary": f"{a.created_at:%d %b %H:%M:%S} · {a.action} · "
                        f"{a.actor_label or a.actor_type}",
                        "evidence": json.dumps(
                            {"old": a.old_value, "new": a.new_value, "reason": a.reason},
                            indent=2,
                        ),
                    }
                )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="audit",
                heading="Audit log",
                intro="Append-only. Every financial action names the ledger "
                "transaction it caused, so a balance cannot be moved silently.",
                columns=[
                    {"key": "when", "label": "When"},
                    {"key": "actor", "label": "Actor"},
                    {"key": "action", "label": "Action"},
                    {"key": "target", "label": "Target", "mono": True},
                    {"key": "financial", "label": "Financial"},
                    {"key": "txn", "label": "Ledger txn", "mono": True},
                    {"key": "reason", "label": "Reason"},
                ],
                actions=None,
                rows=rows,
                evidence_rows=evidence,
            ),
        )


@router.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        from app.models.money import LedgerAccount
        from app.services.ledger import LedgerService

        service = LedgerService(db)
        balance = service.trial_balance(settings.default_currency)
        accounts = db.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.currency == settings.default_currency)
            .order_by(LedgerAccount.kind)
        ).all()
        rows = [
            {
                "id": a.id,
                "kind": a.kind.value,
                "owner": a.owner_type.value,
                "owner_id": str(a.owner_id)[:8] if a.owner_id else "platform",
                "side": a.normal_side.value,
                "balance": fmt(a.balance, a.currency),
            }
            for a in accounts
        ]
        intro = (
            f"Debits {format(balance['debits'], 'f')} · "
            f"credits {format(balance['credits'], 'f')} · "
            f"difference {format(balance['difference'], 'f')}"
            + ("" if balance["difference"] == Decimal(0) else " — NON-ZERO, this is a bug")
        )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="ledger",
                heading="Ledger",
                intro=intro,
                columns=[
                    {"key": "kind", "label": "Account"},
                    {"key": "owner", "label": "Owner type"},
                    {"key": "owner_id", "label": "Owner", "mono": True},
                    {"key": "side", "label": "Normal side"},
                    {"key": "balance", "label": "Balance", "num": True},
                ],
                actions=None,
                rows=rows,
            ),
        )


# --------------------------------------------------------------------------
# Parties
# --------------------------------------------------------------------------


@router.get("/advertisers", response_class=HTMLResponse)
def advertisers_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        rows = []
        for a in db.scalars(
            select(Advertiser).order_by(Advertiser.created_at.desc()).limit(100)
        ).all():
            user = db.get(User, a.user_id)
            wallet = a.wallet
            rows.append(
                {
                    "id": a.id,
                    "name": user.display_name if user else "—",
                    "telegram_id": user.telegram_user_id if user else "—",
                    "status": a.status.value,
                    "status_kind": _status_kind(a.status.value),
                    "deposited": fmt(a.lifetime_deposited, a.currency),
                    "spent": fmt(a.lifetime_spent, a.currency),
                    "available": fmt(wallet.available_balance, a.currency) if wallet else "—",
                    "campaigns": a.campaigns_created,
                    "active": a.status.value == "active",
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="advertisers",
                heading="Advertisers",
                columns=[
                    {"key": "name", "label": "Advertiser"},
                    {"key": "telegram_id", "label": "Telegram ID", "mono": True},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "deposited", "label": "Deposited", "num": True},
                    {"key": "spent", "label": "Spent", "num": True},
                    {"key": "available", "label": "Available", "num": True},
                    {"key": "campaigns", "label": "Campaigns", "num": True},
                ],
                actions=[
                    {
                        "label": "Suspend",
                        "url": "/admin/advertisers/{id}/suspend",
                        "style": "danger",
                        "when": "active",
                        "reason": "reason",
                    }
                ],
                rows=rows,
            ),
        )


@router.get("/publishers", response_class=HTMLResponse)
def publishers_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        rows = []
        for p in db.scalars(
            select(Publisher).order_by(Publisher.created_at.desc()).limit(100)
        ).all():
            user = db.get(User, p.user_id)
            wallet = p.wallet
            rows.append(
                {
                    "id": p.id,
                    "name": user.display_name if user else "—",
                    "telegram_id": user.telegram_user_id if user else "—",
                    "status": p.status.value,
                    "status_kind": _status_kind(p.status.value),
                    "channels": len(p.channels),
                    "earned": fmt(p.lifetime_earned, p.currency),
                    "withdrawn": fmt(p.lifetime_withdrawn, p.currency),
                    "pending": fmt(wallet.pending_balance, p.currency) if wallet else "—",
                    "confirmed": fmt(wallet.confirmed_balance, p.currency) if wallet else "—",
                    "strikes": p.fraud_strikes,
                    "active": p.status.value == "active",
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "table.html",
            _ctx(
                request,
                db,
                staff,
                active="publishers",
                heading="Publishers",
                columns=[
                    {"key": "name", "label": "Publisher"},
                    {"key": "telegram_id", "label": "Telegram ID", "mono": True},
                    {"key": "status", "label": "Status", "pill": True},
                    {"key": "channels", "label": "Channels", "num": True},
                    {"key": "earned", "label": "Earned", "num": True},
                    {"key": "pending", "label": "Pending", "num": True},
                    {"key": "confirmed", "label": "Withdrawable", "num": True},
                    {"key": "withdrawn", "label": "Paid out", "num": True},
                    {"key": "strikes", "label": "Strikes", "num": True},
                ],
                actions=[
                    {
                        "label": "Suspend",
                        "url": "/admin/publishers/{id}/suspend",
                        "style": "danger",
                        "when": "active",
                        "reason": "reason",
                    }
                ],
                rows=rows,
            ),
        )


@router.post("/advertisers/{party_id}/suspend")
def suspend_advertiser_ui(
    request: Request, party_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    return _suspend_party(request, Advertiser, "advertiser", party_id, reason, csrf)


@router.post("/publishers/{party_id}/suspend")
def suspend_publisher_ui(
    request: Request, party_id: uuid.UUID, reason: str = Form(...), csrf: str = Form("")
):
    return _suspend_party(request, Publisher, "publisher", party_id, reason, csrf)


def _suspend_party(request, model, label, party_id, reason, csrf):
    from app.models.enums import UserStatus

    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        row = db.get(model, party_id)
        if row is None:
            return _redirect(f"/admin/{label}s", f"{label} not found", "err")
        old = row.status
        row.status = UserStatus.SUSPENDED
        db.flush()
        AuditService(db).log(
            _actor(request, staff),
            f"{label}.suspended",
            target_type=label,
            target_id=row.id,
            old_value={"status": str(old)},
            new_value={"status": "suspended"},
            reason=reason,
        )
        return _redirect(f"/admin/{label}s", f"{label.capitalize()} suspended")


# --------------------------------------------------------------------------
# Settings and pricing
# --------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                db,
                staff,
                active="settings",
                categories=SettingsService(db).all_by_category(),
            ),
        )


@router.post("/settings")
def update_setting(
    request: Request, key: str = Form(...), value: str = Form(...), csrf: str = Form("")
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        try:
            old, new = SettingsService(db).set(key, value, staff_id=staff.id)
        except AdNetError as exc:
            return _redirect("/admin/settings", exc.message, "err")
        AuditService(db).log(
            _actor(request, staff),
            "setting.changed",
            target_type="system_setting",
            target_id=key,
            old_value={"value": old},
            new_value={"value": new},
        )
        return _redirect("/admin/settings", f"{key} set to {new}")


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    with session_scope() as db:
        try:
            staff = _guard(request, db)
        except AdNetError:
            return RedirectResponse("/admin/login", status_code=303)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        rules = [
            {
                "id": r.id,
                "scope": r.scope.value,
                "scope_value": r.scope_value,
                "multiplier": str(r.multiplier),
                "commission_rate_override": str(r.commission_rate_override)
                if r.commission_rate_override is not None
                else None,
                "min_cpm": format(r.min_cpm, "f") if r.min_cpm is not None else None,
                "max_cpm": format(r.max_cpm, "f") if r.max_cpm is not None else None,
                "active": r.active,
            }
            for r in db.scalars(
                select(PricingRule).order_by(PricingRule.scope, PricingRule.scope_value)
            ).all()
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "pricing.html",
            _ctx(
                request,
                db,
                staff,
                active="pricing",
                rules=rules,
                scopes=[s.value for s in PricingRuleScope],
            ),
        )


@router.post("/pricing")
def save_pricing_rule(
    request: Request,
    scope: str = Form(...),
    scope_value: str = Form(...),
    multiplier: str = Form("1.0"),
    commission_rate_override: str = Form(""),
    min_cpm: str = Form(""),
    max_cpm: str = Form(""),
    csrf: str = Form(""),
):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        try:
            rule = PricingService(db).upsert_rule(
                PricingRuleScope(scope),
                scope_value,
                multiplier=multiplier,
                commission_rate_override=commission_rate_override or None,
                min_cpm=min_cpm or None,
                max_cpm=max_cpm or None,
                staff_id=staff.id,
            )
        except (AdNetError, ValueError, ArithmeticError) as exc:
            return _redirect("/admin/pricing", str(exc), "err")
        AuditService(db).log(
            _actor(request, staff),
            "pricing_rule.upserted",
            target_type="pricing_rule",
            target_id=rule.id,
            new_value={"scope": scope, "scope_value": scope_value, "multiplier": multiplier},
        )
        return _redirect("/admin/pricing", "Pricing rule saved")


@router.post("/pricing/{rule_id}/deactivate")
def deactivate_rule(request: Request, rule_id: uuid.UUID, csrf: str = Form("")):
    with session_scope() as db:
        staff = _guard(request, db)
        verify_csrf(request, csrf)
        from app.admin.auth import require_role

        require_role(staff, Role.ADMIN)
        rule = db.get(PricingRule, rule_id)
        if rule is None:
            return _redirect("/admin/pricing", "Rule not found", "err")
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
        return _redirect("/admin/pricing", "Rule deactivated")


def _status_kind(status: str) -> str:
    return {
        "active": "good",
        "running": "good",
        "approved": "good",
        "verified": "good",
        "paid": "good",
        "confirmed": "good",
        "completed": "good",
        "processed": "good",
        "pending": "warn",
        "submitted": "warn",
        "under_review": "warn",
        "processing": "warn",
        "paused": "warn",
        "requested": "warn",
        "draft": "",
        "rejected": "bad",
        "suspended": "bad",
        "banned": "bad",
        "cancelled": "bad",
        "reversed": "bad",
        "failed": "bad",
    }.get(status, "")
