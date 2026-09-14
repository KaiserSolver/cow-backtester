"""Offline tests for the 0.11.0 readiness-standard batch (T1–T7 of the
2026-09-14 fixes brief). No network: RPC, S3 and solver transports are
monkeypatched; the pinned Arbitrum fixture (auction 8339027) is the auction.
"""
import copy
import gzip
import json
import os
import sys
import urllib.error
from collections import Counter
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cow_backtester import backtest, competition  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")
SETTLEMENT = json.load(open(os.path.join(FIX, "settlement_8339027.json")))
BODY = json.load(open(os.path.join(FIX, "body_8339027_trimmed.json")))
TX = "0x" + "77" * 32
REF = {k.lower(): int(v["referencePrice"]) for k, v in BODY["tokens"].items()
       if v.get("referencePrice") is not None}


# ------------------------------------------------------------------ helpers

def _args(**over):
    """A full args namespace for process_window/readiness_report (what
    build_parser + main would produce), with test-friendly defaults."""
    base = dict(chain="arbitrum-one", env="prod", from_block=1, workers=1, max_auctions=0,
                solvers=[{"url": "http://x", "name": "mine"}], verify_api=False,
                compete=False, archive_dir=None, reward_ev=False, clamp_validto=False,
                self_address=None, budget_s=4.84, budget_source="observed",
                bodies_dir=None, archive_bodies=None, solve_timeout=None,
                min_evidence=None, quiet=True, max_age_hours=6.0, engine_sha=None)
    base.update(over)
    return SimpleNamespace(**base)


def _uniform_solution():
    """The honest replication of the fixture's winner (one USDC->USDT pair)."""
    from cow_backtester import scorer
    dec = scorer.decode_settlement(SETTLEMENT["calldata"])
    ev = scorer.trade_events(SETTLEMENT["logs"])
    toks, tr = dec["tokens"], dec["trades"][0]
    st, bt = toks[tr[0]], toks[tr[1]]
    u_si, u_bi = scorer.uniform_price_indices(toks, st, bt)
    pr = dec["clearing_prices"]
    return {"id": 0, "prices": {st: str(pr[u_si]), bt: str(pr[u_bi])},
            "trades": [{"kind": "fulfillment", "order": ev[0]["uid"],
                        "executedAmount": str(tr[9]), "fee": "0"}],
            "interactions": [], "gas": 150000}


def _patch_window(monkeypatch, dispatch, record=None, s3=None):
    """Wire process_window to the fixture with no network. The competition
    fetch is ALWAYS patched (record=None → "no record"), so --compete never
    reaches the API."""
    monkeypatch.setattr(backtest, "enumerate_settlements",
                        lambda rpcs, frm, to, step=800: ([TX], []))
    monkeypatch.setattr(backtest, "fetch_settlement_cached",
                        lambda rpcs, tx, cache, mcb: SETTLEMENT)
    monkeypatch.setattr(backtest, "block_ts",
                        lambda rpcs, block, mem, cache, mcb: 1_755_000_000 + (block % 1000))
    monkeypatch.setattr(backtest, "s3_auction",
                        s3 or (lambda env, chain, aid, cache=None, errors=None: copy.deepcopy(BODY)))
    monkeypatch.setattr(backtest, "dispatch_solvers", dispatch)
    monkeypatch.setattr(competition, "fetch_competition",
                        lambda api_base, aid, http_get, cache=None: record)


def _dispatch_ok(ms=120, solutions=None):
    def d(solvers, payload, timeout):
        return [(sv, {"solutions": solutions if solutions is not None else [_uniform_solution()]},
                 None, ms) for sv in solvers]
    return d


def _run(monkeypatch, dispatch, record=None, s3=None, **over):
    _patch_window(monkeypatch, dispatch, record=record, s3=s3)
    args = _args(**over)
    st = backtest.process_window(args, ["rpc"], backtest.CHAINS["arbitrum-one"], 1, 100, None, None)
    return st, args


# ------------------------------------------------------------- T1: budget

def test_budget_default_is_observed_table_value():
    assert backtest.resolve_budget("base") == (4.62, "observed")
    assert backtest.resolve_budget("arbitrum-one") == (4.84, "observed")
    assert backtest.resolve_budget("bnb") == (2.35, "observed")


def test_budget_override_wins():
    assert backtest.resolve_budget("base", 3) == (3.0, "override")
    assert backtest.resolve_budget("linea", 7.5) == (7.5, "override")


def test_budget_readiness_refuses_assumed(capsys):
    with pytest.raises(SystemExit) as e:
        backtest.resolve_budget("linea", None, readiness=True)
    assert e.value.code == 2
    assert "linea" in capsys.readouterr().err
    # a plain replay still runs, labeled
    assert backtest.resolve_budget("linea") == (backtest.ASSUMED_BUDGET_S, "assumed")


def test_cli_readiness_without_observed_budget_exits_2_before_network(monkeypatch, capsys):
    """--chain linea --readiness with no --solve-timeout must exit 2 before
    any RPC call (the RPC is patched to explode to prove it)."""
    from cow_backtester import scorer

    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(scorer, "rpc", boom)
    with pytest.raises(SystemExit) as e:
        backtest.main(["--chain", "linea", "--readiness", "--solver-url", "http://x"])
    assert e.value.code == 2
    assert "BUDGETS_S" in capsys.readouterr().err


def test_parser_defaults_no_cap_no_constant_budget():
    a = backtest.build_parser().parse_args([])
    assert a.max_auctions == 0 and a.solve_timeout is None and a.min_evidence is None
    assert backtest.build_parser().parse_args(["--solve-timeout", "3"]).solve_timeout == 3.0


def test_rows_preserve_original_deadline_and_stamp_budget(monkeypatch):
    st, _ = _run(monkeypatch, _dispatch_ok())
    row = st["rows"][0]
    assert row["original_deadline"] == BODY["deadline"]           # archived value kept
    assert row["replay_deadline"] != row["original_deadline"]
    assert row["budget_s"] == 4.84 and row["budget_source"] == "observed"
    assert row["body_sha256"] == backtest.body_sha256(BODY)         # digest before mutation


def test_original_budget_upper_bound_needs_start_block(monkeypatch):
    rec = {"auctionId": 8339027, "auctionStartBlock": 491743380, "auctionDeadlineBlock": 491743415,
           "solutions": []}
    st, args = _run(monkeypatch, _dispatch_ok(), compete=True)
    assert "original_budget_upper_s" not in st["rows"][0]       # no record: unknown
    st, args = _run(monkeypatch, _dispatch_ok(), record=rec, compete=True)
    row = st["rows"][0]
    assert isinstance(row["original_budget_upper_s"], float)
    assert "upper bound" in row["original_budget_upper_basis"].lower()
    assert st["budget_upper"] == [row["original_budget_upper_s"]]
    rep = backtest.readiness_report(st, args)[0]
    assert rep["original_budget_upper_s"]["n"] == 1
    assert rep["budget_s"] == 4.84 and rep["budget_source"] == "observed"


def test_parse_iso_ts_handles_nanoseconds():
    t = backtest.parse_iso_ts("2026-08-11T00:02:12.956731486Z")
    assert t == pytest.approx(1786406532.956731, abs=1e-3)
    assert backtest.parse_iso_ts("2026-08-11T00:02:12Z") == 1786406532.0
    assert backtest.parse_iso_ts("garbage") is None and backtest.parse_iso_ts(None) is None


# ------------------------------------------------------ T2: sample floor/window

def _stat(**over):
    base = {"replayed": 20, "transport": 0, "deadline_miss": 0, "returned": 20, "valid": 20,
            "positive": 20, "beat": 5, "our_surplus": 600, "winner_surplus": 1000,
            "implausible": 0, "attempted": 20, "winner_surplus_attempted": 1000,
            "lost_to_errors": 0, "invalid": Counter(), "errors": Counter(),
            "latency": [100] * 20, "basis_mix": Counter({"exact_uniform": 20})}
    base.update(over)
    return base


def _st(stat, **over):
    st = {"per_solver": {"mine": stat}, "from_block": 1000, "to_block": 8000,
          "txs": list(range(20)), "n_found": 20, "skip": Counter(), "failed_ranges": [],
          "agg": Counter(), "attempted_ts": [1_755_000_000 + 60 * i for i in range(20)],
          "attempted_start_ts": [], "budget_upper": []}
    st.update(over)
    return st


def _ready_args(**over):
    base = dict(chain="base", budget_s=4.62, min_evidence=10)
    base.update(over)
    return _args(**base)


def test_capped_sample_cannot_be_ready(capsys):
    rep = backtest.readiness_report(_st(_stat()), _ready_args(max_auctions=5))[0]
    assert rep["verdict"] == "REVIEW" and rep["sample_capped"] == 5
    assert any(c["label"] == "sample capped" and c["level"] == "warn" for c in rep["checks"])
    assert "SAMPLE CAPPED (--max-auctions 5)" in capsys.readouterr().out


def test_thin_sample_is_review_with_insufficient_line(capsys):
    # 20 attempted, everything ok, table floor 500 → REVIEW
    rep = backtest.readiness_report(_st(_stat()), _ready_args(min_evidence=None))[0]
    assert rep["verdict"] == "REVIEW" and rep["min_evidence"] == 500
    out = capsys.readouterr().out
    assert "INSUFFICIENT SAMPLE (attempted 20 < 500)" in out


def test_healthy_uncapped_full_sample_is_ready():
    s = _stat(replayed=600, returned=600, valid=600, positive=600, attempted=600,
              latency=[100] * 600, basis_mix=Counter({"exact_uniform": 600}))
    st = _st(s, txs=list(range(600)), n_found=600,
             attempted_ts=[1_755_000_000 + 30 * i for i in range(600)])
    rep = backtest.readiness_report(st, _ready_args(min_evidence=None))[0]
    assert rep["verdict"] == "READY" and rep["sample_capped"] == 0


def test_window_printed_three_ways_and_auctions_per_hour(capsys):
    rep = backtest.readiness_report(_st(_stat()), _ready_args())[0]
    w = rep["window"]
    assert w["from_block"] == 1000 and w["to_block"] == 8000
    assert w["span_hours"] == pytest.approx(19 / 60, abs=1e-3)
    assert w["ts_basis"] == "settlement-block timestamps"
    assert rep["auctions_per_hour"] == pytest.approx(20 / (19 / 60), abs=0.1)
    assert rep["counts"] == {"settlements_found": 20, "auctions_formed": 20,
                             "attempted": 20, "replayed": 20, "returned": 20}
    out = capsys.readouterr().out
    assert "window  : blocks 1000..8000" in out and "attempted/h" in out
    assert "settlements found 20 / auctions formed 20 / attempted 20" in out


def test_excluded_top_three_reasons():
    skip = Counter({"s3_404": 5, "tx_uid_mismatch": 3, "rpc_fetch_failed": 2,
                    "decode_failed": 1, "settlement_reverted": 9})
    rep = backtest.readiness_report(_st(_stat(), skip=skip), _ready_args())[0]
    assert rep["excluded"]["n"] == 11
    assert [k for k, _ in rep["excluded"]["top"]] == ["s3_404", "tx_uid_mismatch", "rpc_fetch_failed"]


# --------------------------------------------------- T3: deadline-miss taxonomy

def test_classify_outcome_table():
    assert backtest.classify_outcome("timeout", 5000, 1000) == ("deadline_miss", "timeout")
    assert backtest.classify_outcome(None, 2000, 1000) == ("deadline_miss", "late")
    assert backtest.classify_outcome(None, 900, 1000) == ("answered", None)
    assert backtest.classify_outcome("http_502", 50, 1000) == ("transport", "http_502")
    for r in ("unreachable", "unreachable (OSError)", "response_too_large", "bad_json", "bad_schema"):
        assert backtest.classify_outcome(r, 10, 1000)[0] == "transport"


def test_driver_labels():
    assert backtest.driver_error_table(Counter({"timeout": 2, "late": 1, "http_502": 3,
                                                "unreachable": 1, "bad_json": 1,
                                                "bad_schema": 1, "bad_solver_response": 2})) == {
        "DeadlineExceeded": {"timeout": 2, "late": 1},
        "SolverHttpError": {"http_502": 3, "unreachable": 1},
        "SolverDeserializeError": {"bad_json": 1},
        "SolverDtoError": {"bad_schema": 1, "bad_solver_response": 2},
    }


def test_socket_timeout_is_a_deadline_miss_not_transport(monkeypatch):
    """A real socket timeout through solve() lands in deadline_miss /
    DeadlineExceeded — the driver got nothing by the deadline."""
    def urlopen_timeout(req, timeout=None):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(backtest.urllib.request, "urlopen", urlopen_timeout)
    st, args = _run(monkeypatch, backtest.dispatch_solvers)   # real dispatch → real solve()
    s = st["per_solver"]["mine"]
    assert s["deadline_miss"] == 1 and s["transport"] == 0 and s["replayed"] == 0
    assert s["errors"] == Counter({"timeout": 1})
    rep = backtest.readiness_report(st, args)[0]
    assert rep["errors"] == {"DeadlineExceeded": {"timeout": 1}}
    assert rep["deadline_miss"] == 1 and rep["transport"] == 0
    row = st["rows"][0]["solvers"]["mine"]
    assert row["outcome"] == "deadline_miss" and row["driver_result"] == "DeadlineExceeded"


def test_late_answer_is_a_deadline_miss(monkeypatch):
    """A 2.0 s answer against a 1.0 s budget: the driver would have discarded
    it. Counted as a miss, kept in latency (it IS an answer, a slow one), not
    counted as replayed."""
    st, args = _run(monkeypatch, _dispatch_ok(ms=2000), budget_s=1.0, budget_source="override")
    s = st["per_solver"]["mine"]
    assert s["deadline_miss"] == 1 and s["replayed"] == 0 and s["latency"] == [2000]
    assert s["errors"] == Counter({"late": 1})
    assert backtest.readiness_report(st, args)[0]["errors"] == {"DeadlineExceeded": {"late": 1}}


def test_http_502_is_transport(monkeypatch):
    import io

    def urlopen_502(req, timeout=None):
        raise urllib.error.HTTPError("http://x/solve", 502, "bad gateway", {}, io.BytesIO(b""))
    monkeypatch.setattr(backtest.urllib.request, "urlopen", urlopen_502)
    st, args = _run(monkeypatch, backtest.dispatch_solvers)
    s = st["per_solver"]["mine"]
    assert s["transport"] == 1 and s["deadline_miss"] == 0
    rep = backtest.readiness_report(st, args)[0]
    assert rep["errors"] == {"SolverHttpError": {"http_502": 1}}
    assert rep["transport_rate_pct"] == 100.0


def test_old_error_keys_are_gone_and_rates_are_present():
    rep = backtest.readiness_report(_st(_stat()), _ready_args())[0]
    for gone in ("errored", "past_deadline", "late", "solve_timeout_s"):
        assert gone not in rep
    for present in ("transport", "deadline_miss", "transport_rate_pct",
                    "deadline_miss_rate_pct", "budget_s", "budget_source", "latency_basis"):
        assert present in rep
    json.dumps(rep)


def test_deadline_miss_band_zero_warn_fail():
    ok = backtest.readiness_report(_st(_stat()), _ready_args())[0]
    assert {c["label"]: c["level"] for c in ok["checks"]}["inside the deadline"] == "ok"
    warn = _stat(attempted=200, replayed=198, deadline_miss=2, returned=198, valid=198,
                 positive=198, latency=[100] * 198, errors=Counter({"timeout": 2}))
    rep = backtest.readiness_report(_st(warn), _ready_args())[0]
    assert {c["label"]: c["level"] for c in rep["checks"]}["inside the deadline"] == "warn"
    fail = _stat(attempted=100, replayed=95, deadline_miss=5, returned=95, valid=95,
                 positive=95, latency=[100] * 95, errors=Counter({"late": 5}))
    rep = backtest.readiness_report(_st(fail), _ready_args())[0]
    assert {c["label"]: c["level"] for c in rep["checks"]}["inside the deadline"] == "fail"
    assert rep["verdict"] == "NOT READY"


def test_transport_band_zero_warn_fail():
    warn = _stat(attempted=200, replayed=199, transport=1, returned=199, valid=199,
                 positive=199, latency=[100] * 199, errors=Counter({"http_500": 1}))
    rep = backtest.readiness_report(_st(warn), _ready_args())[0]
    assert {c["label"]: c["level"] for c in rep["checks"]}["no transport errors"] == "warn"
    fail = _stat(attempted=50, replayed=48, transport=2, returned=48, valid=48,
                 positive=48, latency=[100] * 48, errors=Counter({"bad_json": 2}))
    rep = backtest.readiness_report(_st(fail), _ready_args())[0]
    assert {c["label"]: c["level"] for c in rep["checks"]}["no transport errors"] == "fail"


# --------------------------------------------------------------- T4: validity

def _two_pair_solution():
    """One solution trading BOTH fixture pairs: USDC->USDT (the winner's
    trade, at the uniform prices) and WETH->USDC (the synthetic order at a
    rate 20% better than its limit)."""
    sol = _uniform_solution()
    # uniform prices are ratios: scale both by 1e18 so the derived WETH price
    # below keeps its precision (the fixture's USDC price is a small integer)
    sol["prices"] = {k: str(int(v) * 10**18) for k, v in sol["prices"].items()}
    synth = BODY["orders"][1]
    weth, usdc = synth["sellToken"].lower(), synth["buyToken"].lower()
    usdc_price = int(sol["prices"][next(k for k in sol["prices"] if k.lower() == usdc)])
    # weth/usdc = 1.2 * limit  ⇒  weth_price = usdc_price * 1.2 * buyAmount / sellAmount
    weth_price = usdc_price * 6 * int(synth["buyAmount"]) // (5 * int(synth["sellAmount"]))
    sol["prices"][weth] = str(weth_price)
    sol["trades"].append({"kind": "fulfillment", "order": synth["uid"],
                          "executedAmount": synth["sellAmount"], "fee": "0"})
    return sol, (weth, usdc)


def test_fairness_dominated_multi_pair_solution_is_filtered():
    sol, pair = _two_pair_solution()
    free = backtest.validate_and_score({"solutions": [sol]}, BODY, REF)
    assert free["n_valid"] == 1 and free["fairness"] == "not_evaluated"
    ours_on_pair = free["by_pair"][f"{pair[0]}|{pair[1]}"]
    assert ours_on_pair > 0
    # a field single-pair solution on WETH->USDC that beats ours by 1 wei
    filtered = backtest.validate_and_score({"solutions": [sol]}, BODY, REF,
                                           fairness_baseline={pair: ours_on_pair + 1})
    assert filtered["fairness"] == "evaluated"
    assert filtered["fairness_filtered"] == 1 and filtered["n_valid"] == 0
    assert filtered["best_surplus_wei"] == 0            # T6: absent from the capture numerator
    assert filtered["by_order"] == {}
    # equal to the baseline is fair (>=), and a weaker field is fair
    for base in (ours_on_pair, ours_on_pair - 1):
        ok = backtest.validate_and_score({"solutions": [sol]}, BODY, REF,
                                         fairness_baseline={pair: base})
        assert ok["n_valid"] == 1 and ok["fairness_filtered"] == 0
        assert ok["best_surplus_wei"] == free["best_surplus_wei"]


def test_fairness_single_pair_solutions_are_never_filtered():
    sol = _uniform_solution()
    huge = {(k.lower(), v.lower()): 10**30 for k, v in [("0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                                                        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9")]}
    vs = backtest.validate_and_score({"solutions": [sol]}, BODY, REF, fairness_baseline=huge)
    assert vs["n_valid"] == 1 and vs["fairness_filtered"] == 0


def test_fairness_challengers_own_single_pair_raises_the_baseline():
    """As in the real competition, the challenger's single-pair solution on a
    pair sets the baseline its own multi-pair solution must meet."""
    two, pair = _two_pair_solution()
    synth = BODY["orders"][1]
    weth, usdc = pair
    single = {"id": 1, "prices": {weth: str(7 * int(synth["buyAmount"])),
                                  usdc: str(5 * int(synth["sellAmount"]))},   # 1.4x limit: better
              "trades": [{"kind": "fulfillment", "order": synth["uid"],
                          "executedAmount": synth["sellAmount"], "fee": "0"}],
              "interactions": [], "gas": 100000}
    vs = backtest.validate_and_score({"solutions": [two, single]}, BODY, REF, fairness_baseline={})
    assert vs["fairness_filtered"] == 1 and vs["n_valid"] == 1


def test_pair_baselines_from_record():
    o0, o1 = BODY["orders"]
    rec = {"solutions": [
        {"solverAddress": "0xA", "score": "500", "isWinner": True, "filteredOut": False,
         "orders": [{"id": o1["uid"], "sellAmount": o1["sellAmount"], "buyAmount": o1["buyAmount"]}]},
        {"solverAddress": "0xB", "score": "900", "isWinner": False, "filteredOut": True,
         "orders": [{"id": o1["uid"].upper(), "sellAmount": "1", "buyAmount": "1"}]},   # single-pair: counts
        {"solverAddress": "0xC", "score": "5000", "isWinner": False, "filteredOut": False,
         "orders": [{"id": o0["uid"]}, {"id": o1["uid"]}]},                              # two pairs: no baseline
        {"solverAddress": "0xD", "score": "7000", "isWinner": False, "filteredOut": False,
         "orders": [{"id": "0x" + "ee" * 56}]},                                          # unknown uid: skipped
        {"solverAddress": "0xE", "score": "0", "isWinner": False, "filteredOut": False,
         "orders": [{"id": o0["uid"]}]},                                                 # zero score: skipped
    ]}
    stats = Counter()
    base = competition.pair_baselines(rec, BODY, stats=stats)
    assert base == {(o1["sellToken"].lower(), o1["buyToken"].lower()): 900}
    assert stats["fairness_unmapped"] == 1
    assert competition.pair_baselines({"solutions": []}, BODY) == {}


def test_fairness_not_evaluated_without_record_keeps_n_valid(monkeypatch):
    sol, _ = _two_pair_solution()
    st, args = _run(monkeypatch, _dispatch_ok(solutions=[sol]))
    s = st["per_solver"]["mine"]
    assert s["valid"] == 1 and s["fairness_not_evaluated"] == 1 and s["fairness_evaluated"] == 0
    rep = backtest.readiness_report(st, args)[0]
    assert rep["validity_basis"] == "feasibility+eligibility+udcp+fairness:not-evaluated"
    assert st["rows"][0]["solvers"]["mine"]["fairness"] == "not_evaluated"


def test_fairness_evaluated_with_record_filters_and_reports(monkeypatch):
    sol, pair = _two_pair_solution()
    ours = backtest.validate_and_score({"solutions": [sol]}, BODY, REF)["by_pair"][f"{pair[0]}|{pair[1]}"]
    o1 = BODY["orders"][1]
    rec = {"auctionId": 8339027, "auctionStartBlock": 491743380, "auctionDeadlineBlock": 491743415,
           "solutions": [{"solverAddress": "0xF", "score": str(ours + 1), "isWinner": True,
                          "filteredOut": False, "referenceScore": "123",
                          "orders": [{"id": o1["uid"], "sellAmount": o1["sellAmount"],
                                      "buyAmount": o1["buyAmount"]}]}]}
    st, args = _run(monkeypatch, _dispatch_ok(solutions=[sol]), record=rec, compete=True)
    s = st["per_solver"]["mine"]
    assert s["fairness_filtered"] == 1 and s["valid"] == 0 and s["our_surplus"] == 0
    assert s["fairness_evaluated"] == 1
    rep = backtest.readiness_report(st, args)[0]
    assert rep["validity_basis"] == "feasibility+eligibility+udcp+fairness"
    assert rep["fairness_filtered"] == 1
    assert st["rows"][0]["winner_reference_score"] == "123"     # T6.4: copied, unused


def test_zero_surplus_fill_is_valid_and_counted():
    b = copy.deepcopy(BODY)
    o = dict(b["orders"][1])
    o["uid"] = "0x" + "cd" * 56
    o["kind"] = "buy"
    o["sellAmount"] = "1000000000000000000"
    o["buyAmount"] = "3000000000"
    o["fullSellAmount"], o["fullBuyAmount"] = o["sellAmount"], o["buyAmount"]
    o["partiallyFillable"] = False
    b["orders"].append(o)
    at_limit = {"solutions": [{"id": 0, "prices": {o["sellToken"]: "3000000000",
                                                   o["buyToken"]: "1000000000000000000"},
                               "trades": [{"kind": "fulfillment", "order": o["uid"],
                                           "executedAmount": "3000000000", "fee": "0"}],
                               "interactions": [], "gas": 100000}]}
    vs = backtest.validate_and_score(at_limit, b, REF)
    assert vs["n_valid"] == 1 and vs["best_surplus_wei"] == 0
    assert vs["valid_zero_surplus"] == 1


def test_udcp_named_check_counts_and_catches_double_pricing():
    sol = _uniform_solution()
    vs = backtest.validate_and_score({"solutions": [sol]}, BODY, REF)
    assert vs["udcp_checked"] == 1 and vs["udcp_violations"] == 0
    # the same token priced twice under two spellings is two prices for one token
    dup = copy.deepcopy(sol)
    k = next(iter(dup["prices"]))
    dup["prices"][k.upper().replace("0X", "0x")] = str(int(dup["prices"][k]) * 2)
    vs = backtest.validate_and_score({"solutions": [dup]}, BODY, REF)
    assert vs["udcp_violations"] == 1 and vs["n_valid"] == 0
    assert vs["invalid"] == {"udcp_violation": 1}


# -------------------------------------------------------- T5: threshold table

def test_thresholds_default_profile_and_override():
    v, profile, src = backtest.resolve_thresholds("base")
    assert profile == "default" and v == backtest.THRESHOLDS["default"]
    assert set(src.values()) == {"default"}
    v, profile, src = backtest.resolve_thresholds("base", min_evidence=42)
    assert v["min_evidence"] == 42 and src["min_evidence"] == "override"


def test_per_chain_threshold_override_changes_verdict(monkeypatch, capsys):
    # p95 = 0.8 x budget: default latency_pass_frac 0.5 → WARN → REVIEW
    budget_ms = 4620
    s = _stat(latency=[int(budget_ms * 0.8)] * 20)
    rep = backtest.readiness_report(_st(s), _ready_args())[0]
    assert rep["verdict"] == "REVIEW" and rep["threshold_profile"] == "default"
    assert "profile 'default'" in capsys.readouterr().out
    monkeypatch.setitem(backtest.THRESHOLDS, "base", {"latency_pass_frac": 0.9})
    rep = backtest.readiness_report(_st(s), _ready_args())[0]
    assert rep["verdict"] == "READY" and rep["threshold_profile"] == "base"
    assert rep["thresholds"]["latency_pass_frac"] == 0.9
    assert rep["threshold_sources"]["latency_pass_frac"] == "base"
    assert rep["threshold_sources"]["validity_pass"] == "default"
    out = capsys.readouterr().out
    assert "profile 'base'" in out


def test_cli_override_marked_in_header(capsys):
    rep = backtest.readiness_report(_st(_stat()), _ready_args(min_evidence=10))[0]
    assert rep["threshold_sources"]["min_evidence"] == "override"
    assert "(override: min_evidence)" in capsys.readouterr().out


def test_every_threshold_lives_in_the_table():
    keys = {"answer_rate_pass", "answer_rate_warn", "transport_warn", "transport_fail",
            "deadline_miss_warn", "deadline_miss_fail", "latency_pass_frac", "latency_warn_frac",
            "validity_pass", "validity_warn", "capture_pass", "capture_warn", "min_evidence"}
    assert keys <= set(backtest.THRESHOLDS["default"])
    assert backtest.THRESHOLDS["default"]["min_evidence"] == 500


# -------------------------------------------------- T6: capture disclosure

def test_capture_formula_unchanged_and_basis_mix_printed(capsys):
    s = _stat(basis_mix=Counter({"exact_uniform": 15, "wrapper_lower_bound": 5}))
    rep = backtest.readiness_report(_st(s), _ready_args())[0]
    assert rep["capture_pct"] == 60.0 and rep["capture_conditional_pct"] == 60.0
    assert rep["basis_mix"] == {"exact_uniform": 15, "wrapper_lower_bound": 5}
    out = capsys.readouterr().out
    assert "winner basis mix" in out and "wrapper_lower_bound" in out


# ------------------------------------------------ T7: archive and replay

def _counters(st):
    s = dict(st["per_solver"]["mine"])
    s.pop("rank_rows"), s.pop("econ_rows")
    return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in s.items()}


def test_archive_then_replay_from_disk_is_identical(monkeypatch, tmp_path):
    arch = str(tmp_path / "bodies")
    st1, a1 = _run(monkeypatch, _dispatch_ok(), archive_bodies=arch)
    p = backtest.bodies_path(arch, "arbitrum-one", 8339027)
    assert os.path.exists(p)
    with gzip.open(p, "rt") as f:
        assert json.load(f) == BODY                          # canonical, pre-mutation body
    manifest = [json.loads(line) for line in open(os.path.join(arch, "manifest.jsonl"))]
    assert manifest[0]["auction_id"] == 8339027 and manifest[0]["sha256"] == backtest.body_sha256(BODY)
    assert manifest[0]["from_block"] == 1 and manifest[0]["to_block"] == 100
    assert st1["agg"]["bodies_archived"] == 1

    def no_s3(*a, **k):
        raise AssertionError("S3 touched during --bodies-dir replay")
    st2, a2 = _run(monkeypatch, _dispatch_ok(), s3=no_s3, bodies_dir=arch)
    assert st2["bodies_source"] == "archive"
    assert _counters(st1) == _counters(st2)
    assert st1["rows"][0]["body_sha256"] == st2["rows"][0]["body_sha256"]
    assert st1["rows"][0]["original_deadline"] == st2["rows"][0]["original_deadline"]
    r1, r2 = backtest.readiness_report(st1, a1)[0], backtest.readiness_report(st2, a2)[0]
    assert r1["verdict"] == r2["verdict"]
    assert [c["level"] for c in r1["checks"]] == [c["level"] for c in r2["checks"]]
    assert f"--bodies-dir {arch}" in r2["reproduce"] and "--solve-timeout 4.84" in r2["reproduce"]
    assert "--min-evidence 500" in r2["reproduce"] and "engine build sha" in r2["reproduce"]


def test_missing_archived_body_is_a_visible_skip(monkeypatch, tmp_path):
    def no_s3(*a, **k):
        raise AssertionError("S3 touched during --bodies-dir replay")
    st, _ = _run(monkeypatch, _dispatch_ok(), s3=no_s3, bodies_dir=str(tmp_path / "empty"))
    assert st["skip"] == Counter({"body_not_archived": 1})
    assert st["rows"] == []


def test_body_sha256_is_canonical():
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert backtest.body_sha256(a) == backtest.body_sha256(b)
    assert backtest.body_sha256({"a": 1}) != backtest.body_sha256({"a": 2})


def test_archive_and_bodies_dir_are_exclusive():
    with pytest.raises(SystemExit) as e:
        backtest.main(["--archive-bodies", "x", "--bodies-dir", "y"])
    assert e.value.code == 2
