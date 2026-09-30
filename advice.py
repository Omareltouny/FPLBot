"""Phase 4: turns the model + optimiser into /wildcard and /transfers answers."""
from __future__ import annotations

import html
from dataclasses import dataclass

import advisor
import config
import optimize
from advisor import POS_NAMES, Model
from league import Snapshot


@dataclass
class Plan:
    model: Model
    squad: list[int]
    sell: dict[int, int]  # current squad -> estimated selling price (tenths of £m)
    cost: dict[int, int]  # every player -> price used by the optimiser
    bank: int
    budget: int  # sum of current sell prices + bank
    gw: int  # gameweek the squad snapshot is from
    prices_estimated: bool  # True if we couldn't read transfer history (sell = current price)
    chip_available: bool | None


def own_share(snaps: dict[int, Snapshot], me_id: int) -> dict[int, float] | None:
    """Share of sampled rivals owning each player (None when there are no rivals to compare with)."""
    from league import ownership
    counts, n = ownership(snaps, me_id)
    if n == 0:
        return None
    return {el: c / n for el, c in counts.items()}


def make_plan(boot: dict, fixtures: list[dict], my_snap: Snapshot, transfers: list[dict] | None,
              shares: dict[int, float] | None, diff_level: str, gw: int, chip_available: bool | None = None) -> Plan:
    model = advisor.build_model(boot, fixtures, shares, diff_level)
    squad = [p["element"] for p in sorted(my_snap.picks, key=lambda p: p["position"])]
    sell = {i: (advisor.sell_price(model.elements[i], transfers) if transfers is not None
                else model.elements[i]["now_cost"]) for i in squad}
    cost = {i: el["now_cost"] for i, el in model.elements.items()}
    cost.update(sell)  # players you already own are valued at what you'd actually get for them
    bank = (my_snap.entry_history or {}).get("bank", 0) or 0
    return Plan(model, squad, sell, cost, bank, sum(sell.values()) + bank, gw, transfers is None, chip_available)


# ------------------------------------------------------------------ text
def _name(m: Model, i: int) -> str:
    return html.escape(m.elements[i]["web_name"])


def _team(m: Model, i: int) -> str:
    return m.teams[m.elements[i]["team"]]["short_name"]


def _fixtures(m: Model, i: int, n: int = 3) -> str:
    team = m.elements[i]["team"]
    parts = []
    for g in m.gws[:n]:
        fx = m.fx[team].get(g, [])
        if not fx:
            parts.append("–")
        else:
            parts.append("+".join(m.teams[o]["short_name"].upper() if home else m.teams[o]["short_name"].lower()
                                  for o, home, _ in fx))
    return " ".join(parts)


def _flags(m: Model, i: int) -> str:
    ps = m.scores[i]
    out = []
    if ps.avail < 1.0:
        out.append("⚠️ doubtful" if ps.avail >= 0.5 else "🚑 out")
    if ps.own is not None:
        out.append(f"{round(ps.own * 100)}% of rivals own" if ps.own > 0 else "no rival owns")
    return " · ".join(out)


def pline(m: Model, i: int, tag: str = "") -> str:
    el, ps = m.elements[i], m.scores[i]
    extra = _flags(m, i)
    return (f"{tag}<b>{_name(m, i)}</b> ({_team(m, i)}, £{el['now_cost'] / 10:.1f}m) — "
            f"{ps.score:.1f} pts · next: {_fixtures(m, i)}" + (f" · {extra}" if extra else ""))


def _money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def _assumptions(plan: Plan, sample: str | None) -> str:
    m = plan.model
    g = f"GW{m.gws[0]}–GW{m.gws[-1]}" if len(m.gws) > 1 else f"GW{m.gws[0]}"
    txt = (f"Scoring {g}: form, season points per game, points per £m, fixture difficulty (doubles and blanks), "
           f"injuries and rotation.")
    if m.has_rivals and m.diff_strength > 0:
        txt += f" Differential bonus on (+{round(m.diff_strength * 100)}% for a player none of your rivals own)."
    elif not m.has_rivals:
        txt += " No rival data, so no differential bonus (track a league with /trackleague to use it)."
    if sample:
        txt += f"\n{sample}"
    txt += f"\nYour squad is as at the GW{plan.gw} deadline; any transfers you've made since aren't visible to me."
    if plan.prices_estimated:
        txt += "\n⚠️ Couldn't read your transfer history, so sell prices are assumed to be today's prices."
    return txt


def _verdict_note(label: str) -> str:
    return f"<i>{label}</i>"


# --------------------------------------------------------------- wildcard
def build_wildcard(plan: Plan, view: str = "full", sample: str | None = None) -> list[str]:
    m = plan.model
    head = (f"<b>Wildcard planner</b>\nBudget {_money(plan.budget)} (squad {_money(sum(plan.sell.values()))} + bank "
            f"{_money(plan.bank)})\n{_assumptions(plan, sample)}")
    if plan.chip_available is False:
        head += "\n⚠️ It looks like you don't have a Wildcard available for that window."
    out = [head]
    if view == "lite":
        return out + _wildcard_lite(plan)
    return out + _wildcard_full(plan)


def _wildcard_full(plan: Plan) -> list[str]:
    m = plan.model
    new = optimize.solve(m, plan.squad, plan.cost, plan.budget, 15)
    cur_l, new_l = advisor.lineup(plan.squad, m, "score"), advisor.lineup(new, m, "score")
    new_adj = advisor.lineup(new, m)
    keep = [i for i in new if i in plan.squad]
    buy = [i for i in new if i not in plan.squad]
    sell = [i for i in plan.squad if i not in new]
    spent = sum(plan.cost[i] for i in new)

    out = [f"<b>Your best squad</b>: {new_adj['formation'][0]}-{new_adj['formation'][1]}-{new_adj['formation'][2]} · "
           f"expected points over the window <b>{new_l['total']:.0f}</b> vs <b>{cur_l['total']:.0f}</b> for your "
           f"current squad (<b>{new_l['total'] - cur_l['total']:+.0f}</b>)\n"
           f"Captain: <b>{_name(m, new_adj['captain'])}</b> · cost {_money(spent)} of {_money(plan.budget)} "
           f"(bank left {_money(plan.budget - spent)}) · keeping {len(keep)}, buying {len(buy)}"]

    def block(title: str, ids: list[int]) -> str:
        lines = []
        for p in (1, 2, 3, 4):
            for i in sorted((x for x in ids if m.elements[x]["element_type"] == p),
                            key=lambda x: -m.scores[x].adjusted):
                tag = "✅ KEEP " if i in plan.squad else "🟢 BUY "
                lines.append(pline(m, i, f"{POS_NAMES[p]} {tag}"))
        return f"<b>{title}</b>\n" + "\n".join(lines)

    out.append(block("Starting XI", new_adj["xi"]))
    out.append(block("Bench", new_adj["bench"]))
    if sell:
        lines = [pline(m, i, f"{POS_NAMES[m.elements[i]['element_type']]} 🔴 SELL ") +
                 f" · sells for {_money(plan.sell[i])}" for i in sorted(sell, key=lambda x: m.scores[x].adjusted)]
        out.append("<b>Sell</b>\n" + "\n".join(lines))
    else:
        out.append("<b>Sell</b>\nNothing — your current squad is already the best I can build.")
    return out


def _wildcard_lite(plan: Plan) -> list[str]:
    m = plan.model
    swaps = advisor.single_swaps(plan.squad, m, plan.sell, plan.bank)
    sells, keeps = [], []
    for i in sorted(plan.squad, key=lambda x: (m.elements[x]["element_type"], -m.scores[x].adjusted)):
        opts = swaps.get(i, [])
        best = opts[0] if opts else None
        out_of_action = m.scores[i].avail < 0.5
        if out_of_action or (best and best["gain_adj"] >= config.SELL_GAIN_MIN):
            repl = f" → {_name(m, best['in'])} ({_money(m.elements[best['in']]['now_cost'])}, " \
                   f"{best['gain']:+.1f} pts)" if best else ""
            sells.append(pline(m, i, f"{POS_NAMES[m.elements[i]['element_type']]} 🔴 SELL ") + repl)
        else:
            keeps.append(pline(m, i, f"{POS_NAMES[m.elements[i]['element_type']]} ✅ KEEP "))
    out = ["<b>Sell</b> (a clearly better same-position swap exists, or he's out)\n" + ("\n".join(sells) or "None")]
    out.append("<b>Keep</b>\n" + ("\n".join(keeps) or "None"))

    # top buys: best non-owned players per position, affordable within the best single-swap budget
    in_squad = set(plan.squad)
    top = []
    for p in (1, 2, 3, 4):
        pool = [el["id"] for el in m.elements.values()
                if el["element_type"] == p and el["id"] not in in_squad and m.scores[el["id"]].avail >= 0.5
                and (el.get("minutes", 0) > 0 or el["now_cost"] <= 45)]
        pool.sort(key=lambda x: -m.scores[x].adjusted)
        top += [(p, x) for x in pool[:2 if p == 1 else 3]]
    out.append("<b>Top buy targets</b>\n" + "\n".join(pline(m, x, f"{POS_NAMES[p]} 🟢 ") for p, x in top)
               + "\n\n<i>Want the whole squad rebuilt for you? Use /wildcard full.</i>")
    return out


# -------------------------------------------------------------- transfers
def build_transfers(plan: Plan, free_transfers: int = 1, sample: str | None = None) -> list[str]:
    m = plan.model
    head = (f"<b>Transfer advisor</b>\nBank {_money(plan.bank)} · assuming {free_transfers} free transfer"
            f"{'s' if free_transfers != 1 else ''} (each extra costs {config.HIT_COST} points)\n"
            f"{_assumptions(plan, sample)}")
    out = [head]
    results, seen = [], set()
    for k in range(1, config.MAX_TRANSFERS_SHOWN + 1):
        try:
            r = optimize.best_changes(m, plan.squad, plan.cost, plan.budget, k)
        except optimize.NoSolution:
            continue
        key = tuple(r["in"])
        if not r["in"] or key in seen:
            continue
        seen.add(key)
        n = len(r["in"])
        hit = config.HIT_COST * max(0, n - free_transfers)
        r.update(n=n, hit=hit, net=r["gain"] - hit, net_adj=r["gain_adj"] - hit)
        results.append(r)

    if not results:
        return out + ["No upgrade found within your budget — keep your squad as it is."]

    blocks = []
    for r in results:
        pairs = _pair_moves(m, r["out"], r["in"])
        lines = [f"{POS_NAMES[m.elements[o]['element_type']]}  🔴 {_name(m, o)} ({_money(plan.sell[o])}, "
                 f"{m.scores[o].score:.1f} pts) ➜ 🟢 {pline(m, i)}" for o, i in pairs]
        ok = r["net_adj"] > 0
        gain_txt = f"gain <b>{r['gain']:+.1f}</b> pts"
        if m.diff_strength > 0 and abs(r["gain_adj"] - r["gain"]) > 0.3:
            gain_txt += f" ({r['gain_adj']:+.1f} with differential bonus)"
        verdict = (f"hit −{r['hit']} → net <b>{r['net']:+.1f}</b> "
                   f"{'✅ worth it' if ok else '❌ not worth it'}") if r["hit"] else \
                  f"no hit → net <b>{r['net']:+.1f}</b> {'✅ worth it' if ok else '❌ not worth it'}"
        blocks.append(f"<b>{r['n']} transfer{'s' if r['n'] != 1 else ''}</b> — {gain_txt}, {verdict}\n" + "\n".join(lines))
    out += blocks

    worth = [r for r in results if r["net_adj"] > 0]
    if worth:
        best = max(worth, key=lambda r: r["net_adj"])
        out.append(f"<b>Recommendation:</b> make {best['n']} transfer{'s' if best['n'] != 1 else ''} "
                   f"(net {best['net']:+.1f} pts over the window).")
    else:
        out.append("<b>Recommendation:</b> no move beats its cost — roll your free transfer.")
    out.append("<i>Gains are estimated extra points over the window versus your current best XI; a hit is only "
               "worth it if the gain beats it. Captain is included in both lineups.</i>")
    return out


def _pair_moves(m: Model, outs: list[int], ins: list[int]) -> list[tuple[int, int]]:
    """Pair each outgoing player with an incoming one of the same position where possible."""
    remaining = list(ins)
    pairs = []
    for o in sorted(outs, key=lambda x: m.elements[x]["element_type"]):
        pos = m.elements[o]["element_type"]
        match = next((i for i in remaining if m.elements[i]["element_type"] == pos), remaining[0])
        remaining.remove(match)
        pairs.append((o, match))
    return pairs
