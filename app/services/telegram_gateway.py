"""Thin, typed wrapper over the Telegram Bot API.

Every call this module exposes is one the Bot API genuinely supports. Notably
absent — deliberately — is any way to read a channel post's view count, because
the Bot API does not expose it (docs/TELEGRAM_CONSTRAINTS.md). The interface is
abstract so tests and the delivery engine can run against a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.core.errors import AdNetError
from app.core.logging import get_logger

log = get_logger(__name__)

API_ROOT = "https://api.telegram.org"

#: Chat member statuses that mean "can act as an administrator".
ADMIN_STATUSES = frozenset({"creator", "administrator"})


class TelegramError(AdNetError):
    code = "telegram_error"


class ChatNotFound(TelegramError):
    code = "telegram_chat_not_found"
    http_status = 404


class BotNotAdmin(TelegramError):
    code = "telegram_bot_not_admin"
    http_status = 403


@dataclass(frozen=True)
class ChatInfo:
    telegram_chat_id: int
    chat_type: str
    title: str | None
    username: str | None
    description: str | None = None
    invite_link: str | None = None


@dataclass(frozen=True)
class MemberInfo:
    status: str
    can_post_messages: bool = False
    can_delete_messages: bool = False
    is_bot: bool = False
    user_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.status in ADMIN_STATUSES

    @property
    def is_owner(self) -> bool:
        return self.status == "creator"


@dataclass(frozen=True)
class SentMessage:
    telegram_message_id: int
    telegram_chat_id: int
    link: str | None = None


class TelegramGateway(Protocol):
    """The capability surface the rest of the system may rely on."""

    def get_chat(self, chat: int | str) -> ChatInfo: ...
    def get_chat_member_count(self, chat: int | str) -> int: ...
    def get_chat_member(self, chat: int | str, user_id: int) -> MemberInfo: ...
    def get_me_id(self) -> int: ...
    def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: list[dict] | None = None,
        disable_preview: bool = False,
    ) -> SentMessage: ...
    def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str | None = None,
        buttons: list[dict] | None = None,
    ) -> SentMessage: ...
    def send_video(
        self,
        chat_id: int,
        video: str,
        *,
        caption: str | None = None,
        buttons: list[dict] | None = None,
    ) -> SentMessage: ...
    def delete_message(self, chat_id: int, message_id: int) -> bool: ...


class HttpTelegramGateway:
    """Live implementation against api.telegram.org."""

    def __init__(self, token: str | None = None, timeout: float = 15.0) -> None:
        self.token = token or settings.telegram_bot_token
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self._bot_id: int | None = None

    # -- transport ---------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=f"{API_ROOT}/bot{self.token}", timeout=self._timeout
            )
        return self._client

    def _call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            response = self.client.post(f"/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} transport failure: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON") from exc
        if not body.get("ok"):
            description = str(body.get("description", "unknown error"))
            # Never log the payload wholesale: it can carry the bot token in URLs.
            log.warning("telegram_api_error", method=method, description=description)
            lowered = description.lower()
            if "chat not found" in lowered or "chat_id is empty" in lowered:
                raise ChatNotFound(description)
            if "not enough rights" in lowered or "need administrator" in lowered:
                raise BotNotAdmin(description)
            raise TelegramError(description, method=method)
        return body["result"]

    # -- reads -------------------------------------------------------------

    def get_me_id(self) -> int:
        if self._bot_id is None:
            self._bot_id = int(self._call("getMe")["id"])
        return self._bot_id

    def get_chat(self, chat: int | str) -> ChatInfo:
        result = self._call("getChat", chat_id=chat)
        return ChatInfo(
            telegram_chat_id=int(result["id"]),
            chat_type=str(result["type"]),
            title=result.get("title"),
            username=result.get("username"),
            description=result.get("description"),
            invite_link=result.get("invite_link"),
        )

    def get_chat_member_count(self, chat: int | str) -> int:
        return int(self._call("getChatMemberCount", chat_id=chat))

    def get_chat_member(self, chat: int | str, user_id: int) -> MemberInfo:
        result = self._call("getChatMember", chat_id=chat, user_id=user_id)
        user = result.get("user", {})
        return MemberInfo(
            status=str(result.get("status", "left")),
            can_post_messages=bool(result.get("can_post_messages", False)),
            can_delete_messages=bool(result.get("can_delete_messages", False)),
            is_bot=bool(user.get("is_bot", False)),
            user_id=user.get("id"),
        )

    # -- writes ------------------------------------------------------------

    def send_text(
        self, chat_id: int, text: str, *, buttons=None, disable_preview: bool = False
    ) -> SentMessage:
        result = self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=disable_preview,
            reply_markup=_markup(buttons),
        )
        return _sent(result)

    def send_photo(self, chat_id: int, photo: str, *, caption=None, buttons=None) -> SentMessage:
        result = self._call(
            "sendPhoto",
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=_markup(buttons),
        )
        return _sent(result)

    def send_video(self, chat_id: int, video: str, *, caption=None, buttons=None) -> SentMessage:
        result = self._call(
            "sendVideo",
            chat_id=chat_id,
            video=video,
            caption=caption,
            parse_mode="HTML",
            reply_markup=_markup(buttons),
        )
        return _sent(result)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            return bool(self._call("deleteMessage", chat_id=chat_id, message_id=message_id))
        except TelegramError:
            # A post older than Telegram's deletion window, or already gone.
            return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _markup(buttons: list[dict] | None) -> dict | None:
    """Inline URL keyboard. URL buttons are our only measurable click surface."""
    if not buttons:
        return None
    return {"inline_keyboard": [[b] for b in buttons]}


def _sent(result: dict) -> SentMessage:
    chat = result.get("chat", {})
    chat_id = int(chat.get("id", 0))
    message_id = int(result["message_id"])
    username = chat.get("username")
    link = f"https://t.me/{username}/{message_id}" if username else None
    return SentMessage(telegram_message_id=message_id, telegram_chat_id=chat_id, link=link)


class FakeTelegramGateway:
    """In-memory gateway for tests and dry runs.

    Records everything it was asked to send so assertions can check that the
    delivery engine posts exactly once per delivery.
    """

    def __init__(self, bot_id: int = 999_000_111) -> None:
        self.bot_id = bot_id
        self.sent: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.chats: dict[int, ChatInfo] = {}
        self.members: dict[tuple[int, int], MemberInfo] = {}
        self.member_counts: dict[int, int] = {}
        self._next_message_id = 1000
        self.fail_next_send: str | None = None

    def get_me_id(self) -> int:
        return self.bot_id

    def register_chat(
        self,
        chat_id: int,
        *,
        title="Fake Channel",
        username="fakechan",
        chat_type="channel",
        members=10_000,
        bot_admin=True,
        owner_id: int | None = None,
    ) -> ChatInfo:
        info = ChatInfo(chat_id, chat_type, title, username)
        self.chats[chat_id] = info
        self.member_counts[chat_id] = members
        self.members[(chat_id, self.bot_id)] = MemberInfo(
            "administrator" if bot_admin else "member",
            can_post_messages=bot_admin,
            can_delete_messages=bot_admin,
            is_bot=True,
            user_id=self.bot_id,
        )
        if owner_id is not None:
            self.members[(chat_id, owner_id)] = MemberInfo("creator", user_id=owner_id)
        return info

    def get_chat(self, chat: int | str) -> ChatInfo:
        if isinstance(chat, str):
            for info in self.chats.values():
                if info.username and chat.lstrip("@") == info.username:
                    return info
            raise ChatNotFound(f"no such chat {chat}")
        if chat not in self.chats:
            raise ChatNotFound(f"no such chat {chat}")
        return self.chats[chat]

    def get_chat_member_count(self, chat: int | str) -> int:
        return self.member_counts.get(self.get_chat(chat).telegram_chat_id, 0)

    def get_chat_member(self, chat: int | str, user_id: int) -> MemberInfo:
        chat_id = self.get_chat(chat).telegram_chat_id
        return self.members.get((chat_id, user_id), MemberInfo("left", user_id=user_id))

    def _send(self, chat_id: int, kind: str, **kw) -> SentMessage:
        if self.fail_next_send:
            reason, self.fail_next_send = self.fail_next_send, None
            raise TelegramError(reason)
        self._next_message_id += 1
        self.sent.append({"chat_id": chat_id, "kind": kind, **kw})
        info = self.chats.get(chat_id)
        username = info.username if info else None
        return SentMessage(
            self._next_message_id,
            chat_id,
            f"https://t.me/{username}/{self._next_message_id}" if username else None,
        )

    def send_text(self, chat_id, text, *, buttons=None, disable_preview=False):
        return self._send(chat_id, "text", text=text, buttons=buttons)

    def send_photo(self, chat_id, photo, *, caption=None, buttons=None):
        return self._send(chat_id, "photo", photo=photo, caption=caption, buttons=buttons)

    def send_video(self, chat_id, video, *, caption=None, buttons=None):
        return self._send(chat_id, "video", video=video, caption=caption, buttons=buttons)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True


_gateway: TelegramGateway | None = None


def get_gateway() -> TelegramGateway:
    global _gateway
    if _gateway is None:
        _gateway = HttpTelegramGateway()
    return _gateway


def set_gateway(gateway: TelegramGateway | None) -> None:
    """Injection point for tests and for a dry-run worker."""
    global _gateway
    _gateway = gateway
