"""The bot handlers, driven through aiogram's dispatcher.

Updates are fed in exactly as Telegram would deliver them, and the outgoing API
calls are captured by a recording session. No network is involved, so these tests
verify the thing that actually matters: that /start produces a menu, that the
campaign wizard walks through its steps and creates a real campaign, and that
publisher onboarding refuses a channel the user does not administer.
"""

from __future__ import annotations

from datetime import datetime, timezone, UTC
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    DeleteWebhook,
    EditMessageText,
    GetMe,
    SendMessage,
    SetMyCommands,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User as TgUser,
)

BOT_ID = 999_000_111
TOKEN = f"{BOT_ID}:TEST-TOKEN-FOR-DISPATCHER"


class RecordingSession(BaseSession):
    """Captures outgoing API calls and returns plausible canned results."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self._message_id = 1000

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return TgUser(id=BOT_ID, is_bot=True, first_name="AdNet", username="adnet_test_bot")
        if isinstance(method, SendMessage):
            self._message_id += 1
            return Message(
                message_id=self._message_id,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, EditMessageText):
            return Message(
                message_id=getattr(method, "message_id", 1) or 1,
                date=datetime.now(UTC),
                chat=Chat(id=getattr(method, "chat_id", 1) or 1, type="private"),
                text=method.text,
            )
        if isinstance(method, (SetMyCommands, DeleteWebhook)):
            return True
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    # -- assertions helpers ------------------------------------------------

    @property
    def sent(self) -> list[Any]:
        """Every message the bot showed the user, sent or edited."""
        return [c for c in self.calls if isinstance(c, (SendMessage, EditMessageText))]

    @property
    def last_text(self) -> str:
        assert self.sent, "the bot replied with nothing"
        return self.sent[-1].text or ""

    @property
    def last_buttons(self) -> list[str]:
        markup = getattr(self.sent[-1], "reply_markup", None)
        if markup is None or not getattr(markup, "inline_keyboard", None):
            return []
        return [b.callback_data or b.url or "" for row in markup.inline_keyboard for b in row]

    def texts(self) -> str:
        return "\n".join(m.text or "" for m in self.sent)

    def clear(self) -> None:
        self.calls.clear()


@pytest.fixture(scope="session")
def _dispatcher():
    """One Dispatcher for the whole session.

    aiogram attaches a Router to exactly one Dispatcher and refuses a second, and
    the handler modules expose module-level routers. So the Dispatcher is built
    once; per-test isolation comes from swapping its FSM storage below.
    """
    import app.bot.dispatcher as dispatcher_mod

    dispatcher_mod._dispatcher = None
    dispatcher_mod._storage = lambda: MemoryStorage()
    dispatcher = dispatcher_mod.get_dispatcher()
    yield dispatcher
    dispatcher_mod._dispatcher = None


@pytest.fixture
def tg(db, engine, monkeypatch, gateway_fixture, _dispatcher):
    """A bot wired to the test database, the fake Telegram gateway and clean FSM state."""
    from sqlalchemy.orm import sessionmaker

    import app.bot.dispatcher as dispatcher_mod
    import app.db.session as session_mod
    from app.services.telegram_gateway import set_gateway

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(session_mod, "SessionLocal", factory)
    set_gateway(gateway_fixture)

    session = RecordingSession()
    bot = Bot(token=TOKEN, session=session)
    monkeypatch.setattr(dispatcher_mod, "_bot", bot)

    dispatcher = _dispatcher
    # Fresh wizard state per test, without rebuilding the Dispatcher.
    dispatcher.fsm.storage = MemoryStorage()

    class Harness:
        def __init__(self) -> None:
            self.bot = bot
            self.session = session
            self.dispatcher = dispatcher
            self.update_id = 0
            self.user = TgUser(id=555_000_777, is_bot=False, first_name="Rahim", username="rahim")
            self.chat = Chat(id=555_000_777, type="private")
            self.message_id = 500

        async def send(self, text: str):
            """Deliver a text message from the user."""
            self.update_id += 1
            self.message_id += 1
            update = Update(
                update_id=self.update_id,
                message=Message(
                    message_id=self.message_id,
                    date=datetime.now(UTC),
                    chat=self.chat,
                    from_user=self.user,
                    text=text,
                ),
            )
            await dispatcher.feed_update(bot, update)
            return session

        async def tap(self, data: str):
            """Deliver a button press."""
            self.update_id += 1
            update = Update(
                update_id=self.update_id,
                callback_query=CallbackQuery(
                    id=str(self.update_id),
                    from_user=self.user,
                    chat_instance="test",
                    data=data,
                    message=Message(
                        message_id=self.message_id,
                        date=datetime.now(UTC),
                        chat=self.chat,
                        text="previous screen",
                    ),
                ),
            )
            await dispatcher.feed_update(bot, update)
            return session

    yield Harness()
    set_gateway(None)


# --------------------------------------------------------------------------
# /start and the main menu (spec §2, §3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_greets_and_offers_both_roles(tg, db):
    """The first thing a new user sees (spec §3)."""
    await tg.send("/start")

    assert "Welcome" in tg.session.last_text
    assert "AdNet" in tg.session.last_text
    buttons = tg.session.last_buttons
    assert "role:advertiser" in buttons
    assert "role:publisher" in buttons


@pytest.mark.asyncio
async def test_start_creates_the_user_keyed_on_telegram_id(tg, db):
    """Spec §2: identity is the numeric id, not the username."""
    from sqlalchemy import select

    from app.models.identity import User

    await tg.send("/start")
    db.expire_all()
    user = db.scalars(select(User).where(User.telegram_user_id == 555_000_777)).one()
    assert user.username == "rahim"
    assert user.first_name == "Rahim"
    assert user.signup_source == "bot"


@pytest.mark.asyncio
async def test_a_changed_username_does_not_create_a_second_account(tg, db):
    """A username can be transferred; the numeric id cannot."""
    from sqlalchemy import func, select

    from app.models.identity import User

    await tg.send("/start")
    tg.user = TgUser(id=555_000_777, is_bot=False, first_name="Rahim", username="rahim_new_handle")
    await tg.send("/start")

    db.expire_all()
    assert db.scalar(select(func.count(User.id))) == 1
    user = db.scalars(select(User)).one()
    assert user.username == "rahim_new_handle"


@pytest.mark.asyncio
async def test_choosing_advertiser_creates_the_profile_and_wallet(tg, db):
    from sqlalchemy import select

    from app.models.identity import Advertiser

    await tg.send("/start")
    tg.session.clear()
    await tg.tap("role:advertiser")

    assert "Advertiser" in tg.session.last_text
    assert "Available balance" in tg.session.last_text
    assert "campaign:new" in tg.session.last_buttons

    db.expire_all()
    advertiser = db.scalars(select(Advertiser)).one()
    assert advertiser.wallet is not None
    assert advertiser.currency == "BDT"


@pytest.mark.asyncio
async def test_choosing_publisher_creates_the_profile(tg, db):
    from sqlalchemy import select

    from app.models.identity import Publisher

    await tg.send("/start")
    tg.session.clear()
    await tg.tap("role:publisher")

    assert "Publisher" in tg.session.last_text
    assert "channel:add" in tg.session.last_buttons
    db.expire_all()
    assert db.scalars(select(Publisher)).one().wallet is not None


@pytest.mark.asyncio
async def test_help_lists_the_commands_and_explains_billing(tg, db):
    await tg.send("/help")
    text = tg.session.last_text
    for command in ("/wallet", "/campaign", "/withdraw", "/earnings"):
        assert command in text
    # Spec §37: be honest about measurement in user-facing copy.
    assert "independently measure" in text or "measured" in text


@pytest.mark.asyncio
async def test_wallet_shows_both_sides_for_a_dual_role_user(tg, db):
    await tg.send("/start")
    await tg.tap("role:advertiser")
    await tg.tap("role:publisher")
    tg.session.clear()
    await tg.send("/wallet")

    text = tg.session.last_text
    assert "Advertiser wallet" in text
    assert "Publisher earnings" in text
    assert "withdrawable" in text.lower()


@pytest.mark.asyncio
async def test_wallet_tells_an_unregistered_user_what_to_do(tg, db):
    await tg.send("/wallet")
    assert "not registered" in tg.session.last_text


@pytest.mark.asyncio
async def test_profile_shows_the_permanent_telegram_id(tg, db):
    await tg.send("/start")
    tg.session.clear()
    await tg.send("/profile")
    assert "555000777" in tg.session.last_text.replace(",", "")


# --------------------------------------------------------------------------
# The campaign wizard (spec §3)
# --------------------------------------------------------------------------


async def _become_funded_advertiser(tg, db, amount="50000"):
    from sqlalchemy import select

    from app.models.identity import Advertiser
    from app.services.wallet import WalletService

    await tg.send("/start")
    await tg.tap("role:advertiser")
    db.expire_all()
    advertiser = db.scalars(select(Advertiser)).one()
    WalletService(db).credit_deposit(
        advertiser.id, amount, idempotency_key=f"bot-test-{advertiser.id}"
    )
    db.commit()
    return advertiser


@pytest.mark.asyncio
async def test_the_wizard_refuses_to_start_without_funds(tg, db):
    """Told up front, rather than after seven steps of work."""
    await tg.send("/start")
    await tg.tap("role:advertiser")
    tg.session.clear()
    await tg.tap("campaign:new")

    text = tg.session.last_text
    assert "minimum campaign budget" in text
    assert "Add funds" in text or "add funds" in text


@pytest.mark.asyncio
async def test_the_wizard_walks_seven_steps_and_creates_a_campaign(tg, db):
    """The full flow of spec §3, driven exactly as a user would."""
    from sqlalchemy import select

    from app.models.campaigns import Campaign
    from app.models.enums import CampaignStatus

    await _become_funded_advertiser(tg, db)
    tg.session.clear()

    await tg.tap("campaign:new")
    assert "Step 1" in tg.session.last_text

    await tg.send("Exam Preparation 2026")
    assert "Step 2" in tg.session.last_text

    await tg.send("Join our exam preparation course before the deadline")
    assert "destination link" in tg.session.last_text

    await tg.send("https://example.com/exam-2026")
    assert "button text" in tg.session.last_text.lower()

    await tg.send("Enrol now")
    assert "Step 3" in tg.session.last_text

    await tg.send("10000")
    assert "daily budget" in tg.session.last_text

    await tg.send("2000")
    assert "Step 4" in tg.session.last_text
    assert "pmodel:cpm" in tg.session.last_buttons

    await tg.tap("pmodel:cpm")
    assert "your bid" in tg.session.last_text.lower()

    await tg.send("50")
    assert "Step 5" in tg.session.last_text

    await tg.send("BD")
    assert "categories" in tg.session.last_text.lower()

    await tg.send("education")
    assert "Step 6" in tg.session.last_text

    await tg.send("7")
    preview = tg.session.last_text
    assert "Campaign preview" in preview
    assert "Exam Preparation 2026" in preview
    # Spec §6: ৳10,000 at ৳50 CPM is 200,000 impressions — and it must be
    # presented as an estimate, not a promise (spec §37).
    assert "200,000" in preview
    assert "estimate" in preview.lower()
    assert "campaign:confirm" in tg.session.last_buttons

    db.expire_all()
    campaign = db.scalars(select(Campaign)).one()
    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.name == "Exam Preparation 2026"

    await tg.tap("campaign:confirm")
    assert "submitted for review" in tg.session.last_text.lower()
    db.expire_all()
    assert db.scalars(select(Campaign)).one().status is CampaignStatus.SUBMITTED


@pytest.mark.asyncio
async def test_the_wizard_rejects_a_dangerous_link(tg, db):
    await _become_funded_advertiser(tg, db)
    await tg.tap("campaign:new")
    await tg.send("Test campaign")
    await tg.send("Some advertisement text here")
    tg.session.clear()

    await tg.send("javascript:alert(1)")
    assert "https://" in tg.session.last_text  # asked again, with guidance
    assert "Step 3" not in tg.session.last_text  # did not advance


@pytest.mark.asyncio
async def test_the_wizard_rejects_a_budget_above_the_balance(tg, db):
    await _become_funded_advertiser(tg, db, amount="5000")
    await tg.tap("campaign:new")
    await tg.send("Test campaign")
    await tg.send("Some advertisement text here")
    await tg.send("skip")
    tg.session.clear()

    await tg.send("999999")
    assert "available balance" in tg.session.last_text.lower()


@pytest.mark.asyncio
async def test_the_wizard_rejects_a_non_numeric_budget(tg, db):
    await _become_funded_advertiser(tg, db)
    await tg.tap("campaign:new")
    await tg.send("Test campaign")
    await tg.send("Some advertisement text here")
    await tg.send("skip")
    tg.session.clear()

    await tg.send("onek taka")
    assert "Send a number" in tg.session.last_text


@pytest.mark.asyncio
async def test_cancelling_the_wizard_charges_nothing(tg, db):
    from sqlalchemy import func, select

    from app.models.campaigns import Campaign

    await _become_funded_advertiser(tg, db)
    await tg.tap("campaign:new")
    await tg.send("Half-finished campaign")
    tg.session.clear()

    await tg.tap("campaign:cancel")
    assert "Nothing was charged" in tg.session.last_text
    db.expire_all()
    assert db.scalar(select(func.count(Campaign.id))) == 0


@pytest.mark.asyncio
async def test_my_campaigns_lists_them(tg, db):
    await _become_funded_advertiser(tg, db)
    from app.services.campaigns import CampaignDraft, CampaignService
    from tests.test_campaigns import _draft  # reuse the validated draft

    from sqlalchemy import select

    from app.models.identity import Advertiser

    advertiser = db.scalars(select(Advertiser)).one()
    CampaignService(db).create(advertiser, _draft(name="Ramadan Sale"))
    db.commit()
    tg.session.clear()

    await tg.send("/campaigns")
    assert "Ramadan Sale" in tg.session.last_text
    assert any("campaign:view:" in b for b in tg.session.last_buttons)


# --------------------------------------------------------------------------
# Publisher onboarding (spec §4)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_channel_explains_the_two_steps(tg, db):
    await tg.send("/start")
    await tg.tap("role:publisher")
    tg.session.clear()
    await tg.tap("channel:add")

    text = tg.session.last_text
    assert "administrator" in text
    assert "post messages" in text.lower()


@pytest.mark.asyncio
async def test_registering_a_channel_you_do_not_administer_is_refused(tg, db):
    """The core abuse case, through the bot (spec §4)."""
    from sqlalchemy import func, select

    from app.models.telegram import PublisherChannel

    await tg.send("/start")
    await tg.tap("role:publisher")
    await tg.tap("channel:add")
    # The bot is an admin there, but someone else owns the chat.
    from app.services.telegram_gateway import get_gateway

    get_gateway().register_chat(-100_555, username="notmine", owner_id=424_242)
    tg.session.clear()

    await tg.send("@notmine")
    assert "not an owner or administrator" in tg.session.last_text
    db.expire_all()
    assert db.scalar(select(func.count(PublisherChannel.id))) == 0


@pytest.mark.asyncio
async def test_registering_your_own_channel_succeeds_and_asks_for_a_category(tg, db):
    from sqlalchemy import select

    from app.models.telegram import PublisherChannel
    from app.services.telegram_gateway import get_gateway

    await tg.send("/start")
    await tg.tap("role:publisher")
    await tg.tap("channel:add")
    get_gateway().register_chat(
        -100_556,
        username="examchannel",
        title="Exam Channel",
        members=42_000,
        owner_id=tg.user.id,
    )
    tg.session.clear()

    await tg.send("@examchannel")
    assert "Verified" in tg.session.last_text
    assert "category" in tg.session.last_text.lower()

    await tg.send("education")
    assert "active" in tg.session.last_text
    # Spec §37 honesty, restated where the publisher will read it.
    assert "measured impression" in tg.session.last_text

    db.expire_all()
    channel = db.scalars(select(PublisherChannel)).one()
    assert channel.telegram_chat_id == -100_556
    assert channel.category == "education"


@pytest.mark.asyncio
async def test_a_bad_channel_name_lets_the_user_retry(tg, db):
    await tg.send("/start")
    await tg.tap("role:publisher")
    await tg.tap("channel:add")
    tg.session.clear()

    await tg.send("@channel_that_does_not_exist")
    assert "can't see that chat" in tg.session.last_text


# --------------------------------------------------------------------------
# Earnings and withdrawal (spec §12, §13)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earnings_separates_pending_from_withdrawable(tg, db):
    await tg.send("/start")
    await tg.tap("role:publisher")
    tg.session.clear()
    await tg.send("/earnings")

    text = tg.session.last_text
    assert "Pending validation" in text
    assert "withdrawable" in text.lower()
    assert "validation before becoming withdrawable" in text


@pytest.mark.asyncio
async def test_withdraw_refuses_below_the_minimum_and_explains_pending(tg, db):
    await tg.send("/start")
    await tg.tap("role:publisher")
    tg.session.clear()
    await tg.send("/withdraw")

    text = tg.session.last_text
    assert "Minimum withdrawal" in text
    assert "not yet withdrawable" in text


# --------------------------------------------------------------------------
# Staff area
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_area_is_closed_to_ordinary_users(tg, db):
    await tg.send("/start")
    tg.session.clear()
    await tg.send("/admin")
    assert "platform staff" in tg.session.last_text


@pytest.mark.asyncio
async def test_admin_area_opens_for_linked_staff(tg, db, make_staff):
    """Staff access needs a StaffUser row linked to this Telegram id."""
    make_staff(telegram_user_id=tg.user.id)
    db.commit()
    await tg.send("/start")
    tg.session.clear()
    await tg.send("/admin")

    text = tg.session.last_text
    assert "Admin" in text
    assert "Platform revenue" in text


@pytest.mark.asyncio
async def test_report_command_explains_the_reasons(tg, db):
    await tg.send("/report")
    text = tg.session.last_text
    assert "scam" in text and "malware" in text
    assert "moderator" in text.lower()


# --------------------------------------------------------------------------
# Suspension
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_suspended_user_is_not_served(tg, db):
    """The handler raises SuspendedAccount, which the webhook swallows quietly."""
    from sqlalchemy import select

    from app.core.errors import SuspendedAccount
    from app.models.enums import UserStatus
    from app.models.identity import User

    await tg.send("/start")
    db.expire_all()
    user = db.scalars(select(User)).one()
    user.status = UserStatus.SUSPENDED
    db.commit()
    tg.session.clear()

    with pytest.raises(SuspendedAccount):
        await tg.send("/wallet")
    assert tg.session.sent == []
