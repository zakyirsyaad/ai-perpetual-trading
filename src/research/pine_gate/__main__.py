"""``python -m research.pine_gate`` is the same as ``python -m research.pine_gate.cli``."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
