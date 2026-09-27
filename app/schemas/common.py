"""Shared Pydantic schemas. Money is always a string in JSON (spec §11).

A JSON number would be parsed as a float by most clients, silently reintroducing
binary rounding error into a financial API. Amounts therefore travel as strings.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from app.core.money import D, q

#: Serialise every Decimal as a fixed-point string, never a JSON number.
Money = Annotated[Decimal, PlainSerializer(lambda v: format(q(v), "f"), return_type=str)]

T = TypeVar("T")


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MoneyInput(Schema):
    """Accepts a string or integer amount; rejects a float outright."""

    amount: Decimal = Field(..., gt=0)

    @field_validator("amount", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError(
                "send the amount as a string, not a JSON number: a float cannot "
                "represent money exactly"
            )
        return D(value)


class Page(Schema, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class Acknowledged(Schema):
    ok: bool = True
    message: str | None = None


class ErrorBody(Schema):
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(Schema):
    error: ErrorBody


class WalletOut(Schema):
    currency: str
    available: Money
    reserved: Money
    spent: Money
    deposited: Money
    refunded: Money
    pending: Money
    confirmed: Money
    earned: Money
    withdrawn: Money


class TransactionOut(Schema):
    id: uuid.UUID
    transaction_type: str
    signed_amount: Money
    currency: str
    balance_after: Money
    bucket: str
    description: str | None = None
    created_at: datetime
