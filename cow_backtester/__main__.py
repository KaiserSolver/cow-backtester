"""`python3 -m cow_backtester` — same interface as the console script."""

import sys

from .backtest import cli, selftest

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        cli()
