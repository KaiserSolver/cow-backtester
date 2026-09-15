"""Maker-facing request metrics (additive, behind --maker-metrics-db; 2026-09-16).

What an RFQ maker sees from a taker: requests, quotes returned, bids emitted on those quotes, auctions won, fills,
requests per fill, and the share of considerations the maker's own price stream could not price (no-stream).
Per maker x pair x lane, per ISO week. Sources are the engine's intelligence SQLite (settle lane; the `bebop_attempts`,
`submitted_solutions`, `driver_notifications`, `token_symbols` tables) and, optionally, the cached log scan of
`bebop_request_discipline.py` (quote lane + no-stream shares; log window, not weekly). Nothing here touches the
existing readiness checks or their fields; the section exists only when the flag is given.
"""
import collections
import datetime as dt
import json
import sqlite3


def _iso_week(ts):
    d = dt.date.fromisoformat(ts[:10])
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.execute("PRAGMA query_only=1")
    return c


def compute(db_path, weeks=4, maker="bebop", logscan_path=None, now=None):
    """Return a JSON-able dict: {maker, window, rows[], weekly_totals[], sources}. Never raises on an empty DB."""
    now = now or dt.datetime.now(dt.UTC)
    start = (now - dt.timedelta(days=7 * weeks)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    c = _ro(db_path)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sym = {}
    if "token_symbols" in tables:
        sym = {a.lower(): s for a, s in c.execute("SELECT address, symbol FROM token_symbols") if s}

    def pair_name(s, b):
        return f"{sym.get(s, s[:8])}→{sym.get(b, b[:8])}"

    rows = collections.defaultdict(lambda: dict(requests=0, quotes=0, bids=0, wins=0, fills=0))
    uid_pair = {}
    if "bebop_attempts" in tables:
        for ts, uid, s, b, outcome in c.execute(
                "SELECT ts, order_uid, sell_token, buy_token, outcome FROM bebop_attempts WHERE ts>=? AND ts<? AND outcome!='clipped'", (start, end)):
            key = (_iso_week(ts), "settle", (s.lower(), b.lower()))
            rows[key]["requests"] += 1
            rows[key]["quotes"] += outcome == "sig_success"
            uid_pair.setdefault(uid.lower(), (s.lower(), b.lower()))
    bids, won, filled = {}, set(), set()
    if "submitted_solutions" in tables:
        for aid, sid, ts, uids in c.execute(
                "SELECT auction_id, solution_id, ts, order_uids FROM submitted_solutions WHERE backend=? AND ts>=? AND ts<?", (maker, start, end)):
            bids[(aid, sid)] = (ts, [u.strip().lower() for u in uids.split(",") if u.strip()])
    if "driver_notifications" in tables and bids:
        for aid, sid, rsid, kind in c.execute(
                "SELECT auction_id, solution_id, raw_solution_id, kind FROM driver_notifications "
                "WHERE kind IN ('settlementStarted','success') AND ts>=? AND ts<?", (start, end)):
            key = (aid, sid)
            if key not in bids and rsid is not None:
                try:
                    key = (aid, int(rsid))
                except (TypeError, ValueError):
                    pass
            if key in bids:
                won.add(key)
                if kind == "success":
                    filled.add(key)
    for key, (ts, uids) in bids.items():
        for u in uids:
            pair = uid_pair.get(u)
            if not pair:
                continue
            r = rows[(_iso_week(ts), "settle", pair)]
            r["bids"] += 1
            r["wins"] += key in won
            r["fills"] += key in filled
    no_stream = {}
    quote_rows = {}
    log_window = None
    if logscan_path:
        try:
            j = json.load(open(logscan_path))
            log_window = [j.get("first_ts"), j.get("last_ts")]
            for lane, pairs in (j.get("gate") or {}).items():
                for pk, g in pairs.items():
                    fired = g.get("fired", 0) + g.get("fired_no_estimate_override", 0)
                    denom = g.get("decisions", 0) + g.get("skip_no_estimate", 0) + g.get("fired_no_estimate_override", 0) \
                        + g.get("skip_below_limit", 0) + g.get("skip_settle_disabled", 0)
                    if denom:
                        no_stream[(lane, tuple(pk.split("|")))] = dict(share=g.get("skip_no_estimate", 0) / denom, considered=denom, fired=fired)
            for pk, oc in (j.get("req") or {}).get("quote", {}).items():
                s, b = pk.split("|")
                quote_rows[(s, b)] = dict(requests=sum(oc.values()), quotes=oc.get("SIG_SUCCESS", 0))
        except Exception as e:  # the section degrades, never breaks the report
            log_window = f"unreadable: {e}"
    out_rows = []
    for (week, lane, (s, b)), r in sorted(rows.items(), key=lambda kv: (kv[0][0], -kv[1]["requests"])):
        ns = no_stream.get((lane, (s, b)))
        out_rows.append(dict(week=week, lane=lane, maker=maker, pair=pair_name(s, b), sell=s, buy=b, **r,
                             fill_ratio=(r["requests"] / r["fills"]) if r["fills"] else None,
                             no_stream_share=ns["share"] if ns else None))
    for (s, b), q in sorted(quote_rows.items(), key=lambda kv: -kv[1]["requests"]):
        ns = no_stream.get(("quote", (s, b)))
        out_rows.append(dict(week="log-window", lane="quote", maker=maker, pair=pair_name(s, b), sell=s, buy=b, requests=q["requests"],
                             quotes=q["quotes"], bids=None, wins=None, fills=0, fill_ratio=None, no_stream_share=ns["share"] if ns else None))
    totals = collections.defaultdict(lambda: dict(requests=0, quotes=0, bids=0, wins=0, fills=0))
    for r in out_rows:
        if r["lane"] != "settle":
            continue
        t = totals[r["week"]]
        for k in ("requests", "quotes", "bids", "wins", "fills"):
            t[k] += r[k] or 0
    weekly = [dict(week=w, lane="settle", maker=maker, **t, fill_ratio=(t["requests"] / t["fills"]) if t["fills"] else None)
              for w, t in sorted(totals.items())]
    return dict(maker=maker, window=[start, end], weeks=weeks, rows=out_rows, weekly_totals=weekly,
                sources=dict(db=db_path, logscan=logscan_path, log_window=log_window),
                notes=["settle lane from the intelligence DB per ISO week; quote lane and no-stream shares from the cached log scan "
                       "(its own window, not weekly); fills in the quote lane are 0 by construction; wins/bids in the quote lane are not "
                       "attributable from these sources"])


def render_text(mm, top=15):
    L = [f"maker metrics — {mm['maker']} — {mm['window'][0]} → {mm['window'][1]}"]
    L.append("  week      lane    requests   quotes   bids  wins  fills  req/fill")
    for t in mm["weekly_totals"]:
        fr = "-" if t["fill_ratio"] is None else f"{t['fill_ratio']:.0f}"
        L.append(f"  {t['week']}  {t['lane']:<6} {t['requests']:>9,} {t['quotes']:>8,} {t['bids']:>6,} {t['wins']:>5,} {t['fills']:>6,} {fr:>9}")
    L.append(f"  top pairs (settle, latest week):")
    latest = mm["weekly_totals"][-1]["week"] if mm["weekly_totals"] else None
    for r in [r for r in mm["rows"] if r["lane"] == "settle" and r["week"] == latest][:top]:
        ns = "-" if r["no_stream_share"] is None else f"{r['no_stream_share']:.0%}"
        fr = "-" if r["fill_ratio"] is None else f"{r['fill_ratio']:.0f}"
        L.append(f"    {r['pair']:<22} req {r['requests']:>8,}  quotes {r['quotes']:>8,}  bids {r['bids']:>5}  fills {r['fills']:>4}  req/fill {fr:>7}  no-stream {ns:>4}")
    return "\n".join(L)
