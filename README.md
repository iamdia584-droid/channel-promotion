# AdNet — Telegram Advertising Network

An independent, third-party advertising network for Telegram channels and groups:
advertisers fund campaigns, channel owners register as publishers, and the platform
matches, delivers, measures and settles the two sides against a double-entry ledger.

The Telegram bot is the primary interface. A web dashboard handles staff work that
needs real authentication.

---

## Read this first

Two documents explain the decisions that shape everything else:

- **[docs/TELEGRAM_CONSTRAINTS.md](docs/TELEGRAM_CONSTRAINTS.md)** — what the
  Telegram Bot API actually permits. A bot **cannot** read a channel post's view
  counter and **cannot** identify a passive viewer. This is not a limitation of the
  implementation; it is the platform. Measurement is designed around it rather than
  pretending otherwise.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — layering, the chart of
  accounts, the money-flow table for every event, and the pricing formula.

[docs/ROADMAP.md](docs/ROADMAP.md) states what is built and what is deliberately
deferred.

---

## Quick start

```bash
cp .env.example .env          # fill in SECRET_KEY and TELEGRAM_BOT_TOKEN
docker compose up -d db redis
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
python -m app.workers.cli bootstrap        # settings + first admin
uvicorn app.main:app --reload              # API, bot webhook, dashboard
celery -A app.workers.celery_app worker -l info -Q default,delivery,money,fraud,notify
celery -A app.workers.celery_app beat -l info
```

Or the whole stack at once:

```bash
docker compose up --build
```

Then:

- `http://localhost:8000/docs` — API reference (development only)
- `http://localhost:8000/admin` — staff dashboard
- `python -m app.workers.cli set-webhook` — point Telegram at this deployment

To see it work end to end locally without a real bot:

```bash
python -m app.workers.cli seed-demo   # advertiser + publisher + live campaign
python -m app.workers.cli serve       # deliver an ad
python -m app.workers.cli settle      # settle measured impressions
python -m app.workers.cli verify-ledger
```

---

## How the money works

Every balance in the system is derived from a **double-entry ledger**.
`LedgerService.post()` is the only function permitted to change an account
balance, and it refuses any transaction whose debits and credits do not match.
`tests/test_architecture.py` fails the build if any other module tries.

The flow for one impression batch:

```
advertiser deposits          GATEWAY_CLEARING  →  ADVERTISER_AVAILABLE
campaign approved            ADVERTISER_AVAILABLE → ADVERTISER_RESERVED
ad delivered, impressions measured
  settlement                 ADVERTISER_RESERVED → PUBLISHER_PENDING + PLATFORM_REVENUE
  after the validation window PUBLISHER_PENDING  → PUBLISHER_CONFIRMED
publisher withdraws           PUBLISHER_CONFIRMED → PAYOUT_CLEARING + PLATFORM_FEES
operator pays                 PAYOUT_CLEARING     → GATEWAY_CLEARING
```

Settlement is a single posting, so publisher earnings and platform revenue can
never disagree with what the advertiser was charged. A worker re-proves this hourly
by computing the trial balance, and alerts admins — not just a log file — if it is
ever non-zero.

**No floats.** Money is `Decimal` throughout, stored as `NUMERIC(24,6)`.
`app/core/money.py` rejects a float argument outright rather than rounding it, and
amounts cross the JSON boundary as strings because a JSON number would be parsed
back into a float by most clients.

---

## How measurement works

This is where an ad network is honest or is not. Four distinct kinds of
impression, only some of which can bill:

| Kind | Where it comes from | Billable |
|---|---|---|
| `MEASURED` | A tracking-link resolution we observed ourselves, bound to a one-time nonce | **Yes** |
| `TELEGRAM_REPORTED` | A post view-counter delta from an optional external source | Only where configured, always capped |
| `ESTIMATED` | A projection from the channel's historical average views | **Never** |
| `INVALIDATED` | Recorded, then failed validation | No |

Four defences apply in order before an impression bills:

1. **`impressions.dedupe_key` is UNIQUE in the database.** Refresh abuse, replayed
   links and retried workers all collide on it. An application-level "have I seen
   this?" check can lose a race; a unique index cannot.
2. **The measurement window** must still be open.
3. **The ratchet cap** — a delivery cannot bill more than
   `min(channel.avg_views × multiplier, funded impressions)`. A channel cannot bill
   more reach than it has historically demonstrated.
4. **The fraud score** must be below the configured block threshold.

Rejected impressions are *recorded*, never dropped: the evidence is what lets an
admin adjudicate a dispute, and the counters feed fraud detection.

Member count is never a billing basis. A channel with 100,000 members and 3,000
average views is worth a fraction of one with 50,000 members and 25,000 views, and
`test_member_count_alone_does_not_buy_quality` asserts exactly that.

---

## Roles

| Role | Interface | Can |
|---|---|---|
| Advertiser | Bot + REST | Fund a wallet, create and pace campaigns, target, view statistics, request refunds |
| Publisher | Bot + REST | Register and verify channels, set acceptance rules and frequency caps, watch earnings, withdraw |
| Moderator | Dashboard | Review campaigns, creatives and channels; work the report queue; escalate financial and fraud questions to an admin |
| Admin | Dashboard | Everything above, plus payouts, pricing, settings, manual adjustments and the audit log |

Channel ownership is **proven**, never trusted. Registering a channel requires two
independent facts from Telegram itself: our bot is an administrator there, and the
claimant is the creator or an administrator of the same chat. Every attempt, pass
or fail, is recorded.

Payouts and balance adjustments exist only on the web dashboard, behind a password
and TOTP — deliberately not in the bot, where identity rests on a Telegram account
that could be hijacked.

---

## Layout

```
app/
  core/        money, config, errors, security, cache, events, idempotency
  db/          declarative base, custom column types, session
  models/      40 tables, with the invariants as database constraints
  schemas/     Pydantic request/response models
  services/    all business logic — ledger, pricing, delivery, fraud, …
  api/         REST endpoints (FastAPI)
  bot/         Telegram interface (aiogram)
  admin/       staff dashboard (Jinja2)
  workers/     Celery tasks, beat schedule, operational CLI
migrations/    Alembic
docs/          architecture, Telegram constraints, roadmap
tests/         ~380 tests
```

A service never imports from `api`, `bot` or `admin`, so the same business rule
serves all three. `tests/test_architecture.py` enforces that.

---

## Configuration

Every rate, fee, threshold and window is a row in `system_settings`, editable from
the dashboard and audit-logged — **39 of them**, and none hard-coded in business
logic. That includes the platform commission, CPM floors and ceilings, the earnings
validation period, withdrawal fees and limits, pacing burst ratio, the impression
cap multiplier and every fraud threshold.

Pricing multipliers live in `pricing_rules` and compose:

```
effective_cpm = bid × country × category × ad_format × channel_size × quality
publisher_cpm = effective_cpm − effective_cpm × commission_rate
```

A quote is **frozen onto the delivery row** when the ad is placed, so changing a
rule tomorrow cannot retroactively alter what an already-served delivery owes.
Rules are deactivated rather than deleted for the same reason.

Secrets come from the environment only. `.env` is gitignored; `.env.example`
documents every variable with empty values, and a test asserts it stays that way.

---

## Tests

```bash
pytest                                              # SQLite, fast
TEST_DATABASE_URL=postgresql+psycopg://… pytest      # the real database
```

Both are meaningful. `MoneyType` stores integer micro-units on SQLite specifically
so the money CHECK constraints (`gross_amount = net_amount + platform_commission`,
`settled_amount <= reserved_amount`) are evaluated on exact arithmetic there too —
SQLite has no `NUMERIC` type and would otherwise store those columns as IEEE
floats, making a passing suite meaningless. Run against PostgreSQL before shipping
regardless: only it exercises `FOR UPDATE` row locking.

The suite covers what the spec asks to be covered — wallet, ledger, campaign
spending, CPM calculation, publisher earnings, refunds, withdrawals, duplicate
payment callbacks, duplicate impressions, fraud detection and authorization — and
verifies the spec's own worked examples end to end: a ৳10,000 budget at ৳50 CPM
buying 200,000 impressions, and 50,000 impressions at ৳100 CPM settling to exactly
৳4,000 for the publisher and ৳1,000 for the platform, with a zero trial balance
afterwards.

---

## Production notes

- **Not included:** a live payment gateway. `ManualProvider` records deposits
  confirmed out of band (bank transfer, manual bKash) and is idempotent on the
  provider's transaction id. A real bKash/Nagad/SSLCommerz adapter implements the
  same `PaymentProvider` interface; nothing downstream changes.
- **Not included:** MTProto view reading. Without it, billing uses measured
  impressions only. That is the honest default and it is why `NullViewSource`
  ships enabled — see `docs/TELEGRAM_CONSTRAINTS.md`.
- Payout destinations are encrypted at rest and shown masked. A production
  deployment should move that key into a KMS; the seam is
  `app/services/withdrawals.py`.
- Enable TOTP on every staff account before going live. With
  `APP_ENV=production` and `ADMIN_REQUIRE_2FA=true`, sign-in without it is refused.
- `impressions` and `ledger_entries` are the tables that grow without bound. Both
  are already indexed on their query paths; partition by month when volume warrants.
