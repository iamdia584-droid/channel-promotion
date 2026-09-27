"""The live Telegram gateway: error mapping and request shape.

Driven through a mock httpx transport so no network is involved. What matters is
that a Telegram error string becomes the right domain exception — a "chat not
found" must surface as a 404 to the publisher, not a 500.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.telegram_gateway import (
    ADMIN_STATUSES,
    BotNotAdmin,
    ChatNotFound,
    HttpTelegramGateway,
    MemberInfo,
    TelegramError,
    _markup,
    get_gateway,
    set_gateway,
)

TOKEN = "123456:TEST"


def _gateway(handler) -> HttpTelegramGateway:
    gateway = HttpTelegramGateway(token=TOKEN)
    gateway._client = httpx.Client(
        base_url=f"https://api.telegram.org/bot{TOKEN}",
        transport=httpx.MockTransport(handler),
    )
    return gateway


def _ok(result):
    return lambda request: httpx.Response(200, json={"ok": True, "result": result})


def _fail(description, status=400):
    return lambda request: httpx.Response(status, json={"ok": False, "description": description})


def test_a_token_is_required():
    with pytest.raises(TelegramError, match="not configured"):
        HttpTelegramGateway(token="")


def test_get_chat_maps_the_response(monkeypatch):
    gateway = _gateway(
        _ok(
            {
                "id": -1001234567890,
                "type": "channel",
                "title": "Exam Prep",
                "username": "examprep",
                "description": "daily questions",
            }
        )
    )
    info = gateway.get_chat("@examprep")
    assert info.telegram_chat_id == -1001234567890
    assert info.chat_type == "channel"
    assert info.title == "Exam Prep"
    assert info.username == "examprep"


def test_chat_not_found_becomes_a_404_domain_error():
    """This reaches a publisher as guidance, so it must not be a 500."""
    gateway = _gateway(_fail("Bad Request: chat not found"))
    with pytest.raises(ChatNotFound) as exc:
        gateway.get_chat("@nope")
    assert exc.value.http_status == 404


def test_missing_rights_becomes_a_403_domain_error():
    gateway = _gateway(_fail("Bad Request: not enough rights to send text messages"))
    with pytest.raises(BotNotAdmin) as exc:
        gateway.send_text(-100, "hello")
    assert exc.value.http_status == 403


def test_an_unrecognised_error_is_still_a_domain_error():
    gateway = _gateway(_fail("Too Many Requests: retry after 30"))
    with pytest.raises(TelegramError, match="Too Many Requests"):
        gateway.get_chat(-100)


def test_a_transport_failure_is_wrapped():
    def boom(request):
        raise httpx.ConnectError("dns failure")

    with pytest.raises(TelegramError, match="transport failure"):
        _gateway(boom).get_chat(-100)


def test_non_json_response_is_wrapped():
    gateway = _gateway(lambda request: httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(TelegramError, match="non-JSON"):
        gateway.get_chat(-100)


def test_member_info_identifies_admins_and_owners():
    gateway = _gateway(
        _ok(
            {
                "status": "creator",
                "user": {"id": 555, "is_bot": False},
            }
        )
    )
    member = gateway.get_chat_member(-100, 555)
    assert member.is_admin and member.is_owner
    assert member.user_id == 555

    gateway = _gateway(_ok({"status": "member", "user": {"id": 556}}))
    assert not gateway.get_chat_member(-100, 556).is_admin


def test_admin_statuses_are_exactly_creator_and_administrator():
    """Widening this set would let a plain member register someone's channel."""
    assert {"creator", "administrator"} == ADMIN_STATUSES
    for status in ("member", "restricted", "left", "kicked"):
        assert not MemberInfo(status=status).is_admin


def test_send_text_returns_the_message_id_and_link():
    gateway = _gateway(
        _ok(
            {
                "message_id": 4242,
                "chat": {"id": -1001234567890, "username": "examprep", "type": "channel"},
            }
        )
    )
    sent = gateway.send_text(-1001234567890, "an ad")
    assert sent.telegram_message_id == 4242
    assert sent.link == "https://t.me/examprep/4242"


def test_a_chat_without_a_username_has_no_public_link():
    gateway = _gateway(_ok({"message_id": 7, "chat": {"id": -100, "type": "channel"}}))
    assert gateway.send_text(-100, "x").link is None


def test_the_request_carries_html_parse_mode_and_the_keyboard():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 1, "chat": {"id": -100, "type": "channel"}},
            },
        )

    _gateway(handler).send_text(
        -100,
        "<b>ad</b>",
        buttons=[{"text": "Go", "url": "https://x/t/abc"}],
        disable_preview=True,
    )
    assert captured["parse_mode"] == "HTML"
    assert captured["disable_web_page_preview"] is True
    assert captured["reply_markup"]["inline_keyboard"] == [
        [{"text": "Go", "url": "https://x/t/abc"}]
    ]
    # None values are stripped rather than sent as nulls.
    assert all(v is not None for v in captured.values())


def _capture(gateway_factory, method, kind):
    """Send via ``method`` and return the JSON body that reached the transport."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 2, "chat": {"id": -100, "type": "channel"}},
            },
        )

    getattr(gateway_factory(handler), method)(-100, "file-id-xyz", caption="see this")
    return captured


@pytest.mark.parametrize("method,kind", [("send_photo", "photo"), ("send_video", "video")])
def test_photo_and_video_send_their_media(method, kind):
    body = _capture(_gateway, method, kind)
    assert body[kind] == "file-id-xyz"
    assert body["caption"] == "see this"


def test_delete_message_returns_false_rather_than_raising():
    """A post older than Telegram's deletion window is normal, not an error."""
    gateway = _gateway(_fail("Bad Request: message can't be deleted"))
    assert gateway.delete_message(-100, 1) is False

    gateway = _gateway(_ok(True))
    assert gateway.delete_message(-100, 1) is True


def test_member_count_is_an_integer():
    assert _gateway(_ok(48000)).get_chat_member_count(-100) == 48000


def test_get_me_id_is_cached():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "result": {"id": 999111}})

    gateway = _gateway(handler)
    assert gateway.get_me_id() == 999111
    assert gateway.get_me_id() == 999111
    assert calls["n"] == 1  # the bot's own id never changes


def test_markup_helper_stacks_buttons_one_per_row():
    assert _markup(None) is None
    assert _markup([]) is None
    assert _markup([{"text": "a", "url": "u"}, {"text": "b", "url": "v"}]) == {
        "inline_keyboard": [[{"text": "a", "url": "u"}], [{"text": "b", "url": "v"}]]
    }


def test_the_error_log_does_not_carry_the_payload(monkeypatch):
    """Telegram request bodies can contain the bot token in URLs."""
    logged: list = []

    import app.services.telegram_gateway as module

    class Recorder:
        def warning(self, event, **kw):
            logged.append((event, kw))

    monkeypatch.setattr(module, "log", Recorder())
    with pytest.raises(TelegramError):
        _gateway(_fail("Bad Request: something"))._call("sendMessage", text="secret body")

    assert logged
    event, fields = logged[0]
    assert set(fields) == {"method", "description"}
    assert "secret body" not in str(fields)


def test_gateway_injection_point():
    from app.services.telegram_gateway import FakeTelegramGateway

    fake = FakeTelegramGateway()
    set_gateway(fake)
    try:
        assert get_gateway() is fake
    finally:
        set_gateway(None)


def test_close_releases_the_client():
    gateway = _gateway(_ok(True))
    gateway.close()
    assert gateway._client is None
