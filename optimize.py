"""Phase 4: exact squad optimiser (mixed-integer programme solved with SciPy's HiGHS).

Picks a legal 15-man squad (2 GK / 5 DEF / 5 MID / 3 FWD, max 3 per club, within budget), a starting XI
in a legal formation and a captain, maximising XI score + captain bonus + a little bench value.
`max_changes` caps how many players may differ from the current squad, which turns the same model
into the 1-, 2- and 3-transfer recommender.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

import config
from advisor import SQUAD_SHAPE, Model, lineup


class NoSolution(Exception):
    pass


def candidate_ids(model: Model, current: list[int]) -> list[int]:
    keep = set(current)
    out = list(current)
    for el in model.elements.values():
        i = el["id"]
        if i in keep:
            continue
        if model.scores[i].avail >= 0.5 and (el.get("minutes", 0) > 0 or el["now_cost"] <= 45):
            out.append(i)
    return out


def solve(model: Model, current: list[int], cost: dict[int, int], budget: int,
          max_changes: int = 15) -> list[int]:
    """Best squad (list of 15 element ids) under the constraints. cost[i] = price in tenths of £m."""
    ids = candidate_ids(model, current)
    n = len(ids)
    els = [model.elements[i] for i in ids]
    sc = np.array([model.scores[i].adjusted for i in ids])
    price = np.array([cost[i] for i in ids], dtype=float)
    pos = np.array([e["element_type"] for e in els])
    club = np.array([e["team"] for e in els])
    cur = np.array([1 if i in set(current) else 0 for i in ids])

    # variable layout: x (in squad) [0,n) | y (in XI) [n,2n) | c (captain) [2n,3n)
    bw = config.BENCH_WEIGHT
    obj = np.concatenate([bw * sc - 0.0005 * price,        # x: bench value, tiny preference for cheaper
                          (1 - bw) * sc,                   # y: XI value on top of the bench value
                          sc])                             # c: captain bonus
    rows, lo, hi = [], [], []

    def add(coefs: dict[int, float], lb: float, ub: float):
        rows.append(coefs)
        lo.append(lb)
        hi.append(ub)

    add({j: 1 for j in range(n)}, 15, 15)
    for p, k in SQUAD_SHAPE.items():
        add({j: 1 for j in range(n) if pos[j] == p}, k, k)
    add({j: price[j] for j in range(n)}, 0, budget)
    for t in set(club.tolist()):
        add({j: 1 for j in range(n) if club[j] == t}, 0, 3)
    for j in range(n):
        add({n + j: 1, j: -1}, -np.inf, 0)                 # y <= x
        add({2 * n + j: 1, n + j: -1}, -np.inf, 0)         # c <= y
    add({n + j: 1 for j in range(n)}, 11, 11)
    add({2 * n + j: 1 for j in range(n)}, 1, 1)
    add({n + j: 1 for j in range(n) if pos[j] == 1}, 1, 1)
    add({n + j: 1 for j in range(n) if pos[j] == 2}, 3, 5)
    add({n + j: 1 for j in range(n) if pos[j] == 3}, 2, 5)
    add({n + j: 1 for j in range(n) if pos[j] == 4}, 1, 3)
    if max_changes < 15:
        add({j: 1 for j in range(n) if cur[j]}, 15 - max_changes, 15)

    A = lil_matrix((len(rows), 3 * n))
    for r, coefs in enumerate(rows):
        for j, v in coefs.items():
            A[r, j] = v
    res = milp(c=-obj, integrality=np.ones(3 * n), bounds=Bounds(0, 1),
               constraints=LinearConstraint(A.tocsr(), lo, hi), options={"time_limit": 20})
    if res.x is None or res.status not in (0, 1):
        raise NoSolution(res.message)
    x = np.round(res.x[:n]).astype(int)
    squad = [ids[j] for j in range(n) if x[j] == 1]
    if len(squad) != 15:
        raise NoSolution("solver returned an invalid squad")
    return squad


def best_changes(model: Model, current: list[int], cost: dict[int, int], budget: int, k: int) -> dict:
    """Best squad differing from `current` by at most k players. gain = extra expected points over the
    horizon; gain_adj = same including the differential bonus (what the optimiser maximised)."""
    new = solve(model, current, cost, budget, max_changes=k)
    cur_set, new_set = set(current), set(new)
    return {"squad": new, "out": sorted(cur_set - new_set), "in": sorted(new_set - cur_set),
            "gain": lineup(new, model, "score")["total"] - lineup(current, model, "score")["total"],
            "gain_adj": lineup(new, model)["total"] - lineup(current, model)["total"],
            "lineup": lineup(new, model)}
