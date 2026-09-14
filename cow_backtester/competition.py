"""Historical competition records — fetch, archive, rank-vs-field.

The v2 endpoint `/api/v2/solver_competition/{auction_id}` (probed 2026-08-17:
serves records >= 2 months back, 404 for winnerless auctions) returns, per
auction: every submitted solution with `solverAddress`, `score`, `ranking`,
`isWinner`, `filteredOut` (CoW's OWN fairness-filtering outcome for the
historical field), the solution's executed `orders` (id, sellAmount,
buyAmount), per-solution `referenceScore`, the auction's native `prices`, and
`auctionStartBlock` / `auctionDeadlineBlock` (the auction-cut context a
settlement block cannot provide). The per-solution `clearingPrices` field is
deprecated and EMPTY for auctions produced by recent autopilots; nothing in
this package reads it (the only clearing prices used anywhere are decoded from
on-chain settle() calldata, scorer.py).

The historical field's fairness is CoW's flag. A replayed CHALLENGER has no
flag, so `pair_baselines` re-derives the CIP-67 rule the autopilot applies
(winner_selection: a solution trading more than one directed token pair is
filtered when any of its pairs scores below the best single-pair solution on
that pair) from the record, and backtest.validate_and_score applies it.

Rank basis: the challenger is ranked by its best SINGLE solution on the
tool's uniform-price basis, which agrees with the API's official `score` to
within 0.2% on live records (the uniform-vs-custom price wedge is the
protocol fee the score adds back). The rank is therefore approximately exact,
deviating only by a solver-determined fee (zero on chains where the driver
bakes fees into custom prices) and by buy-order native conversion. Every rank
output carries `rank_basis` naming what was compared.
"""
import gzip
import json
import os
import threading


def fetch_competition(chain_api_base, auction_id, http_get, cache=None):
    """Fetch one competition record; None on 404/network failure. Positive
    results are immutable (records exist only after winner selection) and are
    cached under ("competition", auction_id); misses are NOT cached — a fresh
    auction's record may simply not be published yet."""
    if cache:
        hit = cache.get("competition", str(auction_id))
        if hit is not None:
            return hit
    url = f"{chain_api_base}/api/v2/solver_competition/{auction_id}"
    try:
        rec = json.loads(http_get(url, timeout=15))
    except Exception:
        return None
    if not isinstance(rec, dict) or rec.get("auctionId") is None:
        return None
    if cache:
        cache.put("competition", str(auction_id), rec)
    return rec


def archive_path(archive_dir, chain, auction_id):
    return os.path.join(archive_dir, chain, f"{auction_id}.json.gz")


def archive_store(archive_dir, chain, record):
    """Persist one record as {dir}/{chain}/{aid}.json.gz (idempotent — the
    file's existence is the dedupe). Returns True if newly written."""
    aid = record.get("auctionId")
    if aid is None:
        return False
    path = archive_path(archive_dir, chain, aid)
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # process+thread-unique temp name: a --watch run and a manual run sharing
    # an archive dir must not interleave into one .tmp and os.replace a
    # corrupt gzip into place
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(record, f, separators=(",", ":"))
    os.replace(tmp, path)
    return True


def _fair_scores(record):
    """(solverAddress, score) for solutions that survived CoW's fairness
    filtering, best first. Solutions the API marks filteredOut are the ones
    the protocol itself excluded — keeping them would rank the challenger
    against bids that never counted."""
    out = []
    for s in record.get("solutions") or []:
        if s.get("filteredOut"):
            continue
        try:
            out.append(((s.get("solverAddress") or "").lower(), int(s.get("score"))))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def pair_baselines(record, body, stats=None):
    """CIP-67 per-directed-pair baselines from the historical field.

    Mirrors autopilot `winner_selection::compute_baseline_scores`: for every
    submitted solution whose orders all lie on ONE directed (sell, buy) pair,
    the pair's baseline is the best such solution's `score` (CIP-38 score,
    native wei — the basis a replayed challenger's uniform-price surplus
    agrees with to within 0.2%). Multi-pair solutions never set a baseline;
    `filteredOut` is irrelevant here because a single-pair solution cannot be
    filtered by fairness. Solutions with a non-positive score are skipped as
    the autopilot skips them.

    Order → pair comes from the auction body's orders; a solution carrying an
    order the body does not know (a JIT order, an unknown uid) cannot be
    placed on a pair and is skipped, counted in stats["fairness_unmapped"].

    Returns {(sell_token_lower, buy_token_lower): score_wei}, possibly empty
    (empty still means "evaluated": the field simply set no baselines).
    """
    pair_of = {}
    for o in body.get("orders") or []:
        try:
            pair_of[o["uid"].lower()] = (o["sellToken"].lower(), o["buyToken"].lower())
        except (KeyError, AttributeError):
            continue
    baselines = {}
    for s in record.get("solutions") or []:
        try:
            score = int(s.get("score"))
        except (TypeError, ValueError):
            continue
        if score <= 0:
            continue
        pairs = set()
        unmapped = False
        for o in s.get("orders") or []:
            p = pair_of.get((o.get("id") or "").lower())
            if p is None:
                unmapped = True
                break
            pairs.add(p)
        if unmapped:
            if stats is not None:
                stats["fairness_unmapped"] += 1
            continue
        if len(pairs) != 1:
            continue
        (p,) = tuple(pairs)
        if score > baselines.get(p, 0):
            baselines[p] = score
    return baselines


def rank_vs_field(record, our_score_wei, self_address=None):
    """Insert ONE challenger score (a single solution's — the protocol ranks
    solutions, see backtest.challenger_rank) into the historical
    fairness-surviving field. Ties rank the challenger BELOW the historical
    bid (it was there first). Returns None if the record has no rankable
    field."""
    field = _fair_scores(record)
    if not field:
        return None
    rank = 1 + sum(1 for _, sc in field if sc >= our_score_wei)
    winner_addr, winner_score = field[0]
    gap_bps = (None if winner_score <= 0
               else round((winner_score - our_score_wei) / winner_score * 1e4, 2))
    out = {
        "rank": rank,
        "field_size": len(field),
        "winner_solver": winner_addr,
        "winner_score_wei": winner_score,
        "gap_to_winner_bps": gap_bps,
        "beats_winner": our_score_wei > winner_score,
        "filtered_out_bids": sum(1 for s in record.get("solutions") or []
                                 if s.get("filteredOut")),
        "field_solvers": len({a for a, _ in field}),
        "rank_basis": "solution_score_vs_field_scores",
    }
    if self_address:
        mine = [s for s in record.get("solutions") or []
                if (s.get("solverAddress") or "").lower() == self_address.lower()]
        if mine:
            best = max(mine, key=lambda s: int(s.get("score") or 0))
            out["historical_self"] = {
                "score_wei": int(best.get("score") or 0),
                "ranking": best.get("ranking"),
                "is_winner": bool(best.get("isWinner")),
                "filtered_out": bool(best.get("filteredOut")),
            }
    return out


def field_table(rank_rows, top_n=5):
    """Aggregate rank_vs_field rows: rank distribution + the rivals who won.
    rank_rows: list of rank_vs_field() outputs (None entries dropped)."""
    rows = [r for r in rank_rows if r]
    if not rows:
        return None
    n = len(rows)
    ranks = sorted(r["rank"] for r in rows)
    gaps = sorted(r["gap_to_winner_bps"] for r in rows
                  if r["gap_to_winner_bps"] is not None)
    winners = {}
    for r in rows:
        w = winners.setdefault(r["winner_solver"], {"wins": 0, "gaps": []})
        w["wins"] += 1
        if r["gap_to_winner_bps"] is not None:
            w["gaps"].append(r["gap_to_winner_bps"])
    rivals = sorted(winners.items(), key=lambda kv: -kv[1]["wins"])[:top_n]
    return {
        "auctions_ranked": n,
        "rank1_pct": round(100 * sum(1 for r in ranks if r == 1) / n, 1),
        "top3_pct": round(100 * sum(1 for r in ranks if r <= 3) / n, 1),
        "median_rank": ranks[n // 2],
        "median_gap_bps": gaps[len(gaps) // 2] if gaps else None,
        "rank_basis": rows[0].get("rank_basis", "solution_score_vs_field_scores"),
        "rivals": [{"solver": a,
                    "wins": v["wins"],
                    "median_gap_bps": (sorted(v["gaps"])[len(v["gaps"]) // 2]
                                       if v["gaps"] else None)}
                   for a, v in rivals],
    }
