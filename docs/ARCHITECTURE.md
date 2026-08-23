# Architecture

## 1. Guiding constraints

Three facts from the API investigation drive every decision:

1. **League standings are stale.** A live table must be computed in-process.
2. **Picks are immutable after the deadline.** Fetch once per manager per GW,
   cache all week. Live points for the entire user base then cost *one*
   `event/{gw}/live/` call.
3. **Everything is polling.** There is no push. A single shared poller feeds all
   chats; per-chat work is fan-out, not fan-in.

That third point is what keeps this cheap. Ten friend groups watching the same
gameweek generate exactly the same upstream traffic as one.

## 2. Component layout

```
                         Telegram
                            │  webhook (HTTPS)
                     ┌──────▼──────┐
                     │   Caddy     │  TLS, /webhook/<secret> → bot
                     └──────┬──────┘
   ┌────────────────────────▼──────────────────────────┐
   │                    bot container                   │
   │                                                    │
   │  aiogram Dispatcher ── handlers ──┐                │
   │                                    │               │
   │  live poller  ──┐                  │               │
   │  APScheduler ───┼──► Domain services ◄─────────────┤
   │                 │    (live table, differentials,   │
   │                 │     EO, awards, projections)     │
   │                 │              │                   │
   │                 └──────────────┤                   │
   │                          FPL client                │
   │                    (retry · throttle · coalesce)   │
   └───────┬──────────────────────┬─────────────────────┘
           │                      │
     ┌─────▼─────┐          ┌─────▼──────┐        ┌──────────────┐
     │  Redis    │          │ PostgreSQL │        │ FPL public   │
     │ HTTP cache│          │ chats,     │        │ API          │
     │ dedupe    │          │ leagues,   │        │ (no auth)    │
     │ locks     │          │ prefs,     │        └──────▲───────┘
     └───────────┘          │ snapshots  │               │
                            └────────────┘───────────────┘
```

Four layers, strictly one-directional:

* **`fpl/`** — transport. Knows HTTP, retries, caching, JSON shapes. Knows
  nothing about Telegram.
* **`services/`** — domain. Live tables, differentials, EO, auto-sub prediction,
  provisional bonus, awards. Pure functions over dataclasses; **fully unit
  testable with no network and no bot**.
* **`bot/`** — presentation. Formats domain objects into Telegram messages and
  keyboards. Contains no FPL logic.
* **`live/` + `scheduler/`** — time. Decides *when* things happen and pushes
  results into chats.

The payoff: swapping aiogram for something else, or adding a Discord front end,
touches only `bot/`. Every scoring rule stays put.

## 3. The FPL client

A single `httpx.AsyncClient` with:

* **Throttle** — token bucket at 4 req/s, `asyncio.Semaphore(5)` on concurrency.
* **Retry** — exponential backoff with jitter on 429/5xx/timeouts, 4 attempts,
  honouring `Retry-After`. A 403 on a public endpoint is treated as a soft block:
  back off hard (5 min) and serve stale cache rather than retrying into a ban.
* **Two-tier cache** — Redis, keyed by endpoint path. Each entry stores payload +
  `fetched_at`, with a *soft* TTL (serve, then refresh in background) and a
  *hard* TTL (serve stale only if upstream is failing). Never let a Telegram
  command block on a cold 1.6 MB `bootstrap-static` fetch.
* **Single-flight** — a Redis lock per cache key, so twenty simultaneous `/live`
  commands across ten chats produce **one** upstream request.
* **Stale-if-error** — if FPL is down, reply with cached data and a
  `⚠️ data from 6 min ago` footer instead of an error.

## 4. Data model (PostgreSQL)

Postgres holds only what the API can't tell us: bindings, preferences, and
history for deltas. Everything derivable from FPL lives in Redis with a TTL.

```sql
chat(
  id BIGINT PK,                    -- Telegram chat id
  type TEXT,                       -- private | group | supergroup
  title TEXT,
  timezone TEXT DEFAULT 'Asia/Singapore',
  alert_profile TEXT DEFAULT 'big-moments',  -- all|big-moments|digest-only|off
  quiet_hours int4range,           -- e.g. [1,8) local
  settings JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ
)

league(
  id INT PK,                       -- FPL league id, natural key
  name TEXT,
  kind TEXT,                       -- classic | h2h
  scoring TEXT,
  start_event INT,
  admin_entry INT,
  last_synced_at TIMESTAMPTZ
)

chat_league(
  chat_id BIGINT REFERENCES chat ON DELETE CASCADE,
  league_id INT REFERENCES league ON DELETE CASCADE,
  is_default BOOL DEFAULT false,
  added_by BIGINT,
  PRIMARY KEY (chat_id, league_id)
)
-- partial unique index: one default per chat
CREATE UNIQUE INDEX ON chat_league(chat_id) WHERE is_default;

manager(
  entry_id INT PK,                 -- FPL entry id
  player_name TEXT,
  team_name TEXT,
  refreshed_at TIMESTAMPTZ
)

league_manager(
  league_id INT REFERENCES league ON DELETE CASCADE,
  entry_id  INT REFERENCES manager ON DELETE CASCADE,
  PRIMARY KEY (league_id, entry_id)
)

-- links a Telegram human to an FPL team, so the bot can @-mention
identity(
  tg_user_id BIGINT,
  entry_id   INT REFERENCES manager,
  username   TEXT,
  display    TEXT,
  PRIMARY KEY (tg_user_id, entry_id)
)

-- immutable-after-deadline picks; avoids re-fetching every poll
picks(
  entry_id INT,
  event    INT,
  payload  JSONB NOT NULL,
  fetched_at TIMESTAMPTZ,
  PRIMARY KEY (entry_id, event)
)

-- last observed per-player live stats, for delta detection
live_snapshot(
  event INT,
  element INT,
  stats JSONB,
  updated_at TIMESTAMPTZ,
  PRIMARY KEY (event, element)
)

-- idempotency: never post the same alert twice, even across restarts
alert_log(
  chat_id BIGINT,
  event_key TEXT,                  -- e.g. 'gw4:goal:element:328:count:2'
  sent_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (chat_id, event_key)
)

-- final table per GW per league, for /form, /awards, /streaks, /season
gw_result(
  league_id INT, event INT, entry_id INT,
  points INT, net_points INT, rank INT,
  bench_points INT, transfer_cost INT,
  captain_element INT, captain_points INT,
  chip TEXT,
  PRIMARY KEY (league_id, event, entry_id)
)

-- self-refreshing /live messages
live_message(
  chat_id BIGINT, message_id BIGINT,
  league_id INT, event INT, view TEXT,
  expires_at TIMESTAMPTZ,
  PRIMARY KEY (chat_id, message_id)
)
```

`alert_log` is the important one. Alert dedupe cannot live in memory: the bot
will restart mid-gameweek, and posting "⚽ Haaland scores!" twice is the most
obvious possible bug. Every alert gets a deterministic key derived from the
*state* it reports, not the moment it was noticed, and an `INSERT ... ON CONFLICT
DO NOTHING` decides whether to send.

## 5. Live update strategy

### Adaptive cadence

The poller sleeps as long as it can. Its interval is chosen from the fixture
state, not a fixed cron:

| Phase | Condition | Interval |
|---|---|---|
| **Dormant** | no fixture within 30 min | sleep until `next_kickoff − 2 min` (capped 30 min) |
| **Pre-kickoff** | fixture starts within 30 min | 5 min |
| **Live** | any fixture `started && !finished_provisional` | **45 s** |
| **Bonus pending** | all fixtures provisional, `event-status.bonus_added` false | 3 min |
| **Settling** | bonus added, `leagues != "Updated"` | 10 min |
| **Finalising** | `event.data_checked` false but leagues updated | 15 min |
| **Finalised** | `data_checked` true | write `gw_result`, post wrap-up, go dormant |

At 45 s the live phase costs two calls per tick — `event/{gw}/live/` (422 KB) and
`fixtures/?event={gw}` (16 KB) — regardless of how many leagues or chats exist.
Roughly 80 calls and 35 MB across a busy Saturday. Comfortable.

### Tick pipeline

```
tick:
  1. fixtures + live  (one fetch, shared)
  2. diff live vs live_snapshot  →  raw player events
        goal, assist, clean-sheet lost, card, sub, bonus shift,
        defensive-contribution point, minutes crossing 60
  3. for each active league:
        picks (cached all GW) → compute live table
        attribute each player event to the managers who own that player
        detect table events: lead change, someone overtaken, captain blank
  4. per chat: filter by alert_profile, drop anything already in alert_log,
        batch into one message per 60 s window, respect quiet hours
  5. refresh any live_message rows that haven't expired (edit, don't post)
  6. persist new live_snapshot
```

Step 4's batching matters. During a 3-goal 10-minute spell, a naive bot posts
nine messages. This one posts one, with three lines.

### Provisional bonus

For fixtures in play, bonus is computed locally from `fixtures[].stats.bps`:
top three BPS get 3/2/1, ties share the higher value (two tied on top → 3, 3, 1;
three tied → 3, 3, 3). Provisional bonus is rendered in *italics* and the table
footer says `bonus provisional` until `event-status.bonus_added` flips.

### Auto-sub prediction

A starter with `minutes == 0` whose fixture is `finished_provisional` is
substituted by the first bench player (positions 12→15) whose fixture has also
finished and who keeps the formation legal. GK only swaps with GK. Predicted
subs are marked `↑` in output and replaced by FPL's real `automatic_subs` as
soon as they appear.

**This deliberately diverges from FPL's own number.** FPL applies auto-subs only
once *every* fixture in the gameweek has finished, so mid-gameweek
`standings.event_total` still counts the blanked starter's zero. Ours is the
projected final score, which is what a group chat actually wants — but the
difference is real, which is why it's marked rather than hidden.
`build_manager_row(..., predict_subs=False)` reproduces FPL's arithmetic
exactly, and the integration suite asserts equality on that path.

### Backpressure

The poller never awaits Telegram sends inline. Alerts go onto an `asyncio.Queue`
drained by a sender task at 1 message/sec per chat (Telegram's group limit is
~20/min; the sender enforces both). If the queue backs up beyond 50 items for a
chat, it collapses them into a single digest — a bot that spams itself into a
429 during a 6-goal thriller is worse than one that summarises.

## 6. Command flow

```
Telegram → Caddy → aiogram webhook → Dispatcher
   → ThrottleMiddleware  (3 cmds / 10 s per user; silent drop beyond)
   → ChatContextMiddleware  (loads chat + default league, upserts on first use)
   → handler
       ↳ resolve league  (explicit arg > chat default > picker keyboard)
       ↳ services.<x>()  ── fpl client ── redis cache ─ (miss) → FPL API
       ↳ formatting.render()  → HTML text + InlineKeyboard
   → answer / edit
```

Long operations (first `/live` of a gameweek needs N picks fetches) send an
immediate placeholder — `⏳ Building live table…` — then edit it. Nothing in a
handler blocks for more than a second before the user sees something.

Errors are typed: `LeagueNotFound` → "I can't find league 12345, check the ID";
`UpstreamUnavailable` → cached data plus a warning footer; anything else →
generic apology, full traceback to logs and to the owner's private chat.

## 7. Deployment

Docker Compose on your VPS, four services:

```yaml
caddy    # TLS + reverse proxy to bot:8080, auto Let's Encrypt
bot      # python:3.12-slim, non-root, healthcheck, restart: unless-stopped
db       # postgres:16-alpine, named volume, nightly pg_dump to ./backups
redis    # redis:7-alpine, appendonly off (cache only — losing it is harmless)
```

* **Webhook over polling.** One inbound HTTPS route, `/webhook/<random-secret>`,
  validated against `X-Telegram-Bot-Api-Secret-Token`. Long-polling is the
  fallback via `USE_POLLING=true` for local development.
* **Migrations** run in the container entrypoint (`alembic upgrade head`) before
  the bot starts.
* **Single instance.** Two bot replicas would double-post alerts. If you ever
  scale, the poller must be split into its own service with a Redis leader lock;
  the code already isolates it for that reason.
* **Config by environment.** `.env` on the host, never in the image. Required:
  `BOT_TOKEN`, `DATABASE_URL`, `REDIS_URL`, `WEBHOOK_BASE`, `WEBHOOK_SECRET`,
  `OWNER_CHAT_ID`.
* **Observability.** Structured JSON logs to stdout (`docker compose logs`),
  plus `/healthz` reporting DB, Redis and last-successful-FPL-fetch age. A
  Prometheus `/metrics` endpoint is stubbed for later.
* **Backups.** `pg_dump` on a nightly cron into `./backups`, 14-day retention.
  The database is small — bindings and preferences — so this is trivial and
  worth doing, since re-linking six leagues by hand is annoying.
* **Updates.** `git pull && docker compose up -d --build`. Roughly 40 s of
  downtime, invisible outside a live match.

Resource envelope: the bot idles around 120 MB RSS and spikes to ~250 MB while
parsing `bootstrap-static`; Postgres and Redis together want ~150 MB. A 1 GB VPS
is enough; 2 GB is comfortable.
