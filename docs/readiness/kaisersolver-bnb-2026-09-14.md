# Readiness report: kaisersolver on bnb — 2026-09-14

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.0**. Unlike the
2026-08-22 Base report (measured against an assumed 20 s budget on a 25-auction cap, now
superseded), this run uses the observed per-request budget for the chain and was run past
the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction
in the window replayed against the production-configured BNB instance (production settlement contract), which had not yet been activated for driver traffic — so this replay is the only load it carried.

- **Window:** blocks 121850336–121912365, 2026-09-14T14:02:58Z → 2026-09-14T21:48:25Z (7.8 h of chain time)
- **Sample:** 2595 auctions attempted (floor 500), 2595 answered, 497 with ≥1 solution
- **Budget:** 2.35 s (observed; the settle-lane p50 measured on this chain's engine logs, 2026-09-14)
- **Replay lag:** median 1.1 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (496 of 497); none of our solutions were filtered
- **Capture:** 0.00 % as the tool computes it (screen below), **5.65 %** with the valuation artefact described under *Reading the verdict* excluded — both coverage-adjusted
- **Per bid:** on the 429 unflagged auctions we entered, our surplus was 0.93× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 89 % of the window's winner surplus (artefact excluded), and we entered 0 of them
- **Verdict:** **REVIEW** — no failing check; 6 warn-level: bid coverage, latency headroom, competitive vs winners, prices look plausible, scan coverage, field coverage

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  bnb · prod · blocks 121850336..121912365
====================================================================
  window  : blocks 121850336..121912365 | attempted auctions span 2026-09-14T14:02:54Z → 2026-09-14T21:48:20Z (7.76 h, auctionStartBlock timestamps; 334.53 attempted/h)
            settlements found 3102 / auctions formed 2595 / attempted 2595 / replayed 2595 / returned 497
            excluded 388 — top: [('tx_uid_mismatch', 217), ('body_uid_mismatch', 167), ('wrapper_unattributed', 4)]
  budget  : 2.350 s (observed, BUDGETS_S[bnb].settle) | original-deadline upper bound p50 3.470 s / p95 4.077 s over 2588 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         2595 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [WARN] bid coverage             19% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [WARN] latency headroom         p95 1533 ms of a 2350 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness:partial(496/497 evaluated); fairness_filtered 0, zero_surplus 0, udcp 775 checked / 0 violations]
  [WARN] competitive vs winners   0% of winner surplus captured (coverage-adjusted; 0% conditional on answering; basis mix {'exact_uniform': 2467, 'mixed': 81, 'wrapper_lower_bound': 47})
  [WARN] prices look plausible    68 auction(s) flagged implausible_surplus
  [WARN] scan coverage            4246 of 62030 blocks in the window were NOT scanned (RPC getLogs failures) — the field is under-counted
  [WARN] field coverage           3102 settlements, 2595 auctions formed, 388 excluded (13% of field; top reasons [('tx_uid_mismatch', 217), ('body_uid_mismatch', 167), ('wrapper_unattributed', 4)])
  ----------------------------------------------------------------
  answered            : 2595/2595  (100%)   bids: 497   transport: 0   deadline misses: 0
  latency             : p50 906 ms / p95 1533 ms / max 1838 ms   (budget 2350 ms; answered-only incl. late answers)
  surplus vs winners  : 0% captured (coverage-adjusted) / 0% conditional   (497/2595 valid)
  winner basis mix    : {'exact_uniform': 2467, 'mixed': 81, 'wrapper_lower_bound': 47}
  validity basis      : feasibility+eligibility+udcp+fairness:partial(496/497 evaluated)

  Reproduce this run:
    cow-backtester --chain bnb --env prod --from-block 121850336 --to-block 121912365 --bodies-dir <archive> --solve-timeout 2.35 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/bnb --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: 3a36e59e5

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — bid coverage:** 19% of answered auctions carried >=1 solution
- **WARN — latency headroom:** p95 1533 ms of a 2350 ms budget (observed); PASS <= 0.5x, WARN <= 1x
- **WARN — competitive vs winners:** 0% of winner surplus captured (coverage-adjusted; 0% conditional on answering; basis mix {'exact_uniform': 2467, 'mixed': 81, 'wrapper_lower_bound': 47})
- **WARN — prices look plausible:** 68 auction(s) flagged implausible_surplus
- **WARN — scan coverage:** 4246 of 62030 blocks in the window were NOT scanned (RPC getLogs failures) — the field is under-counted
- **WARN — field coverage:** 3102 settlements, 2595 auctions formed, 388 excluded (13% of field; top reasons [('tx_uid_mismatch', 217), ('body_uid_mismatch', 167), ('wrapper_unattributed', 4)])
- **Valuation artefact — auction 25459284:** decoded winner surplus 154,228 BNB (6,003× the rest of the window combined) against a competition reference score of 0. A 2 USDC → MCH sell order (tx 0x540d45c9…, block 121908021): the auction's reference price for MCH values the tokens received at ≈251,000 BNB against 0.0028 BNB sold, so the decoded winner surplus is a reference-price artefact, not delivered value. The tool's 0.11.0 capture and 'competitive vs winners' lines include it; the figures marked 'artefact excluded' in this report do not. A winner-surplus sanity check is queued for 0.11.1.

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the
on-chain winner actually delivered. 68 auctions are flagged here ({'exact_uniform': 68} by winner-baseline
type; largest ratio 54,303×). Claimed prices are not simulated, so these rows are treated as suspect rather
than as evidence of out-competing the winner. 26 of the 68 had a winner surplus below a tenth of the
window's median winner surplus (flagged-row median 0.0000139 BNB vs window median 0.0000389 BNB).
The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for BNB:** reliable inside a tight budget (2.35 s observed; p95 at about two thirds of it is the latency
warn) but not yet competitive as configured, and its 0 % capture line is dominated by one valuation artefact. The instance is production-configured (production settlement contract) and
had not yet been activated for driver traffic when measured; its activation runbook changes the configuration further
once traffic arrives, so the coverage and capture readings describe the instance as it stood on 2026-09-14, not its
post-activation ceiling. Capture is a sum-weighted figure: on the auctions we did enter, our surplus matched
the winner's at 0.93× at the median; with the artefact excluded, capture is 5.65 % and the five largest
auctions (89 % of the window's winner surplus) include 0 we bid on. The scan-coverage and field-coverage warns are the replay's own: the public BNB
RPC used for the run refused some `getLogs` ranges, and a share of BNB settlements could not be matched to their auction
bodies, so the field this instance was measured against is under-counted and the report says so rather than hiding it.

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
