"""Phase 3: background alerts (rival transfers, your league-rank changes) and housekeeping.

The rival set is NOT stored: every tick it is re-derived from the live standings (top N + the
managers around you), so it follows the league table automatically as ranks change each round.
"""
from __future__ import annotations

import asyncio
import html
import logging

from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import ContextTypes

import config
import db
import league
from fpl_client import FPLClient, FPLError

log = logging.getLogger(__name__)


def transfer_lines(scope: league.Scope, me_id: int | None, transfers: dict[int, list[dict]], telegram_id: int,
                   players: dict[int, dict], db_path: str | None = None) -> list[str]:
    """New rival transfers since we last looked. The first look only sets a baseline (no alert flood)."""
    lines: list[str] = []
    for m in scope.members:
        if m.entry == me_id or m.entry not in transfers:
            continue
        ts = transfers[m.entry]
        last = db.get_last_transfer_time(telegram_id, m.entry, db_path)
        newest = max((t["time"] for t in ts), default="")
        if last is None:
            db.set_last_transfer_time(telegram_id, m.entry, newest, db_path)
            continue
        new = sorted((t for t in ts if t["time"] > last), key=lambda t: t["time"])
        if not new:
            continue
        db.set_last_transfer_time(telegram_id, m.entry, newest, db_path)
        moves = ", ".join(
            f"{html.escape(players.get(t['element_out'], {}).get('web_name', '?'))} ➜ "
            f"{html.escape(players.get(t['element_in'], {}).get('web_name', '?'))}" for t in new)
        lines.append(f"#{m.rank:,} {html.escape(league._name(m))} (GW{new[-1].get('event', '?')}): {moves}")
    return lines


def rank_message(sub, scope: league.Scope, db_path: str | None = None) -> str | None:
    """Alert when your rank has moved >= threshold places since the last alert/baseline."""
    threshold = sub["rank_threshold"] or 0
    if threshold <= 0 or not scope.me:
        return None
    new, last = scope.me.rank, sub["last_rank"]
    if last is None:
        db.upsert_alert(sub["telegram_id"], db_path, last_rank=new)
        return None
    if abs(new - last) < threshold:
        return None
    db.upsert_alert(sub["telegram_id"], db_path, last_rank=new)
    arrow = "📈 up" if new < last else "📉 down"
    return (f"{arrow} <b>Your rank in {html.escape(scope.league_name)} moved</b>: "
            f"#{last:,} → #{new:,} ({abs(new - last):,} places)")


async def _fetch_transfers(fpl: FPLClient, ids: list[int], cache: dict[int, list[dict]]) -> dict[int, list[dict]]:
    todo = [i for i in ids if i not in cache]
    results = await asyncio.gather(*(fpl.transfers(i) for i in todo), return_exceptions=True)
    for i, res in zip(todo, results):
        if isinstance(res, list):
            cache[i] = res
        else:
            log.warning("transfers fetch failed for %s: %s", i, res)
    return {i: cache[i] for i in ids if i in cache}


async def process_subscription(bot, fpl: FPLClient, sub, players: dict, cache: dict,
                               db_path: str | None = None) -> None:
    tid = sub["telegram_id"]
    if not db.is_subscribed(tid, db_path):
        return
    user = db.get_user(tid, db_path)
    if not user or not user["fpl_team_id"]:
        return
    scope = await league.find_scope(fpl, sub["league_id"], user["fpl_team_id"])
    messages: list[str] = []

    msg = rank_message(sub, scope, db_path)
    if msg:
        messages.append(msg)

    if sub["transfers"]:
        ids = [m.entry for m in scope.members if m.entry != user["fpl_team_id"]]
        transfers = await _fetch_transfers(fpl, ids, cache)
        lines = transfer_lines(scope, user["fpl_team_id"], transfers, tid, players, db_path)
        if lines:
            extra = len(lines) - config.ALERT_MAX_LINES
            body = "\n".join(lines[: config.ALERT_MAX_LINES]) + (f"\n+{extra} more" if extra > 0 else "")
            messages.append(f"🔁 <b>Rival transfers · {html.escape(scope.league_name)}</b>\n{body}")

    for text in messages:
        try:
            await bot.send_message(chat_id=tid, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Forbidden:  # user blocked the bot: stop alerting them
            db.upsert_alert(tid, db_path, enabled=0)
            log.info("User %s blocked the bot; alerts disabled", tid)
            return


async def alert_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    fpl: FPLClient = context.application.bot_data["fpl"]
    subs = db.active_alerts()
    if not subs:
        return
    try:
        boot = await fpl.bootstrap()
    except FPLError:
        log.warning("Alert tick skipped: FPL API unavailable")
        return
    players = {e["id"]: e for e in boot["elements"]}
    cache: dict[int, list[dict]] = {}  # one transfers fetch per manager per tick, shared by all subscribers
    for sub in subs:
        try:
            await process_subscription(context.bot, fpl, sub, players, cache)
        except FPLError as exc:
            log.warning("Alert processing failed for %s: %s", sub["telegram_id"], exc)
        except Exception:
            log.exception("Alert processing crashed for %s", sub["telegram_id"])


async def maintenance(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop cached squads from past gameweeks so the snapshot table doesn't grow every round."""
    fpl: FPLClient = context.application.bot_data["fpl"]
    try:
        gw = await fpl.current_gameweek()
    except FPLError:
        return
    n = db.prune_snapshots(gw)
    if n:
        log.info("Pruned %d old snapshots", n)
