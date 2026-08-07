# Contributing

Corrections are the most valuable contribution — especially anywhere this
tool's accounting diverges from what the protocol actually does. Every scoring
rule cites its source (GPv2 contracts or `cowprotocol/services`); if you can
show a rule is wrong, please open an issue with the primary-source reference.

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest          # offline suite — no network, must always pass
python3 -m ruff check .    # lint — must be clean
python3 -m cow_backtester.scorer --selftest && python3 -m cow_backtester --selftest  # network tests
```

Ground rules:
- The offline suite stays offline. New scoring behavior needs a fixture-based
  test, and exploit-class regressions (duplicate trades, fee padding, negative
  numerics, malformed shapes) must keep passing.
- No silent drops: anything excluded from scoring gets a tallied reason that
  reaches the scorecard.
- No new runtime dependencies without strong cause (`eth_abi` is the only one).
- The pinned reference values (auction 8339027: event 2385773, surplus 1471,
  fee 805) are load-bearing; never adjust them to make a test pass.
