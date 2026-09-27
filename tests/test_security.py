"""Security primitives: signatures, 2FA, hashing, lockout (spec §28)."""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest

from app.core.errors import PermissionDenied, Unauthenticated
from app.core.security import (
    constant_time_equals,
    hash_password,
    new_recovery_codes,
    new_token,
    token_fingerprint,
    tracking_token,
    verify_password,
    verify_telegram_login,
    verify_totp,
    verify_tracking_token,
    verify_webhook_secret,
)

BOT_TOKEN = "123456:TEST-BOT-TOKEN-FOR-SIGNATURES"


def _init_data(token: str = BOT_TOKEN, **overrides) -> str:
    """Build a correctly signed Telegram WebApp initData payload."""
    import json

    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAA",
        "user": json.dumps({"id": 777_000_111, "first_name": "Test", "username": "tester"}),
    }
    fields.update({k: v for k, v in overrides.items() if v is not None})
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# --------------------------------------------------------------------------
# Telegram WebApp login (spec §28)
# --------------------------------------------------------------------------


def test_a_correctly_signed_payload_is_accepted(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", BOT_TOKEN)
    fields = verify_telegram_login(_init_data())
    assert "user" in fields
    assert "hash" not in fields  # consumed, not passed through


def test_a_forged_payload_is_rejected(monkeypatch):
    """Without this check anyone could claim to be any Telegram user."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", BOT_TOKEN)
    # Signed with the wrong bot token.
    with pytest.raises(Unauthenticated, match="bad initData signature"):
        verify_telegram_login(_init_data(token="999:SOMEONE-ELSES-TOKEN"))


def test_tampering_with_the_user_id_invalidates_the_signature(monkeypatch):
    """The whole point: you cannot swap yourself for another user id."""
    import json

    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", BOT_TOKEN)
    good = _init_data()
    tampered = good.replace(
        urlencode(
            {"user": json.dumps({"id": 777_000_111, "first_name": "Test", "username": "tester"})}
        ).split("=", 1)[1],
        urlencode(
            {"user": json.dumps({"id": 999_999_999, "first_name": "Test", "username": "tester"})}
        ).split("=", 1)[1],
    )
    assert tampered != good
    with pytest.raises(Unauthenticated):
        verify_telegram_login(tampered)


def test_a_payload_with_no_hash_is_rejected(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", BOT_TOKEN)
    with pytest.raises(Unauthenticated, match="missing hash"):
        verify_telegram_login("user=%7B%22id%22%3A1%7D&auth_date=1")


def test_an_expired_payload_is_rejected(monkeypatch):
    """A replayed initData from last week must not still authenticate."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", BOT_TOKEN)
    stale = _init_data(auth_date=str(int(time.time()) - 200_000))
    with pytest.raises(Unauthenticated, match="expired"):
        verify_telegram_login(stale, max_age=86400)


def test_login_fails_closed_without_a_configured_token(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "")
    with pytest.raises(Unauthenticated, match="not configured"):
        verify_telegram_login(_init_data())


# --------------------------------------------------------------------------
# Webhook secret (spec §28)
# --------------------------------------------------------------------------


def test_webhook_secret_must_match(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t-token")
    verify_webhook_secret("s3cr3t-token")  # no exception
    with pytest.raises(Unauthenticated):
        verify_webhook_secret("wrong")
    with pytest.raises(Unauthenticated):
        verify_webhook_secret(None)


def test_production_refuses_to_run_without_a_webhook_secret(monkeypatch):
    """An unverified webhook lets anyone impersonate any Telegram user."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(Unauthenticated, match="not configured"):
        verify_webhook_secret(None)


def test_development_tolerates_a_missing_webhook_secret(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    monkeypatch.setattr(settings, "app_env", "development")
    verify_webhook_secret(None)  # convenient locally, refused in production


# --------------------------------------------------------------------------
# Tracking tokens
# --------------------------------------------------------------------------


def test_tracking_tokens_are_bound_to_their_delivery_and_nonce():
    """A publisher must not be able to mint tracking URLs for their own channel."""
    token = tracking_token("delivery-a", "nonce-1")
    assert verify_tracking_token("delivery-a", "nonce-1", token)
    assert not verify_tracking_token("delivery-b", "nonce-1", token)
    assert not verify_tracking_token("delivery-a", "nonce-2", token)
    assert not verify_tracking_token("delivery-a", "nonce-1", token[:-2] + "xx")


def test_tracking_tokens_are_short_enough_for_a_url():
    token = tracking_token("some-delivery-uuid", new_token(16))
    assert 20 <= len(token) <= 32
    assert "=" not in token and "/" not in token and "+" not in token


# --------------------------------------------------------------------------
# Passwords and 2FA
# --------------------------------------------------------------------------


def test_passwords_are_hashed_not_stored():
    hashed = hash_password("a-sufficiently-long-password")
    assert "a-sufficiently-long-password" not in hashed
    assert hashed.startswith("$argon2")
    assert verify_password("a-sufficiently-long-password", hashed)
    assert not verify_password("wrong-password-entirely", hashed)


def test_the_same_password_hashes_differently_each_time():
    """Per-hash salt: two staff with the same password must not look identical."""
    assert hash_password("identical-password-x") != hash_password("identical-password-x")


def test_short_passwords_are_refused():
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short")


def test_verify_password_tolerates_a_corrupt_hash():
    assert verify_password("anything", "not-a-real-hash") is False


def test_totp_accepts_the_current_code_and_rejects_others():
    import pyotp

    from app.core.security import new_totp_secret, totp_uri

    secret = new_totp_secret()
    assert "otpauth://" in totp_uri(secret, "admin@example.com")
    current = pyotp.TOTP(secret).now()
    assert verify_totp(secret, current)
    assert verify_totp(secret, f"{current[:3]} {current[3:]}")  # spaces tolerated
    assert not verify_totp(secret, "000000")
    assert not verify_totp(secret, "")
    assert not verify_totp("", current)


def test_recovery_codes_are_unique():
    codes = new_recovery_codes(8)
    assert len(codes) == 8 == len(set(codes))
    assert all(len(c) == 10 for c in codes)


def test_token_fingerprints_do_not_reveal_the_token():
    token = new_token()
    fingerprint = token_fingerprint(token)
    assert token not in fingerprint
    assert len(fingerprint) == 64
    assert token_fingerprint(token) == fingerprint


def test_constant_time_comparison():
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")


# --------------------------------------------------------------------------
# Admin sign-in policy
# --------------------------------------------------------------------------


def test_repeated_failures_lock_the_account(db, make_staff):
    """Slows credential stuffing without locking a legitimate admin out for long."""
    from app.admin.auth import MAX_FAILED, authenticate

    staff = make_staff(email="lockme@example.com")
    staff.password_hash = hash_password("correct-horse-battery")
    db.flush()

    for _ in range(MAX_FAILED):
        with pytest.raises(Unauthenticated):
            authenticate(db, "lockme@example.com", "wrong-password-here", None)

    assert staff.locked_until is not None
    # Even the right password is refused while locked.
    with pytest.raises(PermissionDenied, match="temporarily locked"):
        authenticate(db, "lockme@example.com", "correct-horse-battery", None)


def test_a_successful_sign_in_clears_the_failure_counter(db, make_staff):
    from app.admin.auth import authenticate

    staff = make_staff(email="clearme@example.com")
    staff.password_hash = hash_password("correct-horse-battery")
    db.flush()
    with pytest.raises(Unauthenticated):
        authenticate(db, "clearme@example.com", "wrong-password-here", None)
    assert staff.failed_logins == 1

    result = authenticate(db, "clearme@example.com", "correct-horse-battery", None)
    assert result.staff.id == staff.id
    assert staff.failed_logins == 0
    assert staff.last_login_at is not None
    assert result.csrf


def test_totp_is_required_once_enabled(db, make_staff):
    import pyotp

    from app.admin.auth import authenticate
    from app.core.security import new_totp_secret

    secret = new_totp_secret()
    staff = make_staff(email="twofa@example.com", totp_secret=secret, totp_enabled=True)
    staff.password_hash = hash_password("correct-horse-battery")
    db.flush()

    with pytest.raises(Unauthenticated):
        authenticate(db, "twofa@example.com", "correct-horse-battery", None)
    with pytest.raises(Unauthenticated):
        authenticate(db, "twofa@example.com", "correct-horse-battery", "000000")

    result = authenticate(
        db, "twofa@example.com", "correct-horse-battery", pyotp.TOTP(secret).now()
    )
    assert result.staff.id == staff.id


def test_production_refuses_sign_in_without_2fa_enabled(db, make_staff, monkeypatch):
    """Spec §28: admin 2FA. In production it is not optional."""
    from app.admin.auth import authenticate
    from app.core.config import settings

    staff = make_staff(email="no2fa@example.com", totp_enabled=False)
    staff.password_hash = hash_password("correct-horse-battery")
    db.flush()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "admin_require_2fa", True)

    with pytest.raises(PermissionDenied, match="two-factor"):
        authenticate(db, "no2fa@example.com", "correct-horse-battery", None)


def test_an_inactive_staff_account_cannot_sign_in(db, make_staff):
    from app.admin.auth import authenticate

    staff = make_staff(email="gone@example.com", is_active=False)
    staff.password_hash = hash_password("correct-horse-battery")
    db.flush()
    with pytest.raises(Unauthenticated):
        authenticate(db, "gone@example.com", "correct-horse-battery", None)


def test_bootstrap_admin_is_idempotent_and_needs_a_strong_password(db, monkeypatch):
    from app.admin.auth import bootstrap_admin
    from app.core.config import settings
    from app.core.errors import ValidationFailed

    monkeypatch.setattr(settings, "bootstrap_admin_email", "")
    assert bootstrap_admin(db) is None  # nothing configured, nothing created

    monkeypatch.setattr(settings, "bootstrap_admin_email", "boot@example.com")
    monkeypatch.setattr(settings, "bootstrap_admin_password", "tooshort")
    with pytest.raises(ValidationFailed, match="at least 12"):
        bootstrap_admin(db)

    monkeypatch.setattr(settings, "bootstrap_admin_password", "a-long-enough-password")
    first = bootstrap_admin(db)
    second = bootstrap_admin(db)
    assert first is not None and first.id == second.id


def test_secret_redaction_keeps_tokens_out_of_logs(monkeypatch):
    """A bot token in a log sink is a bot token in a breach."""
    from app.core.config import settings
    from app.core.logging import _redact

    monkeypatch.setattr(settings, "telegram_bot_token", "123:SUPER-SECRET-TOKEN")
    event = {
        "event": "call",
        "bot_token": "123:SUPER-SECRET-TOKEN",
        "password": "hunter2",
        "destination": "01712345678",
        "url": "https://api.telegram.org/bot123:SUPER-SECRET-TOKEN/sendMessage",
        "chat_id": -100123,
    }
    cleaned = _redact(None, "info", dict(event))
    assert cleaned["bot_token"] == "***redacted***"
    assert cleaned["password"] == "***redacted***"
    assert cleaned["destination"] == "***redacted***"
    assert "SUPER-SECRET-TOKEN" not in cleaned["url"]
    assert cleaned["chat_id"] == -100123  # ordinary fields survive
