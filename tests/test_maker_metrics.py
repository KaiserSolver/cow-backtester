"""Maker metrics (2026-09-16): additive section behind --maker-metrics-db; no existing summary field changes."""
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cow_backtester import backtest, maker_metrics  # noqa: E402

import datetime as dt


def _db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE bebop_attempts (ts TEXT, auction_id INTEGER, order_uid TEXT, sell_token TEXT, buy_token TEXT, amount_wei TEXT, out_wei TEXT, outcome TEXT, build_category TEXT);
    CREATE TABLE submitted_solutions (ts TEXT, auction_id INTEGER, solution_id INTEGER, backend TEXT, trades INTEGER, order_uids TEXT, dust INTEGER);
    CREATE TABLE driver_notifications (ts TEXT, auction_id INTEGER, solution_id INTEGER, kind TEXT, txn TEXT, reason TEXT, env TEXT, network TEXT, components_json TEXT, is_driver_merge INTEGER, raw_solution_id TEXT);
    CREATE TABLE token_symbols (address TEXT PRIMARY KEY, symbol TEXT, decimals INTEGER);
    INSERT INTO token_symbols VALUES ('0xaaaa','USDC',6),('0xbbbb','WETH',18);
    """)
    now = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
    d1 = (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # 2026-09-14, ISO week 2026-W38
    d9 = (now - dt.timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")  # 2026-09-06, ISO week 2026-W36
    rows = [(d1, 1, "0x01", "0xaaaa", "0xbbbb", "1", "2", "sig_success", None)] * 3 + \
           [(d1, 2, "0x01", "0xaaaa", "0xbbbb", "1", None, "build_failed", "x")] + \
           [(d1, 3, "0x02", "0xaaaa", "0xbbbb", "1", None, "clipped", None)] + \
           [(d9, 9, "0x03", "0xbbbb", "0xaaaa", "1", "2", "sig_success", None)] * 2
    c.executemany("INSERT INTO bebop_attempts VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO submitted_solutions VALUES (?,?,?,?,?,?,?)", [
        (d1, 1, 0, "bebop", 1, "0x01", 0), (d1, 2, 0, "bebop", 1, "0x01", 0), (d1, 2, 1, "uniswap_v3", 1, "0x01", 0)])
    c.executemany("INSERT INTO driver_notifications VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (d1, 1, 0, "settlementStarted", None, None, "prod", "base", None, 0, None),
        (d1, 1, 0, "success", "0xt", None, "prod", "base", None, 0, None),
        (d1, 2, 7, "settlementStarted", None, None, "prod", "base", None, 0, "0")])  # raw_solution_id fallback
    c.commit()
    c.close()
    return now


def test_compute_counts_per_week_pair_lane(tmp_path):
    db = tmp_path / "intel.sqlite"
    now = _db(db)
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps({"first_ts": "a", "last_ts": "b",
                                "gate": {"settle": {"0xaaaa|0xbbbb": {"decisions": 6, "fired": 4, "skip_no_estimate": 2}},
                                         "quote": {"0xbbbb|0xaaaa": {"decisions": 1, "fired": 1, "skip_no_estimate": 3}}},
                                "req": {"quote": {"0xbbbb|0xaaaa": {"SIG_SUCCESS": 5, "fetch failed": 1}}}}))
    mm = maker_metrics.compute(str(db), weeks=2, logscan_path=str(scan), now=now)
    settle = [r for r in mm["rows"] if r["lane"] == "settle"]
    assert len(settle) == 2
    w38 = next(r for r in settle if r["week"] == "2026-W38")
    assert (w38["pair"], w38["requests"], w38["quotes"], w38["bids"], w38["wins"], w38["fills"]) == ("USDC→WETH", 4, 3, 2, 2, 1)
    assert w38["fill_ratio"] == 4.0 and abs(w38["no_stream_share"] - 2 / 8) < 1e-9
    w36 = next(r for r in settle if r["week"] == "2026-W36")
    assert (w36["pair"], w36["requests"], w36["quotes"], w36["bids"], w36["fills"], w36["fill_ratio"]) == ("WETH→USDC", 2, 2, 0, 0, None)
    quote = [r for r in mm["rows"] if r["lane"] == "quote"]
    assert quote and (quote[0]["requests"], quote[0]["quotes"], quote[0]["fills"], quote[0]["bids"]) == (6, 5, 0, None)
    assert abs(quote[0]["no_stream_share"] - 3 / 4) < 1e-9
    assert [t["week"] for t in mm["weekly_totals"]] == ["2026-W36", "2026-W38"]
    assert "maker metrics" in maker_metrics.render_text(mm)


def test_compute_survives_empty_db_and_bad_scan(tmp_path):
    db = tmp_path / "empty.sqlite"
    sqlite3.connect(db).close()
    mm = maker_metrics.compute(str(db), weeks=1, logscan_path=str(tmp_path / "missing.json"))
    assert mm["rows"] == [] and mm["weekly_totals"] == [] and str(mm["sources"]["log_window"]).startswith("unreadable")


class _St(dict):
    """A summary-input stub: real containers for the keys build_summary iterates, 0 for anything else."""

    def __missing__(self, key):
        return 0


def _st():
    from collections import Counter
    return _St(from_block=1, to_block=2, field_by_bucket={}, total_fees=0, submitters={}, api_check=None, rows=[],
               native="ETH", field_econ=[], solvers={}, txs=[], skip=Counter(), failed_ranges=[], n_found=0)


def test_summary_has_no_new_field_without_the_flag():
    args = SimpleNamespace(chain="base", env="prod", solvers=[], reward_ev=False, self_address=None)
    st = _st()
    base = backtest.build_summary(st, args, {}, None)
    assert "maker_metrics" not in base
    with_it = backtest.build_summary(st, args, {"maker_metrics": {"maker": "bebop", "rows": [], "weekly_totals": [], "window": ["a", "b"]}}, None)
    assert with_it["maker_metrics"]["maker"] == "bebop"
    assert {k: v for k, v in with_it.items() if k != "maker_metrics"} == base
