"""Campaign, advertisement and targeting schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.core.money import D
from app.models.enums import CampaignStatus, CampaignType, PricingModel
from app.schemas.common import Money, Schema


class TargetingIn(Schema):
    countries: list[str] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=list, max_length=50)
    categories: list[str] = Field(default_factory=list, max_length=50)
    excluded_categories: list[str] = Field(default_factory=list, max_length=50)
    audience_types: list[str] = Field(default_factory=list, max_length=20)
    min_members: int | None = Field(None, ge=0)
    max_members: int | None = Field(None, ge=0)
    min_avg_views: int | None = Field(None, ge=0)
    max_avg_views: int | None = Field(None, ge=0)

    @field_validator("countries")
    @classmethod
    def _iso_country(cls, value: list[str]) -> list[str]:
        for code in value:
            if len(code) != 2 or not code.isalpha():
                raise ValueError(f"{code!r} is not a 2-letter ISO country code")
        return [c.upper() for c in value]


class CampaignCreate(Schema):
    name: str = Field(..., min_length=2, max_length=200)
    campaign_type: CampaignType
    pricing_model: PricingModel = PricingModel.CPM
    total_budget: Decimal = Field(..., gt=0)
    daily_budget: Decimal = Field(..., gt=0)
    bid_cpm: Decimal = Field(..., gt=0)
    starts_at: datetime
    ends_at: datetime
    body_text: str | None = Field(None, max_length=3500)
    media_file_id: str | None = Field(None, max_length=300)
    media_url: str | None = Field(None, max_length=1000)
    destination_url: str | None = Field(None, max_length=2000)
    cta_text: str | None = Field(None, max_length=64)
    priority: int = Field(5, ge=1, le=10)
    max_impressions_per_user: int | None = Field(None, ge=1)
    max_impressions_per_channel_per_day: int | None = Field(None, ge=1)
    specific_channel_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    targeting: TargetingIn = Field(default_factory=TargetingIn)

    @field_validator("total_budget", "daily_budget", "bid_cpm", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("send money amounts as strings, not JSON numbers")
        return D(value)

    @model_validator(mode="after")
    def _schedule(self) -> "CampaignCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.daily_budget > self.total_budget:
            raise ValueError("daily_budget cannot exceed total_budget")
        return self


class AdvertisementOut(Schema):
    id: uuid.UUID
    status: str
    ad_format: str
    body_text: str | None = None
    media_url: str | None = None
    destination_url: str | None = None
    cta_text: str | None = None
    rejection_reason: str | None = None


class CampaignOut(Schema):
    id: uuid.UUID
    name: str
    status: CampaignStatus
    campaign_type: CampaignType
    pricing_model: PricingModel
    currency: str
    total_budget: Money
    daily_budget: Money
    bid_cpm: Money
    spent_amount: Money
    reserved_amount: Money
    refunded_amount: Money
    billable_impressions: int
    clicks: int
    starts_at: datetime
    ends_at: datetime
    priority: int
    paused_reason: str | None = None
    created_at: datetime
    ads: list[AdvertisementOut] = Field(default_factory=list)


class CampaignStats(Schema):
    status: str
    spend: Money
    remaining_budget: Money
    billable_impressions: int
    clicks: int
    ctr_percent: Decimal
    effective_cpm: Money
    cost_per_click: Money
    deliveries: int
    channels_reached: int
    currency: str


class CampaignPreview(Schema):
    name: str
    type: str
    currency: str
    total_budget: Money
    daily_budget: Money
    bid_cpm: Money
    #: A projection from historical averages, not a commitment. Billing uses
    #: measured impressions only (docs/TELEGRAM_CONSTRAINTS.md).
    estimated_impressions: int
    estimate_is_not_a_guarantee: bool
    countries: list[str]
    languages: list[str]
    categories: list[str]
    starts_at: datetime
    ends_at: datetime
    duration_days: int


class RejectIn(Schema):
    reason: str = Field(..., min_length=3, max_length=500)


class PauseIn(Schema):
    reason: str = Field("paused by advertiser", max_length=300)
