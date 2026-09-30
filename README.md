# FPL Rival Tracker — v5 (free trial release)

A Telegram bot for Fantasy Premier League managers. Phase 1 is onboarding and viewing your own squad. Phase 2 adds mini-league rival analysis, designed for **large leagues (20,000–50,000 managers)**. Phase 3 adds live tracking, alerts and chip prediction. Phase 4 adds the transfer and wildcard advisor, which tells you how to beat your rivals. **Version 5 has no payments.** Instead everything is free for a 3-gameweek trial, and then the bot stops working until a payment method exists (see "Free trial and automatic shutdown").

## Commands

| Command | What it does |
|---|---|
| `/start` | Introduces the bot and how to find your team ID |
| `/setteam <team_id>` | Saves your FPL team ID (validated against the FPL API) |
| `/status` | Shows the free-trial window and your access. Keeps working after the trial ends |
| `/myteam` | Shows your total points, overall rank, and current squad (starting XI and bench, captain and vice marked) |
| `/showleagues` | Lists the leagues on your FPL profile with their IDs |
| `/trackleague <league_id>` | Starts tracking one (classic leagues only). `/addleague` still works as an old alias |
| `/myleagues`, `/removeleague <id>` | List or remove tracked leagues. Commands use the first tracked league unless you pass an ID |
| `/rival <name or team_id>` | **Free.** Your squad vs one rival: shared players, who owns what only, captains |
| `/leaguescan [league_id]` | **Paid.** Rank and point gap vs rivals, premium players you don't own, chips rivals still have |
| `/differentials [league_id]` | **Paid.** Your low-ownership players, and popular players you're missing |
| `/livewinprob [league_id]` | **Paid.** During a gameweek: live score, projected final total and gap for you and each rival, plus "you're ahead of N of M rivals" |
| `/roundprize [n] [league_id]` | **Paid.** Live leaderboard for this gameweek among the top n managers (default 20, max 100) |
| `/predictchip [league_id]` | **Paid.** Which rivals may play a chip soon, plus chips about to expire |
| `/wildcard [lite\|full] [off\|low\|med\|high]` | **Paid.** Plans your wildcard. `full` (default) builds the best 15-man squad within your budget and marks every player KEEP, BUY or SELL, with XI, bench and captain. `lite` just labels your current players SELL or KEEP with the best swap for each, plus top buy targets. The last word sets how hard to chase differentials |
| `/transfers [free_transfers] [off\|low\|med\|high]` | **Paid.** The best 1, 2 and 3 transfers with the points gain, the -4 hit for each extra transfer, and a verdict on whether it's worth it. Assumes 1 free transfer; `/transfers 2` if you have two |
| `/alert` | **Paid.** Push alerts: `/alert on`, `/alert off`, `/alert rank <n>`, `/alert transfers on\|off` |

Commands marked **Paid** are free for everyone during the trial. After the trial they (and every other command) stop working, except for admins in `ADMIN_TELEGRAM_IDS` and anyone an admin has granted access.

## Free trial and automatic shutdown

- **Everything is free for 3 gameweeks.** The trial is global: it starts at the gameweek when the bot first runs (FPL's current gameweek) and covers that gameweek plus the next two. For example, if the bot first runs during GW8, it works through GW8, GW9 and GW10.
- **Then it stops.** The moment FPL's current gameweek moves past the last trial gameweek (at that gameweek's deadline), the bot refuses every command from non-admins with a "trial ended" message. Only `/start` and `/status` still answer, and both say the trial is over. The check runs before every command, so no handler, API call or alert runs for locked users.
- **Who still works:** admins (`ADMIN_TELEGRAM_IDS`), and anyone an admin grants with `/grant <telegram_id> [days]`. `/revoke <telegram_id>` takes it away. Use this for friends and testers while payments are sorted out.
- **Alerts** pause after the trial. Each alert subscriber gets one message saying so.
- **Last free gameweek:** during it, replies to non-admins end with a note that the trial ends after that gameweek.
- **If the FPL API is down,** the bot uses the last known gameweek and never re-opens a finished trial. If the API is down on the very first launch, the trial simply starts on the next successful check.
- **Settings in `.env`:** `TRIAL_GAMEWEEKS=3` sets the length (read the first time the bot runs). `TRIAL_END_GW=<n>` pins the last free gameweek and overrides everything: raise it and restart to extend the trial, lower it to stop sooner. Check the current state any time with `/status`.
- The trial start and end gameweeks live in the `settings` table of the SQLite file. Delete the file (or its `trial_*` rows) to restart the trial.

## How it handles 20k–50k manager leagues

A 50k league is 1,000 standings pages, so the bot never scans a whole league. It analyses a **sample**:

- the **top 20** managers (one standings page), and
- the **10 managers above and below you**, found by jumping straight to your page using your league rank from `entry/{id}/` (typically 2–3 requests in total),

then fetches squads and chip history only for those ~30 managers, at most 6 requests at a time. Squads are cached in SQLite for 30 minutes and standings pages for 10 minutes, and the bootstrap data for an hour, so repeat commands are fast. Every report states what the sample is, and ownership percentages are percentages of that sample, not of the whole league.

**Rivals refresh themselves every round.** Nothing is stored as a fixed rival list. Each scan and each alert check re-reads the live standings (cached 10 minutes) and re-picks the top 20 and your neighbours, so as ranks move the sample moves with them. Old cached squads are pruned automatically.

`/rival <name>` only searches that sample. For anyone else, use `/rival <team_id>` (the ID is in their Points-tab URL).

Tunable in `.env`: `LEAGUE_TOP_N` (max 50), `LEAGUE_NEIGHBORS`, `MAX_CONCURRENCY`, `ALERT_INTERVAL_MINUTES`.

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

## Live tracking, chip prediction and alerts (Phase 3)

**Live numbers** come from FPL's live feed, cached for 2 minutes, so repeated commands never hammer the API. A manager's live score applies captain/vice-captain rules, Bench Boost, transfer hits, and auto-subs once a player's match is over. The projected total adds FPL's own expected points for players still to play (scaled by minutes left in live matches, and split across double-gameweek fixtures). Bonus points may be provisional, so treat the projection as an estimate. Leagues that started mid-season are handled (points are counted from the league's start gameweek).

**`/roundprize`** only looks at the top n managers by league rank, because finding the best score in a 50,000-manager league would need every page. The winner of a round can sit outside that group, and the report says so.

**`/predictchip`** uses transparent rules on squads as they are today: Bench Boost when 6+ of a rival's 15 have a double fixture, Triple Captain when their captain has a double, Free Hit when 4+ starters have no fixture, and a flag for any unused chip whose window closes within 3 gameweeks. Thresholds live in `config.py`.

**Alerts** run in the background while the bot is running (every 15 minutes by default). `/alert on` picks the league; the first check only records a baseline, so you only hear about changes after that. You get a message when a rival in your sample makes a transfer, and when your league rank moves by your chosen number of places (default 5, set with `/alert rank <n>`). Transfer checks cost one API call per rival in the sample, shared between everyone watching the same manager.

## Transfer and wildcard advisor (Phase 4)

**How players are scored** (expected points over the next 5 gameweeks):

1. Base rate per gameweek = 50% FPL form + 35% season points per game + 15% points per £m (cheap productive players get a nudge).
2. Multiplied by fixture difficulty for each gameweek. Double gameweeks add both fixtures and blanks score zero.
3. Reduced for injuries and doubts (mainly the next two gameweeks) and for rotation risk (players who start less than about 75% of available minutes).
4. Later gameweeks count slightly less.
5. **Differential bonus:** players your rivals don't own get a boost, scaled by how many of your sampled rivals own them. `off`, `low`, `med` (default) or `high` set the strength (0%, 5%, 12% or 25% for a player none of your rivals own). With no tracked league there are no rival data and no bonus.

**The optimiser** is exact, not a greedy guess. It uses a mixed-integer programme (SciPy's HiGHS solver) to choose a legal squad (2 GK, 5 DEF, 5 MID, 3 FWD, max 3 per club) within budget, a starting XI in a legal formation, and a captain. The same model with a cap on how many players may change gives the 1, 2 and 3 transfer options. Points per million is handled by the budget constraint.

**Budget and prices:** your budget is what you'd get for your current players plus your bank. Selling prices follow FPL's rule (half of any price rise, rounded down, full loss on a fall) and are worked out from your transfer history and season-start prices.

**Limits you should know about:**
- The public FPL API shows your squad as at the last gameweek deadline. Transfers you've already made for the next gameweek aren't visible, so the advisor plans from that squad.
- Your bank comes from that same deadline snapshot.
- Free transfers aren't read from the API. `/transfers` assumes 1, or pass the number.
- This is a transparent form-and-fixtures model, not a guarantee. Weights and thresholds are in `config.py` (`W_FORM`, `W_PPG`, `W_VALUE`, `FDR_MULT`, `DIFF_LEVELS`, `SELL_GAIN_MIN` and others).

## Tests

```bash
pytest
```

Tests use mocked FPL responses, including a simulated 50,000-manager league (retry logic, caching, database, admin bypass, league scope, chips, auto-subs, live projections, chip prediction, alerts, the scoring model, the optimiser, the trial gate pushed through the real Telegram dispatcher, and the Telegram handlers end to end). They never call the live API or Telegram. They do not call the live API or Telegram.

## Project layout

```
main.py          Bot entry point and command handlers
fpl_client.py    Async FPL API client (User-Agent, retries, cache)
access.py        Free trial window, hard stop after it, who has access
advisor.py       Player scoring model, sell prices, lineups
optimize.py      Exact squad optimiser (wildcard and 1-3 transfers)
advice.py        /wildcard and /transfers answers
league.py        League scope, squad/chip snapshots, analysis and report formatting
live.py          Live scores, auto-subs, projections, /livewinprob and /roundprize
predict.py       Chip prediction
alerts.py        Background transfer and rank alerts, housekeeping
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

Alerts only fire while the bot process is running, so run it as a `systemd` service (see section 10 of the build spec) so it restarts on crash or reboot. Keep `.env` on the server and out of git.

## Roadmap

- Payments: not built. Undecided. Telegram Stars pays out in crypto and Stripe doesn't list Egypt as a supported country, so the provider needs choosing first. The access layer (`access.py`, `users.subscribed` and expiry) is ready for a payment step to plug into.
