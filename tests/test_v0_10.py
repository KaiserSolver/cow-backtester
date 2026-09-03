"""Offline regression tests for the 0.10.0 batch (metric correctness and
silent-zero fixes from the 2026-09-03 review). No network: transports are
monkeypatched, fixtures are the pinned reference data.
"""
import gzip
import io
import json
import os
import sys
import time
import urllib.error
from collections import Counter

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cow_backtester import backtest, economics  # noqa: E402

# ------------------------------------------------------------- transport

class _Resp:
    def __init__(self, raw):
        self._raw = raw

    def read(self, n=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code):
    return urllib.error.HTTPError("http://x/solve", code, f"HTTP {code}", {}, io.BytesIO(b""))


@pytest.mark.parametrize("payload", [
    {"error": "internal solver failure"},
    {},
    {"solutions": {"0": {}}},
    {"solution": []},
])
def test_solve_200_without_solutions_list_is_bad_schema(monkeypatch, payload):
    """BT-13: an HTTP-200 that is not a /solve response is an ERROR, not a
    healthy abstention. Only {"solutions": [...]} is an answer."""
    monkeypatch.setattr(backtest.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(json.dumps(payload).encode()))
    resp, err, _ms = backtest.solve("http://x", {"orders": []}, 5)
    assert resp is None and err == "bad_schema"


def test_solve_empty_solutions_is_a_healthy_answer(monkeypatch):
    monkeypatch.setattr(backtest.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b'{"solutions": []}'))
    resp, err, _ms = backtest.solve("http://x", {"orders": []}, 5)
    assert err is None and resp == {"solutions": []}


def test_solve_accepts_preserialized_bytes(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = req.data
        return _Resp(b'{"solutions": []}')
    monkeypatch.setattr(backtest.urllib.request, "urlopen", fake_urlopen)
    payload = json.dumps({"orders": [], "deadline": "x"}).encode()
    backtest.solve("http://x", payload, 5)
    assert seen["body"] == payload   # byte-identical, not re-serialized


def test_dispatch_is_concurrent_and_in_solver_order(monkeypatch):
    """BT-12: every solver gets the same bytes AND the same wall-clock budget.
    Two solvers that each take 0.3 s must finish in well under 0.6 s."""
    def slow_solve(url, payload, timeout):
        time.sleep(0.3)
        return {"solutions": []}, None, 300
    monkeypatch.setattr(backtest, "solve", slow_solve)
    solvers = [{"url": "http://a", "name": "a"}, {"url": "http://b", "name": "b"}]
    t0 = time.monotonic()
    out = backtest.dispatch_solvers(solvers, b"{}", 5)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, elapsed
    assert [r[0]["name"] for r in out] == ["a", "b"]
    assert all(r[2] is None for r in out)


# ------------------------------------------------------------- ranking

def _record():
    return {"auctionId": 1, "solutions": [
        {"solverAddress": "0xW", "score": "1000", "isWinner": True, "filteredOut": False, "orders": []},
        {"solverAddress": "0xR", "score": "900", "isWinner": False, "filteredOut": False, "orders": []},
    ]}


def test_challenger_rank_uses_best_single_solution_not_combined():
    """BT-3: the protocol ranks SOLUTIONS. Three disjoint 400-wei solutions are
    three third-place bids, not one 1200-wei winner."""
    vs = {"best_single_wei": 400, "best_surplus_wei": 1200, "n_valid": 3}
    fr = backtest.challenger_rank(_record(), vs)
    assert fr["rank"] == 3 and fr["beats_winner"] is False
    assert fr["score_wei"] == 400
    assert fr["rank_combined"] == 1 and fr["combined_score_wei"] == 1200
    assert fr["rank_basis"] == "best_single_solution_vs_field_scores"


def test_challenger_rank_single_solution_has_no_combined_field():
    vs = {"best_single_wei": 950, "best_surplus_wei": 950, "n_valid": 1}
    fr = backtest.challenger_rank(_record(), vs)
    assert fr["rank"] == 2 and "rank_combined" not in fr


# ------------------------------------------------------------- economics

def _rec_scores():
    """Order AA: winner X (1-order solution, score 700) and Y (1-order
    solution, score 300). Order BB: X via a 2-order solution (net fallback)."""
    return {"auctionId": 7, "solutions": [
        {"solverAddress": "0xX", "score": "700", "isWinner": True, "filteredOut": False,
         "orders": [{"id": "0xAA", "sellAmount": "1000", "buyAmount": "1300"}]},
        {"solverAddress": "0xY", "score": "300", "isWinner": False, "filteredOut": False,
         "orders": [{"id": "0xAA", "sellAmount": "1000", "buyAmount": "1100"}]},
        {"solverAddress": "0xX", "score": "999", "isWinner": True, "filteredOut": False,
         "orders": [{"id": "0xBB", "sellAmount": "500", "buyAmount": "600"},
                    {"id": "0xCC", "sellAmount": "500", "buyAmount": "600"}]},
    ]}


def _body():
    return {"orders": [
        {"uid": "0xaa", "kind": "sell", "sellAmount": "1000", "buyAmount": "1000",
         "sellToken": "0xS", "buyToken": "0xB"},
        {"uid": "0xbb", "kind": "sell", "sellAmount": "500", "buyAmount": "500",
         "sellToken": "0xS", "buyToken": "0xB"},
        {"uid": "0xcc", "kind": "sell", "sellAmount": "500", "buyAmount": "500",
         "sellToken": "0xS", "buyToken": "0xB"},
    ]}


def test_field_surplus_uses_score_for_single_order_solutions():
    """BT-2: the record's order amounts are net of protocol fees while the
    challenger is scored gross (== the score basis). For a one-order solution
    the solution's `score` IS that order's score-basis surplus; use it."""
    stats = Counter()
    table = economics.executed_order_surpluses(_rec_scores(), _body(), {"0xb": 10**18}, stats=stats)
    assert table["0xaa"] == {"0xx": 700, "0xy": 300}      # score basis, not 300/100 net
    assert table["0xbb"] == {"0xx": 100}                  # 2-order solution: net fallback
    assert table["0xcc"] == {"0xx": 100}
    assert stats["score_basis_orders"] == 2 and stats["net_basis_orders"] == 2


def test_zero_challenger_row_keeps_errored_auctions_in_the_denominator():
    """BT-4: a solve error must not remove the auction from the consistency
    denominator — the field still earned its terms there."""
    table = {"0xaa": {"0xx": 700, "0xy": 300}}
    good = {"field": economics.consistency_terms(table)[0], "challenger": 0.5,
            "challenger_orders": 1, "executed_orders": 1, "challenger_won": False}
    errored = economics.zero_challenger_row(table)
    assert errored["challenger"] == 0.0 and errored["challenger_orders"] == 0
    assert errored["field"] == good["field"]
    with_err = economics.aggregate_report([good, errored], "mine")
    without = economics.aggregate_report([good], "mine")
    assert with_err["challenger_share_pct"] < without["challenger_share_pct"]
    assert with_err["auctions"] == 2


def test_field_leaderboard_needs_no_challenger():
    """BT-31: the field consistency leaderboard is a purely historical
    quantity and must be computable with no solver endpoint at all."""
    rows = [economics.consistency_terms({"0xaa": {"0xx": 700, "0xy": 300}})[0],
            economics.consistency_terms({"0xbb": {"0xx": 100}})[0]]
    lb = economics.field_leaderboard(rows, self_address="0xY")
    assert lb["auctions"] == 2
    assert lb["leaderboard"][0]["solver"] == "0xx"
    assert lb["leaderboard"][0]["metric"] == pytest.approx(1.7)
    assert lb["leaderboard"][0]["share_pct"] == pytest.approx(85.0)
    assert lb["historical_self_metric"] == pytest.approx(0.3)
    assert economics.field_leaderboard([]) is None


# ------------------------------------------------------------- readiness

class _Args:
    def __init__(self, solvers, quiet=False, solve_timeout=15):
        self.solvers = solvers
        self.chain, self.env = "base", "prod"
        self.solve_timeout = solve_timeout
        self.quiet = quiet


def _stat(**over):
    base = {"replayed": 20, "errored": 0, "returned": 20, "valid": 20, "positive": 20,
            "beat": 5, "our_surplus": 600, "winner_surplus": 1000, "implausible": 0,
            "attempted": 20, "winner_surplus_attempted": 1000, "lost_to_errors": 0,
            "invalid": Counter(), "errors": Counter(), "latency": [100] * 20, "late": 0}
    base.update(over)
    return base


def _st(stat, **over):
    st = {"per_solver": {"mine": stat}, "from_block": 1000, "to_block": 8000,
          "txs": list(range(20)), "n_found": 12, "skip": Counter(), "failed_ranges": [],
          "agg": Counter()}
    st.update(over)
    return st


def _labels(rep):
    return {c["label"]: c for c in rep["checks"]}


def test_readiness_reports_unscanned_blocks(capsys):
    """BT-14: 4,000 of 7,001 blocks never scanned must be visible on the
    readiness screen and in its dict, and must not read as full coverage."""
    args = _Args([{"url": "http://x", "name": "mine"}])
    rep = backtest.readiness_report(_st(_stat(), failed_ranges=[(1000, 4999)]), args)[0]
    c = _labels(rep)["scan coverage"]
    assert c["level"] == "warn" and "4000" in c["detail"]
    assert rep["unscanned_blocks"] == 4000
    assert rep["verdict"] != "READY"
    assert "scan coverage" in capsys.readouterr().out


def test_readiness_field_coverage_counts_every_exclusion():
    """BT-14: every skip reason that removes a settlement from the baseline
    counts as excluded — not just two of them. A reverted settlement is not a
    winner and is NOT an exclusion."""
    args = _Args([{"url": "http://x", "name": "mine"}])
    skip = Counter({"wrapper_unattributed": 1, "s3_body_404": 3, "tx_uid_mismatch": 2,
                    "rpc_fetch_failed": 1, "settlement_reverted": 5})
    rep = backtest.readiness_report(_st(_stat(), skip=skip), args)[0]
    c = _labels(rep)["field coverage"]
    assert "7 excluded" in c["detail"] and c["level"] == "warn"
    assert rep["skipped"] == dict(skip)
    assert backtest.exclusion_count(skip) == 7


def test_readiness_prints_under_quiet(capsys):
    """BT-18: --quiet silences progress, never the readiness screen."""
    args = _Args([{"url": "http://x", "name": "mine"}], quiet=True)
    backtest.readiness_report(_st(_stat()), args)
    out = capsys.readouterr().out
    assert "READINESS" in out and "[READY]" in out


def test_readiness_discloses_clamped_bodies():
    """BT-15: a READY produced from modified bodies must say so."""
    args = _Args([{"url": "http://x", "name": "mine"}])
    st = _st(_stat(), agg=Counter({"validto_clamped_auctions": 4, "validto_clamped_orders": 90}))
    rep = backtest.readiness_report(st, args)[0]
    c = _labels(rep)["bodies unmodified"]
    assert c["level"] == "warn" and "4" in c["detail"]


def test_readiness_gate_verdict():
    assert backtest.gate_failed(["READY"], "not-ready") is False
    assert backtest.gate_failed(["REVIEW"], "not-ready") is False
    assert backtest.gate_failed(["NOT READY"], "not-ready") is True
    assert backtest.gate_failed(["REVIEW"], "review") is True
    assert backtest.gate_failed(["READY", "NOT READY"], None) is False


# ------------------------------------------------------------- transport bugs

def test_preflight_404_is_a_wrong_path_not_reachable(monkeypatch):
    """BT-20: 404/405 on POST /solve means the wrong path — the single most
    common user error — and must fail the preflight with the fix spelled out."""
    def raise404(req, timeout=None):
        raise _http_error(404)
    monkeypatch.setattr(backtest.urllib.request, "urlopen", raise404)
    ok, why = backtest.preflight("http://x/solve")
    assert ok is False and "/solve" in why


def test_preflight_other_http_status_is_alive(monkeypatch):
    def raise400(req, timeout=None):
        raise _http_error(400)
    monkeypatch.setattr(backtest.urllib.request, "urlopen", raise400)
    assert backtest.preflight("http://x")[0] is True


def test_s3_gzip_is_bounded_after_decompression(monkeypatch):
    """BT-17: the byte cap must bound the DECOMPRESSED body, not the wire."""
    bomb = gzip.compress(b"0" * 5000)
    monkeypatch.setattr(backtest, "_MAX_HTTP_BYTES", 1000)
    monkeypatch.setattr(backtest, "_http_get", lambda url, timeout=20: bomb)
    errors = Counter()
    assert backtest.s3_auction("prod", "base", 1, errors=errors) is None
    assert errors["s3_bad_body"] == 1


def test_s3_distinguishes_404_from_transient_failure(monkeypatch):
    """BT-16: a 404 is retention; a 503 is a fetch error (retried once)."""
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    calls = Counter()

    def get_404(url, timeout=20):
        calls["n"] += 1
        raise _http_error(404)
    monkeypatch.setattr(backtest, "_http_get", get_404)
    errors = Counter()
    assert backtest.s3_auction("prod", "base", 1, errors=errors) is None
    assert errors["s3_404"] == 1 and calls["n"] == 1

    calls.clear()

    def get_503(url, timeout=20):
        calls["n"] += 1
        raise _http_error(503)
    monkeypatch.setattr(backtest, "_http_get", get_503)
    errors = Counter()
    assert backtest.s3_auction("prod", "base", 1, errors=errors) is None
    assert errors["s3_fetch_error"] == 1 and calls["n"] == 2


def test_s3_good_body_passes_through(monkeypatch):
    raw = gzip.compress(json.dumps({"id": "1", "orders": []}).encode())
    monkeypatch.setattr(backtest, "_http_get", lambda url, timeout=20: raw)
    assert backtest.s3_auction("prod", "base", 1) == {"id": "1", "orders": []}


# ------------------------------------------------------------- small helpers

def test_select_newest_keeps_the_newest_auctions():
    """Replaces the tautological v0.7 test: exercises the production helper."""
    assert backtest.select_newest([100, 200, 300], 2) == [200, 300]
    assert backtest.select_newest([100, 200, 300], 0) == [100, 200, 300]
    assert backtest.select_newest([100, 200], 5) == [100, 200]


def test_fmt_native_never_prints_a_signed_zero_for_a_real_difference():
    """BT-21: sub-microether surpluses must not render as 0.000000."""
    assert backtest._fmt_native(0) == "0.000000"
    assert backtest._fmt_native(123_456_789_012_345_678) == "0.123457"
    small = backtest._fmt_native(2_300_000_000_000)   # 2.3e-6 native
    assert small != "0.000000" and "e" in small
    assert backtest._fmt_native(-2_300_000_000_000).startswith("-")


def test_row_has_no_deprecated_block_alias():
    """0.7.2 promised the `block` alias for one release; 0.10.0 drops it."""
    import inspect
    src = inspect.getsource(backtest.process_window)
    assert '"block": blk' not in src
