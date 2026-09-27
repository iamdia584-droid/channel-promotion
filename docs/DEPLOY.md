# Deploying

Two paths. **Docker Compose** is the fastest way to a working system. The
**manual** path is what you want if you already run Postgres and Redis.

Everything below was run against PostgreSQL 16 and Redis 7 during development,
except the Docker Compose build itself — the compose file is validated but was not
built in that environment, so budget time for a first `docker compose build`.

---

## Before you start

You need four things:

1. **A server** with a public IP. 1 vCPU / 2 GB RAM is enough to begin.
2. **A domain with HTTPS.** This is not optional: Telegram will only send webhooks
   to an HTTPS URL with a valid certificate. Put Caddy or nginx in front.
3. **A bot token** from [@BotFather](https://t.me/botfather) — `/newbot`, then copy
   the token. Also run `/setprivacy` → Disable if you want the bot to see group
   messages.
4. **A payout method** for paying publishers. The platform records payouts; it does
   not send money. See "Payments" below.

---

## Path A — Docker Compose

```bash
git clone <your-repo> adnet && cd adnet
cp .env.example .env
```

Now edit `.env`. The three that matter most:

```bash
# 32 random bytes. Changing this later invalidates sessions AND makes stored
# payout destinations undecryptable, so set it once and back it up.
SECRET_KEY=$(openssl rand -hex 32)

TELEGRAM_BOT_TOKEN=123456:AA...          # from @BotFather
TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 16)   # any random string

BASE_URL=https://ads.yourdomain.com     # must be the public HTTPS URL
APP_ENV=production

BOOTSTRAP_ADMIN_EMAIL=you@yourdomain.com
BOOTSTRAP_ADMIN_PASSWORD=<at least 12 characters>
```

`docker compose up` fails with `env file .env not found` if you skip the `cp`.

Then:

```bash
docker compose up -d --build     # db, redis, api, worker, beat
docker compose exec api python -m app.workers.cli bootstrap    # settings + admin
docker compose exec api python -m app.workers.cli set-webhook  # point Telegram here
```

`alembic upgrade head` runs automatically in the `api` container's command, so the
schema is created on first boot.

Check it:

```bash
curl https://ads.yourdomain.com/health          # {"status":"ok","database":true}
docker compose logs -f worker                   # should be idle, not erroring
```

Open `https://ads.yourdomain.com/admin`, sign in, and **enable 2FA immediately** —
with `APP_ENV=production` and `ADMIN_REQUIRE_2FA=true`, sign-in is refused for
accounts without it, so do this while you still can.

---

## Path B — Manual

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/adnet
export REDIS_URL=redis://localhost:6379/0
# ... plus the variables from Path A

alembic upgrade head
python -m app.workers.cli bootstrap

# three processes, all long-running
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
celery -A app.workers.celery_app worker -l info -Q default,delivery,money,fraud,notify
celery -A app.workers.celery_app beat -l info

python -m app.workers.cli set-webhook
```

Run the three under systemd or supervisor. **All three are required**: without the
worker nothing is delivered or settled, and without beat nothing is scheduled.

---

## Reverse proxy

Caddy, which handles certificates for you:

```
ads.yourdomain.com {
    reverse_proxy localhost:8000
}
```

nginx needs the real client IP forwarded, because fraud scoring keys on it:

```nginx
location / {
    proxy_pass http://localhost:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

---

## First run, in order

1. `/start` the bot → tap **Publisher** → **Add Channel**.
2. Add the bot to a channel as an admin **with "Post Messages"** permission.
3. Send the channel's `@username` to the bot. It verifies with Telegram that it is
   an admin there *and* that you administer it. Both must hold.
4. Tap **Advertiser** → **Add Funds**. This creates a *pending* deposit; nothing is
   credited yet.
5. Confirm it as an admin: dashboard → or
   `POST /api/v1/admin/deposits/confirm` with the bank/bKash transaction id.
6. Create a campaign in the bot, then approve it on the dashboard.
7. `python -m app.workers.cli serve` (or wait for beat, every 5 minutes).

Try it locally first without a real bot:

```bash
python -m app.workers.cli seed-demo   # advertiser + publisher + live campaign
python -m app.workers.cli serve
python -m app.workers.cli settle
python -m app.workers.cli verify-ledger    # must report difference 0.000000
```

---

## Payments

**The platform does not move money.** `ManualProvider` records deposits you have
already received and payouts you are about to send:

- **Deposits** — advertiser sends to your bKash/bank, you confirm with the
  transaction id. The id is UNIQUE, so entering it twice credits once.
- **Payouts** — dashboard → Withdrawals → open the row to reveal the destination
  (that reveal is audit-logged), send the money by hand, then **Mark paid** with the
  provider reference.

For automatic bKash/Nagad/SSLCommerz, implement the `PaymentProvider` interface in
`app/services/payments.py`. Nothing downstream changes.

---

## Before taking real money

- [ ] 2FA enabled on every staff account
- [ ] `SECRET_KEY` backed up somewhere safe — losing it makes stored payout
      destinations undecryptable
- [ ] Nightly `pg_dump`, and a restore actually tested
- [ ] `platform_commission_rate` and the CPM floor/ceiling set on the Settings page
- [ ] `min_withdrawal` and the withdrawal fee set to your real costs
- [ ] `earnings_validation_hours` set — this is your fraud window, and shortening it
      means paying out before you can claw back
- [ ] `docker compose logs beat` shows the hourly `verify_ledger` job running

---

## When something is wrong

| Symptom | Cause |
|---|---|
| Bot silent | Webhook not set, or `BASE_URL` is not the public HTTPS URL. Check `getWebhookInfo`. |
| Webhook returns 401 | `TELEGRAM_WEBHOOK_SECRET` differs from what was registered. Re-run `set-webhook`. |
| Ads never deliver | Worker not running; or no channel is `active`; or the advertiser has no available balance. `python -m app.workers.cli serve` prints the reason. |
| Earnings stay pending | Normal for `earnings_validation_hours`. Beat confirms them hourly. |
| `verify-ledger` non-zero | **Stop payouts and investigate.** This should be impossible; it means a balance changed outside the ledger. |
| `docker compose up` → `env file .env not found` | You skipped `cp .env.example .env`. |

---

## Scaling

The first bottleneck is the delivery worker, not the API. Scale it first:

```bash
docker compose up -d --scale worker=4
```

Per-channel Redis locks mean extra workers cannot double-post to the same channel.
Run exactly **one** beat process — two would double-schedule every job.

`impressions` and `ledger_entries` grow without bound. Both are indexed on their
query paths; partition by month when they get large. Never delete from either:
`impressions` is the evidence behind every payout, and `ledger_entries` is the
audit trail.
