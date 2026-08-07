# Changelog

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
