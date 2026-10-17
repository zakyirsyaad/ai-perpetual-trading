"""``python -m research.mode_backtest`` runs the command line of ``research.mode_backtest.cli``."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
