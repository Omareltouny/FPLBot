# FPL Rival Tracker — Phase 1

A Telegram bot for Fantasy Premier League managers. Phase 1 is the core skeleton: onboarding and viewing your own squad. League scanning, differentials, live tracking and payments come in later phases.

## What works in Phase 1

| Command | What it does |
|---|---|
| `/start` | Introduces the bot and how to find your team ID |
| `/setteam <team_id>` | Saves your FPL team ID (validated against the FPL API) |
| `/myteam` | Shows your total points, overall rank, and current squad (starting XI and bench, captain and vice marked) |

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

Open your team on the FPL website. The ID is the number in the URL:
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

Tests use mocked FPL responses (retry logic, 404 handling, caching, database, admin bypass, squad formatting). They do not call the live API or Telegram.

## Project layout

```
main.py          Bot entry point and command handlers
fpl_client.py    Async FPL API client (User-Agent, retries, cache)
db.py            SQLite schema and helpers, subscription check
config.py        Settings loaded from .env
tests/           Unit tests
.env.example     Config template (copy to .env, never commit .env)
```

## Troubleshooting

- **"TELEGRAM_BOT_TOKEN is not set"**: `.env` is missing or not in the folder you run `main.py` from.
- **"FPL API is unavailable"**: the API goes down around gameweek deadlines and between seasons. Wait and retry; the bot retries automatically first.
- **"I couldn't find that team ID"**: use the number from your FPL team URL, digits only.
- **`/myteam` shows the previous gameweek**: your team has no picks for the current gameweek yet, so the bot falls back one.

## Deploying to a server

Run it as a `systemd` service (see section 10 of the build spec) so it restarts on crash or reboot. Keep `.env` on the server and out of git.

## Roadmap

- Phase 2: `/addleague`, `/leaguescan`, `/differentials`, large-league support
- Phase 3: live win probability, transfer alerts, chip prediction
- Phase 4: Telegram Stars payments, free/paid gating, `/upgrade`
