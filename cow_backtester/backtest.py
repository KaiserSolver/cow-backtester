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
chain's native token at the auction's referencePrice. Measured against the v2
competition API's official `score` this basis agrees to within 0.2% (7 live
records, Arbitrum + Base, 2026-09-03): the uniform-vs-custom price wedge IS the
protocol fee, which the CIP-38 score adds back. It overstates the score only by
a solver-determined fee (zero on chains where the driver bakes fees into the
custom prices) and deviates on buy orders by the limit/market gap in the native
conversion (documented in README).

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
import hashlib
import io
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

from . import competition, economics, scorer
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
    "Surplus basis = before-fee surplus over signed limits at uniform prices. Measured "
    "within 0.2% of CoW's official CIP-38 score on live records (the uniform-vs-custom wedge "
    "is the protocol fee the score adds back); it overstates the score only by a "
    "solver-determined fee, and values buy-order surplus at the sell token's reference "
    "price where the score uses the limit ratio into the buy token.",
    "Beating the winning set is necessary, not sufficient: real winner selection also applies "
    "fairness filters and bids score net of gas.",
    "Solver prices are CLAIMED, not simulated. Feasibility is checked; routes are not executed.",
    "Wrapper-routed settlements (solver router entrypoints; measured Aug 2026: ~43% of mainnet, "
    "~40% of Base, ~2% of Arbitrum settlements, and larger than direct ones at the median) are "
    "attributed via the v2 by-tx-hash endpoint and PROVEN by uid overlap with the S3 body, then "
    "scored on a DELIVERED basis (Trade events vs signed limits) which understates the direct "
    "before-fee basis by the fee wedge. Unresolvable ones are reported under wrapper_unattributed.",
]

# ----------------------------------------------------------- readiness tables
#
# Every numeric choice the readiness verdict rests on lives in one of the
# tables below (v0.11.0), never in a literal inside readiness_report(), so it
# can be changed without a code edit and every run prints the values it used.

# Observed per-request budget the driver actually gives a solver, seconds,
# settle lane p50 from the 2026-09-14 engine-log audit (readiness-inputs-report
# G.1: Arbitrum 4.838 s over 94,575 requests, Base 4.615 s over 60,191, BNB
# 2.353 s over 138). None = not observed on that chain; --readiness then
# refuses to run without an explicit --solve-timeout (an ASSUMED budget must
# never produce a verdict). Quote-lane budgets (~2.8 s) are not replayed here.
BUDGETS_S = {
    "arbitrum-one": {"settle": 4.84},
    "base":         {"settle": 4.62},
    "bnb":          {"settle": 2.35},
    "mainnet":      {"settle": None},
    "xdai":         {"settle": None},
    "polygon":      {"settle": None},
    "avalanche":    {"settle": None},
    "linea":        {"settle": None},
    "ink":          {"settle": None},
    "plasma":       {"settle": None},
}
# Budget a plain (non-readiness) replay falls back to on a chain with no
# observed value and no override — labeled `assumed` everywhere it appears.
ASSUMED_BUDGET_S = 20.0
# Extra HTTP wait past the advertised budget so a slow solver is OBSERVED
# (and counted as a deadline miss), not cut off by the client.
HTTP_GRACE_S = 5.0

# Verdict thresholds. `default` applies to every chain; a chain key holds only
# the overrides for that chain (v0 ships none — the mechanism is exercised by
# tests). Rates are percentages of attempted auctions unless named otherwise.
THRESHOLDS = {
    "default": {
        "answer_rate_pass": 90.0, "answer_rate_warn": 50.0,   # replayed / attempted
        "transport_warn": 1.0, "transport_fail": 1.0,         # PASS = 0; WARN <= warn%; FAIL > fail%
        "deadline_miss_warn": 1.0, "deadline_miss_fail": 1.0,  # same shape
        "latency_pass_frac": 0.5, "latency_warn_frac": 1.0,   # p95 as a fraction of the budget
        "validity_pass": 90.0, "validity_warn": 50.0,         # valid / returned
        "capture_pass": 50.0, "capture_warn": 0.0,            # PASS >= pass; WARN > warn
        "bid_coverage_pass": 50.0,                            # returned / replayed (warn-only)
        "field_coverage_excluded_max_frac": 0.05,             # excluded / settlements found
        "min_evidence": 500,                                  # attempted auctions for READY
    },
}

# Winner-surplus sanity check (v0.11.1). A decoded winner surplus larger than
# ARTEFACT_RATIO x the winner surplus of EVERY OTHER attempted auction in the
# window combined is a valuation artefact of the auction's reference prices,
# not delivered value — observed on BNB auction 25459284, a 2 USDC -> MCH sell
# order whose referencePrice valued the tokens received at ~251,000 BNB
# (6,003x the rest of a 2,595-auction window; the sum-weighted capture read
# 0.0 %). Such an auction is listed with its ratio, excluded from the capture
# the 'competitive vs winners' verdict reads (both figures print), and marked
# in the JSON. The rule needs ARTEFACT_MIN_ATTEMPTED attempted auctions before
# it may fire — a thin window has no "rest" to measure against.
ARTEFACT_RATIO = 100
ARTEFACT_MIN_ATTEMPTED = 20

# Driver-side labels for the per-reason error dict (autopilot's
# `solutions{solver,result}` vocabulary) so a reader can line the replay's
# failures up with what the protocol would have recorded.
DRIVER_ERROR_LABELS = {
    "timeout": "DeadlineExceeded", "late": "DeadlineExceeded",
    "bad_json": "SolverDeserializeError",
    "bad_schema": "SolverDtoError", "bad_solver_response": "SolverDtoError",
    # everything else (http_<code>, unreachable*, response_too_large): SolverHttpError
}
DRIVER_ERROR_DEFAULT = "SolverHttpError"
# Which bucket a failure lands in: a deadline miss is the driver discarding an
# answer it never got in time; everything else is transport.
DEADLINE_MISS_REASONS = {"timeout", "late"}


# ---------------------------------------------------------------- primitives

MAX_U256 = (1 << 256) - 1


def to_u256(v):
    """Parse a DTO U256: non-negative int, decimal string, or 0x-hex string.
    Rejects negatives, bools, exotic Python int syntax the DTO would refuse,
    and anything above 2^256-1 — Python ints are unbounded, so without the
    upper check a buggy solver response could smuggle un-representable
    amounts straight into the score (v0.7.2 audit P0)."""
    if isinstance(v, bool):
        raise ValueError(f"not a U256: {v!r}")
    n = None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str):
        s = v.strip()
        if _HEX.match(s):
            n = int(s, 16)
        elif _DEC.match(s):
            n = int(s)
    if n is None:
        raise ValueError(f"not a U256: {v!r}")
    if n < 0 or n > MAX_U256:
        raise ValueError(f"out of U256 range: {v!r}")
    return n


# Hard ceiling on any single HTTP body. Real payloads top out around a few
# MB gzip'd (largest observed competition record ~170 KB, S3 bodies a few MB);
# without a cap a rogue endpoint can exhaust memory (audit hardening item).
_MAX_HTTP_BYTES = 64 * 1024 * 1024


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"cow-backtester/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(_MAX_HTTP_BYTES + 1)
    if len(data) > _MAX_HTTP_BYTES:
        raise ValueError(f"response exceeds {_MAX_HTTP_BYTES} bytes: {url.split('?')[0]}")
    return data


def _gunzip_bounded(raw):
    """Decompress with the byte cap applied to the DECOMPRESSED size — a
    1000x gzip bomb passes the wire cap and would otherwise exhaust memory."""
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as g:
        data = g.read(_MAX_HTTP_BYTES + 1)
    if len(data) > _MAX_HTTP_BYTES:
        raise ValueError(f"decompressed body exceeds {_MAX_HTTP_BYTES} bytes")
    return data


def s3_auction(env, chain, auction_id, cache=None, errors=None):
    """Archived /solve body (immutable, so always cacheable). None when the
    body is unavailable; the REASON is tallied into `errors` (a Counter) so a
    transient 5xx storm is never mistaken for retention expiry:
    s3_404 (evicted / never existed) · s3_fetch_error (network/5xx, retried
    once) · s3_bad_body (not gzip/JSON, or over the decompressed cap)."""
    if cache:
        hit = cache.get(f"s3-{env}", auction_id)
        if hit is not None:
            return hit
    url = f"{BUCKET}/{env}/{chain}/auction/{auction_id}.json"
    for attempt in range(2):
        try:
            raw = _http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if errors is not None:
                    errors["s3_404"] += 1
                return None
        except Exception:
            pass
        else:
            try:
                body = _gunzip_bounded(raw) if raw[:2] == b"\x1f\x8b" else raw
                out = json.loads(body)
            except Exception:
                if errors is not None:
                    errors["s3_bad_body"] += 1
                return None
            if cache:
                cache.put(f"s3-{env}", auction_id, out)
            return out
        if attempt == 0:
            time.sleep(0.5)
    if errors is not None:
        errors["s3_fetch_error"] += 1
    return None


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
    """POST a /solve body (a dict, or pre-serialized bytes so several solvers
    receive byte-identical payloads). Returns (response|None, error|None,
    latency_ms). The HTTP wait exceeds the advertised deadline so a solver
    computing right up to its deadline is never cut off.

    Only `{"solutions": [...]}` is an answer. Any other 200 body — an error
    envelope, an empty object, a typo'd key — is `bad_schema`, an ERROR, so a
    misrouted gateway or a crashing solver can never read as a healthy
    abstention (v0.10.0; the empty list stays the legitimate abstention)."""
    body = (bytes(auction_body) if isinstance(auction_body, (bytes, bytearray))
            else json.dumps(auction_body).encode())
    req = urllib.request.Request(f"{solver_url.rstrip('/')}/solve", body,
                                 {"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        # Solver endpoints are user-supplied: cap the read so a runaway
        # endpoint returns a structured error, not memory exhaustion.
        with urllib.request.urlopen(req, timeout=timeout + HTTP_GRACE_S) as r:
            raw = r.read(_MAX_HTTP_BYTES + 1)
        if len(raw) > _MAX_HTTP_BYTES:
            return None, "response_too_large", int((time.monotonic() - t0) * 1000)
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
    if not isinstance(out.get("solutions"), list):
        return None, "bad_schema", ms
    return out, None, ms


def dispatch_solvers(solvers, payload, timeout):
    """POST the SAME bytes to every solver CONCURRENTLY under the one shared
    deadline, so an A/B compares identical inputs AND identical wall-clock
    budgets. (Sequential dispatch handed the second solver a deadline already
    consumed by the first one's compute — a handicap the size of the rival's
    latency, which call-order rotation cannot neutralise; v0.10.0.)
    Returns [(solver, response, error, latency_ms)] in `solvers` order."""
    if len(solvers) == 1:
        sv = solvers[0]
        return [(sv, *solve(sv["url"], payload, timeout))]
    with ThreadPoolExecutor(max_workers=len(solvers)) as pool:
        return list(pool.map(lambda sv: (sv, *solve(sv["url"], payload, timeout)), solvers))


def challenger_rank(comp_rec, vs, self_address=None):
    """Rank the challenger the way the protocol ranks: by its best SINGLE
    solution's score against the field's per-solution scores. The combined
    disjoint-solution total (what capture metrics use) is NOT a bid the
    protocol ever sees — three 400-wei solutions are three third-place bids,
    not one 1200-wei winner — so it is reported alongside, labeled, never as
    the rank (v0.10.0)."""
    single = vs.get("best_single_wei", 0)
    fr = competition.rank_vs_field(comp_rec, single, self_address)
    if fr is None:
        return None
    fr["score_wei"] = single
    fr["rank_basis"] = "best_single_solution_vs_field_scores"
    combined = vs.get("best_surplus_wei", 0)
    if combined != single:
        fr2 = competition.rank_vs_field(comp_rec, combined)
        fr["rank_combined"] = fr2["rank"]
        fr["combined_score_wei"] = combined
    return fr


def select_newest(aids_sorted_asc, cap):
    """--max-auctions keeps the NEWEST auctions (replay uses live liquidity,
    so freshness is the whole game). cap 0 = all."""
    if cap and len(aids_sorted_asc) > cap:
        return aids_sorted_asc[-cap:]
    return list(aids_sorted_asc)


def _fmt_native(wei):
    """Native amount for the scorecard: 6 decimals when that shows the value,
    scientific notation below that — an L2 A/B whose whole result is a few
    microether must not print as a tie of 0.000000."""
    x = wei / 1e18
    if x == 0:
        return "0.000000"
    if abs(x) >= 1e-4:
        return f"{x:.6f}"
    return f"{x:.3e}"


# Skip reasons that REMOVE a settlement/auction from the winner baseline. A
# reverted settlement is not a winner, so it is not an exclusion.
_NON_EXCLUSION_SKIPS = {"settlement_reverted"}


def exclusion_count(skip):
    return sum(v for k, v in (skip or {}).items() if k not in _NON_EXCLUSION_SKIPS)


def gate_failed(verdicts, fail_on):
    """--fail-on: 'not-ready' trips on NOT READY; 'review' trips on REVIEW or
    NOT READY; None never trips."""
    if not fail_on:
        return False
    bad = {"NOT READY"} if fail_on == "not-ready" else {"NOT READY", "REVIEW"}
    return any(v in bad for v in verdicts)


# ------------------------------------------------- readiness-standard helpers

def resolve_budget(chain, solve_timeout=None, readiness=False, lane="settle"):
    """The per-request budget the replay advertises, seconds, and where it
    came from: `override` (--solve-timeout), `observed` (BUDGETS_S), or
    `assumed` (ASSUMED_BUDGET_S). With `readiness=True` an assumed budget is
    refused (SystemExit 2, usage error): a verdict measured against a budget
    nobody observed is not a readiness number."""
    if solve_timeout is not None:
        return float(solve_timeout), "override"
    observed = (BUDGETS_S.get(chain) or {}).get(lane)
    if observed is not None:
        return float(observed), "observed"
    if readiness:
        print(f"ERROR: no observed per-request budget for --chain {chain} ({lane} lane) in "
              f"BUDGETS_S, and --readiness refuses to measure against an assumed one. "
              f"Pass the driver's real budget explicitly, e.g. --solve-timeout 4.6 "
              f"(seconds), or add an observed value to BUDGETS_S.", file=sys.stderr)
        raise SystemExit(2)
    return float(ASSUMED_BUDGET_S), "assumed"


def resolve_thresholds(chain, min_evidence=None):
    """Threshold profile for a chain: `default` with the chain's overrides
    laid on top. Returns (values, profile_name, sources) where sources maps
    each key to 'default' | '<chain>' | 'override' (CLI)."""
    values = dict(THRESHOLDS["default"])
    sources = {k: "default" for k in values}
    profile = "default"
    over = THRESHOLDS.get(chain)
    if over:
        profile = chain
        for k, v in over.items():
            values[k] = v
            sources[k] = chain
    if min_evidence is not None:
        values["min_evidence"] = int(min_evidence)
        sources["min_evidence"] = "override"
    return values, profile, sources


def classify_outcome(err, ms, budget_ms):
    """Map one dispatch outcome to the driver's taxonomy.

    Returns (bucket, reason) with bucket in {'answered', 'deadline_miss',
    'transport'} and reason the fine-grained label (None for an answer in
    time). A `timeout` is a deadline miss (the driver got nothing by the
    deadline); an answer that arrives after the budget is ALSO a deadline miss
    (`late` — the driver would have discarded it); every other error is
    transport."""
    if err is None:
        if ms > budget_ms:
            return "deadline_miss", "late"
        return "answered", None
    if err in DEADLINE_MISS_REASONS:
        return "deadline_miss", err
    return "transport", err


def driver_error_label(reason):
    return DRIVER_ERROR_LABELS.get(reason, DRIVER_ERROR_DEFAULT)


def driver_error_table(errors):
    """{driver_label: {fine_reason: n}} from a flat per-reason Counter."""
    out = {}
    for reason, n in (errors or {}).items():
        out.setdefault(driver_error_label(reason), {})[reason] = n
    return out


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]


def body_sha256(body):
    """Digest of the canonical JSON form of an auction body, computed BEFORE
    the replay touches it (deadline stamp, --clamp-validto), so an archived
    body and a re-fetched one hash identically."""
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stamp_deadline(body, row, budget_s):
    """Preserve the archived wire deadline, then advertise a fresh one of
    exactly `budget_s` seconds. Returns the stamped ISO deadline."""
    row["original_deadline"] = body.get("deadline")
    body["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=budget_s)
                        ).isoformat().replace("+00:00", "Z")
    row["replay_deadline"] = body["deadline"]
    return body["deadline"]


def parse_iso_ts(s):
    """Seconds since the epoch for an ISO-8601 UTC timestamp with any
    sub-second precision (the driver emits nanoseconds); None if unparseable."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?([+-]\d\d:\d\d)$", t)
    if not m:
        return None
    base, frac, tz = m.groups()
    try:
        dt = datetime.fromisoformat(base + tz)
    except ValueError:
        return None
    return dt.timestamp() + (float("0" + frac) if frac else 0.0)


def bodies_path(bodies_dir, chain, auction_id):
    return os.path.join(bodies_dir, chain, f"{auction_id}.json.gz")


def archive_body(bodies_dir, chain, auction_id, body, sha, frm, to):
    """--archive-bodies: store one body as DIR/<chain>/<aid>.json.gz (canonical
    JSON) and append a manifest line. Idempotent on the body file; the
    manifest records every run that used it."""
    path = bodies_path(bodies_dir, chain, auction_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(body, f, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, path)
    with open(os.path.join(bodies_dir, "manifest.jsonl"), "a", encoding="utf-8") as mf:
        mf.write(json.dumps({"auction_id": auction_id, "chain": chain, "sha256": sha,
                             "from_block": frm, "to_block": to,
                             "fetched_at": datetime.now(timezone.utc).isoformat()
                             .replace("+00:00", "Z")}) + "\n")


def load_archived_body(bodies_dir, chain, auction_id):
    """--bodies-dir: the archived body, or None when it is not archived. There
    is deliberately NO S3 fallback — a reproduction must fail loudly."""
    path = bodies_path(bodies_dir, chain, auction_id)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def preflight(solver_url, timeout=10):
    """Fail-fast reachability probe. An HTTP response proves the endpoint is
    alive — EXCEPT 404/405 on POST /solve, which means the wrong path (the tool
    appends /solve; passing the full path is the most common user error, and
    every replay would then error). Returns (ok, reason)."""
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
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False, (f"HTTP {e.code} for POST {solver_url.rstrip('/')}/solve — the tool "
                           f"appends /solve to --solver-url; pass the endpoint base "
                           f"(e.g. http://host:8080), not the full /solve path")
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


def validate_and_score(resp, body, ref_prices, fairness_baseline=None):
    """Validate + score a solver response on the shared comparison basis.

    A solution is scored only if EVERY fulfillment trade is feasible: known
    order uid (case-insensitive), strict U256 numerics, prices for both tokens,
    at most one fulfillment per uid (GPv2 accumulates fills; duplicates are
    rejected as the driver does), no over-fill (exact for fill-or-kill), and
    on-chain feasibility at the fee-adjusted terms — the net delivery must
    still cover the gross-scaled limit, so a padded fee cannot manufacture
    surplus. Violations invalidate the SOLUTION (tallied), never score zero.

    Validity is the protocol's three-part definition (v0.11.0):
      (i)  eligibility — at least one scored fulfillment (a zero-surplus fill
           still counts; tallied as `valid_zero_surplus` so the edge shows);
      (ii) uniform directional clearing prices — one price per traded token
           in the solution's `prices` map (`udcp_violation` when a token is
           priced twice under different spellings; `udcp_checked` counts the
           solutions the check ran on);
      (iii) CIP-67 fairness — when `fairness_baseline` ({(sell, buy): wei},
           the field's best single-pair score per directed pair, see
           competition.pair_baselines) is given, a solution trading MORE than
           one directed pair is filtered if any of its pairs scores below that
           pair's baseline (single-pair solutions are never filtered; the
           challenger's own single-pair solutions raise the baselines exactly
           as they would in the real competition). Filtered solutions are
           counted in `fairness_filtered`, dropped from `n_valid` and from the
           capture numerator. Without a baseline `fairness` is
           `not_evaluated` and nothing is filtered.

    Valid solutions are combined CIP-67 style: best-first, disjoint directed
    (sell,buy) pairs — a strong solver can win several slots of one auction.
    """
    out = {"best_surplus_wei": 0, "best_single_wei": 0, "n_solutions": 0,
           "n_valid": 0, "invalid": {}, "jit_ignored": 0, "no_refprice": 0,
           "by_pair": {}, "by_order": {},
           "valid_zero_surplus": 0, "udcp_checked": 0, "udcp_violations": 0,
           "fairness_filtered": 0,
           "fairness": "evaluated" if fairness_baseline is not None else "not_evaluated"}
    if not isinstance(resp, dict) or not isinstance(resp.get("solutions"), list):
        return out
    by_uid = {o["uid"].lower(): o for o in body.get("orders", [])}
    invalid = Counter()
    best_by_order = {}
    candidates = []          # (surplus_wei, frozenset(pairs), {pair: wei})
    feasible = []            # (surplus_wei, frozenset(pairs), {pair: wei}, {uid: wei})

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
            # (ii) UDCP, structural: the schema carries ONE prices map per
            # solution, so a token can only be priced twice by appearing under
            # two spellings. Named so the report can say how many were checked.
            out["udcp_checked"] += 1
            if len(prices) != len(prices_raw):
                out["udcp_violations"] += 1
                reasons.add("udcp_violation")

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
            order_wei = {}
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
                order_wei[uid] = wei
                scored += 1

            if reasons:
                for r in reasons:
                    invalid[r] += 1
                continue
            if scored == 0:
                continue
            if total == 0:
                out["valid_zero_surplus"] += 1
            feasible.append((total, frozenset(pair_wei), dict(pair_wei), order_wei))
        except (_Malformed, TypeError, AttributeError, KeyError):
            invalid["malformed"] += 1
            continue

    # (iii) fairness, applied AFTER every feasible solution is known: the
    # challenger's own single-pair solutions raise the baselines, exactly as
    # they would in the real competition (winner_selection::compute_baseline_scores).
    baseline = None
    if fairness_baseline is not None:
        baseline = dict(fairness_baseline)
        for total, pairs, _pw, _ in feasible:
            if len(pairs) == 1 and total > 0:
                (p,) = tuple(pairs)
                if total > baseline.get(p, 0):
                    baseline[p] = total
    for total, pairs, pw, order_wei in feasible:
        if (baseline is not None and len(pairs) > 1
                and any(p in baseline and pw[p] < baseline[p] for p in pairs)):
            out["fairness_filtered"] += 1
            continue
        out["n_valid"] += 1
        candidates.append((total, pairs, pw))
        # best valid bid per order across this response's solutions — the
        # CIP-85 v2 consistency metric is built from the solver's BEST
        # fair bid on each executed order
        for uid, wei in order_wei.items():
            if wei > best_by_order.get(uid, -1):
                best_by_order[uid] = wei

    out["by_order"] = {u: w for u, w in best_by_order.items()}

    combined, chosen, combiner = _best_disjoint_combination(candidates)
    by_pair = defaultdict(int)
    for _, _, pw in chosen:
        for k, v in pw.items():
            by_pair[k] += v
    out["best_single_wei"] = max((c[0] for c in candidates), default=0)
    out["best_surplus_wei"] = combined
    out["combiner"] = combiner
    out["by_pair"] = {f"{a}|{b}": v for (a, b), v in by_pair.items()}
    out["invalid"] = dict(invalid)
    return out


# Above this size, exact search could blow up (2^n subsets) and we fall back
# to greedy; real /solve responses carry a handful of solutions, so the
# fallback should never fire outside adversarial inputs. The result is
# labeled either way (`combiner: exact|greedy`).
_EXACT_COMBINER_MAX = 20


def _best_disjoint_combination(candidates):
    """Maximum-weight selection of solutions with pairwise-disjoint directed
    pairs (the solver's best internally compatible combination — NOT a CIP-67
    competition simulation, which would need the historical field + fairness
    filtering). Greedy highest-first is NOT optimal here: solutions A=10 on
    {P1,P2} vs B=6 on {P1} + C=6 on {P2} — greedy takes 10, optimum is 12.
    Exact branch-and-bound over ≤ _EXACT_COMBINER_MAX candidates; sorted
    descending so the remaining-sum bound prunes hard.
    Returns (total_wei, chosen_candidates, combiner_label)."""
    if not candidates:
        return 0, [], "exact"
    cands = sorted(candidates, key=lambda c: c[0], reverse=True)
    if len(cands) > _EXACT_COMBINER_MAX:
        taken, combined, chosen = set(), 0, []
        for c in cands:
            if c[1] & taken:
                continue
            combined += c[0]
            taken |= c[1]
            chosen.append(c)
        return combined, chosen, "greedy"
    suffix = [0] * (len(cands) + 1)
    for i in range(len(cands) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + cands[i][0]
    best = {"total": 0, "chosen": []}

    def dfs(i, taken, total, chosen):
        if total > best["total"]:
            best["total"], best["chosen"] = total, list(chosen)
        if i == len(cands) or total + suffix[i] <= best["total"]:
            return
        score, pairs, _ = cands[i]
        if not (pairs & taken):
            chosen.append(cands[i])
            dfs(i + 1, taken | pairs, total + score, chosen)
            chosen.pop()
        dfs(i + 1, taken, total, chosen)

    dfs(0, frozenset(), 0, [])
    return best["total"], best["chosen"], "exact"


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
        aids = select_newest(aids, args.max_auctions)
        say(f"      {n_found} auctions found; capped to {len(aids)} newest "
              f"(per --max-auctions; NOT a full-field sample)")
    else:
        say(f"      {n_found} auctions from {len(txs)} settlement txs")

    # prefetch bodies concurrently (immutable and cached, so cheap on re-runs)
    bodies_dir = getattr(args, "bodies_dir", None)
    archive_bodies = getattr(args, "archive_bodies", None)
    say("[3/4] " + ("loading archived auction bodies" if bodies_dir else "fetching auction bodies")
          + " + winner baselines"
          + (f" + replaying {len(args.solvers)} solver(s)" if args.solvers else ""))
    agg = Counter()

    def _fetch_body(a):
        if bodies_dir:
            # --bodies-dir: the archive is the ONLY source. A missing body is a
            # visible skip, never a silent S3 fallback (reproducibility, v0.11.0).
            b = load_archived_body(bodies_dir, args.chain, a)
            return b, (None if b else "body_not_archived")
        errs = Counter()
        b = s3_auction(args.env, args.chain, a, cache, errors=errs)
        return b, (next(iter(errs)) if errs else None)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        bodies = dict(zip(aids, pool.map(_fetch_body, aids), strict=True))
    # Digest every body BEFORE the replay mutates it (deadline stamp,
    # --clamp-validto); archive on request.
    body_shas = {}
    for a, (b, _err) in bodies.items():
        if not b:
            continue
        body_shas[a] = body_sha256(b)
        if archive_bodies:
            try:
                archive_body(archive_bodies, args.chain, a, b, body_shas[a], frm, to)
                agg["bodies_archived"] += 1
            except OSError as e:
                agg["archive_error"] += 1
                if agg["archive_error"] == 1:
                    say(f"      ⚠ --archive-bodies write failed: {e}")

    rows = []
    field_by_bucket = defaultdict(lambda: [0, 0])
    pair_stats = defaultdict(lambda: {"trades": 0, "winner_wei": 0,
                                      "solvers": defaultdict(int), "label": None})
    submitters = defaultdict(lambda: {"settlements": 0, "surplus_wei": 0})
    total_fees = 0
    ages = []
    ts_mem = {}
    api_check = Counter()
    per_solver = {s["name"]: {"replayed": 0, "returned": 0, "valid": 0,
                              "positive": 0, "beat": 0, "our_surplus": 0,
                              "winner_surplus": 0, "implausible": 0,
                              # v0.7.2 coverage-adjusted accounting: attempted
                              # counts EVERY auction sent to the solver, and
                              # winner_surplus_attempted accrues the baseline
                              # on errors too — otherwise a solver improves
                              # its capture ratio by failing hard auctions
                              # (survivorship bias, audit P0).
                              "attempted": 0, "winner_surplus_attempted": 0,
                              "lost_to_errors": 0,
                              "our_surplus_exact": 0, "winner_surplus_exact": 0,
                              "rank_rows": [], "econ_rows": [],
                              "invalid": Counter(), "errors": Counter(),
                              "latency": [],
                              # v0.11.0 driver taxonomy: a failure is either a
                              # deadline miss (timeout, or an answer after the
                              # budget) or a transport error — never "errored".
                              "transport": 0, "deadline_miss": 0,
                              # v0.11.0 validity disclosure
                              "fairness_filtered": 0, "fairness_evaluated": 0,
                              "fairness_not_evaluated": 0, "valid_zero_surplus": 0,
                              "udcp_checked": 0, "udcp_violations": 0,
                              "basis_mix": Counter()}
                  for s in args.solvers}
    field_econ = []      # per-auction FIELD consistency terms (needs no solver)
    now_ts = time.time()
    done = 0
    # v0.11.0 window disclosure: when each attempted auction happened
    attempted_ts = []          # settlement-block timestamps
    attempted_start_ts = []    # auctionStartBlock timestamps (needs --compete)
    budget_upper = []          # original_deadline − auctionStartBlock ts, seconds
    budget_s = float(getattr(args, "budget_s", None) or ASSUMED_BUDGET_S)
    budget_ms = budget_s * 1000

    try:
        for aid in aids:
            txrecs = auctions[aid]
            body, body_err = bodies.get(aid, (None, None))
            if not body:
                skip[body_err or "s3_body_missing"] += 1
                continue
            ref_prices = {k.lower(): int(v["referencePrice"])
                          for k, v in body.get("tokens", {}).items()
                          if v.get("referencePrice") is not None}
            jit_owners = {a.lower() for a in body.get("surplusCapturingJitOrderOwners", []) or []}

            by_uid = {o["uid"].lower() for o in body.get("orders", [])}
            # PER-TRANSACTION attribution proof (v0.7.2): each settlement tx
            # must individually show UID overlap with the auction body (or be
            # all-JIT). The old group-level any() let one correctly attributed
            # tx vouch for every other tx in the auction group — a
            # misattributed wrapper tx would then pollute the winner baseline.
            kept = []
            for r in txrecs:
                tx_overlap = any(ev["uid"] in by_uid for ev in r["events"])
                tx_all_jit = bool(jit_owners) and all(
                    ev["owner"] in jit_owners for ev in r["events"])
                if tx_overlap or tx_all_jit:
                    kept.append(r)
                else:
                    skip["tx_uid_mismatch"] += 1
            if not kept:
                skip["body_uid_mismatch"] += 1
                continue
            txrecs = kept

            winner_total = winner_fees = 0
            winner_txs, per_trades = [], []
            # Baseline quality label (v0.7.2): direct settlements score on the
            # exact uniform-price basis; wrapper settlements on the delivered
            # (lower-bound) basis. Aggregating the two silently mixes bases,
            # so every row and every capture metric carries the label.
            n_wrap = sum(1 for r in txrecs if r.get("dec") is None)
            baseline_quality = ("exact_uniform" if n_wrap == 0
                                else "wrapper_lower_bound" if n_wrap == len(txrecs)
                                else "mixed")
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

            # Highest SETTLEMENT block of the auction's txs — NOT the auction
            # cut block (state at bid time is earlier; see auction_start_block
            # / auction_deadline_block when --compete fetched the record).
            blk = max(r["block"] for r in txrecs)
            ts = block_ts(rpcs, blk, ts_mem, cache, max_cacheable)
            age_h = (now_ts - ts) / 3600 if ts is not None else None
            if age_h is not None:
                ages.append(age_h)

            row = {"v": VERSION, "chain": args.chain, "env": args.env, "auction_id": aid,
                   "settlement_block": blk, "block_ts": ts,
                   "age_hours": round(age_h, 2) if age_h is not None else None,
                   "winner_txs": winner_txs, "winner_surplus_wei": winner_total,
                   "winner_fee_wei": winner_fees, "baseline_quality": baseline_quality,
                   "body_sha256": body_shas.get(aid)}

            if args.verify_api:
                v = verify_against_api(args.chain, aid, [t["tx"] for t in winner_txs])
                api_check[v] += 1
                row["api_check"] = v

            comp_rec = None
            fair_base = None      # CIP-67 baselines; None = fairness not evaluated
            start_ts = None
            if args.compete or args.archive_dir:
                api_base = f"{COW_API}/{CHAINS[args.chain]['api']}"
                comp_rec = competition.fetch_competition(api_base, aid, _http_get, cache)
                if comp_rec:
                    agg["competition_fetched"] += 1
                    # The auction-CUT context (what bidders actually saw) —
                    # complements settlement_block, which is always later.
                    row["auction_start_block"] = comp_rec.get("auctionStartBlock")
                    row["auction_deadline_block"] = comp_rec.get("auctionDeadlineBlock")
                    if isinstance(row["auction_start_block"], int):
                        start_ts = block_ts(rpcs, row["auction_start_block"], ts_mem,
                                            cache, max_cacheable)
                        row["auction_start_ts"] = start_ts
                    # v0.11.0: the winner's referenceScore, copied for the
                    # reader; no check consumes it.
                    winners = [s for s in comp_rec.get("solutions") or [] if s.get("isWinner")]
                    if winners:
                        best = max(winners, key=lambda s: int(s.get("score") or 0))
                        if best.get("referenceScore") is not None:
                            row["winner_reference_score"] = best["referenceScore"]
                    fair_base = competition.pair_baselines(comp_rec, body, stats=agg)
                    if args.archive_dir:
                        try:
                            if competition.archive_store(args.archive_dir, args.chain, comp_rec):
                                agg["competition_archived"] += 1
                        except OSError as e:
                            # a filesystem problem must not discard the
                            # scored window
                            agg["archive_error"] += 1
                            if agg["archive_error"] == 1:
                                say(f"      ⚠ --archive-dir write failed: {e}")
                else:
                    agg["competition_missing"] += 1
            econ_table = None
            if comp_rec is not None and getattr(args, "reward_ev", False):
                econ_table = economics.executed_order_surpluses(
                    comp_rec, body, ref_prices, stats=agg)
                # The field's real historical terms exist with or without a
                # challenger — the consistency leaderboard needs no solver.
                field_econ.append(economics.consistency_terms(econ_table)[0])

            if args.solvers:
                expired = sum(1 for o in body.get("orders", [])
                              if int(o.get("validTo", 0)) < now_ts)
                n_orders = max(len(body.get("orders", [])), 1)
                row["expired_orders_pct"] = round(100 * expired / n_orders, 1)
                if args.clamp_validto and expired:
                    horizon = int(now_ts) + int(budget_s) + 600
                    for o in body.get("orders", []):
                        if int(o.get("validTo", 0)) < now_ts:
                            o["validTo"] = horizon
                    row["validto_clamped"] = expired
                    # Disclosed everywhere a verdict is read: a READY produced
                    # from modified bodies must say so (coverage block,
                    # readiness screen, _meta line).
                    agg["validto_clamped_auctions"] += 1
                    agg["validto_clamped_orders"] += expired

                row["solvers"] = {}
                # ONE shared deadline per auction, serialized ONCE, dispatched
                # to every solver CONCURRENTLY: identical bytes and identical
                # wall-clock budget (v0.7.2 made the bytes identical; v0.10.0
                # makes the budget identical — sequential calls handed the
                # second solver a deadline the first one had already consumed).
                # v0.11.0: the budget is the OBSERVED driver budget (or the
                # operator's override), never a 20 s constant, and the archived
                # wire deadline is preserved on the row before it is replaced.
                stamp_deadline(body, row, budget_s)
                row["budget_s"] = budget_s
                row["budget_source"] = getattr(args, "budget_source", "assumed")
                orig = parse_iso_ts(row.get("original_deadline"))
                if orig is not None and start_ts is not None:
                    # upper bound on the true budget: the driver sent the body
                    # some (unrecorded) time after the auction-start block
                    row["original_budget_upper_s"] = round(orig - start_ts, 3)
                    row["original_budget_upper_basis"] = (
                        "original_deadline minus auctionStartBlock timestamp — an UPPER "
                        "bound on the driver's real per-request budget (send time unknown)")
                    budget_upper.append(row["original_budget_upper_s"])
                if ts is not None:
                    attempted_ts.append(ts)
                if start_ts is not None:
                    attempted_start_ts.append(start_ts)
                payload = json.dumps(body).encode()
                for sv, resp, err, ms in dispatch_solvers(args.solvers, payload, budget_s):
                    st = per_solver[sv["name"]]
                    st["attempted"] += 1
                    st["winner_surplus_attempted"] += winner_total
                    st["basis_mix"][baseline_quality] += 1
                    if baseline_quality == "exact_uniform":
                        # exact-basis denominator accrues on ATTEMPTED too, or
                        # the "cleanest" number carries the survivorship bias
                        # the headline was fixed for (v0.10.0)
                        st["winner_surplus_exact"] += winner_total
                    if econ_table is not None and comp_rec is not None:
                        st["rank_eligible"] = st.get("rank_eligible", 0) + 1
                    bucket, reason = classify_outcome(err, ms, budget_ms)
                    if bucket != "answered":
                        st[bucket] += 1
                        st["errors"][reason] += 1
                        st["lost_to_errors"] += winner_total
                        if err is None:
                            # a LATE answer is still an answer for latency
                            # purposes — hiding it would flatter p95
                            st["latency"].append(ms)
                        row["solvers"][sv["name"]] = {
                            "solve_error": reason, "driver_result": driver_error_label(reason),
                            "outcome": bucket, "latency_ms": ms}
                        if econ_table is not None:
                            # the field still earned its terms here; we earned
                            # nothing — keep the auction in BOTH sides
                            st["econ_rows"].append(economics.zero_challenger_row(econ_table))
                        continue
                    try:
                        vs = validate_and_score(resp, body, ref_prices, fairness_baseline=fair_base)
                    except Exception as e:
                        st["transport"] += 1
                        st["errors"]["bad_solver_response"] += 1
                        st["lost_to_errors"] += winner_total
                        row["solvers"][sv["name"]] = {
                            "solve_error": f"bad_solver_response ({type(e).__name__})",
                            "driver_result": driver_error_label("bad_solver_response"),
                            "outcome": "transport", "latency_ms": ms}
                        if econ_table is not None:
                            st["econ_rows"].append(economics.zero_challenger_row(econ_table))
                        continue
                    # latency describes ANSWERS; a dead endpoint's fast
                    # failures must not buy it a flattering p95
                    st["latency"].append(ms)
                    st["replayed"] += 1
                    st["fairness_filtered"] += vs["fairness_filtered"]
                    st["valid_zero_surplus"] += vs["valid_zero_surplus"]
                    st["udcp_checked"] += vs["udcp_checked"]
                    st["udcp_violations"] += vs["udcp_violations"]
                    if vs["n_solutions"]:
                        st["fairness_evaluated" if vs["fairness"] == "evaluated"
                           else "fairness_not_evaluated"] += 1
                    st["winner_surplus"] += winner_total
                    ours = vs["best_surplus_wei"]
                    st["our_surplus"] += ours
                    if baseline_quality == "exact_uniform":
                        st["our_surplus_exact"] += ours
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
                                                 "n_solutions", "n_valid", "invalid",
                                                 "fairness", "fairness_filtered",
                                                 "valid_zero_surplus", "udcp_checked",
                                                 "udcp_violations")}
                    vs_row["latency_ms"] = ms
                    vs_row["outcome"] = "answered"
                    if implausible:
                        vs_row["flags"] = ["implausible_surplus"]
                    if comp_rec is not None and args.compete:
                        if vs["n_valid"]:
                            fr = challenger_rank(comp_rec, vs, args.self_address)
                            if fr:
                                vs_row["field_rank"] = fr
                                st["rank_rows"].append(fr)
                        else:
                            # answered with a record but placed no valid bid:
                            # counted, so "rank 1 in 100%" cannot hide a 5%
                            # bid rate
                            st["rank_no_bid"] = st.get("rank_no_bid", 0) + 1
                    if econ_table is not None:
                        field_t, ch_t, ch_orders = economics.consistency_terms(
                            econ_table, vs.get("by_order"))
                        st["econ_rows"].append({
                            "field": field_t,
                            "challenger": ch_t,
                            "challenger_orders": ch_orders,
                            "executed_orders": len(econ_table),
                            "challenger_won":
                                (vs_row.get("field_rank") or {}).get("rank") == 1,
                        })
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
            "field_econ": field_econ,
            "attempted_ts": attempted_ts, "attempted_start_ts": attempted_start_ts,
            "budget_upper": budget_upper, "bodies_source": "archive" if bodies_dir else "s3",
            "interrupted": interrupted, "from_block": frm, "to_block": to, "native": nat}


# ---------------------------------------------------------------- reporting

def _pct(a, b):
    return None if not b else 100 * a / b


def solver_ledger(rows, name):
    """Per-auction view of the sums process_window accrues for one solver:
    one entry per auction the solver was SENT (answered or not), carrying the
    decoded winner surplus and the solver's row result (v0.11.1)."""
    out = []
    for row in rows or []:
        vs = (row.get("solvers") or {}).get(name)
        if vs is None:
            continue
        out.append({"auction_id": row.get("auction_id"),
                    "winner_surplus_wei": int(row.get("winner_surplus_wei") or 0),
                    "winner_reference_score": row.get("winner_reference_score"),
                    "vs": vs})
    return out


def winner_surplus_artefacts(entries):
    """BT-10: auctions whose decoded winner surplus exceeds ARTEFACT_RATIO x the
    rest of the attempted window's winner surplus combined — a reference-price
    valuation artefact, not delivered value. `entries` are solver_ledger()
    dicts (anything with auction_id / winner_surplus_wei works). Returns
    [{auction_id, winner_surplus_wei, rest_wei, ratio, our_surplus_wei,
    answered, winner_reference_score}] by descending ratio; [] below
    ARTEFACT_MIN_ATTEMPTED entries, or when the rest of the window has no
    surplus to measure against. By construction at most one auction can
    satisfy the rule (two would need more than the whole window between
    them), so two artefacts of similar size mask each other — the listing is
    a list for JSON stability, not because several can appear."""
    if len(entries) < ARTEFACT_MIN_ATTEMPTED:
        return []
    total = sum(e["winner_surplus_wei"] for e in entries)
    out = []
    for e in entries:
        w = e["winner_surplus_wei"]
        rest = total - w
        if rest > 0 and w > ARTEFACT_RATIO * rest:
            vs = e.get("vs") or {}
            out.append({"auction_id": e["auction_id"], "winner_surplus_wei": w,
                        "rest_wei": rest, "ratio": round(w / rest, 1),
                        "our_surplus_wei": int(vs.get("best_surplus_wei") or 0),
                        "answered": "n_solutions" in vs,
                        "winner_reference_score": e.get("winner_reference_score")})
    return sorted(out, key=lambda a: -a["ratio"])


def per_bid_ratios(entries, exclude_ids=()):
    """BT-12: our best valid surplus / the winner's surplus on every UNFLAGGED
    bid auction — answered with >= 1 solution, not flagged implausible_surplus,
    not a winner-surplus artefact, winner surplus > 0 (a zero winner has no
    ratio; a bid with no valid solution counts as 0). Their median is the
    per-bid read the sum-weighted capture hides: a solver can match the
    winners bid for bid and still capture little when it never enters the
    largest auctions."""
    out = []
    for e in entries:
        vs = e.get("vs") or {}
        if e["auction_id"] in exclude_ids or not vs.get("n_solutions"):
            continue
        if "implausible_surplus" in (vs.get("flags") or []) or e["winner_surplus_wei"] <= 0:
            continue
        out.append(int(vs.get("best_surplus_wei") or 0) / e["winner_surplus_wei"])
    return out


PER_BID_RATIO_BASIS = ("median over answered auctions with >= 1 solution of (best valid surplus / "
                       "winner surplus); implausible_surplus rows, winner-surplus artefacts and "
                       "zero-winner auctions excluded")


def readiness_report(st, args):
    """A one-screen pre-production readiness check for a single solver endpoint:
    did it answer, how fast, and were its solutions sane against the winners?
    Reuses the counterfactual pass's per-solver stats — no extra fetching.
    Returns a JSON-able dict; also prints the report unless --quiet.

    v0.11.0: every threshold comes from THRESHOLDS (per-chain overrides on top
    of `default`), the budget from BUDGETS_S / --solve-timeout (never an
    assumed constant), failures follow the driver's taxonomy (deadline miss vs
    transport), a capped or thin sample cannot be READY, and the header says
    exactly which window, budget and thresholds produced the verdict.

    v0.11.1: a winner-surplus valuation artefact (> ARTEFACT_RATIO x the rest
    of the attempted window) is listed and excluded from the capture the
    'competitive vs winners' verdict reads — both figures print — and the
    per-bid median ratio (ours / winner over unflagged bid auctions) prints
    next to the sum-weighted capture."""
    reports = []
    chain = getattr(args, "chain", None)
    env = getattr(args, "env", None)
    budget_s = getattr(args, "budget_s", None)
    budget_source = getattr(args, "budget_source", None)
    if budget_s is None:
        budget_s, budget_source = resolve_budget(
            chain, getattr(args, "solve_timeout", None), readiness=True)
    budget_s = float(budget_s)
    budget_ms = budget_s * 1000
    thr, profile, thr_src = resolve_thresholds(chain, getattr(args, "min_evidence", None))
    min_evidence = thr["min_evidence"]
    nat = st.get("native") or CHAINS.get(chain, {}).get("native", "ETH")
    cap = int(getattr(args, "max_auctions", 0) or 0)

    # window disclosure (shared by every solver in the run)
    found = len(st.get("txs") or [])
    n_found = st.get("n_found", 0)
    sk = st.get("skip") or {}
    excl = exclusion_count(sk)
    top_excl = sorted(((k, v) for k, v in sk.items() if k not in _NON_EXCLUSION_SKIPS),
                      key=lambda kv: -kv[1])[:3]
    unscanned = sum(hi - lo + 1 for lo, hi in (st.get("failed_ranges") or []))
    span_src = st.get("attempted_start_ts") or st.get("attempted_ts") or []
    ts_basis = ("auctionStartBlock timestamps" if st.get("attempted_start_ts")
                else "settlement-block timestamps")
    span_start = min(span_src) if span_src else None
    span_end = max(span_src) if span_src else None
    span_hours = ((span_end - span_start) / 3600.0
                  if span_src and span_end > span_start else None)
    bu = sorted(st.get("budget_upper") or [])
    upper = ({"p50_s": _percentile(bu, 0.5), "p95_s": _percentile(bu, 0.95), "n": len(bu),
              "basis": "original_deadline minus auctionStartBlock timestamp (upper bound)"}
             if bu else None)
    clamped = (st.get("agg") or {}).get("validto_clamped_auctions", 0)

    def _iso(t):
        return (datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
                .replace("+00:00", "Z") if t is not None else "n/a")

    for sv in args.solvers:
        s = st["per_solver"][sv["name"]]
        transport = s.get("transport", 0)
        dmiss = s.get("deadline_miss", 0)
        attempted = s.get("attempted") or (s["replayed"] + transport + dmiss)
        lat = sorted(s["latency"])
        p50, p95, pmax = _percentile(lat, 0.5), _percentile(lat, 0.95), (lat[-1] if lat else None)
        # v0.7.2 semantics split: an HTTP-200 `{"solutions": []}` is a HEALTHY
        # answer (the schema's legitimate abstention), not an endpoint
        # failure — so answer rate counts parsed responses (replayed), and
        # bid coverage separately counts auctions with >=1 solution.
        answer_rate = _pct(s["replayed"], attempted)
        bid_rate = _pct(s["returned"], s["replayed"])
        valid_rate = _pct(s["valid"], s["returned"])
        transport_rate = _pct(transport, attempted)
        dmiss_rate = _pct(dmiss, attempted)
        # Coverage-adjusted capture is the honest readiness read: errors keep
        # the historical winner in the denominator (survivorship-bias fix).
        capture_adj = _pct(s["our_surplus"],
                           s.get("winner_surplus_attempted") or s["winner_surplus"])
        capture_cond = _pct(s["our_surplus"], s["winner_surplus"])
        capture = capture_adj
        basis_mix = dict(s.get("basis_mix") or {})
        # v0.11.1 (BT-10/12): the per-auction ledger behind those sums. A
        # winner-surplus valuation artefact is listed and the capture the
        # verdict reads excludes it (both figures print); the per-bid median
        # ratio says how we do on the auctions we actually enter.
        ledger = solver_ledger(st.get("rows"), sv["name"])
        artefacts = winner_surplus_artefacts(ledger)
        art_ids = {a["auction_id"] for a in artefacts}
        if artefacts:
            art_ours = sum(a["our_surplus_wei"] for a in artefacts)
            capture_ex = _pct(s["our_surplus"] - art_ours,
                              (s.get("winner_surplus_attempted") or s["winner_surplus"])
                              - sum(a["winner_surplus_wei"] for a in artefacts))
            capture_cond_ex = _pct(s["our_surplus"] - art_ours,
                                   s["winner_surplus"]
                                   - sum(a["winner_surplus_wei"] for a in artefacts if a["answered"]))
        else:
            capture_ex, capture_cond_ex = capture, capture_cond     # nothing excluded
        bid_ratios = per_bid_ratios(ledger, art_ids)
        per_bid_median = statistics.median(bid_ratios) if bid_ratios else None
        fe, fne = s.get("fairness_evaluated", 0), s.get("fairness_not_evaluated", 0)
        if fe and not fne:
            validity_basis = "feasibility+eligibility+udcp+fairness"
        elif fe:
            validity_basis = f"feasibility+eligibility+udcp+fairness:partial({fe}/{fe + fne} evaluated)"
        else:
            validity_basis = "feasibility+eligibility+udcp+fairness:not-evaluated"
        auctions_per_hour = (round(attempted / span_hours, 2)
                             if span_hours and attempted else None)
        errors = driver_error_table(s["errors"])

        checks = []

        def chk(ok, warn, label, detail, _out=checks):
            _out.append({"level": "ok" if ok else ("warn" if warn else "fail"),
                         "label": label, "detail": detail})
        chk(attempted > 0, False, "reached auctions",
            f"{attempted} auctions attempted")
        if attempted:
            chk(transport == 0, (transport_rate or 0) <= thr["transport_warn"],
                "no transport errors",
                (f"{transport}/{attempted} ({transport_rate:.2f}%) transport errors "
                 f"[{', '.join(k for k in errors if k != 'DeadlineExceeded')}]")
                if transport else "0 transport errors")
            chk((answer_rate or 0) >= thr["answer_rate_pass"],
                (answer_rate or 0) >= thr["answer_rate_warn"], "answers reliably",
                f"{answer_rate:.0f}% returned a parseable response inside the budget (incl. "
                f"legitimate empty solutions)" if answer_rate is not None else "n/a")
            if s["replayed"]:
                # Low bid coverage is a strategy fact, not an endpoint fault —
                # warn at most, never fail.
                chk((bid_rate or 0) >= thr["bid_coverage_pass"], True, "bid coverage",
                    f"{bid_rate:.0f}% of answered auctions carried >=1 solution"
                    if bid_rate is not None else "n/a")
            chk(dmiss == 0, (dmiss_rate or 0) <= thr["deadline_miss_warn"], "inside the deadline",
                (f"{dmiss}/{attempted} ({dmiss_rate:.2f}%) deadline misses [DeadlineExceeded: "
                 f"{errors.get('DeadlineExceeded', {})}]") if dmiss else "0 deadline misses")
            if p95 is not None:
                chk(p95 <= budget_ms * thr["latency_pass_frac"],
                    p95 <= budget_ms * thr["latency_warn_frac"], "latency headroom",
                    f"p95 {p95} ms of a {budget_ms:.0f} ms budget ({budget_source}); "
                    f"PASS <= {thr['latency_pass_frac']:g}x, WARN <= {thr['latency_warn_frac']:g}x")
            if s["returned"]:
                chk((valid_rate or 0) >= thr["validity_pass"],
                    (valid_rate or 0) >= thr["validity_warn"], "solutions are valid",
                    (f"{valid_rate:.0f}% of bid auctions had >=1 valid solution "
                     f"[{validity_basis}; fairness_filtered {s.get('fairness_filtered', 0)}, "
                     f"zero_surplus {s.get('valid_zero_surplus', 0)}, "
                     f"udcp {s.get('udcp_checked', 0)} checked / "
                     f"{s.get('udcp_violations', 0)} violations]")
                    if valid_rate is not None else "n/a")
                # the verdict reads the ex-artefact capture (the headline
                # itself when no artefact was found); both numbers print
                chk((capture_ex or 0) >= thr["capture_pass"], (capture_ex or 0) > thr["capture_warn"],
                    "competitive vs winners",
                    (f"{capture_ex:.0f}% of winner surplus captured (coverage-adjusted; "
                     f"{capture_cond_ex:.0f}% conditional on answering; basis mix {basis_mix})"
                     + (f"; {len(artefacts)} valuation artefact(s) excluded — "
                        f"{capture:.0f}% / {capture_cond:.0f}% including them"
                        if artefacts and capture is not None and capture_cond is not None else ""))
                    if capture_ex is not None and capture_cond_ex is not None else "n/a")
            elif s["replayed"]:
                # Healthy endpoint that never bid: nothing to assess — that is
                # a strategy fact, not an endpoint failure, but it also cannot
                # evidence readiness.
                chk(False, True, "competitive vs winners",
                    "no bids returned on any answered auction — nothing to assess")
            if s["implausible"]:
                chk(False, True, "prices look plausible",
                    f"{s['implausible']} auction(s) flagged implausible_surplus")
            if artefacts:
                # A reference-price artefact in the WINNER data (the mirror of
                # 'prices look plausible', which is about ours). The verdict
                # already reads the ex-artefact capture; this row makes sure a
                # human sees what was excluded, so the window is REVIEW at best.
                chk(False, True, "winner surplus plausible",
                    f"{len(artefacts)} auction(s) excluded from the capture verdict as valuation "
                    f"artefact(s) — " + "; ".join(
                        f"auction {a['auction_id']}: {_fmt_native(a['winner_surplus_wei'])} {nat} "
                        f"winner surplus, {a['ratio']:g}x the rest of the attempted window combined"
                        + (f" (competition referenceScore {a['winner_reference_score']})"
                           if a.get("winner_reference_score") is not None else "")
                        for a in artefacts)
                    + f" [rule: > {ARTEFACT_RATIO}x the rest, >= {ARTEFACT_MIN_ATTEMPTED} attempted]")
            # Coverage disclosure: a verdict against a partial field is only
            # meaningful if the reader can see how partial. Never a hard fail
            # (it's environmental, not the solver's doing) — but excluded
            # above the table fraction or ANY unscanned span downgrades to
            # REVIEW so the number gets read.
            if unscanned:
                span = (st.get("to_block", 0) - st.get("from_block", 0) + 1) or 1
                chk(False, True, "scan coverage",
                    f"{unscanned} of {span} blocks in the window were NOT scanned "
                    f"(RPC getLogs failures) — the field is under-counted")
            else:
                chk(True, True, "scan coverage", "every block in the window was scanned")
            if found:
                chk(excl <= thr["field_coverage_excluded_max_frac"] * found, True, "field coverage",
                    f"{found} settlements, {n_found} auctions formed, "
                    f"{excl} excluded ({100 * excl / found:.0f}% of field; top reasons "
                    f"{top_excl})")
            if clamped:
                chk(False, True, "bodies unmodified",
                    f"--clamp-validto extended expired validTo on {clamped} auction(s) "
                    f"({(st.get('agg') or {}).get('validto_clamped_orders', 0)} orders); "
                    f"replay bodies differ from the archived auctions")
            if cap:
                # a capped sample is a slice of the field, not the field
                chk(False, True, "sample capped",
                    f"--max-auctions {cap}: a capped sample cannot be READY")
        verdict = ("READY" if all(c["level"] == "ok" for c in checks)
                   else "NOT READY" if any(c["level"] == "fail" for c in checks)
                   else "REVIEW")
        # Evidence floor: READY is a strong claim — a thin sample must not
        # produce it. Below the floor the best verdict is REVIEW.
        if verdict == "READY" and attempted < min_evidence:
            verdict = "REVIEW"
            chk(False, True, "sufficient evidence",
                f"only {attempted} auction(s) attempted; READY needs >= "
                f"{min_evidence} (min_evidence, {thr_src['min_evidence']})")

        bodies = (getattr(args, "archive_bodies", None) or getattr(args, "bodies_dir", None)
                  or "<bodies-dir: rerun with --archive-bodies DIR to make this reproducible>")
        frm, to = st.get("from_block"), st.get("to_block")
        reproduce = (
            f"cow-backtester --chain {chain} --env {env} --from-block {frm} --to-block {to} "
            f"--bodies-dir {bodies} --solve-timeout {budget_s:g} --min-evidence {min_evidence}"
            + (f" --max-auctions {cap}" if cap else "")
            + (" --compete" if getattr(args, "compete", False) else "")
            + f" \\\n        --rpc-url <rpc> --solver-url {sv['url']} --solver-name {sv['name']} --readiness"
            f"\n    # cow-backtester {VERSION} · engine build sha: "
            + (getattr(args, "engine_sha", None)
               or "<fill in: the endpoint's boot-line git_sha (not exposed over /solve)>"))

        rep = {
            "solver": sv["name"], "url": sv["url"], "verdict": verdict,
            "chain": chain, "env": env,
            "auctions_attempted": attempted, "answered": s["replayed"],
            "bids": s["returned"], "valid": s["valid"],
            "answer_rate_pct": answer_rate, "bid_rate_pct": bid_rate,
            "valid_rate_pct": valid_rate,
            "transport": transport, "deadline_miss": dmiss,
            "transport_rate_pct": transport_rate, "deadline_miss_rate_pct": dmiss_rate,
            "errors": errors,
            "capture_pct": capture, "capture_conditional_pct": capture_cond,
            "capture_ex_artefact_pct": capture_ex,
            "capture_conditional_ex_artefact_pct": capture_cond_ex,
            "artefact_auctions": artefacts,
            "artefact_rule": {"ratio": ARTEFACT_RATIO, "min_attempted": ARTEFACT_MIN_ATTEMPTED,
                              "evaluated": len(ledger) >= ARTEFACT_MIN_ATTEMPTED,
                              "basis": ("winner_surplus_wei > ratio x the rest of the attempted "
                                        "window's winner surplus combined")},
            "per_bid_median_ratio": per_bid_median, "per_bid_ratio_n": len(bid_ratios),
            "per_bid_ratio_basis": PER_BID_RATIO_BASIS,
            "basis_mix": basis_mix,
            "p50_ms": p50, "p95_ms": p95, "max_ms": pmax,
            "latency_basis": ("answered auctions only, late answers included; a dead "
                              "endpoint's fast failures must not flatter p95"),
            "budget_s": budget_s, "budget_source": budget_source,
            "original_budget_upper_s": upper,
            "validity_basis": validity_basis,
            "fairness_filtered": s.get("fairness_filtered", 0),
            "fairness_evaluated": fe, "fairness_not_evaluated": fne,
            "valid_zero_surplus": s.get("valid_zero_surplus", 0),
            "udcp_checked": s.get("udcp_checked", 0),
            "udcp_violations": s.get("udcp_violations", 0),
            "thresholds": thr, "threshold_profile": profile, "threshold_sources": thr_src,
            "min_evidence": min_evidence, "sample_capped": cap,
            "window": {"from_block": frm, "to_block": to,
                       "span_start_ts": span_start, "span_end_ts": span_end,
                       "span_hours": round(span_hours, 3) if span_hours is not None else None,
                       "ts_basis": ts_basis},
            "auctions_per_hour": auctions_per_hour,
            "counts": {"settlements_found": found, "auctions_formed": n_found,
                       "attempted": attempted, "replayed": s["replayed"],
                       "returned": s["returned"]},
            "excluded": {"n": excl, "top": top_excl},
            "implausible": s["implausible"], "checks": checks,
            "unscanned_blocks": unscanned,
            "skipped": dict(sk),
            "validto_clamped_auctions": clamped,
            "reproduce": reproduce,
        }
        reports.append(rep)
        # --quiet silences PROGRESS (stderr), never the verdict: a CI gate
        # redirecting stdout must still get the screen (v0.10.0).
        mark = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}
        print()
        print("=" * 68)
        print(f"  READINESS — {sv['name']}   [{verdict}]")
        print(f"  {chain} · {env} · blocks {frm}..{to}")
        print("=" * 68)
        print(f"  window  : blocks {frm}..{to} | attempted auctions span "
              f"{_iso(span_start)} → {_iso(span_end)}"
              + (f" ({span_hours:.2f} h, {ts_basis}; {auctions_per_hour} attempted/h)"
                 if span_hours is not None else f" ({ts_basis})"))
        print(f"            settlements found {found} / auctions formed {n_found} / attempted "
              f"{attempted} / replayed {s['replayed']} / returned {s['returned']}")
        print(f"            excluded {excl}" + (f" — top: {top_excl}" if top_excl else ""))
        print(f"  budget  : {budget_s:.3f} s ({budget_source}"
              + (f", BUDGETS_S[{chain}].settle" if budget_source == "observed" else "")
              + ")"
              + (f" | original-deadline upper bound p50 {upper['p50_s']:.3f} s / p95 "
                 f"{upper['p95_s']:.3f} s over {upper['n']} attempted"
                 if upper else " | original-deadline upper bound n/a (needs --compete)"))
        ov = {k for k, v in thr_src.items() if v == "override"}
        print(f"  thresholds: profile '{profile}' — "
              f"answer_rate >={thr['answer_rate_pass']:g}/{thr['answer_rate_warn']:g}% · "
              f"transport 0/<={thr['transport_warn']:g}% · "
              f"deadline_miss 0/<={thr['deadline_miss_warn']:g}% · "
              f"latency p95 <={thr['latency_pass_frac']:g}x/{thr['latency_warn_frac']:g}x budget · "
              f"validity >={thr['validity_pass']:g}/{thr['validity_warn']:g}% · "
              f"capture >={thr['capture_pass']:g}/>{thr['capture_warn']:g}% · "
              f"min_evidence {min_evidence}"
              + (f" (override: {', '.join(sorted(ov))})" if ov else ""))
        if cap:
            print(f"  SAMPLE CAPPED (--max-auctions {cap}): a capped sample cannot be READY")
        if attempted < min_evidence:
            print(f"  INSUFFICIENT SAMPLE (attempted {attempted} < {min_evidence})")
        for c in checks:
            print(f"  [{mark[c['level']]}] {c['label']:<24} {c['detail']}")
        print("  " + "-" * 64)
        print(f"  answered            : {s['replayed']}/{attempted}"
              + (f"  ({answer_rate:.0f}%)" if answer_rate is not None else "")
              + f"   bids: {s['returned']}   transport: {transport}   deadline misses: {dmiss}")
        if p50 is not None:
            print(f"  latency             : p50 {p50} ms / p95 {p95} ms / max {pmax} ms"
                  f"   (budget {budget_ms:.0f} ms; answered-only incl. late answers)")
        if capture is not None:
            print(f"  surplus vs winners  : {capture:.0f}% captured (coverage-adjusted)"
                  + (f" / {capture_cond:.0f}% conditional" if capture_cond is not None else "")
                  + f"   ({s['valid']}/{s['replayed']} valid)")
        if artefacts and capture_ex is not None:
            print(f"  ex-artefact capture : {capture_ex:.0f}% captured (coverage-adjusted)"
                  + (f" / {capture_cond_ex:.0f}% conditional" if capture_cond_ex is not None else "")
                  + f"   excluding {len(artefacts)} auction(s): "
                  + ", ".join(f"{a['auction_id']} ({a['ratio']:g}x the rest)" for a in artefacts))
        if ledger:
            print("  per-bid median      : "
                  + (f"{per_bid_median:.3f}x ours/winner over {len(bid_ratios)} unflagged bid auction(s)"
                     if per_bid_median is not None else "n/a (no unflagged bid auctions)"))
        if basis_mix:
            print(f"  winner basis mix    : {basis_mix}")
        print(f"  validity basis      : {validity_basis}")
        if s["errors"]:
            print(f"  errors              : {errors}")
        print()
        print("  Reproduce this run:")
        print("    " + reproduce)
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
    if a.get("competition_fetched") or a.get("competition_missing"):
        print(f"  competition records   : {a['competition_fetched']} fetched / "
              f"{a['competition_missing']} missing (404 = winnerless or evicted; "
              f"transient failures are not distinguished by the API)")
    if a.get("archive_error"):
        print(f"  ⚠ archive writes failed: {a['archive_error']} (see stderr)")
    if a.get("validto_clamped_auctions"):
        print(f"  ⚠ bodies modified      : --clamp-validto extended validTo on "
              f"{a['validto_clamped_auctions']} auction(s) / {a['validto_clamped_orders']} order(s)")
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
        failed = s.get("transport", 0) + s.get("deadline_miss", 0)
        print()
        print("=" * 68)
        print(f"  COUNTERFACTUAL — {sv['name']}  ({s['replayed']} replayed"
              + (f", {s.get('transport', 0)} transport + {s.get('deadline_miss', 0)} "
                 f"deadline-miss/EXCLUDED" if failed else "") + ")")
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
        print(f"  our surplus (sum)   : {_fmt_native(s['our_surplus'])} {nat}")
        wsa = s.get("winner_surplus_attempted") or s["winner_surplus"]
        print(f"  winners (attempted) : {_fmt_native(wsa)} {nat}  (all auctions sent, "
              f"errors count as zero for us)")
        cap_adj = _pct(s["our_surplus"], wsa)
        cap_cond = _pct(s["our_surplus"], s["winner_surplus"])
        if cap_adj is not None:
            print(f"  capture (adjusted)  : {cap_adj:.1f}% of the winning set "
                  f"(headline; survivorship-safe)")
        if cap_cond is not None and cap_cond != cap_adj:
            print(f"  capture (answered)  : {cap_cond:.1f}%  (only auctions we "
                  f"responded to — diagnostic)")
        if s.get("lost_to_errors"):
            print(f"  lost to errors      : {_fmt_native(s['lost_to_errors'])} {nat} "
                  f"of winner surplus on auctions where we errored/timed out")
        cape = _pct(s.get("our_surplus_exact", 0), s.get("winner_surplus_exact", 0))
        if cape is not None:
            print(f"  capture (exact-basis): {cape:.1f}%  (direct settlements only, all "
                  f"attempted — same scoring basis both sides)")
        econ = economics.aggregate_report(
            s.get("econ_rows") or [], sv["name"],
            budget_cow=getattr(args, "consistency_budget", None),
            self_address=getattr(args, "self_address", None))
        if econ:
            print("  consistency (CIP-85 v2, proxy):")
            print(f"    challenger metric : {econ['challenger_metric']} over "
                  f"{econ['challenger_orders_bid']}/{econ['executed_orders']} "
                  f"executed orders ({econ['auctions']} auctions)")
            floor = ("MET" if econ["win_floor_met"]
                     else "NOT met — zero-win chains pay ZERO")
            print(f"    pool share        : {econ['challenger_share_pct']}%  "
                  f"(win floor {floor})")
            if "consistency_cow_estimate" in econ:
                print(f"    est. consistency  : ~{econ['consistency_cow_estimate']} COW "
                      f"of a {econ['budget_cow']} COW budget")
            if "historical_self_metric" in econ:
                print(f"    historical self   : {econ['historical_self_metric']} "
                      f"(your REAL metric in these records)")
            for e in econ["field_leaderboard"][:4]:
                print(f"    field {e['solver'][:10]}  metric {e['metric']}")
        ft = competition.field_table(s.get("rank_rows") or [])
        if ft:
            elig = s.get("rank_eligible") or (ft["auctions_ranked"] + s.get("rank_no_bid", 0))
            print(f"  field rank          : rank1 {ft['rank1_pct']}% / top3 "
                  f"{ft['top3_pct']}% of {ft['auctions_ranked']} ranked auctions "
                  f"({s.get('rank_no_bid', 0)} answered with no valid bid; "
                  f"{elig} answered with a record), median rank {ft['median_rank']}, "
                  f"median gap to winner {ft['median_gap_bps']} bps  "
                  f"[best single solution vs field scores]")
            for rv in ft["rivals"][:3]:
                print(f"    rival {rv['solver'][:10]}  wins={rv['wins']}  "
                      f"median gap {rv['median_gap_bps']} bps")
        if s["latency"]:
            lat = sorted(s["latency"])
            p50 = lat[len(lat) // 2]
            p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
            print(f"  solve latency       : p50 {p50} ms / p95 {p95} ms"
                  + (f"   ⚠ {s['deadline_miss']} deadline miss(es)" if s.get("deadline_miss") else ""))
        if s["errors"]:
            print(f"  solve errors        : {driver_error_table(s['errors'])}  (excluded from sums)")
        if s["invalid"]:
            print(f"  invalid solutions   : {dict(s['invalid'])}  (excluded from scoring)")
        if s["implausible"]:
            print(f"  ⚠ {s['implausible']} auction(s) flagged implausible_surplus — prices are CLAIMED,")
            print("    not simulated; treat those rows as suspect.")

    # field consistency leaderboard — purely historical, needs no solver
    if getattr(args, "reward_ev", False):
        lb = economics.field_leaderboard(st.get("field_econ") or [],
                                         self_address=getattr(args, "self_address", None))
        print()
        print("=" * 68)
        print("  FIELD CONSISTENCY (CIP-85 v2 metric, historical, score basis)")
        print("=" * 68)
        if not lb:
            print("  no competition records in this window (all 404/missing) — nothing to rank")
        else:
            print(f"  {lb['auctions']} auctions with a record; metric = Σ per-order share of "
                  f"the executed-order surplus pool")
            for e in lb["leaderboard"]:
                print(f"  {e['solver'][:44]:>44} {e['metric']:>10.4f} {e['share_pct']:>7.2f}%")
            if "historical_self_metric" in lb:
                print(f"  your historical metric: {lb['historical_self_metric']} "
                      f"({lb.get('historical_self_share_pct')}% of the pool)")
            print(f"  ({lb['basis']})")

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
        line(f"surplus ({nat})", _fmt_native(s1["our_surplus"]), _fmt_native(s2["our_surplus"]))
        # coverage-adjusted (attempted) denominators, like the headline — the
        # answered-only ratio let a solver that failed half the field tie one
        # that answered everything (v0.10.0)
        w1 = s1.get("winner_surplus_attempted") or s1["winner_surplus"]
        w2 = s2.get("winner_surplus_attempted") or s2["winner_surplus"]
        c1, c2 = _pct(s1["our_surplus"], w1), _pct(s2["our_surplus"], w2)
        line("capture % (adjusted)", "n/a" if c1 is None else f"{c1:.1f}", "n/a" if c2 is None else f"{c2:.1f}")
        line("transport errors", s1.get("transport", 0), s2.get("transport", 0))
        line("deadline misses", s1.get("deadline_miss", 0), s2.get("deadline_miss", 0))
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
        sign = "+" if delta > 0 else ""
        print(f"  → {n1} minus {n2}: {sign}{_fmt_native(delta)} {nat} ({delta:+d} wei)")
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
        wsa = s.get("winner_surplus_attempted") or s["winner_surplus"]
        solvers.append({
            "name": sv["name"], "replayed": s["replayed"],
            "transport": s.get("transport", 0), "deadline_miss": s.get("deadline_miss", 0),
            "failed": s.get("transport", 0) + s.get("deadline_miss", 0),
            "errors": driver_error_table(s.get("errors")),
            "returned": s["returned"], "valid": s["valid"], "positive": s["positive"],
            "beat": s["beat"], "our_surplus": s["our_surplus"],
            # Headline = coverage-adjusted (errors keep the winner in the
            # denominator); conditional kept as a labeled diagnostic.
            "capture_pct": _pct(s["our_surplus"], wsa),
            "capture_conditional_pct": _pct(s["our_surplus"], s["winner_surplus"]),
            "capture_exact_basis_pct": _pct(s.get("our_surplus_exact", 0),
                                            s.get("winner_surplus_exact", 0)),
            "winner_surplus_attempted": wsa,
            "lost_to_errors_wei": s.get("lost_to_errors", 0),
            "implausible": s["implausible"],
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            "field": competition.field_table(s.get("rank_rows") or []),
            "consistency": economics.aggregate_report(
                s.get("econ_rows") or [], sv["name"],
                budget_cow=getattr(args, "consistency_budget", None),
                self_address=getattr(args, "self_address", None)),
        })
    cov = {"settlement txs found": len(st["txs"]),
           "auctions formed": st["n_found"], "auctions scored": len(st["rows"])}
    for k, v in st["skip"].most_common():
        cov[f"skipped: {k}"] = v
    if st["failed_ranges"]:
        cov["unscanned blocks"] = sum(hi - lo + 1 for lo, hi in st["failed_ranges"])
    a = st.get("agg") or {}
    if a.get("competition_fetched") or a.get("competition_missing"):
        cov["competition records fetched/missing"] = (
            f"{a.get('competition_fetched', 0)}/{a.get('competition_missing', 0)}")
    if a.get("validto_clamped_auctions"):
        cov["bodies modified by --clamp-validto (auctions/orders)"] = (
            f"{a['validto_clamped_auctions']}/{a['validto_clamped_orders']}")
    if st["ages"]:
        cov["auction age median (h)"] = round(statistics.median(st["ages"]), 1)
    if st["api_check"]:
        cov["v2 API match/mismatch/unavailable"] = (
            f"{st['api_check']['match']}/{st['api_check']['mismatch']}/{st['api_check']['unavailable']}")
    summary = {
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
        "field_consistency": (economics.field_leaderboard(
            st.get("field_econ") or [], self_address=getattr(args, "self_address", None))
            if getattr(args, "reward_ev", False) else None),
        "head_to_head": extra.get("head_to_head"), "pairs": extra.get("pairs"),
        "readiness": extra.get("readiness"),
        "submitters": [{"address": a, "name": (solver_names_map or {}).get(a),
                        "settlements": v["settlements"], "surplus_wei": v["surplus_wei"]}
                       for a, v in sorted(st["submitters"].items(),
                                          key=lambda kv: -kv[1]["surplus_wei"])[:10]],
        "caveats": CAVEATS,
    }
    # Additive (2026-09-16): present ONLY when --maker-metrics-db ran, so every existing field is untouched.
    if extra.get("maker_metrics") is not None:
        summary["maker_metrics"] = extra["maker_metrics"]
    return summary


# ---------------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(
        prog="cow-backtester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="CoW historical-auction backtester + counterfactual scorecard",
        epilog="Exit codes: 0 success, 1 runtime error, 2 usage error, 4 --fail-on gate tripped, "
               "130 interrupted (partial scorecard printed). Self-tests: python3 -m "
               "cow_backtester.scorer --unittest (offline) / --selftest, "
               "python3 -m cow_backtester --selftest (network).")
    ap.add_argument("--chain", default="arbitrum-one", choices=sorted(CHAINS),
                    help="chain to scan (make this explicit in scripts)")
    ap.add_argument("--env", default="prod", choices=["prod", "staging"],
                    help="CoW environment whose auctions to replay")
    ap.add_argument("--blocks", type=int, default=2000,
                    help="scan the most recent N blocks (head-relative)")
    ap.add_argument("--from-block", type=int, default=None, help="absolute scan start (reproducible)")
    ap.add_argument("--to-block", type=int, default=None, help="absolute scan end; omit to use the chain head")
    ap.add_argument("--max-auctions", type=int, default=0,
                    help="cap auctions scored, newest first (0 = all). A capped "
                         "sample is a slice of the field: --readiness then caps the "
                         "verdict at REVIEW")
    ap.add_argument("--rpc-url", default=None, help="chain RPC (recommended)")
    ap.add_argument("--solver-url", action="append", default=[],
                    help="solver /solve endpoint; repeat for A/B")
    ap.add_argument("--solver-name", action="append", default=[],
                    help="label for the corresponding --solver-url")
    ap.add_argument("--solve-timeout", type=float, default=None,
                    help="OVERRIDE the per-request budget advertised to the solver, "
                         "seconds (HTTP waits +5). Default: the observed driver budget "
                         "for the chain (BUDGETS_S); --readiness refuses to run on a "
                         "chain with no observed value unless this is given")
    ap.add_argument("--workers", type=int, default=8, help="concurrent RPC/S3 fetches")
    ap.add_argument("--cache-dir", default=".cowbt-cache", help="cache directory")
    ap.add_argument("--no-cache", action="store_true", help="disable the cache")
    ap.add_argument("--json-out", default=None, help="stream one JSON row per auction")
    ap.add_argument("--html-out", default=None, help="write a single-file HTML report")
    ap.add_argument("--solver-map", default=None,
                    help="JSON {address: name} to label winning submitters")
    ap.add_argument("--archive-bodies", default=None, metavar="DIR",
                    help="store every auction body used in the run as "
                         "DIR/<chain>/<auction_id>.json.gz plus DIR/manifest.jsonl, so the "
                         "run can be reproduced after the S3 bucket evicts the bodies")
    ap.add_argument("--bodies-dir", default=None, metavar="DIR",
                    help="replay from a --archive-bodies archive instead of S3; a body "
                         "missing from the archive is skipped as body_not_archived, "
                         "never fetched")
    ap.add_argument("--readiness", action="store_true",
                    help="print a one-screen pre-prod readiness check for the "
                         "solver endpoint(s) instead of the full field scorecard "
                         "(answer rate, latency, validity, surplus vs winners)")
    ap.add_argument("--min-evidence", type=int, default=None,
                    help="OVERRIDE the minimum attempted auctions before --readiness "
                         "may say READY (below it the best verdict is REVIEW). "
                         "Default: THRESHOLDS min_evidence (500)")
    ap.add_argument("--fail-on", choices=["not-ready", "review"], default=None,
                    help="with --readiness: exit 4 when any endpoint's verdict is "
                         "NOT READY (not-ready) or REVIEW-or-worse (review) — a CI gate")
    ap.add_argument("--compete", action="store_true",
                    help="fetch each auction's historical competition record "
                         "(v2 API) and rank the challenger against the "
                         "fairness-surviving field (rank, gap to winner, rivals)")
    ap.add_argument("--archive-dir", default=None,
                    help="persist fetched competition records as "
                         "DIR/<chain>/<auction_id>.json.gz (implies fetching; "
                         "builds the local competition dataset)")
    ap.add_argument("--reward-ev", action="store_true",
                    help="estimate CIP-85 v2 consistency economics: the "
                         "challenger's counterfactual metric vs the historical "
                         "field, a field consistency leaderboard, and a COW "
                         "estimate when --consistency-budget is given "
                         "(implies --compete)")
    ap.add_argument("--consistency-budget", type=float, default=None,
                    help="the chain's weekly consistency pool in COW; converts "
                         "the challenger's share into a COW/week estimate")
    ap.add_argument("--self-address", default=None,
                    help="your historical solverAddress — adds shadow-vs-actual "
                         "comparison when it appears in a competition record")
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
    ap.add_argument("--maker-metrics-db", default=None, metavar="SQLITE",
                    help="ADDITIVE maker-facing request metrics section (requests/quotes/bids/wins/fills/req-per-fill/"
                         "no-stream per maker x pair x lane, weekly) from the engine's intelligence SQLite; absent = "
                         "the report is unchanged")
    ap.add_argument("--maker-metrics-logscan", default=None, metavar="JSON",
                    help="optional cached log scan (bebop_request_discipline.py) for the quote lane + no-stream shares")
    ap.add_argument("--maker-metrics-weeks", type=int, default=4, help="ISO weeks back (default 4)")
    ap.add_argument("--maker-metrics-maker", default="bebop", help="maker/backend name (default bebop)")
    ap.add_argument("--version", action="version", version=f"cow-backtester {VERSION}")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    global QUIET
    QUIET = args.quiet

    if args.blocks <= 0:
        ap.error("--blocks must be > 0")
    if args.max_auctions < 0:
        ap.error("--max-auctions must be >= 0")
    if args.solve_timeout is not None and args.solve_timeout <= 0:
        ap.error("--solve-timeout must be > 0 seconds")
    if args.min_evidence is not None and args.min_evidence < 1:
        ap.error("--min-evidence must be >= 1")
    if args.archive_bodies and args.bodies_dir:
        ap.error("--archive-bodies and --bodies-dir are exclusive (an archive replays from itself)")
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

    # The per-request budget, resolved ONCE and printed everywhere it matters.
    # --readiness on a chain with no observed budget and no override exits 2
    # here, before any network call (v0.11.0).
    args.budget_s, args.budget_source = resolve_budget(
        args.chain, args.solve_timeout, readiness=bool(args.readiness and args.solvers))
    if args.solvers:
        say(f"      per-request budget {args.budget_s:g} s ({args.budget_source})")

    solver_names_map = None
    if args.solver_map:
        try:
            solver_names_map = {k.lower(): v for k, v in json.load(open(args.solver_map)).items()}
        except Exception as e:
            print(f"ERROR: cannot read --solver-map {args.solver_map}: {e}", file=sys.stderr)
            sys.exit(1)

    jout = None
    if args.json_out:
        # v0.11.1: under --watch the file is APPENDED to, so a restart continues
        # the same JSON Lines file instead of truncating the previous run (one
        # _meta line per cycle marks the window boundaries; the first cycle
        # after a restart re-scans --blocks from head, so consumers dedupe by
        # auction_id). A one-shot run still starts a fresh file.
        mode = "a" if args.watch else "w"
        try:
            needs_newline = False
            if mode == "a" and os.path.exists(args.json_out) and os.path.getsize(args.json_out) > 0:
                # a previous run killed mid-line must not get our first row
                # glued onto its partial last line
                with open(args.json_out, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    needs_newline = f.read(1) != b"\n"
            jout = open(args.json_out, mode)
            if needs_newline:
                jout.write("\n")
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

    if getattr(args, "reward_ev", False):
        args.compete = True
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
        if getattr(args, "maker_metrics_db", None):
            from . import maker_metrics
            extra = dict(extra or {})
            extra["maker_metrics"] = maker_metrics.compute(
                args.maker_metrics_db, weeks=args.maker_metrics_weeks, maker=args.maker_metrics_maker,
                logscan_path=args.maker_metrics_logscan)
            say(maker_metrics.render_text(extra["maker_metrics"]))

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
                "budget_s": args.budget_s, "budget_source": args.budget_source,
                "bodies_source": st.get("bodies_source"),
                "archive_bodies": args.archive_bodies, "bodies_dir": args.bodies_dir,
                "max_auctions": args.max_auctions,
                "threshold_profile": resolve_thresholds(args.chain, args.min_evidence)[1],
                "settlement_txs_found": len(st["txs"]),
                "auctions_formed": st["n_found"], "rows": len(st["rows"]),
                "skipped": dict(st["skip"]),
                "failed_ranges": [list(r) for r in st["failed_ranges"]],
                "unscanned_blocks": sum(hi - lo + 1 for lo, hi in st["failed_ranges"]),
                "competition_missing": st["agg"].get("competition_missing", 0),
                "validto_clamped_auctions": st["agg"].get("validto_clamped_auctions", 0),
                # v0.11.1: rows are streamed before the window total is known,
                # so the per-row line cannot carry the artefact flag — join on
                # auction_id (readiness.artefact_auctions has the full detail)
                "winner_surplus_artefacts": [
                    {k: a[k] for k in ("auction_id", "winner_surplus_wei", "ratio")}
                    for a in winner_surplus_artefacts(
                        [{"auction_id": r["auction_id"], "winner_surplus_wei": r["winner_surplus_wei"]}
                         for r in st["rows"]])],
                "caveats": CAVEATS}}) + "\n")
            jout.flush()
            say(f"  {'appended' if args.watch else 'wrote'} {len(st['rows'])} rows + 1 _meta line "
                f"-> {args.json_out}")

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
    if args.readiness and args.solvers and gate_failed([r["verdict"] for r in readiness],
                                                        getattr(args, "fail_on", None)):
        print(f"readiness gate --fail-on {args.fail_on}: tripped", file=sys.stderr)
        sys.exit(4)


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
    if not body:
        # The S3 bucket keeps ~1 month; the pinned auction ages out of it.
        # Every invariant below is generic, so pick a recent direct settle()
        # whose body is still served instead of failing on data expiry.
        print("pinned auction left the S3 retention window; picking a recent settlement",
              file=sys.stderr)
        head = int(scorer.rpc(rpcs, "eth_blockNumber", []), 16)
        recent, _failed = enumerate_settlements(rpcs, head - 3000, head)
        s = dec = ev = body = None
        for cand in reversed(recent):
            cs = scorer.fetch_settlement(rpcs, cand)
            if not cs or cs["status"] != "0x1" or cs["calldata"][:10].lower() != scorer.SETTLE_SELECTOR:
                continue
            try:
                cdec = scorer.decode_settlement(cs["calldata"])
            except Exception:
                continue
            cev = scorer.trade_events(cs["logs"])
            if cdec["auction_id"] is None or len(cev) != len(cdec["trades"]) or not cev:
                continue
            cbody = s3_auction("prod", "arbitrum-one", cdec["auction_id"])
            if cbody:
                s, dec, ev, body, tx = cs, cdec, cev, cbody, cand
                break
        assert body, "no recent settlement with an S3 body found — is the bucket reachable?"
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
