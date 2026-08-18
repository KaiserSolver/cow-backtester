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

Honesty labels (same doctrine as the rest of the tool):
- `basis`: field surpluses come from record amounts (net of protocol fees);
  a replayed challenger's come from its raw response (gross of fees) — a few
  bps flattering to the challenger on fee-bearing orders.
- `fairness`: the challenger's bids are ASSUMED fairness-surviving; the
  historical field uses CoW's actual `filteredOut` flags.
- `success_rate` is reported as a separate flag (`win_floor_met`), never
  silently folded in: a chain with zero wins in the period pays zero.
"""
from collections import defaultdict

from . import scorer


def executed_order_surpluses(record, body, ref_prices):
    """Per executed order, each non-filtered solver's surplus in native wei.

    Executed orders = the union of order ids in winning solutions. For every
    non-filteredOut solution touching such an order, surplus = the solution's
    executed amounts vs the body's signed limits, valued at the auction's
    reference price (the same basis for every solver, so the metric's RATIOS
    are unaffected by the fee wedge in record amounts).

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
        for o in s.get("orders") or []:
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
            if wei > table[uid].get(solver, -1):
                table[uid][solver] = wei
    return {u: d for u, d in table.items() if d}


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
    # their historical denominators — the small cross-term (our presence
    # shrinking THEIR terms) is ignored, which UNDERSTATES our share slightly:
    # conservative direction, labeled below.
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
        "basis": "field=record-amounts(net-of-fees) vs challenger=raw-response; "
                 "challenger fairness assumed; share is a conservative floor "
                 "(field denominators keep historical values)",
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
