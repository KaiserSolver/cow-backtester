"""CIP-85 v2 consistency economics from archived competition records.

The v2 consistency metric (forum t/3474, effective 2026-06-30) pays
`success_rate × Σ_executed_orders( surplus_you(o) / Σ_solvers surplus_s(o) )`
per solver per chain per week, where the sum runs over orders that actually
SETTLED, each solver contributes its best fairness-surviving bid on that
order, and success_rate is settled/won (zero if the solver won nothing that
period on that chain). The weekly pool splits pro-rata over every solver's
metric sum.

Everything in that formula is computable from data this tool already has:
the competition record carries every solution's executed amounts per order
and CoW's own fairness flag (`filteredOut`), the S3 auction body carries the
signed limits, and the auction's native prices value surplus consistently
across solvers. That yields, per auction:

- each FIELD solver's metric terms (their real, historical consistency),
- a CHALLENGER's counterfactual terms, inserted into the denominators,
- and, aggregated, a consistency share that converts to COW when a weekly
  budget is supplied.

Basis (v0.10.0): the record's per-order executed amounts are NET of protocol
fees while a replayed challenger is scored on the tool's uniform-price basis,
which equals the official `score` (gross of protocol fees) to within 0.2% —
dividing one by the other inflated the challenger's share ~1.4x on measured
records. So a field solution carrying exactly ONE order now contributes that
solution's `score` (the score-basis surplus of that order); multi-order
solutions fall back to the per-order net computation and are counted under
`net_basis_orders` so the residual mixing is disclosed, never silent.

Honesty labels (same doctrine as the rest of the tool):
- `fairness`: the challenger's bids are ASSUMED fairness-surviving; the
  historical field uses CoW's actual `filteredOut` flags.
- `success_rate` is reported as a separate flag (`win_floor_met`), never
  silently folded in: a chain with zero wins in the period pays zero. The
  challenger's estimate therefore assumes success_rate = 1.
- auctions where the challenger ERRORED stay in both sides with a zero
  challenger term (the field still earned its terms there).
"""
from collections import defaultdict

from . import scorer


def executed_order_surpluses(record, body, ref_prices, stats=None):
    """Per executed order, each non-filtered solver's surplus in native wei,
    on the SCORE basis where the record allows it.

    Executed orders = the union of order ids in winning solutions. For every
    non-filteredOut solution touching such an order:
    - a solution with exactly ONE order contributes its `score` — the official
      score-basis surplus of that order, the same basis a replayed challenger
      is scored on (`score_basis_orders` in `stats`);
    - otherwise surplus = executed amounts vs the body's signed limits at the
      auction reference price, which is NET of protocol fees and therefore
      understates the score basis by the fee wedge (`net_basis_orders`).

    Returns {uid: {solver_address: best_surplus_wei}}.
    """
    by_uid = {o["uid"].lower(): o for o in body.get("orders", [])}
    executed = set()
    for s in record.get("solutions") or []:
        if s.get("isWinner"):
            for o in s.get("orders") or []:
                executed.add((o.get("id") or "").lower())
    executed.discard("")

    table = defaultdict(dict)
    for s in record.get("solutions") or []:
        if s.get("filteredOut"):
            continue
        solver = (s.get("solverAddress") or "").lower()
        orders = s.get("orders") or []
        if len(orders) == 1:
            uid = (orders[0].get("id") or "").lower()
            try:
                score = int(s.get("score"))
            except (TypeError, ValueError):
                score = None
            if uid in executed and score is not None and score > 0:
                if stats is not None:
                    stats["score_basis_orders"] += 1
                if score > table[uid].get(solver, -1):
                    table[uid][solver] = score
                continue
        for o in orders:
            uid = (o.get("id") or "").lower()
            if uid not in executed:
                continue
            order = by_uid.get(uid)
            if not order:
                continue
            try:
                exec_sell = int(o["sellAmount"])
                exec_buy = int(o["buyAmount"])
                kind = order["kind"]
                limit_sell = int(order.get("fullSellAmount", order["sellAmount"]))
                limit_buy = int(order.get("fullBuyAmount", order["buyAmount"]))
                executed_amt = exec_sell if kind == "sell" else exec_buy
                s_atoms = scorer.gross_surplus_atoms(
                    kind, executed_amt, limit_sell, limit_buy, exec_buy, exec_sell)
                if s_atoms <= 0:
                    continue
                stoken = scorer.surplus_token(
                    kind, order["sellToken"].lower(), order["buyToken"].lower())
                ref = ref_prices.get(stoken)
                if ref is None:
                    continue
                wei = scorer.to_native(s_atoms, ref)
            except (KeyError, TypeError, ValueError):
                continue
            if stats is not None:
                stats["net_basis_orders"] += 1
            if wei > table[uid].get(solver, -1):
                table[uid][solver] = wei
    return {u: d for u, d in table.items() if d}


def zero_challenger_row(surplus_table):
    """The per-auction row for an auction where the challenger ERRORED (or
    returned an unparseable response): the field's real terms, nothing for
    us. Dropping such auctions from both sides inflated the share (measured
    +27% with 3 of 10 auctions errored)."""
    field, _ch, _n = consistency_terms(surplus_table)
    return {"field": field, "challenger": 0.0, "challenger_orders": 0,
            "executed_orders": len(surplus_table), "challenger_won": False}


def field_leaderboard(field_rows, top_n=8, self_address=None):
    """Window-level FIELD consistency leaderboard from per-auction field
    terms — a purely historical quantity that needs no challenger and no
    solver endpoint. field_rows: list of {solver: terms} dicts."""
    rows = [r for r in field_rows if r]
    if not rows:
        return None
    total = defaultdict(float)
    for r in rows:
        for solver, t in r.items():
            total[solver] += t
    pool = sum(total.values())
    board = sorted(total.items(), key=lambda kv: -kv[1])
    out = {
        "auctions": len(rows),
        "pool_metric": round(pool, 4),
        "leaderboard": [{"solver": s, "metric": round(t, 4),
                         "share_pct": round(100 * t / pool, 2) if pool else 0.0}
                        for s, t in board[:top_n]],
        "basis": "score basis for single-order solutions (official score), net-of-fee "
                 "fallback for multi-order solutions; fairness per CoW's filteredOut flags",
    }
    if self_address:
        mine = total.get(self_address.lower(), 0.0)
        out["historical_self_metric"] = round(mine, 4)
        out["historical_self_share_pct"] = round(100 * mine / pool, 2) if pool else 0.0
    return out


def consistency_terms(surplus_table, challenger_by_order=None):
    """Metric terms per solver for one auction.

    Field solver terms use the historical denominators. Challenger terms
    insert the challenger's surplus into each order's denominator (its
    presence would have diluted everyone, itself included).

    Returns ({solver: sum_of_terms}, challenger_sum, challenger_orders_bid).
    """
    field = defaultdict(float)
    challenger = 0.0
    challenger_orders = 0
    cbo = {k.lower(): v for k, v in (challenger_by_order or {}).items()}
    for uid, per_solver in surplus_table.items():
        denom = sum(per_solver.values())
        if denom <= 0:
            continue
        for solver, wei in per_solver.items():
            field[solver] += wei / denom
        ours = cbo.get(uid, 0)
        if ours > 0:
            challenger += ours / (denom + ours)
            challenger_orders += 1
    return dict(field), challenger, challenger_orders


def aggregate_report(per_auction, challenger_name, budget_cow=None,
                     self_address=None, top_n=8):
    """Fold per-auction results into the window-level consistency report.

    per_auction: list of dicts with keys field (solver->terms), challenger,
    challenger_orders, executed_orders, challenger_won (bool).
    """
    rows = [r for r in per_auction if r]
    if not rows:
        return None
    field_total = defaultdict(float)
    challenger_sum = 0.0
    challenger_orders = 0
    executed_total = 0
    wins = 0
    for r in rows:
        for solver, t in r["field"].items():
            field_total[solver] += t
        challenger_sum += r["challenger"]
        challenger_orders += r["challenger_orders"]
        executed_total += r["executed_orders"]
        wins += bool(r.get("challenger_won"))
    # Challenger share of the (challenger-inclusive) pool. Field terms keep
    # their historical denominators — the cross-term (our presence shrinking
    # THEIR terms) is ignored, which understates our share somewhat; the
    # basis mismatch that used to OVERSTATE it (net field vs gross challenger)
    # is fixed at the source (executed_order_surpluses), so the residual
    # direction is conservative on records made of single-order solutions.
    pool = sum(field_total.values()) + challenger_sum
    share = challenger_sum / pool if pool > 0 else 0.0
    leaderboard = sorted(field_total.items(), key=lambda kv: -kv[1])[:top_n]
    out = {
        "auctions": len(rows),
        "executed_orders": executed_total,
        "challenger": challenger_name,
        "challenger_metric": round(challenger_sum, 4),
        "challenger_orders_bid": challenger_orders,
        "challenger_share_pct": round(100 * share, 2),
        "win_floor_met": wins > 0,
        "counterfactual_wins": wins,
        "field_leaderboard": [
            {"solver": s, "metric": round(t, 4)} for s, t in leaderboard],
        "basis": "field=score basis (single-order solutions) with net-of-fee fallback "
                 "(multi-order) vs challenger=uniform-price basis (== score within 0.2%); "
                 "challenger fairness and success_rate=1 assumed; errored auctions kept "
                 "with a zero challenger term; field denominators keep historical values "
                 "(cross-term ignored)",
    }
    if self_address:
        out["historical_self_metric"] = round(
            field_total.get(self_address.lower(), 0.0), 4)
    if budget_cow is not None:
        est = share * budget_cow
        out["budget_cow"] = budget_cow
        out["consistency_cow_estimate"] = round(est, 2)
        if wins == 0:
            out["consistency_cow_estimate_note"] = (
                "win floor NOT met in sample — a real week with zero wins on "
                "this chain pays ZERO consistency regardless of the metric")
    return out
