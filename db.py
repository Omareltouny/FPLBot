"""SQLite storage (schema per spec section 6)."""
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    fpl_team_id INTEGER,
    subscribed INTEGER DEFAULT 0,
    subscription_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS tracked_leagues (
    telegram_id INTEGER,
    league_id INTEGER,
    league_name TEXT,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);
CREATE TABLE IF NOT EXISTS rival_snapshots (
    league_id INTEGER,
    fpl_team_id INTEGER,
    gameweek INTEGER,
    squad_json TEXT,
    chips_used_json TEXT,
    fetched_at TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def set_team(telegram_id: int, team_id: int, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO users (telegram_id, fpl_team_id) VALUES (?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET fpl_team_id = excluded.fpl_team_id",
            (telegram_id, team_id),
        )


def get_user(telegram_id: int, path: str | None = None):
    with connect(path) as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def is_subscribed(telegram_id: int, path: str | None = None) -> bool:
    """Admins always pass; otherwise need an active paid subscription."""
    if telegram_id in config.ADMIN_TELEGRAM_IDS:
        return True
    user = get_user(telegram_id, path)
    if not user or not user["subscribed"]:
        return False
    expires = user["subscription_expires_at"]
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
                return False
        except ValueError:
            return False
    return True
