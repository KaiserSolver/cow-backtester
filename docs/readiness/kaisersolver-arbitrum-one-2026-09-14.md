# Readiness report: kaisersolver on arbitrum-one — 2026-09-14 → 15

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.0**. Unlike the
2026-08-22 Base report (measured against an assumed 20 s budget on a 25-auction cap, now
superseded), this run uses the observed per-request budget for the chain and was run past
the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction
in the window replayed against the production Arbitrum instance, serving the CoW driver's live traffic during the run.

- **Window:** blocks 505098473–505365109 (fixed; the window cited in the forum post), 2026-09-14T14:05:14Z → 2026-09-15T08:50:16Z (18.8 h of chain time)
- **Sample:** 1030 auctions attempted (floor 500), 1030 answered, 491 with ≥1 solution
- **Budget:** 4.84 s (observed; the settle-lane p50 measured on this chain's engine logs, 2026-09-14)
- **Replay lag:** median 0.6 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (491 of 491); 13 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 36.36 % of the on-chain winners' surplus, coverage-adjusted (the screen below rounds to whole percent)
- **Own wins:** 24 of the 1030 attempted auctions were settled on-chain by kaisersolver's own account (counted per auction)
- **Per bid:** on the 482 unflagged auctions we entered, our surplus was 0.979× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 19 % of the window's winner surplus, and we entered 1 of them
- **Verdict:** **REVIEW** — no failing check; 3 warn-level: bid coverage, competitive vs winners, prices look plausible

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  arbitrum-one · prod · blocks 505098473..505365109
====================================================================
  window  : blocks 505098473..505365109 | attempted auctions span 2026-09-14T14:05:08Z → 2026-09-15T08:50:09Z (18.75 h, auctionStartBlock timestamps; 54.93 attempted/h)
            settlements found 1094 / auctions formed 1030 / attempted 1030 / replayed 1030 / returned 491
            excluded 30 — top: [('tx_uid_mismatch', 12), ('body_uid_mismatch', 11), ('wrapper_unattributed', 7)]
  budget  : 4.840 s (observed, BUDGETS_S[arbitrum-one].settle) | original-deadline upper bound p50 5.888 s / p95 6.378 s over 1024 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         1030 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [WARN] bid coverage             48% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [PASS] latency headroom         p95 1979 ms of a 4840 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness; fairness_filtered 13, zero_surplus 0, udcp 1443 checked / 0 violations]
  [WARN] competitive vs winners   36% of winner surplus captured (coverage-adjusted; 36% conditional on answering; basis mix {'exact_uniform': 912, 'wrapper_lower_bound': 108, 'mixed': 10})
  [WARN] prices look plausible    8 auction(s) flagged implausible_surplus
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           1094 settlements, 1030 auctions formed, 30 excluded (3% of field; top reasons [('tx_uid_mismatch', 12), ('body_uid_mismatch', 11), ('wrapper_unattributed', 7)])
  ----------------------------------------------------------------
  answered            : 1030/1030  (100%)   bids: 491   transport: 0   deadline misses: 0
  latency             : p50 793 ms / p95 1979 ms / max 3230 ms   (budget 4840 ms; answered-only incl. late answers)
  surplus vs winners  : 36% captured (coverage-adjusted) / 36% conditional   (490/1030 valid)
  winner basis mix    : {'exact_uniform': 912, 'wrapper_lower_bound': 108, 'mixed': 10}
  validity basis      : feasibility+eligibility+udcp+fairness

  Reproduce this run:
    cow-backtester --chain arbitrum-one --env prod --from-block 505098473 --to-block 505365109 --bodies-dir <archive> --solve-timeout 4.84 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/arbitrum-one --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: 3a36e59e5

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — bid coverage:** 48% of answered auctions carried >=1 solution
- **WARN — competitive vs winners:** 36% of winner surplus captured (coverage-adjusted; 36% conditional on answering; basis mix {'exact_uniform': 912, 'wrapper_lower_bound': 108, 'mixed': 10})
- **WARN — prices look plausible:** 8 auction(s) flagged implausible_surplus

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the
on-chain winner actually delivered. 8 auctions are flagged here ({'exact_uniform': 7, 'wrapper_lower_bound': 1} by winner-baseline
type; largest ratio 603×). Claimed prices are not simulated, so these rows are treated as suspect rather
than as evidence of out-competing the winner. 5 of the 8 had a winner surplus below a tenth of the
window's median winner surplus (flagged-row median 0.0000059 ETH vs window median 0.0000695 ETH).
The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for Arbitrum:** reliable and fast; the warns are about competitiveness — we bid on fewer than half of the
settled auctions and capture a minority of the winners' surplus, which matches this instance's known profile (a
quote-lane specialist on Arbitrum). 13 of our bids were fairness-filtered by the CIP-67 rule and count as invalid
exactly as the protocol would have treated them. The replay added ≈0.9 % to the instance's live request load.

## Method notes
- Regenerated 2026-09-15T12:06:42Z over a fixed block window (505098473–505365109, the window cited in the forum post) from the
  watch run's rows: every settled auction whose settlement block lies inside it, one row per auction id.
  The earlier snapshot of the same run (commit d16ef14, taken 2026-09-14 21:45Z) covered the first part of this window.
- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction
  counted once). The verdict is the tool's own `readiness_report` over those counters.
- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver;
  none occurred. "transport" is every other failure; none occurred.
- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.
- Bodies were archived (`--archive-bodies`, SHA-256 per body) so the run is reproducible after the
  public instance bucket evicts them; the archive is ours, hence `<archive>` in the reproduce line.
- Signal, not guarantee: replays quote live liquidity against archived auctions.

## Reproduce

```
    cow-backtester --chain arbitrum-one --env prod --from-block 505098473 --to-block 505365109 --bodies-dir <archive> --solve-timeout 4.84 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/arbitrum-one --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: 3a36e59e5
```

Body archive manifest (root `manifest.jsonl` of the `--archive-bodies` directory, one line per archived body with its SHA-256):
SHA-256 `0c809a600e676f998a0a7bf550ef810b87889e72b6ccc1845851dcb00f2cfa89` · 9,650 lines · 2026-09-15 09:36:16Z.
