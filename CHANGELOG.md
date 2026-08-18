# Changelog

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
