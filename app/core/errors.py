"""Domain errors. Transport layers map these onto HTTP codes / bot messages."""

from __future__ import annotations


class AdNetError(Exception):
    """Base class. ``code`` is a stable machine-readable string."""

    code = "error"
    http_status = 400

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.context = context

    def to_dict(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message, "context": self.context}}


class NotFound(AdNetError):
    code = "not_found"
    http_status = 404


class PermissionDenied(AdNetError):
    code = "permission_denied"
    http_status = 403


class Unauthenticated(AdNetError):
    code = "unauthenticated"
    http_status = 401


class ValidationFailed(AdNetError):
    code = "validation_failed"
    http_status = 422


class Conflict(AdNetError):
    code = "conflict"
    http_status = 409


class RateLimited(AdNetError):
    code = "rate_limited"
    http_status = 429


# --- financial -------------------------------------------------------------


class LedgerError(AdNetError):
    code = "ledger_error"
    http_status = 500


class UnbalancedTransaction(LedgerError):
    code = "unbalanced_transaction"


class InsufficientFunds(AdNetError):
    code = "insufficient_funds"
    http_status = 409


class DuplicateTransaction(Conflict):
    code = "duplicate_transaction"


class CurrencyMismatch(LedgerError):
    code = "currency_mismatch"


# --- domain ----------------------------------------------------------------


class SuspendedAccount(PermissionDenied):
    code = "account_suspended"


class CampaignNotDeliverable(AdNetError):
    code = "campaign_not_deliverable"


class ChannelNotVerified(AdNetError):
    code = "channel_not_verified"


class FraudBlocked(AdNetError):
    code = "fraud_blocked"
    http_status = 403


class WithdrawalError(AdNetError):
    code = "withdrawal_error"
