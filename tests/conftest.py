"""Test fixtures.

Tests run against in-memory SQLite with foreign keys enforced. The financial
code paths are dialect-agnostic: ``FOR UPDATE`` is skipped on SQLite (which
serialises writers anyway) and every invariant that protects money is either a
CHECK/UNIQUE constraint that SQLite also enforces, or explicit service logic.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import pytest

#: Set ``TEST_DATABASE_URL`` to a PostgreSQL DSN to run the suite against the real
#: database. Strongly preferred for the financial tests: SQLite has no NUMERIC
#: type and stores DECIMAL columns as floats, so the CHECK constraints that
#: enforce ``gross = net + commission`` and ``settled <= reserved`` are evaluated
#: on inexact float arithmetic there. Those constraints are a core money
#: guarantee, so verifying them on SQLite alone would be false confidence.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
ON_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

requires_postgres = pytest.mark.skipif(
    not ON_POSTGRES,
    reason="needs PostgreSQL: SQLite stores NUMERIC as float and cannot enforce "
    "exact-decimal CHECK constraints (set TEST_DATABASE_URL)",
)

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.core.money import D
from app.db.base import utcnow
from app.models import Base
from app.models.campaigns import Advertisement, Campaign, CampaignTarget
from app.models.enums import (
    AdStatus,
    CampaignStatus,
    CampaignType,
    ChannelStatus,
    ChatType,
    PricingModel,
    VerificationStatus,
)
from app.models.identity import Advertiser, Publisher, StaffUser, User
from app.models.enums import Role
from app.models.telegram import PublisherChannel, TelegramChat


# --------------------------------------------------------------------------
# Redis stub — the tests must not need a Redis server.
# --------------------------------------------------------------------------


class FakeRedis:
    """Enough of the redis API for locks, counters, rate limits and caching."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        return True

    def setex(self, key, ttl, value):
        self.store[key] = str(value)
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    def incr(self, key):
        return self.incrby(key, 1)

    def incrby(self, key, amount=1):
        new = int(self.store.get(key, 0)) + amount
        self.store[key] = str(new)
        return new

    def expire(self, key, ttl):
        return True

    def eval(self, script, numkeys, *args):
        key = args[0]
        if "incr" in script:  # rate limit script
            return self.incrby(key, 1)
        if "del" in script:  # unlock script
            token = args[1]
            if self.store.get(key) == token:
                return self.delete(key)
            return 0
        raise AssertionError("unexpected script")

    def pipeline(self):
        return FakePipeline(self)

    def ping(self):
        return True


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.ops: list = []

    def incrby(self, key, amount):
        self.ops.append(("incrby", key, amount))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "incrby":
                out.append(self.client.incrby(op[1], op[2]))
            else:
                out.append(True)
        self.ops.clear()
        return out


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import app.core.cache as cache_mod

    client = FakeRedis()
    monkeypatch.setattr(cache_mod, "_client", client)
    monkeypatch.setattr(cache_mod, "get_redis", lambda: client)
    return client


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _pg_engine():
    """One PostgreSQL engine for the session; each test gets a clean schema."""
    eng = create_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    with eng.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def engine(request):
    if ON_POSTGRES:
        eng = request.getfixturevalue("_pg_engine")
        # Truncate rather than recreate: far faster, and it resets sequences.
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        with eng.connect() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            conn.commit()
        yield eng
        return

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _seed_settings(db):
    from app.services.settings_service import SettingsService

    SettingsService(db).seed_defaults()
    db.commit()


# --------------------------------------------------------------------------
# Domain factories
# --------------------------------------------------------------------------

_tg_id = iter(range(10_000_001, 10_999_999))


@pytest.fixture
def make_user(db):
    def _make(**kw) -> User:
        user = User(
            telegram_user_id=kw.pop("telegram_user_id", next(_tg_id)),
            username=kw.pop("username", None),
            first_name=kw.pop("first_name", "Test"),
            **kw,
        )
        db.add(user)
        db.flush()
        return user

    return _make


@pytest.fixture
def make_advertiser(db, make_user):
    def _make(currency: str = "BDT", **kw) -> Advertiser:
        user = make_user(is_advertiser=True)
        adv = Advertiser(user_id=user.id, currency=currency, **kw)
        db.add(adv)
        db.flush()
        return adv

    return _make


@pytest.fixture
def make_publisher(db, make_user):
    def _make(currency: str = "BDT", **kw) -> Publisher:
        user = make_user(is_publisher=True)
        pub = Publisher(user_id=user.id, currency=currency, **kw)
        db.add(pub)
        db.flush()
        return pub

    return _make


@pytest.fixture
def make_staff(db):
    def _make(role: Role = Role.ADMIN, **kw) -> StaffUser:
        staff = StaffUser(
            email=kw.pop("email", f"staff-{uuid.uuid4().hex[:8]}@example.com"),
            password_hash="x" * 32,
            role=role,
            **kw,
        )
        db.add(staff)
        db.flush()
        return staff

    return _make


@pytest.fixture
def make_channel(db, make_publisher):
    def _make(
        publisher: Publisher | None = None,
        *,
        members: int = 50_000,
        avg_views: int = 20_000,
        category: str = "education",
        country: str = "BD",
        language: str = "bn",
        status: ChannelStatus = ChannelStatus.ACTIVE,
        quality: str = "0.7",
        **kw,
    ) -> PublisherChannel:
        publisher = publisher or make_publisher()
        chat = TelegramChat(
            telegram_chat_id=kw.pop("telegram_chat_id", -next(_tg_id)),
            chat_type=ChatType.CHANNEL,
            username=kw.pop("username", f"chan{uuid.uuid4().hex[:6]}"),
            title="Test Channel",
            member_count=members,
            bot_is_admin=True,
            bot_can_post=True,
            bot_can_delete=True,
        )
        db.add(chat)
        db.flush()
        channel = PublisherChannel(
            publisher_id=publisher.id,
            telegram_chat_id_ref=chat.id,
            telegram_chat_id=chat.telegram_chat_id,
            category=category,
            country=country,
            language=language,
            status=status,
            verification_status=VerificationStatus.VERIFIED,
            verified_at=utcnow(),
            avg_views=avg_views,
            median_views=avg_views,
            quality_score=D(quality),
            **kw,
        )
        db.add(channel)
        db.flush()
        return channel

    return _make


@pytest.fixture
def make_campaign(db, make_advertiser):
    def _make(
        advertiser: Advertiser | None = None,
        *,
        total_budget: str = "10000",
        daily_budget: str = "2000",
        bid_cpm: str = "50",
        status: CampaignStatus = CampaignStatus.RUNNING,
        countries: list | None = None,
        categories: list | None = None,
        languages: list | None = None,
        excluded_categories: list | None = None,
        audience_types: list | None = None,
        with_ad: bool = True,
        **kw,
    ) -> Campaign:
        advertiser = advertiser or make_advertiser()
        now = utcnow()
        campaign = Campaign(
            advertiser_id=advertiser.id,
            name=kw.pop("name", "Test Campaign"),
            campaign_type=kw.pop("campaign_type", CampaignType.TEXT),
            pricing_model=kw.pop("pricing_model", PricingModel.CPM),
            currency=advertiser.currency,
            status=status,
            total_budget=D(total_budget),
            daily_budget=D(daily_budget),
            bid_cpm=D(bid_cpm),
            starts_at=kw.pop("starts_at", now - timedelta(hours=1)),
            ends_at=kw.pop("ends_at", now + timedelta(days=7)),
            **kw,
        )
        db.add(campaign)
        db.flush()
        db.add(
            CampaignTarget(
                campaign_id=campaign.id,
                countries=countries if countries is not None else ["BD"],
                categories=categories if categories is not None else ["education"],
                languages=languages if languages is not None else ["bn"],
                excluded_categories=excluded_categories or [],
                audience_types=audience_types or [],
            )
        )
        if with_ad:
            db.add(
                Advertisement(
                    campaign_id=campaign.id,
                    status=AdStatus.APPROVED,
                    ad_format=campaign.campaign_type,
                    body_text="Exam Preparation 2026 - enrol now",
                    destination_url="https://example.com/course",
                    cta_text="Enrol",
                )
            )
        db.flush()
        return campaign

    return _make


@pytest.fixture
def gateway_fixture():
    from app.services.telegram_gateway import FakeTelegramGateway

    return FakeTelegramGateway()


@pytest.fixture
def funded(db):
    """Give an advertiser a funded wallet."""
    from app.services.wallet import WalletService

    def _fund(advertiser, amount="100000"):
        WalletService(db).credit_deposit(
            advertiser.id, amount, idempotency_key=f"fund:{advertiser.id}:{amount}"
        )
        return advertiser

    return _fund


@pytest.fixture
def delivery_engine(db, gateway_fixture):
    from app.services.delivery import DeliveryService

    return DeliveryService(db, gateway_fixture)


@pytest.fixture
def sent_delivery(
    db, gateway_fixture, delivery_engine, make_campaign, make_channel, funded, make_advertiser
):
    """A campaign delivered into a channel, ready to receive impressions."""

    def _make(*, bid_cpm="100", avg_views=20_000, budget="50000", commission="0.20", **kw):
        from app.services.settings_service import SettingsService

        s = SettingsService(db)
        s.set("platform_commission_rate", commission)
        s.set("quality_multiplier_floor", "1.0000")
        s.set("quality_multiplier_ceiling", "1.0000")
        advertiser = funded(make_advertiser(), "200000")
        campaign = make_campaign(
            advertiser, bid_cpm=bid_cpm, total_budget=budget, daily_budget=budget, **kw
        )
        channel = make_channel(avg_views=avg_views)
        gateway_fixture.register_chat(channel.telegram_chat_id, username="c1")
        result = delivery_engine.deliver_to(channel)
        assert result.delivery is not None, result.reason
        return result.delivery, campaign, channel, advertiser

    return _make


@pytest.fixture
def client(engine, monkeypatch, fake_redis):
    """FastAPI TestClient bound to the test database and a fake Telegram gateway."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import app.db.session as session_mod
    from app.services.telegram_gateway import FakeTelegramGateway, set_gateway

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(session_mod, "SessionLocal", factory)

    gateway = FakeTelegramGateway()
    set_gateway(gateway)

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as test_client:
        test_client.gateway = gateway
        yield test_client
    set_gateway(None)


@pytest.fixture
def api_token(db):
    """Issue a signed API session for a user, creating roles as needed."""
    from app.api.deps import issue_session_token

    def _make(*, advertiser=False, publisher=False, telegram_user_id=None):
        from app.api.v1.auth import ensure_advertiser, ensure_publisher
        from app.models.identity import User

        user = User(
            telegram_user_id=telegram_user_id or next(_tg_id),
            first_name="API",
            username="apiuser",
        )
        db.add(user)
        db.flush()
        if advertiser:
            ensure_advertiser(db, user)
        if publisher:
            ensure_publisher(db, user)
        db.commit()
        return issue_session_token(user), user

    return _make
