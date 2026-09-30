"""Phase 2: league scanning logic, built for very large leagues (20k-50k managers).

We never walk a whole league. A 50k league is 1,000 standings pages; instead we fetch:
  * page 1 (the top 50; we use the top N), and
  * the single page that contains *you* (found via your rank on entry/{id}/),
    plus a neighbouring page only if your window crosses a page boundary.
Squads/chips are then fetched only for that small sample (~30 managers) and cached in SQLite.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import db
from fpl_client import FPLClient, FPLError, FPLNotFound

log = logging.getLogger(__name__)

PAGE_SIZE = 50
CHIP_ORDER = ["wildcard", "freehit", "bboost", "3xc"]
CHIP_LABELS = {"wildcard": "WC", "freehit": "FH", "bboost": "BB", "3xc": "TC"}
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


# ----------------------------------------------------------------- data types
@dataclass
class Member:
    entry: int
    entry_name: str
    player_name: str
    rank: int
    total: int
    event_total: int


@dataclass
class Scope:
    league_id: int
    league_name: str
    me: Member | None
    members: list[Member]  # top N + neighbours around me, sorted by rank (me included)
    start_event: int = 1  # first gameweek this league counts points from


@dataclass
class Snapshot:
    picks: list[dict]
    chips: list[dict]  # chips used so far (from entry/{id}/history/)
    active_chip: str | None = None
    entry_history: dict | None = None  # picks response "entry_history" (transfer cost etc.)
    history_current: list | None = None  # per-GW totals from history/ (used for live totals)


def _member(r: dict) -> Member:
    return Member(
        entry=r["entry"],
        entry_name=r.get("entry_name", ""),
        player_name=r.get("player_name", ""),
        rank=r.get("rank") or r.get("rank_sort") or 0,
        total=r.get("total", 0),
        event_total=r.get("event_total", 0),
    )


# ------------------------------------------------------------- finding scope
async def _my_league_rank(fpl: FPLClient, league_id: int, team_id: int) -> int | None:
    entry = await fpl.entry(team_id)
    for lg in (entry.get("leagues") or {}).get("classic", []):
        if lg.get("id") == league_id:
            return lg.get("entry_rank") or None
    return None


async def _locate_me(fpl: FPLClient, league_id: int, team_id: int, page1: dict, neighbors: int):
    """Return (me_row, all_rows_from_fetched_pages_sorted) using as few page fetches as possible."""
    pages: dict[int, dict] = {1: page1}

    def find():
        for p, d in pages.items():
            for r in d["standings"]["results"]:
                if r["entry"] == team_id:
                    return p, r
        return None

    hit = find()
    if hit is None:
        rank = await _my_league_rank(fpl, league_id, team_id)
        if rank is None:
            return None, []
        guess = (rank - 1) // PAGE_SIZE + 1
        for p in (guess, guess - 1, guess + 1):  # rank on entry/ can lag a little
            if p < 1 or p in pages:
                continue
            try:
                pages[p] = await fpl.standings(league_id, p)
            except FPLError:
                continue
            hit = find()
            if hit:
                break
    if hit is None:
        return None, []

    my_page, me_row = hit
    results = pages[my_page]["standings"]["results"]
    idx = next(i for i, r in enumerate(results) if r["entry"] == team_id)
    # Window crosses a page boundary -> grab that one neighbouring page too.
    if idx < neighbors and my_page > 1 and (my_page - 1) not in pages:
        try:
            pages[my_page - 1] = await fpl.standings(league_id, my_page - 1)
        except FPLError:
            pass
    if idx >= len(results) - neighbors and pages[my_page]["standings"].get("has_next") \
            and (my_page + 1) not in pages:
        try:
            pages[my_page + 1] = await fpl.standings(league_id, my_page + 1)
        except FPLError:
            pass

    rows = sorted((r for d in pages.values() for r in d["standings"]["results"]),
                  key=lambda r: r.get("rank") or r.get("rank_sort") or 0)
    return me_row, rows


async def find_scope(fpl: FPLClient, league_id: int, my_team_id: int,
                     top_n: int | None = None, neighbors: int | None = None) -> Scope:
    top_n = max(1, min(top_n or config.LEAGUE_TOP_N, PAGE_SIZE))
    neighbors = config.LEAGUE_NEIGHBORS if neighbors is None else neighbors

    first = await fpl.standings(league_id, 1)  # raises FPLNotFound for H2H / bad id
    name = first["league"]["name"]
    page1 = first["standings"]["results"]
    chosen: dict[int, Member] = {r["entry"]: _member(r) for r in page1[:top_n]}

    me: Member | None = None
    me_row, rows = await _locate_me(fpl, league_id, my_team_id, first, neighbors)
    if me_row is not None:
        i = next(i for i, r in enumerate(rows) if r["entry"] == my_team_id)
        for r in rows[max(0, i - neighbors): i + neighbors + 1]:
            chosen[r["entry"]] = _member(r)
        me = chosen[my_team_id]

    members = sorted(chosen.values(), key=lambda m: m.rank)
    return Scope(league_id, name, me, members, first["league"].get("start_event") or 1)


# ----------------------------------------------------------- squads & chips
def _fresh(fetched_at: str) -> bool:
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return age.total_seconds() < config.SNAPSHOT_TTL_SECONDS


async def _picks_with_fallback(fpl: FPLClient, team_id: int, gw: int) -> dict:
    try:
        return await fpl.picks(team_id, gw)
    except FPLNotFound:
        if gw > 1:
            return await fpl.picks(team_id, gw - 1)
        raise


async def load_snapshot(fpl: FPLClient, league_id: int, team_id: int, gw: int,
                        db_path: str | None = None, need_history: bool = True) -> Snapshot:
    """Squad (+ chip/points history) for one manager, cached in SQLite.

    need_history=False fetches picks only (half the API calls, used by /roundprize); such rows are
    stored with chips_used_json = 'null' and upgraded on demand if a caller later needs history.
    """
    row = db.get_snapshot(league_id, team_id, gw, db_path)
    hist = None
    if row and _fresh(row["fetched_at"]):
        picks_data = json.loads(row["squad_json"])
        chips = json.loads(row["chips_used_json"])
        if chips is not None or not need_history:
            return _to_snapshot(picks_data, chips)
        hist = await fpl.history(team_id)  # upgrade a picks-only row
    elif need_history:
        picks_data, hist = await asyncio.gather(_picks_with_fallback(fpl, team_id, gw), fpl.history(team_id))
    else:
        picks_data = await _picks_with_fallback(fpl, team_id, gw)
    chips = None
    if hist is not None:
        chips = hist.get("chips", [])
        picks_data = {**picks_data, "history_current": hist.get("current", [])}
    db.save_snapshot(league_id, team_id, gw, json.dumps(picks_data), json.dumps(chips), db_path)
    return _to_snapshot(picks_data, chips)


def _to_snapshot(picks_data: dict, chips: list | None) -> Snapshot:
    return Snapshot(picks_data["picks"], chips or [], picks_data.get("active_chip"),
                    picks_data.get("entry_history") or {}, picks_data.get("history_current") or [])


async def gather_snapshots(fpl: FPLClient, scope: Scope, gw: int,
                           db_path: str | None = None,
                           need_history: bool = True) -> tuple[dict[int, Snapshot], int]:
    """Fetch squads+chips for the sampled members (concurrency is capped inside FPLClient)."""
    ids = [m.entry for m in scope.members]
    results = await asyncio.gather(
        *(load_snapshot(fpl, scope.league_id, i, gw, db_path, need_history) for i in ids), return_exceptions=True
    )
    snaps, failed = {}, 0
    for i, res in zip(ids, results):
        if isinstance(res, Snapshot):
            snaps[i] = res
        else:
            failed += 1
            log.warning("Snapshot failed for %s: %s", i, res)
    return snaps, failed


# ------------------------------------------------------------------ analysis
def ownership(snaps: dict[int, Snapshot], exclude: int | None) -> tuple[Counter, int]:
    """(element -> number of sampled rivals owning him, number of rivals sampled)."""
    c: Counter = Counter()
    n = 0
    for tid, s in snaps.items():
        if tid == exclude:
            continue
        n += 1
        for p in s.picks:
            c[p["element"]] += 1
    return c, n


def premium_ids(players: dict[int, dict], n: int | None = None) -> list[int]:
    n = n or config.PREMIUM_COUNT
    ranked = sorted(players.values(), key=lambda p: (-p["now_cost"], p["web_name"]))
    return [p["id"] for p in ranked[:n]]


def chips_available(boot: dict, used: list[dict], gw: int) -> list[str]:
    """Chips a manager can still play. Uses bootstrap `chips` windows when present so it adapts
    to rule changes (e.g. two of each chip split across season halves); otherwise assumes one each."""
    used = [u for u in used if u.get("name") in CHIP_LABELS]
    defs = [d for d in (boot.get("chips") or []) if d.get("name") in CHIP_LABELS]
    avail: list[str] = []
    if defs:
        for name in CHIP_ORDER:
            for d in (x for x in defs if x["name"] == name):
                start, stop = d.get("start_event") or 1, d.get("stop_event") or 99
                if not (start <= gw <= stop):
                    continue
                taken = sum(1 for u in used if u["name"] == name and start <= u.get("event", 0) <= stop)
                if taken < (d.get("number") or 1) and name not in avail:
                    avail.append(name)
    else:
        avail = [n for n in CHIP_ORDER if not any(u["name"] == n for u in used)]
    return avail


# ---------------------------------------------------------------- formatting
def _name(m: Member, width: int = 16) -> str:
    n = m.player_name or m.entry_name
    return n if len(n) <= width else n[: width - 1] + "…"


def _pname(players: dict, el: int) -> str:
    return html.escape(players.get(el, {}).get("web_name", "?"))


def _price(players: dict, el: int) -> str:
    return f"£{players.get(el, {}).get('now_cost', 0) / 10:.1f}m"


def _pct(k: int, n: int) -> str:
    return f"{round(100 * k / n)}%" if n else "–"


def sample_note(scope: Scope, snaps: dict, failed: int) -> str:
    top = min(config.LEAGUE_TOP_N, len(scope.members))
    txt = (f"Sample: top {top} plus {config.LEAGUE_NEIGHBORS} above/below you "
           f"({len(snaps)} squads loaded). Big leagues aren't scanned in full.")
    if scope.me is None:
        txt += "\n⚠️ I couldn't find you in this league's standings, so this is the top only."
    if failed:
        txt += f"\n⚠️ {failed} manager(s) couldn't be loaded (FPL API hiccup)."
    return txt


def build_leaguescan(scope: Scope, snaps: dict[int, Snapshot], failed: int, boot: dict, gw: int) -> list[str]:
    players = {e["id"]: e for e in boot["elements"]}
    me = scope.me
    my_total = me.total if me else None
    out = [f"<b>{html.escape(scope.league_name)}</b> · GW{gw}\n{sample_note(scope, snaps, failed)}"]
    if me:
        out[0] += f"\n\n<b>You:</b> #{me.rank:,} · {me.total} pts"

    # 1. rank & gap
    rows = ["      #  Manager           Pts   Gap"]
    for m in scope.members:
        gap = "  you" if m.entry == (me.entry if me else -1) else (f"{m.total - my_total:+d}" if my_total is not None else "")
        rows.append(f"{m.rank:>7,}  {html.escape(_name(m)):<16} {m.total:>4}  {gap:>5}")
    out.append("<b>Rank &amp; gap</b> (+ = they lead you)\n<pre>" + "\n".join(rows) + "</pre>")

    # 2. premiums you don't own
    if me and me.entry in snaps:
        mine = {p["element"] for p in snaps[me.entry].picks}
        own, n = ownership(snaps, me.entry)
        missing = [(own[e], e) for e in premium_ids(players) if e not in mine and own[e] > 0]
        missing.sort(key=lambda t: (-t[0], players[t[1]]["web_name"]))
        lines = []
        by_rank = {m.entry: m for m in scope.members}
        for k, e in missing[:8]:
            owners = [by_rank[t].player_name or by_rank[t].entry_name for t, s in snaps.items()
                      if t != me.entry and any(p["element"] == e for p in s.picks)]
            lines.append(f"• {_pname(players, e)} ({_price(players, e)}) — {k}/{n} own "
                         f"(e.g. {html.escape(', '.join(owners[:3]))})")
        out.append("<b>Premium players you don't own</b>\n" + ("\n".join(lines) if lines else "None — you own every premium your rivals do."))
    elif me:
        out.append("<i>Couldn't load your squad, so premium comparison is skipped.</i>")

    # 3. chips
    avail = {t: chips_available(boot, s.chips, gw) for t, s in snaps.items() if not me or t != me.entry}
    n = len(avail)
    if n:
        summary = " · ".join(f"{CHIP_LABELS[c]} {sum(c in a for a in avail.values())}/{n}" for c in CHIP_ORDER)
        by_entry = {m.entry: m for m in scope.members}
        lines = []
        for m in scope.members[:10]:
            if m.entry in avail and (not me or m.entry != me.entry):
                have = " ".join(CHIP_LABELS[c] for c in avail[m.entry]) or "none"
                lines.append(f"#{m.rank:,} {html.escape(_name(m))}: {have}")
        out.append("<b>Chips still available</b> (WC wildcard, FH free hit, BB bench boost, TC triple captain)\n"
                   f"Rivals with each chip: {summary}\n" + "\n".join(lines))
    return out


def build_differentials(scope: Scope, snaps: dict[int, Snapshot], failed: int, boot: dict, gw: int) -> list[str]:
    players = {e["id"]: e for e in boot["elements"]}
    me = scope.me
    if not me or me.entry not in snaps:
        return ["I couldn't load your squad/standings position, so I can't compute differentials."]
    mine = {p["element"] for p in snaps[me.entry].picks}
    own, n = ownership(snaps, me.entry)
    if n == 0:
        return ["No rival squads could be loaded right now. Try again in a few minutes."]

    diffs = sorted(((own[e] / n, e) for e in mine if own[e] / n <= config.DIFF_MAX_OWNERSHIP))
    template = sorted(((-own[e] / n, e) for e in own if e not in mine and own[e] / n >= config.TEMPLATE_MIN_OWNERSHIP))

    out = [f"<b>Differentials · {html.escape(scope.league_name)} · GW{gw}</b>\n{sample_note(scope, snaps, failed)}"]
    out.append("<b>Your differentials</b> (you own, few rivals do)\n" + (
        "\n".join(f"• {_pname(players, e)} ({_price(players, e)}) — {_pct(own[e], n)} of rivals" for _, e in diffs[:10])
        or "None — your squad is close to the pack."))
    out.append("<b>Template players you're missing</b> (rivals own, you don't)\n" + (
        "\n".join(f"• {_pname(players, e)} ({_price(players, e)}) — {_pct(own[e], n)} of rivals" for _, e in template[:10])
        or "None — you own everyone most rivals own."))
    return out


def compare_squads(my_picks: list[dict], their_picks: list[dict], players: dict, teams: dict,
                   their_label: str) -> list[str]:
    mine = {p["element"] for p in my_picks}
    theirs = {p["element"] for p in their_picks}

    def fmt(ids):
        rows = []
        for pos in (1, 2, 3, 4):
            names = [f"{_pname(players, e)} ({teams.get(players[e]['team'], {}).get('short_name', '?')})"
                     for e in sorted(ids, key=lambda x: -players[x]["now_cost"])
                     if players.get(e, {}).get("element_type") == pos]
            if names:
                rows.append(f"<b>{POSITIONS[pos]}</b> " + ", ".join(names))
        return "\n".join(rows) or "—"

    def cap(picks):
        c = next((p["element"] for p in picks if p.get("is_captain")), None)
        return _pname(players, c) if c else "?"

    lab = html.escape(their_label)
    return [
        f"<b>You vs {lab}</b>\nShared players: {len(mine & theirs)}/15 · "
        f"Captains: you {cap(my_picks)}, them {cap(their_picks)}",
        f"<b>Only you own</b>\n{fmt(mine - theirs)}",
        f"<b>Only {lab} owns</b>\n{fmt(theirs - mine)}",
    ]


async def top_members(fpl: FPLClient, league_id: int, n: int) -> Scope:
    """Top-N managers by league rank only (n > 50 fetches extra standings pages, 50 per page)."""
    n = max(1, n)
    first = await fpl.standings(league_id, 1)
    rows = list(first["standings"]["results"])
    page, has_next = 1, first["standings"].get("has_next")
    while len(rows) < n and has_next:
        page += 1
        data = await fpl.standings(league_id, page)
        rows += data["standings"]["results"]
        has_next = data["standings"].get("has_next")
    members = [_member(r) for r in rows[:n]]
    return Scope(league_id, first["league"]["name"], None, members, first["league"].get("start_event") or 1)
