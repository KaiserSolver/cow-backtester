# Changelog

## 0.11.2 — 2026-09-23

A fix to the 0.11.1 winner-surplus sanity check, which could not see a
cluster of artefacts valued at one bogus reference price, contributed by
[@C-09-07](https://github.com/C-09-07) after running the tool on Plasma. Clean
and single-outlier windows print byte-identical output. Our own readiness
reports also moved out of the repository.

- **A cluster of valuation artefacts is caught, not only a single outlier.** The 0.11.1
  `winner surplus plausible` rule tested each attempted auction against the rest of the window
  combined, so at most one auction could fire and co-scaled artefacts masked each other. Plasma
  auctions 8939411, 8952964 and 9040629, whose buy token's `referencePrice` (≈5.0e37) valued each
  winner surplus at ≈7.4e32 wei, each sat at ≈0.5× the other two: nothing fired, and the sum-weighted
  capture read ≈0 % for every solver. The rule now tests the k largest auctions as a set and flags the
  largest set that qualifies: a strict minority of the window whose smallest member exceeds
  `ARTEFACT_RATIO` (100)× the window outside the set and, for k > 1, also `ARTEFACT_GROUP_GAP` (10⁹,
  next to the other two constants)× the largest auction outside it. The gap keeps a short window of
  ordinary auctions beside dust from reading as a cluster, since real order sizes spread across
  seven orders of magnitude. k = 1 is the 0.11.1 rule unchanged: clean and single-outlier windows
  print byte-identical output. `rest_wei` is now the window outside every flagged auction; flagged
  auctions are still listed with their ratio, never dropped. JSON: `artefact_rule` gains `group_gap`
  and a longer `basis`. Found running the tool on Plasma, reported and fixed by
  [@C-09-07](https://github.com/C-09-07) in [#1](https://github.com/KaiserSolver/cow-backtester/pull/1).
- **Our own readiness reports moved out.** `docs/readiness/` is gone from this repository. The
  reports (2026-08-22 and the three of 2026-09-14, byte-identical) and every future run live in a
  checksummed record of their own, `KaiserSolver/kaisersolver-readiness`, each with its SHA-256 and
  the fingerprints of the rows it was built from. The tool is for any solver; one solver's record does
  not belong in its checkout. The versions cited on the forum remain at commit `e7b3b73`.

## 0.11.1 — 2026-09-15

A winner-surplus sanity check, two readiness readings the sum-weighted
capture was hiding, and a `--watch` file-handling fix. Prompted by the BNB
report of 2026-09-14 (`docs/readiness/kaisersolver-bnb-2026-09-14.md`),
whose 0.0 % capture was one auction.

- **A valuation artefact in the winner data no longer decides the verdict.**
  BNB auction 25459284 — a 2 USDC → MCH sell order whose `referencePrice`
  valued the MCH received at ≈251,000 BNB against 0.0028 BNB sold — decoded
  to a winner surplus of 154,228 BNB, 6,003× the rest of a 2,595-auction
  window combined, and alone turned the chain's capture from 5.65 % into
  0.00 % and `competitive vs winners` into a WARN. `--readiness` now flags an
  attempted auction whose decoded winner surplus exceeds `ARTEFACT_RATIO`
  (100)× the winner surplus of every other attempted auction combined, once
  at least `ARTEFACT_MIN_ATTEMPTED` (20) auctions were attempted; both
  constants live next to `THRESHOLDS`, and a window whose other auctions
  carry no surplus has nothing to measure against, so nothing fires. Such an
  auction is listed with its ratio (and the competition `referenceScore`
  when `--compete` fetched it) on a warn-level `winner surplus plausible`
  check — the mirror of `prices look plausible`, which is about the
  challenger's prices — so the window is REVIEW at best and a human sees
  what was excluded. Capture prints both ways: an `ex-artefact capture`
  line under `surplus vs winners`, and the `competitive vs winners` detail
  carries the including-figures; its verdict reads the ex-artefact capture.
  The auction leaves both sides (our surplus on it is valued at the same
  bogus price). JSON: `capture_ex_artefact_pct`,
  `capture_conditional_ex_artefact_pct`, `artefact_auctions` (`auction_id`,
  `winner_surplus_wei`, `rest_wei`, `ratio`, `our_surplus_wei`, `answered`,
  `winner_reference_score`) and `artefact_rule` (the constants and whether
  the rule could fire). The JSONL `_meta` line carries
  `winner_surplus_artefacts` (`auction_id`, `winner_surplus_wei`, `ratio`):
  rows are streamed before the window total is known, so the per-row line
  cannot carry the flag — join on `auction_id`. With no artefact every
  number, line and check is unchanged. The rule is single-pass (each auction
  against all the others), so at most one auction can ever satisfy it; two
  artefacts of similar size would mask each other.
- **Per-bid median ratio.** A `per-bid median` line prints the median of
  (our best valid surplus ÷ the winner's surplus) over the unflagged bid
  auctions — answered with ≥ 1 solution, not `implausible_surplus`, not an
  artefact, winner surplus > 0 (a bid with no valid solution counts as 0) —
  next to the sum-weighted capture, because a solver can match the winners
  bid for bid and still capture little when it never enters the largest
  auctions. JSON: `per_bid_median_ratio`, `per_bid_ratio_n`,
  `per_bid_ratio_basis`.
- **`--json-out` appends under `--watch`.** The file was opened in `w` mode,
  so a restarted watch run truncated the fixed file name (operators were
  summing files to work around it). Under `--watch` it is now opened in
  append mode — the same JSON Lines shape, rows plus one `_meta` line per
  cycle — and a partial last line left by a killed run is terminated before
  the first new row. Without `--watch` a run still starts a fresh file. The
  first cycle after a restart re-scans `--blocks` from head, so dedupe by
  `auction_id` when summing a file across restarts.

## 0.11.0 — 2026-09-14

Readiness-standard batch. The 2026-09-14 audit (`readiness-inputs-report.md`)
found that `--readiness` measured against a budget nobody observed, on a
sample nobody could reproduce, with a validity that was not the protocol's.
This release makes the tool measure what a public specification can say it
measures. Every numeric choice now lives in a table, never in a literal.

- **The budget is the driver's, not a 20 s constant.** The replay used to
  overwrite the archived `deadline` with `now + --solve-timeout` (default
  20 s) on every chain; the real settle-lane budget is 4.6–4.9 s (audit
  G.1). `BUDGETS_S` carries the observed per-chain values (Arbitrum 4.84,
  Base 4.62, BNB 2.35); `--solve-timeout` is now an optional OVERRIDE
  (float, > 0), and `--readiness` on a chain with no observed budget and no
  override exits 2 before any network call — an `assumed` budget must never
  produce a verdict (plain replays fall back to `ASSUMED_BUDGET_S`, labeled).
  Rows keep the archived value as `original_deadline`; with `--compete`
  they also carry `original_budget_upper_s` (deadline minus the
  auction-start-block timestamp — an upper bound on the true budget, and
  labeled so) and the readiness header prints its p50/p95. The header,
  JSON and `_meta` line carry `budget_s` and `budget_source`
  (`observed` | `override` | `assumed`).
- **A capped or thin sample cannot be READY.** `--max-auctions` defaults
  to 0 (no cap); any cap adds a `sample capped` WARN and the header says
  `SAMPLE CAPPED (--max-auctions N)`. `min_evidence` moves into the
  threshold table at 500 attempted auctions (`--min-evidence` overrides);
  below it the header says `INSUFFICIENT SAMPLE (attempted N < 500)`. The
  header now prints the window three ways — block range, wall-clock span
  of the attempted auctions (auction-start timestamps under `--compete`,
  settlement timestamps otherwise), and `settlements found / auctions
  formed / attempted / replayed / returned` — plus `excluded` with the top
  three reasons; JSON adds `window`, `counts`, `excluded` and
  `auctions_per_hour`.
- **Deadline misses mirror the driver.** `errored`/`late` are gone. A
  socket `timeout` is a deadline miss (the driver got nothing in time), and
  so is an answer that lands after the budget (`late` — the driver would
  have discarded it; it still counts in the answered-only latency sample so
  it cannot flatter p95). Everything else is `transport`. The per-reason
  dict is keyed by the driver's labels — `DeadlineExceeded`,
  `SolverHttpError`, `SolverDeserializeError`, `SolverDtoError` — with the
  fine-grained reason underneath. Checks: deadline misses and transport
  errors each PASS at 0, WARN at ≤ 1% of attempted, FAIL above (the old
  `late*5 <= attempted` / `errored < attempted` rules are gone). Rows carry
  `outcome` and `driver_result`.
- **Validity is the protocol's three-part definition.** (i) eligibility is
  unchanged, with `valid_zero_surplus` counting fills at the limit;
  (ii) UDCP is a named structural check (`udcp_checked` / `udcp_violation`
  when a token is priced twice under two spellings); (iii) CIP-67 fairness
  is applied to the CHALLENGER when a competition record is available:
  `competition.pair_baselines` re-derives the autopilot rule (best
  single-pair solution per directed pair; a multi-pair solution is filtered
  when any pair scores below its baseline; the challenger's own single-pair
  solutions raise the baselines as they would in the real auction).
  Filtered solutions count in `fairness_filtered`, leave `n_valid` and the
  capture numerator, and `validity_basis` says
  `feasibility+eligibility+udcp+fairness` or `…+fairness:not-evaluated`.
- **Thresholds live in `THRESHOLDS`** (`default` plus per-chain overrides;
  v0 ships `default` only). The header prints the profile and every value
  applied, `(override: …)` marks CLI overrides, and the JSON carries the
  resolved dict with a per-key source.
- **Capture: disclosure, not a new formula.** The basis mix
  (`exact_uniform` / `wrapper_lower_bound` / `mixed` among attempted
  auctions) is printed and in the JSON next to `capture_pct` and
  `capture_conditional_pct`; the record's `referenceScore` is copied to the
  row as `winner_reference_score` for the reader (no check uses it).
- **Runs are reproducible after the bucket evicts the bodies.**
  `--archive-bodies DIR` stores every body used as
  `DIR/<chain>/<id>.json.gz` (canonical JSON, digested before the replay
  touches it) plus `manifest.jsonl`; `--bodies-dir DIR` replays from the
  archive and skips a missing body as `body_not_archived`, never falling
  back to S3. Every row carries `body_sha256`. The `Reproduce this run`
  line is a complete command (`--bodies-dir`, the resolved
  `--solve-timeout`, `--min-evidence`, any cap) with the tool version and a
  placeholder for the engine build sha the operator must fill in.

Also:

- `competition.py` no longer advertises the deprecated per-solution
  `clearingPrices` field (empty on recent autopilots; never read).
- `docs/readiness/kaisersolver-base-2026-08-22.md` is marked superseded
  (measured against the pre-0.11 20 s budget and a 25-auction cap).
- `build_parser()` / `main(argv)` for offline CLI tests; the HTML report's
  counterfactual table shows `failed` (transport + deadline misses).

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
