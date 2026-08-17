# cow-backtester

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
pip install .          # installs the `cow-backtester` command (one dependency: eth_abi)

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
report instead of the full field scorecard:

```bash
cow-backtester --chain base --blocks 2000 --rpc-url <your-rpc> \
        --solver-url http://localhost:8080 --solver-name mine --readiness
```

It replays recent auctions against your endpoint and reports the four things
that gate a pre-prod solver — does it answer, is it fast enough, are its
solutions valid, and are they competitive with the on-chain winners — as
pass/warn checks with a `READY` / `REVIEW` / `NOT READY` verdict:

```
====================================================================
  READINESS — mine   [REVIEW]
  base · prod · blocks 49506127..49510000
====================================================================
  [PASS] reached auctions         50 auctions attempted
  [WARN] no transport errors      2/50 errored
  [PASS] answers reliably         96% returned a solution
  [PASS] inside the deadline      0 past deadline
  [PASS] latency headroom         p95 420 ms of a 15000 ms budget
  [PASS] solutions are valid      98% passed limit/fee checks
  [PASS] competitive vs winners   61% of winner surplus captured
```

It prints the exact `--from-block/--to-block` command to reproduce the run,
and the same data lands in `--json-out`/`--html-out` under `readiness`. Works
on any of the supported chains, so you can readiness-check an endpoint for a
chain you are not yet onboarded on. It is a signal, not a settlement
guarantee — pair it with a self-hosted shadow run before going to production.

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

The bundled mock solver lets you see the full counterfactual/A/B output in
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
* What the number is: user surplus + protocol fees + network fee. CoW's
  official ranking score is user surplus + protocol fees only, so absolute
  levels here overstate the official score by the network fee (on the pinned
  reference trade, a $2.4 fill where the network fee dominates: 1471 vs an
  official 666 atoms; the gap shrinks as trades grow). When two solutions carry
  very different fees this basis can order them differently than CoW would;
  gas-heavy solutions are flattered. It is the only basis we found that can
  be computed symmetrically offline for both sides.

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
`expired_orders_pct`, flags). `--html-out` writes a single self-contained HTML
report: no external assets, light/dark aware, fine to attach to a PR or post.

## Flags

| flag | meaning |
|---|---|
| `--chain` | `mainnet`, `arbitrum-one`, `base`, `xdai`, `polygon`, `bnb`, `avalanche`, `linea`, `ink`, `plasma` |
| `--env` | `prod` (default) or `staging` |
| `--blocks N` | scan the most recent N blocks |
| `--from-block/--to-block` | absolute, reproducible window |
| `--max-auctions N` | cap auctions scored, newest first (default 25; 0 = all) |
| `--rpc-url` | your RPC (recommended — public defaults rot and rate-limit) |
| `--solver-url` / `--solver-name` | repeatable; two of them = A/B |
| `--solve-timeout S` | deadline advertised to the solver (HTTP waits S+5) |
| `--workers N` | concurrent RPC/S3 fetches (default 8) |
| `--cache-dir` / `--no-cache` | content cache (default `.cowbt-cache`) |
| `--json-out` / `--html-out` | machine-readable rows / HTML report |
| `--solver-map FILE` | JSON `{address: name}` to label winning submitters |
| `--verify-api` | cross-check winner txs against the v2 competition API |
| `--compete` | rank the challenger against the historical fairness-surviving field (rank / gap / rivals) |
| `--archive-dir DIR` | persist competition records as `DIR/<chain>/<id>.json.gz` (local dataset) |
| `--self-address 0x…` | your historical solverAddress → shadow-vs-actual comparison |
| `--min-evidence N` | attempted-auction floor before `--readiness` may say READY (default 10) |
| `--clamp-validto` | extend expired `validTo` so engines that filter them still solve |
| `--max-age-hours H` | warn when replayed auctions are older than this |
| `--watch N` | continuous mode: rescan every N seconds from the last block |
| `--quiet` | suppress progress (stderr); the scorecard still prints to stdout |
| `--version` | print version |

Progress goes to **stderr**, the scorecard to **stdout** (`2>/dev/null` gives
clean results; `--json-out`/`--html-out` are unaffected). Exit codes: **0**
success, **1** runtime error, **2** usage error, **130** interrupted mid-run
(a partial scorecard is printed when auctions had already been processed;
stopping `--watch` during its idle sleep is a clean stop and exits 0).

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
fee take 805. `mock_solver.py` (modes: empty / limit / better / hex / garbage /
invalid) exercises the wire path including validation and implausibility
guards. CI runs the offline suite on Python 3.10 and 3.12.

## Files

| file | role |
|---|---|
| `cow_backtester/scorer.py` | settlement decode: auction id + winner surplus |
| `cow_backtester/backtest.py` | enumerate, group, replay, validate, scorecard |
| `cow_backtester/cache.py` | immutable-fact content cache |
| `cow_backtester/report.py` | single-file HTML report |
| `mock_solver.py` | test/demo solver (six modes) |
| `fixtures/` | pinned reference data for the offline tests |
| `tests/` | offline pytest suite, no network |
| `docs/DESIGN_NOTES.md` | design decisions and validation history |

MIT licensed. Contributions and corrections welcome — especially from CoW core
devs on anything where this tool's accounting diverges from the protocol's.
</content>
