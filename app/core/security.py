"""Password hashing, tokens, 2FA, Telegram webhook verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import parse_qsl

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.config import settings
from app.core.errors import Unauthenticated

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, ValueError):
        return False


def needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


# --- opaque tokens ---------------------------------------------------------


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_fingerprint(token: str) -> str:
    """Store this, never the token itself, so a DB leak yields nothing usable."""
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- signed payloads (admin sessions, tracking tokens) ---------------------


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def sign(payload: dict, salt: str) -> str:
    return _serializer(salt).dumps(payload)


def unsign(token: str, salt: str, max_age: int) -> dict:
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except BadSignature as exc:
        raise Unauthenticated("invalid or expired token") from exc


# --- 2FA -------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=settings.app_name)


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def new_recovery_codes(count: int = 8) -> list[str]:
    return [secrets.token_hex(5) for _ in range(count)]


# --- Telegram --------------------------------------------------------------


def verify_webhook_secret(header_value: str | None) -> None:
    """Telegram echoes our configured secret in X-Telegram-Bot-Api-Secret-Token.

    Without this check anyone who learns the webhook URL could inject updates and
    impersonate any Telegram user (spec §28).
    """
    expected = settings.telegram_webhook_secret
    if not expected:
        if settings.is_production:
            raise Unauthenticated("webhook secret is not configured")
        return
    if not header_value or not constant_time_equals(header_value, expected):
        raise Unauthenticated("bad webhook secret")


def verify_telegram_login(init_data: str, max_age: int = 86400) -> dict[str, str]:
    """Validate a Telegram WebApp ``initData`` payload (used by the web dashboard).

    Implements Telegram's documented HMAC scheme: the signing key is
    HMAC_SHA256(bot_token, "WebAppData").
    """
    if not settings.telegram_bot_token:
        raise Unauthenticated("bot token not configured")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        raise Unauthenticated("missing hash")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise Unauthenticated("bad initData signature")
    auth_date = int(pairs.get("auth_date", "0"))
    if auth_date and time.time() - auth_date > max_age:
        raise Unauthenticated("initData expired")
    return pairs


# --- tracking tokens -------------------------------------------------------


def tracking_token(delivery_id: str, nonce: str) -> str:
    """Short, URL-safe, tamper-proof token for a click/impression tracking link.

    Truncated HMAC: an advertiser or publisher cannot forge tokens for deliveries
    they do not own, and cannot mint extra nonces to inflate impressions.
    """
    mac = hmac.new(
        settings.secret_key.encode(), f"{delivery_id}:{nonce}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac[:18]).decode().rstrip("=")


def verify_tracking_token(delivery_id: str, nonce: str, token: str) -> bool:
    return hmac.compare_digest(tracking_token(delivery_id, nonce), token)
