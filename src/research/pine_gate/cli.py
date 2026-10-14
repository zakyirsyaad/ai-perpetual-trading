"""Command line for the Pine gate research: ``python -m research.pine_gate.cli <command>``.

Research code; the bot never imports it. Standard library only. Thin wiring:
the subcommands live in ``cli_data`` (fetch, warmup, funding, collect,
check-data), ``cli_stages`` (calibrate, train, test, report) and ``cli_checks``
(vst-check, decisions); this module parses arguments, writes the spec and maps
errors to exit codes.

Exit codes: 0 success, 1 an expected failure (one ``error:`` line on stderr),
2 a usage error or a holdout-lock refusal. Expected errors never print a
traceback, and nothing ever prints secrets or environment values. Output files
are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence

from . import cli_checks, cli_data, cli_stages
from . import cli_common as common
from .fetch import ExchangeError, IncompleteDayError
from .lock import HoldoutLockError
from .spec import spec_document, spec_sha256

DEFAULT_DATA_DIR = "~/replay/pine_gate/data"
DEFAULT_OUT_DIR = "~/replay/out"
_EXPECTED_ERRORS = (common.CliError, OSError, ValueError, NotImplementedError, ExchangeError,
                    IncompleteDayError)


def _iso_date(text: str) -> date:
    """``YYYY-MM-DD`` only, the same on Python 3.10 and 3.11."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def _symbol(text: str) -> str:
    try:
        return common.check_symbol(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _symbols(text: str) -> List[str]:
    return [_symbol(part.strip()) for part in text.split(",") if part.strip()]


def cmd_spec(args: argparse.Namespace) -> int:
    """Write the pre-registered spec as canonical JSON; its file sha256 is the spec sha256."""
    text = json.dumps(spec_document(), sort_keys=True, separators=(",", ":"))
    common.write_exclusive(Path(args.write).expanduser(), text, "spec")
    print(spec_sha256())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m research.pine_gate.cli",
                                     description="Pine-adapted gate research (Phase 1).")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("spec", help="write the pre-registered spec; prints its sha256")
    p.add_argument("--write", required=True, metavar="PATH")
    p.set_defaults(handler=cmd_spec)
    cli_data.add_parsers(sub, _iso_date, _symbols)
    cli_stages.add_parsers(sub)
    cli_checks.add_parsers(sub, _symbol)
    return parser


def _fail(prefix: str, exc: BaseException) -> None:
    print(f"{prefix}: {common.one_line(exc)}", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed the usage message
        return exc.code if isinstance(exc.code, int) else 2
    args.data_dir = Path(args.data_dir).expanduser()
    args.out_dir = Path(args.out_dir).expanduser()
    try:
        return args.handler(args)
    except HoldoutLockError as exc:
        _fail("holdout lock", exc)
        return 2
    except _EXPECTED_ERRORS as exc:
        _fail("error", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
