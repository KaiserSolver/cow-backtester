# Readiness-standard fixes — backtester half (T1–T8)

**Branch:** `readiness-v0.11` from `72ffbde` (v0.10.0 + 2 CI commits); released as v0.11.0.
**Date:** 2026-09-14. **Audit referenced:** `readiness-inputs-report.md` (this repo, untracked), sections B, C, D, E, G.1, H.
**Runtime dependency:** still `eth_abi` only (`pyproject.toml:12`); the new code uses `hashlib`, `gzip`, `json`, `re`, `datetime` from the standard library.

## Commits on `readiness-v0.11`

| sha | tasks | subject |
|---|---|---|
| `ea43dbe` | T1–T7 | readiness: observed budget, sample floor, driver deadline taxonomy, protocol validity, threshold table, capture disclosure, body archive |
| `5432a98` | T8 | release: 0.11.0 — changelog, README flags, competition docstring, supersede the 08-22 readiness report |
| (this file) | — | readiness-fixes-report.md |

## Test-suite results

Commands: `python3 -m pytest` (offline suite; `addopts = "-q"`), `python3 -m cow_backtester.scorer --unittest`, `ruff check cow_backtester/ tests/`.

| suite | before (`72ffbde`) | after (`5432a98`) |
|---|---|---|
| `python3 -m pytest` | **86 passed** (tests/test_offline.py 60, tests/test_v0_10.py 26), exit 0 | **124 passed** (+38 in `tests/test_v0_11.py`, 0 removed), 0.61 s, exit 0 |
| `python3 -m cow_backtester.scorer --unittest` | UNIT PASS ×2 (surplus math; fixture decode) | UNIT PASS ×2 |
| `ruff check cow_backtester/ tests/` | not run | All checks passed |

Legacy tests touched (semantics, not deleted): five readiness tests in `tests/test_offline.py` and the readiness fixtures in `tests/test_v0_10.py` were re-pinned to the renamed counters (`errored`/`late`/`past_deadline` → `transport`/`deadline_miss`) and now pass `min_evidence=10` explicitly because the table floor is 500 (see T2). `test_readiness_slow_but_answering_is_review` was re-pinned at 1 deadline miss in 101 attempted (0.99 %): under the new band 1 in 21 (4.8 %) is correctly a FAIL, which the old assertion did not anticipate.

---

## T1 — Real budget instead of the 20 s constant

**Files changed:** `cow_backtester/backtest.py` (`BUDGETS_S`, `ASSUMED_BUDGET_S`, `HTTP_GRACE_S`, `resolve_budget()`, `stamp_deadline()`, `parse_iso_ts()`, `process_window` body/deadline section, `build_parser` `--solve-timeout`, `main` resolution before any RPC, `_meta` line).

**What changed**
1. `stamp_deadline()` preserves the archived value as `row["original_deadline"]` before overwriting. Under `--compete`/`--archive-dir` the auction-start block's timestamp is fetched (one cached `eth_getBlockByNumber`) and `row["original_budget_upper_s"] = original_deadline − auctionStartBlock_ts` (3 dp) is recorded with `row["original_budget_upper_basis"]` spelling out that it is an UPPER bound (send time unknown), per audit C.4. `parse_iso_ts` handles the driver's nanosecond timestamps.
2. `BUDGETS_S = {chain: {"settle": s | None}}` seeded from audit G.1 (`arbitrum-one` 4.84, `base` 4.62, `bnb` 2.35; the other seven chains `None`). `budget_source ∈ {observed, override, assumed}` is on the readiness header, the readiness JSON, every row (`budget_s`, `budget_source`) and the `_meta` line. `--readiness` on a chain with `None` and no override prints which chain lacks an observed budget and how to pass `--solve-timeout`, then exits 2 — before the RPC chain-id probe.
3. `--solve-timeout` is an optional override (`float`, default `None`, must be > 0). `body["deadline"] = now + budget` where budget is the resolved value.
4. HTTP read timeout is `budget + HTTP_GRACE_S` (5 s); an answer after `budget` is a deadline miss (T3).
5. The header prints `budget : 4.620 s (observed, BUDGETS_S[base].settle) | original-deadline upper bound p50 … / p95 … over N attempted` (or `n/a (needs --compete)`).

**Tests added** (`tests/test_v0_11.py`): `test_budget_default_is_observed_table_value` (base → 4.62 `observed`), `test_budget_override_wins` (`--solve-timeout 3` → 3.0 `override`), `test_budget_readiness_refuses_assumed` (exit 2, message names the chain), `test_cli_readiness_without_observed_budget_exits_2_before_network` (`--chain linea --readiness` exits 2 with the RPC patched to raise), `test_parser_defaults_no_cap_no_constant_budget`, `test_rows_preserve_original_deadline_and_stamp_budget`, `test_original_budget_upper_bound_needs_start_block`, `test_parse_iso_ts_handles_nanoseconds`.

**Deviations**
- `--solve-timeout` changed from `int` (floor 3) to `float` (> 0): observed budgets are fractional (4.62) and the brief's own example passes 3. The old `>= 3 s` floor is gone.
- The brief specifies the refusal for `--readiness` only. A plain replay (no `--readiness`) on a chain with no observed value falls back to `ASSUMED_BUDGET_S = 20.0`, labeled `assumed` on every row and in `_meta`, so the counterfactual scorecard keeps working on the seven un-audited chains.

**Unknown:** the observed values are settle-lane p50s from a 4–6-day log window (audit F.2); whether they drift week to week is `unknown` until the engine's `solve_timing` table (engine E3) has a month of data. What would settle it: re-run `scripts/intelligence/readiness_budget.py` monthly and update the table.

---

## T2 — Sample floor and window reporting

**Files changed:** `backtest.py` (`--max-auctions` default, `THRESHOLDS["default"]["min_evidence"]`, `--min-evidence` default `None`, `process_window` timestamp collection `attempted_ts` / `attempted_start_ts`, `readiness_report` header + `window`/`counts`/`excluded`/`auctions_per_hour`/`sample_capped`).

**What changed**
1. `--max-auctions` defaults to `0` (no cap). A cap > 0 adds a `sample capped` WARN check (so the verdict logic itself yields REVIEW for an otherwise-READY run; NOT READY stays NOT READY) and the header prints `SAMPLE CAPPED (--max-auctions N): a capped sample cannot be READY`.
2. `min_evidence` is `500` in the threshold table; `--min-evidence` overrides it (marked `(override: min_evidence)` in the header). Below the floor the verdict is capped at REVIEW and the header prints `INSUFFICIENT SAMPLE (attempted N < 500)`.
3. The header prints the window three ways: `blocks a..b`, the wall-clock span of the attempted auctions (`auctionStartBlock timestamps` under `--compete`, otherwise `settlement-block timestamps`, named in `window.ts_basis`), and `settlements found / auctions formed / attempted / replayed / returned`, plus `excluded N — top: [(reason, n) × 3]`.
4. JSON: `window {from_block, to_block, span_start_ts, span_end_ts, span_hours, ts_basis}`, `counts`, `excluded {n, top}`, `auctions_per_hour` (attempted ÷ span hours), `sample_capped`, `min_evidence`.

**Tests added:** `test_capped_sample_cannot_be_ready` (`--max-auctions 5`, all checks ok → REVIEW + header line), `test_thin_sample_is_review_with_insufficient_line` (20 attempted, all ok → REVIEW, `min_evidence == 500`, header line), `test_healthy_uncapped_full_sample_is_ready` (600 attempted → READY), `test_window_printed_three_ways_and_auctions_per_hour`, `test_excluded_top_three_reasons`, `test_parser_defaults_no_cap_no_constant_budget` (default run has no cap).

**Deviation:** none in substance. `auctions_per_hour` is `None` when the span is zero or `attempted` is 0 (a one-auction window has no rate).

---

## T3 — Deadline-miss taxonomy mirroring the driver

**Files changed:** `backtest.py` (`DRIVER_ERROR_LABELS`, `DRIVER_ERROR_DEFAULT`, `DEADLINE_MISS_REASONS`, `classify_outcome()`, `driver_error_label()`, `driver_error_table()`, `process_window` dispatch loop, `readiness_report` checks/JSON, `print_scorecard`, `build_summary`), `cow_backtester/report.py` (HTML counterfactual table column `failed`).

**What changed**
1. `errored` is replaced by `transport` and `deadline_miss`. `classify_outcome(err, ms, budget_ms)`: `timeout` → deadline miss; an answer with `ms > budget_ms` → deadline miss (`late`); `http_<code>`, `unreachable*`, `response_too_large`, `bad_json`, `bad_schema`, `bad_solver_response` → transport.
2. Per-reason dict keyed by the driver's labels with the fine reason underneath: `DeadlineExceeded {timeout, late}`, `SolverHttpError {http_*, unreachable*, response_too_large}`, `SolverDeserializeError {bad_json}`, `SolverDtoError {bad_schema, bad_solver_response}`. Rows carry `outcome` (`answered | deadline_miss | transport`) and `driver_result`.
3. `answer_rate = replayed / attempted` (unchanged), `transport_rate_pct`, `deadline_miss_rate_pct`. Latency percentiles stay answered-only; the header prints `answered-only incl. late answers` and the JSON carries `latency_basis` with the reason.
4. Thresholds (from `THRESHOLDS`): deadline misses PASS `= 0`, WARN `≤ 1 %`, FAIL `> 1 %`; transport the same. `late*5 <= attempted` and `errored < attempted` are gone.

**Tests added:** `test_classify_outcome_table`, `test_driver_labels`, `test_socket_timeout_is_a_deadline_miss_not_transport` (a real `URLError("timed out")` through `solve()` → `deadline_miss`, `{"DeadlineExceeded": {"timeout": 1}}`, `transport == 0`), `test_late_answer_is_a_deadline_miss` (2,000 ms answer vs 1.0 s budget → `deadline_miss`, `{"DeadlineExceeded": {"late": 1}}`), `test_http_502_is_transport` (`{"SolverHttpError": {"http_502": 1}}`), `test_old_error_keys_are_gone_and_rates_are_present` (`errored`, `past_deadline`, `late`, `solve_timeout_s` absent from the JSON), `test_deadline_miss_band_zero_warn_fail`, `test_transport_band_zero_warn_fail`.

**Deviations / interpretation**
- A late answer is NOT counted as `replayed` (the driver would have discarded it, so it cannot contribute bids, validity or capture) but its latency IS kept in the answered-only sample — it is an answer, a slow one, and dropping it would flatter p95. The brief does not say either way; this is the reading that mirrors the driver.
- `THRESHOLDS` carries both `transport_warn`/`transport_fail` and `deadline_miss_warn`/`deadline_miss_fail` as the brief lists them; with the brief's band (WARN ≤ 1 %, FAIL > 1 %) the two values coincide (1.0). The check uses the `_warn` key as the WARN ceiling; the `_fail` key is kept so a future profile can open a gap.

---

## T4 — Validity to the protocol definition

**Files changed:** `backtest.py` (`validate_and_score(resp, body, ref_prices, fairness_baseline=None)`, `process_window` fairness wiring and counters, `readiness_report` `validity_basis`), `cow_backtester/competition.py` (`pair_baselines()`; module docstring).

**What changed**
1. (i) unchanged; `valid_zero_surplus` counts solutions whose scored trades all sit at the limit (still valid), printed in the validity check detail and carried on rows and in the readiness JSON.
2. (ii) UDCP is a named structural check: `udcp_checked` counts every solution the check ran on; a token priced twice under two spellings (`0xAbC…` and `0xabc…`) is `udcp_violation` (invalidating, counted in `udcp_violations`). No prices are re-derived from executed amounts. The validity detail reads `udcp N checked / M violations`.
3. (iii) `competition.pair_baselines(record, body)` mirrors autopilot `winner_selection::compute_baseline_scores` (`crates/winner-selection/src/arbitrator.rs:676-691` in the engine repo, read 2026-09-14): for every submitted solution whose orders all lie on ONE directed `(sell, buy)` pair, the pair's baseline is the best such solution's `score`; multi-pair solutions never set a baseline; non-positive scores are skipped. `validate_and_score` then applies `partition_unfair_solutions` (`arbitrator.rs:61-105`): a feasible solution with more than one pair is filtered when any of its pairs scores below that pair's baseline (`score >= baseline` keeps it); single-pair solutions are never filtered; the challenger's own single-pair solutions raise the baselines exactly as they would in the real competition. Filtered solutions are counted in `fairness_filtered`, excluded from `n_valid`, from `by_order` (the CIP-85 consistency input) and from the capture numerator (T6). Without a record, `fairness = "not_evaluated"`, nothing is filtered, and the JSON says so.
4. `valid_rate = valid / returned` unchanged; `validity_basis` is `feasibility+eligibility+udcp+fairness`, `…+fairness:not-evaluated`, or `…+fairness:partial(N/M evaluated)` when only some attempted auctions had a record.

**Tests added:** `test_fairness_dominated_multi_pair_solution_is_filtered` (a two-pair challenger solution vs a field single-pair solution 1 wei better on one pair → filtered, `n_valid` 1 → 0, `best_surplus_wei` 0; equal or weaker field → kept), `test_fairness_single_pair_solutions_are_never_filtered`, `test_fairness_challengers_own_single_pair_raises_the_baseline`, `test_pair_baselines_from_record` (single-pair sets the baseline regardless of `filteredOut`; multi-pair, unknown-uid and zero-score solutions do not; `fairness_unmapped` counted), `test_fairness_not_evaluated_without_record_keeps_n_valid`, `test_fairness_evaluated_with_record_filters_and_reports` (end-to-end through `process_window` with a patched record), `test_zero_surplus_fill_is_valid_and_counted`, `test_udcp_named_check_counts_and_catches_double_pricing`.

**Deviations / interpretation**
- The brief says "apply the same CIP-67 filter used in `_fair_scores`". `_fair_scores` does not implement a filter — it reads CoW's `filteredOut` flag, which does not exist for a replayed challenger. The filter was therefore re-derived from the autopilot's implementation as described above; the historical field still uses CoW's flag everywhere it did before.
- Basis mismatch, disclosed: field baselines are CIP-38 scores (surplus + protocol fees, `record.score`), the challenger's per-pair values are the tool's uniform-price before-fee surplus, which agrees with the score to within 0.2 % (README, v0.10.0). A challenger pair within 0.2 % of a baseline can be classified either way. What would settle it: the field's per-pair scores are not published; only the autopilot's own scoring would.
- A field solution carrying an order the body does not know (a JIT order, an unknown uid) cannot be placed on a pair and is skipped (`agg["fairness_unmapped"]`); a smaller baseline set is the optimistic direction for the challenger, so the count is reported.
- An empty baseline set from a record with no single-pair solutions still counts as `evaluated` (the field set no baselines; nothing can be filtered).

---

## T5 — Per-chain threshold table

**Files changed:** `backtest.py` (`THRESHOLDS`, `resolve_thresholds()`, `readiness_report` uses only table values; header and JSON).

**What changed**
1. `THRESHOLDS = {"default": {...}, "<chain>": {...overrides...}}` with `answer_rate_pass/warn`, `transport_warn/fail`, `deadline_miss_warn/fail`, `latency_pass_frac` (0.5), `latency_warn_frac` (1.0), `validity_pass/warn`, `capture_pass/warn`, `min_evidence` (500), plus the two auxiliary knobs that were also literals (`bid_coverage_pass` 50, `field_coverage_excluded_max_frac` 0.05). v0 ships `default` only.
2. The header prints `thresholds: profile '<name>' — …every value…` and the JSON row carries `thresholds` (resolved dict), `threshold_profile`, `threshold_sources` (per key: `default | <chain> | override`).
3. `--min-evidence` and `--solve-timeout` win over the table; the header appends `(override: min_evidence)`; the budget line says `(override)`.

**Tests added:** `test_thresholds_default_profile_and_override`, `test_per_chain_threshold_override_changes_verdict` (monkeypatched `THRESHOLDS["base"] = {"latency_pass_frac": 0.9}` turns a REVIEW into READY and prints `profile 'base'`; a chain with no entry prints `profile 'default'`), `test_cli_override_marked_in_header`, `test_every_threshold_lives_in_the_table`.

**Deviation:** none.

---

## T6 — Capture ratio: disclosure, not a new formula

**Files changed:** `backtest.py` (`basis_mix` Counter per solver in `process_window`; `winner_reference_score` on rows; `readiness_report` prints and carries `basis_mix`, `capture_pct`, `capture_conditional_pct`).

**What changed:** formula and winner basis untouched (`our_surplus / winner_surplus_attempted`, `scorer.py` not modified). The numerator excludes fairness-filtered solutions because they never enter the disjoint combination (T4). The header prints `winner basis mix : {'exact_uniform': n, 'wrapper_lower_bound': m}` and the capture check detail includes it; `winner_reference_score` is the `referenceScore` of the highest-scoring winning solution in the record, copied for the reader and consumed by no check.

**Tests added:** `test_capture_formula_unchanged_and_basis_mix_printed`; the fairness-filtered surplus is asserted absent from `best_surplus_wei`/`our_surplus` in `test_fairness_dominated_multi_pair_solution_is_filtered` and `test_fairness_evaluated_with_record_filters_and_reports`; `winner_reference_score` asserted in the latter.

**Deviation:** when an auction has several winning solutions, `winner_reference_score` is the top-scoring winner's value (one number per row, as the brief names it), not a list.

---

## T7 — Reproducibility: body archive and replay-from-disk

**Files changed:** `backtest.py` (`body_sha256()`, `bodies_path()`, `archive_body()`, `load_archived_body()`, `process_window` body fetch, `build_parser` `--archive-bodies` / `--bodies-dir`, `readiness_report` reproduce line, `_meta`).

**What changed**
1. `--archive-bodies DIR` writes `DIR/<chain>/<auction_id>.json.gz` (canonical JSON: sorted keys, compact separators — the same bytes `body_sha256` digests) and appends to `DIR/manifest.jsonl` `{auction_id, chain, sha256, from_block, to_block, fetched_at}`. The body file is idempotent; the manifest records every run that used the body.
2. `--bodies-dir DIR` replays from the archive; a missing body is the skip `body_not_archived`. There is no S3 fallback (the test patches S3 to raise).
3. Every row carries `body_sha256`, digested before the deadline stamp and before `--clamp-validto`.
4. The reproduce line is a complete command: `cow-backtester --chain … --env … --from-block … --to-block … --bodies-dir <dir> --solve-timeout <resolved> --min-evidence <n> [--max-auctions N] [--compete] --rpc-url <rpc> --solver-url … --solver-name … --readiness` followed by `# cow-backtester 0.11.0 · engine build sha: <fill in …>` (the engine does not expose its build sha over `/solve`; engine E2 puts it on the boot line). The `_meta` line carries `bodies_source`, `archive_bodies`, `bodies_dir`.

**Tests added:** `test_archive_then_replay_from_disk_is_identical` (an S3 run with `--archive-bodies` followed by a `--bodies-dir` run over the same blocks with S3 patched to raise: identical per-solver counters, `body_sha256`, `original_deadline`, verdict and check levels; manifest fields; reproduce line carries `--bodies-dir`, the resolved `--solve-timeout 4.84`, `--min-evidence 500`, the engine-sha placeholder), `test_missing_archived_body_is_a_visible_skip` (`body_not_archived`, no rows), `test_body_sha256_is_canonical`, `test_archive_and_bodies_dir_are_exclusive` (usage error, exit 2).

**Deviations / interpretation**
- "Every S3 body used in the run" is read as every body fetched for the window's auctions (after the `--max-auctions` cut), including bodies later skipped by the uid cross-check — archiving before the skip is what makes the skip itself reproducible.
- `--archive-bodies` and `--bodies-dir` together are a usage error (an archive replays from itself).

**Unknown:** the archive/replay identity is proven offline on the fixture; a live S3 round-trip on a real window was not run in this session (no network in the suite, and the brief forbids touching endpoints from here). What would settle it: one `--archive-bodies` run followed by one `--bodies-dir` run on the same `--from-block/--to-block` against the staging route.

---

## T8 — Version, changelog, stale docs

**Files changed:** `cow_backtester/_version.py` (`0.11.0`), `CHANGELOG.md` (0.11.0 entry in the existing style, one bullet per T1–T7 plus "Also"), `cow_backtester/competition.py` (module docstring no longer lists per-solution `clearingPrices` as returned; states it is deprecated/empty and unread, and where the challenger fairness rule comes from), `docs/readiness/kaisersolver-base-2026-08-22.md` (superseded banner at the top: pre-0.11 20 s budget and 25-auction cap; not regenerated), `README.md` (flag rows for `--max-auctions`, `--solve-timeout`, `--min-evidence` updated; rows for `--archive-bodies` / `--bodies-dir` added).

**Tests:** the suite runs against the bumped version (`VERSION` is imported by `scorer.py` and stamped on every row/reproduce line); no dedicated version test exists in the repo and none was added.

**Deviation:** README flag rows were updated beyond the two files the brief names, because they stated the old defaults (25 / 10 / constant timeout) and would have been the most-read stale doc.

---

## Not done / out of scope, stated

- Tagging and the PyPI release are a separate step (see CHANGELOG 0.11.0).
- `readiness-inputs-report.md` and `readiness_budget_distribution.py` from the 2026-09-14 audit remain untracked in this repo, as that brief required; they are not part of these commits.
- The winner-surplus reconstruction (`scorer.py`), the disjoint combiner and the S3/RPC caches are untouched, per the brief.
- The HTML report (`report.py`) shows `failed` (transport + deadline misses) in the counterfactual table; it does not yet render the new readiness header fields (window, budget, thresholds) beyond the checks table — the JSON carries them.
