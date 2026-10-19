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
* ``--mode L`` (the near-mean LONG with the live bot's exits, BTC-USDT only) runs
  ``stages_live.train_live_stage`` and ``test_live_stage`` instead, and needs ``--live-config``
  (``config/modes_live_exit.yaml``); the lock binds that file's sha256. After ``train``, an edit
  that still loads is refused by the lock (``test`` exits 2) and an edit that fails validation
  exits 1; neither claims the one shot. ``--live-config`` with ``--mode A`` or ``--mode L``
  without it is a usage error (exit 2); any other mode exits 1.
* ``compare-forward --pair --config --live-config --data-dir --forward-trades --from --to
  --out-dir`` runs ``forward_compare.compare_forward_stage``: mode L (i) and (ii) backtested over
  the LONG forward test's week ``[--from, --to)`` WIB beside the forward bot's summary of the same
  window, from the CSV ``report_forward_test.py`` exported. No lock, start record or marker is read
  or written. It refuses (exit 2, as a lock refusal) a pair other than BTC-USDT, an empty or
  reversed window, a ``--from`` whose day before falls before ``uji.ujian_selesai`` (mode L's test
  period stays unread) and a ``--data-dir`` holding ``holdout_runs/`` (use a separate forward data
  folder). It prints the JSON and the Indonesian report it wrote, then its status line.
* ``rescore --trade-log PATH ... --config --live-config --out-dir`` runs ``rescore.rescore_stage``:
  Mode A's Phase 1 trade logs repriced with the costs of ``config/modes_live_exit.yaml``, as a
  cost sensitivity and never a pass or fail. It reads only the logs given, no bar file, and prints
  the JSON and the Indonesian report it wrote.

Exit codes: 0 success, 1 an expected failure (one ``error:`` line on stderr and no traceback,
unless ``MODE_BACKTEST_DEBUG=1``), 2 a usage error (argparse's, or a ``--live-config`` that does
not fit the mode) or a lock refusal (``lock.LockError``: a
missing or changed lock, or a test that already ran; every mismatch is printed on stdout before
the error line), or a refusal of ``compare-forward`` (``forward_compare.CompareRefusal``). Output
files are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from research.pine_gate.cli_common import CliError, one_line

from . import forward_compare, rescore, stages, stages_live, step0
from .data import verify_copy
from .lock import LockError

DEBUG_ENV = "MODE_BACKTEST_DEBUG"
LIVE_MODE = "L"
_EXPECTED_ERRORS = (CliError, step0.ReproductionError, OSError, ValueError)


def now() -> datetime:
    """The clock that dates the output files; tests replace it."""
    return datetime.now(timezone.utc)


def _path(text: str) -> Path:
    return Path(text).expanduser()


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from exc


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


class UsageError(Exception):
    """The options do not fit together; ``main`` prints one error line and exits 2."""


def _check_live_config(args: argparse.Namespace) -> bool:
    """True when the run is mode L. ``--live-config`` goes with mode L and with nothing else."""
    is_live = args.mode == LIVE_MODE
    if is_live and args.live_config is None:
        raise UsageError(f"--mode {LIVE_MODE} needs --live-config")
    if not is_live and args.live_config is not None and args.mode == stages.PHASE1_MODE:
        raise UsageError(f"--live-config is for --mode {LIVE_MODE} only, not --mode {args.mode}")
    return is_live


def cmd_train(args: argparse.Namespace) -> int:
    if _check_live_config(args):
        result = stages_live.train_live_stage(args.config, args.live_config, args.data_dir,
                                              args.out_dir, args.pair, now())
    else:
        result = stages.train_stage(args.config, args.data_dir, args.out_dir, args.mode,
                                    args.pair, now())
    _print_outputs(result)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    if _check_live_config(args):
        result = stages_live.test_live_stage(args.config, args.live_config, args.data_dir,
                                             args.out_dir, args.pair, now())
    else:
        result = stages.test_stage(args.config, args.data_dir, args.out_dir, args.mode,
                                   args.pair, now())
    _print_outputs(result)
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    json_path = rescore.rescore_stage(args.trade_log, args.config, args.live_config, args.out_dir,
                                      now())
    print(json_path)
    print(json_path.with_suffix(".md"))
    return 0


def cmd_compare_forward(args: argparse.Namespace) -> int:
    result = forward_compare.compare_forward_stage(
        args.config, args.live_config, args.data_dir, args.forward_trades, args.pair,
        args.first_day, args.end_day, args.out_dir, now())
    _print_outputs(result)
    return 0


def _add_stage_parser(sub: Any, name: str, handler: Callable[[argparse.Namespace], int],
                      help_text: str) -> None:
    """``name`` with the six options both stages take; ``sub`` is the subparsers action."""
    p = sub.add_parser(name, help=help_text)
    p.add_argument("--mode", required=True, help="the mode to run (A, or L with --live-config)")
    p.add_argument("--pair", required=True, help="the pair to run (BTC-USDT)")
    p.add_argument("--config", required=True, type=_path, help="config/modes.yaml")
    p.add_argument("--live-config", type=_path, default=None,
                   help="config/modes_live_exit.yaml; required with --mode L, refused with "
                        "--mode A")
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

    p = sub.add_parser("compare-forward",
                       help="backtest mode L over the LONG forward test's week, beside its result")
    p.add_argument("--pair", required=True, help="the pair to run (BTC-USDT)")
    p.add_argument("--config", required=True, type=_path, help="config/modes.yaml")
    p.add_argument("--live-config", required=True, type=_path,
                   help="config/modes_live_exit.yaml")
    p.add_argument("--data-dir", required=True, type=_path,
                   help="a forward data folder holding prod/ (never the Phase 1 one, which holds "
                        "holdout_runs/)")
    p.add_argument("--forward-trades", required=True, type=_path,
                   help="the CSV report_forward_test.py exported for the same window")
    p.add_argument("--from", dest="first_day", required=True, type=_day,
                   help="first day, WIB (YYYY-MM-DD)")
    p.add_argument("--to", dest="end_day", required=True, type=_day,
                   help="end day, WIB, exclusive (YYYY-MM-DD)")
    p.add_argument("--out-dir", required=True, type=_path)
    p.set_defaults(handler=cmd_compare_forward)

    p = sub.add_parser("rescore", help="reprice Phase 1 trade logs with the live-exit costs "
                                       "(a cost sensitivity, not a test)")
    p.add_argument("--trade-log", required=True, action="append", type=_path,
                   help="a Phase 1 trade log CSV; repeat for several")
    p.add_argument("--config", required=True, type=_path, help="config/modes.yaml")
    p.add_argument("--live-config", required=True, type=_path,
                   help="config/modes_live_exit.yaml (its biaya block gives the new costs)")
    p.add_argument("--out-dir", required=True, type=_path)
    p.set_defaults(handler=cmd_rescore)
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
    except (UsageError, forward_compare.CompareRefusal) as exc:
        _fail(exc)
        return 2
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
