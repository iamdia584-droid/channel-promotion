# Implementation roadmap

## Phase 1 — MVP (spec §38) — IMPLEMENTED IN THIS REPO
1. Telegram login / identity by `telegram_user_id` (BIGINT, never username)
2. Advertiser registration
3. Publisher registration
4. Channel verification via real `getChatMember` admin checks
5. Wallet with available / reserved / spent
6. Campaign creation (bot wizard + REST)
7. Admin campaign approval & ad moderation
8. Publisher campaign assignment (delivery engine)
9. Impression tracking using only technically available Telegram metrics
10. Configurable CPM + pricing engine
11. Publisher earnings, pending → confirmed
12. Withdrawal requests + admin processing
13. Admin web dashboard
14. Double-entry transaction ledger
15. Basic fraud detection

## Phase 2
- MTProto view-source adapter, wired to `VIEW_COUNTER` measurement mode
- CPC pricing model and conversion postbacks
- Second-price auction tuning, reserve prices
- Advertiser self-serve web dashboard (the REST API already backs it)
- CSV/scheduled report delivery via Telegram

## Phase 3
- Real payment gateway adapters (bKash / Nagad / SSLCommerz) behind the existing
  `PaymentProvider` interface — `ManualProvider` ships today
- ML fraud scoring on top of the existing signal framework
- Multi-currency with FX at posting time
- Read replicas + table partitioning for `impressions` / `ledger_entries`
