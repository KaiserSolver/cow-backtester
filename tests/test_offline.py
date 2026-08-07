"""Offline test suite for cow-backtester — no network, ever.

Everything runs against pinned fixtures: the reference settlement calldata +
Trade-event log (Arbitrum auction 8339027, checked against on-chain data,
Aug 2026) and a trimmed auction body. Covers the scoring
math, the winner reconstruction path, the full challenger-validation matrix
(duplicate trades, fee padding, negative amounts, malformed shapes), the
cache, and USD-rate derivation.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cow_backtester import backtest, scorer  # noqa: E402
from cow_backtester.cache import Cache  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")

# Pinned reference values (see docs/DESIGN_NOTES.md).
AUCTION_ID = 8339027
EVENT_BUY_AMOUNT = 2385773       # on-chain Trade event, exact
UNIFORM_SURPLUS_ATOMS = 1471     # before-fee surplus at uniform prices
FEE_TAKE_ATOMS = 805
WINNER_SURPLUS_WEI = 767957704005


@pytest.fixture(scope="module")
def settlement():
    return json.load(open(os.path.join(FIX, "settlement_8339027.json")))


@pytest.fixture(scope="module")
def body():
    return json.load(open(os.path.join(FIX, "body_8339027_trimmed.json")))


@pytest.fixture(scope="module")
def decoded(settlement):
    return scorer.decode_settlement(settlement["calldata"])


@pytest.fixture(scope="module")
def events(settlement):
    return scorer.trade_events(settlement["logs"])


@pytest.fixture(scope="module")
def ref_prices(body):
    return {k.lower(): int(v["referencePrice"]) for k, v in body["tokens"].items()
            if v.get("referencePrice") is not None}


# ------------------------------------------------------------- surplus math

def test_surplus_math_vectors():
    assert scorer.gross_surplus_atoms("sell", 100, 100, 150, 2, 1) == 50
    assert scorer.gross_surplus_atoms("buy", 100, 200, 100, 2, 3) == 50
    assert scorer.gross_surplus_atoms("sell", 100, 100, 150, 3, 2) == 0
    assert scorer.gross_surplus_atoms("buy", 100, 200, 100, 1, 2) == 0
    assert scorer.gross_surplus_atoms("sell", 100, 100, 150, 1, 1) < 0
    assert scorer.surplus_token("sell", "a", "b") == "b"
    assert scorer.surplus_token("buy", "a", "b") == "a"


def test_to_u256_strictness():
    assert backtest.to_u256(5) == 5
    assert backtest.to_u256("123") == 123
    assert backtest.to_u256("0xFF") == 255
    for bad in (-1, "-5", "+5", "1_000", True, False, "abc", None, 1.5, ""):
        with pytest.raises((ValueError, TypeError)):
            backtest.to_u256(bad)


# ------------------------------------------------------ winner reconstruction

def test_decode_settlement(decoded):
    assert decoded["auction_id"] == AUCTION_ID
    assert decoded["suffix_len"] == 8
    assert len(decoded["trades"]) == 1
    tr = decoded["trades"][0]
    assert (tr[8] & scorer.FLAG_KIND_BUY) == 0
    assert bool(tr[8] & scorer.FLAG_PARTIALLY_FILLABLE)


def test_decode_rejects_non_settle():
    with pytest.raises(ValueError):
        scorer.decode_settlement("0xdeadbeef" + "00" * 64)


def test_trade_events_address_filter(settlement):
    logs = list(settlement["logs"])
    # a same-topic event from a FOREIGN contract must be ignored
    foreign = dict(logs[0])
    foreign["address"] = "0x1111111111111111111111111111111111111111"
    assert len(scorer.trade_events([foreign] + logs)) == 1
    # anonymous events (no topics) must not crash
    assert scorer.trade_events([{"address": scorer.SETTLEMENT, "topics": [], "data": "0x"}]) == []


def test_custom_price_delivery_matches_event(decoded, events):
    tr = decoded["trades"][0]
    sti, bti, executed = tr[0], tr[1], tr[9]
    pr = decoded["clearing_prices"]
    bought = -(-executed * pr[sti] // pr[bti])
    assert bought == events[0]["buy_amount"] == EVENT_BUY_AMOUNT


def test_winner_surplus_from_fixtures(decoded, events, body, ref_prices):
    w = scorer.winner_settlement_surplus(decoded, events, body, ref_prices)
    assert "error" not in w
    assert w["total_surplus_wei"] == WINNER_SURPLUS_WEI
    pt = w["per_trade"][0]
    usdt = pt["buy"]
    assert pt["surplus_wei"] == UNIFORM_SURPLUS_ATOMS * ref_prices[usdt] // 10**18
    assert pt["fee_wei"] == FEE_TAKE_ATOMS * ref_prices[usdt] // 10**18
    assert w["uid_overlap"] == 1 and w["jit_excluded"] == 0 and w["anomalies"] == 0


def test_winner_event_mismatch_is_error(decoded, body, ref_prices):
    w = scorer.winner_settlement_surplus(decoded, [], body, ref_prices)
    assert w.get("error") == "trade_event_mismatch"


# --------------------------------------------------- challenger validation

def _mk(body, decoded, events, ps, pb, uid=None, executed=None, fee="0"):
    toks = decoded["tokens"]
    tr = decoded["trades"][0]
    st, bt = toks[tr[0]], toks[tr[1]]
    return {"solutions": [{"id": 0, "prices": {st: ps, bt: pb},
                           "trades": [{"kind": "fulfillment",
                                       "order": uid or events[0]["uid"],
                                       "executedAmount": executed if executed is not None else str(tr[9]),
                                       "fee": fee}],
                           "interactions": [], "gas": 150000}]}


@pytest.fixture(scope="module")
def uniform(decoded):
    toks = decoded["tokens"]
    tr = decoded["trades"][0]
    u_si, u_bi = scorer.uniform_price_indices(toks, toks[tr[0]], toks[tr[1]])
    return str(decoded["clearing_prices"][u_si]), str(decoded["clearing_prices"][u_bi])


def test_honest_replication_scores_winner(body, decoded, events, ref_prices, uniform):
    vs = backtest.validate_and_score(_mk(body, decoded, events, *uniform), body, ref_prices)
    assert vs["n_valid"] == 1
    assert vs["best_surplus_wei"] == WINNER_SURPLUS_WEI
    assert sum(vs["by_pair"].values()) == WINNER_SURPLUS_WEI


def test_hex_and_case_equivalence(body, decoded, events, ref_prices, uniform):
    ps, pb = uniform
    r = _mk(body, decoded, events, hex(int(ps)), hex(int(pb)),
            uid=events[0]["uid"].upper(), executed=hex(decoded["trades"][0][9]))
    vs = backtest.validate_and_score(r, body, ref_prices)
    assert vs["best_surplus_wei"] == WINNER_SURPLUS_WEI


def test_limit_violation_is_invalid(body, decoded, events, ref_prices):
    pb = decoded["clearing_prices"][1]
    vs = backtest.validate_and_score(
        _mk(body, decoded, events, str(pb // 2), str(pb)), body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"limit_violation": 1}


def test_duplicate_trades_invalid(body, decoded, events, ref_prices, uniform):
    r = _mk(body, decoded, events, *uniform)
    r["solutions"][0]["trades"] *= 3
    vs = backtest.validate_and_score(r, body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"duplicate_trade": 1}


def test_fee_padding_invalid(body, decoded, events, ref_prices, uniform):
    o = body["orders"][0]
    avail = int(o["sellAmount"])
    vs = backtest.validate_and_score(
        _mk(body, decoded, events, *uniform, executed="1", fee=str(avail - 1)),
        body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"limit_violation": 1}


def test_negative_amount_invalid(body, decoded, events, ref_prices, uniform):
    vs = backtest.validate_and_score(
        _mk(body, decoded, events, *uniform, executed="-5"), body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"bad_numeric": 1}


@pytest.mark.parametrize("junk", [
    [{"prices": {}, "trades": None}],
    ["garbage"],
    [{"prices": [], "trades": []}],
    [None],
    [{"prices": {"0xaa": "1"}, "trades": ["x"]}],
])
def test_malformed_never_crashes(body, ref_prices, junk):
    vs = backtest.validate_and_score({"solutions": junk}, body, ref_prices)
    assert vs["n_valid"] == 0


def test_empty_trades_not_valid(body, ref_prices):
    vs = backtest.validate_and_score(
        {"solutions": [{"prices": {}, "trades": []}]}, body, ref_prices)
    assert vs["n_solutions"] == 1 and vs["n_valid"] == 0


def test_pair_collision_no_double_count(body, decoded, events, ref_prices, uniform):
    s = _mk(body, decoded, events, *uniform)["solutions"][0]
    vs = backtest.validate_and_score({"solutions": [s, dict(s)]}, body, ref_prices)
    assert vs["n_valid"] == 2
    assert vs["best_surplus_wei"] == WINNER_SURPLUS_WEI   # same pair: no sum


def test_disjoint_pairs_combine(body, decoded, events, ref_prices, uniform):
    """CIP-67 greedy combination: solutions on DISJOINT pairs sum."""
    s1 = _mk(body, decoded, events, *uniform)["solutions"][0]
    synth = body["orders"][1]        # WETH->USDC synthetic order
    weth, usdc = synth["sellToken"].lower(), synth["buyToken"].lower()
    # price it at 1.2x the limit rate: ps/pb = 1.2 * buyAmount/sellAmount
    ps, pb = 6 * int(synth["buyAmount"]), 5 * int(synth["sellAmount"])
    s2 = {"id": 1, "prices": {weth: str(ps), usdc: str(pb)},
          "trades": [{"kind": "fulfillment", "order": synth["uid"],
                      "executedAmount": synth["sellAmount"], "fee": "0"}],
          "interactions": [], "gas": 100000}
    vs = backtest.validate_and_score({"solutions": [s1, s2]}, body, ref_prices)
    assert vs["n_valid"] == 2
    assert vs["best_surplus_wei"] > vs["best_single_wei"]   # disjoint: summed
    assert len(vs["by_pair"]) == 2


def test_jit_trades_ignored_not_invalid(body, decoded, events, ref_prices, uniform):
    r = _mk(body, decoded, events, *uniform)
    r["solutions"][0]["trades"].append({"kind": "jit", "order": {"sellToken": "0xaa"},
                                        "executedAmount": "1"})
    vs = backtest.validate_and_score(r, body, ref_prices)
    assert vs["n_valid"] == 1 and vs["jit_ignored"] == 1
    assert vs["best_surplus_wei"] == WINNER_SURPLUS_WEI


# ------------------------------------------------------------------- sizing

def test_derive_native_usd(body):
    rate = backtest.derive_native_usd(body, "arbitrum-one")
    assert rate is not None and 100 < rate < 100_000


def test_derive_native_usd_no_stables(body):
    assert backtest.derive_native_usd({"tokens": {}}, "arbitrum-one") is None


def test_size_bucket_edges():
    assert backtest.size_bucket(None) == "unknown"
    assert backtest.size_bucket(0) == "$0-100"
    assert backtest.size_bucket(100) == "$100-1k"
    assert backtest.size_bucket(2_000_000) == "$1M+"


# -------------------------------------------------------------------- cache

def test_cache_roundtrip(tmp_path):
    c = Cache(str(tmp_path), "testchain")
    assert c.get("kind", "k") is None
    c.put("kind", "k", {"a": [1, 2]})
    assert c.get("kind", "k") == {"a": [1, 2]}
    assert c.stats()["writes"] == 1 and c.stats()["hits"] == 1


def test_cache_disabled(tmp_path):
    c = Cache(str(tmp_path), "testchain", enabled=False)
    c.put("kind", "k", {"a": 1})
    assert c.get("kind", "k") is None
    assert not any(tmp_path.iterdir())


def test_cache_key_sanitization(tmp_path):
    c = Cache(str(tmp_path), "testchain")
    c.put("kind", "../../evil/../key", {"ok": True})
    assert c.get("kind", "../../evil/../key") == {"ok": True}
    # separators are flattened: exactly one file, landing exactly in
    # <root>/testchain/kind/ (dots without separators cannot escape)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(files) == 1
    assert files[0].parent == tmp_path / "testchain" / "kind"
    assert "/" not in files[0].name and "\\" not in files[0].name


# ------------------------------------------------ buy-order validation matrix

@pytest.fixture(scope="module")
def buy_body(body):
    """Body extended with a synthetic partiallyFillable BUY order (WETH sold
    for USDC bought) — the buy branch of the validation matrix."""
    import copy
    b = copy.deepcopy(body)
    o = dict(b["orders"][1])
    o["uid"] = "0x" + "cd" * 56
    o["kind"] = "buy"
    o["sellToken"] = b["orders"][1]["sellToken"]     # WETH
    o["buyToken"] = b["orders"][1]["buyToken"]       # USDC
    o["sellAmount"] = "1000000000000000000"          # willing to sell up to 1 WETH
    o["buyAmount"] = "3000000000"                    # to buy exactly 3000 USDC
    o["fullSellAmount"] = o["sellAmount"]
    o["fullBuyAmount"] = o["buyAmount"]
    o["partiallyFillable"] = False
    b["orders"].append(o)
    return b


def _mk_buy(buy_body, ps, pb, executed, fee="0"):
    o = buy_body["orders"][2]
    return {"solutions": [{"id": 0,
                           "prices": {o["sellToken"]: ps, o["buyToken"]: pb},
                           "trades": [{"kind": "fulfillment", "order": o["uid"],
                                       "executedAmount": executed, "fee": fee}],
                           "interactions": [], "gas": 100000}]}


def test_buy_order_at_limit_valid_zero(buy_body, ref_prices):
    # sold = executed * pb/ps == limit exactly -> valid, zero surplus
    vs = backtest.validate_and_score(
        _mk_buy(buy_body, "3000000000", "1000000000000000000", "3000000000"),
        buy_body, ref_prices)
    assert vs["n_valid"] == 1 and vs["best_surplus_wei"] == 0


def test_buy_order_better_than_limit_positive(buy_body, ref_prices):
    # sell only half as much per unit bought -> positive surplus (sell token)
    vs = backtest.validate_and_score(
        _mk_buy(buy_body, "6000000000", "1000000000000000000", "3000000000"),
        buy_body, ref_prices)
    assert vs["n_valid"] == 1 and vs["best_surplus_wei"] > 0


def test_buy_order_limit_violation(buy_body, ref_prices):
    # sold exceeds the limit -> invalid
    vs = backtest.validate_and_score(
        _mk_buy(buy_body, "1500000000", "1000000000000000000", "3000000000"),
        buy_body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"limit_violation": 1}


def test_buy_order_fee_bounded(buy_body, ref_prices):
    # at-limit fill + any positive fee overruns the sell limit -> invalid
    vs = backtest.validate_and_score(
        _mk_buy(buy_body, "3000000000", "1000000000000000000", "3000000000",
                fee="1000000000000000"),
        buy_body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"limit_violation": 1}


def test_buy_order_fok_mismatch(buy_body, ref_prices):
    vs = backtest.validate_and_score(
        _mk_buy(buy_body, "6000000000", "1000000000000000000", "1500000000"),
        buy_body, ref_prices)
    assert vs["n_valid"] == 0 and vs["invalid"] == {"fok_mismatch": 1}


# ------------------------------------- enumerate_settlements range splitting

def test_enumerate_splits_failing_ranges(monkeypatch):
    """A wide getLogs failure must split and retry; whatever still fails
    must come back in failed_ranges rather than reading as zero results."""
    calls = []

    def fake_rpc(urls, method, params, tries=3):
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        calls.append((lo, hi))
        if hi - lo > 100:
            return None          # RPC refuses wide ranges
        if lo <= 150 <= hi:
            return [{"transactionHash": "0xabc", "blockNumber": hex(150)}]
        return []

    monkeypatch.setattr(scorer, "rpc", fake_rpc)
    txs, failed = backtest.enumerate_settlements(["u"], 100, 899)
    assert txs == ["0xabc"]
    assert failed == []
    assert any(hi - lo > 100 for lo, hi in calls)    # wide attempts happened...
    assert any(hi - lo <= 100 for lo, hi in calls)   # ...and splitting recovered them
    # (failed == [] above is the load-bearing claim: every failure was recovered)


def test_enumerate_reports_unrecoverable_ranges(monkeypatch):
    monkeypatch.setattr(scorer, "rpc", lambda *a, **k: None)
    txs, failed = backtest.enumerate_settlements(["u"], 100, 899)
    assert txs == []
    assert failed and sum(hi - lo + 1 for lo, hi in failed) == 800


# -------------------------------------------------------------- html report

def test_report_render_utf8(tmp_path):
    from cow_backtester import report
    out = tmp_path / "r.html"
    summary = {"chain": "base", "env": "prod", "native": "ETH", "version": "test",
               "from_block": 1, "to_block": 2, "coverage": {"auctions scored": 1},
               "field_surplus_wei": 10**15, "field_fees_wei": 0,
               "buckets": [{"label": "$0-100", "trades": 1, "surplus_wei": 10**15}],
               "solvers": [], "solver_names": [],
               "pairs": [{"pair": "wΞth->ust💱", "trades": 1, "winner_wei": 10**15,
                          "solvers": {}}],
               "submitters": [], "caveats": ["c"], "head_to_head": None}
    report.render(summary, [{}], str(out))
    html = out.read_text(encoding="utf-8")     # must be valid UTF-8
    assert "wΞth-&gt;ust💱" in html or "wΞth" in html   # symbols escaped, not mojibake
    assert "charset='utf-8'" in html
