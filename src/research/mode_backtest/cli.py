"""Command line of the mode backtest: ``python -m research.mode_backtest <command>``.

Research code; the bot never imports it. Thin wiring that parses arguments and maps errors to exit
codes, as ``research.pine_gate.cli`` does:

* ``step0 --data-dir --pine-dir --config --out-dir`` runs ``step0.run_step0``: it replays the Pine
  Phase 1 holdout, checks it against the holdout file and writes the split by side and condition.
  When the check fails, every difference is printed on stdout before the error line.
* ``verify-copy --data-dir --sha256sum`` compares a local data copy with the ``sha256sum``
  listing printed on the VPS (``data.verify_copy``) and prints every problem.

Exit codes: 0 success, 1 an expected failure (one ``error:`` line on stderr and no traceback,
unless ``MODE_BACKTEST_DEBUG=1``), 2 a usage error. Output files are never overwritten.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from research.pine_gate.cli_common import CliError, one_line

from . import step0
from .data import verify_copy

DEBUG_ENV = "MODE_BACKTEST_DEBUG"
_EXPECTED_ERRORS = (CliError, step0.ReproductionError, OSError, ValueError)


def now() -> datetime:
    """The clock that dates the output files; tests replace it."""
    return datetime.now(timezone.utc)


def _path(text: str) -> Path:
    return Path(text).expanduser()


def cmd_step0(args: argparse.Namespace) -> int:
    try:
        json_path = step0.run_step0(args.data_dir, args.pine_dir, args.config, args.out_dir,
                                    now())
    except step0.ReproductionError as exc:
        for problem in exc.problems:
            print(problem)
        raise
    print(json_path)
    print(json_path.with_suffix(".md"))
    return 0


def cmd_verify_copy(args: argparse.Namespace) -> int:
    problems = verify_copy(args.data_dir, args.sha256sum.read_text(encoding="utf-8"))
    for problem in problems:
        print(problem)
    if problems:
        noun = "problem" if len(problems) == 1 else "problems"
        raise CliError(f"{len(problems)} {noun}: the copy in {args.data_dir} is not the VPS data")
    print("0 problems: every file matches the VPS listing")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m research.mode_backtest",
                                     description="Offline backtest of the LONG module (phase 1).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("step0", help="replay the Pine holdout, check it against its file and "
                                     "split it by side and condition")
    p.add_argument("--data-dir", required=True, type=_path,
                   help="the copied production data (the folder holding prod/)")
    p.add_argument("--pine-dir", required=True, type=_path,
                   help="the Pine Phase 1 selection, calibration and holdout files")
    p.add_argument("--config", required=True, type=_path, help="config/modes.yaml")
    p.add_argument("--out-dir", required=True, type=_path)
    p.set_defaults(handler=cmd_step0)

    p = sub.add_parser("verify-copy", help="compare a data copy with the VPS sha256sum listing")
    p.add_argument("--data-dir", required=True, type=_path)
    p.add_argument("--sha256sum", required=True, type=_path,
                   help="the listing printed by sha256sum on the VPS")
    p.set_defaults(handler=cmd_verify_copy)
    return parser


def _fail(exc: BaseException) -> None:
    """One line on stderr; with ``MODE_BACKTEST_DEBUG=1`` the traceback first."""
    if os.environ.get(DEBUG_ENV) == "1":
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    print(f"error: {one_line(exc)}", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed the usage message
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return args.handler(args)
    except _EXPECTED_ERRORS as exc:
        _fail(exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
