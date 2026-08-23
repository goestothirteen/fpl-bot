# FPL mini-league Telegram bot

Tracks Fantasy Premier League mini-leagues live in group chats, so nobody has to
open ten team pages on a Saturday afternoon.

The FPL website's league table lags reality by hours. This bot computes the
table itself from live match data, so `/live` is current to the minute — points,
players still to play, captains, differentials, bench damage.

```
Sunday League FC · GW4
┌──────────────────────────────────┐
│  #  Team             GW    Tot  ⏳ │
│ ─────────────────────────────────  │
│  1  Ten Hag's Lads   62    284   2 │
│  2  Sonny Delight    58    281   3 │
│  3  Klopp Fiction TC 57↑   279   1 │
│  4  Mark's XI        41*   266   4 │
└──────────────────────────────────┘
* after hit · ↑ auto-sub projected · live
```

## Quick start

```bash
cp .env.example .env      # add BOT_TOKEN from @BotFather, set USE_POLLING=true
echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)" >> .env
docker compose up -d --build
docker compose logs -f bot
```

Polling mode needs no domain, no TLS and no open ports. The full walkthrough —
including the BotFather steps and what the logs should say — is in
[`docs/DEPLOY.md`](docs/DEPLOY.md).

Then in your group chat:

```
/link 123456       # the number in your league's FPL URL
/me 7654321        # the number in your own team's URL — optional, enables @-mentions
/live
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/API_RESEARCH.md`](docs/API_RESEARCH.md) | Every FPL endpoint, verified live, with auth requirements, rate-limit reality and the staleness traps |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | Full feature and command catalogue |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data model, live-update strategy, deployment |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Step-by-step runbook: BotFather, VPS, first test, troubleshooting |
| [`docs/USER_GUIDE.html`](docs/USER_GUIDE.html) | Mobile-friendly guide to send to the people in your group chats |

## How it works, briefly

Three facts drive the design:

1. **League standings are stale.** `last_updated_data` lags by hours, so the
   live table is computed in-process:
   `Σ(live_points[element] × multiplier)` over all 15 picks. The `multiplier`
   field already encodes bench, captain, triple captain and bench boost.
2. **Picks are immutable after the deadline.** Each manager's picks are fetched
   once per gameweek and cached all week. Live points for every manager in every
   league then cost *one* `event/{gw}/live/` call.
3. **Everything is polling.** One shared poller feeds all chats, so ten friend
   groups generate the same upstream traffic as one.

No FPL login is needed anywhere — every endpoint the bot uses is public.

## Project layout

```
src/fplbot/
  fpl/        transport: httpx client, throttle, retry, Redis cache
  services/   domain: live tables, auto-subs, bonus, differentials, awards
              (pure functions — no network, no Telegram, fully unit tested)
  bot/        presentation: aiogram handlers, formatting, keyboards
  live/       the poller, event detection, the outbound notifier
  scheduler/  deadline reminders, price watch, nightly maintenance
  db/         SQLAlchemy models and repository helpers
```

The layering is strict and one-directional. Swapping aiogram out, or adding a
Discord front end, touches only `bot/`.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest                                   # unit tests, no network
.venv/bin/pytest --run-integration -m integration  # hits the real FPL API
.venv/bin/ruff check src tests
```

Run the bot locally against SQLite-free Postgres in Docker but with long
polling instead of a webhook:

```bash
docker compose up -d db redis
USE_POLLING=true DATABASE_URL=postgresql+asyncpg://fpl:fpl@localhost:5432/fpl \
  .venv/bin/python -m fplbot
```

There's also a no-Telegram smoke script that renders a real league table to the
terminal — the fastest way to check the FPL side still works:

```bash
.venv/bin/python scripts/smoke.py 314
```

## Production notes

* **One instance only.** Two bot replicas would double-post alerts. Scaling
  means splitting the poller into its own service behind a Redis leader lock;
  the code is already isolated for that.
* **Backups:** `./docker/backup.sh` nightly via host cron. The database is small
  but re-linking leagues by hand is annoying.
* **Updating:** `git pull && docker compose up -d --build`.
* **Be a good citizen.** The FPL API is undocumented and unauthenticated. The
  client throttles to 4 req/s with concurrency 5 and backs off hard on 403/429.
  Don't raise those numbers.
