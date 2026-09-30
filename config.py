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
