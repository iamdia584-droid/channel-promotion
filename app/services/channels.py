"""Publisher channel registration and ownership verification (spec §4, §20).

A submitted username proves nothing — anyone can type ``@someoneelseschannel``.
Verification requires two independent facts from Telegram itself:

1. **Our bot is an administrator** of the chat with the rights it needs to post.
2. **The claimant is the creator or an administrator** of that same chat.

Both come from ``getChatMember``, which is authoritative. Every attempt — pass
or fail — is recorded in ``channel_verification_attempts`` so a disputed or
hijacked registration can be reconstructed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.db.base import utcnow
from app.models.enums import ChannelStatus, ChatType, MeasurementMode, VerificationStatus
from app.models.identity import Publisher, User
from app.models.telegram import (
    ChannelVerificationAttempt,
    PublisherChannel,
    TelegramChat,
)
from app.services.settings_service import SettingsService
from app.services.telegram_gateway import (
    ChatNotFound,
    MemberInfo,
    TelegramError,
    TelegramGateway,
    get_gateway,
)

#: Chat types we can monetise. A private chat is not inventory.
SUPPORTED_TYPES = {"channel", "group", "supergroup"}


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    evidence: dict
    channel: PublisherChannel | None = None

    @property
    def ok(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    def user_message(self) -> str:
        """Actionable guidance, not a bare rejection."""
        return {
            VerificationStatus.VERIFIED: "Ownership verified.",
            VerificationStatus.BOT_NOT_ADMIN: (
                "I'm not an administrator there yet. Add me as an admin with "
                "permission to post messages, then try again."
            ),
            VerificationStatus.CLAIMANT_NOT_ADMIN: (
                "Your Telegram account is not an owner or administrator of that "
                "chat, so it cannot be registered under your account."
            ),
            VerificationStatus.INSUFFICIENT_RIGHTS: (
                "I'm an admin there but cannot post messages. Enable the "
                "'Post Messages' permission for me and try again."
            ),
            VerificationStatus.REVOKED: "My admin rights there were removed.",
            VerificationStatus.UNVERIFIED: "Verification could not be completed.",
        }[self.status]


class ChannelService:
    def __init__(self, session: Session, gateway: TelegramGateway | None = None) -> None:
        self.session = session
        self.gateway = gateway or get_gateway()
        self.settings = SettingsService(session)

    # -- registration ------------------------------------------------------

    def register(
        self,
        publisher: Publisher,
        identifier: str,
        *,
        category: str | None = None,
        language: str | None = None,
        country: str | None = None,
    ) -> VerificationResult:
        """Resolve, verify and register a chat. Idempotent per (publisher, chat)."""
        identifier = _normalise(identifier)
        if not identifier:
            raise ValidationFailed("Send a channel username like @mychannel, or its invite link.")

        claimant = self.session.get(User, publisher.user_id)
        if claimant is None:  # pragma: no cover
            raise NotFound("publisher user missing")

        try:
            info = self.gateway.get_chat(identifier)
        except ChatNotFound:
            self._record_attempt(publisher, identifier, None, VerificationStatus.UNVERIFIED,
                                 {"error": "chat_not_found", "submitted": identifier})
            raise NotFound(
                "I can't see that chat. Check the username, and make sure I've been "
                "added to it."
            ) from None
        except TelegramError as exc:
            self._record_attempt(publisher, identifier, None, VerificationStatus.UNVERIFIED,
                                 {"error": str(exc), "submitted": identifier})
            raise

        if info.chat_type not in SUPPORTED_TYPES:
            raise ValidationFailed(
                f"A {info.chat_type} cannot be registered. Channels, groups and "
                "supergroups are supported."
            )

        chat = self._upsert_chat(info)
        if chat.is_blacklisted:
            raise PermissionDenied(
                "That chat is blocked from the network.", reason=chat.blacklist_reason
            )

        # A chat already claimed by a *different* publisher is a conflict, not a
        # silent re-assignment: whoever verified first holds the inventory.
        existing = self.session.scalars(
            select(PublisherChannel).where(
                PublisherChannel.telegram_chat_id == info.telegram_chat_id
            )
        ).one_or_none()
        if existing is not None and existing.publisher_id != publisher.id:
            raise Conflict(
                "That chat is already registered by another publisher. Contact "
                "support if you believe this is wrong."
            )

        result = self.verify_chat(info.telegram_chat_id, claimant.telegram_user_id)
        self._record_attempt(
            publisher, identifier, info.telegram_chat_id, result.status, result.evidence
        )
        if not result.ok:
            return result

        # Verified inventory either goes live at once or waits for a human,
        # depending on the admin setting (spec §20).
        approved_status = (
            ChannelStatus.ACTIVE
            if self.settings.bool_("channel_auto_approve")
            else ChannelStatus.VERIFIED
        )
        channel = existing or PublisherChannel(
            publisher_id=publisher.id,
            telegram_chat_id_ref=chat.id,
            telegram_chat_id=info.telegram_chat_id,
            measurement_mode=MeasurementMode.CLICK_ONLY,
            # Set explicitly: a column default is only applied at flush, so
            # relying on it here would leave status None for the check below.
            status=approved_status,
        )
        channel.category = (category or channel.category or "").lower() or None
        channel.language = (language or channel.language or "").lower() or None
        channel.country = (country or channel.country or "").upper() or None
        channel.verification_status = VerificationStatus.VERIFIED
        channel.verified_at = utcnow()
        channel.verification_evidence = result.evidence
        channel.rejection_reason = None
        if channel.status in (ChannelStatus.PENDING, ChannelStatus.REJECTED):
            channel.status = approved_status
        if existing is None:
            self.session.add(channel)
        self.session.flush()

        self.refresh_member_count(channel)
        return VerificationResult(VerificationStatus.VERIFIED, result.evidence, channel)

    # -- verification ------------------------------------------------------

    def verify_chat(self, telegram_chat_id: int, claimant_telegram_id: int) -> VerificationResult:
        """The actual proof. Both facts must hold; neither is inferred."""
        bot_id = self.gateway.get_me_id()
        bot = self._safe_member(telegram_chat_id, bot_id)
        claimant = self._safe_member(telegram_chat_id, claimant_telegram_id)
        chat_type = self.gateway.get_chat(telegram_chat_id).chat_type

        evidence = {
            "checked_at": utcnow().isoformat(),
            "telegram_chat_id": telegram_chat_id,
            "chat_type": chat_type,
            "bot_status": bot.status,
            "bot_can_post_messages": bot.can_post_messages,
            "bot_can_delete_messages": bot.can_delete_messages,
            "claimant_telegram_user_id": claimant_telegram_id,
            "claimant_status": claimant.status,
            "method": "getChatMember",
        }

        if not bot.is_admin:
            return VerificationResult(VerificationStatus.BOT_NOT_ADMIN, evidence)
        # Channels gate posting behind can_post_messages; in groups an admin can
        # post by default and Telegram does not set that flag at all.
        if chat_type == "channel" and not bot.can_post_messages:
            return VerificationResult(VerificationStatus.INSUFFICIENT_RIGHTS, evidence)
        if not claimant.is_admin:
            return VerificationResult(VerificationStatus.CLAIMANT_NOT_ADMIN, evidence)
        return VerificationResult(VerificationStatus.VERIFIED, evidence)

    def revalidate(self, channel: PublisherChannel) -> VerificationResult:
        """Re-check rights periodically. Publishers do remove the bot later."""
        publisher = self.session.get(Publisher, channel.publisher_id)
        user = self.session.get(User, publisher.user_id) if publisher else None
        if user is None:  # pragma: no cover
            raise NotFound("publisher user missing")
        result = self.verify_chat(channel.telegram_chat_id, user.telegram_user_id)
        if result.ok:
            channel.verification_status = VerificationStatus.VERIFIED
            channel.verification_evidence = result.evidence
            if channel.status is ChannelStatus.PAUSED and channel.rejection_reason == "rights_lost":
                channel.status = ChannelStatus.ACTIVE
                channel.rejection_reason = None
        else:
            # Losing rights pauses inventory; it does not destroy the record or
            # the publisher's accrued earnings.
            channel.verification_status = VerificationStatus.REVOKED
            if channel.status.can_serve:
                channel.status = ChannelStatus.PAUSED
                channel.rejection_reason = "rights_lost"
        self.session.flush()
        return result

    # -- chat facts --------------------------------------------------------

    def _upsert_chat(self, info) -> TelegramChat:
        chat = self.session.scalars(
            select(TelegramChat).where(TelegramChat.telegram_chat_id == info.telegram_chat_id)
        ).one_or_none()
        if chat is None:
            chat = TelegramChat(
                telegram_chat_id=info.telegram_chat_id,
                chat_type=ChatType(info.chat_type),
                first_seen_at=utcnow(),
            )
            self.session.add(chat)
        chat.username = info.username
        chat.title = info.title
        chat.description = info.description
        chat.invite_link = info.invite_link
        self.session.flush()
        return chat

    def refresh_member_count(self, channel: PublisherChannel) -> int:
        """Member count is display/targeting metadata only — never a billing basis."""
        try:
            count = self.gateway.get_chat_member_count(channel.telegram_chat_id)
        except TelegramError:
            return channel.chat.member_count if channel.chat else 0
        chat = self.session.get(TelegramChat, channel.telegram_chat_id_ref)
        if chat is not None:
            chat.member_count = count
            chat.member_count_checked_at = utcnow()
            self.session.flush()
        return count

    def refresh_bot_rights(self, channel: PublisherChannel) -> MemberInfo:
        bot = self._safe_member(channel.telegram_chat_id, self.gateway.get_me_id())
        chat = self.session.get(TelegramChat, channel.telegram_chat_id_ref)
        if chat is not None:
            chat.bot_is_admin = bot.is_admin
            chat.bot_can_post = bot.can_post_messages or (
                bot.is_admin and chat.chat_type is not ChatType.CHANNEL
            )
            chat.bot_can_delete = bot.can_delete_messages
            chat.bot_rights_checked_at = utcnow()
            self.session.flush()
        return bot

    def _safe_member(self, chat_id: int, user_id: int) -> MemberInfo:
        try:
            return self.gateway.get_chat_member(chat_id, user_id)
        except TelegramError:
            return MemberInfo(status="left", user_id=user_id)

    def _record_attempt(
        self,
        publisher: Publisher,
        identifier: str,
        chat_id: int | None,
        result: VerificationStatus,
        evidence: dict,
    ) -> None:
        self.session.add(
            ChannelVerificationAttempt(
                publisher_id=publisher.id,
                submitted_identifier=identifier[:300],
                telegram_chat_id=chat_id,
                result=result,
                evidence=evidence,
            )
        )
        self.session.flush()

    # -- publisher self-service -------------------------------------------

    def set_status(
        self, channel: PublisherChannel, status: ChannelStatus, reason: str | None = None
    ) -> PublisherChannel:
        channel.status = status
        if reason:
            channel.rejection_reason = reason
        self.session.flush()
        return channel

    def list_for_publisher(self, publisher_id: uuid.UUID) -> list[PublisherChannel]:
        return list(
            self.session.scalars(
                select(PublisherChannel)
                .where(PublisherChannel.publisher_id == publisher_id)
                .order_by(PublisherChannel.created_at.desc())
            ).all()
        )


def _normalise(identifier: str) -> str:
    """Accept @name, t.me/name, https://t.me/name, or a raw numeric chat id."""
    value = (identifier or "").strip()
    if not value:
        return ""
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "telegram.me/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.split("?")[0].strip("/")
    if value.startswith("@"):
        return value
    if value.lstrip("-").isdigit():
        return value
    return f"@{value}" if value else ""
