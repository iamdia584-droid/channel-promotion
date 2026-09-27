"""HTTP ``Idempotency-Key`` support for mutating endpoints (spec §26)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.db.base import utcnow
from app.models.ops import IdempotencyRecord

TTL_HOURS = 24


def fingerprint(payload: object) -> str:
    """Stable hash of the request body, so the same key with a different body is
    caught rather than silently returning the wrong cached response."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:64]


def lookup(session: Session, scope: str, key: str, payload: object) -> dict | None:
    row = session.scalars(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope, IdempotencyRecord.key == key
        )
    ).one_or_none()
    if row is None:
        return None
    if row.expires_at <= utcnow():
        session.delete(row)
        session.flush()
        return None
    if row.request_fingerprint != fingerprint(payload):
        raise Conflict(
            "this Idempotency-Key was already used with a different request body",
            scope=scope,
        )
    return {"status_code": row.status_code, "body": row.response_body}


def store(
    session: Session, scope: str, key: str, payload: object, status_code: int, body: dict
) -> None:
    row = IdempotencyRecord(
        key=key,
        scope=scope,
        request_fingerprint=fingerprint(payload),
        status_code=status_code,
        response_body=body,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=TTL_HOURS),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        pass  # a concurrent request stored it first; its response stands


def purge_expired(session: Session) -> int:
    rows = session.scalars(
        select(IdempotencyRecord).where(IdempotencyRecord.expires_at <= utcnow())
    ).all()
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)
