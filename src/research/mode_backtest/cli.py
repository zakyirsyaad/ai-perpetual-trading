"""Command line of the mode backtest: ``python -m research.mode_backtest <command>``.

Research code; the bot never imports it. Thin wiring that parses arguments and maps errors to exit
codes, as ``research.pine_gate.cli`` does:

* ``step0 --data-dir --pine-dir --config --out-dir`` runs ``step0.run_step0``: it replays the Pine
  Phase 1 holdout, checks it against the holdout file and writes the split by side and condition.
  When the check fails, every difference is printed on stdout before the error line.
* ``verify-copy --data-dir --sha256sum`` compares a local data copy with the ``sha256sum``
  listing printed on the VPS (``data.verify_copy``) and prints every problem.
* ``train`` and ``test``, each with ``--mode --pair --config --data-dir --out-dir``, run
  ``stages.train_stage`` and ``stages.test_stage``: the training stage and its lock, then the
  one-shot test against it. Each prints the files it wrote (and the start record and run marker of
  the test), then its status line. The lock and the start record go in ``--out-dir``, the run
  marker in ``--data-dir``/holdout_runs. Once either record exists, ``test`` and ``train`` both
  refuse (exit 2): the marker for its data folder, the start record for its out folder whatever
  data folder a rerun names. So run both stages with one pair of folders. Phase 1 runs Mode A on
  BTC-USDT only; anything else is an expected failure.

Exit codes: 0 success, 1 an expected failure (one ``error:`` line on stderr and no traceback,
unless ``MODE_BACKTEST_DEBUG=1``), 2 a usage error or a lock refusal (``lock.LockError``: a
missing or changed lock, or a test that already ran; every mismatch is printed on stdout before
the error line). Output files are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from research.pine_gate.cli_common import CliError, one_line

from . import stages, step0
from .data import verify_copy
from .lock import LockError

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


def _print_outputs(result: Path) -> None:
    """The files a stage wrote, its run marker if it claimed one, then its status line."""
    doc = json.loads(result.read_text(encoding="utf-8"))
    for name in doc["files"]:
        print(result.parent / name)
    if "marker" in doc:
        print(doc["marker"])
    print(doc["status_line"])


def cmd_train(args: argparse.Namespace) -> int:
    _print_outputs(stages.train_stage(args.config, args.data_dir, args.out_dir, args.mode,
                                      args.pair, now()))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    _print_outputs(stages.test_stage(args.config, args.data_dir, args.out_dir, args.mode,
                                     args.pair, now()))
    return 0


def _add_stage_parser(sub: Any, name: str, handler: Callable[[argparse.Namespace], int],
                      help_text: str) -> None:
    """``name`` with the five options both stages take; ``sub`` is the subparsers action."""
    p = sub.add_parser(name, help=help_text)
    p.add_argument("--mode", required=True, help="the mode to run (Phase 1: A)")
    p.add_argument("--pair", required=True, help="the pair to run (Phase 1: BTC-USDT)")
    p.add_argument("--config", required=True, type=_path, help="config/modes.yaml")
    p.add_argument("--data-dir", required=True, type=_path,
                   help="the copied production data (the folder holding prod/); the run markers "
                        "go in its holdout_runs/")
    p.add_argument("--out-dir", required=True, type=_path,
                   help="where the results, the trade logs, the lock and the test's start record "
                        "go; once that record exists neither stage runs again, so use one folder "
                        "for train and test")
    p.set_defaults(handler=handler)


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

    _add_stage_parser(sub, "train", cmd_train,
                      "run the training stage and write its results and the lock")
    _add_stage_parser(sub, "test", cmd_test,
                      "run the one-shot test against the lock and write the verdict")
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
    except LockError as exc:
        for problem in exc.problems:
            print(problem)
        _fail(exc)
        return 2
    except _EXPECTED_ERRORS as exc:
        _fail(exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
