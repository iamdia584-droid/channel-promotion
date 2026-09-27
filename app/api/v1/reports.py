"""Report filing and moderation endpoints (spec §19)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request
from pydantic import Field

from app.api.deps import CurrentPrincipal, DbSession, RequireStaff, throttle
from app.core.errors import NotFound
from app.models.enums import ReportReason, ReportStatus, ReviewTarget
from app.models.ops import Report
from app.schemas.common import Acknowledged, Schema
from app.services.audit import Actor
from app.services.moderation import ModerationService

router = APIRouter(prefix="/reports", tags=["reports"])


class ReportIn(Schema):
    target_type: ReviewTarget
    target_id: uuid.UUID
    reason: ReportReason
    details: str | None = Field(None, max_length=2000)
    delivery_id: uuid.UUID | None = None


class ReportOut(Schema):
    id: uuid.UUID
    target_type: ReviewTarget
    target_id: uuid.UUID
    reason: ReportReason
    status: ReportStatus
    details: str | None = None
    resolution: str | None = None
    created_at: object
    handled_at: object | None = None


class ResolutionIn(Schema):
    resolution: str = Field(..., min_length=3, max_length=500)


@router.post(
    "",
    response_model=ReportOut,
    status_code=201,
    dependencies=[throttle("report", limit=20, window=3600)],
)
def file_report(db: DbSession, principal: CurrentPrincipal, body: ReportIn) -> ReportOut:
    """Report an ad, campaign or channel. Never acted on automatically (spec §19)."""
    report = ModerationService(db).file_report(
        target_type=body.target_type,
        target_id=body.target_id,
        reason=body.reason,
        details=body.details,
        reporter_user_id=principal.user.id if principal.user else None,
        reporter_telegram_id=(principal.user.telegram_user_id if principal.user else None),
        delivery_id=body.delivery_id,
    )
    return ReportOut.model_validate(report)


@router.get("/queue", response_model=list[ReportOut])
def report_queue(db: DbSession, staff: RequireStaff) -> list[ReportOut]:
    return [ReportOut.model_validate(r) for r in ModerationService(db).open_reports()]


@router.get("", response_model=list[ReportOut])
def list_reports(
    db: DbSession,
    staff: RequireStaff,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[ReportOut]:
    return [ReportOut.model_validate(r) for r in ModerationService(db).all_reports(limit, offset)]


def _report(db, report_id: uuid.UUID) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        raise NotFound("report not found")
    return report


@router.post("/{report_id}/uphold")
def uphold_report(
    db: DbSession,
    staff: RequireStaff,
    request: Request,
    report_id: uuid.UUID,
    body: ResolutionIn,
) -> dict:
    outcome = ModerationService(db).uphold(
        _report(db, report_id), _actor(request, staff), body.resolution
    )
    return {
        "report_id": str(outcome.report.id),
        "status": outcome.report.status.value,
        "campaign_suspended": outcome.campaign_suspended,
        "channel_suspended": outcome.channel_suspended,
    }


@router.post("/{report_id}/dismiss", response_model=Acknowledged)
def dismiss_report(
    db: DbSession,
    staff: RequireStaff,
    request: Request,
    report_id: uuid.UUID,
    body: ResolutionIn,
) -> Acknowledged:
    ModerationService(db).dismiss(_report(db, report_id), _actor(request, staff), body.resolution)
    return Acknowledged(message="report dismissed")


def _actor(request: Request, staff) -> Actor:
    return Actor.staff(
        staff,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
