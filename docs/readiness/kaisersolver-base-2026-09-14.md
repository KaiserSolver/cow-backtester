# Readiness report: kaisersolver on base — 2026-09-14

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.0**. Unlike the
2026-08-22 Base report (measured against an assumed 20 s budget on a 25-auction cap, now
superseded), this run uses the observed per-request budget for the chain and was run past
the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction
in the window replayed against the production Base instance, serving the CoW driver's live traffic during the run.

- **Window:** blocks 51300926–51316580, 2026-09-14T13:06:39Z → 2026-09-14T21:48:27Z (8.7 h of chain time)
- **Sample:** 1041 auctions attempted (floor 500), 1041 answered, 535 with ≥1 solution
- **Budget:** 4.62 s (observed; the settle-lane p50 measured on this chain's engine logs, 2026-09-14)
- **Replay lag:** median 0.8 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (532 of 535); 41 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 72.64 % of the on-chain winners' surplus, coverage-adjusted (the screen below rounds to whole percent)
- **Per bid:** on the 453 unflagged auctions we entered, our surplus was 0.94× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 27 % of the window's winner surplus, and we entered 5 of them
- **Verdict:** **REVIEW** — no failing check; 2 warn-level: latency headroom, prices look plausible

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  base · prod · blocks 51300926..51316580
====================================================================
  window  : blocks 51300926..51316580 | attempted auctions span 2026-09-14T13:06:31Z → 2026-09-14T21:48:19Z (8.70 h, auctionStartBlock timestamps; 119.7 attempted/h)
            settlements found 1379 / auctions formed 1041 / attempted 1041 / replayed 1041 / returned 535
            excluded 22 — top: [('wrapper_unattributed', 19), ('tx_uid_mismatch', 2), ('body_uid_mismatch', 1)]
  budget  : 4.620 s (observed, BUDGETS_S[base].settle) | original-deadline upper bound p50 5.369 s / p95 5.563 s over 1033 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         1041 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [PASS] bid coverage             51% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [WARN] latency headroom         p95 2378 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness:partial(532/535 evaluated); fairness_filtered 41, zero_surplus 0, udcp 1816 checked / 0 violations]
  [PASS] competitive vs winners   73% of winner surplus captured (coverage-adjusted; 73% conditional on answering; basis mix {'mixed': 59, 'exact_uniform': 756, 'wrapper_lower_bound': 226})
  [WARN] prices look plausible    82 auction(s) flagged implausible_surplus
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           1379 settlements, 1041 auctions formed, 22 excluded (2% of field; top reasons [('wrapper_unattributed', 19), ('tx_uid_mismatch', 2), ('body_uid_mismatch', 1)])
  ----------------------------------------------------------------
  answered            : 1041/1041  (100%)   bids: 535   transport: 0   deadline misses: 0
  latency             : p50 704 ms / p95 2378 ms / max 3531 ms   (budget 4620 ms; answered-only incl. late answers)
  surplus vs winners  : 73% captured (coverage-adjusted) / 73% conditional   (535/1041 valid)
  winner basis mix    : {'mixed': 59, 'exact_uniform': 756, 'wrapper_lower_bound': 226}
  validity basis      : feasibility+eligibility+udcp+fairness:partial(532/535 evaluated)

  Reproduce this run:
    cow-backtester --chain base --env prod --from-block 51300926 --to-block 51316580 --bodies-dir <archive> --solve-timeout 4.62 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/base --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: e777ab394

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — latency headroom:** p95 2378 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
- **WARN — prices look plausible:** 82 auction(s) flagged implausible_surplus

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the
on-chain winner actually delivered. 82 auctions are flagged here ({'mixed': 2, 'exact_uniform': 65, 'wrapper_lower_bound': 15} by winner-baseline
type; largest ratio 69,442×). Claimed prices are not simulated, so these rows are treated as suspect rather
than as evidence of out-competing the winner. 42 of the 82 had a winner surplus below a tenth of the
window's median winner surplus (flagged-row median 0.0000060 ETH vs window median 0.0000614 ETH).
The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for Base:** reliable and competitive. Tail latency (p95 at about half the observed budget) is the one
operational warn; the plausibility warn is a disclosure about claimed prices, not a failure. This instance served the
live driver throughout the run; the replay added ≈1.7 % to its request load.

## Method notes
- Snapshot of a still-running watch, taken 2026-09-14T21:49:23Z; the window ends at the last auction replayed by then.
- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction
  counted once). The verdict is the tool's own `readiness_report` over those counters.
- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver;
  none occurred. "transport" is every other failure; none occurred.
- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.
- Bodies were archived (`--archive-bodies`, SHA-256 per body) so the run is reproducible after the
  public instance bucket evicts them; the archive is ours, hence `<archive>` in the reproduce line.
- Signal, not guarantee: replays quote live liquidity against archived auctions.
