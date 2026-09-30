"""FPL Rival Tracker — Telegram bot (Phases 1-5)."""
import asyncio
import html
import logging

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes, TypeHandler

import config
import access
import advice
import advisor
import alerts
import db
import league
import live
import optimize
import predict
from fpl_client import FPLClient, FPLError, FPLNotFound

log = logging.getLogger("fpl-bot")

POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

WELCOME = (
    "👋 <b>FPL Rival Tracker</b>\n\n"
    "Beat the specific rivals in your mini-league.\n\n"
    "<b>Finding your team ID:</b> open the FPL website, select the <b>Points</b> tab and check the URL. "
    "The number after /entry/ is your team ID "
    "(fantasy.premierleague.com/entry/<b>1234567</b>/event/5).\n\n"
    "<b>Setup</b>\n"
    "1. /setteam &lt;team_id&gt;\n"
    "2. /showleagues — see your leagues and their IDs\n"
    "3. /trackleague &lt;league_id&gt; — pick the one to analyse\n\n"
    "<b>Free</b>\n"
    "/myteam — your squad, points and rank\n"
    "/rival &lt;name or team id&gt; — compare squads with one rival\n\n"
    "<b>Paid</b>\n"
    "/leaguescan — rank gaps, premiums you lack, rival chips\n"
    "/differentials — your differentials and template gaps\n"
    "/livewinprob — live projected score vs your rivals\n"
    "/roundprize — live leaderboard for this gameweek\n"
    "/predictchip — which rivals may chip soon\n"
    "/alert — rival transfer and rank-change alerts\n"
    "/wildcard [lite|full] [low|med|high] — plan your wildcard: who to buy, sell, keep\n"
    "/transfers [free transfers] — best 1-3 transfers and whether each hit is worth it\n\n"
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
    state = await access.refresh(context.application.bot_data["fpl"])
    if access.is_locked(update.effective_user.id, state=state):
        await update.message.reply_text(access.denied_message(state))
        return
    await update.message.reply_text(WELCOME + "\n\n" + access.trial_line(state), parse_mode=ParseMode.HTML)


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


async def send_sections(update: Update, sections: list[str], limit: int = 3800) -> None:
    """Pack sections into Telegram-sized messages (4096 char cap)."""
    footer = access.footer_for(update.effective_user.id) if update.effective_user else ""
    if footer and sections:
        sections = sections[:-1] + [sections[-1] + footer]
    chunk = ""
    for sec in sections:
        sec = sec[:limit]
        if chunk and len(chunk) + 2 + len(sec) > limit:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            chunk = ""
        chunk = f"{chunk}\n\n{sec}" if chunk else sec
    if chunk:
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def showleagues(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the user's own leagues (with IDs) straight from their FPL profile."""
    uid = update.effective_user.id
    user = db.get_user(uid)
    if not user or not user["fpl_team_id"]:
        await update.message.reply_text("Set your team first: /setteam <team_id>")
        return
    fpl: FPLClient = context.application.bot_data["fpl"]
    try:
        entry = await fpl.entry(user["fpl_team_id"])
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    classic = (entry.get("leagues") or {}).get("classic", [])
    private = [l for l in classic if l.get("league_type") == "x"] or classic
    if not private:
        await update.message.reply_text(
            "I couldn't find any leagues on your FPL profile. If you know a league ID, use /trackleague <league_id>.")
        return
    lines = [f"• <b>{html.escape(l['name'])}</b> — ID <code>{l['id']}</code> (your rank {l.get('entry_rank', '?')})"
             for l in private[:20]]
    await update.message.reply_text(
        "Your leagues:\n\n" + "\n".join(lines) + "\n\nTrack one with /trackleague &lt;id&gt;.",
        parse_mode=ParseMode.HTML)


async def trackleague(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user = db.get_user(uid)
    if not user or not user["fpl_team_id"]:
        await update.message.reply_text("Set your team first: /setteam <team_id>")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /trackleague <league_id>\nNot sure of the ID? Send /showleagues.")
        return
    fpl: FPLClient = context.application.bot_data["fpl"]
    league_id = int(context.args[0])
    try:
        data = await fpl.standings(league_id, 1)
    except FPLNotFound:
        await update.message.reply_text(
            "I couldn't find a classic league with that ID (head-to-head leagues aren't supported yet). "
            "Send /showleagues to see your leagues and their IDs.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    name = data["league"]["name"]
    db.add_league(uid, league_id, name)
    await update.message.reply_text(f"✅ Tracking <b>{html.escape(name)}</b>. Try /rival or /leaguescan.",
                                    parse_mode=ParseMode.HTML)


async def addleague(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Old name, kept so nobody's muscle memory breaks: with an ID it tracks, without one it lists."""
    await (trackleague if context.args else showleagues)(update, context)


async def myleagues(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    leagues = db.get_leagues(update.effective_user.id)
    if not leagues:
        await update.message.reply_text("No leagues yet. Use /showleagues, then /trackleague <id>")
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
        await update.message.reply_text("Track a league first: /showleagues, then /trackleague <id>")
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
    if not access.has_access(update.effective_user.id):
        await update.message.reply_text(access.denied_message())
        return
    res = await _resolve(update, context.args[0] if context.args else None)
    if not res:
        return
    team_id, lg = res
    await update.message.reply_text("🔍 Scanning your rivals — this can take ~15 seconds in a big league…")
    try:
        scope, snaps, failed, boot, gw = await _scan(context, team_id, lg["league_id"])
    except FPLNotFound:
        await update.message.reply_text("I couldn't load that league. Is it a classic league? Try /trackleague again.")
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


# ------------------------------------------------------------------ Phase 3
async def _paid_run(update: Update, context: ContextTypes.DEFAULT_TYPE, runner) -> None:
    """Shared wrapper: paywall, league resolution, friendly API errors. runner(league_row, team_id) -> sections."""
    if not access.has_access(update.effective_user.id):
        await update.message.reply_text(access.denied_message())
        return
    res = await _resolve(update, next((a for a in context.args if a.isdigit() and int(a) > 100), None))
    if not res:
        return
    team_id, lg = res
    await update.message.reply_text("🔍 Working on it — this can take ~15 seconds in a big league…")
    try:
        sections = await runner(lg, team_id)
    except FPLNotFound:
        await update.message.reply_text("I couldn't load that league. Is it a classic league? Try /trackleague again.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    await send_sections(update, sections)


async def livewinprob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fpl: FPLClient = context.application.bot_data["fpl"]

    async def run(lg, team_id):
        scope, snaps, failed, boot, gw = await _scan(context, team_id, lg["league_id"])
        feed = live.build_feed(await fpl.live(gw), await fpl.fixtures(gw))
        elements = {e["id"]: e for e in boot["elements"]}
        return live.build_livewinprob(scope, snaps, failed, feed, elements, gw)

    await _paid_run(update, context, run)


async def roundprize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/roundprize [n] [league_id] — live gameweek leaderboard among the top n (default 20, max 100)."""
    fpl: FPLClient = context.application.bot_data["fpl"]
    n = next((int(a) for a in context.args if a.isdigit() and int(a) <= 100), config.LEAGUE_TOP_N)
    n = max(1, min(n, config.ROUNDPRIZE_MAX_N))

    async def run(lg, team_id):
        lock = context.application.bot_data.setdefault("locks", {}).setdefault(lg["league_id"], asyncio.Lock())
        async with lock:
            gw = await fpl.current_gameweek()
            boot = await fpl.bootstrap()
            scope = await league.top_members(fpl, lg["league_id"], n)
            snaps, failed = await league.gather_snapshots(fpl, scope, gw, need_history=False)
        feed = live.build_feed(await fpl.live(gw), await fpl.fixtures(gw))
        elements = {e["id"]: e for e in boot["elements"]}
        return live.build_roundprize(scope, snaps, failed, feed, elements, gw, team_id, n)

    await _paid_run(update, context, run)


async def predictchip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fpl: FPLClient = context.application.bot_data["fpl"]

    async def run(lg, team_id):
        scope, snaps, failed, boot, gw = await _scan(context, team_id, lg["league_id"])
        return predict.build_predictchip(scope, snaps, failed, boot, await fpl.fixtures(), gw)

    await _paid_run(update, context, run)


ALERT_HELP = (
    "/alert on [league_id] — start alerts for a tracked league\n"
    "/alert off — stop alerts\n"
    "/alert rank &lt;n&gt; — tell me when your league rank moves by n places or more (0 = off)\n"
    "/alert transfers on|off — rival transfer alerts"
)


async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not access.has_access(uid):
        await update.message.reply_text(access.denied_message())
        return
    args = [a.lower() for a in context.args]
    sub = db.get_alert(uid)

    if args and args[0] == "on":
        res = await _resolve(update, args[1] if len(args) > 1 else None)
        if not res:
            return
        _, lg = res
        db.upsert_alert(uid, league_id=lg["league_id"], enabled=1, last_rank=None,
                        rank_threshold=(sub["rank_threshold"] if sub else config.ALERT_DEFAULT_RANK_THRESHOLD))
        await update.message.reply_text(
            f"🔔 Alerts on for <b>{html.escape(lg['league_name'] or str(lg['league_id']))}</b>. "
            f"I check every {config.ALERT_INTERVAL_MINUTES} minutes; the first check just records a baseline, "
            "so you'll only hear about changes after that.", parse_mode=ParseMode.HTML)
    elif args and args[0] == "off":
        db.upsert_alert(uid, enabled=0)
        await update.message.reply_text("🔕 Alerts off.")
    elif len(args) == 2 and args[0] == "rank" and args[1].isdigit():
        db.upsert_alert(uid, rank_threshold=int(args[1]))
        n = int(args[1])
        await update.message.reply_text("Rank alerts off." if n == 0 else f"I'll alert you when your rank moves {n}+ places.")
    elif len(args) == 2 and args[0] == "transfers" and args[1] in ("on", "off"):
        db.upsert_alert(uid, transfers=1 if args[1] == "on" else 0)
        await update.message.reply_text(f"Rival transfer alerts {args[1]}.")
    else:
        if sub and sub["enabled"] and sub["league_id"]:
            lg = next((l for l in db.get_leagues(uid) if l["league_id"] == sub["league_id"]), None)
            status = (f"🔔 <b>On</b> for {html.escape(lg['league_name'] if lg else str(sub['league_id']))}\n"
                      f"Transfers: {'on' if sub['transfers'] else 'off'} · "
                      f"Rank alert: {sub['rank_threshold'] or 'off'}{' places' if sub['rank_threshold'] else ''}")
        else:
            status = "🔕 Alerts are off."
        await update.message.reply_text(
            status + "\n\nRivals are the top managers plus the ones around you, re-picked from the live table "
            "every check, so the list follows the standings each round.\n\n" + ALERT_HELP, parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ Phase 4
async def _advisor_run(update: Update, context: ContextTypes.DEFAULT_TYPE, diff: str, build) -> None:
    """Paywall, gather squad + rival ownership + prices, then `await build(plan, sample_note)`."""
    uid = update.effective_user.id
    if not access.has_access(uid):
        await update.message.reply_text(access.denied_message())
        return
    user = db.get_user(uid)
    if not user or not user["fpl_team_id"]:
        await update.message.reply_text("Set your team first: /setteam <team_id>")
        return
    my_id = user["fpl_team_id"]
    leagues = db.get_leagues(uid)
    league_arg = next((a for a in context.args if a.isdigit() and int(a) > 100), None)
    lg = next((l for l in leagues if str(l["league_id"]) == league_arg), None) if league_arg else (leagues[0] if leagues else None)
    await update.message.reply_text("🧮 Crunching form, fixtures and your rivals' squads — this can take ~20 seconds…")
    fpl: FPLClient = context.application.bot_data["fpl"]
    try:
        fixtures = await fpl.fixtures()
        shares, sample = None, None
        if lg:
            scope, snaps, failed, boot, gw = await _scan(context, my_id, lg["league_id"])
            my_snap = snaps.get(my_id) or await league.load_snapshot(fpl, lg["league_id"], my_id, gw)
            shares = advice.own_share(snaps, my_id)
            sample = league.sample_note(scope, snaps, failed)
        else:
            gw = await fpl.current_gameweek()
            boot = await fpl.bootstrap()
            my_snap = await league.load_snapshot(fpl, 0, my_id, gw)
        try:
            transfers = await fpl.transfers(my_id)
        except FPLError:
            transfers = None  # sell prices fall back to current prices, and the report says so
        nxt = advisor.next_gameweek(boot)
        chip_ok = "wildcard" in league.chips_available(boot, my_snap.chips, nxt)
        plan = advice.make_plan(boot, fixtures, my_snap, transfers, shares, diff, gw, chip_ok)
        sections = await asyncio.to_thread(build, plan, sample)
    except optimize.NoSolution:
        await update.message.reply_text("I couldn't build a valid squad within your budget. Try again after the next price update.")
        return
    except FPLNotFound:
        await update.message.reply_text("I couldn't load your team or league. Check /setteam and /trackleague.")
        return
    except FPLError:
        await update.message.reply_text("The FPL API is unavailable right now. Try again in a few minutes.")
        return
    await send_sections(update, sections)


async def wildcard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wildcard [lite|full] [off|low|med|high] — plan a wildcard, optionally chasing differentials harder."""
    args = [a.lower() for a in context.args]
    view = "lite" if "lite" in args else "full"
    diff = next((a for a in args if a in config.DIFF_LEVELS), config.DIFF_DEFAULT)
    await _advisor_run(update, context, diff, lambda plan, sample: advice.build_wildcard(plan, view, sample))


async def transfers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/transfers [free_transfers] [off|low|med|high] — best 1-3 transfers with a hit check."""
    args = [a.lower() for a in context.args]
    ft = next((int(a) for a in args if a.isdigit() and 1 <= int(a) <= 5), 1)
    diff = next((a for a in args if a in config.DIFF_LEVELS), config.DIFF_DEFAULT)
    await _advisor_run(update, context, diff, lambda plan, sample: advice.build_transfers(plan, ft, sample))


# ---------------------------------------------------------- trial gate (Phase 5)
ALWAYS_ALLOWED = {"start", "status"}


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler. Once the free trial is over, everything but /start and /status is
    refused for anyone who isn't an admin or manually granted access."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or not (msg.text or "").startswith("/"):
        return
    cmd = msg.text.split()[0][1:].split("@")[0].lower()
    if cmd in ALWAYS_ALLOWED or access.is_admin(user.id):
        return
    state = await access.refresh(context.application.bot_data["fpl"])
    if access.is_locked(user.id, state=state):
        await msg.reply_text(access.denied_message(state))
        raise ApplicationHandlerStop


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    state = await access.refresh(context.application.bot_data["fpl"])
    lines = [access.trial_line(state)]
    if access.is_admin(uid):
        lines.append("You're an admin: full access, always.")
    else:
        user = db.get_user(uid)
        if user and user["subscribed"] and db.is_subscribed(uid):
            lines.append("You have full access" + (f" until {user['subscription_expires_at'][:10]}." if user["subscription_expires_at"] else "."))
        elif access.is_locked(uid, state=state):
            lines.append("The bot is paused for you until paid plans launch.")
    await update.message.reply_text("\n".join(lines))


def _admin_target(update: Update, args: list[str]):
    if not access.is_admin(update.effective_user.id):
        return None
    return int(args[0]) if args and args[0].isdigit() else None


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /grant <telegram_id> [days=30] — give someone full access by hand."""
    if not access.is_admin(update.effective_user.id):
        return  # silently ignore: don't reveal admin commands
    target = _admin_target(update, context.args)
    if target is None:
        await update.message.reply_text("Usage: /grant <telegram_id> [days=30]")
        return
    days = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    expires = db.grant_subscription(target, days)
    await update.message.reply_text(f"✅ {target} has full access until {expires[:10]}.")


async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not access.is_admin(update.effective_user.id):
        return
    target = _admin_target(update, context.args)
    if target is None:
        await update.message.reply_text("Usage: /revoke <telegram_id>")
        return
    db.revoke_subscription(target)
    await update.message.reply_text(f"Access revoked for {target}.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again.")


COMMAND_MENU = [
    BotCommand("start", "How this bot works"),
    BotCommand("status", "Your free-trial / access status"),
    BotCommand("setteam", "Set your FPL team ID"),
    BotCommand("myteam", "Your squad, points and rank"),
    BotCommand("showleagues", "List your leagues and their IDs"),
    BotCommand("trackleague", "Track a league by ID"),
    BotCommand("myleagues", "Leagues you're tracking"),
    BotCommand("rival", "Compare squads with one rival"),
    BotCommand("leaguescan", "Rank gaps, premiums, rival chips (paid)"),
    BotCommand("differentials", "Your differentials (paid)"),
    BotCommand("livewinprob", "Live projected score vs rivals (paid)"),
    BotCommand("roundprize", "Live gameweek leaderboard (paid)"),
    BotCommand("predictchip", "Who may chip soon (paid)"),
    BotCommand("alert", "Rival transfer / rank alerts (paid)"),
    BotCommand("wildcard", "Plan your wildcard: buy, sell, keep (paid)"),
    BotCommand("transfers", "Best 1-3 transfers + hit check (paid)"),
]


async def post_init(app: Application) -> None:
    app.bot_data["fpl"] = FPLClient()
    try:
        await app.bot.set_my_commands(COMMAND_MENU)
    except Exception:  # cosmetic only
        log.warning("Could not set the command menu", exc_info=True)


async def post_shutdown(app: Application) -> None:
    fpl = app.bot_data.get("fpl")
    if fpl:
        await fpl.aclose()


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)  # trial gate runs before everything else
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("setteam", setteam))
    app.add_handler(CommandHandler("myteam", myteam))
    app.add_handler(CommandHandler("showleagues", showleagues))
    app.add_handler(CommandHandler("trackleague", trackleague))
    app.add_handler(CommandHandler("addleague", addleague))  # legacy alias
    app.add_handler(CommandHandler("myleagues", myleagues))
    app.add_handler(CommandHandler("removeleague", removeleague))
    app.add_handler(CommandHandler("rival", rival))
    app.add_handler(CommandHandler("leaguescan", leaguescan))
    app.add_handler(CommandHandler("differentials", differentials))
    app.add_handler(CommandHandler("livewinprob", livewinprob))
    app.add_handler(CommandHandler("roundprize", roundprize))
    app.add_handler(CommandHandler("predictchip", predictchip))
    app.add_handler(CommandHandler("alert", alert))
    app.add_handler(CommandHandler("wildcard", wildcard))
    app.add_handler(CommandHandler("transfers", transfers))
    app.add_error_handler(error_handler)
    if app.job_queue:
        app.job_queue.run_repeating(alerts.alert_tick, interval=config.ALERT_INTERVAL_MINUTES * 60, first=60)
        app.job_queue.run_repeating(alerts.maintenance, interval=6 * 3600, first=120)
    else:
        log.warning("No job queue: install python-telegram-bot[job-queue] for alerts")
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
