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
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

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


@pytest.fixture
def engine():
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
                excluded_categories=[],
                audience_types=[],
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
