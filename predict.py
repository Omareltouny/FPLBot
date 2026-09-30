"""Phase 3: chip prediction from fixture runs and chip-window expiry.

Transparent heuristics (they only use squads as they are *today*, and managers change squads):
  * Bench Boost  - a coming GW where >= BB_DOUBLE_MIN of their 15 players have a double fixture
  * Triple Captain - their current captain's team has a double fixture in a coming GW
  * Free Hit     - a coming GW where >= FH_BLANK_MIN of their starting XI have no fixture
  * Expiring     - any unused chip whose window closes within CHIP_EXPIRY_WARN_GWS gameweeks
"""
from __future__ import annotations

import html
from collections import defaultdict

import config
from league import CHIP_LABELS, CHIP_ORDER, Member, Scope, Snapshot, _name, chips_available, sample_note

CHIP_NAMES = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost", "3xc": "Triple Captain"}


def fixture_counts(fixtures: list[dict]) -> dict[int, dict[int, int]]:
    """event -> team -> number of fixtures that GW (postponed/unscheduled fixtures have no event)."""
    out: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for f in fixtures:
        ev = f.get("event")
        if ev is None:
            continue
        for team in (f.get("team_h"), f.get("team_a")):
            if team is not None:
                out[ev][team] += 1
    return out


def expiring_chips(boot: dict, used: list[dict], next_gw: int) -> list[tuple[str, int]]:
    """(chip, last GW it can be played) for unused chips whose window closes soon."""
    res = []
    for d in boot.get("chips") or []:
        name = d.get("name")
        if name not in CHIP_LABELS:
            continue
        start, stop = d.get("start_event") or 1, d.get("stop_event") or 99
        if not (start <= next_gw <= stop) or stop - next_gw > config.CHIP_EXPIRY_WARN_GWS or stop >= 38:
            continue
        taken = sum(1 for u in used if u.get("name") == name and start <= u.get("event", 0) <= stop)
        if taken < (d.get("number") or 1):
            res.append((name, stop))
    return res


def flags_for(snap: Snapshot, boot: dict, fc: dict, elements: dict, gw: int, horizon: int) -> list[dict]:
    """Earliest signal per chip for one manager."""
    picks = sorted(snap.picks, key=lambda p: p["position"])
    cap = next((p for p in picks if p.get("is_captain")), None)
    found: dict[str, dict] = {}
    for g in range(gw + 1, min(gw + horizon, 38) + 1):
        teams = fc.get(g)
        if not teams:
            continue
        avail = chips_available(boot, snap.chips, g)
        team_of = lambda p: elements[p["element"]]["team"]
        if "bboost" in avail and "bboost" not in found:
            doubles = sum(1 for p in picks if teams.get(team_of(p), 0) >= 2)
            if doubles >= config.BB_DOUBLE_MIN:
                found["bboost"] = {"chip": "bboost", "gw": g, "why": f"{doubles} squad players have a double fixture"}
        if "3xc" in avail and "3xc" not in found and cap and teams.get(team_of(cap), 0) >= 2:
            found["3xc"] = {"chip": "3xc", "gw": g,
                            "why": f"captain {elements[cap['element']]['web_name']} has a double fixture"}
        if "freehit" in avail and "freehit" not in found:
            blanks = sum(1 for p in picks[:11] if teams.get(team_of(p), 0) == 0)
            if blanks >= config.FH_BLANK_MIN:
                found["freehit"] = {"chip": "freehit", "gw": g, "why": f"{blanks} of their XI have no fixture"}
    return sorted(found.values(), key=lambda f: f["gw"])


def build_predictchip(scope: Scope, snaps: dict[int, Snapshot], failed: int, boot: dict,
                      fixtures: list[dict], gw: int) -> list[str]:
    horizon = config.CHIP_HORIZON_GWS
    elements = {e["id"]: e for e in boot["elements"]}
    fc = fixture_counts(fixtures)
    me_id = scope.me.entry if scope.me else None
    members = {m.entry: m for m in scope.members}

    out = [f"<b>Chip watch · {html.escape(scope.league_name)}</b>\nLooking at GW{gw + 1}–GW{min(gw + horizon, 38)}.\n"
           f"{sample_note(scope, snaps, failed)}\n"
           "<i>Heuristic: based on squads as they are today; managers can and do change them.</i>"]

    # calendar of unusual gameweeks
    cal = []
    for g in range(gw + 1, min(gw + horizon, 38) + 1):
        teams = fc.get(g)
        if not teams:
            continue
        doubles = sum(1 for n in teams.values() if n >= 2)
        all_teams = {t["id"] for t in boot["teams"]}
        blanks = len(all_teams - set(teams))
        if doubles or blanks:
            cal.append(f"GW{g}: {doubles} team(s) with a double, {blanks} with no fixture")
    out.append("<b>Fixture calendar</b>\n" + ("\n".join(cal) if cal else "No double or blank gameweeks in this window."))

    rival_flags: list[tuple[Member, dict]] = []
    my_flags: list[dict] = []
    expiring: list[tuple[Member, str, int]] = []
    for tid, snap in snaps.items():
        m = members.get(tid)
        if not m:
            continue
        fl = flags_for(snap, boot, fc, elements, gw, horizon)
        if tid == me_id:
            my_flags = fl
            continue
        rival_flags += [(m, f) for f in fl]
        expiring += [(m, name, stop) for name, stop in expiring_chips(boot, snap.chips, gw + 1)]

    rival_flags.sort(key=lambda t: (t[1]["gw"], t[0].rank))
    lines = [f"#{m.rank:,} {html.escape(_name(m))} — {CHIP_NAMES[f['chip']]} in GW{f['gw']} ({f['why']})"
             for m, f in rival_flags[:20]]
    if len(rival_flags) > 20:
        lines.append(f"+{len(rival_flags) - 20} more")
    out.append("<b>Rivals likely to use a chip</b>\n" + ("\n".join(lines) if lines else "No strong signals right now."))

    if expiring:
        by_chip: dict[tuple[str, int], list[Member]] = defaultdict(list)
        for m, name, stop in expiring:
            by_chip[(name, stop)].append(m)
        exp_lines = []
        for (name, stop), ms in sorted(by_chip.items(), key=lambda kv: (kv[0][1], CHIP_ORDER.index(kv[0][0]))):
            who = ", ".join(html.escape(_name(m, 14)) for m in sorted(ms, key=lambda m: m.rank)[:5])
            more = f" +{len(ms) - 5}" if len(ms) > 5 else ""
            exp_lines.append(f"{CHIP_NAMES[name]} (window closes GW{stop}): {len(ms)} rivals — {who}{more}")
        out.append("<b>Chips about to expire</b> (use it or lose it)\n" + "\n".join(exp_lines))

    if me_id in snaps:
        mine = [f"{CHIP_NAMES[f['chip']]} in GW{f['gw']} ({f['why']})" for f in my_flags]
        my_exp = [f"{CHIP_NAMES[n]} closes GW{s}" for n, s in expiring_chips(boot, snaps[me_id].chips, gw + 1)]
        out.append("<b>You</b>\n" + ("\n".join(mine + my_exp) or "No chip opportunities flagged for your squad."))
    return out
