"""Phase 4: transfer & wildcard advisor — scoring model.

Score of a player = expected points over the next few gameweeks:

    base      = W_FORM*form + W_PPG*points_per_game + W_VALUE*ppg*(6/price)    (points per GW)
    per GW    = base * fixture multiplier (from FDR; doubles add, blanks give 0)
                     * availability (injury/doubt)  * minutes security (rotation)
    score     = sum over the horizon, later GWs discounted slightly
    adjusted  = score * (1 + diff_strength * (1 - share of your rivals who own him))

The last line is the differential bonus: players few of your rivals own get a lift, players
they all own get none. The optimiser lives in optimize.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import config

POS_NAMES = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_SHAPE = {1: 2, 2: 5, 3: 5, 4: 3}
FORMATIONS = [(d, m, f) for d in (3, 4, 5) for m in (2, 3, 4, 5) for f in (1, 2, 3) if d + m + f == 10]


def next_gameweek(boot: dict) -> int:
    events = boot["events"]
    for e in events:
        if e.get("is_next"):
            return e["id"]
    cur = next((e["id"] for e in events if e.get("is_current")), None)
    return min((cur or 0) + 1, 38)


def finished_gameweeks(boot: dict) -> int:
    return sum(1 for e in boot["events"] if e.get("finished"))


def fixture_index(fixtures: list[dict]) -> dict[int, dict[int, list[tuple[int, bool, int]]]]:
    """team -> gameweek -> [(opponent, is_home, difficulty)]. Unscheduled fixtures are ignored."""
    idx: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for f in fixtures:
        ev = f.get("event")
        if ev is None:
            continue
        idx[f["team_h"]][ev].append((f["team_a"], True, f.get("team_h_difficulty") or 3))
        idx[f["team_a"]][ev].append((f["team_h"], False, f.get("team_a_difficulty") or 3))
    return idx


def availability(el: dict) -> float:
    chance = el.get("chance_of_playing_next_round")
    status = el.get("status", "a")
    if chance is not None:
        return max(0.0, min(1.0, chance / 100))
    if status in ("i", "s", "u", "n"):
        return 0.0
    if status == "d":
        return 0.75
    return 1.0


@dataclass
class PlayerScore:
    id: int
    score: float  # expected points over the horizon (before differential bonus)
    adjusted: float  # after differential bonus — this is what the optimiser maximises
    per_gw: list[float]
    avail: float
    own: float | None  # share of sampled rivals owning him (None = no league data)


@dataclass
class Model:
    boot: dict
    elements: dict[int, dict]
    teams: dict[int, dict]
    fx: dict
    gws: list[int]
    scores: dict[int, PlayerScore] = field(default_factory=dict)
    diff_strength: float = 0.0
    has_rivals: bool = False


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def base_rate(el: dict) -> float:
    price = max(el["now_cost"] / 10, 3.5)
    ppg = _f(el.get("points_per_game"))
    base = config.W_FORM * _f(el.get("form")) + config.W_PPG * ppg + config.W_VALUE * ppg * (6.0 / price)
    if base <= 0:  # pre-season / no data: lean on FPL's own next-GW expectation
        base = 0.8 * _f(el.get("ep_next"))
    return base


def minutes_security(el: dict, finished: int) -> float:
    if finished < 1:
        return 1.0
    frac = min(1.0, el.get("minutes", 0) / (90 * finished))
    return min(1.0, frac / config.MINUTES_FULL_FRACTION)


def build_model(boot: dict, fixtures: list[dict], own_share: dict[int, float] | None,
                diff_level: str = config.DIFF_DEFAULT) -> Model:
    nxt = next_gameweek(boot)
    gws = [g for g in range(nxt, nxt + config.ADVISOR_HORIZON_GWS) if g <= 38]
    m = Model(boot, {e["id"]: e for e in boot["elements"]}, {t["id"]: t for t in boot["teams"]},
              fixture_index(fixtures), gws)
    m.has_rivals = own_share is not None
    m.diff_strength = config.DIFF_LEVELS.get(diff_level, 0.0) if m.has_rivals else 0.0
    fin = finished_gameweeks(boot)
    for el in boot["elements"]:
        base = base_rate(el) * minutes_security(el, fin)
        av = availability(el)
        per_gw = []
        for i, g in enumerate(gws):
            mult = sum(config.FDR_MULT.get(diff, 1.0) for _, _, diff in m.fx[el["team"]].get(g, []))
            a = av if i < 2 else 0.5 * (av + 1)  # an injury is assumed to matter most in the next two GWs
            per_gw.append(base * mult * a * config.ADVISOR_DECAY ** i)
        score = sum(per_gw)
        own = own_share.get(el["id"], 0.0) if own_share is not None else None
        adj = score * (1 + m.diff_strength * (1 - own)) if own is not None else score
        m.scores[el["id"]] = PlayerScore(el["id"], score, adj, per_gw, av, own)
    return m


# ---------------------------------------------------------------- prices
def buy_price(el: dict, transfers: list[dict]) -> int:
    """What the manager paid (tenths of £m): latest transfer-in cost, else the season-start price."""
    ins = [t for t in transfers if t.get("element_in") == el["id"]]
    if ins:
        return max(ins, key=lambda t: t.get("time", ""))["element_in_cost"]
    return el["now_cost"] - el.get("cost_change_start", 0)


def sell_price(el: dict, transfers: list[dict]) -> int:
    """FPL rule: you keep half of any price rise (rounded down), but take the full loss on a fall."""
    buy, now = buy_price(el, transfers), el["now_cost"]
    return buy + (now - buy) // 2 if now > buy else now


# --------------------------------------------------------------- lineups
def lineup(squad: list[int], model: Model, key: str = "adjusted") -> dict:
    """Best XI + captain + bench for a 15-man squad (formation chosen to maximise score)."""
    sc = lambda i: getattr(model.scores[i], key)
    by_pos: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
    for i in squad:
        by_pos[model.elements[i]["element_type"]].append(i)
    for p in by_pos:
        by_pos[p].sort(key=sc, reverse=True)
    best = None
    for d, mid, f in FORMATIONS:
        if len(by_pos[1]) < 1 or len(by_pos[2]) < d or len(by_pos[3]) < mid or len(by_pos[4]) < f:
            continue
        xi = by_pos[1][:1] + by_pos[2][:d] + by_pos[3][:mid] + by_pos[4][:f]
        total = sum(sc(i) for i in xi)
        if best is None or total > best[0]:
            best = (total, xi, (d, mid, f))
    if best is None:
        return {"total": 0.0, "xi": [], "captain": None, "bench": list(squad), "formation": None, "xi_score": 0.0}
    xi_total, xi, formation = best
    cap = max(xi, key=sc)
    bench = [i for i in squad if i not in xi]
    bench.sort(key=lambda i: (model.elements[i]["element_type"] != 1, -sc(i)))  # GK first, then by score
    total = xi_total + sc(cap) + config.BENCH_WEIGHT * sum(sc(i) for i in bench)
    return {"total": total, "xi": xi, "captain": cap, "bench": bench, "formation": formation, "xi_score": xi_total}


def club_ok(squad: list[int], elements: dict) -> bool:
    counts: dict[int, int] = defaultdict(int)
    for i in squad:
        counts[elements[i]["team"]] += 1
    return all(c <= 3 for c in counts.values())


def single_swaps(squad: list[int], model: Model, sell: dict[int, int], bank: int, top: int = 3) -> dict[int, list[dict]]:
    """For each squad player, the best same-position replacements (honours budget + club rule).
    gain = extra expected points; gain_adj = same including the differential bonus (used for ranking)."""
    base = lineup(squad, model)["total"]
    base_raw = lineup(squad, model, "score")["total"]
    in_squad = set(squad)
    by_pos: dict[int, list[int]] = defaultdict(list)
    for el in model.elements.values():
        if el["id"] not in in_squad and model.scores[el["id"]].avail >= 0.5 \
                and (el.get("minutes", 0) > 0 or el["now_cost"] <= 45):
            by_pos[el["element_type"]].append(el["id"])
    out: dict[int, list[dict]] = {}
    for pid in squad:
        el = model.elements[pid]
        budget = bank + sell[pid]
        rest = [i for i in squad if i != pid]
        opts = []
        # only the ~25 best-scoring affordable candidates per slot: keeps this O(15*25) lineups
        pool = sorted((j for j in by_pos[el["element_type"]] if model.elements[j]["now_cost"] <= budget),
                      key=lambda j: -model.scores[j].adjusted)[:25]
        for j in pool:
            new = rest + [j]
            if not club_ok(new, model.elements):
                continue
            opts.append({"in": j, "gain_adj": lineup(new, model)["total"] - base,
                         "gain": lineup(new, model, "score")["total"] - base_raw})
        opts.sort(key=lambda o: -o["gain_adj"])
        out[pid] = opts[:top]
    return out
