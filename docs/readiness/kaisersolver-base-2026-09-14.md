# Readiness report: kaisersolver on base — 2026-09-14 → 15

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.0**. Unlike the
2026-08-22 Base report (measured against an assumed 20 s budget on a 25-auction cap, now
superseded), this run uses the observed per-request budget for the chain and was run past
the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction
in the window replayed against the production Base instance, serving the CoW driver's live traffic during the run.

- **Window:** blocks 51300926–51336483 (fixed; the window cited in the forum post), 2026-09-14T13:06:39Z → 2026-09-15T08:51:53Z (19.8 h of chain time)
- **Sample:** 2098 auctions attempted (floor 500), 2098 answered, 1047 with ≥1 solution
- **Budget:** 4.62 s (observed; the settle-lane p50 measured on this chain's engine logs, 2026-09-14)
- **Replay lag:** median 0.7 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (1037 of 1047); 95 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 63.13 % of the on-chain winners' surplus, coverage-adjusted (the screen below rounds to whole percent)
- **Own wins:** 72 of the 2098 attempted auctions were settled on-chain by kaisersolver's own account (counted per auction)
- **Per bid:** on the 937 unflagged auctions we entered, our surplus was 0.955× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 24 % of the window's winner surplus, and we entered 4 of them
- **Verdict:** **REVIEW** — no failing check; 3 warn-level: bid coverage, latency headroom, prices look plausible

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  base · prod · blocks 51300926..51336483
====================================================================
  window  : blocks 51300926..51336483 | attempted auctions span 2026-09-14T13:06:31Z → 2026-09-15T08:51:45Z (19.75 h, auctionStartBlock timestamps; 106.21 attempted/h)
            settlements found 2507 / auctions formed 2098 / attempted 2098 / replayed 2098 / returned 1047
            excluded 36 — top: [('wrapper_unattributed', 33), ('tx_uid_mismatch', 2), ('body_uid_mismatch', 1)]
  budget  : 4.620 s (observed, BUDGETS_S[base].settle) | original-deadline upper bound p50 5.356 s / p95 5.504 s over 2080 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         2098 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [WARN] bid coverage             50% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [WARN] latency headroom         p95 2364 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness:partial(1037/1047 evaluated); fairness_filtered 95, zero_surplus 0, udcp 3485 checked / 0 violations]
  [PASS] competitive vs winners   63% of winner surplus captured (coverage-adjusted; 63% conditional on answering; basis mix {'mixed': 85, 'exact_uniform': 1578, 'wrapper_lower_bound': 435})
  [WARN] prices look plausible    110 auction(s) flagged implausible_surplus
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           2507 settlements, 2098 auctions formed, 36 excluded (1% of field; top reasons [('wrapper_unattributed', 33), ('tx_uid_mismatch', 2), ('body_uid_mismatch', 1)])
  ----------------------------------------------------------------
  answered            : 2098/2098  (100%)   bids: 1047   transport: 0   deadline misses: 0
  latency             : p50 758 ms / p95 2364 ms / max 3531 ms   (budget 4620 ms; answered-only incl. late answers)
  surplus vs winners  : 63% captured (coverage-adjusted) / 63% conditional   (1047/2098 valid)
  winner basis mix    : {'mixed': 85, 'exact_uniform': 1578, 'wrapper_lower_bound': 435}
  validity basis      : feasibility+eligibility+udcp+fairness:partial(1037/1047 evaluated)

  Reproduce this run:
    cow-backtester --chain base --env prod --from-block 51300926 --to-block 51336483 --bodies-dir <archive> --solve-timeout 4.62 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/base --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: e777ab394

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — bid coverage:** 50% of answered auctions carried >=1 solution
- **WARN — latency headroom:** p95 2364 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
- **WARN — prices look plausible:** 110 auction(s) flagged implausible_surplus

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the
on-chain winner actually delivered. 110 auctions are flagged here ({'mixed': 2, 'exact_uniform': 88, 'wrapper_lower_bound': 20} by winner-baseline
type; largest ratio 69,442×). Claimed prices are not simulated, so these rows are treated as suspect rather
than as evidence of out-competing the winner. 58 of the 110 had a winner surplus below a tenth of the
window's median winner surplus (flagged-row median 0.0000061 ETH vs window median 0.0000675 ETH).
The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for Base:** reliable and competitive. Tail latency (p95 at about half the observed budget) is the one
operational warn; the plausibility warn is a disclosure about claimed prices, not a failure. This instance served the
live driver throughout the run; the replay added ≈1.7 % to its request load.

## Method notes
- Regenerated 2026-09-15T12:06:42Z over a fixed block window (51300926–51336483, the window cited in the forum post) from the
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
    cow-backtester --chain base --env prod --from-block 51300926 --to-block 51336483 --bodies-dir <archive> --solve-timeout 4.62 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/base --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: e777ab394
```

Body archive manifest (root `manifest.jsonl` of the `--archive-bodies` directory, one line per archived body with its SHA-256):
SHA-256 `0c809a600e676f998a0a7bf550ef810b87889e72b6ccc1845851dcb00f2cfa89` · 9,650 lines · 2026-09-15 09:36:16Z.
