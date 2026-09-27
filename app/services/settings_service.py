"""Admin-configurable settings (spec §6, §31).

No CPM, commission rate, fee, threshold or window is hard-coded in business
logic. Every one is declared here with a default, a type and a valid range, is
editable from the admin dashboard, and every change is audit-logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.errors import ValidationFailed
from app.core.money import D, q
from app.models.enums import SelectionMode, SettingType
from app.models.ops import SystemSetting

CACHE_PREFIX = "setting:"
CACHE_TTL = 60


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: str
    value_type: SettingType
    category: str
    description: str
    min_value: str | None = None
    max_value: str | None = None


#: The complete configuration surface. Adding a knob means adding a row here.
SETTING_SPECS: tuple[SettingSpec, ...] = (
    # --- pricing ---
    SettingSpec(
        "base_cpm",
        "50.000000",
        SettingType.DECIMAL,
        "pricing",
        "Default advertiser CPM used when a campaign does not bid.",
        "0.000001",
    ),
    SettingSpec(
        "min_cpm",
        "5.000000",
        SettingType.DECIMAL,
        "pricing",
        "Lowest advertiser CPM bid accepted.",
        "0.000001",
    ),
    SettingSpec(
        "max_cpm",
        "10000.000000",
        SettingType.DECIMAL,
        "pricing",
        "Highest advertiser CPM bid accepted.",
        "0.000001",
    ),
    SettingSpec(
        "platform_commission_rate",
        "0.3000",
        SettingType.DECIMAL,
        "pricing",
        "Platform share of gross ad spend. 0.30 = 30%.",
        "0",
        "1",
    ),
    SettingSpec(
        "min_campaign_budget",
        "500.000000",
        SettingType.DECIMAL,
        "pricing",
        "Minimum total campaign budget.",
        "0.000001",
    ),
    SettingSpec(
        "min_daily_budget",
        "100.000000",
        SettingType.DECIMAL,
        "pricing",
        "Minimum daily campaign budget.",
        "0.000001",
    ),
    SettingSpec(
        "quality_multiplier_floor",
        "0.5000",
        SettingType.DECIMAL,
        "pricing",
        "Lowest quality multiplier a channel can attract.",
        "0.0001",
        "10",
    ),
    SettingSpec(
        "quality_multiplier_ceiling",
        "1.5000",
        SettingType.DECIMAL,
        "pricing",
        "Highest quality multiplier a channel can attract.",
        "0.0001",
        "10",
    ),
    # --- payouts ---
    SettingSpec(
        "min_withdrawal",
        "500.000000",
        SettingType.DECIMAL,
        "payout",
        "Minimum publisher withdrawal amount.",
        "0.000001",
    ),
    SettingSpec(
        "max_withdrawal_per_day",
        "100000.000000",
        SettingType.DECIMAL,
        "payout",
        "Per-publisher daily withdrawal ceiling.",
        "0.000001",
    ),
    SettingSpec(
        "withdrawal_fee_flat",
        "10.000000",
        SettingType.DECIMAL,
        "payout",
        "Flat fee deducted from a withdrawal.",
        "0",
    ),
    SettingSpec(
        "withdrawal_fee_percent",
        "0.0100",
        SettingType.DECIMAL,
        "payout",
        "Percentage fee deducted from a withdrawal. 0.01 = 1%.",
        "0",
        "1",
    ),
    SettingSpec(
        "large_withdrawal_alert",
        "50000.000000",
        SettingType.DECIMAL,
        "payout",
        "Withdrawals at or above this notify admins.",
        "0",
    ),
    SettingSpec(
        "earnings_validation_hours",
        "72",
        SettingType.INT,
        "payout",
        "Hours earnings stay pending before becoming withdrawable.",
        "0",
        "8760",
    ),
    # --- delivery ---
    SettingSpec(
        "selection_mode",
        SelectionMode.AUCTION.value,
        SettingType.STRING,
        "delivery",
        "Campaign selection algorithm: fixed_cpm, auction or weighted.",
    ),
    SettingSpec(
        "auction_second_price_increment",
        "0.0100",
        SettingType.DECIMAL,
        "delivery",
        "Fraction above the runner-up bid charged in a second-price auction.",
        "0",
        "1",
    ),
    SettingSpec(
        "weighted_top_n",
        "5",
        SettingType.INT,
        "delivery",
        "Candidate pool size for weighted selection.",
        "1",
        "100",
    ),
    SettingSpec(
        "max_advertiser_inventory_share",
        "0.4000",
        SettingType.DECIMAL,
        "delivery",
        "Max share of one channel's daily slots a single advertiser may take.",
        "0.01",
        "1",
    ),
    SettingSpec(
        "pacing_burst_ratio",
        "1.2500",
        SettingType.DECIMAL,
        "delivery",
        "How far ahead of the hourly pacing target a campaign may run.",
        "1",
        "10",
    ),
    SettingSpec(
        "default_max_ads_per_day",
        "3",
        SettingType.INT,
        "delivery",
        "Default per-channel daily ad cap.",
        "0",
        "100",
    ),
    SettingSpec(
        "default_max_ads_per_week",
        "15",
        SettingType.INT,
        "delivery",
        "Default per-channel weekly ad cap.",
        "0",
        "500",
    ),
    SettingSpec(
        "default_min_ad_interval_minutes",
        "240",
        SettingType.INT,
        "delivery",
        "Default minimum gap between ads in one channel.",
        "0",
        "10080",
    ),
    SettingSpec(
        "ad_post_ttl_hours",
        "48",
        SettingType.INT,
        "delivery",
        "Hours before a delivered ad post may be removed. 0 disables removal.",
        "0",
        "8760",
    ),
    # --- measurement ---
    SettingSpec(
        "impression_window_hours",
        "48",
        SettingType.INT,
        "measurement",
        "Hours after posting during which impressions may still be billed.",
        "1",
        "8760",
    ),
    SettingSpec(
        "impression_cap_multiplier",
        "1.5000",
        SettingType.DECIMAL,
        "measurement",
        "Billable impressions per delivery cap, as a multiple of channel avg_views.",
        "0.1",
        "10",
    ),
    SettingSpec(
        "min_avg_views_to_serve",
        "100",
        SettingType.INT,
        "measurement",
        "Channels below this average view count are not eligible.",
        "0",
    ),
    SettingSpec(
        "click_dedupe_window_minutes",
        "60",
        SettingType.INT,
        "measurement",
        "Repeat clicks from the same identity inside this window do not bill.",
        "0",
        "1440",
    ),
    # --- fraud ---
    SettingSpec(
        "fraud_review_threshold",
        "31",
        SettingType.INT,
        "fraud",
        "Score at or above which an event is queued for review.",
        "0",
        "100",
    ),
    SettingSpec(
        "fraud_block_threshold",
        "81",
        SettingType.INT,
        "fraud",
        "Score at or above which an impression does not bill.",
        "0",
        "100",
    ),
    SettingSpec(
        "fraud_hold_earnings_threshold",
        "61",
        SettingType.INT,
        "fraud",
        "Score at or above which earnings are held past the normal window.",
        "0",
        "100",
    ),
    SettingSpec(
        "fraud_max_view_growth_ratio",
        "3.0000",
        SettingType.DECIMAL,
        "fraud",
        "Daily view growth above this multiple of trailing average is suspicious.",
        "1",
        "100",
    ),
    SettingSpec(
        "fraud_min_view_member_ratio",
        "0.0200",
        SettingType.DECIMAL,
        "fraud",
        "Below this views/members ratio a channel looks member-inflated.",
        "0",
        "1",
    ),
    SettingSpec(
        "fraud_max_ctr",
        "0.2500",
        SettingType.DECIMAL,
        "fraud",
        "CTR above this is implausible for display advertising.",
        "0",
        "1",
    ),
    # --- general ---
    SettingSpec(
        "platform_name",
        "AdNet",
        SettingType.STRING,
        "general",
        "Name shown to users in the bot and dashboard.",
    ),
    SettingSpec(
        "support_username",
        "",
        SettingType.STRING,
        "general",
        "Telegram username handling support requests.",
    ),
    SettingSpec(
        "campaign_auto_approve",
        "false",
        SettingType.BOOL,
        "general",
        "Skip human moderation of new campaigns. Off by default.",
    ),
    SettingSpec(
        "channel_auto_approve",
        "true",
        SettingType.BOOL,
        "general",
        "Activate a channel automatically once ownership is verified.",
    ),
    SettingSpec(
        "registration_open",
        "true",
        SettingType.BOOL,
        "general",
        "Allow new advertiser/publisher signups.",
    ),
    SettingSpec(
        "hour_weights",
        "[1,1,1,1,1,2,3,4,5,6,7,7,7,7,7,7,8,9,9,8,6,4,3,2]",
        SettingType.JSON,
        "delivery",
        "Relative spend weight per UTC hour, used for hourly pacing.",
    ),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTING_SPECS}


class SettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- reads -------------------------------------------------------------

    def raw(self, key: str) -> str:
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise ValidationFailed(f"unknown setting {key!r}")
        cached = cache_get(CACHE_PREFIX + key)
        if cached is not None:
            return cached
        row = self.session.scalars(
            select(SystemSetting).where(SystemSetting.key == key)
        ).one_or_none()
        value = row.value if row is not None else spec.default
        cache_set(CACHE_PREFIX + key, value, CACHE_TTL)
        return value

    def get(self, key: str) -> Any:
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise ValidationFailed(f"unknown setting {key!r}")
        return _coerce(self.raw(key), spec.value_type)

    def decimal(self, key: str) -> Decimal:
        value = self.get(key)
        if not isinstance(value, Decimal):
            raise ValidationFailed(f"setting {key!r} is not a decimal")
        return value

    def money(self, key: str) -> Decimal:
        return q(self.decimal(key))

    def int_(self, key: str) -> int:
        value = self.get(key)
        if not isinstance(value, int):
            raise ValidationFailed(f"setting {key!r} is not an int")
        return value

    def bool_(self, key: str) -> bool:
        return bool(self.get(key))

    def str_(self, key: str) -> str:
        return str(self.get(key))

    def all_by_category(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for spec in SETTING_SPECS:
            out.setdefault(spec.category, []).append(
                {
                    "key": spec.key,
                    "value": self.raw(spec.key),
                    "default": spec.default,
                    "type": spec.value_type.value,
                    "description": spec.description,
                    "min": spec.min_value,
                    "max": spec.max_value,
                    "is_default": self.raw(spec.key) == spec.default,
                }
            )
        return out

    # -- writes ------------------------------------------------------------

    def set(self, key: str, value: str, staff_id=None) -> tuple[str, str]:
        """Validate and persist. Returns ``(old, new)`` for the audit log."""
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise ValidationFailed(f"unknown setting {key!r}")
        value = value.strip()
        _coerce(value, spec.value_type)  # type check
        _range_check(value, spec)

        row = self.session.scalars(
            select(SystemSetting).where(SystemSetting.key == key)
        ).one_or_none()
        old = row.value if row is not None else spec.default
        if row is None:
            row = SystemSetting(
                key=key,
                value=value,
                value_type=spec.value_type,
                category=spec.category,
                description=spec.description,
                min_value=spec.min_value,
                max_value=spec.max_value,
            )
            self.session.add(row)
        else:
            row.value = value
        row.updated_by_staff_id = staff_id
        self.session.flush()
        cache_delete(CACHE_PREFIX + key)
        return old, value

    def seed_defaults(self) -> int:
        """Materialise any missing setting rows. Idempotent."""
        existing = set(self.session.scalars(select(SystemSetting.key)).all())
        created = 0
        for spec in SETTING_SPECS:
            if spec.key in existing:
                continue
            self.session.add(
                SystemSetting(
                    key=spec.key,
                    value=spec.default,
                    value_type=spec.value_type,
                    category=spec.category,
                    description=spec.description,
                    min_value=spec.min_value,
                    max_value=spec.max_value,
                )
            )
            created += 1
        self.session.flush()
        return created


def _coerce(raw: str, value_type: SettingType) -> Any:
    import json

    try:
        if value_type is SettingType.INT:
            return int(raw)
        if value_type is SettingType.DECIMAL:
            return D(raw)
        if value_type is SettingType.BOOL:
            lowered = raw.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
            raise ValueError(f"not a boolean: {raw!r}")
        if value_type is SettingType.JSON:
            return json.loads(raw)
        return raw
    except (ValueError, ArithmeticError) as exc:
        raise ValidationFailed(f"invalid {value_type.value} value {raw!r}: {exc}") from exc


def _range_check(raw: str, spec: SettingSpec) -> None:
    if spec.value_type not in {SettingType.INT, SettingType.DECIMAL}:
        return
    value = D(raw)
    if spec.min_value is not None and value < D(spec.min_value):
        raise ValidationFailed(f"{spec.key} must be >= {spec.min_value}")
    if spec.max_value is not None and value > D(spec.max_value):
        raise ValidationFailed(f"{spec.key} must be <= {spec.max_value}")
