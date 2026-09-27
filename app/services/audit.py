"""Audit logging (spec §34).

An admin must not be able to change a financial record silently. Every financial
adjustment writes both a ledger transaction and an audit row carrying
``old_value``/``new_value`` and the actor, and the two reference each other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.ops import AuditLog


@dataclass(frozen=True)
class Actor:
    """Who did it. ``system`` for scheduled work, ``staff`` for a dashboard action."""

    type: str
    id: str | None = None
    label: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def system(cls, label: str = "scheduler") -> Actor:
        return cls("system", None, label)

    @classmethod
    def staff(cls, staff, ip=None, user_agent=None, request_id=None) -> Actor:
        return cls("staff", str(staff.id), staff.email, ip, user_agent, request_id)

    @classmethod
    def user(cls, user) -> Actor:
        return cls("user", str(user.id), user.display_name)


class AuditService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log(
        self,
        actor: Actor,
        action: str,
        *,
        target_type: str | None = None,
        target_id: Any = None,
        old_value: dict | None = None,
        new_value: dict | None = None,
        reason: str | None = None,
        is_financial: bool = False,
        ledger_transaction_id: uuid.UUID | None = None,
    ) -> AuditLog:
        row = AuditLog(
            actor_type=actor.type,
            actor_id=actor.id,
            actor_label=actor.label,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            old_value=_clean(old_value),
            new_value=_clean(new_value),
            reason=reason[:500] if reason else None,
            ip_address=actor.ip,
            user_agent=(actor.user_agent or "")[:300] or None,
            request_id=actor.request_id,
            is_financial=is_financial,
            ledger_transaction_id=ledger_transaction_id,
            created_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def financial(
        self,
        actor: Actor,
        action: str,
        *,
        target_type: str,
        target_id: Any,
        ledger_transaction_id: uuid.UUID,
        old_value: dict | None = None,
        new_value: dict | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        """A financial action must always name the ledger transaction it caused."""
        return self.log(
            actor,
            action,
            target_type=target_type,
            target_id=target_id,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            is_financial=True,
            ledger_transaction_id=ledger_transaction_id,
        )

    def recent(
        self, limit: int = 100, offset: int = 0, financial_only: bool = False
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
        if financial_only:
            stmt = stmt.where(AuditLog.is_financial.is_(True))
        return list(self.session.scalars(stmt.limit(limit).offset(offset)).all())

    def for_target(self, target_type: str, target_id: Any) -> list[AuditLog]:
        return list(
            self.session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.target_type == target_type,
                    AuditLog.target_id == str(target_id),
                )
                .order_by(AuditLog.created_at.desc())
            ).all()
        )


def _clean(value: dict | None) -> dict:
    """JSON-safe: Decimals and UUIDs become strings, never floats."""
    if not value:
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (dict, list, str, int, bool)) or item is None:
            out[key] = item
        else:
            out[key] = str(item)
    return out
