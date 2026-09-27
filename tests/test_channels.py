"""Ownership verification must be proven by Telegram, never taken on trust."""

from __future__ import annotations

import pytest

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.models.enums import ChannelStatus, VerificationStatus
from app.models.identity import User
from app.models.telegram import ChannelVerificationAttempt, TelegramChat
from app.services.channels import ChannelService, _normalise
from app.services.settings_service import SettingsService
from app.services.telegram_gateway import FakeTelegramGateway


@pytest.fixture
def gateway():
    return FakeTelegramGateway()


@pytest.fixture
def service(db, gateway):
    return ChannelService(db, gateway)


def _owner_tg(db, publisher) -> int:
    return db.get(User, publisher.user_id).telegram_user_id


def test_submitting_someone_elses_channel_is_rejected(db, service, gateway, make_publisher):
    """The core abuse case: claiming a channel you do not administer."""
    publisher = make_publisher()
    # The bot is an admin, but the claimant is a nobody in that chat.
    gateway.register_chat(-1001, username="notmine", owner_id=777_777)

    result = service.register(publisher, "@notmine")
    assert result.status is VerificationStatus.CLAIMANT_NOT_ADMIN
    assert result.ok is False
    assert "not an owner or administrator" in result.user_message()
    # No channel row was created.
    assert service.list_for_publisher(publisher.id) == []


def test_bot_not_admin_is_rejected_with_actionable_guidance(
    db, service, gateway, make_publisher
):
    publisher = make_publisher()
    gateway.register_chat(
        -1002, username="noadmin", bot_admin=False, owner_id=_owner_tg(db, publisher)
    )
    result = service.register(publisher, "@noadmin")
    assert result.status is VerificationStatus.BOT_NOT_ADMIN
    assert "administrator" in result.user_message()


def test_admin_without_post_rights_is_rejected_for_channels(
    db, service, gateway, make_publisher
):
    from app.services.telegram_gateway import MemberInfo

    publisher = make_publisher()
    owner = _owner_tg(db, publisher)
    gateway.register_chat(-1003, username="norights", owner_id=owner)
    # Admin, but Telegram did not grant can_post_messages.
    gateway.members[(-1003, gateway.bot_id)] = MemberInfo(
        "administrator", can_post_messages=False, is_bot=True, user_id=gateway.bot_id
    )
    result = service.register(publisher, "@norights")
    assert result.status is VerificationStatus.INSUFFICIENT_RIGHTS
    assert "Post Messages" in result.user_message()


def test_group_admin_needs_no_post_messages_flag(db, service, gateway, make_publisher):
    """Telegram does not set can_post_messages for groups; requiring it would
    wrongly reject every group."""
    from app.services.telegram_gateway import MemberInfo

    publisher = make_publisher()
    owner = _owner_tg(db, publisher)
    gateway.register_chat(-1004, username="mygroup", chat_type="supergroup", owner_id=owner)
    gateway.members[(-1004, gateway.bot_id)] = MemberInfo(
        "administrator", can_post_messages=False, is_bot=True, user_id=gateway.bot_id
    )
    result = service.register(publisher, "@mygroup")
    assert result.ok is True


def test_successful_registration_stores_the_facts_spec_requires(
    db, service, gateway, make_publisher
):
    """Spec §4: store chat id, username, title, type, member count, category…"""
    publisher = make_publisher()
    gateway.register_chat(
        -100500, username="examchannel", title="Exam Prep",
        members=42_000, owner_id=_owner_tg(db, publisher),
    )
    result = service.register(publisher, "@examchannel", category="education",
                              language="bn", country="bd")
    assert result.ok
    channel = result.channel
    assert channel.telegram_chat_id == -100500
    assert channel.category == "education"
    assert channel.country == "BD"          # normalised upper
    assert channel.language == "bn"         # normalised lower
    assert channel.verification_status is VerificationStatus.VERIFIED
    assert channel.verified_at is not None
    chat = db.get(TelegramChat, channel.telegram_chat_id_ref)
    assert chat.title == "Exam Prep"
    assert chat.username == "examchannel"
    assert chat.member_count == 42_000
    assert chat.chat_type.value == "channel"


def test_verification_evidence_is_recorded_for_audit(db, service, gateway, make_publisher):
    publisher = make_publisher()
    gateway.register_chat(-100501, username="ch1", owner_id=_owner_tg(db, publisher))
    result = service.register(publisher, "@ch1")
    ev = result.channel.verification_evidence
    assert ev["method"] == "getChatMember"
    assert ev["bot_status"] == "administrator"
    assert ev["claimant_status"] == "creator"
    assert "checked_at" in ev


def test_every_attempt_is_logged_including_failures(db, service, gateway, make_publisher):
    publisher = make_publisher()
    gateway.register_chat(-1006, username="fail1", owner_id=999_999)  # not the claimant
    service.register(publisher, "@fail1")
    gateway.register_chat(-1007, username="ok1", owner_id=_owner_tg(db, publisher))
    service.register(publisher, "@ok1")

    attempts = db.query(ChannelVerificationAttempt).all()
    results = {a.result for a in attempts}
    assert VerificationStatus.CLAIMANT_NOT_ADMIN in results
    assert VerificationStatus.VERIFIED in results
    assert len(attempts) == 2


def test_unknown_chat_is_reported_not_registered(db, service, make_publisher):
    publisher = make_publisher()
    with pytest.raises(NotFound, match="can't see that chat"):
        service.register(publisher, "@doesnotexist")
    assert db.query(ChannelVerificationAttempt).count() == 1


def test_another_publishers_channel_cannot_be_hijacked(
    db, service, gateway, make_publisher
):
    first, second = make_publisher(), make_publisher()
    gateway.register_chat(-1008, username="taken", owner_id=_owner_tg(db, first))
    assert service.register(first, "@taken").ok

    # Make the second publisher a genuine admin too — still must not take it over.
    from app.services.telegram_gateway import MemberInfo
    gateway.members[(-1008, _owner_tg(db, second))] = MemberInfo(
        "administrator", user_id=_owner_tg(db, second)
    )
    with pytest.raises(Conflict, match="already registered by another publisher"):
        service.register(second, "@taken")


def test_re_registering_own_channel_is_idempotent(db, service, gateway, make_publisher):
    publisher = make_publisher()
    gateway.register_chat(-1009, username="mine", owner_id=_owner_tg(db, publisher))
    first = service.register(publisher, "@mine", category="education")
    second = service.register(publisher, "@mine", category="technology")
    assert first.channel.id == second.channel.id
    assert second.channel.category == "technology"
    assert len(service.list_for_publisher(publisher.id)) == 1


def test_blacklisted_chat_is_refused(db, service, gateway, make_publisher):
    publisher = make_publisher()
    gateway.register_chat(-1010, username="bad", owner_id=_owner_tg(db, publisher))
    service.register(publisher, "@bad")
    chat = db.scalars(
        db.query(TelegramChat).filter(TelegramChat.telegram_chat_id == -1010).statement
    ).one()
    chat.is_blacklisted = True
    chat.blacklist_reason = "scam network"
    db.flush()

    with pytest.raises(PermissionDenied, match="blocked from the network"):
        service.register(publisher, "@bad")


def test_private_chat_type_is_refused(db, service, gateway, make_publisher):
    publisher = make_publisher()
    gateway.register_chat(1011, username="dm", chat_type="private",
                          owner_id=_owner_tg(db, publisher))
    with pytest.raises(ValidationFailed, match="cannot be registered"):
        service.register(publisher, "@dm")


def test_auto_approve_setting_controls_initial_status(db, gateway, make_publisher):
    publisher = make_publisher()
    gateway.register_chat(-1012, username="auto", owner_id=_owner_tg(db, publisher))
    SettingsService(db).set("channel_auto_approve", "false")
    result = ChannelService(db, gateway).register(publisher, "@auto")
    # Verified but held for manual admin review (spec §20).
    assert result.channel.status is ChannelStatus.VERIFIED

    publisher2 = make_publisher()
    gateway.register_chat(-1013, username="auto2", owner_id=_owner_tg(db, publisher2))
    SettingsService(db).set("channel_auto_approve", "true")
    result2 = ChannelService(db, gateway).register(publisher2, "@auto2")
    assert result2.channel.status is ChannelStatus.ACTIVE


def test_losing_bot_rights_pauses_inventory_without_destroying_it(
    db, service, gateway, make_publisher
):
    from app.services.telegram_gateway import MemberInfo

    publisher = make_publisher()
    gateway.register_chat(-1014, username="revoke", owner_id=_owner_tg(db, publisher))
    channel = service.register(publisher, "@revoke").channel
    assert channel.status is ChannelStatus.ACTIVE

    # Publisher removes the bot as admin.
    gateway.members[(-1014, gateway.bot_id)] = MemberInfo("member", is_bot=True,
                                                          user_id=gateway.bot_id)
    service.revalidate(channel)
    assert channel.verification_status is VerificationStatus.REVOKED
    assert channel.status is ChannelStatus.PAUSED
    assert channel.id is not None  # record retained, earnings intact

    # Rights restored → inventory resumes.
    gateway.members[(-1014, gateway.bot_id)] = MemberInfo(
        "administrator", can_post_messages=True, is_bot=True, user_id=gateway.bot_id
    )
    service.revalidate(channel)
    assert channel.status is ChannelStatus.ACTIVE
    assert channel.verification_status is VerificationStatus.VERIFIED


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@chan", "@chan"),
        ("chan", "@chan"),
        ("https://t.me/chan", "@chan"),
        ("t.me/chan/", "@chan"),
        ("https://t.me/chan?start=x", "@chan"),
        ("-1001234567890", "-1001234567890"),
        ("   ", ""),
    ],
)
def test_identifier_normalisation(raw, expected):
    assert _normalise(raw) == expected


def test_empty_identifier_is_rejected(db, service, make_publisher):
    with pytest.raises(ValidationFailed):
        service.register(make_publisher(), "   ")
