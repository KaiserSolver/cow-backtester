# Readiness report: kaisersolver on Base — 2026-08-22

The Readiness Standard, applied to ourselves. This is `cow-backtester --readiness`
run against our own production Base solver (via its staging route, same binary and
configuration that serves production auctions), over a pinned block range so the
run is reproducible by anyone.

Verdict: **REVIEW** — every operational check passes; the two warnings are
competitive-capture reads that the tool itself tells you to treat with care
(replays quote live liquidity against archived auctions).

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  base · prod · blocks 50314011..50316011
====================================================================
  [PASS] reached auctions         25 auctions attempted
  [PASS] no transport errors      0 errors
  [PASS] answers reliably         100% returned a parseable response (incl. legitimate empty solutions)
  [PASS] bid coverage             64% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 past deadline
  [PASS] latency headroom         p95 1548 ms of a 20000 ms budget
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution
  [WARN] competitive vs winners   43% of winner surplus captured (coverage-adjusted; 43% conditional on answering)
  [WARN] prices look plausible    1 auction(s) flagged implausible_surplus
  [PASS] field coverage           75 settlements, 71 auctions formed, 0 unattributed (0% of field excluded)
  ----------------------------------------------------------------
  answered            : 25/25  (100%)   bids: 16
  latency             : p50 966 ms / p95 1548 ms / max 1973 ms   (budget 20000 ms)
  surplus vs winners  : 43% captured   (16/25 valid)
```

## Reproduce

```
cow-backtester --chain base --env prod --from-block 50314011 --to-block 50316011 \
    --rpc-url <your-rpc> --solver-url <your-solver-base-url> --solver-name kaisersolver --readiness
```

Note the tool appends `/solve` to `--solver-url` — pass the endpoint base, not the
full path. (We got this wrong on our own first run; the preflight accepted a 404 as
"reachable" and every replay errored. Fix queued for the next release.)

## Reading the warnings honestly

- **43% of winner surplus captured** compares our *live-liquidity* replay against
  what historically won each auction. The comparison is noisy in both directions:
  it misses liquidity the replay cannot see (RFQ maker books — much of what wins
  on Base), and it can inflate individual reads when prices have moved in our
  favor since the auction (the flag below is one such catch). On a maker-heavy
  field we believe it understates our competitive position, but we publish it as
  measured rather than adjusted.
- **1 `implausible_surplus` flag**: on one auction our replay found ~17× the
  winner's surplus. The tool refuses to credit that — live liquidity had moved
  since the auction, and a result too good to be true is treated as suspect
  rather than as capture. That guard exists precisely so this report can't
  flatter its author.

## Method notes

- 2,001 blocks (~67 minutes of Base mainnet, 2026-08-22 ≈16:49–17:56 UTC),
  75 settlement transactions, 71 auctions formed, replay capped to the 25 newest
  (tool default `--max-auctions`; the replayed sample spans ≈17:33–17:56 UTC).
- The solver under test is the production instance (same process and production
  configuration that serves live auctions; source tree `bbefaf759`) addressed
  via its staging route so the replay traffic stays out of production telemetry.
- Readiness is a liveness-and-validity standard, not a settlement guarantee —
  pair with self-hosted shadow before production, as the tool's own output says.
