"""FPL Rival Tracker — Telegram bot (Phases 1-2)."""
import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db
import league
from fpl_client import FPLClient, FPLError, FPLNotFound

log = logging.getLogger("fpl-bot")

POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

WELCOME = (
    "👋 <b>FPL Rival Tracker</b>\n\n"
    "Beat the specific rivals in your mini-league.\n\n"
    "<b>Finding your team ID:</b> open the FPL website, select the <b>Points</b> tab and check the URL. "
    "The number after /entry/ is your team ID "
    "(fantasy.premierleague.com/entry/<b>1234567</b>/event/5).\n\n"
    "1. /setteam &lt;team_id&gt;\n"
    "2. /myteam — your squad, points and rank\n"
    "3. /addleague — pick your mini-league\n"
    "4. /rival &lt;name or team id&gt; — compare squads with one rival\n"
    "5. /leaguescan and /differentials — full rival analysis (paid)\n"
    "Also: /myleagues, /removeleague &lt;id&gt;"
)


def format_squad(picks: list[dict], players: dict[int, dict], teams: dict[int, dict]) -> str:
    def line(p: dict) -> str:
        el = players.get(p["element"], {})
        tag = " (C)" if p.get("is_captain") else " (VC)" if p.get("is_vice_captain") else ""
        team = teams.get(el.get("team"), {}).get("short_name", "?")
        return f"{html.escape(el.get('web_name', '?'))}{tag} — {team}"

    out = []
    for label, group in (("Starting XI", picks[:11]), ("Bench", picks[11:])):
        rows = []
        for pos in (1, 2, 3, 4):
            names = [line(p) for p in group if players.get(p["element"], {}).get("element_type") == pos]
            if names:
                rows.append(f"<b>{POSITIONS[pos]}</b>: " + ", ".join(names))
        out.append(f"<u>{label}</u>\n" + "\n".join(rows))
    return "\n\n".join(out)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def setteam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /setteam <team_id> (digits only, e.g. /setteam 1234567).\n"
            "To find it: open the FPL website, select the Points tab and check the URL — the number after /entry/ is your team ID.")
        return
    team_id = int(context.args[0])
    fpl: FPLClient = context.application.bot_data["fpl"]
    try:
        entry = await fpl.entry(team_id)
    except FPLNotFound:
        await update.message.reply_text("I couldn't find that team ID. Open the Points tab on the FPL website and check the number after /entry/ in the URL.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now (often around deadlines). Try again in a few minutes.")
        return
    db.set_team(update.effective_user.id, team_id)
    await update.message.reply_text(
        f"✅ Saved: {entry.get('name', 'your team')} (manager: "
        f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}). Try /myteam.".replace("  ", " ")
    )


async def myteam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = db.get_user(update.effective_user.id)
    if not user or not user["fpl_team_id"]:
        await update.message.reply_text("Set your team first: /setteam <team_id>")
        return
    team_id = user["fpl_team_id"]
    fpl: FPLClient = context.application.bot_data["fpl"]
    try:
        entry = await fpl.entry(team_id)
        gw = await fpl.current_gameweek()
        try:
            picks = await fpl.picks(team_id, gw)
        except FPLNotFound:
            # Team may not have picks for this GW yet (e.g. joined late) — fall back one GW.
            gw = max(gw - 1, 1)
            picks = await fpl.picks(team_id, gw)
        boot = await fpl.bootstrap()
    except FPLNotFound:
        await update.message.reply_text("I couldn't load that team. Re-run /setteam with a valid ID.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return

    players = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    rank = entry.get("summary_overall_rank")
    header = (
        f"<b>{html.escape(entry.get('name', 'Team'))}</b>\n"
        f"Total points: <b>{entry.get('summary_overall_points', '?')}</b>\n"
        f"Overall rank: <b>{f'{rank:,}' if isinstance(rank, int) else '?'}</b>\n"
        f"Squad for GW{gw}\n\n"
    )
    await update.message.reply_text(header + format_squad(picks["picks"], players, teams), parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ Phase 2
PAYWALL = "🔒 This is a paid feature. Payments are coming soon — for now it's available to admins only."


async def send_sections(update: Update, sections: list[str], limit: int = 3800) -> None:
    """Pack sections into Telegram-sized messages (4096 char cap)."""
    chunk = ""
    for sec in sections:
        sec = sec[:limit]
        if chunk and len(chunk) + 2 + len(sec) > limit:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            chunk = ""
        chunk = f"{chunk}\n\n{sec}" if chunk else sec
    if chunk:
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def addleague(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user = db.get_user(uid)
    if not user or not user["fpl_team_id"]:
        await update.message.reply_text("Set your team first: /setteam <team_id>")
        return
    fpl: FPLClient = context.application.bot_data["fpl"]

    if not context.args:  # list the user's own leagues so they can pick an ID
        try:
            entry = await fpl.entry(user["fpl_team_id"])
        except FPLError:
            await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
            return
        classic = (entry.get("leagues") or {}).get("classic", [])
        private = [l for l in classic if l.get("league_type") == "x"] or classic
        if not private:
            await update.message.reply_text("Usage: /addleague <league_id>\nFind it in your league's standings page URL.")
            return
        lines = [f"• <b>{html.escape(l['name'])}</b> — ID <code>{l['id']}</code> (your rank {l.get('entry_rank', '?')})"
                 for l in private[:20]]
        await update.message.reply_text(
            "Your leagues — add one with /addleague &lt;id&gt;:\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if not context.args[0].isdigit():
        await update.message.reply_text("Usage: /addleague <league_id> (digits only)")
        return
    league_id = int(context.args[0])
    try:
        data = await fpl.standings(league_id, 1)
    except FPLNotFound:
        await update.message.reply_text(
            "I couldn't find a classic league with that ID (head-to-head leagues aren't supported yet). "
            "Send /addleague on its own to see your leagues and their IDs.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    name = data["league"]["name"]
    db.add_league(uid, league_id, name)
    await update.message.reply_text(f"✅ Tracking <b>{html.escape(name)}</b>. Try /rival or /leaguescan.",
                                    parse_mode=ParseMode.HTML)


async def myleagues(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    leagues = db.get_leagues(update.effective_user.id)
    if not leagues:
        await update.message.reply_text("No leagues yet. Use /addleague")
        return
    lines = [f"• <b>{html.escape(l['league_name'] or '?')}</b> — <code>{l['league_id']}</code>"
             + (" (default)" if i == 0 else "") for i, l in enumerate(leagues)]
    await update.message.reply_text(
        "Tracked leagues (commands use the first one unless you pass an ID):\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML)


async def removeleague(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removeleague <league_id>")
        return
    ok = db.remove_league(update.effective_user.id, int(context.args[0]))
    await update.message.reply_text("Removed." if ok else "That league isn't tracked.")


async def _resolve(update: Update, league_arg: str | None):
    """-> (my_team_id, league_row) or None after replying with what's missing."""
    uid = update.effective_user.id
    user = db.get_user(uid)
    if not user or not user["fpl_team_id"]:
        await update.message.reply_text("Set your team first: /setteam <team_id>")
        return None
    leagues = db.get_leagues(uid)
    if not leagues:
        await update.message.reply_text("Add a league first: /addleague")
        return None
    if league_arg and league_arg.isdigit():
        chosen = next((l for l in leagues if l["league_id"] == int(league_arg)), None)
        if not chosen:
            await update.message.reply_text("That league isn't tracked. See /myleagues.")
            return None
        return user["fpl_team_id"], chosen
    return user["fpl_team_id"], leagues[0]


async def _scan(context: ContextTypes.DEFAULT_TYPE, team_id: int, league_id: int):
    fpl: FPLClient = context.application.bot_data["fpl"]
    lock = context.application.bot_data.setdefault("locks", {}).setdefault(league_id, asyncio.Lock())
    async with lock:  # don't run two scans of the same league at once
        gw = await fpl.current_gameweek()
        boot = await fpl.bootstrap()
        scope = await league.find_scope(fpl, league_id, team_id)
        snaps, failed = await league.gather_snapshots(fpl, scope, gw)
    return scope, snaps, failed, boot, gw


async def _paid_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, builder) -> None:
    if not db.is_subscribed(update.effective_user.id):
        await update.message.reply_text(PAYWALL)
        return
    res = await _resolve(update, context.args[0] if context.args else None)
    if not res:
        return
    team_id, lg = res
    await update.message.reply_text("🔍 Scanning your rivals — this can take ~15 seconds in a big league…")
    try:
        scope, snaps, failed, boot, gw = await _scan(context, team_id, lg["league_id"])
    except FPLNotFound:
        await update.message.reply_text("I couldn't load that league. Is it a classic league? Try /addleague again.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    await send_sections(update, builder(scope, snaps, failed, boot, gw))


async def leaguescan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _paid_scan(update, context, league.build_leaguescan)


async def differentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _paid_scan(update, context, league.build_differentials)


async def rival(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free: compare with ONE rival, by manager/team name (searched in the sample) or team ID."""
    if not context.args:
        await update.message.reply_text("Usage: /rival <manager or team name> or /rival <team_id>")
        return
    res = await _resolve(update, None)
    if not res:
        return
    my_id, lg = res
    fpl: FPLClient = context.application.bot_data["fpl"]
    query = " ".join(context.args).strip()
    try:
        gw = await fpl.current_gameweek()
        boot = await fpl.bootstrap()
        if query.isdigit():
            rival_id = int(query)
            label = (await fpl.entry(rival_id)).get("name", f"team {rival_id}")
        else:
            scope = await league.find_scope(fpl, lg["league_id"], my_id)
            q = query.lower()
            hits = [m for m in scope.members if m.entry != my_id
                    and (q in m.player_name.lower() or q in m.entry_name.lower())]
            if not hits:
                await update.message.reply_text(
                    "No match among the top managers or the ones around you (big leagues aren't searched in full). "
                    "Use /rival <team_id> for anyone — the ID is in their Points-tab URL.")
                return
            if len(hits) > 1:
                lines = [f"• #{m.rank:,} {html.escape(m.player_name)} / {html.escape(m.entry_name)} — <code>{m.entry}</code>"
                         for m in hits[:8]]
                await update.message.reply_text("Several matches — use /rival &lt;team_id&gt;:\n\n" + "\n".join(lines),
                                                parse_mode=ParseMode.HTML)
                return
            rival_id, label = hits[0].entry, hits[0].player_name or hits[0].entry_name
        mine, theirs = await asyncio.gather(
            league.load_snapshot(fpl, lg["league_id"], my_id, gw),
            league.load_snapshot(fpl, lg["league_id"], rival_id, gw))
    except FPLNotFound:
        await update.message.reply_text("I couldn't find that manager or league.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    players = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    await send_sections(update, league.compare_squads(mine.picks, theirs.picks, players, teams, label))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again.")


async def post_init(app: Application) -> None:
    app.bot_data["fpl"] = FPLClient()


async def post_shutdown(app: Application) -> None:
    fpl = app.bot_data.get("fpl")
    if fpl:
        await fpl.aclose()


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setteam", setteam))
    app.add_handler(CommandHandler("myteam", myteam))
    app.add_handler(CommandHandler("addleague", addleague))
    app.add_handler(CommandHandler("myleagues", myleagues))
    app.add_handler(CommandHandler("removeleague", removeleague))
    app.add_handler(CommandHandler("rival", rival))
    app.add_handler(CommandHandler("leaguescan", leaguescan))
    app.add_handler(CommandHandler("differentials", differentials))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    db.init_db()
    log.info("Starting bot (polling). Admins: %d", len(config.ADMIN_TELEGRAM_IDS))
    build_app().run_polling()


if __name__ == "__main__":
    main()
