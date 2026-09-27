# Architecture

## Components

```
                        ┌────────────────────┐
  Telegram  ──webhook──►│  FastAPI app        │
                        │  /telegram/webhook  │──► aiogram Dispatcher (bot UI)
                        │  /api/v1/*          │──► REST API (advertiser/publisher/admin)
  Browser   ──HTTPS───► │  /admin/*           │──► Jinja2 admin dashboard
                        │  /t/{token}         │──► tracking redirect (impression capture)
                        └─────────┬───────────┘
                                  │
                   ┌──────────────┼───────────────┐
                   ▼              ▼               ▼
              PostgreSQL       Redis          Celery workers
           (source of truth) (cache, locks,  (delivery, settlement,
                              rate limits,    fraud, analytics,
                              idempotency)    notifications)
```

Nothing heavy runs in the bot request path (spec §29). Webhook handlers validate,
persist, enqueue, and return. All money, delivery and fraud work happens in
workers inside explicit DB transactions.

## Layering

| Layer | Directory | Rule |
|---|---|---|
| Transport | `app/api`, `app/bot`, `app/admin` | Parse, authorize, delegate. No business logic, no money math. |
| Services | `app/services` | All business rules. Pure-ish, take a `Session`, raise domain errors. |
| Models | `app/models` | SQLAlchemy 2.0 mapped classes + constraints. Constraints are the last line of defence. |
| Schemas | `app/schemas` | Pydantic v2 request/response validation. |
| Workers | `app/workers` | Celery tasks; thin wrappers around services. |

A service never imports from `app/api` or `app/bot`. Enforced by review, and by
`tests/test_layering.py`.

## Money

- Every monetary column is `NUMERIC(24, 6)`. **No floats anywhere.**
  `tests/test_money.py` asserts no financial model field maps to `Float`.
- `app/core/money.py` provides `D()`, `q()` (quantize to 6dp, `ROUND_HALF_UP`)
  and `cpm_cost(impressions, cpm)` which is the *only* place the /1000 division
  is written.
- Currency is `BDT` by default, configurable, and stored on every row. Mixed
  currency arithmetic raises.

## Double-entry ledger (spec §15)

Three tables:

- `ledger_accounts` — one row per (owner, kind). Holds a cached `balance` and a
  `normal_side` (DEBIT for asset/expense, CREDIT for liability/revenue).
- `ledger_transactions` — one row per money movement, with a **UNIQUE
  `idempotency_key`**. This single constraint is what makes replayed payment
  callbacks safe (spec §26).
- `ledger_entries` — two or more rows per transaction. `post()` refuses to commit
  unless `sum(debits) == sum(credits)`.

`LedgerService.post()` is the only function in the codebase permitted to change
an account balance. It locks accounts with `SELECT ... FOR UPDATE` in ascending
id order (deadlock-free), writes entries with `balance_after` snapshots, and
mirrors the advertiser/publisher projections onto `wallets` in the same DB
transaction. `wallets` is a *projection*, never an authority;
`tests/test_ledger.py::test_projection_matches_ledger` proves they agree.

Account kinds:

```
ADVERTISER_AVAILABLE   liability  money we owe the advertiser, spendable
ADVERTISER_RESERVED    liability  committed to a campaign, not yet spent
ADVERTISER_SPENT       revenue    recognised ad spend
PUBLISHER_PENDING      liability  earned, inside validation window
PUBLISHER_CONFIRMED    liability  withdrawable
PLATFORM_REVENUE       revenue    our margin
PLATFORM_FEES          revenue    withdrawal fees
GATEWAY_CLEARING       asset      the platform's cash position
PAYOUT_CLEARING        liability  payouts committed but not yet sent
FRAUD_CLAWBACK         revenue    reversed earnings

There is no ADVERTISER_SPENT or PUBLISHER_PAID account. Cumulative advertiser
spend is the sum of that advertiser's SETTLEMENT postings, and cumulative payout
the sum of their WITHDRAWAL_PAID postings. An account that is debited and credited
the same amount in one movement nets to zero and tracks nothing.
```

## Money flows

| Event | Debit | Credit |
|---|---|---|
| Deposit confirmed | GATEWAY_CLEARING | ADVERTISER_AVAILABLE |
| Campaign budget reserved | ADVERTISER_AVAILABLE | ADVERTISER_RESERVED |
| Reservation released (pause/cancel) | ADVERTISER_RESERVED | ADVERTISER_AVAILABLE |
| Settlement of an impression batch | ADVERTISER_RESERVED | PUBLISHER_PENDING + PLATFORM_REVENUE |
| Earnings confirmed | PUBLISHER_PENDING | PUBLISHER_CONFIRMED |
| Earnings reversed (fraud) | PUBLISHER_PENDING | FRAUD_CLAWBACK |
| Withdrawal requested | PUBLISHER_CONFIRMED | PAYOUT_CLEARING + PLATFORM_FEES |
| Withdrawal paid | PAYOUT_CLEARING | GATEWAY_CLEARING |
| Withdrawal rejected | PAYOUT_CLEARING + PLATFORM_FEES | PUBLISHER_CONFIRMED |
| Refund | ADVERTISER_RESERVED | ADVERTISER_AVAILABLE (or GATEWAY_CLEARING for cash-out) |

Settlement is one atomic `post()`: the advertiser's reserved prepayment is
earned (debit) and becomes, to the exact paisa, a liability to the publisher plus
platform revenue (credits). Because it is a single balanced posting, publisher
earnings and platform revenue can never disagree with what the advertiser was
charged. Cumulative advertiser spend is the sum of their settlement postings
rather than a separate account, which keeps the chart free of an account that
would net to zero.

## Pricing engine (spec §7, §31)

```
advertiser_cpm = campaign.bid_cpm                      (advertiser's bid, floor-checked)
effective_cpm  = advertiser_cpm
                 × country_multiplier
                 × category_multiplier
                 × quality_multiplier(channel)
                 × format_multiplier(ad.kind)
commission     = effective_cpm × commission_rate       (rate from settings, per-country/category overridable)
publisher_cpm  = effective_cpm − commission
```

All multipliers live in `pricing_rules` (admin-editable, versioned, never
hard-coded). `PricingService.quote()` returns a frozen `PriceQuote` that is
*persisted on the delivery row*, so a later change to pricing rules can never
retroactively alter an already-served delivery's economics.

## Ad delivery engine (spec §9, §32)

`DeliveryService.plan(channel)` runs the 13 steps of §9:

1. Candidate campaigns: status `RUNNING`, inside schedule, budget remaining,
   daily budget remaining, hourly pace token available.
2. Hard filters: targeting (country/language/category/size/views), channel
   category acceptance, blacklists, frequency caps (per-channel per-day,
   per-week, min interval), per-campaign-per-channel cap.
3. Score each survivor:
   `score = effective_cpm × targeting_relevance × publisher_fit × quality
            × pacing_factor × priority_weight × (1 − fatigue)`
4. Selection mode from settings: `FIXED_CPM` (highest effective CPM),
   `AUCTION` (highest bid, charged second-price + increment), or `WEIGHTED`
   (softmax-weighted random over the top N).
5. Anti-monopoly: an advertiser cannot take more than
   `settings.max_advertiser_inventory_share` of a channel's daily slots.

Selection is deterministic given a seed, which makes `tests/test_delivery.py`
meaningful.

## Pacing (spec §10)

Two-level token bucket in Redis, authoritative counters in Postgres:

- Daily: `campaign_daily_spend` row per (campaign, date), capped at `daily_budget`.
- Hourly: target spend for the hour is
  `daily_budget × hour_weight[h] / sum(hour_weight)`; a campaign may run ahead by
  at most `settings.pacing_burst_ratio`.
- Budget is *reserved* before a delivery is sent and released if the delivery
  fails, so a crash cannot overspend.

## Fraud (spec §16)

`FraudService.score(event)` runs independent signal functions, each returning
0–100 with evidence. The composite is a weighted max-blend (not a sum, so one
noisy signal cannot alone reach 100). Bands: 0–30 normal, 31–60 review, 61–80
suspicious, 81–100 high risk. Actions are *graduated*: score alone never bans;
it flags, throttles, holds earnings, or opens a `fraud_events` case with the full
evidence JSON for an admin.

## Events (spec §30)

`app/core/events.py` — an in-process dispatcher that persists every event to
`event_log` and fans out to Celery. Event names are exactly those in §30.

## Idempotency (spec §26)

Three independent mechanisms:
1. `ledger_transactions.idempotency_key` UNIQUE — money.
2. `deposits.provider_transaction_id` UNIQUE — payment callbacks.
3. `impressions.dedupe_key` UNIQUE — impressions.
4. `Idempotency-Key` header support on all mutating API routes, backed by
   `idempotency_records`.

## Running the tests

```bash
pytest                                    # SQLite, fast
TEST_DATABASE_URL=postgresql+psycopg://... pytest   # the real thing
```

Both are meaningful. `MoneyType` stores micro-unit integers on SQLite precisely so
that the money CHECK constraints (`gross_amount = net_amount + platform_commission`,
`settled_amount <= reserved_amount`) are evaluated on exact arithmetic there too —
SQLite has no NUMERIC type and would otherwise store these columns as IEEE floats,
making those constraints fail for ordinary commission rates and, worse, making a
passing suite meaningless. Run against PostgreSQL before shipping regardless: it is
what production uses, and only it exercises `FOR UPDATE` row locking.
