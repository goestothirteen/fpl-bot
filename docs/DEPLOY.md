# Deploying to your VPS

Written for: a Linux VPS with Docker and Docker Compose already installed, **no
domain name**. We run in **long-polling mode**, which means the bot dials out to
Telegram rather than Telegram calling in. No domain, no TLS certificate, no open
ports, no firewall changes. Every feature works identically to webhook mode.

You can switch to webhooks later — see the last section — but there's no reason
to on day one.

---

## Step 1 — Create the bot (2 minutes, on your phone or desktop)

In Telegram, message **@BotFather**:

```
/newbot
```

It asks for a display name (anything, e.g. `Sunday League Bot`) and then a
username, which must end in `bot` (e.g. `sundayleague_fpl_bot`). It replies with
a token that looks like `8012345678:AAF...`. **That token is a password — treat
it like one.** Anyone holding it controls the bot.

Two settings worth changing while you're in there:

```
/setprivacy   → select your bot → Enable
```

Privacy mode **enabled** is correct. The bot still receives every message that
starts with `/`, which is all it needs, and it can't read your friends' ordinary
chatter. Leave it on.

```
/setcommands  → select your bot
```

Paste this so commands autocomplete in the chat:

```
live - Live league table
left - Who still has players to play
diff - Differentials, or head-to-head
captains - Captain spread and returns
bench - Points left on benches
chips - Chips remaining this half
deadline - Next deadline countdown
news - Injury flags for owned players
awards - Gameweek awards
transfers - This week's transfers
form - Last four gameweeks
template - League template and bravest picks
player - Player card
fixtures - Upcoming fixtures
link - Link an FPL league to this chat
leagues - Manage linked leagues
me - Claim your FPL team
settings - Alert preferences
help - All commands
```

Finally, message **@userinfobot** and it will reply with your numeric user ID.
Keep it — that's your `OWNER_CHAT_ID`, where the bot reports its own errors.

---

## Step 2 — Put the code on the server

However you prefer. Then:

```bash
cd /srv/fpl-bot      # or wherever you put it
ls                   # you should see docker-compose.yml, Dockerfile, src/
```

---

## Step 3 — Configure

```bash
cp .env.example .env
nano .env
```

Set these four and leave everything else alone:

```ini
BOT_TOKEN=8012345678:AAF...        # from BotFather
OWNER_CHAT_ID=123456789            # from @userinfobot
USE_POLLING=true                   # no domain needed
DEFAULT_TIMEZONE=Asia/Singapore
```

Then give Postgres a real password — Compose reads it from the same file:

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)" >> .env
chmod 600 .env
```

`DATABASE_URL` and `REDIS_URL` are set by Compose and override whatever's in
`.env`, so ignore those lines.

---

## Step 4 — Start it

```bash
docker compose up -d --build
```

First build takes 1–2 minutes. Then watch it come up:

```bash
docker compose logs -f bot
```

You're looking for, in order:

```
waiting for postgres…
running migrations…
INFO  [alembic.runtime.migration] Running upgrade  -> af2bc3e21a4b, initial schema
{"event": "health.listening", "port": 8080, ...}
{"event": "bot.starting", "mode": "polling", ...}
{"event": "poller.started", ...}
```

`poller.started` means it's alive. Ctrl-C stops following the logs; it does not
stop the bot.

Confirm all three containers are healthy:

```bash
docker compose ps
```

`bot` should reach `healthy` within about 30 seconds. Caddy will not appear —
it's behind a Compose profile and only starts in webhook mode.

---

## Step 5 — Test it privately first

Before letting it loose on your friends, open a **direct message** with your bot
in Telegram and send:

```
/start
```

You should get a welcome message. Then, using any league ID you know:

```
/link 123456
/live
```

If `/live` returns a table, everything works — the Telegram side, the database,
and the FPL side all just proved themselves in one command.

---

## Step 6 — Make a private test group

Before your friends see it, give yourself a sandbox.

1. In Telegram: **New Group** → add your bot as the only other member → name it
   something like `FPL Bot Test`.
2. In that group, link one of your real mini-leagues:

   ```
   /link 123456
   ```

   Find the number by opening the mini-league on the FPL website and reading the
   URL: `.../leagues/123456/standings/c`.

3. Work through the whole surface:

   ```
   /me 1234567     your own team id, from .../entry/1234567/
   /live           the flagship — tap the buttons under it
   /left
   /captains
   /bench
   /diff
   /chips
   /deadline
   /news
   /whois
   /settings       try switching to "all" so alerts actually fire
   ```

   `/live` will take a few seconds the first time — it fetches every manager's
   picks once, then caches them for the rest of the gameweek. Run it again and
   it should be instant.

4. To see the automatic alerts, do this during a match. With `/settings` on
   `all`, goals and assists by anyone in the league should appear within about a
   minute of happening, and the `/live` message should rewrite itself in place.

If something looks wrong, `docker compose logs --tail=50 bot` in another window
will usually say why.

**A note on group IDs.** Telegram gives a group a new chat id the moment it
becomes a supergroup — which happens when you add enough members or make it
public. The bot detects this and carries your linked leagues and settings across
automatically, posting a short note when it does. You don't need to re-link
anything.

---

## Step 7 — Add it to the real groups

1. Open the group → **Add members** → search your bot's username → add it.
2. Send `/link <that group's league id>`.
3. Everyone sends `/me <their entry id>` — optional, but it's what lets the bot
   @-mention people when their players score.
4. Agree on a volume with `/settings`. `big-moments` is the default and is the
   right answer for most chats.
5. Send round the user guide: `docs/USER_GUIDE.html`.

Repeat for the second group with **its own** league ID. Nothing on the server
changes — one bot process serves every group independently, each with its own
league, volume setting and quiet hours.

---

## Day-to-day

```bash
docker compose logs -f bot            # follow logs
docker compose restart bot            # restart just the bot
docker compose down                   # stop everything
docker compose up -d --build          # after pulling new code
docker compose exec db psql -U fpl fpl   # poke at the database
curl localhost:8080/healthz           # from on the server
```

Set up the nightly backup once:

```bash
crontab -e
# add:
30 3 * * * cd /srv/fpl-bot && ./docker/backup.sh
```

---

## If something goes wrong

**`/start` gets no reply.** The token is wrong or has a stray space. Check with
`docker compose logs bot | grep -i unauthorized`.

**`bot` container restarts in a loop.** Read the last 50 lines:
`docker compose logs --tail=50 bot`. Nine times out of ten it's a typo in
`.env`.

**`bot` shows as `unhealthy` but works.** The healthcheck calls the FPL API; if
FPL is briefly down the check fails while the bot itself is fine. It recovers on
its own.

**`/link` says it can't find the league.** You've got a team ID rather than a
league ID. League URLs contain `/leagues/`, team URLs contain `/entry/`.

**`/live` is slow the first time each gameweek.** Expected — it fetches every
manager's picks once, then caches them for the rest of the week. Subsequent
calls are near-instant.

**Nothing posts automatically.** Check `/settings` isn't set to `off`, and
remember quiet hours are 1am–8am local by default.

---

## Later: switching to webhooks

Only worth it if you want marginally lower latency and slightly less outbound
traffic. You'll need a domain pointed at the server and ports 80/443 open.

```bash
# in .env
USE_POLLING=false
WEBHOOK_BASE=https://fpl.yourdomain.com
WEBHOOK_SECRET=<openssl rand -hex 24>
DOMAIN=fpl.yourdomain.com
```

```bash
docker compose --profile webhook up -d --build
```

Caddy fetches a Let's Encrypt certificate automatically. To go back, set
`USE_POLLING=true` and run `docker compose up -d` without the profile.
