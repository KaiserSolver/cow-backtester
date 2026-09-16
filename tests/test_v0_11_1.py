"""Offline tests for the 0.11.1 batch: BT-10 winner-surplus sanity check,
BT-11 --json-out append under --watch, BT-12 per-bid median ratio. No
network: the hand-built run state mirrors what process_window returns (rows
with per-solver results, sums accrued the same way), and the --watch CLI
tests patch the RPC, the window scan and the idle sleep.
"""
import copy
import json
import os
import sys
from collections import Counter
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cow_backtester import backtest, scorer  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")
ARTEFACT_ID = 25459284        # the BNB 2026-09-14 auction that read the chain's capture as 0 %


# ------------------------------------------------------------------ helpers

def _args(**over):
    base = dict(chain="bnb", env="prod", from_block=1, workers=1, max_auctions=0,
                solvers=[{"url": "http://x", "name": "mine"}], verify_api=False,
                compete=False, archive_dir=None, reward_ev=False, clamp_validto=False,
                self_address=None, budget_s=2.35, budget_source="observed",
                bodies_dir=None, archive_bodies=None, solve_timeout=None,
                min_evidence=10, quiet=True, max_age_hours=6.0, engine_sha=None)
    base.update(over)
    return SimpleNamespace(**base)


def _row(aid, winner, ours=None, n_solutions=None, n_valid=None, flags=None, error=None):
    """One process_window row for solver 'mine': answered with a bid (`ours`
    given), answered empty (`ours` None), or a failure (`error` given)."""
    if error:
        vs = {"solve_error": error, "driver_result": backtest.driver_error_label(error),
              "outcome": "transport", "latency_ms": 50}
    else:
        n_sol = n_solutions if n_solutions is not None else (1 if ours is not None else 0)
        vs = {"best_surplus_wei": ours or 0, "best_single_wei": ours or 0,
              "n_solutions": n_sol,
              "n_valid": n_valid if n_valid is not None else (1 if ours is not None else 0),
              "invalid": {}, "fairness": "not_evaluated", "fairness_filtered": 0,
              "valid_zero_surplus": 0, "udcp_checked": n_sol, "udcp_violations": 0,
              "latency_ms": 100, "outcome": "answered"}
        if flags:
            vs["flags"] = list(flags)
    return {"v": backtest.VERSION, "chain": "bnb", "env": "prod", "auction_id": aid,
            "settlement_block": 1000 + aid % 1000, "winner_surplus_wei": winner,
            "baseline_quality": "exact_uniform", "solvers": {"mine": vs}}


def _state(rows):
    """Run state consistent with `rows`, accrued the way process_window does."""
    s = {"replayed": 0, "transport": 0, "deadline_miss": 0, "returned": 0, "valid": 0,
         "positive": 0, "beat": 0, "our_surplus": 0, "winner_surplus": 0, "implausible": 0,
         "attempted": 0, "winner_surplus_attempted": 0, "lost_to_errors": 0,
         "invalid": Counter(), "errors": Counter(), "latency": [], "basis_mix": Counter()}
    for r in rows:
        vs = r["solvers"]["mine"]
        s["attempted"] += 1
        s["winner_surplus_attempted"] += r["winner_surplus_wei"]
        s["basis_mix"][r["baseline_quality"]] += 1
        if "solve_error" in vs:
            s["transport"] += 1
            s["errors"][vs["solve_error"]] += 1
            s["lost_to_errors"] += r["winner_surplus_wei"]
            continue
        s["replayed"] += 1
        s["latency"].append(vs["latency_ms"])
        s["winner_surplus"] += r["winner_surplus_wei"]
        s["our_surplus"] += vs["best_surplus_wei"]
        s["returned"] += 1 if vs["n_solutions"] else 0
        s["valid"] += 1 if vs["n_valid"] else 0
        s["positive"] += 1 if vs["best_surplus_wei"] > 0 else 0
        s["beat"] += 1 if vs["best_surplus_wei"] > r["winner_surplus_wei"] else 0
        s["implausible"] += 1 if "implausible_surplus" in vs.get("flags", []) else 0
    return {"per_solver": {"mine": s}, "rows": rows, "from_block": 1000, "to_block": 8000,
            "txs": list(range(len(rows))), "n_found": len(rows), "skip": Counter(),
            "failed_ranges": [], "agg": Counter(),
            "attempted_ts": [1_755_000_000 + 60 * i for i in range(len(rows))],
            "attempted_start_ts": [], "budget_upper": [], "native": "BNB"}


def _normal_rows(n, winner=1000, ours=600):
    return [_row(1 + i, winner, ours) for i in range(n)]


def _checks(rep):
    return {c["label"]: c for c in rep["checks"]}


# ------------------------------------------------ BT-10: winner-surplus artefact

def test_artefact_auction_is_listed_excluded_and_verdict_reads_ex_artefact(capsys):
    """BNB 2026-09-14 in miniature: 23 ordinary auctions we match at 60 %, plus
    one auction whose decoded winner surplus dwarfs the rest of the window
    combined (the 25459284 shape; we bid nothing on it). The headline capture
    reads ~0 %; the verdict must read the ex-artefact 60 %, both must print,
    and the artefact must be listed with its id and ratio."""
    rows = _normal_rows(23) + [_row(ARTEFACT_ID, 10**7, ours=None)]
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert [a["auction_id"] for a in rep["artefact_auctions"]] == [ARTEFACT_ID]
    art = rep["artefact_auctions"][0]
    assert art["ratio"] == 434.8                       # 10**7 vs 23 x 1000, one decimal
    assert art["winner_surplus_wei"] == 10**7 and art["rest_wei"] == 23_000
    assert art["our_surplus_wei"] == 0 and art["answered"] is True
    # both captures: the headline keeps the artefact, the verdict does not
    assert rep["capture_pct"] == pytest.approx(100 * 13_800 / (23_000 + 10**7))
    assert rep["capture_ex_artefact_pct"] == pytest.approx(60.0)
    assert rep["capture_conditional_ex_artefact_pct"] == pytest.approx(60.0)
    c = _checks(rep)["competitive vs winners"]
    assert c["level"] == "ok" and c["detail"].startswith("60% of winner surplus captured")
    assert "1 valuation artefact(s) excluded — 0% / 0% including them" in c["detail"]
    # the artefact row itself: warn-level, id + ratio + the rule that fired
    w = _checks(rep)["winner surplus plausible"]
    assert w["level"] == "warn" and str(ARTEFACT_ID) in w["detail"] and "434.8x" in w["detail"]
    assert "> 100x the rest, >= 20 attempted" in w["detail"] and "BNB" in w["detail"]
    assert rep["verdict"] == "REVIEW"                  # a human must see what was excluded
    rule = rep["artefact_rule"]
    assert (rule["ratio"], rule["min_attempted"], rule["evaluated"]) == (100, 20, True)
    out = capsys.readouterr().out
    assert "ex-artefact capture : 60% captured (coverage-adjusted) / 60% conditional" in out
    assert f"excluding 1 auction(s): {ARTEFACT_ID} (434.8x the rest)" in out
    assert "[WARN] winner surplus plausible" in out
    json.dumps(rep)                                    # lands in the summary JSON/HTML


def test_no_artefact_keeps_every_number_line_and_check_unchanged(capsys):
    rows = _normal_rows(24)
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert rep["artefact_auctions"] == [] and rep["artefact_rule"]["evaluated"] is True
    assert rep["capture_ex_artefact_pct"] == rep["capture_pct"] == pytest.approx(60.0)
    assert rep["capture_conditional_ex_artefact_pct"] == rep["capture_conditional_pct"]
    assert "winner surplus plausible" not in _checks(rep)
    assert _checks(rep)["competitive vs winners"]["detail"] == (
        "60% of winner surplus captured (coverage-adjusted; 60% conditional on answering; "
        "basis mix {'exact_uniform': 24})")
    assert rep["verdict"] == "READY"
    out = capsys.readouterr().out
    assert "artefact" not in out


def test_artefact_rule_boundaries():
    def flagged(entries):
        return [a["auction_id"] for a in backtest.winner_surplus_artefacts(entries)]

    def entries(big, n_rest, rest_each=1000):
        return ([{"auction_id": i, "winner_surplus_wei": rest_each} for i in range(1, n_rest + 1)]
                + [{"auction_id": 99, "winner_surplus_wei": big}])
    # exactly 100x the rest is NOT an artefact; one wei more is
    assert flagged(entries(100 * 19_000, 19)) == []
    assert flagged(entries(100 * 19_000 + 1, 19)) == [99]
    # below the attempted floor the rule cannot fire, however large
    assert flagged(entries(10**30, 18)) == []          # 19 attempted
    assert flagged(entries(10**30, 19)) == [99]        # 20 attempted
    # a window whose other auctions carry NO surplus has nothing to measure
    # against: the only auction with surplus is not an artefact
    assert flagged(entries(10**30, 19, rest_each=0)) == []
    # two of similar size no longer mask each other: both leave the window
    assert flagged(entries(10**30, 19) + [{"auction_id": 98, "winner_surplus_wei": 10**30}]) == [99, 98]
    # beyond a single outlier, the set must also sit more than ARTEFACT_GROUP_GAP
    # x above the largest auction left below it: exactly 1e9x is NOT an artefact
    pair = [{"auction_id": 98, "winner_surplus_wei": 10**9 * 1000}]
    assert flagged(entries(10**9 * 1000, 19) + pair) == []
    pair[0]["winner_surplus_wei"] += 1
    assert flagged(entries(10**9 * 1000 + 1, 19) + pair) == [99, 98]
    # a single outlier still fires alone, without dragging a large-but-ordinary
    # auction in after it
    assert flagged(entries(10**30, 18) + [{"auction_id": 98, "winner_surplus_wei": 10**6}]) == [99]
    # a cluster must be a strict minority of the window: 10 of 21 is a
    # cluster, 10 of 20 is half the window
    big = [{"auction_id": 100 + i, "winner_surplus_wei": 10**30} for i in range(9)]
    assert flagged(entries(10**30, 11) + big) == [99] + [100 + i for i in range(9)]
    assert flagged(entries(10**30, 10) + big) == []
    # a larger artefact above a cluster must not hide it: the search runs again
    # on what is left, and every flagged auction is measured against the window
    # outside ALL of them
    ordinary = [{"auction_id": i, "winner_surplus_wei": 1000} for i in range(1, 24)]
    nested = ([{"auction_id": 97, "winner_surplus_wei": 10**40}]
              + [{"auction_id": 100 + i, "winner_surplus_wei": 7 * 10**32} for i in range(3)])
    assert flagged(ordinary + nested) == [97, 100, 101, 102]
    assert {a["rest_wei"] for a in backtest.winner_surplus_artefacts(ordinary + nested)} == {23_000}
    # the minority cap holds across rounds, not per round: 1 + 9 of 21 is a
    # minority, 1 + 9 of 20 is not (the 9 then stay in the window)
    nine = [{"auction_id": 100 + i, "winner_surplus_wei": 10**30} for i in range(9)]
    top = [{"auction_id": 97, "winner_surplus_wei": 10**40}]
    assert flagged(ordinary[:11] + top + nine) == [97] + [100 + i for i in range(9)]
    assert flagged(ordinary[:10] + top + nine) == [97]
    # one bad price on orders of different sizes: the largest two dominate the
    # window first but sit within the gap of the third, so the search must go
    # on to the set of three rather than give up
    whales = [{"auction_id": i, "winner_surplus_wei": 10**18} for i in range(1, 24)]
    for third in (7 * 10**30, 7 * 10**28):
        tiered = [{"auction_id": 100, "winner_surplus_wei": 74 * 10**31},
                  {"auction_id": 101, "winner_surplus_wei": 73 * 10**31},
                  {"auction_id": 102, "winner_surplus_wei": third}]
        assert flagged(whales + tiered) == [100, 101, 102]


def test_two_population_window_flags_nothing():
    """A short window of ordinary auctions (5e17-2.4e18 wei) beside dust
    (1e10-3e10 wei): every ordinary auction is far more than 100x all the dust
    combined, but seven orders of magnitude is a real spread of order sizes,
    not a valuation artefact. Flagging the ordinary auctions as a group would
    leave the ex-artefact capture measured over dust alone."""
    rows = ([_row(1 + i, (5 + i) * 10**17, (3 + i) * 10**17) for i in range(20)]
            + [_row(100 + i, (1 + i % 3) * 10**10, ours=None) for i in range(30)])
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert rep["artefact_auctions"] == [] and rep["artefact_rule"]["evaluated"] is True
    assert rep["capture_ex_artefact_pct"] == rep["capture_pct"]
    assert "winner surplus plausible" not in _checks(rep)


@pytest.mark.parametrize("above", [{}, {ARTEFACT_ID: 10**40}], ids=["alone", "under-a-larger-artefact"])
def test_cluster_of_co_scaled_artefacts_is_listed_and_excluded(above):
    """Plasma, three auctions (8939411, 8952964, 9040629) whose buy token's
    referencePrice (~5.0e37) valued each winner surplus at ~7.4e32 wei. None is
    100x the other two combined, so a one-vs-rest rule lists nothing and the
    capture reads 0 %; cleaned, the window reads what the solver does. The
    rows carry the decoded per-auction winner surplus, which is all the rule
    reads. A second, larger artefact from another token in the same window must
    not hide the cluster either."""
    cluster = {8939411: 740 * 10**30, 8952964: 738 * 10**30, 9040629: 743 * 10**30, **above}
    rows = _normal_rows(23) + [_row(aid, w, ours=None) for aid, w in cluster.items()]
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert {a["auction_id"] for a in rep["artefact_auctions"]} == set(cluster)
    for a in rep["artefact_auctions"]:
        assert a["winner_surplus_wei"] == cluster[a["auction_id"]]
        assert a["rest_wei"] == 23_000                  # the window outside the cluster
    assert rep["capture_pct"] == pytest.approx(0.0, abs=1e-20)
    assert rep["capture_ex_artefact_pct"] == pytest.approx(60.0)
    assert rep["capture_conditional_ex_artefact_pct"] == pytest.approx(60.0)
    assert _checks(rep)["competitive vs winners"]["level"] == "ok"
    w = _checks(rep)["winner surplus plausible"]
    assert w["level"] == "warn" and w["detail"].startswith(f"{len(cluster)} auction(s) excluded")
    assert all(str(aid) in w["detail"] for aid in cluster)
    assert f"the rest of the attempted window combined (outside the {len(cluster)} flagged)" in w["detail"]
    assert "[rule: > 100x the rest, >= 20 attempted; a group also > 1e+09x the largest auction below it]" \
        in w["detail"]
    assert rep["artefact_rule"]["group_gap"] == 10**9
    assert rep["verdict"] == "REVIEW"
    assert rep["per_bid_ratio_n"] == 23 and rep["per_bid_median_ratio"] == pytest.approx(0.6)


def test_thin_window_cannot_flag_an_artefact():
    rows = _normal_rows(18) + [_row(ARTEFACT_ID, 10**7, ours=None)]     # 19 attempted
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert rep["artefact_auctions"] == [] and rep["artefact_rule"]["evaluated"] is False
    assert rep["capture_ex_artefact_pct"] == rep["capture_pct"]
    assert _checks(rep)["competitive vs winners"]["level"] == "warn"   # the artefact still bites


def test_artefact_we_never_answered_leaves_the_conditional_sum_alone():
    rows = _normal_rows(23) + [_row(ARTEFACT_ID, 10**7, error="http_502")]
    rep = backtest.readiness_report(_state(rows), _args())[0]
    art = rep["artefact_auctions"][0]
    assert art["answered"] is False and art["our_surplus_wei"] == 0
    assert rep["capture_pct"] == pytest.approx(100 * 13_800 / (23_000 + 10**7))
    assert rep["capture_conditional_pct"] == pytest.approx(60.0)      # never in the answered sum
    assert rep["capture_ex_artefact_pct"] == pytest.approx(60.0)
    assert rep["capture_conditional_ex_artefact_pct"] == pytest.approx(60.0)


def test_our_bid_on_the_artefact_auction_is_excluded_too():
    """Bidding on the artefact auction inflates OUR surplus by the same bogus
    reference price: the auction leaves both sides of the ex-artefact capture
    and the per-bid median."""
    rows = _normal_rows(23) + [_row(ARTEFACT_ID, 10**7, ours=5 * 10**6)]
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert rep["capture_pct"] == pytest.approx(100 * (13_800 + 5 * 10**6) / (23_000 + 10**7))
    assert rep["capture_ex_artefact_pct"] == pytest.approx(60.0)
    assert rep["artefact_auctions"][0]["our_surplus_wei"] == 5 * 10**6
    assert rep["per_bid_ratio_n"] == 23 and rep["per_bid_median_ratio"] == pytest.approx(0.6)


def test_state_without_rows_keeps_legacy_behaviour():
    """The 0.10/0.11 tests build the run state without rows; the new keys must
    exist, be inert, and every existing number must be untouched."""
    st = _state(_normal_rows(24))
    st.pop("rows")
    rep = backtest.readiness_report(st, _args())[0]
    assert rep["artefact_auctions"] == [] and rep["artefact_rule"]["evaluated"] is False
    assert rep["per_bid_median_ratio"] is None and rep["per_bid_ratio_n"] == 0
    assert rep["capture_ex_artefact_pct"] == rep["capture_pct"] == pytest.approx(60.0)
    assert rep["verdict"] == "READY"


# ----------------------------------------------------- BT-12: per-bid median

def test_per_bid_median_ratio_over_unflagged_bids(capsys):
    rows = [_row(1, 1000, 500), _row(2, 1000, 900), _row(3, 1000, 1000), _row(4, 1000, 1200),
            _row(5, 1000, 50_000, flags=["implausible_surplus"]),   # flagged: out
            _row(6, 1000, ours=None),                                # answered, no bid: out
            _row(7, 1000, error="timeout"),                          # never answered: out
            _row(8, 0, 10),                                          # zero winner: no ratio
            _row(9, 1000, 0, n_solutions=2, n_valid=0)]              # bid, nothing valid: 0
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert rep["per_bid_ratio_n"] == 5                               # 0.5 0.9 1.0 1.2 0
    assert rep["per_bid_median_ratio"] == pytest.approx(0.9)
    assert "implausible_surplus" in rep["per_bid_ratio_basis"]
    out = capsys.readouterr().out
    assert "per-bid median      : 0.900x ours/winner over 5 unflagged bid auction(s)" in out


def test_per_bid_median_is_none_without_bids(capsys):
    rows = [_row(i, 1000, ours=None) for i in range(1, 6)]
    rep = backtest.readiness_report(_state(rows), _args())[0]
    assert rep["per_bid_median_ratio"] is None and rep["per_bid_ratio_n"] == 0
    assert "per-bid median      : n/a (no unflagged bid auctions)" in capsys.readouterr().out


def test_ledger_reads_real_process_window_rows(monkeypatch):
    """The ledger's row contract (solvers.<name>.best_surplus_wei / n_solutions
    / flags next to winner_surplus_wei) against a real process_window row on
    the pinned Arbitrum fixture, with the solver abstaining."""
    from cow_backtester import competition
    settlement = json.load(open(os.path.join(FIX, "settlement_8339027.json")))
    body = json.load(open(os.path.join(FIX, "body_8339027_trimmed.json")))
    monkeypatch.setattr(backtest, "enumerate_settlements",
                        lambda rpcs, frm, to, step=800: (["0x" + "77" * 32], []))
    monkeypatch.setattr(backtest, "fetch_settlement_cached", lambda rpcs, tx, cache, mcb: settlement)
    monkeypatch.setattr(backtest, "block_ts", lambda rpcs, block, mem, cache, mcb: 1_755_000_000)
    monkeypatch.setattr(backtest, "s3_auction",
                        lambda env, chain, aid, cache=None, errors=None: copy.deepcopy(body))
    monkeypatch.setattr(competition, "fetch_competition",
                        lambda api_base, aid, http_get, cache=None: None)
    monkeypatch.setattr(backtest, "dispatch_solvers",
                        lambda solvers, payload, timeout: [(sv, {"solutions": []}, None, 80)
                                                           for sv in solvers])
    args = _args(chain="arbitrum-one", budget_s=4.84)
    st = backtest.process_window(args, ["rpc"], backtest.CHAINS["arbitrum-one"], 1, 100, None, None)
    led = backtest.solver_ledger(st["rows"], "mine")
    assert len(led) == 1 and led[0]["auction_id"] == 8339027
    assert led[0]["winner_surplus_wei"] == st["rows"][0]["winner_surplus_wei"] > 0
    assert led[0]["vs"]["n_solutions"] == 0 and backtest.per_bid_ratios(led) == []
    rep = backtest.readiness_report(st, args)[0]
    assert rep["artefact_auctions"] == [] and rep["per_bid_ratio_n"] == 0


# ------------------------------------------- BT-11: --json-out under --watch

def _patch_main(monkeypatch, rows):
    """main() with no network: chain id + head from a fake RPC, the window
    scan replaced by a fixed state (rows streamed to --json-out as the real
    one does), the scorecard silenced, and the watch sleep raising Ctrl-C so
    exactly one cycle runs per call."""
    monkeypatch.setattr(backtest, "QUIET", backtest.QUIET)     # --quiet must not leak
    monkeypatch.setattr(scorer, "rpc", lambda rpcs, method, params:
                        {"eth_chainId": hex(56), "eth_blockNumber": hex(5000)}[method])
    base = _state(rows)

    def window(args, rpcs, cfg, frm, to, cache, jout):
        for r in rows:
            jout.write(json.dumps(r) + "\n")
        return dict(base, from_block=frm, to_block=to, interrupted=False, ages=[],
                    api_check=Counter(), field_by_bucket={}, pair_stats={}, submitters={},
                    total_fees=0, field_econ=[], bodies_source="s3")
    monkeypatch.setattr(backtest, "process_window", window)
    monkeypatch.setattr(backtest, "print_scorecard", lambda st, args, cache, m=None: {})

    def stop(_seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(backtest.time, "sleep", stop)


def _lines(path):
    return [json.loads(ln) for ln in open(path).read().splitlines()]


def test_watch_json_out_appends_across_restarts_and_one_shot_truncates(monkeypatch, tmp_path):
    rows = _normal_rows(3)
    _patch_main(monkeypatch, rows)
    out = str(tmp_path / "watch.jsonl")
    argv = ["--chain", "bnb", "--rpc-url", "http://rpc", "--no-cache", "--quiet", "--json-out", out]
    backtest.main(argv + ["--watch", "5"])     # one cycle, then Ctrl-C in the idle sleep
    backtest.main(argv + ["--watch", "5"])     # a "restart": must APPEND, not truncate
    lines = _lines(out)
    assert sum(1 for ln in lines if "_meta" in ln) == 2
    assert sum(1 for ln in lines if "auction_id" in ln) == 6
    assert lines[-1]["_meta"]["winner_surplus_artefacts"] == []
    backtest.main(argv)                        # a one-shot run starts a fresh file, as before
    lines = _lines(out)
    assert sum(1 for ln in lines if "_meta" in ln) == 1
    assert sum(1 for ln in lines if "auction_id" in ln) == 3


def test_watch_append_terminates_a_partial_last_line(monkeypatch, tmp_path):
    _patch_main(monkeypatch, _normal_rows(2))
    out = tmp_path / "watch.jsonl"
    out.write_text('{"auction_id": 1, "winner_surplus_wei": 5')     # killed mid-write, no newline
    backtest.main(["--chain", "bnb", "--rpc-url", "http://rpc", "--no-cache", "--quiet",
                   "--json-out", str(out), "--watch", "5"])
    lines = out.read_text().splitlines()
    assert lines[0] == '{"auction_id": 1, "winner_surplus_wei": 5'   # left as found
    parsed = [json.loads(ln) for ln in lines[1:]]                     # ours are whole lines
    assert sum(1 for ln in parsed if "_meta" in ln) == 1 and len(parsed) == 3


def test_meta_line_marks_the_artefact_auction(monkeypatch, tmp_path):
    rows = _normal_rows(23) + [_row(ARTEFACT_ID, 10**7, ours=None)]
    _patch_main(monkeypatch, rows)
    out = str(tmp_path / "run.jsonl")
    backtest.main(["--chain", "bnb", "--rpc-url", "http://rpc", "--no-cache", "--quiet",
                   "--json-out", out])
    meta = [ln for ln in _lines(out) if "_meta" in ln][0]["_meta"]
    assert meta["v"] == backtest.VERSION
    assert meta["winner_surplus_artefacts"] == [
        {"auction_id": ARTEFACT_ID, "winner_surplus_wei": 10**7, "ratio": 434.8}]
