# FPL API — investigation findings

All findings below were verified by live HTTP calls on **2026-08-23**, during
Gameweek 1 of the 2026/27 season (fixtures in progress, bonus provisional).

Base URL: `https://fantasy.premierleague.com/api/`

---

## 1. Authentication

**Everything the bot needs is public and unauthenticated.** There is no API key,
no OAuth, no registration. Send a normal browser `User-Agent` and you get JSON.

| Endpoint | Auth? | Verified |
|---|---|---|
| `bootstrap-static/` | no | 200, 1.59 MB |
| `fixtures/?event={gw}` | no | 200 |
| `event/{gw}/live/` | no | 200, 422 KB |
| `element-summary/{player_id}/` | no | 200 |
| `entry/{entry_id}/` | no | 200 |
| `entry/{entry_id}/history/` | no | 200 |
| `entry/{entry_id}/transfers/` | no | 200 |
| `entry/{entry_id}/event/{gw}/picks/` | no | 200 |
| `leagues-classic/{league_id}/standings/` | no | 200 |
| `leagues-h2h/{league_id}/standings/` | no | endpoint shape confirmed; 404 on a non-existent league id |
| `leagues-h2h-matches/league/{league_id}/?page=1` | no | as above |
| `league/{league_id}/cup-status/` | no | 200 |
| `event-status/` | no | 200 |
| `dream-team/{gw}/` | no | 200 |
| `team/set-piece-notes/` | no | 200 |
| `stats/most-valuable-teams/` | no | 200 |
| `my-team/{entry_id}/` | **YES** | 403 `Authentication credentials were not provided.` |
| `entry/{entry_id}/transfers-latest/` | **YES** | 403 |
| `me/` | (session) | 200 but `{"player": null}` when anonymous |

**Consequence for the bot:** it never needs anyone's FPL password. The only
things it cannot see are (a) a manager's *unsaved / pre-deadline* team via
`my-team/`, and (b) transfers made in the current, not-yet-deadlined gameweek.
Both require a logged-in session cookie. **Do not build that in** — FPL's login
flow sits behind Cloudflare + reCAPTCHA, breaks often, and asking friends for
their FPL password is a bad idea. Everything below works without it.

---

## 2. What you get from a league ID alone

`GET /leagues-classic/{league_id}/standings/?page_standings=1&phase=1`

```jsonc
{
  "league": { "id": 314, "name": "Overall", "league_type": "s",
              "scoring": "c", "start_event": 1, "admin_entry": null,
              "closed": false, "has_cup": true, "code_privacy": "p" },
  "last_updated_data": "2026-08-23T04:58:22Z",
  "standings": {
    "has_next": true, "page": 1,
    "results": [
      { "entry": 2085396, "entry_name": "Gregory Peck", "player_name": "Matz Sels",
        "rank": 41, "last_rank": 0, "rank_sort": 51,
        "event_total": 80, "total": 80, "club_badge_src": null }
    ]
  },
  "new_entries": { "has_next": false, "page": 1, "results": [] }
}
```

Key facts:

* **Private leagues work.** `league_type` is `"s"` (system/global) or `"x"`
  (private). A private league's standings are readable by anyone who knows the
  numeric ID — the invite code only gates *joining*, not reading. Your friend
  groups' leagues are therefore fully accessible.
* **50 entries per page.** Paginate with `page_standings=N` until
  `standings.has_next` is false. A 20-person friend league is one call.
* **`entry`** is the manager ID — the key to every per-manager endpoint.
* **`phase`** (1 = overall, 2..11 = monthly phases) gives month-by-month tables
  for free. `bootstrap-static.phases` lists them.

### The staleness trap

`standings` is **not live**. `last_updated_data` was `04:58Z` while matches were
kicking off hours later. FPL recomputes league tables in batches — typically
after each match day, and always after bonus is confirmed. `event_total` in the
standings payload lags real time by hours.

**So a live table has to be computed by the bot itself.** That is the single
most important architectural consequence.

---

## 3. Computing live points yourself — validated

Algorithm:

1. `GET /event/{gw}/live/` → `elements[].stats.total_points` for all 604 players.
2. For each manager: `GET /entry/{entry}/event/{gw}/picks/` → 15 picks, each with
   `element`, `position` (1-15), `multiplier`, plus `active_chip` and `entry_history`.
3. `live_points = Σ (total_points[pick.element] × pick.multiplier)` over **all 15 picks**.

`multiplier` already encodes everything: bench = 0, starter = 1, captain = 2,
triple captain = 3, and under Bench Boost the bench picks come back as 1. No
special-casing needed.

Verified against the top 5 of the Overall league mid-GW1:

```
Intekma            api_event_total= 95   computed= 95   chip=None    to_play=0
the reds           api_event_total= 93   computed= 93   chip=3xc     to_play=1
Bear Netherland    api_event_total= 92   computed= 92   chip=None    to_play=0
Yemen اب           api_event_total= 92   computed= 92   chip=bboost  to_play=4
Balotellitubbies   api_event_total= 91   computed= 91   chip=bboost  to_play=1
```

Exact match, including both chip types. Subtract
`entry_history.event_transfers_cost` for the net score.

### Bonus points

`event/{gw}/live/` **already includes provisional bonus** once a fixture reaches
`finished_provisional: true` — all six completed GW1 fixtures had 3-4 players
with `stats.bonus > 0` even though `event-status` still reported
`bonus_added: false`. But **during** a match `bonus` is `0`, so for in-play
fixtures the bot must derive provisional bonus itself from `stats.bps`
(3/2/1 to the top three BPS scorers, ties share — 2 tied for 1st → 3,3,1).
`fixtures/?event={gw}` carries a per-fixture `bps` stat array for exactly this.

### Auto-subs

`picks.automatic_subs` is only populated **after all of a manager's players'
fixtures have finished**. Mid-gameweek it is `[]`. For an accurate live table the
bot predicts subs locally: a starter on 0 minutes whose fixture is
`finished_provisional` is replaced by the first eligible bench player
(in `position` order 12→15) that keeps the formation legal
(`element_types`: 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD, from `bootstrap-static`).

### Players left to play

Join `pick.element → element.team → fixture`. A pick is "yet to play" if its
team's GW fixture has `started: false`, and "in play" if `started: true` and
`finished_provisional: false`. Blank/double gameweeks fall out of this naturally
because a team can have 0 or 2 fixtures in one event.

---

## 4. Per-manager endpoints

| Endpoint | Gives you |
|---|---|
| `entry/{id}/` | name, region, `summary_overall_points/rank`, `last_deadline_value`, `last_deadline_bank`, every league they're in |
| `entry/{id}/history/` | `current[]` per-GW points/rank/bench/transfer cost/value/bank; `past[]` season history; `chips[]` with the GW each chip was played |
| `entry/{id}/transfers/` | every transfer this season: `element_in/out`, `element_in_cost`, `event`, `time` |
| `entry/{id}/event/{gw}/picks/` | the 15 picks, captain, chip, `entry_history` |

**`picks/` returns 404 for a gameweek whose deadline has not passed** (verified:
GW2 → 404). Teams are hidden until lockout, which is correct behaviour — the bot
cannot spoil anyone's plans, and "who's captaining whom" only works post-deadline.

---

## 5. `bootstrap-static/` — the reference data

One 1.6 MB call, refreshed a few times a day. Contains:

* `elements[604]` — every player. Beyond the obvious: `form`, `ep_this`/`ep_next`
  (FPL's own expected points), `selected_by_percent`, `status` + `news` +
  `chance_of_playing_next_round` (injury flags), `expected_goals`/`expected_assists`
  and the `_per_90` variants, `defensive_contribution`, `bps`, `ict_index`,
  penalty/set-piece order, `cost_change_event`, `now_cost`.
* **New for 2026/27:** `price_change_projections`, `price_change_hourly_rate`,
  `price_change_percent`, `price_change_locked_until` — FPL now publishes its own
  price-change forecasting, so the bot can do price alerts without a third-party
  scraper.
* `events[38]` — `deadline_time`, `deadline_time_epoch`, `is_current`/`is_next`/
  `is_previous`, `finished`, `data_checked`, `average_entry_score`,
  `highest_score`, `most_captained`, `most_selected`, `most_transferred_in`,
  `chip_plays[]`, `top_element_info`, `ranked_count`.
* `teams[20]` — short names, form, and the six `strength_*` ratings.
* `element_types[4]` — GK/DEF/MID/FWD with squad rules (1 GK, 3-5 DEF, 2-5 MID,
  1-3 FWD, 11 on pitch).
* `chips[8]`, `phases[11]`, `game_config.scoring` (all point values, including
  the `defensive_contribution` rules), `total_players` (9,327,530 this season).

### 2026/27 rule changes visible in the API

`chips` has **eight** entries, not four: wildcard, free hit, bench boost and
triple captain each appear twice — once for `start_event 1/2 → stop_event 19`
and once for `20 → 38`. Chips reset at GW20 and **the bot must track two halves
separately** when reporting who has what left.

`game_config.scoring` still carries `mng_*` keys (manager scoring) but
`element_types` has only 4 positions, so managers are not selectable this season.

---

## 6. Live-match data

`GET /event/{gw}/live/` — 422 KB, all 604 players:

```jsonc
{ "elements": [ { "id": 1,
    "stats": { "minutes": 90, "goals_scored": 0, "assists": 0, "clean_sheets": 1,
               "bonus": 0, "bps": 24, "saves": 1, "defensive_contribution": 0,
               "expected_goals": "0.00", "total_points": 6, ... },
    "explain": [ { "fixture": 1, "stats": [
        {"identifier":"minutes","points":2,"value":90,"points_modification":0},
        {"identifier":"assists","points":3,"value":1,"points_modification":0},
        {"identifier":"clean_sheets","points":4,"value":1,"points_modification":0},
        {"identifier":"bonus","points":2,"value":2,"points_modification":0} ] } ] } ] }
```

`explain` is a per-fixture point breakdown — this is what powers a
"why did Haaland get 13?" reply, and diffing consecutive polls of it is how the
bot detects "Saka just assisted" without any external feed.

`GET /fixtures/?event={gw}` — 16 KB. `started`, `finished`,
`finished_provisional`, `minutes`, live scores, `kickoff_time`, and a `stats[]`
array of goals/assists/cards/bps/saves per fixture with player IDs. Cheap enough
to poll every 30-60s; this is the bot's clock.

`GET /event-status/` — tiny. Per-day `bonus_added` and a `leagues` field that
flips to `"Updated"` when FPL has finished recomputing league tables. The correct
signal for "the gameweek is truly final, post the wrap-up".

---

## 7. Limits and operational reality

* **No documented rate limit, no auth throttle.** 25 sequential and 60 requests
  at concurrency 30 all returned 200 with no `429` and no `Retry-After`.
* That is **not** permission to hammer it. It is an undocumented private API
  behind Cloudflare; abusive traffic gets IP-blocked, and there is no appeal.
  Budget: keep steady-state under ~1 request/second, cap concurrency at ~5, and
  back off exponentially on any 429/403/5xx.
* **The real cost driver is `picks/`: one call per manager per gameweek.** Six
  leagues × 20 managers = 120 calls. But picks are **immutable after the
  deadline** (only `automatic_subs` and `entry_history` change), so fetch each
  manager's picks once per gameweek and cache for the whole week. Live points
  then cost **one** `event/{gw}/live/` call for *all* managers in *all* leagues.
* `bootstrap-static/` is 1.6 MB — cache 6-12 h, and never call it per command.
* No CORS-friendly headers, no webhooks, no push. Everything is polling.
* No official terms permit commercial use. A private bot for friend leagues is
  the normal, tolerated use case; don't publish it as a paid service.
* Endpoints have changed shape between seasons before (this season added
  `price_change_projections`, `region`, `known_name`, `scout_risks`). Parse
  defensively — treat unknown fields as optional.

---

## 8. Endpoints the bot will actually use

| Purpose | Endpoint | Cache TTL |
|---|---|---|
| Reference data | `bootstrap-static/` | 6 h (30 min near deadline) |
| Gameweek clock | `event-status/` | 60 s |
| Fixture state | `fixtures/?event={gw}` | 30 s live / 30 min idle |
| Live player points | `event/{gw}/live/` | 45 s live / 1 h settled |
| League members | `leagues-classic/{id}/standings/` | 10 min |
| Manager picks | `entry/{id}/event/{gw}/picks/` | until end of GW |
| Manager season | `entry/{id}/history/` | 1 h |
| Transfers | `entry/{id}/transfers/` | 10 min |
| Player detail | `element-summary/{id}/` | 6 h |
