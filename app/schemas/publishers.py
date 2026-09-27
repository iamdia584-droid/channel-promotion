"""Publisher, channel, earnings and withdrawal schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.models.enums import ChannelStatus, PayoutMethod, WithdrawalStatus
from app.schemas.common import Money, Schema


class ChannelRegisterIn(Schema):
    identifier: str = Field(
        ...,
        min_length=2,
        max_length=300,
        description="@username, a t.me link, or a numeric chat id",
    )
    category: str | None = Field(None, max_length=48)
    language: str | None = Field(None, max_length=8)
    country: str | None = Field(None, min_length=2, max_length=2)


class ChannelOut(Schema):
    id: uuid.UUID
    telegram_chat_id: int
    title: str | None = None
    username: str | None = None
    status: ChannelStatus
    verification_status: str
    category: str | None = None
    language: str | None = None
    country: str | None = None
    members: int = 0
    avg_views: int
    avg_ad_views: int
    total_impressions: int
    total_clicks: int
    total_ads_served: int
    earned: Money
    #: A coarse band only. The internal quality weights and fraud signals are
    #: deliberately not exposed to publishers (spec §17).
    quality_band: str
    auto_advertising: bool
    created_at: datetime


class ChannelSettingsIn(Schema):
    auto_advertising: bool | None = None
    accepted_categories: list[str] | None = Field(None, max_length=50)
    max_ads_per_day: int | None = Field(None, ge=0, le=100)
    max_ads_per_week: int | None = Field(None, ge=0, le=500)
    min_ad_interval_minutes: int | None = Field(None, ge=0, le=10080)
    min_cpm_floor: Decimal | None = Field(None, ge=0)

    @field_validator("min_cpm_floor", mode="before")
    @classmethod
    def _no_floats(cls, value):
        if isinstance(value, float):
            raise ValueError("send min_cpm_floor as a string, not a JSON number")
        return value


class EarningOut(Schema):
    id: uuid.UUID
    status: str
    billable_impressions: int
    publisher_cpm: Money
    gross_amount: Money
    platform_commission: Money
    net_amount: Money
    currency: str
    confirm_after: datetime
    confirmed_at: datetime | None = None
    created_at: datetime


class EarningsSummary(Schema):
    pending: Money
    confirmed: Money
    paid: Money
    reversed: Money
    total_earned: Money
    billable_impressions: int
    effective_cpm: Money


class PayoutMethodIn(Schema):
    method: PayoutMethod
    destination: str = Field(..., min_length=4, max_length=64)
    account_name: str | None = Field(None, max_length=120)
    bank_name: str | None = Field(None, max_length=120)
    branch: str | None = Field(None, max_length=120)
    label: str | None = Field(None, max_length=64)
    make_default: bool = True


class PayoutMethodOut(Schema):
    id: uuid.UUID
    method: PayoutMethod
    label: str | None = None
    account_name: str | None = None
    #: Masked form only — the full destination is never returned by the API.
    destination_masked: str
    bank_name: str | None = None
    is_default: bool
    created_at: datetime


class WithdrawalIn(Schema):
    amount: Decimal = Field(..., gt=0)
    payout_method_id: uuid.UUID

    @field_validator("amount", mode="before")
    @classmethod
    def _no_floats(cls, value):
        if isinstance(value, float):
            raise ValueError("send the amount as a string, not a JSON number")
        return value


class WithdrawalOut(Schema):
    id: uuid.UUID
    status: WithdrawalStatus
    amount: Money
    fee: Money
    net_amount: Money
    currency: str
    method: PayoutMethod
    destination_masked: str
    provider_reference: str | None = None
    rejection_reason: str | None = None
    created_at: datetime
    processed_at: datetime | None = None


class FeeQuoteOut(Schema):
    amount: Money
    fee: Money
    net: Money
    minimum: Money
    currency: str
