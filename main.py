"""FPL Rival Tracker — Telegram bot (Phase 1: /start, /setteam, /myteam)."""
import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db
from fpl_client import FPLClient, FPLError, FPLNotFound

log = logging.getLogger("fpl-bot")

POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

WELCOME = (
    "👋 <b>FPL Rival Tracker</b>\n\n"
    "Beat the specific rivals in your mini-league.\n\n"
    "1. /setteam &lt;team_id&gt; — your team ID is the number in your FPL URL "
    "(fantasy.premierleague.com/entry/<b>1234567</b>/event/5)\n"
    "2. /myteam — see your squad, points and rank\n\n"
    "League scanning is coming soon."
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
        await update.message.reply_text("Usage: /setteam <team_id>  (digits only, e.g. /setteam 1234567)")
        return
    team_id = int(context.args[0])
    fpl: FPLClient = context.application.bot_data["fpl"]
    try:
        entry = await fpl.entry(team_id)
    except FPLNotFound:
        await update.message.reply_text("I couldn't find that team ID. Check the number in your FPL team URL.")
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
