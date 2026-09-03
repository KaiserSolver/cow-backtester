# Changelog

## 0.10.0 — 2026-09-03

Metric-correctness batch from a six-lane review (scoring and CIP-85
economics, orchestration and silent-zero paths, packaging and docs). The
numbers people quote first:

- **The basis label was wrong, in the direction that matters.** This tool's
  uniform-price surplus agrees with CoW's official CIP-38 `score` to within
  0.2% on live records (pinned reference: 767,957,704,005 vs
  769,523,899,186 wei). Earlier releases said it "overstates the score by
  the network fee (2.21x on the pinned trade)"; that comparison used the
  after-fee surplus as the reference, not the score. The uniform-vs-custom
  price wedge reported as `fee_wei` is the protocol fee the score adds back.
  README, caveats, docstrings and the HTML report are restated; the
  `fee_wei` JSON keys are kept for row stability.
- **`--compete` ranks the right number.** The challenger is ranked by its
  best SINGLE solution against the field's per-solution scores, as the
  protocol ranks. The combined disjoint-solution total (what capture uses)
  was being ranked before — three 400-wei solutions out-ranked a 1000-wei
  winner and set `win_floor_met`. Rows carry `score_wei` and, when it
  differs, `rank_combined` / `combined_score_wei`, labeled. Auctions
  answered with no valid bid are counted (`rank_no_bid`) so "rank 1 in
  100%" cannot hide a 5% bid rate; `rank_basis` is
  `best_single_solution_vs_field_scores`.
- **`--reward-ev` compares like with like.** The record's per-order
  amounts are net of protocol fees while the challenger is scored gross;
  the ratio inflated the challenger's share ~1.4x on measured records while
  labeled "conservative". A field solution with one order now contributes
  its official `score`; multi-order solutions fall back to the net
  computation and are counted (`net_basis_orders`). Auctions where the
  challenger errored stay in both sides with a zero term (dropping them
  inflated the share +27% with 3 of 10 errored). The field consistency
  leaderboard prints with `--reward-ev` alone — no `--solver-url` needed —
  in the scorecard, JSON (`field_consistency`) and HTML.
- **A/B solvers get the same wall-clock budget.** Bodies were byte-identical
  but dispatched sequentially, so the second solver received a deadline the
  first one's compute had already consumed (negative at the default 20 s
  when the rival used its budget). All solvers now receive the same bytes
  concurrently under the one deadline; call-order rotation is gone because
  there is no order.
- **A 200 that is not a `/solve` response is an error.** `{"error": …}`,
  `{}`, a `solutions` dict or a typo'd key counted as a healthy abstention
  with zero errors; readiness printed PASS for an endpoint that errored on
  every auction. Now `bad_schema`; `{"solutions": []}` remains the only
  legitimate empty answer.
- **Unscanned blocks and every exclusion reach the readiness verdict.**
  `failed_ranges` were visible only on stderr and in the full scorecard;
  the readiness "field coverage" check counted two of ~eight skip reasons.
  Readiness now has a `scan coverage` check (WARN on any unscanned span),
  `field coverage` counts every reason that removes a settlement from the
  baseline (a reverted settlement is not one), and the dict carries
  `unscanned_blocks` and `skipped`. The JSONL `_meta` line carries
  `failed_ranges`, `unscanned_blocks`, `competition_missing` and
  `validto_clamped_auctions`.

Also:

- `--readiness --quiet` printed nothing and NOT READY exited 0. `--quiet`
  now silences progress only; `--fail-on not-ready|review` exits 4 for CI.
- Head-to-head `capture %` and `capture (exact-basis)` used answered-only
  denominators (the survivorship bias 0.7.2 removed from the headline);
  both now use attempted denominators, and the head-to-head shows `errored`.
- Latency percentiles describe answers only; failed calls no longer buy a
  dead endpoint a flattering p95.
- `--clamp-validto` modifications are disclosed in the coverage block, the
  readiness screen (`bodies unmodified` WARN), JSON and `_meta`.
- S3 body failures are split: `s3_404` (retention), `s3_fetch_error`
  (network/5xx, retried once), `s3_bad_body` — and the byte cap now bounds
  the DECOMPRESSED body (a 1000x gzip bomb passed the wire cap).
  Competition records fetched/missing are printed in the coverage block.
- `preflight` treats 404/405 on POST `/solve` as the wrong-path error it is
  (the tool appends `/solve`) instead of "reachable".
- An `--archive-dir` write failure no longer aborts the window; the temp
  file is process+thread-unique.
- Native amounts below 0.0001 print in scientific notation instead of
  `0.000000`; the head-to-head delta also prints in wei.
- The network self-test picks a recent settlement automatically when the
  pinned auction has aged out of the S3 bucket.
- The deprecated `block` row alias (promised for one release in 0.7.2) is
  removed; use `settlement_block`, or `auction_start_block` /
  `auction_deadline_block` from `--compete` for the auction-cut state.
- CI asserts the usage-error exit code is 2 (a traceback passed before),
  tests Python 3.13, and `.gitignore` covers the HTML reports and archive
  directories the README tells users to create in a checkout.
- Tests: 26 new offline cases (`tests/test_v0_10.py`) reproduce each item;
  the tautological `--max-auctions` test now exercises the real helper.

## 0.9.0 — 2026-08-18

**`--reward-ev`: CIP-85 v2 consistency economics.** Capture ratios and ranks
measure competitiveness; solver income on most CoW chains is the consistency
pool. This release computes the actual v2 metric
(`Σ executed orders: your_best_fair_surplus / Σ all_solvers_surplus`) from
the same competition records `--compete` already fetches:

- the challenger's counterfactual metric, per-order surpluses inserted into
  the historical denominators (conservatively — field terms keep their
  historical values, so the reported share is a floor);
- every FIELD solver's real historical metric — a consistency leaderboard
  for the window (also useful for spotting pool-dilution patterns);
- with `--self-address`, your actual historical metric side by side with
  the replayed one;
- the win floor, reported explicitly: v2 pays ZERO on a chain where the
  solver won nothing in the period, so the estimate never hides it;
- `--consistency-budget <COW>` converts the share into a COW/week estimate.

Honesty labels throughout: field surpluses come from record amounts (net of
protocol fees) while a replayed challenger's are gross (a few bps flattering,
labeled `basis`); challenger fairness is assumed (the field uses CoW's own
`filteredOut` flags); success_rate is a flag, not a silent multiplier.


## 0.8.0 — 2026-08-17

**`--compete`: rank your solver against the historical field.** The v2
competition endpoint serves per-auction records by auction id (probed:
retention ≥ 2 months) carrying every submitted solution — solver, score,
ranking, CoW's own fairness-filtering outcome (`filteredOut`), per-solution
clearing prices, reference scores, and the auction's start/deadline blocks.

- `--compete` fetches each scored auction's record and inserts the
  challenger's surplus into the fairness-surviving score list: per-auction
  `field_rank` (rank, field size, gap-to-winner bps, winner solver), and a
  per-solver summary — rank-1 %, top-3 %, median rank/gap, and a rivals
  table (who beat you, how often, by how much). Honesty label everywhere:
  `rank_basis: surplus_vs_score_proxy` — historical scores include protocol
  fees, challenger surplus does not, so the rank is a floor.
- `--archive-dir DIR` persists every fetched record as
  `DIR/<chain>/<auction_id>.json.gz` (idempotent) — a local competition
  dataset that outlives the API's retention window and feeds future
  fairness-simulation / reward-EV work.
- `--self-address 0x…` adds shadow-vs-actual: when your historical
  solverAddress appears in a record, the row carries its actual score,
  ranking, and fairness outcome next to the replayed one.
- Rows gain `auction_start_block` / `auction_deadline_block` — the auction
  CUT context (what bidders saw), complementing `settlement_block`.

## 0.7.2 — 2026-08-17

Scoring-fidelity batch from an independent line-level audit. Every item
below changes reported numbers or their labels — see README caveats.

- **Coverage-adjusted capture is the new headline.** Solver errors now keep
  the historical winner's surplus in the denominator (previously an errored
  auction vanished from the ratio — a solver could look better by failing
  hard auctions). The old ratio is kept as `capture_conditional_pct`
  ("when it answered, how competitive was it"), and a new
  `lost_to_errors_wei` reports winner surplus forfeited to errors/timeouts.
- **Exact best-combination optimizer.** The greedy highest-first selection
  of internally compatible solutions undercounted challengers (A=10 on two
  pairs beats B+C=6+6 split — greedy picked 10, optimum is 12). Now solved
  exactly (branch-and-bound; greedy fallback above 20 candidates, labeled
  via `combiner`).
- **U256 range enforcement.** `to_u256` accepted arbitrarily large Python
  ints; values above 2^256-1 are now rejected as invalid.
- **Scoring-basis labels.** Every row carries `baseline_quality`
  (`exact_uniform` / `wrapper_lower_bound` / `mixed`); a same-basis
  `capture_exact_basis_pct` (direct settlements only) is reported alongside
  the mixed-basis proxy, and the HTML cards are relabeled accordingly
  ("Winner fee take" → "Known direct-path fee wedge").
- **Per-transaction wrapper attribution.** UID-overlap proof is now enforced
  per settlement transaction, not per auction group (one attributed tx can
  no longer vouch for an unrelated one; drops count as `tx_uid_mismatch`).
- **A/B on byte-identical bodies.** One shared `deadline` per auction is
  computed before the solver loop (was regenerated per request); rows carry
  `replay_deadline`.
- **Readiness semantics.** `{"solutions": []}` counts as a healthy answer
  (schema-legitimate abstention) with bid coverage reported separately;
  "valid" wording clarified to auctions with ≥1 valid solution; READY now
  requires `--min-evidence` attempted auctions (default 10) — below that the
  best verdict is REVIEW. Readiness is now rendered in the HTML report.
- **`settlement_block`** replaces the row field `block` (kept one release as
  a deprecated alias): it is the settlement's block, NOT the auction cut
  block — README fork guidance corrected to match.
- Docs: `--max-auctions` keeps the **newest** auctions (behavior since
  0.7.1; help/README said oldest).

## 0.7.1 — 2026-08-16

- Wrapper-routed settlements (solver router contracts, ~43% of mainnet /
  ~40% of Base settlements) are attributed via the v2 by-tx-hash endpoint,
  proven by UID overlap against the S3 auction body, and scored on a
  clearly-labeled delivered basis (`entry: "wrapper"`). Previously excluded
  silently.
- Submitter identification prefers the competition `solverAddress` over the
  relay EOA. Coverage disclosure in the scorecard, `--readiness`, and a
  `_meta` line in `--json-out`. `--max-auctions` keeps the newest auctions.

## 0.7.0 — 2026-08-12

- `--readiness`: a one-screen pre-production readiness check for a solver
  endpoint. Replays recent auctions against the endpoint and reports answer
  rate, latency (p50/p95/max vs the solve budget), solution validity, and
  surplus captured against the on-chain winners, with a READY / REVIEW /
  NOT READY verdict and a copy-paste command to reproduce the exact run.
  A focused alternative to the full field scorecard for anyone evaluating a
  solver before staging or shadow. Emitted in the `--json-out`/`--html-out`
  summary under `readiness`.

## 0.6.0 — 2026-08-07

First public release.

- Replays archived CoW auctions (the public S3 instance bucket) against any
  solver's `/solve` endpoint, offline.
- Reconstructs each auction's winning set from on-chain settlement calldata
  and Trade events; groups multiple winning transactions per auction (CIP-67);
  optional cross-check against the v2 competition API (`--verify-api`).
- Scores both sides on the same basis: before-fee surplus over signed limits
  at uniform clearing prices, converted at the auction's reference prices.
- Validates solver responses the way the settlement layer would: per-order
  fill accounting, fill-or-kill exactness, fee-adjusted limit feasibility,
  strict U256 parsing; infeasible solutions are excluded and tallied.
- A/B mode: repeatable `--solver-url`, rotating call order, head-to-head
  panel, per-pair breakdown, solve-latency stats.
- Ten chains, content cache, concurrent fetching, streamed JSONL rows,
  single-file HTML report, watch mode.
- 37 offline tests against pinned fixtures; CI runs them plus lint and a
  packaging check on Python 3.10 and 3.12.

Pre-release development history is summarized in `docs/DESIGN_NOTES.md`.
