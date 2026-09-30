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
CREATE TABLE IF NOT EXISTS alert_subs (
    telegram_id INTEGER PRIMARY KEY,
    league_id INTEGER,
    enabled INTEGER DEFAULT 1,
    transfers INTEGER DEFAULT 1,
    rank_threshold INTEGER DEFAULT 5,   -- 0 = rank alerts off
    last_rank INTEGER
);
CREATE TABLE IF NOT EXISTS seen_transfers (
    telegram_id INTEGER,
    fpl_team_id INTEGER,
    last_time TEXT,                     -- newest transfer timestamp already alerted ('' = baseline, none yet)
    PRIMARY KEY (telegram_id, fpl_team_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_key
    ON rival_snapshots (league_id, fpl_team_id, gameweek);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_key
    ON tracked_leagues (telegram_id, league_id);
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


def add_league(telegram_id: int, league_id: int, name: str, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO tracked_leagues (telegram_id, league_id, league_name) VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_id, league_id) DO UPDATE SET league_name = excluded.league_name",
            (telegram_id, league_id, name),
        )


def remove_league(telegram_id: int, league_id: int, path: str | None = None) -> bool:
    with connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM tracked_leagues WHERE telegram_id = ? AND league_id = ?",
            (telegram_id, league_id),
        )
        return cur.rowcount > 0


def get_leagues(telegram_id: int, path: str | None = None) -> list:
    with connect(path) as conn:
        return conn.execute(
            "SELECT * FROM tracked_leagues WHERE telegram_id = ? ORDER BY rowid", (telegram_id,)
        ).fetchall()


def get_snapshot(league_id: int, team_id: int, gw: int, path: str | None = None):
    with connect(path) as conn:
        return conn.execute(
            "SELECT * FROM rival_snapshots WHERE league_id = ? AND fpl_team_id = ? AND gameweek = ?",
            (league_id, team_id, gw),
        ).fetchone()


def save_snapshot(league_id: int, team_id: int, gw: int, squad_json: str, chips_json: str,
                  path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rival_snapshots "
            "(league_id, fpl_team_id, gameweek, squad_json, chips_used_json, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (league_id, team_id, gw, squad_json, chips_json, datetime.now(timezone.utc).isoformat()),
        )


# ------------------------------------------------------------------ alerts
def get_alert(telegram_id: int, path: str | None = None):
    with connect(path) as conn:
        return conn.execute("SELECT * FROM alert_subs WHERE telegram_id = ?", (telegram_id,)).fetchone()


def upsert_alert(telegram_id: int, path: str | None = None, **fields) -> None:
    allowed = {"league_id", "enabled", "transfers", "rank_threshold", "last_rank"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    with connect(path) as conn:
        conn.execute("INSERT OR IGNORE INTO alert_subs (telegram_id) VALUES (?)", (telegram_id,))
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE alert_subs SET {sets} WHERE telegram_id = ?", (*fields.values(), telegram_id))


def active_alerts(path: str | None = None) -> list:
    with connect(path) as conn:
        return conn.execute("SELECT * FROM alert_subs WHERE enabled = 1 AND league_id IS NOT NULL").fetchall()


def get_last_transfer_time(telegram_id: int, team_id: int, path: str | None = None) -> str | None:
    with connect(path) as conn:
        row = conn.execute("SELECT last_time FROM seen_transfers WHERE telegram_id = ? AND fpl_team_id = ?",
                           (telegram_id, team_id)).fetchone()
        return row["last_time"] if row else None


def set_last_transfer_time(telegram_id: int, team_id: int, time_str: str, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO seen_transfers (telegram_id, fpl_team_id, last_time) VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_id, fpl_team_id) DO UPDATE SET last_time = excluded.last_time",
            (telegram_id, team_id, time_str),
        )


def prune_snapshots(keep_from_gw: int, path: str | None = None) -> int:
    """Drop cached squads from old gameweeks so the table doesn't grow every round."""
    with connect(path) as conn:
        return conn.execute("DELETE FROM rival_snapshots WHERE gameweek < ?", (keep_from_gw,)).rowcount
