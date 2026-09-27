"""Domain events (spec §30).

Events are persisted to ``event_log`` in the same transaction as the change that
caused them — a transactional outbox — then dispatched to workers. That ordering
means a crash between "commit" and "notify" loses a notification but never a
financial fact, and the outbox can be replayed.

Event names are exactly those listed in spec §30.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.ops import EventLog

log = get_logger(__name__)


class Event:
    CAMPAIGN_CREATED = "campaign.created"
    CAMPAIGN_APPROVED = "campaign.approved"
    CAMPAIGN_REJECTED = "campaign.rejected"
    CAMPAIGN_STARTED = "campaign.started"
    CAMPAIGN_PAUSED = "campaign.paused"
    CAMPAIGN_RESUMED = "campaign.resumed"
    CAMPAIGN_COMPLETED = "campaign.completed"
    CAMPAIGN_CANCELLED = "campaign.cancelled"
    CAMPAIGN_LOW_BALANCE = "campaign.low_balance"

    AD_SUBMITTED = "ad.submitted"
    AD_APPROVED = "ad.approved"
    AD_REJECTED = "ad.rejected"
    AD_DELIVERED = "ad.delivered"

    IMPRESSION_RECORDED = "impression.recorded"
    CLICK_RECORDED = "click.recorded"

    CHANNEL_REGISTERED = "channel.registered"
    CHANNEL_APPROVED = "channel.approved"
    CHANNEL_SUSPENDED = "channel.suspended"

    EARNING_CREATED = "earning.created"
    EARNING_CONFIRMED = "earning.confirmed"
    EARNING_REVERSED = "earning.reversed"

    DEPOSIT_CONFIRMED = "deposit.confirmed"
    REFUND_PROCESSED = "refund.processed"

    WITHDRAWAL_CREATED = "withdrawal.created"
    WITHDRAWAL_PAID = "withdrawal.paid"
    WITHDRAWAL_REJECTED = "withdrawal.rejected"

    FRAUD_DETECTED = "fraud.detected"

    ALL = None  # populated below


Event.ALL = frozenset(
    value for key, value in vars(Event).items()
    if not key.startswith("_") and isinstance(value, str)
)

#: In-process subscribers, used by tests and by the worker entrypoints.
_handlers: dict[str, list[Callable[[dict], None]]] = {}


def subscribe(name: str, handler: Callable[[dict], None]) -> None:
    _handlers.setdefault(name, []).append(handler)


def clear_subscribers() -> None:
    _handlers.clear()


def emit(
    session: Session,
    name: str,
    payload: dict[str, Any] | None = None,
    *,
    aggregate_type: str | None = None,
    aggregate_id: Any = None,
) -> EventLog:
    """Record an event. Never raises into the caller's transaction."""
    if name not in Event.ALL:
        log.warning("unknown_event_name", name=name)
    row = EventLog(
        name=name,
        payload=_jsonable(payload or {}),
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id) if aggregate_id is not None else None,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()

    for handler in _handlers.get(name, []):
        try:
            handler(row.payload)
        except Exception as exc:  # a subscriber must not break the business change
            log.error("event_handler_failed", name=name, error=str(exc))
    return row


def pending(session: Session, limit: int = 500) -> list[EventLog]:
    return list(
        session.scalars(
            select(EventLog)
            .where(EventLog.dispatched.is_(False))
            .order_by(EventLog.created_at)
            .limit(limit)
        ).all()
    )


def mark_dispatched(session: Session, event: EventLog, error: str | None = None) -> None:
    event.attempts += 1
    if error:
        event.last_error = error[:500]
    else:
        event.dispatched = True
        event.dispatched_at = utcnow()
    session.flush()


def _jsonable(payload: dict) -> dict:
    """Decimals and UUIDs become strings; never a float in an event payload."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, bool)) or value is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = _jsonable(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [v if isinstance(v, (str, int, bool)) or v is None else str(v)
                        for v in value]
        else:
            out[key] = str(value)
    return out
