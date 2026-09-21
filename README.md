# cow-backtester

[![PyPI](https://img.shields.io/pypi/v/cow-backtester)](https://pypi.org/project/cow-backtester/)

Offline backtester, A/B harness, and counterfactual scorecard for CoW Protocol
solvers.

Point it at your own solver's `/solve` endpoint and it replays recent CoW
auctions against it, then scores each of your solutions against the set of
solutions that actually won on-chain. Point it at two endpoints and it runs
a head-to-head A/B on identical auctions, so a routing or config change can
be judged before it goes to production.

Nothing touches production: no shadow mode, no staging deployment, no keys.

## Why this exists

Today a solver can only be evaluated *live*: shadow mode consumes the
production auction stream, and the local playground runs against a chain fork.
Neither lets you take a fixed set of recent auctions, run your solver against
them offline, and ask "would I have out-surplused the winners, and by how
much?" the way you'd backtest a trading strategy. This tool does.

## Quick start

Python 3.10+.

```bash
pip install cow-backtester   # installs the `cow-backtester` command (one dependency: eth_abi)

# 1. Baseline — what the field actually captured (no solver needed)
cow-backtester --chain base --blocks 2000 --rpc-url <your-rpc>

# 2. Counterfactual — replay through your solver
cow-backtester --chain base --blocks 2000 --rpc-url <your-rpc> \
        --solver-url http://localhost:8080 --json-out results.jsonl

# 3. A/B — two solvers, same auctions, head-to-head
cow-backtester --chain base --blocks 5000 --rpc-url <your-rpc> \
        --solver-url http://localhost:8080 --solver-name baseline \
        --solver-url http://localhost:8081 --solver-name candidate \
        --html-out ab.html
```

From a source checkout without installing, `python3 -m cow_backtester ...`
works identically. Your endpoint only needs the standard CoW solver-engine
API (`POST /solve`).

## Readiness check

If you are bringing up a new solver and want a fast "is this endpoint healthy
enough to face production auctions?" read, `--readiness` prints a one-screen
report instead of the full field scorecard. We hold ourselves to it: our own
solvers' reports, run past the 500-auction floor with 0.11.0 on 2026-09-14, were
kept under `docs/readiness/` here until 2026-09-21 and now live in a separate,
checksummed record of their own (`KaiserSolver/kaisersolver-readiness`). This
repository ships the tool; that one is one solver's record of applying it to
itself. The versions cited on the forum remain in this repository's history at
commit `e7b3b73` (`git show e7b3b73:docs/readiness/kaisersolver-base-2026-09-14.md`).

```bash
cow-backtester --chain base --blocks 2000 --rpc-url <your-rpc> \
        --solver-url http://localhost:8080 --solver-name mine --readiness
```

It replays recent auctions against your endpoint and reports up to eleven
checks across four dimensions — does it answer, is it fast enough, are its
solutions valid, are they competitive with the on-chain winners — plus how
complete the field it was measured against actually was, as pass/warn checks
with a `READY` / `REVIEW` / `NOT READY` verdict. Real output, our own Base solver,
a 300-block run on 2026-09-14 with 0.11.0 (RPC redacted, nothing else):

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  base · prod · blocks 51302436..51302735
====================================================================
  window  : blocks 51302436..51302735 | attempted auctions span 2026-09-14T13:57:09Z → 2026-09-14T14:06:25Z (0.15 h, settlement-block timestamps; 187.77 attempted/h)
            settlements found 32 / auctions formed 29 / attempted 29 / replayed 29 / returned 7
            excluded 0
  budget  : 4.620 s (observed, BUDGETS_S[base].settle) | original-deadline upper bound n/a (needs --compete)
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  INSUFFICIENT SAMPLE (attempted 29 < 500)
  [PASS] reached auctions         29 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [WARN] bid coverage             24% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [PASS] latency headroom         p95 1107 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness:not-evaluated; fairness_filtered 0, zero_surplus 0, udcp 25 checked / 0 violations]
  [PASS] competitive vs winners   91% of winner surplus captured (coverage-adjusted; 91% conditional on answering; basis mix {'exact_uniform': 23, 'wrapper_lower_bound': 6})
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           32 settlements, 29 auctions formed, 0 excluded (0% of field; top reasons [])
  ----------------------------------------------------------------
  answered            : 29/29  (100%)   bids: 7   transport: 0   deadline misses: 0
  latency             : p50 348 ms / p95 1107 ms / max 1460 ms   (budget 4620 ms; answered-only incl. late answers)
  surplus vs winners  : 91% captured (coverage-adjusted) / 91% conditional   (7/29 valid)
  winner basis mix    : {'exact_uniform': 23, 'wrapper_lower_bound': 6}
  validity basis      : feasibility+eligibility+udcp+fairness:not-evaluated

  Reproduce this run:
    cow-backtester --chain base --env prod --from-block 51302436 --to-block 51302735 --bodies-dir <bodies-dir: rerun with --archive-bodies DIR to make this reproducible> --solve-timeout 4.62 --min-evidence 500 \
        --rpc-url <rpc> --solver-url http://127.0.0.1:11090/prod/base --solver-name kaisersolver --readiness
    # cow-backtester 0.11.0 · engine build sha: <fill in: the endpoint's boot-line git_sha (not exposed over /solve)>

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

Twenty-nine auctions is a smoke test, not evidence: the verdict is held at REVIEW by the
500-auction floor no matter how the checks read, and the header says so. The full-window
reports, run past the floor on all three chains the same day, are the three files linked above.

Two readings the sum-weighted capture hides (0.11.1). When one auction's decoded
winner surplus exceeds 100× the rest of the window combined it is a reference-price
valuation artefact, not delivered value (BNB auction 25459284 decoded to 154,228 BNB
and alone read the chain's capture as 0 %): it is listed with its ratio under a
warn-level `winner surplus plausible` check, an `ex-artefact capture` line prints
under `surplus vs winners`, and the `competitive vs winners` verdict reads the
ex-artefact figure with both numbers shown; the rule needs 20 attempted auctions. A
`per-bid median` line gives ours ÷ the winner's surplus at the median over the
unflagged auctions we bid on — a solver can match winners bid for bid and still capture
little when it never enters the largest auctions. In the readiness dict:
`artefact_auctions`, `capture_ex_artefact_pct`, `per_bid_median_ratio`.

It prints the exact `--from-block/--to-block` command to reproduce the run,
and the same data lands in `--json-out`/`--html-out` under `readiness`
(including `unscanned_blocks` and every skip reason). `--fail-on not-ready`
(or `review`) turns it into a CI gate: exit code 4 when the verdict trips.
The screen prints even under `--quiet`, which silences progress only. Works
on any of the supported chains, so you can readiness-check an endpoint for a
chain you are not yet onboarded on. It is a signal, not a settlement
guarantee — pair it with a self-hosted shadow run before going to production.

## Consistency economics (`--reward-ev`)

On most CoW chains the money is not in winning — it is in the CIP-85
consistency pool, which pays
`success_rate × Σ executed orders ( your best fair bid's surplus / everyone's )`.
`--reward-ev` computes the Σ term of that metric from the competition
records: your solver's counterfactual metric and pool share, every field
solver's real historical metric (a consistency leaderboard for the window —
this needs no `--solver-url` at all), and, with `--consistency-budget <COW>`,
a COW/week estimate. `success_rate` is not folded in silently: the win floor
is reported as a flag — v2 pays zero on a chain where a solver won nothing in
the period — and the estimate assumes it is met. Bases are matched: a field
solution carrying one order contributes its official `score`, the same basis
the replayed challenger is scored on (multi-order solutions fall back to the
net-of-fee per-order computation and are counted as such); auctions where
your solver errored stay in the denominator with a zero term; challenger
fairness is assumed while the field uses CoW's own `filteredOut` flags.

## Rank against the historical field (`--compete`)

Capture ratio tells you how much surplus you generate; `--compete` tells you
**where you would have ranked**. For every scored auction it fetches the
historical competition record (every submitted solution with its score,
CoW's own fairness-filtering outcome, and the winner) and inserts your
solver's result into the fairness-surviving score list:

```bash
cow-backtester --chain base --blocks 2000 --rpc-url <your-rpc> \
        --solver-url http://localhost:8080 --solver-name mine --compete \
        --archive-dir ./competition-data
```

The scorecard gains a field-rank line (rank-1 %, top-3 %, median rank,
median gap to the winner in bps) and a rivals table — which solvers beat
you, how often, and by how much. Per-auction `field_rank` lands in the JSON
rows. Two honesty notes: historical scores include protocol fees while your
replayed surplus does not, so the reported rank is a **floor** (labeled
`surplus_vs_score_proxy`); and records exist only for auctions that had a
winner. `--archive-dir` keeps every fetched record on disk — the API serves
roughly two months of history, so an archive you build today is a dataset
you keep. `--self-address` marks your historical solverAddress so rows show
shadow-vs-actual side by side.

### Try it without a solver

The mock solver in the source checkout (`mock_solver.py`; not installed by the wheel) lets you see the full counterfactual/A/B output in
about a minute, before wiring up your own engine:

```bash
MODE=limit  python3 mock_solver.py 8901 &   # fills exactly at the limit
MODE=better python3 mock_solver.py 8902 &   # fills at 1.5x the limit

cow-backtester --chain base --blocks 1500 --rpc-url <your-rpc> \
        --solver-url http://127.0.0.1:8901 --solver-name at-limit \
        --solver-url http://127.0.0.1:8902 --solver-name better --html-out demo.html
kill %1 %2
```

You get the per-solver panels, the head-to-head table, and the pair
breakdown. The `implausible_surplus` guard usually fires on the "better"
mock: claiming 1.5x the limit on real order flow is exactly what the
validator exists to flag.

## How it works (all public data)

1. Input: the exact `/solve` bodies CoW sent solvers, from the public S3
   instance bucket (`solver-instances.s3.amazonaws.com/<env>/<chain>/auction/<id>.json`).
   The archived file *is* a valid `/solve` request body.
2. Winner reconstruction: from the on-chain settlements. (The v1
   `/solver_competition` endpoints were removed in the competition-data
   migration; v2 exists but is retention-bounded. Reconstructing from chain
   data is trustless and independent of API retention — and `--verify-api`
   cross-checks it against v2 where available.) Settlements are grouped by
   auction: since combinatorial auctions (CIP-67), one auction can settle
   through several winning transactions, so the baseline is the combined
   winning set.
3. Replay and score: each auction body is POSTed to each solver; responses
   are validated (below) and scored with the same function, the same
   limits, and the same price convention as the winners. Your valid solutions
   are combined the CIP-67 way (best-first, disjoint directed token pairs).

## The comparison basis (read this before quoting numbers)

Both sides are scored on **before-fee surplus over the signed order limits, at
uniform clearing prices**, converted to the chain's native token at the
auction's own reference prices.

* Signed limits: CoW's accounting scores against the order's signed amounts
  (`fullSellAmount`/`fullBuyAmount`), not the remaining/fee-adjusted amounts.
* Uniform prices: on-chain settlements carry per-trade post-fee "custom"
  prices; the first occurrence of each token in the price vector is the uniform
  (pre-fee) price, which is what autopilot itself resolves. Scoring the winner
  post-fee but a challenger pre-fee would bias every comparison.
* Surplus token: buy token for sell orders, sell token for buy orders,
  converted at that token's `referencePrice` (matches official accounting).
* What the number is: measured against the v2 competition API's official
  `score` on seven live records (Arbitrum and Base, September 2026) this basis
  agrees to within 0.2% — on the pinned reference settlement,
  767,957,704,005 wei here vs an official 769,523,899,186. The CIP-38 score is
  after-fee surplus plus protocol fees, and the uniform-vs-custom price wedge
  this basis includes *is* the protocol fee (on Arbitrum and Base the driver
  bakes it into the custom prices; solvers report `fee: 0`). Earlier releases
  described this basis as "overstating the score by the network fee"; that
  comparison used the after-fee surplus as the reference, not the score.
  Residual deviations: a solver-determined fee (zero on those chains) is
  included here and not in the score, and buy-order surplus is valued at the
  sell token's reference price where the score converts at the limit ratio
  into the buy token (about 8% of Base orders are buy-kind). It is the only
  basis we found that can be computed symmetrically offline for both sides.

## Response validation (your solver can't accidentally cheat)

A solution is scored only if **every** fulfillment trade is feasible:

* known order uid (case-insensitive) and strict U256 numerics (decimal or
  0x-hex; negatives and malformed values rejected);
* prices present for both tokens;
* at most one fulfillment per order (GPv2 accumulates fills and the driver
  rejects duplicate trades, so N copies of a trade can't score N times the
  surplus);
* no over-fill (`executed + fee` vs `sellAmount` for sell orders, `executed`
  vs `buyAmount` for buy orders; exact for fill-or-kill);
* on-chain feasibility at the fee-adjusted terms: the net delivery must still
  cover the gross-scaled limit, which is what GPv2 enforces, so a padded `fee`
  cannot manufacture surplus.

Violations mark the whole **solution INVALID** (tallied with a reason), never a
silent zero. Solutions scoring >10× the winning set (or >0.001 native when the
winning set scored 0) are flagged `implausible_surplus`. Malformed responses
are tallied, never crash a run. **Prices are claimed, not simulated** — the
tool checks feasibility; it does not execute routes.

## What a run prints

* COVERAGE: settlements found, auctions formed/scored, every skip with its
  reason, unscanned block spans, auction ages, cache stats, `--verify-api` results.
* FIELD SURPLUS: winner surplus by USD size bucket, plus the winners' fee take.
* WINNING SUBMITTERS: who is actually winning (label them with `--solver-map`).
* COUNTERFACTUAL (per solver): returned / valid / positive / beat the
  winning set, surplus sums, capture ratio, solve latency p50+p95, invalid
  reasons, error classes.
* HEAD TO HEAD (exactly two solvers): side-by-side metrics, per-auction
  win counts, and the surplus delta.
* TOP PAIRS: winner surplus by token pair with each solver's surplus
  beside it: *where* you win and lose, not just by how much.

A real single-solver run (Arbitrum, eight auctions, trimmed):

```
====================================================================
  COUNTERFACTUAL — kaisersolver  (8 replayed)
====================================================================
  returned a solution : 7/8
  valid solutions     : 7/8
  positive surplus    : 7/8
  beat the winning set: 0/8
  our surplus (sum)   : 0.001643 ETH
  winners (sum)       : 0.001748 ETH  (replayed auctions only)
  capture ratio       : 94.0% of the winning set
  solve latency       : p50 1174 ms / p95 2471 ms

====================================================================
  TOP PAIRS BY WINNER SURPLUS (top 2)
====================================================================
                        pair  trades         winner   kaisersolver
                   WETH->MOR       7       0.001722       0.001643
                 USDC->USD₮0       1       0.000026       0.000000
```

Eight auctions is a small sample, but the shape is the point: this solver is
consistently a few percent behind one competitor on one pair, and the table
says which pair. That took minutes to learn here; it took weeks from logs.

`--json-out` streams one row per auction (block, timestamp, age, winner txs +
submitters + surplus/fees, per-solver validation detail and latency,
`expired_orders_pct`, flags). Under `--watch` the file is appended to, so a
restart continues the same JSON Lines file (one `_meta` line per cycle; the
first cycle after a restart re-scans `--blocks` from head, so dedupe by
`auction_id` when summing); a one-shot run overwrites it. `--html-out` writes a
single self-contained HTML report: no external assets, light/dark aware, fine
to attach to a PR or post.

## Flags

| flag | meaning |
|---|---|
| `--chain` | `mainnet`, `arbitrum-one`, `base`, `xdai`, `polygon`, `bnb`, `avalanche`, `linea`, `ink`, `plasma` |
| `--env` | `prod` (default) or `staging` |
| `--blocks N` | scan the most recent N blocks |
| `--from-block/--to-block` | absolute, reproducible window |
| `--max-auctions N` | cap auctions scored, newest first (default 0 = all; a cap holds `--readiness` at REVIEW) |
| `--rpc-url` | your RPC (recommended — public defaults rot and rate-limit) |
| `--solver-url` / `--solver-name` | repeatable; two of them = A/B |
| `--solve-timeout S` | OVERRIDE the per-request budget advertised to the solver, seconds (HTTP waits S+5). Default: the observed driver budget for the chain (`BUDGETS_S`); `--readiness` refuses to run on a chain with no observed value unless given |
| `--workers N` | concurrent RPC/S3 fetches (default 8) |
| `--cache-dir` / `--no-cache` | content cache (default `.cowbt-cache`) |
| `--json-out` / `--html-out` | machine-readable rows / HTML report |
| `--solver-map FILE` | JSON `{address: name}` to label winning submitters |
| `--verify-api` | cross-check winner txs against the v2 competition API |
| `--compete` | rank the challenger against the historical fairness-surviving field (rank / gap / rivals) |
| `--archive-dir DIR` | persist competition records as `DIR/<chain>/<id>.json.gz` (local dataset) |
| `--self-address 0x…` | your historical solverAddress → shadow-vs-actual comparison |
| `--reward-ev` | CIP-85 v2 consistency economics: counterfactual metric, field leaderboard, COW estimate (implies `--compete`) |
| `--consistency-budget N` | the chain's weekly consistency pool in COW, to convert share → COW/week |
| `--readiness` | one-screen pre-prod verdict for the endpoint(s) instead of the field scorecard |
| `--min-evidence N` | OVERRIDE the attempted-auction floor before `--readiness` may say READY (table default 500) |
| `--archive-bodies DIR` | store every auction body used as `DIR/<chain>/<id>.json.gz` + `manifest.jsonl` so the run survives S3 eviction |
| `--bodies-dir DIR` | replay from a `--archive-bodies` archive; a missing body is `body_not_archived`, never an S3 fallback |
| `--fail-on not-ready\|review` | with `--readiness`: exit 4 when a verdict trips — a CI gate |
| `--clamp-validto` | extend expired `validTo` so engines that filter them still solve |
| `--max-age-hours H` | warn when replayed auctions are older than this |
| `--watch N` | continuous mode: rescan every N seconds from the last block; `--json-out` appends across restarts |
| `--quiet` | suppress progress (stderr); the scorecard and the readiness screen still print to stdout |
| `--version` | print version |

Progress goes to **stderr**, the scorecard to **stdout** (`2>/dev/null` gives
clean results; `--json-out`/`--html-out` are unaffected). Exit codes: **0**
success, **1** runtime error, **2** usage error, **4** `--fail-on` readiness
gate tripped, **130** interrupted mid-run (a partial scorecard is printed when
auctions had already been processed; stopping `--watch` during its idle sleep
is a clean stop and exits 0).

A/B runs post byte-identical bodies to every solver **concurrently** under one
shared deadline, so each endpoint gets the same inputs and the same wall-clock
budget. Any HTTP 200 that is not `{"solutions": [...]}` is an error
(`bad_schema`), never a healthy abstention; `{"solutions": []}` is the only
legitimate empty answer. The JSONL `_meta` line carries `failed_ranges`,
`unscanned_blocks`, `competition_missing`, `validto_clamped_auctions` and
`winner_surplus_artefacts` (rows are streamed before the window total is known,
so the artefact flag lives here — join on `auction_id`) so a pipeline inherits
the run's coverage caveats.

## Caching

Only **immutable** facts are cached: archived auction bodies, settlements below
the reorg margin, and block timestamps. Nothing derived from live liquidity is
ever cached, so a warm re-run is faster (3.8x on a measured six-auction,
two-solver run) without changing a single number. Delete `.cowbt-cache` any time, or pass `--no-cache`.

## Limitations

* **Replay uses live liquidity.** Your solver quotes against *current* chain
  state, not the historical block. The counterfactual is **indicative** — it
  answers "how does my solver handle this real order flow", not "the exact
  outcome at that block". Ages are printed and a warning fires beyond
  `--max-age-hours` (default 6h). Prefer recent, short windows. Each row
  carries `settlement_block` — the block the winning settlement landed in,
  which is *later* than the auction cut block the bidders actually saw. A
  fork pinned there is an approximation, not a faithful auction replay (it
  can even include the settlement itself); true historical-fork replay needs
  the auction cut block, which this tool does not yet reconstruct.
* **The S3 bucket retains roughly one month** of auctions (measured Aug 2026).
  This is a recent-window backtester, not an archive.
* **Entrypoint coverage.** Direct `settle()` calls are attributed by the
  auction id appended to their calldata. Wrapper-routed settlements (solver
  router contracts — measured Aug 2026 at **~43% of mainnet, ~40% of Base,
  ~2% of Arbitrum** settlements, and *larger* than direct ones at the median,
  so they are not a random slice) are attributed via the v2
  `solver_competition/by_tx_hash` endpoint and **proven by uid overlap with
  the S3 auction body** before scoring; they score on a DELIVERED basis
  (Trade events vs signed limits), which understates the direct-path
  before-fee basis by the settlement's fee wedge (typically a few bps) —
  rows carry `entry: "wrapper"` so the bases are distinguishable. Settlements
  the endpoint cannot resolve are counted under `wrapper_unattributed` and
  disclosed in the coverage block, `--readiness`, and the `_meta` JSON line.
* **"Beat the winning set" is necessary, not sufficient.** Real winner
  selection also applies fairness filters, and bids score net of gas; the tool
  also takes your solutions at face value while the driver merges and simulates.
* **Orders keep their historical `validTo`** (~1%/day expire; more near the
  retention edge). Engines that filter expired orders look age-degraded — see
  `expired_orders_pct` per row, or pass `--clamp-validto`.
* **JIT orders.** The winner baseline credits surplus-capturing JIT trades
  (owners in `surplusCapturingJitOrderOwners`), per official accounting; the
  challenger side never credits JIT (a response's JIT order has no verifiable
  owner). On auctions where winner surplus comes from CoW-AMM-style JIT, the
  comparison is conservative *against* the challenger, never in its favor.
* **Bring your own RPC.** Public defaults rot and rate-limit `eth_getLogs`; the
  tool splits and retries failing ranges and always **reports** any span it
  could not scan rather than under-counting silently. `eth_chainId` is checked
  against `--chain` on startup.

## Provenance of the reconstruction

* Auction id: CoW's driver appends it to `settle()` calldata; autopilot reads
  back exactly the **last 8 bytes** (`META_DATA_LEN = 8` in
  `cowprotocol/services`). The tool applies the same rule, only when the
  calldata prefix re-encodes canonically, cross-checked by requiring **at least
  one** settled order uid to appear in the fetched body (`body_uid_mismatch`
  guard — this also catches staging/prod contamination, which shares the
  settlement contract).
* Trade events are filtered by **emitting address + topic** and must align 1:1
  with calldata trades, else the settlement is skipped (`trade_event_mismatch`).
* Fill-or-kill: GPv2 ignores the calldata `executedAmount` and uses the signed
  amount — the tool applies the same substitution.
* Response-schema authority: `crates/solvers-dto` / the solver-engine OpenAPI
  in `cowprotocol/services`.
* `--verify-api` compares the reconstructed winning-tx set per auction against
  the v2 competition endpoint (match / mismatch / unavailable counters).

## Tests

```bash
pip install -e ".[dev]"                      # pytest + ruff (one-time)
python3 -m pytest                            # OFFLINE suite (fixtures; no network)
python3 -m cow_backtester.scorer --unittest  # offline core checks
python3 -m cow_backtester.scorer --selftest  # network: winner path on the reference settlement
python3 -m cow_backtester --selftest         # network: counterfactual == winner baseline +
                                             # every exploit class as regression cases
```

The pinned reference (Arbitrum auction 8339027): decode integrity is exact
against the GPv2 `Trade` event (2385773 atoms), before-fee surplus 1471 atoms,
protocol-fee wedge 805 atoms, 767,957,704,005 wei vs the official score of
769,523,899,186. The network self-test picks a recent settlement automatically
once the pinned auction ages out of the S3 bucket. `mock_solver.py` (modes:
empty / limit / better / hex / garbage / invalid; from a source checkout, it is
not installed by the wheel) exercises the wire path including validation and
implausibility guards. CI runs the offline suite on Python 3.10, 3.12 and 3.13.

## Files

| file | role |
|---|---|
| `cow_backtester/scorer.py` | settlement decode: auction id + winner surplus |
| `cow_backtester/backtest.py` | enumerate, group, replay, validate, scorecard |
| `cow_backtester/cache.py` | immutable-fact content cache |
| `cow_backtester/competition.py` | v2 competition records: fetch, archive, rank vs field |
| `cow_backtester/economics.py` | CIP-85 v2 consistency metric, field leaderboard |
| `cow_backtester/report.py` | single-file HTML report |
| `mock_solver.py` | test/demo solver (six modes) |
| `fixtures/` | pinned reference data for the offline tests |
| `tests/` | offline pytest suite, no network |
| `docs/DESIGN_NOTES.md` | design decisions and validation history |

MIT licensed. Contributions and corrections welcome — especially from CoW core
devs on anything where this tool's accounting diverges from the protocol's.
</content>
