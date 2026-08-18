#!/usr/bin/env python3
"""Single-file HTML report for cow-backtester runs.

No external assets, no JS libraries, no network — the output opens anywhere
and can be attached to a PR or a forum post. Kept plain: it is a measurement
artifact, so the numbers and the caveats carry the page.
"""

import html


def _esc(x):
    return html.escape(str(x))


def _eth(wei, nat="ETH"):
    return f"{wei / 1e18:.6f} {nat}"


def render(summary, rows, path):
    nat = summary.get("native", "ETH")
    solvers = summary.get("solvers", [])
    css = """
    :root{--bg:#fbfbfd;--fg:#14161a;--mut:#5b6470;--line:#e3e6ea;--card:#fff;--accent:#3d5afe;--warn:#b45309;--bad:#c0392b;--good:#166534}
    @media (prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ee;--mut:#98a1ad;--line:#242a33;--card:#151a21;--accent:#8ea2ff;--warn:#e0a458;--bad:#f0857a;--good:#6ee7a8}}
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
    .wrap{max-width:1000px;margin:0 auto;padding:40px 22px 70px}
    h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
    .sub{color:var(--mut);margin:0 0 26px;font-size:14px}
    h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:32px 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line)}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:13px 15px}
    .card .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
    .card .v{font-size:19px;font-variant-numeric:tabular-nums;margin-top:3px}
    table{border-collapse:collapse;width:100%;font-size:13.5px}
    th,td{text-align:left;padding:7px 11px 7px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;white-space:nowrap}
    th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
    .scroll{overflow-x:auto}
    code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
    .warn{color:var(--warn)} .bad{color:var(--bad)} .good{color:var(--good)}
    .note{color:var(--mut);font-size:13px;margin:8px 0 0}
    ul.caveats{color:var(--mut);font-size:13.5px;padding-left:18px}
    ul.caveats li{margin:5px 0}
    """
    p = []
    p.append(f"<style>{css}</style>")
    p.append('<div class="wrap">')
    p.append(f"<h1>cow-backtester report — {_esc(summary.get('chain'))}</h1>")
    p.append(f'<p class="sub">{_esc(summary.get("env"))} · blocks '
             f'{_esc(summary.get("from_block"))}–{_esc(summary.get("to_block"))} · '
             f'{len(rows)} auctions scored · tool v{_esc(summary.get("version"))}</p>')

    # headline cards
    p.append('<div class="grid">')
    p.append(f'<div class="card"><div class="k">Auctions scored</div><div class="v">{len(rows)}</div></div>')
    p.append(f'<div class="card"><div class="k">Winner surplus (mixed-basis proxy)</div><div class="v">{_eth(summary.get("field_surplus_wei", 0), nat)}</div></div>')
    p.append(f'<div class="card"><div class="k">Known direct-path fee wedge</div><div class="v">{_eth(summary.get("field_fees_wei", 0), nat)}</div></div>')
    for s in solvers:
        cap = s.get("capture_pct")
        p.append(f'<div class="card"><div class="k">{_esc(s["name"])} capture (coverage-adj.)</div>'
                 f'<div class="v">{"n/a" if cap is None else f"{cap:.1f}%"}</div></div>')
    p.append("</div>")
    p.append('<p class="note">Winner surplus mixes exact uniform-price scoring (direct '
             'settlements) with delivered-basis lower bounds (wrapper settlements); '
             'fee take covers direct settlements only — wrapper fees are unknown, '
             'not zero. Per-row <code>baseline_quality</code> labels the basis.</p>')

    # readiness (when --readiness ran)
    for rep in summary.get("readiness") or []:
        v = rep.get("verdict", "?")
        cls = {"READY": "good", "REVIEW": "warn"}.get(v, "bad")
        p.append(f'<h2>Readiness — {_esc(rep.get("solver"))} '
                 f'<span class="{cls}">[{_esc(v)}]</span></h2>')
        p.append("<div class='scroll'><table><tr><th>check</th><th>level</th><th>detail</th></tr>")
        for c in rep.get("checks", []):
            ccls = {"ok": "good", "warn": "warn"}.get(c.get("level"), "bad")
            p.append(f"<tr><td>{_esc(c.get('label'))}</td>"
                     f"<td class='{ccls}'>{_esc(c.get('level'))}</td>"
                     f"<td>{_esc(c.get('detail'))}</td></tr>")
        p.append("</table></div>")

    # coverage
    cov = summary.get("coverage", {})
    p.append("<h2>Coverage</h2><div class='scroll'><table>")
    p.append("<tr><th>metric</th><th>value</th></tr>")
    for k, v in cov.items():
        cls = ' class="warn"' if ("skip" in k or "unscanned" in k) and v else ""
        p.append(f"<tr><td>{_esc(k)}</td><td{cls}>{_esc(v)}</td></tr>")
    p.append("</table></div>")

    # field surplus by size
    p.append("<h2>Field surplus by size</h2><div class='scroll'><table>")
    p.append(f"<tr><th>size bucket</th><th>trades</th><th>surplus ({nat})</th></tr>")
    for b in summary.get("buckets", []):
        p.append(f"<tr><td>{_esc(b['label'])}</td><td>{b['trades']:,}</td>"
                 f"<td>{b['surplus_wei'] / 1e18:.6f}</td></tr>")
    p.append("</table></div>")

    # per-solver counterfactual
    if solvers:
        p.append("<h2>Counterfactual</h2><div class='scroll'><table>")
        p.append("<tr><th>solver</th><th>replayed</th><th>returned</th><th>valid</th>"
                 "<th>positive</th><th>beat</th><th>surplus</th><th>capture</th>"
                 "<th>p50 ms</th><th>p95 ms</th><th>errors</th><th>flagged</th></tr>")
        for s in solvers:
            cap = s.get("capture_pct")
            p.append(
                f"<tr><td><b>{_esc(s['name'])}</b></td><td>{s['replayed']}</td><td>{s['returned']}</td>"
                f"<td>{s['valid']}</td><td>{s['positive']}</td>"
                f"<td class='{'good' if s['beat'] else ''}'>{s['beat']}</td>"
                f"<td>{_eth(s['our_surplus'], nat)}</td>"
                f"<td>{'n/a' if cap is None else f'{cap:.1f}%'}</td>"
                f"<td>{s.get('p50_ms', '-')}</td><td>{s.get('p95_ms', '-')}</td>"
                f"<td class='{'bad' if s['errored'] else ''}'>{s['errored']}</td>"
                f"<td class='{'warn' if s.get('implausible') else ''}'>{s.get('implausible', 0)}</td></tr>")
        p.append("</table></div>")
        p.append('<p class="note">Indicative: replayed against <b>live</b> liquidity and scored '
                 'vs the historical winning set. Prices are claimed by the solver, not simulated.</p>')

    # consistency economics (--reward-ev)
    for s in solvers:
        ec = s.get("consistency")
        if not ec:
            continue
        p.append(f"<h2>Consistency economics — {_esc(s['name'])} <span class='sub'>(CIP-85 v2 proxy)</span></h2>")
        floor = "met" if ec.get("win_floor_met") else "NOT met (zero-win chains pay zero)"
        est = (f' · est. <b>{_esc(ec.get("consistency_cow_estimate"))} COW</b> of a '
               f'{_esc(ec.get("budget_cow"))} COW budget'
               if ec.get("consistency_cow_estimate") is not None else "")
        p.append(f'<p class="sub">metric {_esc(ec["challenger_metric"])} over '
                 f'{ec["challenger_orders_bid"]}/{ec["executed_orders"]} executed orders · '
                 f'pool share {_esc(ec["challenger_share_pct"])}% · win floor {floor}{est}</p>')
        p.append("<div class='scroll'><table><tr><th>field solver</th><th>metric</th></tr>")
        for e in ec.get("field_leaderboard", []):
            p.append(f"<tr><td><code>{_esc(e['solver'])}</code></td><td>{_esc(e['metric'])}</td></tr>")
        p.append("</table></div>")
        p.append(f'<p class="note">{_esc(ec.get("basis", ""))}</p>')

    # field ranking (--compete)
    for s in solvers:
        ft = s.get("field")
        if not ft:
            continue
        p.append(f"<h2>Field ranking — {_esc(s['name'])}</h2>")
        p.append(f'<p class="sub">rank 1 in {ft["rank1_pct"]}% / top-3 in '
                 f'{ft["top3_pct"]}% of {ft["auctions_ranked"]} ranked auctions · '
                 f'median rank {ft["median_rank"]} · median gap to winner '
                 f'{_esc(ft["median_gap_bps"])} bps</p>')
        p.append("<div class='scroll'><table><tr><th>rival (winner)</th>"
                 "<th>wins vs us</th><th>median gap (bps)</th></tr>")
        for rv in ft.get("rivals", []):
            p.append(f"<tr><td><code>{_esc(rv['solver'])}</code></td>"
                     f"<td>{rv['wins']}</td><td>{_esc(rv['median_gap_bps'])}</td></tr>")
        p.append("</table></div>")
        p.append('<p class="note">Rank basis: challenger surplus inserted into the '
                 'fairness-surviving historical score list (scores include protocol '
                 'fees, surplus does not — the rank is a floor, not flattery).</p>')

    # head-to-head
    h2h = summary.get("head_to_head")
    if h2h:
        p.append("<h2>Head to head</h2><div class='scroll'><table>")
        p.append("<tr><th>metric</th><th>" + "</th><th>".join(_esc(n) for n in h2h["names"]) + "</th></tr>")
        for label, vals in h2h["rows"]:
            p.append(f"<tr><td>{_esc(label)}</td>" + "".join(f"<td>{_esc(v)}</td>" for v in vals) + "</tr>")
        p.append("</table></div>")

    # pair breakdown
    pairs = summary.get("pairs", [])
    if pairs:
        p.append("<h2>Top pairs by winner surplus</h2><div class='scroll'><table>")
        head = ["pair", "trades", f"winner ({nat})"] + [f"{n} ({nat})" for n in summary.get("solver_names", [])]
        p.append("<tr><th>" + "</th><th>".join(_esc(h) for h in head) + "</th></tr>")
        for pr in pairs:
            cells = [f"<td><code>{_esc(pr['pair'])}</code></td>", f"<td>{pr['trades']}</td>",
                     f"<td>{pr['winner_wei'] / 1e18:.6f}</td>"]
            for n in summary.get("solver_names", []):
                cells.append(f"<td>{pr.get('solvers', {}).get(n, 0) / 1e18:.6f}</td>")
            p.append("<tr>" + "".join(cells) + "</tr>")
        p.append("</table></div>")

    # winners
    subs = summary.get("submitters", [])
    if subs:
        p.append("<h2>Winning submitters</h2><div class='scroll'><table>")
        p.append(f"<tr><th>submitter</th><th>settlements</th><th>surplus ({nat})</th></tr>")
        for s in subs:
            p.append(f"<tr><td><code>{_esc(s['name'] or s['address'])}</code></td>"
                     f"<td>{s['settlements']}</td><td>{s['surplus_wei'] / 1e18:.6f}</td></tr>")
        p.append("</table></div>")

    p.append("<h2>How to read this</h2><ul class='caveats'>")
    for c in summary.get("caveats", []):
        p.append(f"<li>{_esc(c)}</li>")
    p.append("</ul>")
    p.append("</div>")

    with open(path, "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'>"
                f"<title>cow-backtester — {_esc(summary.get('chain'))}</title>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "\n".join(p))
    return path
