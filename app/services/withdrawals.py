"""Publisher withdrawals and payout methods (spec §13).

Money leaves the confirmed balance the moment a request is made, into
``PAYOUT_CLEARING``. That way a publisher cannot request the same balance twice
while an operator is still processing the first request.

Payout destinations are encrypted at rest and only ever displayed masked
(spec §13: never expose sensitive payment information unnecessarily).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.errors import (
    Conflict,
    InsufficientFunds,
    NotFound,
    ValidationFailed,
    WithdrawalError,
)
from app.core.money import ZERO, D, q
from app.db.base import utcnow
from app.models.enums import (
    AccountKind,
    PayoutMethod,
    TransactionType,
    WithdrawalStatus,
)
from app.models.identity import Publisher
from app.models.money import PayoutMethodRecord, Withdrawal
from app.services.audit import Actor, AuditService
from app.services.ledger import LedgerService, credit, debit
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService


# --------------------------------------------------------------------------
# Destination protection
# --------------------------------------------------------------------------


def _key() -> bytes:
    """Derive an encryption key from SECRET_KEY. Rotating SECRET_KEY re-keys these."""
    return hashlib.sha256(f"payout-dest|{app_settings.secret_key}".encode()).digest()


def encrypt_destination(value: str) -> str:
    """Authenticated stream cipher over the destination.

    Keystream is HMAC-SHA256 in counter mode, tagged with HMAC so tampering is
    detectable. A dedicated KMS is the production answer; this keeps the plaintext
    out of the database without adding a native dependency.
    """
    raw = value.encode()
    key = _key()
    nonce = hashlib.sha256(raw + key).digest()[:16]
    stream = b""
    counter = 0
    while len(stream) < len(raw):
        stream += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(raw, stream[: len(raw)]))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode()


def decrypt_destination(blob: str) -> str:
    data = base64.urlsafe_b64decode(blob.encode())
    nonce, tag, cipher = data[:16], data[16:32], data[32:]
    key = _key()
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise WithdrawalError("payout destination failed its integrity check")
    stream = b""
    counter = 0
    while len(stream) < len(cipher):
        stream += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(cipher, stream[: len(cipher)])).decode()


def mask_destination(value: str) -> str:
    """Show only enough to recognise the account: ``01XXXXXX789``."""
    cleaned = "".join(ch for ch in value if ch.isalnum())
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    keep_front = 2 if len(cleaned) > 8 else 0
    keep_back = 3
    return (
        cleaned[:keep_front]
        + "*" * (len(cleaned) - keep_front - keep_back)
        + cleaned[-keep_back:]
    )


def destination_fingerprint(method: PayoutMethod | str, value: str) -> str:
    """Stable hash used to spot one destination shared by many publishers."""
    cleaned = "".join(ch for ch in value if ch.isalnum()).lower()
    material = f"{method}|{cleaned}|{app_settings.secret_key}"
    return hashlib.sha256(material.encode()).hexdigest()[:64]


# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeeBreakdown:
    amount: Decimal
    fee: Decimal
    net: Decimal


class WithdrawalService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = SettingsService(session)
        self.wallets = WalletService(session)
        self.ledger = LedgerService(session)
        self.audit = AuditService(session)

    # -- payout methods ----------------------------------------------------

    def add_payout_method(
        self,
        publisher_id: uuid.UUID,
        method: PayoutMethod,
        destination: str,
        *,
        account_name: str | None = None,
        bank_name: str | None = None,
        branch: str | None = None,
        label: str | None = None,
        make_default: bool = True,
    ) -> PayoutMethodRecord:
        destination = (destination or "").strip()
        if not destination:
            raise ValidationFailed("a payout destination is required")
        self._validate_destination(method, destination)

        fingerprint = destination_fingerprint(method, destination)
        existing = self.session.scalars(
            select(PayoutMethodRecord).where(
                PayoutMethodRecord.publisher_id == publisher_id,
                PayoutMethodRecord.method == method,
                PayoutMethodRecord.destination_fingerprint == fingerprint,
            )
        ).one_or_none()
        record = existing or PayoutMethodRecord(
            publisher_id=publisher_id, method=method,
            destination_fingerprint=fingerprint,
        )
        record.destination_masked = mask_destination(destination)
        record.destination_encrypted = encrypt_destination(destination)
        record.account_name = account_name
        record.bank_name = bank_name
        record.branch = branch
        record.label = label
        record.is_active = True
        if existing is None:
            self.session.add(record)
        self.session.flush()

        if make_default:
            self.session.execute(
                PayoutMethodRecord.__table__.update()
                .where(
                    PayoutMethodRecord.publisher_id == publisher_id,
                    PayoutMethodRecord.id != record.id,
                )
                .values(is_default=False)
            )
            record.is_default = True
            self.session.flush()
        return record

    def _validate_destination(self, method: PayoutMethod, destination: str) -> None:
        digits = "".join(ch for ch in destination if ch.isdigit())
        if method in (PayoutMethod.BKASH, PayoutMethod.NAGAD, PayoutMethod.ROCKET):
            # Bangladeshi mobile wallet: 11 digits starting 01, or +880 form.
            if digits.startswith("880"):
                digits = "0" + digits[3:]
            if len(digits) != 11 or not digits.startswith("01"):
                raise ValidationFailed(
                    f"{method.value} numbers are 11 digits starting with 01, "
                    "for example 01712345678"
                )
        elif method is PayoutMethod.BANK:
            if len(digits) < 8:
                raise ValidationFailed("a bank account number needs at least 8 digits")

    def list_payout_methods(self, publisher_id: uuid.UUID) -> list[PayoutMethodRecord]:
        return list(
            self.session.scalars(
                select(PayoutMethodRecord)
                .where(
                    PayoutMethodRecord.publisher_id == publisher_id,
                    PayoutMethodRecord.is_active.is_(True),
                )
                .order_by(PayoutMethodRecord.is_default.desc(),
                          PayoutMethodRecord.created_at.desc())
            ).all()
        )

    def reveal_destination(self, record: PayoutMethodRecord, actor: Actor) -> str:
        """Decrypt for an operator about to pay out. Always audited."""
        self.audit.log(
            actor, "payout_method.revealed",
            target_type="payout_method", target_id=record.id,
            reason="operator viewed the full payout destination",
        )
        return decrypt_destination(record.destination_encrypted)

    # -- fees --------------------------------------------------------------

    def quote_fee(self, amount: object) -> FeeBreakdown:
        amount = q(amount)
        flat = self.settings.money("withdrawal_fee_flat")
        percent = self.settings.decimal("withdrawal_fee_percent")
        fee = q(flat + amount * percent)
        if fee >= amount:
            raise ValidationFailed(
                f"the withdrawal fee ({fee}) would consume the whole amount"
            )
        return FeeBreakdown(amount, fee, q(amount - fee))

    # -- request -----------------------------------------------------------

    def request(
        self,
        publisher_id: uuid.UUID,
        amount: object,
        payout_method_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
        actor: Actor | None = None,
    ) -> Withdrawal:
        publisher = self.session.get(Publisher, publisher_id)
        if publisher is None:
            raise NotFound("publisher not found")
        if not publisher.is_active:
            raise WithdrawalError("this publisher account cannot withdraw right now")

        method = self.session.get(PayoutMethodRecord, payout_method_id)
        if method is None or method.publisher_id != publisher_id or not method.is_active:
            raise NotFound("payout method not found")

        amount = q(amount)
        minimum = self.settings.money("min_withdrawal")
        if amount < minimum:
            raise ValidationFailed(f"the minimum withdrawal is {minimum}")

        # Daily ceiling across everything not rejected/cancelled.
        cap = self.settings.money("max_withdrawal_per_day")
        if cap > ZERO:
            today = q(
                self.session.scalar(
                    select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                        Withdrawal.publisher_id == publisher_id,
                        Withdrawal.created_at >= utcnow() - timedelta(days=1),
                        Withdrawal.status.not_in(
                            [WithdrawalStatus.REJECTED, WithdrawalStatus.CANCELLED]
                        ),
                    )
                )
                or 0
            )
            if today + amount > cap:
                raise ValidationFailed(
                    f"this would exceed the daily withdrawal limit of {cap} "
                    f"({today} already requested today)"
                )

        breakdown = self.quote_fee(amount)
        wallet = self.wallets.locked(self.wallets.for_publisher(publisher_id).id)
        if q(wallet.confirmed_balance) < amount:
            raise InsufficientFunds(
                "not enough confirmed balance; pending earnings are not yet "
                "withdrawable",
                confirmed=str(q(wallet.confirmed_balance)),
                requested=str(amount),
                pending=str(q(wallet.pending_balance)),
            )

        idempotency_key = idempotency_key or f"withdrawal:{uuid.uuid4()}"
        existing = self.session.scalars(
            select(Withdrawal).where(Withdrawal.idempotency_key == idempotency_key)
        ).one_or_none()
        if existing is not None:
            return existing

        withdrawal = Withdrawal(
            publisher_id=publisher_id,
            payout_method_id=method.id,
            status=WithdrawalStatus.PENDING,
            amount=breakdown.amount,
            fee=breakdown.fee,
            net_amount=breakdown.net,
            currency=wallet.currency,
            method=method.method,
            destination_masked=method.destination_masked,
            idempotency_key=idempotency_key,
        )
        self.session.add(withdrawal)
        self.session.flush()

        # Move the money out of the withdrawable balance immediately.
        result = self.ledger.post(
            transaction_type=TransactionType.WITHDRAWAL_REQUEST,
            currency=wallet.currency,
            legs=[
                debit(AccountKind.PUBLISHER_CONFIRMED, breakdown.amount, publisher_id),
                credit(AccountKind.PAYOUT_CLEARING, breakdown.net),
                *([credit(AccountKind.PLATFORM_FEES, breakdown.fee)]
                  if breakdown.fee > ZERO else []),
            ],
            idempotency_key=f"withdrawal-request:{withdrawal.id}",
            description=f"Withdrawal requested via {method.method.value}",
            publisher_id=publisher_id,
            reference_type="withdrawal",
            reference_id=str(withdrawal.id),
        )
        wallet.confirmed_balance = q(D(wallet.confirmed_balance) - breakdown.amount)
        wallet.version += 1
        withdrawal.request_transaction_id = result.id
        self.session.flush()

        from app.services.fraud import FraudService

        FraudService(self.session).score_withdrawal(withdrawal)

        self.audit.financial(
            actor or Actor.system("publisher-request"), "withdrawal.requested",
            target_type="withdrawal", target_id=withdrawal.id,
            ledger_transaction_id=result.id,
            new_value={
                "amount": str(breakdown.amount), "fee": str(breakdown.fee),
                "net": str(breakdown.net), "method": method.method.value,
                "fraud_score": withdrawal.fraud_score,
            },
        )
        return withdrawal

    # -- operator transitions ---------------------------------------------

    def mark_processing(self, withdrawal: Withdrawal, actor: Actor) -> Withdrawal:
        if withdrawal.status is not WithdrawalStatus.PENDING:
            raise Conflict(f"cannot start processing a {withdrawal.status.value} withdrawal")
        if withdrawal.fraud_hold:
            raise WithdrawalError(
                "this withdrawal is on fraud hold and needs review before payout",
                fraud_score=withdrawal.fraud_score,
            )
        old = withdrawal.status
        withdrawal.status = WithdrawalStatus.PROCESSING
        withdrawal.processed_by_staff_id = _staff_id(actor)
        self.session.flush()
        self.audit.log(
            actor, "withdrawal.processing", target_type="withdrawal",
            target_id=withdrawal.id, old_value={"status": str(old)},
            new_value={"status": str(withdrawal.status)},
        )
        return withdrawal

    def mark_paid(
        self, withdrawal: Withdrawal, actor: Actor, provider_reference: str
    ) -> Withdrawal:
        if withdrawal.status not in (WithdrawalStatus.PENDING, WithdrawalStatus.PROCESSING):
            raise Conflict(f"cannot pay a {withdrawal.status.value} withdrawal")
        if not provider_reference.strip():
            raise ValidationFailed("a provider reference is required to record a payout")

        result = self.ledger.post(
            transaction_type=TransactionType.WITHDRAWAL_PAID,
            currency=withdrawal.currency,
            legs=[
                # The committed payout liability is discharged and the platform's
                # cash position falls by the same amount.
                debit(AccountKind.PAYOUT_CLEARING, withdrawal.net_amount),
                credit(AccountKind.GATEWAY_CLEARING, withdrawal.net_amount),
            ],
            idempotency_key=f"withdrawal-paid:{withdrawal.id}",
            description=f"Withdrawal paid, ref {provider_reference}",
            publisher_id=withdrawal.publisher_id,
            reference_type="withdrawal",
            reference_id=str(withdrawal.id),
        )
        old = withdrawal.status
        withdrawal.status = WithdrawalStatus.PAID
        withdrawal.processed_at = utcnow()
        withdrawal.processed_by_staff_id = _staff_id(actor)
        withdrawal.provider_reference = provider_reference.strip()[:128]
        withdrawal.settle_transaction_id = result.id

        wallet = self.wallets.locked(self.wallets.for_publisher(withdrawal.publisher_id).id)
        wallet.withdrawn_total = q(D(wallet.withdrawn_total) + q(withdrawal.net_amount))
        wallet.version += 1
        publisher = self.session.get(Publisher, withdrawal.publisher_id)
        if publisher is not None:
            publisher.lifetime_withdrawn = q(
                D(publisher.lifetime_withdrawn) + q(withdrawal.net_amount)
            )
        self.session.flush()

        self.audit.financial(
            actor, "withdrawal.paid", target_type="withdrawal", target_id=withdrawal.id,
            ledger_transaction_id=result.id,
            old_value={"status": str(old)},
            new_value={"status": "paid", "net": str(withdrawal.net_amount),
                       "reference": withdrawal.provider_reference},
        )
        return withdrawal

    def reject(self, withdrawal: Withdrawal, actor: Actor, reason: str) -> Withdrawal:
        """Return the full amount — fee included — to the publisher."""
        if withdrawal.status.is_terminal:
            raise Conflict(f"cannot reject a {withdrawal.status.value} withdrawal")
        if not reason.strip():
            raise ValidationFailed("a rejection reason is required")

        legs = [
            debit(AccountKind.PAYOUT_CLEARING, withdrawal.net_amount),
            credit(AccountKind.PUBLISHER_CONFIRMED, withdrawal.amount,
                   withdrawal.publisher_id),
        ]
        if q(withdrawal.fee) > ZERO:
            # Unwind the fee too: we never collected on a payout we did not make.
            legs.insert(1, debit(AccountKind.PLATFORM_FEES, withdrawal.fee))

        result = self.ledger.post(
            transaction_type=TransactionType.WITHDRAWAL_REVERSAL,
            currency=withdrawal.currency,
            legs=legs,
            idempotency_key=f"withdrawal-reject:{withdrawal.id}",
            description=f"Withdrawal rejected: {reason}",
            publisher_id=withdrawal.publisher_id,
            reference_type="withdrawal",
            reference_id=str(withdrawal.id),
            allow_negative=True,
        )
        old = withdrawal.status
        withdrawal.status = WithdrawalStatus.REJECTED
        withdrawal.rejection_reason = reason[:300]
        withdrawal.processed_at = utcnow()
        withdrawal.processed_by_staff_id = _staff_id(actor)
        withdrawal.settle_transaction_id = result.id

        wallet = self.wallets.locked(self.wallets.for_publisher(withdrawal.publisher_id).id)
        wallet.confirmed_balance = q(D(wallet.confirmed_balance) + q(withdrawal.amount))
        wallet.version += 1
        self.session.flush()

        self.audit.financial(
            actor, "withdrawal.rejected", target_type="withdrawal",
            target_id=withdrawal.id, ledger_transaction_id=result.id,
            old_value={"status": str(old)},
            new_value={"status": "rejected"}, reason=reason,
        )
        return withdrawal

    def cancel(self, withdrawal: Withdrawal, actor: Actor) -> Withdrawal:
        """Publisher-initiated cancellation, allowed only while still pending."""
        if withdrawal.status is not WithdrawalStatus.PENDING:
            raise Conflict(
                f"a {withdrawal.status.value} withdrawal can no longer be cancelled"
            )
        result = self.reject(withdrawal, actor, "cancelled by the publisher")
        result.status = WithdrawalStatus.CANCELLED
        self.session.flush()
        return result

    # -- reads -------------------------------------------------------------

    def list_for_publisher(
        self, publisher_id: uuid.UUID, limit: int = 50
    ) -> list[Withdrawal]:
        return list(
            self.session.scalars(
                select(Withdrawal)
                .where(Withdrawal.publisher_id == publisher_id)
                .order_by(Withdrawal.created_at.desc())
                .limit(limit)
            ).all()
        )

    def pending_queue(self, limit: int = 100) -> list[Withdrawal]:
        return list(
            self.session.scalars(
                select(Withdrawal)
                .where(
                    Withdrawal.status.in_(
                        [WithdrawalStatus.PENDING, WithdrawalStatus.PROCESSING]
                    )
                )
                .order_by(Withdrawal.fraud_hold.desc(), Withdrawal.created_at)
                .limit(limit)
            ).all()
        )


def _staff_id(actor: Actor):
    if actor.type != "staff" or not actor.id:
        return None
    try:
        return uuid.UUID(actor.id)
    except (ValueError, TypeError):  # pragma: no cover
        return None
