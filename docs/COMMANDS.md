# Feature & command catalogue

Design rule: **one command = one message you'd actually want in a group chat.**
Everything fits a phone screen, uses monospace tables for alignment, and offers
inline buttons for the obvious follow-up rather than making people type again.

`<league>` is optional everywhere — omit it and the bot uses the chat's default
league. Groups with several leagues linked get a picker.

---

## A. Setup & identity

| Command | What it does |
|---|---|
| `/link <league_id>` | Bind an FPL league to this chat. Fetches the standings, confirms the name, becomes default if it's the first. |
| `/leagues` | List leagues bound to this chat; buttons to set default / unlink. |
| `/me <entry_id>` | Claim a team as yours, so the bot can @-mention you and personalise replies. Also accepts a pasted `fantasy.premierleague.com/entry/123/event/5` URL. |
| `/whois` | Who in the league has claimed their team, who hasn't. |
| `/settings` | Toggle alert categories, set quiet hours, pick timezone (defaults Asia/Singapore). |

---

## B. The core three (asked for)

| Command | What it does |
|---|---|
| `/live` | **The flagship.** Live-computed table: rank, name, live GW points, players yet to play, players in play, chip, captain and whether the captain has played. Auto-refresh button. |
| `/left` | Just the "to play" column, sorted — who still has bullets in the chamber. Splits into *yet to kick off* / *currently on the pitch*. |
| `/diff [manager]` | Differentials. Two modes: league-wide (players owned by ≤ 1-2 managers, with their live points) and head-to-head (`/diff @mark` → exactly which players separate you two, and the net swing so far). |

---

## C. Live-gameweek features worth having

| Command | What it does |
|---|---|
| `/captains` | Captain armband spread across the league, with live captain points and how much each is up or down versus the most popular pick. |
| `/bench` | Points rotting on benches, live. Reliably the funniest message of the weekend. |
| `/chips` | Who has played what, and — because chips reset at GW20 in 2026/27 — what each manager has left **in the current half**. |
| `/eo <player>` | Effective ownership within the league (ownership + captaincy), the number that actually decides whether a haul helps or hurts you. |
| `/swing` | Biggest rank movers since the gameweek started, and the current projected final table. |
| `/hits` | Who took transfer hits this week and whether the players brought in have paid them back yet. |
| `/mini <a> <b>` | Head-to-head deep dive between two managers: shared players, unique players, running point differential this GW and season. |
| `/watch <player>` | Personal alert when that player scores, assists, gets subbed or picks up a card. |

## D. Between gameweeks

| Command | What it does |
|---|---|
| `/deadline` | Countdown to the next deadline in each member's local time. Auto-posts at T-24h, T-2h, T-15m. |
| `/table [gw]` | Official standings, plus a per-gameweek league table (`/table 3` = who won GW3). |
| `/form` | Rolling last-4-gameweek points per manager — who's actually hot versus who banked a big GW1. |
| `/transfers` | Every transfer made in the league since the last deadline, grouped by manager. |
| `/template` | The league's "template team": players owned by >50% of the group, and who is bravest (lowest overlap with it). |
| `/rank` | Overall ranks with weekly deltas, plus percentile within the league. |
| `/season` | Season-long story: cumulative points chart, biggest single GW, worst GW, total hits paid, total bench points, best/worst captain calls. |
| `/prices` | Price risers and fallers among players your league owns, using the new 2026/27 `price_change_projections` field. |
| `/news` | Injury and availability flags **for players owned in this league only** — filtered, not the global list. |
| `/fixtures [team]` | Next 5 fixtures with FDR colouring; `/fixtures ARS` for one club. |
| `/player <name>` | Player card: form, xG/xA per 90, ownership, upcoming fixtures, price trajectory, and who in the league owns them. |
| `/dream` | Gameweek dream team, and which of your league actually owned each pick. |

## E. Social / group-chat glue

| Command | What it does |
|---|---|
| `/awards` | End-of-gameweek awards: Manager of the Week, Bench Warmer, Captain Disaster, Best Differential, Lucky Escape. Auto-posts when the GW finalises. |
| `/streaks` | Longest current run of green arrows / top-half finishes / weeks above league average. |
| `/predict` | Simple projected final standings from current form and remaining fixtures. Deliberately naive and therefore argument-provoking. |
| `/roast` | One-line generated jab at whoever finished last this week. Opt-in per chat. |
| `/poll <question>` | Native Telegram poll — "who's captaining Haaland?" |
| `/h2h` | For head-to-head leagues: this week's fixtures with live scores. |
| `/cup` | FPL Cup progress once it starts (GW14+ this season). |

## F. Automatic posts (no command)

| Trigger | Message |
|---|---|
| T-24h / T-2h / T-15m before deadline | Deadline reminder; the T-15m one @-mentions anyone whose team looks unchanged since last GW. |
| First kickoff of the GW | "Gameweek 4 is live" + the starting table. |
| A league-owned player scores / assists | "⚽ Saka (owned by Mark, Jia Hao, +2) — Mark is captaining him." Batched into one message per 60s to avoid spam. |
| Red card / early sub of an owned player | Immediate, it changes plans. |
| Lead change in the live table | "Wei Ming takes the lead, +3 over Mark." Rate-limited to once every 5 minutes. |
| Final whistle of the last GW fixture | Provisional final table + bench/captain summary. |
| `event-status.bonus_added` flips true | Confirmed table with bonus applied, and a diff versus the provisional one. |
| GW finalised (`data_checked`) | Full wrap-up + `/awards`. |
| Price change window (~01:30 UTC) | Only if it affects a player someone in the league owns. |

---

## Interaction patterns

* **Inline buttons over typing.** Every table ends with `🔄 Refresh`, and
  `/live` offers `Bench · Captains · Differentials` so the whole gameweek can be
  browsed by tapping.
* **Auto-refreshing message.** `/live` during a match edits its own message every
  60s for 15 minutes instead of posting new ones, then parks with a Refresh
  button. One message, always current, no chat flood.
* **Inline mode.** `@yourbot haaland` in *any* chat returns a player card —
  useful for arguing in a chat the bot isn't a member of.
* **Quiet hours.** SGT means European night matches finish around 06:00. Default
  quiet hours 01:00-08:00 SGT queue alerts into a single morning digest.
* **Per-chat alert profile.** `all` / `big-moments` / `digest-only` / `off`.
