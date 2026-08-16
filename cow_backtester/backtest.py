#!/usr/bin/env python3
"""
CoW historical-auction backtester + counterfactual scorecard.

Replays recent historical CoW auctions offline and scores solutions against
what ACTUALLY won on-chain — no live shadow mode, no production risk, no keys.

  BASELINE (no solver needed): enumerate winning settlements, group them by
  auction (since CIP-67, ONE auction can settle through SEVERAL winning
  transactions), and reconstruct each winning set's surplus from public data.

  COUNTERFACTUAL (--solver-url, repeatable): fetch each auction's exact /solve
  body from the S3 instance bucket, POST it to each solver, validate the
  response, and score it on the SAME basis as the winners. With two solvers
  this is an A/B on identical inputs: judge a config or routing change before
  deploying it.

Comparison basis (identical on both sides — see scorer.py): before-fee surplus
over the SIGNED order limits at uniform clearing prices, converted to the
chain's native token at the auction's referencePrice. That equals user surplus
+ protocol fees + network fee; CoW's official ranking score is user surplus +
protocol fees only, so absolute levels here overstate the official score by the
network-fee component (documented in README).

Winner data comes from on-chain settlements, not an API. Use --verify-api to
cross-check the reconstruction against the (retention-bounded) v2 competition
endpoint.

Usage:
  cow-backtester --chain base --blocks 2000
  cow-backtester --chain base --from-block 49620000 --to-block 49630000 \
        --solver-url http://localhost:8080 --json-out results.jsonl
  cow-backtester --chain base --blocks 5000 \
        --solver-url http://a:8080 --solver-name baseline \
        --solver-url http://b:8080 --solver-name candidate --html-out ab.html
"""

import argparse
import gzip
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import scorer
from .cache import Cache
from .scorer import VERSION, _ceildiv

# Progress/diagnostics go to stderr so stdout stays clean for the scorecard
# (pipe-friendly: `cow-backtester ... > scorecard.txt` captures only results).
QUIET = False


def say(*a, **k):
    if not QUIET:
        print(*a, file=sys.stderr, **k)
        sys.stderr.flush()

BUCKET = "https://solver-instances.s3.amazonaws.com"
COW_API = "https://api.cow.fi"

# Chains served by the S3 instance bucket (enumerated live from the bucket).
# chain ids and RPCs verified against live endpoints, 2026-08; native = the gas token,
# which is the unit every surplus number here is denominated in.
CHAINS = {
    "mainnet":      {"id": 1,     "native": "ETH",   "api": "mainnet",
                     "rpcs": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"]},
    "arbitrum-one": {"id": 42161, "native": "ETH",   "api": "arbitrum_one",
                     "rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"]},
    "base":         {"id": 8453,  "native": "ETH",   "api": "base",
                     "rpcs": ["https://mainnet.base.org", "https://base-rpc.publicnode.com"]},
    "xdai":         {"id": 100,   "native": "xDAI",  "api": "xdai",
                     "rpcs": ["https://rpc.gnosischain.com", "https://gnosis-rpc.publicnode.com"]},
    "polygon":      {"id": 137,   "native": "POL",   "api": "polygon",
                     "rpcs": ["https://polygon-bor-rpc.publicnode.com"]},
    "bnb":          {"id": 56,    "native": "BNB",   "api": "bnb",
                     "rpcs": ["https://bsc-dataseed.bnbchain.org", "https://bsc-rpc.publicnode.com"]},
    "avalanche":    {"id": 43114, "native": "AVAX",  "api": "avalanche",
                     "rpcs": ["https://api.avax.network/ext/bc/C/rpc",
                              "https://avalanche-c-chain-rpc.publicnode.com"]},
    "linea":        {"id": 59144, "native": "ETH",   "api": "linea",
                     "rpcs": ["https://rpc.linea.build", "https://linea-rpc.publicnode.com"]},
    "ink":          {"id": 57073, "native": "ETH",   "api": "ink",
                     "rpcs": ["https://rpc-gel.inkonchain.com", "https://ink.drpc.org"]},
    "plasma":       {"id": 9745,  "native": "XPL",   "api": "plasma",
                     "rpcs": ["https://rpc.plasma.to"]},
}

# Stablecoins per chain (address -> decimals), used ONLY to derive the
# auction's own native/USD rate from referencePrice for size bucketing.
# A missing entry costs nothing but an "unknown" size bucket.
STABLES = {
    "arbitrum-one": {"0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,
                     "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,
                     "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": 18},
    "base":         {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6},
    "mainnet":      {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,
                     "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,
                     "0x6b175474e89094c44da98b954eedeac495271d0f": 18},
    "xdai":         {"0xddafbb505ad214d7b80b1f830fccc89b60fb7a83": 6,   # USDC
                     "0x4ecaba5870353805a9f068101a40e0f32ed605c6": 6},  # USDT
    "polygon":      {"0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6,   # USDC
                     "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 6},  # USDT
    "bnb":          {"0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,  # USDC
                     "0x55d398326f99059ff775485246999027b3197955": 18},  # USDT
    "avalanche":    {"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": 6,   # USDC
                     "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": 6},  # USDT
    "linea":        {"0x176211869ca2b568f2a7d4ee941e073a821ee1ff": 6},  # USDC
}

SIZE_BUCKETS = [(0, 100, "$0-100"), (100, 1e3, "$100-1k"), (1e3, 1e4, "$1k-10k"),
                (1e4, 1e5, "$10k-100k"), (1e5, 1e6, "$100k-1M"), (1e6, 1e15, "$1M+")]

_DEC = re.compile(r"^[0-9]+$")
_HEX = re.compile(r"^0x[0-9a-fA-F]+$")
REORG_MARGIN = 32          # don't cache settlements newer than this many blocks
CAVEATS = [
    "Replay uses LIVE liquidity; winners are historical. The counterfactual is indicative, "
    "not a claim about what would have happened at that block.",
    "Surplus basis = user surplus + protocol fees + network fee (before-fee, signed limits, "
    "uniform prices). CoW's official score excludes the network fee.",
    "Beating the winning set is necessary, not sufficient: real winner selection also applies "
    "fairness filters and bids score net of gas.",
    "Solver prices are CLAIMED, not simulated. Feasibility is checked; routes are not executed.",
    "Wrapper-routed settlements (solver router entrypoints; measured Aug 2026: ~43% of mainnet, "
    "~40% of Base, ~2% of Arbitrum settlements, and larger than direct ones at the median) are "
    "attributed via the v2 by-tx-hash endpoint and PROVEN by uid overlap with the S3 body, then "
    "scored on a DELIVERED basis (Trade events vs signed limits) which understates the direct "
    "before-fee basis by the fee wedge. Unresolvable ones are reported under wrapper_unattributed.",
]


# ---------------------------------------------------------------- primitives

def to_u256(v):
    """Parse a DTO U256: non-negative int, decimal string, or 0x-hex string.
    Rejects negatives, bools and exotic Python int syntax the DTO would refuse."""
    if isinstance(v, bool):
        raise ValueError(f"not a U256: {v!r}")
    if isinstance(v, int):
        if v < 0:
            raise ValueError(f"negative U256: {v!r}")
        return v
    if isinstance(v, str):
        s = v.strip()
        if _HEX.match(s):
            return int(s, 16)
        if _DEC.match(s):
            return int(s)
    raise ValueError(f"not a U256: {v!r}")


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"cow-backtester/{VERSION}"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def s3_auction(env, chain, auction_id, cache=None):
    """Archived /solve body (immutable, so always cacheable)."""
    if cache:
        hit = cache.get(f"s3-{env}", auction_id)
        if hit is not None:
            return hit
    url = f"{BUCKET}/{env}/{chain}/auction/{auction_id}.json"
    try:
        raw = _http_get(url)
        body = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        out = json.loads(body)
    except Exception:
        return None
    if cache:
        cache.put(f"s3-{env}", auction_id, out)
    return out


def fetch_settlement_cached(rpcs, tx, cache, max_cacheable_block):
    """Settlement tx + receipt. Cached only below the reorg margin."""
    if cache:
        hit = cache.get("settlement", tx)
        if hit is not None:
            return hit
    s = scorer.fetch_settlement(rpcs, tx)
    if s and cache and s["block"] <= max_cacheable_block:
        cache.put("settlement", tx, s)
    return s


def block_ts(rpcs, block, mem, cache, max_cacheable_block):
    if block in mem:
        return mem[block]
    if cache and block <= max_cacheable_block:
        hit = cache.get("blockts", block)
        if hit is not None:
            mem[block] = hit
            return hit
    b = scorer.rpc(rpcs, "eth_getBlockByNumber", [hex(block), False])
    ts = int(b["timestamp"], 16) if b and b.get("timestamp") else None
    mem[block] = ts
    if ts is not None and cache and block <= max_cacheable_block:
        cache.put("blockts", block, ts)
    return ts


def enumerate_settlements(rpc_urls, from_block, to_block, step=800):
    """Unique settlement tx hashes in [from_block, to_block] via Trade events.

    Returns (tx_hashes_oldest_first, failed_ranges). getLogs failures are never
    swallowed as zero: failing ranges are split and retried, and whatever still
    fails comes back in failed_ranges so the caller reports the gap."""
    seen, rows, failed = set(), [], []

    def fetch(lo, hi, depth=0):
        logs = scorer.rpc(rpc_urls, "eth_getLogs", [{
            "fromBlock": hex(lo), "toBlock": hex(hi),
            "address": scorer.SETTLEMENT, "topics": [scorer.TRADE_TOPIC],
        }])
        if logs is None:
            if hi - lo <= 50 or depth >= 8:
                failed.append((lo, hi))
                return
            mid = (lo + hi) // 2
            fetch(lo, mid, depth + 1)
            fetch(mid + 1, hi, depth + 1)
            return
        for lg in logs:
            h = lg["transactionHash"]
            if h not in seen:
                seen.add(h)
                rows.append((int(lg["blockNumber"], 16), h))

    b = from_block
    while b <= to_block:
        end = min(b + step - 1, to_block)
        fetch(b, end)
        b = end + 1
    rows.sort()
    return [h for _, h in rows], failed


# ------------------------------------------------------------------- sizing

def derive_native_usd(body, chain):
    """Native/USD from the auction's own reference prices: for a stable with d
    decimals, native_usd = 1e36 / (10^d * referencePrice). Median over the
    stables present, sanity-bounded. None if underivable."""
    tokens_lc = {k.lower(): v for k, v in body.get("tokens", {}).items()}
    rates = []
    for addr, dec in STABLES.get(chain, {}).items():
        t = tokens_lc.get(addr)
        if not t or t.get("referencePrice") is None:
            continue
        ref = int(t["referencePrice"])
        if ref <= 0:
            continue
        rate = 1e36 / (10 ** dec * ref)
        if 0.01 < rate < 1_000_000:
            rates.append(rate)
    return statistics.median(rates) if rates else None


def trade_usd(pt, ref_prices, native_usd):
    if native_usd is None:
        return None
    leg = pt["sell"] if pt["kind"] == "sell" else pt["buy"]
    ref = ref_prices.get(leg)
    if ref is None:
        return None
    return (pt["executed"] * ref // 10**18) / 1e18 * native_usd


def size_bucket(usd):
    if usd is None:
        return "unknown"
    for lo, hi, label in SIZE_BUCKETS:
        if lo <= usd < hi:
            return label
    return "unknown"


def token_symbol(body, addr):
    for k, v in body.get("tokens", {}).items():
        if k.lower() == addr:
            s = v.get("symbol")
            if s:
                return s
    return addr[:8]


# ------------------------------------------------------------------ solving

def solve(solver_url, auction_body, timeout):
    """POST a /solve body. Returns (response|None, error|None, latency_ms).
    The HTTP wait exceeds the advertised deadline so a solver computing right
    up to its deadline is never cut off."""
    body = json.dumps(auction_body).encode()
    req = urllib.request.Request(f"{solver_url.rstrip('/')}/solve", body,
                                 {"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        raw = urllib.request.urlopen(req, timeout=timeout + 5).read()
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}", int((time.monotonic() - t0) * 1000)
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e)).lower()
        return None, ("timeout" if "timed out" in reason else "unreachable"), int((time.monotonic() - t0) * 1000)
    except TimeoutError:
        return None, "timeout", int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return None, f"unreachable ({type(e).__name__})", int((time.monotonic() - t0) * 1000)
    ms = int((time.monotonic() - t0) * 1000)
    try:
        out = json.loads(raw)
    except Exception:
        return None, "bad_json", ms
    if not isinstance(out, dict):
        return None, "bad_json", ms
    return out, None, ms


def preflight(solver_url, timeout=10):
    """Fail-fast reachability probe. Any HTTP response proves the endpoint is
    alive. Returns (ok, reason)."""
    minimal = {"id": "0", "orders": [], "tokens": {}, "liquidity": [],
               "effectiveGasPrice": "0",
               "deadline": (datetime.now(timezone.utc) + timedelta(seconds=5)
                            ).isoformat().replace("+00:00", "Z"),
               "surplusCapturingJitOrderOwners": []}
    req = urllib.request.Request(f"{solver_url.rstrip('/')}/solve", json.dumps(minimal).encode(),
                                 {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True, None
    except urllib.error.HTTPError:
        return True, None
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e)).lower()
        return False, ("accepted the connection but never responded (solver hung?)"
                       if "timed out" in reason else "connection-level failure")
    except TimeoutError:
        return False, "accepted the connection but never responded (solver hung?)"
    except Exception as e:
        return False, f"connection-level failure ({type(e).__name__})"


class _Malformed(Exception):
    pass


def validate_and_score(resp, body, ref_prices):
    """Validate + score a solver response on the shared comparison basis.

    A solution is scored only if EVERY fulfillment trade is feasible: known
    order uid (case-insensitive), strict U256 numerics, prices for both tokens,
    at most one fulfillment per uid (GPv2 accumulates fills; duplicates are
    rejected as the driver does), no over-fill (exact for fill-or-kill), and
    on-chain feasibility at the fee-adjusted terms — the net delivery must
    still cover the gross-scaled limit, so a padded fee cannot manufacture
    surplus. Violations invalidate the SOLUTION (tallied), never score zero.

    Valid solutions are combined CIP-67 style: best-first, disjoint directed
    (sell,buy) pairs — a strong solver can win several slots of one auction.
    """
    out = {"best_surplus_wei": 0, "best_single_wei": 0, "n_solutions": 0,
           "n_valid": 0, "invalid": {}, "jit_ignored": 0, "no_refprice": 0,
           "by_pair": {}}
    if not isinstance(resp, dict) or not isinstance(resp.get("solutions"), list):
        return out
    by_uid = {o["uid"].lower(): o for o in body.get("orders", [])}
    invalid = Counter()
    candidates = []          # (surplus_wei, frozenset(pairs), {pair: wei})

    for sol in resp["solutions"]:
        out["n_solutions"] += 1
        reasons = set()
        try:
            if not isinstance(sol, dict):
                raise _Malformed
            prices_raw, trades_raw = sol.get("prices"), sol.get("trades")
            if not isinstance(prices_raw, dict) or not isinstance(trades_raw, list):
                raise _Malformed
            try:
                prices = {str(k).lower(): to_u256(v) for k, v in prices_raw.items()}
            except ValueError:
                invalid["bad_numeric"] += 1
                continue

            fulfills = {}
            for tr in trades_raw:
                if not isinstance(tr, dict):
                    reasons.add("malformed")
                    continue
                kf = tr.get("kind")
                if kf == "jit":
                    out["jit_ignored"] += 1
                    continue
                if kf != "fulfillment":
                    reasons.add("unknown_trade_kind")
                    continue
                uid = str(tr.get("order", "")).lower()
                if uid not in by_uid:
                    reasons.add("unknown_order")
                    continue
                if tr.get("executedAmount") is None:
                    reasons.add("missing_executed")
                    continue
                try:
                    fill = (to_u256(tr["executedAmount"]), to_u256(tr.get("fee", "0") or "0"))
                except ValueError:
                    reasons.add("bad_numeric")
                    continue
                fulfills.setdefault(uid, []).append(fill)

            total, scored = 0, 0
            pair_wei = defaultdict(int)
            for uid, fills in fulfills.items():
                if len(fills) > 1:
                    reasons.add("duplicate_trade")
                    continue
                executed, fee = fills[0]
                o = by_uid[uid]
                kind = o["kind"]
                pf = bool(o.get("partiallyFillable"))
                avail_sell, avail_buy = to_u256(o["sellAmount"]), to_u256(o["buyAmount"])
                limit_sell = to_u256(o.get("fullSellAmount", o["sellAmount"]))
                limit_buy = to_u256(o.get("fullBuyAmount", o["buyAmount"]))
                st, bt = o["sellToken"].lower(), o["buyToken"].lower()
                ps, pb = prices.get(st), prices.get(bt)
                if not ps or not pb:
                    reasons.add("missing_price")
                    continue
                if kind == "sell":
                    gross = executed + fee
                    if pf and gross > avail_sell:
                        reasons.add("overfill")
                        continue
                    if not pf and gross != avail_sell:
                        reasons.add("fok_mismatch")
                        continue
                    if _ceildiv(executed * ps, pb) < _ceildiv(gross * limit_buy, limit_sell):
                        reasons.add("limit_violation")
                        continue
                    s_atoms = scorer.gross_surplus_atoms("sell", gross, limit_sell, limit_buy, ps, pb)
                else:
                    if pf and executed > avail_buy:
                        reasons.add("overfill")
                        continue
                    if not pf and executed != avail_buy:
                        reasons.add("fok_mismatch")
                        continue
                    if executed * pb // ps + fee > executed * limit_sell // limit_buy:
                        reasons.add("limit_violation")
                        continue
                    s_atoms = scorer.gross_surplus_atoms("buy", executed, limit_sell, limit_buy, ps, pb)
                if s_atoms < 0:
                    reasons.add("limit_violation")
                    continue
                ref = ref_prices.get(scorer.surplus_token(kind, st, bt))
                if ref is None:
                    out["no_refprice"] += 1
                    continue
                wei = scorer.to_native(s_atoms, ref)
                total += wei
                pair_wei[(st, bt)] += wei
                scored += 1

            if reasons:
                for r in reasons:
                    invalid[r] += 1
                continue
            if scored == 0:
                continue
            out["n_valid"] += 1
            candidates.append((total, frozenset(pair_wei), dict(pair_wei)))
        except (_Malformed, TypeError, AttributeError, KeyError):
            invalid["malformed"] += 1
            continue

    candidates.sort(key=lambda c: c[0], reverse=True)
    taken, combined, by_pair = set(), 0, defaultdict(int)
    for s, prs, pw in candidates:
        if prs & taken:
            continue
        combined += s
        taken |= prs
        for k, v in pw.items():
            by_pair[k] += v
    out["best_single_wei"] = candidates[0][0] if candidates else 0
    out["best_surplus_wei"] = combined
    out["by_pair"] = {f"{a}|{b}": v for (a, b), v in by_pair.items()}
    out["invalid"] = dict(invalid)
    return out


def resolve_auction_by_tx(chain, tx, cache):
    """Propose (auction_id, solver_address) for a wrapper-routed settlement via
    the v2 by-tx-hash endpoint. The API only PROPOSES the id — scoring still
    requires uid overlap between the receipt's Trade events and the S3 auction
    body (the chain-side proof), preserving trustless attribution. Positive
    results are immutable and cached; a 404 (competition row not yet / no
    longer served) is returned as None and NOT cached."""
    if cache:
        hit = cache.get("aid-by-tx", tx)
        if hit is not None:
            return hit.get("aid"), (hit.get("solver") or "").lower() or None
    api = CHAINS[chain]["api"]
    url = f"{COW_API}/{api}/api/v2/solver_competition/by_tx_hash/{tx}"
    try:
        d = json.loads(_http_get(url, timeout=15))
    except Exception:
        return None, None
    aid = d.get("auctionId")
    if aid is None:
        return None, None
    solver = None
    for sol in d.get("solutions") or []:
        if sol.get("isWinner") and str(sol.get("txHash") or "").lower() == tx.lower():
            solver = (sol.get("solverAddress") or "").lower() or None
            break
    if cache:
        cache.put("aid-by-tx", tx, {"aid": int(aid), "solver": solver})
    return int(aid), solver


def verify_against_api(chain, auction_id, our_txs):
    """Cross-check the on-chain reconstruction against the v2 competition
    endpoint. Returns 'match' | 'mismatch' | 'unavailable'."""
    api = CHAINS[chain]["api"]
    url = f"{COW_API}/{api}/api/v2/solver_competition/{auction_id}"
    try:
        d = json.loads(_http_get(url, timeout=15))
    except Exception:
        return "unavailable"
    theirs = {str(h).lower() for h in (d.get("transactionHashes") or [])}
    if not theirs:
        return "unavailable"
    return "match" if theirs == {t.lower() for t in our_txs} else "mismatch"


# ------------------------------------------------------------------ pipeline

def process_window(args, rpcs, chain_cfg, frm, to, cache, jout):
    """Scan a block window end-to-end; returns the run-state dict consumed by print_scorecard/build_summary."""
    nat = chain_cfg["native"]
    say(f"[1/4] scanning {args.chain} settlements in blocks {frm}..{to} "
          f"({to - frm + 1} blocks, {'absolute' if args.from_block is not None else 'head-relative'})")
    txs, failed_ranges = enumerate_settlements(rpcs, frm, to)
    say(f"      found {len(txs)} settlement txs")
    if failed_ranges:
        miss = sum(hi - lo + 1 for lo, hi in failed_ranges)
        say(f"      ⚠ {len(failed_ranges)} block range(s) ({miss} blocks) FAILED to scan "
              f"(RPC getLogs); results under-count the field by those blocks.")

    max_cacheable = to - REORG_MARGIN
    skip = Counter()
    auctions = defaultdict(list)
    interrupted = False

    say(f"[2/4] decoding settlements (concurrency {args.workers}) + grouping by auction "
          f"(CIP-67: one auction can settle via several winning txs)")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            fetched = list(pool.map(
                lambda t: (t, fetch_settlement_cached(rpcs, t, cache, max_cacheable)), txs))
        for tx, s in fetched:
            if not s:
                skip["rpc_fetch_failed"] += 1
                continue
            if s["status"] != "0x1":
                skip["settlement_reverted"] += 1
                continue
            if s["calldata"][:10].lower() != scorer.SETTLE_SELECTOR:
                # Wrapper entrypoint: the inner settle() calldata is hidden,
                # but the GPv2 Trade events are on the receipt and the v2
                # by-tx-hash endpoint proposes the auction id (proven later by
                # uid overlap with the S3 body). Dropping these deleted ~40%
                # of the Base/mainnet field — and the biggest solvers with it.
                events = scorer.trade_events(s["logs"])
                if not events:
                    skip["non_settle_entrypoint"] += 1
                    continue
                aid, solver = resolve_auction_by_tx(args.chain, tx, cache)
                if aid is None:
                    skip["wrapper_unattributed"] += 1
                    continue
                auctions[aid].append(
                    {"tx": tx, "from": solver or s.get("to") or s["from"],
                     "block": s["block"], "dec": None, "events": events,
                     "entry": "wrapper"})
                continue
            try:
                dec = scorer.decode_settlement(s["calldata"])
            except Exception:
                skip["decode_failed"] += 1
                continue
            if dec["auction_id"] is None:
                skip["no_auction_id_suffix"] += 1
                continue
            events = scorer.trade_events(s["logs"])
            if len(events) != len(dec["trades"]):
                skip["trade_event_mismatch"] += 1
                continue
            auctions[dec["auction_id"]].append(
                {"tx": tx, "from": s["from"], "block": s["block"], "dec": dec, "events": events})
    except KeyboardInterrupt:
        interrupted = True
        say("\n      ⚠ interrupted — continuing with partial data")

    aids = sorted(auctions)
    n_found = len(aids)
    if args.max_auctions and n_found > args.max_auctions:
        # Keep the NEWEST auctions: replay uses live liquidity, so freshness
        # is the whole game — the old head-of-list cap kept the stalest.
        aids = aids[-args.max_auctions:]
        say(f"      {n_found} auctions found; capped to {len(aids)} newest "
              f"(per --max-auctions; NOT a full-field sample)")
    else:
        say(f"      {n_found} auctions from {len(txs)} settlement txs")

    # prefetch bodies concurrently (immutable and cached, so cheap on re-runs)
    say("[3/4] fetching auction bodies + winner baselines"
          + (f" + replaying {len(args.solvers)} solver(s)" if args.solvers else ""))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        bodies = dict(zip(aids, pool.map(
            lambda a: s3_auction(args.env, args.chain, a, cache), aids), strict=True))

    rows = []
    field_by_bucket = defaultdict(lambda: [0, 0])
    pair_stats = defaultdict(lambda: {"trades": 0, "winner_wei": 0,
                                      "solvers": defaultdict(int), "label": None})
    submitters = defaultdict(lambda: {"settlements": 0, "surplus_wei": 0})
    total_fees = 0
    agg = Counter()
    ages = []
    ts_mem = {}
    api_check = Counter()
    per_solver = {s["name"]: {"replayed": 0, "errored": 0, "returned": 0, "valid": 0,
                              "positive": 0, "beat": 0, "our_surplus": 0,
                              "winner_surplus": 0, "implausible": 0,
                              "invalid": Counter(), "errors": Counter(),
                              "latency": [], "late": 0}
                  for s in args.solvers}
    now_ts = time.time()
    done = 0

    try:
        for idx, aid in enumerate(aids):
            txrecs = auctions[aid]
            body = bodies.get(aid)
            if not body:
                skip["s3_body_missing"] += 1
                continue
            ref_prices = {k.lower(): int(v["referencePrice"])
                          for k, v in body.get("tokens", {}).items()
                          if v.get("referencePrice") is not None}
            jit_owners = {a.lower() for a in body.get("surplusCapturingJitOrderOwners", []) or []}

            by_uid = {o["uid"].lower() for o in body.get("orders", [])}
            any_overlap = any(ev["uid"] in by_uid for r in txrecs for ev in r["events"])
            all_jit = bool(jit_owners) and all(
                ev["owner"] in jit_owners for r in txrecs for ev in r["events"])
            if not any_overlap and not all_jit:
                skip["body_uid_mismatch"] += 1
                continue

            winner_total = winner_fees = 0
            winner_txs, per_trades = [], []
            for r in txrecs:
                if r.get("dec") is None:
                    # wrapper-routed: events-only scoring (delivered basis)
                    w = scorer.wrapper_settlement_surplus(r["events"], body, ref_prices)
                else:
                    w = scorer.winner_settlement_surplus(r["dec"], r["events"], body, ref_prices)
                if "error" in w:
                    skip[w["error"]] += 1
                    continue
                winner_total += w["total_surplus_wei"]
                winner_fees += w["total_fee_wei"]
                agg["jit_excluded"] += w["jit_excluded"]
                agg["no_refprice"] += w["skipped_no_refprice"]
                agg["anomalies"] += w.get("anomalies", 0)
                per_trades.extend(w["per_trade"])
                winner_txs.append({"tx": r["tx"], "block": r["block"], "submitter": r["from"],
                                   "entry": r.get("entry", "direct"),
                                   "surplus_wei": w["total_surplus_wei"],
                                   "fee_wei": w["total_fee_wei"], "trades": len(w["per_trade"])})
                sub = submitters[r["from"]]
                sub["settlements"] += 1
                sub["surplus_wei"] += w["total_surplus_wei"]
            if not winner_txs:
                continue

            native_usd = derive_native_usd(body, args.chain)
            for pt in per_trades:
                if pt["surplus_wei"] is None:
                    continue
                fb = field_by_bucket[size_bucket(trade_usd(pt, ref_prices, native_usd))]
                fb[0] += 1
                fb[1] += pt["surplus_wei"]
                key = f"{pt['sell']}|{pt['buy']}"
                ps = pair_stats[key]
                ps["trades"] += 1
                ps["winner_wei"] += pt["surplus_wei"]
                if ps["label"] is None:
                    ps["label"] = f"{token_symbol(body, pt['sell'])}->{token_symbol(body, pt['buy'])}"
            total_fees += winner_fees

            blk = max(r["block"] for r in txrecs)
            ts = block_ts(rpcs, blk, ts_mem, cache, max_cacheable)
            age_h = (now_ts - ts) / 3600 if ts is not None else None
            if age_h is not None:
                ages.append(age_h)

            row = {"v": VERSION, "chain": args.chain, "env": args.env, "auction_id": aid,
                   "block": blk, "block_ts": ts,
                   "age_hours": round(age_h, 2) if age_h is not None else None,
                   "winner_txs": winner_txs, "winner_surplus_wei": winner_total,
                   "winner_fee_wei": winner_fees}

            if args.verify_api:
                v = verify_against_api(args.chain, aid, [t["tx"] for t in winner_txs])
                api_check[v] += 1
                row["api_check"] = v

            if args.solvers:
                expired = sum(1 for o in body.get("orders", [])
                              if int(o.get("validTo", 0)) < now_ts)
                n_orders = max(len(body.get("orders", [])), 1)
                row["expired_orders_pct"] = round(100 * expired / n_orders, 1)
                if args.clamp_validto and expired:
                    horizon = int(now_ts) + args.solve_timeout + 600
                    for o in body.get("orders", []):
                        if int(o.get("validTo", 0)) < now_ts:
                            o["validTo"] = horizon
                    row["validto_clamped"] = expired

                row["solvers"] = {}
                # rotate which solver goes first so neither gets a systematic
                # first-mover advantage from chain state drifting between calls
                order = args.solvers[idx % len(args.solvers):] + args.solvers[:idx % len(args.solvers)]
                for sv in order:
                    body["deadline"] = (datetime.now(timezone.utc)
                                        + timedelta(seconds=args.solve_timeout)
                                        ).isoformat().replace("+00:00", "Z")
                    resp, err, ms = solve(sv["url"], body, args.solve_timeout)
                    st = per_solver[sv["name"]]
                    st["latency"].append(ms)
                    if ms > args.solve_timeout * 1000:
                        st["late"] += 1
                    if err:
                        st["errored"] += 1
                        st["errors"][err] += 1
                        row["solvers"][sv["name"]] = {"solve_error": err, "latency_ms": ms}
                        continue
                    try:
                        vs = validate_and_score(resp, body, ref_prices)
                    except Exception as e:
                        st["errored"] += 1
                        st["errors"]["bad_solver_response"] += 1
                        row["solvers"][sv["name"]] = {
                            "solve_error": f"bad_solver_response ({type(e).__name__})",
                            "latency_ms": ms}
                        continue
                    st["replayed"] += 1
                    st["winner_surplus"] += winner_total
                    ours = vs["best_surplus_wei"]
                    st["our_surplus"] += ours
                    if vs["n_solutions"]:
                        st["returned"] += 1
                    if vs["n_valid"]:
                        st["valid"] += 1
                    if ours > 0:
                        st["positive"] += 1
                    if ours > winner_total:
                        st["beat"] += 1
                    for k, v in vs["invalid"].items():
                        st["invalid"][k] += v
                    for pk, pv in vs["by_pair"].items():
                        pair_stats[pk]["solvers"][sv["name"]] += pv
                        if pair_stats[pk]["label"] is None:
                            a, b = pk.split("|")
                            pair_stats[pk]["label"] = f"{token_symbol(body, a)}->{token_symbol(body, b)}"
                    implausible = (ours > 10 * winner_total if winner_total > 0 else ours > 10**15)
                    if implausible:
                        st["implausible"] += 1
                    vs_row = {k: vs[k] for k in ("best_surplus_wei", "best_single_wei",
                                                 "n_solutions", "n_valid", "invalid")}
                    vs_row["latency_ms"] = ms
                    if implausible:
                        vs_row["flags"] = ["implausible_surplus"]
                    row["solvers"][sv["name"]] = vs_row

            rows.append(row)
            if jout:
                jout.write(json.dumps(row) + "\n")
                jout.flush()
            done += 1
            if done % 25 == 0:
                say(f"      {done}/{len(aids)} auctions")
    except KeyboardInterrupt:
        interrupted = True
        say("\n      ⚠ interrupted — continuing to scorecard with partial data")

    return {"rows": rows, "txs": txs, "n_found": n_found, "skip": skip,
            "failed_ranges": failed_ranges, "field_by_bucket": field_by_bucket,
            "pair_stats": pair_stats, "submitters": submitters, "total_fees": total_fees,
            "agg": agg, "ages": ages, "per_solver": per_solver, "api_check": api_check,
            "interrupted": interrupted, "from_block": frm, "to_block": to, "native": nat}


# ---------------------------------------------------------------- reporting

def _pct(a, b):
    return None if not b else 100 * a / b


def readiness_report(st, args):
    """A one-screen pre-production readiness check for a single solver endpoint:
    did it answer, how fast, and were its solutions sane against the winners?
    Reuses the counterfactual pass's per-solver stats — no extra fetching.
    Returns a JSON-able dict; also prints the report unless --quiet."""
    reports = []
    for sv in args.solvers:
        s = st["per_solver"][sv["name"]]
        attempted = s["replayed"] + s["errored"]
        lat = sorted(s["latency"])
        p50 = lat[len(lat) // 2] if lat else None
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
        pmax = lat[-1] if lat else None
        answer_rate = _pct(s["returned"], attempted)
        valid_rate = _pct(s["valid"], s["replayed"])
        capture = _pct(s["our_surplus"], s["winner_surplus"])
        # A conservative pass/warn read for a first-time integrator.
        checks = []

        def chk(ok, warn, label, detail, _out=checks):
            _out.append({"level": "ok" if ok else ("warn" if warn else "fail"),
                         "label": label, "detail": detail})
        chk(attempted > 0, False, "reached auctions",
            f"{attempted} auctions attempted")
        if attempted:
            chk(s["errored"] == 0, s["errored"] < attempted, "no transport errors",
                f"{s['errored']}/{attempted} errored" if s["errored"] else "0 errors")
            chk((answer_rate or 0) >= 90, (answer_rate or 0) >= 50, "answers reliably",
                f"{answer_rate:.0f}% returned a solution" if answer_rate is not None else "n/a")
            chk(s["late"] == 0, s["late"] * 5 <= attempted, "inside the deadline",
                f"{s['late']} past the advertised deadline"
                if s["late"] else "0 past deadline")
            if p95 is not None:
                budget = args.solve_timeout * 1000
                chk(p95 <= budget * 0.5, p95 <= budget, "latency headroom",
                    f"p95 {p95} ms of a {budget} ms budget")
            if s["replayed"]:
                chk((valid_rate or 0) >= 90, (valid_rate or 0) >= 50, "solutions are valid",
                    f"{valid_rate:.0f}% passed limit/fee checks" if valid_rate is not None else "n/a")
                chk((capture or 0) >= 50, (capture or 0) > 0, "competitive vs winners",
                    f"{capture:.0f}% of winner surplus captured" if capture is not None else "n/a")
            if s["implausible"]:
                chk(False, True, "prices look plausible",
                    f"{s['implausible']} auction(s) flagged implausible_surplus")
            # Coverage disclosure: a verdict against a partial field is only
            # meaningful if the reader can see how partial. Never a hard fail
            # (it's environmental, not the solver's doing) — but >5% excluded
            # downgrades to REVIEW so the number gets read.
            found = len(st.get("txs") or [])
            if found:
                sk = st.get("skip") or {}
                excl = (sk.get("wrapper_unattributed", 0)
                        + sk.get("non_settle_entrypoint", 0))
                chk(excl <= 0.05 * found, True, "field coverage",
                    f"{found} settlements, {st.get('n_found', 0)} auctions formed, "
                    f"{excl} unattributed ({100 * excl / found:.0f}% of field excluded)")
        verdict = ("READY" if all(c["level"] == "ok" for c in checks)
                   else "NOT READY" if any(c["level"] == "fail" for c in checks)
                   else "REVIEW")
        rep = {
            "solver": sv["name"], "url": sv["url"], "verdict": verdict,
            "auctions_attempted": attempted, "answered": s["returned"],
            "answer_rate_pct": answer_rate, "errored": s["errored"],
            "valid_rate_pct": valid_rate, "capture_pct": capture,
            "p50_ms": p50, "p95_ms": p95, "max_ms": pmax,
            "past_deadline": s["late"], "solve_timeout_s": args.solve_timeout,
            "implausible": s["implausible"], "checks": checks,
            "errors": dict(s["errors"]),
        }
        reports.append(rep)
        if args.quiet:
            continue
        mark = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}
        print()
        print("=" * 68)
        print(f"  READINESS — {sv['name']}   [{verdict}]")
        print(f"  {args.chain} · {args.env} · blocks {st['from_block']}..{st['to_block']}")
        print("=" * 68)
        for c in checks:
            print(f"  [{mark[c['level']]}] {c['label']:<24} {c['detail']}")
        print("  " + "-" * 64)
        print(f"  answered            : {s['returned']}/{attempted}"
              + (f"  ({answer_rate:.0f}%)" if answer_rate is not None else ""))
        if p50 is not None:
            print(f"  latency             : p50 {p50} ms / p95 {p95} ms / max {pmax} ms"
                  f"   (budget {args.solve_timeout * 1000} ms)")
        if capture is not None:
            print(f"  surplus vs winners  : {capture:.0f}% captured"
                  f"   ({s['valid']}/{s['replayed']} valid)")
        if s["errors"]:
            print(f"  errors              : {dict(s['errors'])}")
        print()
        print("  Reproduce this run:")
        blk = f"--from-block {st['from_block']} --to-block {st['to_block']}"
        print(f"    cow-backtester --chain {args.chain} --env {args.env} {blk} \\")
        print(f"        --rpc-url <rpc> --solver-url {sv['url']} --solver-name {sv['name']} --readiness")
    if not args.quiet:
        print()
        print("  Note: replays live liquidity against archived auctions — a readiness")
        print("  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.")
    return reports


def print_scorecard(st, args, cache, solver_names_map=None):
    nat = st["native"]
    rows = st["rows"]
    say("[4/4] scorecard")
    if st["interrupted"]:
        print("(PARTIAL RUN — interrupted)\n")
    print("=" * 68)
    print(f"  COVERAGE  ({args.chain}, {args.env}, blocks {st['from_block']}..{st['to_block']})")
    print("=" * 68)
    print(f"  settlement txs found  : {len(st['txs'])}")
    print(f"  auctions formed       : {st['n_found']} (scored {len(rows)})")
    if st["skip"]:
        print(f"  skipped               : {sum(st['skip'].values())}")
        for reason, n in st["skip"].most_common():
            print(f"      {reason:>24} : {n}")
    a = st["agg"]
    if any(a.values()):
        print(f"  per-trade exclusions  : jit_excluded {a['jit_excluded']} | "
              f"no_refprice {a['no_refprice']} | uniform_anomalies {a['anomalies']}")
    if st["failed_ranges"]:
        print(f"  ⚠ unscanned block span : "
              f"{sum(hi - lo + 1 for lo, hi in st['failed_ranges'])} blocks (RPC getLogs failures)")
    if st["ages"]:
        ages = st["ages"]
        print(f"  auction age (hours)   : min {min(ages):.1f} / median {statistics.median(ages):.1f} / max {max(ages):.1f}")
        if args.solvers and max(ages) > args.max_age_hours:
            print(f"  ⚠ some auctions exceed --max-age-hours={args.max_age_hours:.0f}: replay uses LIVE")
            print("    liquidity, so older auctions make the counterfactual less indicative.")
    if st["api_check"]:
        c = st["api_check"]
        line = f"  v2 API cross-check    : match {c['match']} / mismatch {c['mismatch']} / unavailable {c['unavailable']}"
        print(line + ("   ⚠ MISMATCHES — investigate" if c["mismatch"] else ""))
    if cache and cache.enabled:
        cs = cache.stats()
        print(f"  cache                 : {cs['hits']} hits / {cs['misses']} misses / {cs['writes']} writes")

    print()
    print("=" * 68)
    print(f"  FIELD SURPLUS (before-fee, signed limits) — {len(rows)} auctions")
    print("=" * 68)
    print(f"  {'size bucket':>12} {'trades':>8} {'surplus (' + nat + ')':>18}")
    for _lo, _hi, label in SIZE_BUCKETS + [(0, 0, "unknown")]:
        n, surp = st["field_by_bucket"].get(label, [0, 0])
        if n:
            print(f"  {label:>12} {n:>8,} {surp / 1e18:>18.6f}")
    tot_n = sum(v[0] for v in st["field_by_bucket"].values())
    tot_s = sum(v[1] for v in st["field_by_bucket"].values())
    print(f"  {'TOTAL':>12} {tot_n:>8,} {tot_s / 1e18:>18.6f}")
    print(f"  (winners' fee take alongside: {st['total_fees'] / 1e18:.6f} {nat})")

    # winning submitters
    subs = sorted(st["submitters"].items(), key=lambda kv: -kv[1]["surplus_wei"])[:10]
    if subs:
        print()
        print("=" * 68)
        print(f"  WINNING SUBMITTERS (top {len(subs)})")
        print("=" * 68)
        print(f"  {'submitter':>44} {'settles':>8} {'surplus (' + nat + ')':>18}")
        for addr, v in subs:
            label = (solver_names_map or {}).get(addr, addr)
            print(f"  {label[:44]:>44} {v['settlements']:>8,} {v['surplus_wei'] / 1e18:>18.6f}")

    # per-solver counterfactual
    for sv in args.solvers:
        s = st["per_solver"][sv["name"]]
        print()
        print("=" * 68)
        print(f"  COUNTERFACTUAL — {sv['name']}  ({s['replayed']} replayed"
              + (f", {s['errored']} errored/EXCLUDED" if s["errored"] else "") + ")")
        print("=" * 68)
        if s["replayed"] == 0:
            print("  ⚠ SOLVER NEVER ANSWERED — scorecard void.")
            for k, v in s["errors"].most_common():
                print(f"      {k:>24} : {v}")
            continue
        print(f"  returned a solution : {s['returned']}/{s['replayed']}")
        print(f"  valid solutions     : {s['valid']}/{s['replayed']}")
        print(f"  positive surplus    : {s['positive']}/{s['replayed']}")
        print(f"  beat the winning set: {s['beat']}/{s['replayed']}")
        print(f"  our surplus (sum)   : {s['our_surplus'] / 1e18:.6f} {nat}")
        print(f"  winners (sum)       : {s['winner_surplus'] / 1e18:.6f} {nat}  (replayed auctions only)")
        cap = _pct(s["our_surplus"], s["winner_surplus"])
        if cap is not None:
            print(f"  capture ratio       : {cap:.1f}% of the winning set")
        if s["latency"]:
            lat = sorted(s["latency"])
            p50 = lat[len(lat) // 2]
            p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
            print(f"  solve latency       : p50 {p50} ms / p95 {p95} ms"
                  + (f"   ⚠ {s['late']} past the advertised deadline" if s["late"] else ""))
        if s["errors"]:
            print(f"  solve errors        : {dict(s['errors'])}  (excluded from sums)")
        if s["invalid"]:
            print(f"  invalid solutions   : {dict(s['invalid'])}  (excluded from scoring)")
        if s["implausible"]:
            print(f"  ⚠ {s['implausible']} auction(s) flagged implausible_surplus — prices are CLAIMED,")
            print("    not simulated; treat those rows as suspect.")

    # head-to-head
    h2h = None
    if len(args.solvers) == 2:
        n1, n2 = args.solvers[0]["name"], args.solvers[1]["name"]
        s1, s2 = st["per_solver"][n1], st["per_solver"][n2]
        print()
        print("=" * 68)
        print(f"  HEAD TO HEAD — {n1} vs {n2}")
        print("=" * 68)
        h2h_rows = []
        def line(label, v1, v2):
            print(f"  {label:>22} : {str(v1):>18} {str(v2):>18}")
            h2h_rows.append((label, [v1, v2]))
        line("replayed", s1["replayed"], s2["replayed"])
        line("returned", s1["returned"], s2["returned"])
        line("valid", s1["valid"], s2["valid"])
        line("beat winning set", s1["beat"], s2["beat"])
        line(f"surplus ({nat})", f"{s1['our_surplus'] / 1e18:.6f}", f"{s2['our_surplus'] / 1e18:.6f}")
        c1, c2 = _pct(s1["our_surplus"], s1["winner_surplus"]), _pct(s2["our_surplus"], s2["winner_surplus"])
        line("capture %", "n/a" if c1 is None else f"{c1:.1f}", "n/a" if c2 is None else f"{c2:.1f}")
        if s1["latency"] and s2["latency"]:
            line("latency p50 ms", sorted(s1["latency"])[len(s1["latency"]) // 2],
                 sorted(s2["latency"])[len(s2["latency"]) // 2])
        # per-auction verdicts on the auctions BOTH replayed successfully
        wins = {n1: 0, n2: 0, "tie": 0}
        for r in rows:
            sv = r.get("solvers") or {}
            a1, a2 = sv.get(n1), sv.get(n2)
            if not a1 or not a2 or "solve_error" in a1 or "solve_error" in a2:
                continue
            x, y = a1["best_surplus_wei"], a2["best_surplus_wei"]
            wins[n1 if x > y else n2 if y > x else "tie"] += 1
        print(f"  {'per-auction wins':>22} : {wins[n1]:>18} {wins[n2]:>18}   (ties {wins['tie']})")
        h2h_rows.append(("per-auction wins", [wins[n1], wins[n2]]))
        delta = s1["our_surplus"] - s2["our_surplus"]
        print(f"  → {n1} minus {n2}: {delta / 1e18:+.6f} {nat}")
        h2h = {"names": [n1, n2], "rows": h2h_rows}

    # pair breakdown
    pairs = sorted(st["pair_stats"].items(), key=lambda kv: -kv[1]["winner_wei"])[:12]
    pair_list = []
    if pairs:
        print()
        print("=" * 68)
        print(f"  TOP PAIRS BY WINNER SURPLUS (top {len(pairs)})")
        print("=" * 68)
        names = [s["name"] for s in args.solvers]
        hdr = f"  {'pair':>26} {'trades':>7} {'winner':>14}"
        for n in names:
            hdr += f" {n[:12]:>14}"
        print(hdr)
        for key, v in pairs:
            line = f"  {(v['label'] or key)[:26]:>26} {v['trades']:>7} {v['winner_wei'] / 1e18:>14.6f}"
            for n in names:
                line += f" {v['solvers'].get(n, 0) / 1e18:>14.6f}"
            print(line)
            pair_list.append({"pair": v["label"] or key, "trades": v["trades"],
                              "winner_wei": v["winner_wei"],
                              "solvers": {n: v["solvers"].get(n, 0) for n in names}})

    return {"head_to_head": h2h, "pairs": pair_list}


def build_summary(st, args, extra, solver_names_map=None):
    nat = st["native"]
    solvers = []
    for sv in args.solvers:
        s = st["per_solver"][sv["name"]]
        lat = sorted(s["latency"])
        solvers.append({
            "name": sv["name"], "replayed": s["replayed"], "errored": s["errored"],
            "returned": s["returned"], "valid": s["valid"], "positive": s["positive"],
            "beat": s["beat"], "our_surplus": s["our_surplus"],
            "capture_pct": _pct(s["our_surplus"], s["winner_surplus"]),
            "implausible": s["implausible"],
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
        })
    cov = {"settlement txs found": len(st["txs"]),
           "auctions formed": st["n_found"], "auctions scored": len(st["rows"])}
    for k, v in st["skip"].most_common():
        cov[f"skipped: {k}"] = v
    if st["failed_ranges"]:
        cov["unscanned blocks"] = sum(hi - lo + 1 for lo, hi in st["failed_ranges"])
    if st["ages"]:
        cov["auction age median (h)"] = round(statistics.median(st["ages"]), 1)
    if st["api_check"]:
        cov["v2 API match/mismatch/unavailable"] = (
            f"{st['api_check']['match']}/{st['api_check']['mismatch']}/{st['api_check']['unavailable']}")
    return {
        "version": VERSION, "chain": args.chain, "env": args.env, "native": nat,
        "from_block": st["from_block"], "to_block": st["to_block"],
        "coverage": cov,
        "field_surplus_wei": sum(v[1] for v in st["field_by_bucket"].values()),
        "field_fees_wei": st["total_fees"],
        "buckets": [{"label": lb, "trades": st["field_by_bucket"][lb][0],
                     "surplus_wei": st["field_by_bucket"][lb][1]}
                    for _, _, lb in SIZE_BUCKETS + [(0, 0, "unknown")]
                    if st["field_by_bucket"].get(lb, [0, 0])[0]],
        "solvers": solvers, "solver_names": [s["name"] for s in args.solvers],
        "head_to_head": extra.get("head_to_head"), "pairs": extra.get("pairs"),
        "readiness": extra.get("readiness"),
        "submitters": [{"address": a, "name": (solver_names_map or {}).get(a),
                        "settlements": v["settlements"], "surplus_wei": v["surplus_wei"]}
                       for a, v in sorted(st["submitters"].items(),
                                          key=lambda kv: -kv[1]["surplus_wei"])[:10]],
        "caveats": CAVEATS,
    }


# ---------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(
        prog="cow-backtester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="CoW historical-auction backtester + counterfactual scorecard",
        epilog="Exit codes: 0 success, 1 runtime error, 2 usage error, 130 interrupted "
               "(partial scorecard printed). Self-tests: python3 -m cow_backtester.scorer "
               "--unittest (offline) / --selftest, python3 -m cow_backtester --selftest (network).")
    ap.add_argument("--chain", default="arbitrum-one", choices=sorted(CHAINS),
                    help="chain to scan (make this explicit in scripts)")
    ap.add_argument("--env", default="prod", choices=["prod", "staging"],
                    help="CoW environment whose auctions to replay")
    ap.add_argument("--blocks", type=int, default=2000,
                    help="scan the most recent N blocks (head-relative)")
    ap.add_argument("--from-block", type=int, default=None, help="absolute scan start (reproducible)")
    ap.add_argument("--to-block", type=int, default=None, help="absolute scan end; omit to use the chain head")
    ap.add_argument("--max-auctions", type=int, default=25,
                    help="cap auctions scored, oldest first (0 = all)")
    ap.add_argument("--rpc-url", default=None, help="chain RPC (recommended)")
    ap.add_argument("--solver-url", action="append", default=[],
                    help="solver /solve endpoint; repeat for A/B")
    ap.add_argument("--solver-name", action="append", default=[],
                    help="label for the corresponding --solver-url")
    ap.add_argument("--solve-timeout", type=int, default=20,
                    help="deadline advertised to the solver, seconds (HTTP waits +5)")
    ap.add_argument("--workers", type=int, default=8, help="concurrent RPC/S3 fetches")
    ap.add_argument("--cache-dir", default=".cowbt-cache", help="cache directory")
    ap.add_argument("--no-cache", action="store_true", help="disable the cache")
    ap.add_argument("--json-out", default=None, help="stream one JSON row per auction")
    ap.add_argument("--html-out", default=None, help="write a single-file HTML report")
    ap.add_argument("--solver-map", default=None,
                    help="JSON {address: name} to label winning submitters")
    ap.add_argument("--readiness", action="store_true",
                    help="print a one-screen pre-prod readiness check for the "
                         "solver endpoint(s) instead of the full field scorecard "
                         "(answer rate, latency, validity, surplus vs winners)")
    ap.add_argument("--verify-api", action="store_true",
                    help="cross-check winner txs against the v2 competition API")
    ap.add_argument("--clamp-validto", action="store_true",
                    help="extend expired validTo so engines that filter them can still solve")
    ap.add_argument("--max-age-hours", type=float, default=6.0,
                    help="warn when replayed auctions are older than this")
    ap.add_argument("--watch", type=int, default=0,
                    help="continuous mode: rescan every N seconds from the last block")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress progress output (scorecard still prints to stdout)")
    ap.add_argument("--version", action="version", version=f"cow-backtester {VERSION}")
    args = ap.parse_args()

    global QUIET
    QUIET = args.quiet

    if args.blocks <= 0:
        ap.error("--blocks must be > 0")
    if args.max_auctions < 0:
        ap.error("--max-auctions must be >= 0")
    if args.solve_timeout < 3:
        ap.error("--solve-timeout must be >= 3 seconds")
    if args.workers < 1:
        ap.error("--workers must be >= 1")
    if args.to_block is not None and args.from_block is None:
        ap.error("--to-block requires --from-block")
    if (args.from_block is not None and args.to_block is not None
            and args.from_block > args.to_block):
        ap.error("--from-block must be <= --to-block")
    if len(args.solver_name) > len(args.solver_url):
        ap.error("more --solver-name than --solver-url")
    if args.watch and args.from_block is not None:
        ap.error("--watch is incompatible with a fixed --from-block window")

    names = list(args.solver_name) + [
        f"solver{i + 1}" for i in range(len(args.solver_name), len(args.solver_url))]
    if len(set(names)) != len(names):
        ap.error("--solver-name values must be unique")
    args.solvers = [{"url": u, "name": n} for u, n in zip(args.solver_url, names, strict=True)]

    solver_names_map = None
    if args.solver_map:
        try:
            solver_names_map = {k.lower(): v for k, v in json.load(open(args.solver_map)).items()}
        except Exception as e:
            print(f"ERROR: cannot read --solver-map {args.solver_map}: {e}", file=sys.stderr)
            sys.exit(1)

    jout = None
    if args.json_out:
        try:
            jout = open(args.json_out, "w")
        except OSError as e:
            print(f"ERROR: cannot write --json-out {args.json_out}: {e}", file=sys.stderr)
            sys.exit(1)
    if args.html_out:
        try:
            d = os.path.dirname(os.path.abspath(args.html_out))
            if not os.path.isdir(d):
                raise OSError(f"no such directory: {d}")
        except OSError as e:
            print(f"ERROR: cannot write --html-out {args.html_out}: {e}", file=sys.stderr)
            sys.exit(1)

    cfg = CHAINS[args.chain]
    rpcs = [args.rpc_url] if args.rpc_url else cfg["rpcs"]

    cid = scorer.rpc(rpcs, "eth_chainId", [])
    if cid is None:
        print("ERROR: RPC unreachable", file=sys.stderr)
        sys.exit(1)
    if int(cid, 16) != cfg["id"]:
        print(f"ERROR: RPC reports chainId {int(cid, 16)} but --chain {args.chain} "
              f"expects {cfg['id']} — wrong --rpc-url?", file=sys.stderr)
        sys.exit(1)

    for sv in args.solvers:
        say(f"[0/4] preflight: probing {sv['name']} at {sv['url']} ...")
        ok, why = preflight(sv["url"])
        if not ok:
            print(f"ERROR: solver {sv['name']} ({sv['url']}): {why}", file=sys.stderr)
            sys.exit(1)
        say("      reachable")

    cache = Cache(args.cache_dir, args.chain, enabled=not args.no_cache)

    cycle = 0
    last_to = None
    was_interrupted = False
    while True:
        if args.from_block is not None:
            frm = max(0, args.from_block)
            to = args.to_block
            if to is None:
                head = scorer.rpc(rpcs, "eth_blockNumber", [])
                if head is None:
                    print("ERROR: RPC unreachable", file=sys.stderr)
                    sys.exit(1)
                to = int(head, 16)
        else:
            head = scorer.rpc(rpcs, "eth_blockNumber", [])
            if head is None:
                print("ERROR: RPC unreachable", file=sys.stderr)
                sys.exit(1)
            to = int(head, 16)
            frm = (last_to + 1) if last_to is not None else max(0, to - args.blocks + 1)
            if frm > to:
                time.sleep(args.watch)
                continue

        if cycle:
            say("\n" + "#" * 68 + f"\n# watch cycle {cycle}\n" + "#" * 68)
        st = process_window(args, rpcs, cfg, frm, to, cache, jout)
        was_interrupted = was_interrupted or st["interrupted"]
        if args.readiness and args.solvers:
            readiness = readiness_report(st, args)
            extra = {"readiness": readiness}
        else:
            if args.readiness:
                say("  --readiness needs at least one --solver-url; "
                    "printing the field scorecard instead.")
            extra = print_scorecard(st, args, cache, solver_names_map)

        if args.html_out:
            from . import report
            summary = build_summary(st, args, extra, solver_names_map)
            report.render(summary, st["rows"], args.html_out)
            say(f"  wrote HTML report -> {args.html_out}")
        if jout:
            # One _meta line per window so pipelines inherit coverage + the
            # caveats instead of silently trusting a partial field.
            jout.write(json.dumps({"_meta": {
                "v": VERSION, "chain": args.chain, "env": args.env,
                "from_block": st["from_block"], "to_block": st["to_block"],
                "settlement_txs_found": len(st["txs"]),
                "auctions_formed": st["n_found"], "rows": len(st["rows"]),
                "skipped": dict(st["skip"]), "caveats": CAVEATS}}) + "\n")
            jout.flush()
            say(f"  wrote {len(st['rows'])} rows + 1 _meta line -> {args.json_out}")

        last_to = to
        cycle += 1
        if not args.watch:
            break
        say(f"\n  sleeping {args.watch}s (watch mode; Ctrl-C to stop)")
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            say("\nstopped")
            break

    if jout:
        jout.close()
    if was_interrupted:
        sys.exit(130)


def cli():
    """Console entry point: exit cleanly (130) on Ctrl-C instead of dumping a traceback."""
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)


def selftest():
    """Network self-test of the counterfactual path, no live solver needed.
    Guarantees: exact winner reproduction; duplicate trades, fee padding,
    negative amounts, and malformed shapes are rejected; pair-colliding
    solutions never double-count."""
    rpcs = CHAINS["arbitrum-one"]["rpcs"]
    tx = "0xc103e7f2f31f6c302de778dda0fc5c10c8ec27e638b97fb47442235b305075cc"
    s = scorer.fetch_settlement(rpcs, tx)
    assert s, "RPC unreachable"
    dec = scorer.decode_settlement(s["calldata"])
    ev = scorer.trade_events(s["logs"])
    body = s3_auction("prod", "arbitrum-one", dec["auction_id"])
    assert body, "S3 body missing"
    refp = {k.lower(): int(v["referencePrice"]) for k, v in body["tokens"].items()
            if v.get("referencePrice") is not None}
    w = scorer.winner_settlement_surplus(dec, ev, body, refp)
    assert "error" not in w

    toks, pr = dec["tokens"], dec["clearing_prices"]
    tr = dec["trades"][0]
    st_, bt_ = toks[tr[0]], toks[tr[1]]
    u_si, u_bi = scorer.uniform_price_indices(toks, st_, bt_)
    executed = tr[9]
    uid = ev[0]["uid"]

    def sol(ps, pb, uid_, ex, fee="0"):
        return {"id": 0, "prices": {st_: ps, bt_: pb},
                "trades": [{"kind": "fulfillment", "order": uid_,
                            "executedAmount": ex, "fee": fee}],
                "interactions": [], "gas": 150000}

    vs = validate_and_score({"solutions": [sol(str(pr[u_si]), str(pr[u_bi]), uid, str(executed))]}, body, refp)
    assert vs["n_valid"] == 1 and vs["best_surplus_wei"] == w["total_surplus_wei"], vs
    assert sum(vs["by_pair"].values()) == w["total_surplus_wei"], vs["by_pair"]

    vs2 = validate_and_score({"solutions": [sol(hex(pr[u_si]), hex(pr[u_bi]), uid.upper(), hex(executed))]}, body, refp)
    assert vs2["best_surplus_wei"] == w["total_surplus_wei"], vs2

    vs3 = validate_and_score({"solutions": [sol(str(pr[u_bi] // 2), str(pr[u_bi]), uid, str(executed))]}, body, refp)
    assert vs3["n_valid"] == 0 and vs3["invalid"].get("limit_violation") == 1, vs3

    dup = sol(str(pr[u_si]), str(pr[u_bi]), uid, str(executed))
    dup["trades"] = dup["trades"] * 3
    vs4 = validate_and_score({"solutions": [dup]}, body, refp)
    assert vs4["n_valid"] == 0 and vs4["invalid"].get("duplicate_trade") == 1, vs4

    o = next(o for o in body["orders"] if o["uid"].lower() == uid)
    avail = int(o["sellAmount"])
    vs5 = validate_and_score({"solutions": [sol(str(pr[u_si]), str(pr[u_bi]), uid, "1", str(avail - 1))]}, body, refp)
    assert vs5["n_valid"] == 0 and vs5["invalid"].get("limit_violation") == 1, vs5

    vs6 = validate_and_score({"solutions": [sol(str(pr[u_si]), str(pr[u_bi]), uid, "-5")]}, body, refp)
    assert vs6["n_valid"] == 0 and vs6["invalid"].get("bad_numeric") == 1, vs6

    for junk in ([{"prices": {}, "trades": None}], ["garbage"], [{"prices": [], "trades": []}],
                 [{"prices": {st_: "1"}, "trades": ["x"]}], [None]):
        assert validate_and_score({"solutions": junk}, body, refp)["n_valid"] == 0, junk

    two = {"solutions": [sol(str(pr[u_si]), str(pr[u_bi]), uid, str(executed)),
                         sol(str(pr[u_si]), str(pr[u_bi]), uid, str(executed))]}
    vs8 = validate_and_score(two, body, refp)
    assert vs8["n_valid"] == 2 and vs8["best_surplus_wei"] == w["total_surplus_wei"], vs8

    # native/USD derivation from the auction's own reference prices
    rate = derive_native_usd(body, "arbitrum-one")
    assert rate and 100 < rate < 100_000, rate

    print(f"SELFTEST PASS: counterfactual == winner baseline exactly "
          f"({w['total_surplus_wei']} wei, auction {dec['auction_id']}); by_pair consistent; "
          f"hex/case OK; limit-violation, duplicate-trade, fee-padding, negative amounts all "
          f"INVALID; malformed shapes never crash; pair-collision no double-count; "
          f"native/USD derived {rate:.0f}.")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        cli()
