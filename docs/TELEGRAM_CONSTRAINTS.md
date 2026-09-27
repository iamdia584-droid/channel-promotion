# Telegram Platform Constraints (read this before touching delivery or measurement)

Spec §37 requires that we verify what the Telegram **Bot API** actually permits
before designing delivery and measurement. This document is the result of that
analysis and it constrains the code in `app/services/delivery.py`,
`app/services/measurement.py` and `app/services/impressions.py`.

## What a bot CAN do

| Capability | API | Notes |
|---|---|---|
| Post to a channel/group | `sendMessage`, `sendPhoto`, `sendVideo` | Requires the bot to be an **administrator** with `can_post_messages` (channels). |
| Attach inline URL buttons | `reply_markup` | This is our primary *measurable* signal. |
| Verify admin rights | `getChatMember(chat_id, bot_id)` | Authoritative ownership/permission proof. |
| Verify the claimant is an owner/admin | `getChatMember(chat_id, user_id)` | Status must be `creator` or `administrator`. |
| Read member count | `getChatMemberCount` | Cheap, pollable, **easily inflated** — never a billing basis. |
| Resolve a chat | `getChat` | Gives title, type, username, (sometimes) description. |
| Delete/edit its own post | `deleteMessage`, `editMessageText` | Used for expiry and takedowns. |
| Receive button presses | `callback_query` | Only for buttons with `callback_data`, i.e. inside the bot. |

## What a bot CANNOT do

These are hard limits. Any design that assumes otherwise is broken:

1. **A bot cannot read a channel post's view counter.** The `views` field exists
   only on MTProto `Message` objects available to *user* clients. `Message` in the
   Bot API has no `views`. So "Telegram-reported views" are **not obtainable by
   the bot alone**.
2. **A bot cannot enumerate who viewed a post.** There is no per-user impression
   data of any kind. Per-user frequency capping is therefore only possible for
   users who *interact* (click), never for passive viewers.
3. **A bot cannot post into a chat where it is not an admin**, and it cannot add
   itself. The publisher must add it. There is no way to "push ads into every
   channel".
4. **A bot cannot see a channel's subscriber list, growth history, or
   demographics.** Country/language/audience data is publisher-declared and must
   be treated as a *claim* until corroborated.
5. **`getChatMemberCount` is not a reach metric.** 100k members with 3k average
   views is worth a fraction of 50k real views (spec §5).

## Consequence: four distinct impression kinds

We never conflate these. `impressions.kind` is an enum and only one kind is
billable by default:

| Kind | Source | Billable? |
|---|---|---|
| `MEASURED` | A unique, first-party, deduplicated event we observed ourselves: a tracking-link resolution or a bot deep-link open, each bound to a one-time nonce. | **Yes** |
| `TELEGRAM_REPORTED` | A post view counter delta supplied by an optional MTProto reader (`app/services/measurement.py::MTProtoViewSource`, disabled by default, requires a user session the operator supplies). | Yes, only when the source is enabled AND the publisher is `VERIFIED`, and always capped. |
| `ESTIMATED` | A model output from the channel's historical average views. Used for advertiser *forecasts* and publisher *estimates* only. | **Never** |
| `INVALIDATED` | Recorded, then failed fraud validation or exceeded a cap. Retained for audit; reversed if already accrued. | No |

`ESTIMATED` rows exist so the UI can show "estimated impressions" honestly next
to "billable impressions" without faking precision.

## The billing rule (implemented in `app/services/impressions.py`)

An impression is **billable** when all of the following hold:

1. Its `kind` is billable per the table above.
2. Its `dedupe_key` is globally unique (DB unique constraint, not application
   logic) — this makes duplicate counting and refresh abuse structurally
   impossible, not merely unlikely.
3. Its delivery is still inside the ad's measurement window
   (`settings.impression_window_hours`).
4. The delivery's billable count is below
   `min(channel.avg_views * settings.impression_cap_multiplier, campaign remaining budget in impressions)`.
   This is the **ratchet cap**: a channel cannot bill more reach than it has
   historically demonstrated.
5. Its `fraud_score` is below `settings.fraud_block_threshold`.
6. View-counter-derived impressions are monotonic: we store
   `ad_deliveries.reported_views_high_water` and only ever bill the positive
   delta above it, so a counter that jumps and drops cannot double-bill.

Anything failing 3–6 is still written to the ledger of impressions with
`validation_status` set accordingly. The impression table is append-only; there
is no `UPDATE` path that erases evidence.

## Measurement adapters

Because capabilities differ per channel, measurement is pluggable per publisher
channel (`publisher_channels.measurement_mode`):

- `CLICK_ONLY` — the safe default. Only `MEASURED` click/deep-link events bill.
  Works for every channel, requires nothing but the bot being an admin.
- `VIEW_COUNTER` — additionally consumes `TELEGRAM_REPORTED` deltas from a
  configured view source. Requires operator-supplied MTProto credentials and an
  admin flag on the channel.
- `HYBRID` — both, with click events taking precedence and view deltas capped so
  the two cannot double-count the same delivery.

The abstract interface is `ViewSource` in `app/services/measurement.py`. The
default binding is `NullViewSource`, which returns nothing — so a fresh
deployment bills only what it can actually prove.
