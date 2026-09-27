"""HTTP layer: authentication, authorisation, validation, idempotency (spec §25, §26)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.money import q
from app.db.base import utcnow


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _campaign_body(**kw) -> dict:
    now = utcnow()
    body = {
        "name": "Exam Preparation 2026",
        "campaign_type": "text",
        # Money as strings: a JSON number would be parsed as a float.
        "total_budget": "10000",
        "daily_budget": "2000",
        "bid_cpm": "50",
        "starts_at": now.isoformat(),
        "ends_at": (now + timedelta(days=7)).isoformat(),
        "body_text": "Join our exam preparation course today",
        "destination_url": "https://example.com/course",
        "cta_text": "Enrol now",
        "targeting": {"countries": ["BD"], "categories": ["education"],
                      "languages": ["bn"]},
    }
    body.update(kw)
    return body


# --------------------------------------------------------------------------
# Authentication and authorisation
# --------------------------------------------------------------------------


def test_unauthenticated_requests_are_rejected(client):
    for path in [
        "/api/v1/advertisers/me/wallet",
        "/api/v1/publishers/me/channels",
        "/api/v1/admin/overview",
    ]:
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthenticated"


def test_garbage_token_is_rejected(client):
    response = client.get("/api/v1/advertisers/me/wallet",
                          headers=_auth("not-a-real-token"))
    assert response.status_code == 401


def test_publisher_cannot_reach_advertiser_endpoints(client, api_token):
    """Role separation is enforced server-side, not by hiding the button."""
    token, _ = api_token(publisher=True)
    response = client.get("/api/v1/advertisers/me/wallet", headers=_auth(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_advertiser_cannot_reach_publisher_endpoints(client, api_token):
    token, _ = api_token(advertiser=True)
    assert client.get("/api/v1/publishers/me/earnings/summary",
                      headers=_auth(token)).status_code == 403


def test_ordinary_user_cannot_reach_admin_endpoints(client, api_token):
    token, _ = api_token(advertiser=True, publisher=True)
    for path in ["/api/v1/admin/overview", "/api/v1/admin/settings",
                 "/api/v1/admin/audit", "/api/v1/admin/withdrawals/queue"]:
        assert client.get(path, headers=_auth(token)).status_code == 403, path


def test_suspended_account_is_locked_out(client, api_token, db):
    from app.models.enums import UserStatus

    token, user = api_token(advertiser=True)
    user.status = UserStatus.SUSPENDED
    db.commit()
    response = client.get("/api/v1/advertisers/me/wallet", headers=_auth(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_suspended"


# --------------------------------------------------------------------------
# Money is never a JSON float (spec §11)
# --------------------------------------------------------------------------


def test_wallet_amounts_are_strings_not_numbers(client, api_token):
    token, _ = api_token(advertiser=True)
    response = client.get("/api/v1/advertisers/me/wallet", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    for field in ("available", "reserved", "spent", "pending", "confirmed"):
        assert isinstance(body[field], str), f"{field} must be a string"
    assert body["available"] == "0.000000"
    assert body["currency"] == "BDT"


def test_float_budget_is_rejected(client, api_token):
    """Accepting 10000.5 as a JSON number would import binary rounding error."""
    token, _ = api_token(advertiser=True)
    response = client.post(
        "/api/v1/advertisers/me/campaigns",
        json=_campaign_body(total_budget=10000.5), headers=_auth(token),
    )
    assert response.status_code == 422
    assert any(
        "string" in d["message"].lower()
        for d in response.json()["error"]["context"]["details"]
    )


# --------------------------------------------------------------------------
# Campaign endpoints
# --------------------------------------------------------------------------


def test_campaign_create_read_and_preview(client, api_token):
    token, _ = api_token(advertiser=True)
    created = client.post("/api/v1/advertisers/me/campaigns",
                          json=_campaign_body(), headers=_auth(token))
    assert created.status_code == 201, created.text
    campaign = created.json()
    assert campaign["status"] == "draft"
    assert campaign["total_budget"] == "10000.000000"
    assert campaign["bid_cpm"] == "50.000000"

    campaign_id = campaign["id"]
    fetched = client.get(f"/api/v1/advertisers/me/campaigns/{campaign_id}",
                         headers=_auth(token))
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Exam Preparation 2026"

    preview = client.get(f"/api/v1/advertisers/me/campaigns/{campaign_id}/preview",
                         headers=_auth(token))
    assert preview.status_code == 200
    # ৳10,000 at ৳50 CPM = 200,000 impressions — and flagged as an estimate.
    assert preview.json()["estimated_impressions"] == 200_000
    assert preview.json()["estimate_is_not_a_guarantee"] is True


def test_idempotency_key_prevents_duplicate_campaigns(client, api_token):
    """Spec §26: a retried create must not produce two campaigns."""
    token, _ = api_token(advertiser=True)
    body = _campaign_body()
    headers = {**_auth(token), "Idempotency-Key": "create-once-123"}

    first = client.post("/api/v1/advertisers/me/campaigns", json=body, headers=headers)
    second = client.post("/api/v1/advertisers/me/campaigns", json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    listing = client.get("/api/v1/advertisers/me/campaigns", headers=_auth(token))
    assert listing.json()["total"] == 1


def test_reusing_an_idempotency_key_with_a_different_body_is_a_conflict(client, api_token):
    """Silently returning the cached response would be the wrong answer."""
    token, _ = api_token(advertiser=True)
    headers = {**_auth(token), "Idempotency-Key": "same-key"}
    client.post("/api/v1/advertisers/me/campaigns",
                json=_campaign_body(), headers=headers)
    conflict = client.post(
        "/api/v1/advertisers/me/campaigns",
        json=_campaign_body(name="A completely different campaign"), headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


def test_another_advertisers_campaign_is_not_readable(client, api_token):
    mine, _ = api_token(advertiser=True)
    theirs, _ = api_token(advertiser=True)
    created = client.post("/api/v1/advertisers/me/campaigns",
                          json=_campaign_body(), headers=_auth(theirs))
    campaign_id = created.json()["id"]
    response = client.get(f"/api/v1/advertisers/me/campaigns/{campaign_id}",
                          headers=_auth(mine))
    # 404 rather than 403: confirming existence would leak that the id is real.
    assert response.status_code == 404


def test_submitting_without_funds_reports_insufficient_funds(client, api_token):
    token, _ = api_token(advertiser=True)
    created = client.post("/api/v1/advertisers/me/campaigns",
                          json=_campaign_body(), headers=_auth(token))
    campaign_id = created.json()["id"]
    response = client.post(
        f"/api/v1/advertisers/me/campaigns/{campaign_id}/submit", headers=_auth(token)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_funds"


@pytest.mark.parametrize(
    "bad",
    [
        {"destination_url": "javascript:alert(1)"},
        {"total_budget": "0"},
        {"bid_cpm": "-5"},
        {"name": "x"},
        {"targeting": {"countries": ["BANGLADESH"]}},
        {"priority": 99},
    ],
)
def test_invalid_campaign_bodies_are_rejected(client, api_token, bad):
    token, _ = api_token(advertiser=True)
    response = client.post("/api/v1/advertisers/me/campaigns",
                           json=_campaign_body(**bad), headers=_auth(token))
    assert response.status_code in (400, 422), response.text


def test_inverted_schedule_is_rejected_at_the_schema(client, api_token):
    token, _ = api_token(advertiser=True)
    now = utcnow()
    response = client.post(
        "/api/v1/advertisers/me/campaigns",
        json=_campaign_body(starts_at=now.isoformat(),
                            ends_at=(now - timedelta(days=1)).isoformat()),
        headers=_auth(token),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Publisher endpoints
# --------------------------------------------------------------------------


def test_registering_an_unowned_channel_is_refused(client, api_token, db):
    token, user = api_token(publisher=True)
    # Bot is admin, but someone else owns the chat.
    client.gateway.register_chat(-100777, username="notmine", owner_id=424242)
    response = client.post("/api/v1/publishers/me/channels",
                           json={"identifier": "@notmine"}, headers=_auth(token))
    assert response.status_code == 422
    assert "not an owner or administrator" in response.json()["error"]["message"]


def test_registering_an_owned_channel_succeeds(client, api_token, db):
    token, user = api_token(publisher=True)
    client.gateway.register_chat(
        -100778, username="mychan", title="My Channel", members=30_000,
        owner_id=user.telegram_user_id,
    )
    response = client.post(
        "/api/v1/publishers/me/channels",
        json={"identifier": "@mychan", "category": "education", "country": "BD"},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["telegram_chat_id"] == -100778
    assert body["title"] == "My Channel"
    assert body["members"] == 30_000
    assert body["category"] == "education"
    # A band, never the raw quality score or fraud signals (spec §17).
    assert body["quality_band"] in {"poor", "fair", "good", "excellent"}
    assert "quality_score" not in body
    assert "fraud_score" not in body


def test_payout_destination_is_never_returned_in_full(client, api_token):
    token, _ = api_token(publisher=True)
    created = client.post(
        "/api/v1/publishers/me/payout-methods",
        json={"method": "bkash", "destination": "01712345678"}, headers=_auth(token),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["destination_masked"] == "01******678"
    assert "01712345678" not in created.text
    assert "destination" not in body or body.get("destination") is None


def test_invalid_payout_number_is_rejected(client, api_token):
    token, _ = api_token(publisher=True)
    response = client.post(
        "/api/v1/publishers/me/payout-methods",
        json={"method": "bkash", "destination": "12345"}, headers=_auth(token),
    )
    assert response.status_code == 422
    assert "01" in response.json()["error"]["message"]


def test_withdrawal_without_confirmed_balance_is_refused(client, api_token):
    token, _ = api_token(publisher=True)
    method = client.post(
        "/api/v1/publishers/me/payout-methods",
        json={"method": "bkash", "destination": "01712345678"}, headers=_auth(token),
    ).json()
    response = client.post(
        "/api/v1/publishers/me/withdrawals",
        json={"amount": "1000", "payout_method_id": method["id"]}, headers=_auth(token),
    )
    assert response.status_code in (409, 422)


def test_another_publishers_channel_is_not_readable(client, api_token):
    mine, _ = api_token(publisher=True)
    theirs, their_user = api_token(publisher=True)
    client.gateway.register_chat(-100779, username="theirs",
                                 owner_id=their_user.telegram_user_id)
    created = client.post("/api/v1/publishers/me/channels",
                          json={"identifier": "@theirs"}, headers=_auth(theirs))
    channel_id = created.json()["id"]
    assert client.get(f"/api/v1/publishers/me/channels/{channel_id}",
                      headers=_auth(mine)).status_code == 404


# --------------------------------------------------------------------------
# Tracking endpoint
# --------------------------------------------------------------------------


def test_tracking_link_records_an_impression_and_redirects(client, db, sent_delivery):
    from app.models.delivery import Impression
    from app.services.delivery import DeliveryService

    delivery, campaign, channel, advertiser = sent_delivery()
    url = DeliveryService(db).tracking_url(delivery)
    db.commit()
    path = "/t/" + url.split("/t/", 1)[1]

    response = client.get(path, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/course"
    assert db.query(Impression).filter(Impression.delivery_id == delivery.id).count() == 1


def test_forged_tracking_token_is_rejected(client, db, sent_delivery):
    """A publisher must not be able to mint tracking URLs for their own channel."""
    from app.models.delivery import Impression

    delivery, *_ = sent_delivery()
    db.commit()
    response = client.get(f"/t/{delivery.id}/forged-token-aaaa", follow_redirects=False)
    assert response.status_code == 404
    assert db.query(Impression).count() == 0


def test_tracking_link_for_unknown_delivery_is_not_found(client):
    import uuid

    response = client.get(f"/t/{uuid.uuid4()}/whatever", follow_redirects=False)
    assert response.status_code == 404


def test_repeated_tracking_hits_bill_once(client, db, sent_delivery):
    from app.models.delivery import AdDelivery
    from app.services.delivery import DeliveryService

    delivery, *_ = sent_delivery()
    url = DeliveryService(db).tracking_url(delivery)
    delivery_id = delivery.id
    db.commit()
    path = "/t/" + url.split("/t/", 1)[1]

    for _ in range(6):
        assert client.get(path, follow_redirects=False).status_code == 302

    db.expire_all()
    reloaded = db.get(AdDelivery, delivery_id)
    assert reloaded.billable_impressions == 1
    assert reloaded.clicks == 1


# --------------------------------------------------------------------------
# Telegram webhook (spec §28)
# --------------------------------------------------------------------------


def test_webhook_rejects_a_missing_secret_token(client, monkeypatch):
    """Without this check anyone who learns the URL could impersonate any user."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "telegram_webhook_secret", "the-secret")
    response = client.post("/telegram/webhook", json={"update_id": 1})
    assert response.status_code == 401


def test_webhook_rejects_a_wrong_secret_token(client, monkeypatch):
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "telegram_webhook_secret", "the-secret")
    response = client.post(
        "/telegram/webhook", json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 401


def test_webhook_returns_200_even_when_a_handler_fails(client, monkeypatch):
    """A non-2xx makes Telegram retry forever, so handler errors must be swallowed."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "telegram_webhook_secret", "")
    response = client.post("/telegram/webhook", json={"update_id": 1, "garbage": True})
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Admin dashboard and ops
# --------------------------------------------------------------------------


def test_admin_dashboard_redirects_when_not_signed_in(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_admin_login_page_renders(client):
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert "Staff sign-in" in response.text


def test_admin_login_rejects_bad_credentials_without_leaking(client, db, make_staff):
    from app.core.security import hash_password

    staff = make_staff(email="admin@example.com")
    staff.password_hash = hash_password("correct-horse-battery")
    db.commit()

    wrong_password = client.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "wrong-password-here"},
    )
    unknown_email = client.post(
        "/admin/login",
        data={"email": "nobody@example.com", "password": "wrong-password-here"},
    )
    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    # Identical message: distinguishing them would enumerate staff emails.
    assert "incorrect" in wrong_password.text
    assert "incorrect" in unknown_email.text


def test_admin_can_sign_in_and_see_the_dashboard(client, db, make_staff):
    from app.core.security import hash_password

    staff = make_staff(email="admin2@example.com")
    staff.password_hash = hash_password("correct-horse-battery")
    db.commit()

    login = client.post(
        "/admin/login",
        data={"email": "admin2@example.com", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "adnet_admin" in login.cookies or client.cookies.get("adnet_admin")

    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "Total ad spend" in dashboard.text
    assert "Platform revenue" in dashboard.text


def test_security_headers_are_present(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_health_and_root(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").json()["api"] == "/api/v1"


def test_internal_errors_do_not_leak_details(client, api_token, monkeypatch):
    """A stack trace or SQL in a response body is an information leak."""
    from app.services import wallet as wallet_mod

    token, _ = api_token(advertiser=True)

    def boom(self, advertiser_id, create=True):
        raise RuntimeError("secret internal detail: table wallets")

    monkeypatch.setattr(wallet_mod.WalletService, "for_advertiser", boom)

    # A TestClient re-raises server exceptions by default, which bypasses the
    # application's own handler — the very thing under test. The flag is fixed at
    # construction, so this needs its own client.
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app, raise_server_exceptions=False) as strict:
        response = strict.get("/api/v1/advertisers/me/wallet", headers=_auth(token))
    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert response.json()["error"]["code"] == "internal_error"
