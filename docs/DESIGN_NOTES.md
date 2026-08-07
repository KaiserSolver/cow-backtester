# Design notes and validation history

Technical provenance for anyone auditing the tool. Every scoring rule in this tool cites a
primary source (the GPv2 contracts or `cowprotocol/services`); this file
records how the implementation was validated and which failure classes were
found and closed during development.

## How it was validated

Before release, every scoring rule was checked adversarially against the GPv2
contracts and `cowprotocol/services`, and the response validator was attacked
directly with hostile inputs. That process turned up two working exploits
(both fixed) and the failure classes listed below. Everything found is either
fixed and pinned as an offline regression test in `tests/`, or documented in
the README's limitations.

## The pinned reference settlement

Arbitrum auction **8339027**, settlement
`0xc103e7f2f31f6c302de778dda0fc5c10c8ec27e638b97fb47442235b305075cc`
(USDC→USDT partial fill, one of the auction's two CIP-67 winning txs):

- custom-price delivery reconstructed from calldata equals the on-chain GPv2
  `Trade` event exactly (2385773 atoms);
- before-fee surplus at uniform prices over signed limits: 1471 atoms;
  fee take (uniform minus custom delivery): 805 atoms;
- the reconstructed winning-tx set matched the v2 competition API on a
  five-auction spot check on Arbitrum, Aug 2026 (`--verify-api` prints the
  match/mismatch counters on any run).

The test suite pins these constants.

## Failure classes found and closed (all regression-tested)

- **Scoring-basis asymmetries**: scoring the winner at post-fee custom prices
  while a challenger uses pre-fee uniform prices; scoring against remaining
  instead of signed amounts; wrong surplus token for buy orders.
- **Aggregation**: treating each settlement tx as its own auction (CIP-67
  settles one auction through several winning txs); challenger scored as one
  solution instead of a disjoint winning set.
- **Response-validation exploits**: duplicate fulfillments double-counting
  surplus; fee padding manufacturing surplus (closed by the net-delivery
  feasibility check — the condition GPv2 itself enforces); negative/malformed
  numerics; malformed shapes crashing the run.
- **Silent data loss**: getLogs failures reading as "zero settlements";
  unattributable settlements dropped without a tally; solver transport
  failures deflating the capture ratio.
- **Integrity**: Trade events matched by topic only (spoofable via pre-hooks
  — closed with an emitting-address filter + 1:1 alignment requirement);
  auction-id suffix read without a canonical-encoding check; staging/prod
  contamination (closed with the body-uid cross-check).

## Design decisions

- Before-fee surplus basis (= user surplus + protocol fees + network fee):
  the only basis we found that can be computed symmetrically offline for both
  sides. The README documents how it differs from CoW's official ranking score.
- On-chain winner reconstruction rather than the competition API:
  trustless, retention-independent, verifiable; `--verify-api` cross-checks
  where the v2 endpoint has data.
- Claimed-not-simulated: the validator enforces feasibility (limits,
  fills, fees) but does not execute routes; implausible results are flagged,
  not hidden.
- Fork-at-block replay is out of scope: a solver's RPC configuration
  cannot be injected per-request; rows carry `block` so a cooperating solver
  can be pointed at a pinned fork externally.
