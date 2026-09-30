"""Configuration loaded from environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "fpl_tracker.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


# Admins always bypass the subscription check.
ADMIN_TELEGRAM_IDS = _parse_ids(os.getenv("ADMIN_TELEGRAM_IDS", ""))

FPL_BASE_URL = "https://fantasy.premierleague.com/api/"
FPL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BOOTSTRAP_TTL_SECONDS = 3600

# --- Phase 2: league scanning (tuned for 20k-50k manager leagues) ---
# We never scan a whole league. Rivals = top N + the managers around you.
LEAGUE_TOP_N = min(int(os.getenv("LEAGUE_TOP_N", "20")), 50)  # max 50 (one standings page)
LEAGUE_NEIGHBORS = int(os.getenv("LEAGUE_NEIGHBORS", "10"))  # managers above AND below you
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "6"))  # simultaneous FPL requests
SNAPSHOT_TTL_SECONDS = 1800  # cached squads/chips per manager
STANDINGS_TTL_SECONDS = 600
PREMIUM_COUNT = 25  # "premium" = the N most expensive players
DIFF_MAX_OWNERSHIP = 0.10  # <=10% of sampled rivals own him => differential
TEMPLATE_MIN_OWNERSHIP = 0.40  # >=40% of sampled rivals own him => template

# --- Phase 3: live tracking, alerts, chip prediction ---
LIVE_TTL_SECONDS = 120  # event/{gw}/live/ + per-GW fixtures: at most one refresh per 2 minutes
ALERT_INTERVAL_MINUTES = int(os.getenv("ALERT_INTERVAL_MINUTES", "15"))
ALERT_DEFAULT_RANK_THRESHOLD = 5
ALERT_MAX_LINES = 15  # transfer lines per alert message
ROUNDPRIZE_MAX_N = 100  # managers considered for /roundprize (50 per standings page)
CHIP_HORIZON_GWS = 6  # how many upcoming gameweeks /predictchip looks at
BB_DOUBLE_MIN = 6  # squad players with a double fixture for a Bench Boost flag
FH_BLANK_MIN = 4  # starters with no fixture for a Free Hit flag
CHIP_EXPIRY_WARN_GWS = 3  # chip window closing within N gameweeks => flag
