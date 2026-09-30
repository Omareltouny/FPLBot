"""Phase 3: live points, projected scores, /livewinprob and /roundprize.

Numbers come from the public FPL live feed (event/{gw}/live/) plus fixtures. Two honest caveats,
repeated in the bot's output: bonus points can be provisional until a match is final, and the
"expected remaining" part uses FPL's own expected-points figures, so projections are estimates.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from league import Scope, Snapshot, _name

CAP_FALLBACK_MULT = 2


@dataclass
class LiveFeed:
    points: dict[int, int]  # element -> live total points this GW
    minutes: dict[int, int]  # element -> minutes played so far
    fixtures: dict[int, list[dict]]  # team -> [{"state": pending|live|done, "minutes": int}]
    started: bool  # at least one fixture has kicked off
    finished: bool  # every fixture is finished
    n_fixtures: int
    n_done: int


@dataclass
class LiveResult:
    gw_points: int  # live gameweek score, net of transfer hits, after estimated auto-subs
    remaining: float  # expected further points from players still to play
    left: int  # players in the effective XI with a fixture still to play / in progress
    hit: int = 0
    no_picks: bool = False  # manager has no squad for this GW yet


def build_feed(live_json: dict, fixtures_json: list) -> LiveFeed:
    points, minutes = {}, {}
    for e in live_json.get("elements", []):
        st = e.get("stats", {})
        points[e["id"]] = st.get("total_points", 0)
        minutes[e["id"]] = st.get("minutes", 0)
    fx: dict[int, list[dict]] = {}
    n_done = 0
    for f in fixtures_json:
        done = bool(f.get("finished") or f.get("finished_provisional"))
        state = "done" if done else "live" if f.get("started") else "pending"
        n_done += done
        for team in (f.get("team_h"), f.get("team_a")):
            if team is not None:
                fx.setdefault(team, []).append({"state": state, "minutes": f.get("minutes") or 0})
    n = len(fixtures_json)
    started = any(x["state"] != "pending" for lst in fx.values() for x in lst)
    return LiveFeed(points, minutes, fx, started, n > 0 and n_done == n, n, n_done)


def _team_done(feed: LiveFeed, team: int) -> bool:
    return all(f["state"] == "done" for f in feed.fixtures.get(team, []))  # blank GW => True


def _remaining_ep(feed: LiveFeed, el: dict) -> float:
    fx = feed.fixtures.get(el["team"], [])
    if not fx:
        return 0.0
    try:
        ep = float(el.get("ep_this") or 0)
    except ValueError:
        ep = 0.0
    per = ep / len(fx)
    total = 0.0
    for f in fx:
        if f["state"] == "pending":
            total += per
        elif f["state"] == "live":
            total += per * max(0, 90 - f["minutes"]) / 90
    return total


def _valid_formation(xi: list[dict], typ) -> bool:
    c = {1: 0, 2: 0, 3: 0, 4: 0}
    for p in xi:
        c[typ(p)] += 1
    return c[1] == 1 and c[2] >= 3 and c[3] >= 2 and c[4] >= 1


def live_result(snap: Snapshot, feed: LiveFeed, elements: dict[int, dict], gw: int) -> LiveResult:
    eh = snap.entry_history or {}
    if eh.get("event") not in (None, gw):  # picks fell back to an earlier GW: no squad for this one yet
        return LiveResult(0, 0.0, 0, 0, no_picks=True)
    hit = eh.get("event_transfers_cost", 0) or 0

    picks = sorted(snap.picks, key=lambda p: p["position"])
    typ = lambda p: elements[p["element"]]["element_type"]
    mins = lambda p: feed.minutes.get(p["element"], 0)
    blank = lambda p: mins(p) == 0 and _team_done(feed, elements[p["element"]]["team"])  # confirmed out

    if snap.active_chip == "bboost":
        xi = list(picks)  # all 15 count, no auto-subs
    else:
        xi, bench = list(picks[:11]), picks[11:]
        gk_out = next((p for p in xi if typ(p) == 1 and blank(p)), None)
        gk_sub = next((p for p in bench if typ(p) == 1), None)
        if gk_out and gk_sub and mins(gk_sub) > 0:
            xi[xi.index(gk_out)] = gk_sub
        for b in bench:  # bench priority order
            if typ(b) == 1 or mins(b) == 0:
                continue
            for i, st in enumerate(xi):
                if typ(st) != 1 and blank(st):
                    trial = xi[:i] + [b] + xi[i + 1:]
                    if _valid_formation(trial, typ):
                        xi = trial
                        break

    cap = next((p for p in picks if p.get("is_captain")), None)
    vice = next((p for p in picks if p.get("is_vice_captain")), None)
    cap_mult = cap["multiplier"] if cap and cap.get("multiplier", 0) >= 2 else CAP_FALLBACK_MULT
    cap_el = cap["element"] if cap else None
    if cap and blank(cap) and vice and not blank(vice):
        cap_el = vice["element"]

    pts, remaining, left = 0, 0.0, 0
    for p in xi:
        m = cap_mult if p["element"] == cap_el else 1
        pts += feed.points.get(p["element"], 0) * m
        el = elements[p["element"]]
        if not _team_done(feed, el["team"]):
            left += 1
            remaining += _remaining_ep(feed, el) * m
    return LiveResult(pts - hit, remaining, left, hit)


def league_prev_total(history_current: list[dict], gw: int, start_event: int = 1) -> int | None:
    """League points banked before this GW (leagues can start mid-season, so subtract that offset)."""
    if not history_current:
        return None
    tot = {h["event"]: h["total_points"] for h in history_current}
    end = tot.get(gw - 1, 0)
    begin = tot.get(start_event - 1, 0) if start_event > 1 else 0
    return end - begin


# ---------------------------------------------------------------- reports
_CAVEAT = ("<i>Estimate: bonus can be provisional and 'Proj' adds FPL's expected points for players still "
           "to play. Auto-subs are applied once a player's match is over.</i>")


def _status(feed: LiveFeed, gw: int) -> str:
    if feed.finished:
        return f"GW{gw} is finished — these are the final scores."
    return f"GW{gw} live · {feed.n_done}/{feed.n_fixtures} matches finished."


def build_livewinprob(scope: Scope, snaps: dict[int, Snapshot], failed: int, feed: LiveFeed,
                      elements: dict[int, dict], gw: int) -> list[str]:
    from league import sample_note

    head = f"<b>Live win check · {html.escape(scope.league_name)}</b>\n{_status(feed, gw)}\n{sample_note(scope, snaps, failed)}"
    if not feed.started:
        return [f"GW{gw} hasn't kicked off yet, so there's nothing live to track. Try again once the first match starts."]
    if not scope.me or scope.me.entry not in snaps:
        return [head, "I couldn't load your squad or find you in the standings, so I can't compare you with rivals."]

    rows = []
    for m in scope.members:
        s = snaps.get(m.entry)
        if not s:
            continue
        r = live_result(s, feed, elements, gw)
        prev = league_prev_total(s.history_current or [], gw, scope.start_event)
        if prev is None:  # no history cached: fall back to the standings total
            prev = m.total - m.event_total
        live_total = prev + r.gw_points
        rows.append((m, r, live_total, live_total + round(r.remaining)))
    me_row = next(x for x in rows if x[0].entry == scope.me.entry)
    rivals = [x for x in rows if x[0].entry != scope.me.entry]
    m_live, m_proj = me_row[2], me_row[3]
    beat_live = sum(1 for x in rivals if x[2] < m_live)
    beat_proj = sum(1 for x in rivals if x[3] < m_proj)
    summary = (f"<b>Right now you're ahead of {beat_live} of {len(rivals)} rivals</b>"
               f" · projected to finish ahead of {beat_proj}.")

    lines = ["Manager          GW  Left  Live  Proj  Gap"]
    for m, r, live_total, proj in sorted(rows, key=lambda x: -x[3]):
        you = m.entry == scope.me.entry
        gap = " you" if you else f"{proj - m_proj:+d}"
        lines.append(f"{html.escape(_name(m, 15)):<15} {r.gw_points:>3}  {r.left:>4}  {live_total:>4}  {proj:>4} {gap:>4}")
    return [head, summary,
            "<pre>" + "\n".join(lines) + "</pre>\nGap = their projected total minus yours (+ = they lead).",
            _CAVEAT]


def build_roundprize(scope: Scope, snaps: dict[int, Snapshot], failed: int, feed: LiveFeed,
                     elements: dict[int, dict], gw: int, me_id: int | None, n: int, show: int = 15) -> list[str]:
    head = (f"<b>Round prize · {html.escape(scope.league_name)}</b>\n{_status(feed, gw)}\n"
            f"Looking at the top {n} managers by league rank ({len(snaps)} squads loaded). "
            "⚠️ In a huge league the round winner can sit outside this group — this shows who leads <i>among these managers</i>.")
    if failed:
        head += f"\n⚠️ {failed} manager(s) couldn't be loaded."
    if not feed.started:
        return [f"GW{gw} hasn't kicked off yet, so there's no live round to track."]
    scored = []
    for m in scope.members:
        s = snaps.get(m.entry)
        if s:
            scored.append((m, live_result(s, feed, elements, gw)))
    scored.sort(key=lambda x: (-x[1].gw_points, x[0].rank))
    lines = ["Pos  Manager          GW  Left  Overall"]
    for i, (m, r) in enumerate(scored, 1):
        if i <= show or m.entry == me_id:
            tag = " ◄ you" if m.entry == me_id else ""
            lines.append(f"{i:>3}  {html.escape(_name(m, 15)):<15} {r.gw_points:>3}  {r.left:>4}  #{m.rank:,}{tag}")
        elif i == show + 1:
            lines.append("...")
    return [head, "<pre>" + "\n".join(lines) + "</pre>", _CAVEAT]
