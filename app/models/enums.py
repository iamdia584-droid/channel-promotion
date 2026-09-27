"""All domain enumerations. Stored as short strings for readable SQL."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADVERTISER = "advertiser"
    PUBLISHER = "publisher"
    MODERATOR = "moderator"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    BANNED = "banned"
    DELETED = "deleted"


# --- campaigns & ads -------------------------------------------------------


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            CampaignStatus.COMPLETED,
            CampaignStatus.CANCELLED,
            CampaignStatus.REJECTED,
        }


class AdStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    SUSPENDED = "suspended"


class CampaignType(StrEnum):
    CHANNEL_POST = "channel_post"
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    BUTTON_LINK = "button_link"


class PricingModel(StrEnum):
    CPM = "cpm"  # per 1000 billable impressions
    CPV = "cpv"  # per billable view
    CPC = "cpc"  # per validated click (phase 2)


class SelectionMode(StrEnum):
    FIXED_CPM = "fixed_cpm"
    AUCTION = "auction"
    WEIGHTED = "weighted"


class AudienceType(StrEnum):
    ANY = "any"
    GENERAL = "general"
    STUDENT = "student"
    PROFESSIONAL = "professional"
    BUSINESS = "business"
    TECH = "tech"


# --- publishers & channels -------------------------------------------------


class ChatType(StrEnum):
    CHANNEL = "channel"
    GROUP = "group"
    SUPERGROUP = "supergroup"


class ChannelStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    ACTIVE = "active"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    REJECTED = "rejected"

    @property
    def can_serve(self) -> bool:
        return self in {ChannelStatus.VERIFIED, ChannelStatus.ACTIVE}


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    BOT_NOT_ADMIN = "bot_not_admin"
    CLAIMANT_NOT_ADMIN = "claimant_not_admin"
    INSUFFICIENT_RIGHTS = "insufficient_rights"
    VERIFIED = "verified"
    REVOKED = "revoked"


class MeasurementMode(StrEnum):
    """Which impression kinds may bill for this channel. See docs/TELEGRAM_CONSTRAINTS.md."""

    CLICK_ONLY = "click_only"
    VIEW_COUNTER = "view_counter"
    HYBRID = "hybrid"


# --- delivery & measurement ------------------------------------------------


class DeliveryStatus(StrEnum):
    PLANNED = "planned"
    RESERVED = "reserved"
    SENT = "sent"
    FAILED = "failed"
    MEASURING = "measuring"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    REVERSED = "reversed"


class ImpressionKind(StrEnum):
    MEASURED = "measured"
    TELEGRAM_REPORTED = "telegram_reported"
    ESTIMATED = "estimated"

    @property
    def billable_by_default(self) -> bool:
        return self in {ImpressionKind.MEASURED, ImpressionKind.TELEGRAM_REPORTED}


class ImpressionSource(StrEnum):
    TRACKING_LINK = "tracking_link"
    BOT_DEEPLINK = "bot_deeplink"
    VIEW_COUNTER = "view_counter"
    ESTIMATOR = "estimator"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    DUPLICATE = "duplicate"
    CAPPED = "capped"
    OUT_OF_WINDOW = "out_of_window"
    FRAUDULENT = "fraudulent"
    INVALIDATED = "invalidated"

    @property
    def is_billable(self) -> bool:
        return self is ValidationStatus.VALID


# --- money -----------------------------------------------------------------


class AccountKind(StrEnum):
    ADVERTISER_AVAILABLE = "advertiser_available"
    ADVERTISER_RESERVED = "advertiser_reserved"
    PUBLISHER_PENDING = "publisher_pending"
    PUBLISHER_CONFIRMED = "publisher_confirmed"
    PLATFORM_REVENUE = "platform_revenue"
    PLATFORM_FEES = "platform_fees"
    GATEWAY_CLEARING = "gateway_clearing"
    PAYOUT_CLEARING = "payout_clearing"
    FRAUD_CLAWBACK = "fraud_clawback"


class AccountOwnerType(StrEnum):
    ADVERTISER = "advertiser"
    PUBLISHER = "publisher"
    PLATFORM = "platform"


class EntryDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"

    @property
    def opposite(self) -> EntryDirection:
        return EntryDirection.CREDIT if self is EntryDirection.DEBIT else EntryDirection.DEBIT


class TransactionType(StrEnum):
    DEPOSIT = "deposit"
    BUDGET_RESERVE = "budget_reserve"
    BUDGET_RELEASE = "budget_release"
    SETTLEMENT = "settlement"
    EARNING_CONFIRM = "earning_confirm"
    EARNING_REVERSAL = "earning_reversal"
    WITHDRAWAL_REQUEST = "withdrawal_request"
    WITHDRAWAL_PAID = "withdrawal_paid"
    WITHDRAWAL_REVERSAL = "withdrawal_reversal"
    REFUND = "refund"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class TransactionStatus(StrEnum):
    POSTED = "posted"
    REVERSED = "reversed"


class DepositStatus(StrEnum):
    INITIATED = "initiated"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class EarningStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REVERSED = "reversed"
    PAID = "paid"


class WithdrawalStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            WithdrawalStatus.PAID,
            WithdrawalStatus.REJECTED,
            WithdrawalStatus.CANCELLED,
        }


class PayoutMethod(StrEnum):
    BKASH = "bkash"
    NAGAD = "nagad"
    ROCKET = "rocket"
    BANK = "bank"
    OTHER = "other"


class RefundStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSED = "processed"


# --- fraud, moderation, ops ------------------------------------------------


class FraudBand(StrEnum):
    NORMAL = "normal"  # 0-30
    REVIEW = "review"  # 31-60
    SUSPICIOUS = "suspicious"  # 61-80
    HIGH_RISK = "high_risk"  # 81-100

    @classmethod
    def of(cls, score: int) -> FraudBand:
        if score <= 30:
            return cls.NORMAL
        if score <= 60:
            return cls.REVIEW
        if score <= 80:
            return cls.SUSPICIOUS
        return cls.HIGH_RISK


class FraudSubject(StrEnum):
    IMPRESSION = "impression"
    CLICK = "click"
    DELIVERY = "delivery"
    CHANNEL = "channel"
    PUBLISHER = "publisher"
    ADVERTISER = "advertiser"
    CAMPAIGN = "campaign"
    WITHDRAWAL = "withdrawal"
    DEPOSIT = "deposit"


class FraudCaseStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class ReviewDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class ReviewTarget(StrEnum):
    CAMPAIGN = "campaign"
    ADVERTISEMENT = "advertisement"
    CHANNEL = "channel"


class ReportReason(StrEnum):
    SCAM = "scam"
    MALWARE = "malware"
    MISLEADING = "misleading"
    ADULT = "adult"
    ILLEGAL = "illegal"
    SPAM = "spam"
    IMPERSONATION = "impersonation"
    COPYRIGHT = "copyright"
    OTHER = "other"


class ReportStatus(StrEnum):
    OPEN = "open"
    REVIEWING = "reviewing"
    UPHELD = "upheld"
    DISMISSED = "dismissed"


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class SettingType(StrEnum):
    STRING = "string"
    INT = "int"
    DECIMAL = "decimal"
    BOOL = "bool"
    JSON = "json"


class PricingRuleScope(StrEnum):
    COUNTRY = "country"
    CATEGORY = "category"
    QUALITY_TIER = "quality_tier"
    AD_FORMAT = "ad_format"
    CHANNEL_SIZE = "channel_size"
