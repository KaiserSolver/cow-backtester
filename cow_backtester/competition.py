"""Historical competition records — fetch, archive, rank-vs-field.

The v2 endpoint `/api/v2/solver_competition/{auction_id}` (probed 2026-08-17:
serves records >= 2 months back, 404 for winnerless auctions) returns, per
auction: every submitted solution with `solverAddress`, `score`, `ranking`,
`isWinner`, `filteredOut` (CoW's OWN fairness-filtering outcome — no local
CIP-67 re-implementation needed), per-solution `clearingPrices` and executed
orders, per-solution `referenceScore`, the auction's native `prices`, and
`auctionStartBlock` / `auctionDeadlineBlock` (the auction-cut context a
settlement block cannot provide).

Rank basis honesty: the API's `score` includes protocol fees on top of
surplus; the challenger is ranked by its plain surplus (no fee uplift), so
its rank is a floor, never flattery. Every rank output is labeled
`rank_basis: "surplus_vs_score_proxy"` until fee-adjusted scoring lands.
"""
import gzip
import json
import os


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
    tmp = path + ".tmp"
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


def rank_vs_field(record, our_score_wei, self_address=None):
    """Insert a challenger score into the historical (fairness-surviving)
    field. Ties rank the challenger BELOW the historical bid (it was there
    first; also keeps the proxy conservative). Returns None if the record has
    no rankable field."""
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
        "rank_basis": "surplus_vs_score_proxy",
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
        "rank_basis": "surplus_vs_score_proxy",
        "rivals": [{"solver": a,
                    "wins": v["wins"],
                    "median_gap_bps": (sorted(v["gaps"])[len(v["gaps"]) // 2]
                                       if v["gaps"] else None)}
                   for a, v in rivals],
    }
