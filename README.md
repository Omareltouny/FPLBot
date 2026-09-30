# FPL Rival Tracker — Phases 1–2

A Telegram bot for Fantasy Premier League managers. Phase 1 is onboarding and viewing your own squad. Phase 2 adds mini-league rival analysis, designed for **large leagues (20,000–50,000 managers)**. Live tracking, alerts and payments come in later phases.

## Commands

| Command | What it does |
|---|---|
| `/start` | Introduces the bot and how to find your team ID |
| `/setteam <team_id>` | Saves your FPL team ID (validated against the FPL API) |
| `/myteam` | Shows your total points, overall rank, and current squad (starting XI and bench, captain and vice marked) |
| `/addleague` | With no argument, lists your leagues and their IDs. `/addleague <league_id>` starts tracking one (classic leagues only) |
| `/myleagues`, `/removeleague <id>` | List or remove tracked leagues. Commands use the first tracked league unless you pass an ID |
| `/rival <name or team_id>` | **Free.** Your squad vs one rival: shared players, who owns what only, captains |
| `/leaguescan [league_id]` | **Paid.** Rank and point gap vs rivals, premium players you don't own, chips rivals still have |
| `/differentials [league_id]` | **Paid.** Your low-ownership players, and popular players you're missing |

Paid commands are available to `ADMIN_TELEGRAM_IDS` until payments arrive in Phase 4.

## How it handles 20k–50k manager leagues

A 50k league is 1,000 standings pages, so the bot never scans a whole league. It analyses a **sample**:

- the **top 20** managers (one standings page), and
- the **10 managers above and below you**, found by jumping straight to your page using your league rank from `entry/{id}/` (typically 2–3 requests in total),

then fetches squads and chip history only for those ~30 managers, at most 6 requests at a time. Squads are cached in SQLite for 30 minutes and standings pages for 10 minutes, and the bootstrap data for an hour, so repeat commands are fast. Every report states what the sample is, and ownership percentages are percentages of that sample, not of the whole league.

`/rival <name>` only searches that sample. For anyone else, use `/rival <team_id>` (the ID is in their Points-tab URL).

Tunable in `.env`: `LEAGUE_TOP_N` (max 50), `LEAGUE_NEIGHBORS`, `MAX_CONCURRENCY`.

Chips: availability is read from the `chips` windows in the FPL data when present, so it follows the season's rules (for example chips split across halves of the season). If those windows are missing, the bot assumes one of each chip for the season.

Also included: browser User-Agent header and retries with backoff on the FPL API, a one-hour `bootstrap-static` cache (stale data is served if the API is down), the SQLite schema, and an admin allow-list that bypasses the subscription check.

## Requirements

- Python 3.11+
- A Telegram account

## Setup

### 1. Create the bot and get a token

1. In Telegram, message **@BotFather** and send `/newbot`.
2. Choose a name and a username (must end in `bot`).
3. Copy the token it gives you.

### 2. Get your Telegram user ID

Message **@userinfobot**. It replies with your numeric ID.

### 3. Find your FPL team ID

Open the FPL website, select the **Points** tab and check the URL. The ID is the number after `/entry/`:
`fantasy.premierleague.com/entry/`**`1234567`**`/event/5`

### 4. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN=your-token-from-botfather
ADMIN_TELEGRAM_IDS=your-telegram-user-id
```

`ADMIN_TELEGRAM_IDS` is comma-separated. Listed users always get full (paid) access. Optional: `DB_PATH` (default `fpl_tracker.db`) and `LOG_LEVEL` (default `INFO`).

### 5. Install and run

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

The bot uses polling, so no webhook, domain or open port is needed. In Telegram, message your bot: `/start`, `/setteam <your id>`, `/myteam`.

## Tests

```bash
pytest
```

Tests use mocked FPL responses, including a simulated 50,000-manager league (retry logic, caching, database, admin bypass, league scope, chips, reports). They do not call the live API or Telegram.

## Project layout

```
main.py          Bot entry point and command handlers
fpl_client.py    Async FPL API client (User-Agent, retries, cache)
league.py        League scope, squad/chip snapshots, analysis and report formatting
db.py            SQLite schema and helpers, subscription check
config.py        Settings loaded from .env
tests/           Unit tests
.env.example     Config template (copy to .env, never commit .env)
```

## Troubleshooting

- **"TELEGRAM_BOT_TOKEN is not set"**: `.env` is missing or not in the folder you run `main.py` from.
- **"FPL API is unavailable"**: the API goes down around gameweek deadlines and between seasons. Wait and retry; the bot retries automatically first.
- **"I couldn't find that team ID"**: use the number from your FPL team URL, digits only.
- **"I couldn't find you in this league"** in a scan: your league rank couldn't be matched; the report falls back to the top managers only.
- **`/myteam` shows the previous gameweek**: your team has no picks for the current gameweek yet, so the bot falls back one.

## Deploying to a server

Run it as a `systemd` service (see section 10 of the build spec) so it restarts on crash or reboot. Keep `.env` on the server and out of git.

## Roadmap

- Phase 3: live win probability, transfer alerts, chip prediction
- Phase 4: Telegram Stars payments, free/paid gating, `/upgrade`
