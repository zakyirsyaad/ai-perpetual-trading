"""Stage subcommands of the Pine gate CLI: calibrate, train, test and report.

Research code; the bot never imports it. Standard library only. ``test``
enforces the holdout lock before any holdout data is read: the selection file
must pass ``load_selection_checked`` under the current spec sha256, the
calibration must be the one the selection was made with, and no holdout file
may exist yet. Only then is the full dataset loaded.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import cli_common as common
from .cli_checks import DECISIONS_PREFIX, VST_PREFIX
from .calibrate import load_entry_latencies_ms, s_star, stop_overshoot_detail
from .holdout import HOLDOUT_PREFIX, _check_calibration, _refuse_any_holdout, test_stage
from .pipeline import (
    SELECTION_PREFIX,
    TRAIN_DATA_UNTIL_MS,
    latencies_sha256,
    load_dataset,
    load_selection_checked,
    train_stage,
)
from .report import write_report
from .spec import spec_sha256

CALIBRATION_PREFIX = "pine_gate_calibration_"
_SRC_ROOT = Path(__file__).resolve().parents[2]
_CODE_EXTRA = ("bot/strategy/pine_indicators.py",)


# --------------------------------------------------------------------------- calibrate


async def _read_calibration(get_rows: Any, settings: Any) -> Tuple[Any, List[float]]:
    detail = await stop_overshoot_detail(
        get_rows, settings.supabase_trades_table, settings.supabase_ai_decisions_table
    )
    latencies = await load_entry_latencies_ms(get_rows, settings.supabase_ai_call_metrics_table)
    return detail, latencies


def cmd_calibrate(args: argparse.Namespace) -> int:
    path = common.stamped_path(args.out_dir, CALIBRATION_PREFIX)
    if path.exists():
        raise FileExistsError(f"{path}: already exists; remove it deliberately to write again")
    settings = common.call_live("loading the bot settings", common.make_settings)
    get_rows = common.call_live("opening the Supabase reader",
                                lambda: common.make_get_rows(settings))
    detail, latencies = common.call_live(
        "reading Supabase", lambda: asyncio.run(_read_calibration(get_rows, settings))
    )
    doc = {
        "s_star_bps": s_star(detail.samples),
        "n_stops": len(detail.samples),
        "n_skipped": detail.skipped,
        "latencies_ms": [float(x) for x in latencies],
        "latencies_sha256": latencies_sha256(latencies),
        "created_at": common.now().isoformat(),
    }
    common.write_json_exclusive(path, doc)
    print(f"s* {doc['s_star_bps']} bps, n_stops {doc['n_stops']}, "
          f"n_skipped {doc['n_skipped']}, n_latencies {len(latencies)}")
    print(path)
    return 0


# --------------------------------------------------------------------------- train / test


def cmd_train(args: argparse.Namespace) -> int:
    calib = common.load_calibration_file(Path(args.calibration).expanduser())
    dataset = load_dataset(args.data_dir, TRAIN_DATA_UNTIL_MS)
    print(train_stage(dataset, calib, args.out_dir, spec_sha256()))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    selection_path = Path(args.selection).expanduser()
    spec_sha = spec_sha256()
    selection = load_selection_checked(selection_path, spec_sha)  # the lock comes first
    calib = common.load_calibration_file(Path(args.calibration).expanduser())
    _check_calibration(selection_path, selection, calib)
    _refuse_any_holdout(args.out_dir)
    dataset = load_dataset(args.data_dir, None)
    print(test_stage(dataset, selection_path, calib, args.out_dir, spec_sha,
                     seed=selection.get("seed", 0)))
    return 0


# --------------------------------------------------------------------------- report


def code_files() -> List[Tuple[str, str]]:
    """(path relative to ``src``, sha256) of every research .py plus pine_indicators.py."""
    paths = sorted((_SRC_ROOT / "research").rglob("*.py"))
    paths += [_SRC_ROOT / rel for rel in _CODE_EXTRA]
    files = [(p.relative_to(_SRC_ROOT).as_posix(), common.file_sha256(p)) for p in paths]
    return sorted(files)


def code_sha256() -> str:
    text = "\n".join(f"{rel}:{sha}" for rel, sha in code_files())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vst_symbol(path: Path) -> str:
    """``pine_gate_vst_<SYMBOL>_<YYYYMMDD>.json`` -> ``<SYMBOL>``."""
    stem = path.name[len(VST_PREFIX):-len(".json")]
    return stem.rsplit("_", 1)[0]


def _vst_paths(out_dir: Path, explicit: Optional[List[str]]) -> Dict[str, Path]:
    if explicit:
        paths = [Path(p).expanduser() for p in explicit]
    else:
        paths = sorted(Path(out_dir).glob(f"{VST_PREFIX}*_*.json"))  # newest last per symbol
    return {_vst_symbol(p): p for p in paths}


def _pick(explicit: Optional[str], out_dir: Path, prefix: str) -> Optional[Path]:
    if explicit:
        return Path(explicit).expanduser()
    return common.newest(out_dir, f"{prefix}*.json")


def gather_results(args: argparse.Namespace) -> Dict[str, Any]:
    sel_path = _pick(args.selection, args.out_dir, SELECTION_PREFIX)
    hold_path = _pick(args.holdout, args.out_dir, HOLDOUT_PREFIX)
    dec_path = _pick(args.decisions, args.out_dir, DECISIONS_PREFIX)
    selection = common.json_doc(sel_path)
    decisions = common.json_doc(dec_path)
    vst = {symbol: common.json_doc(p) for symbol, p in _vst_paths(args.out_dir, args.vst).items()}
    return {
        "selection": selection,
        "holdout": common.json_doc(hold_path),
        "vst": vst,
        "decisions": common.as_mapping(decisions).get("buckets"),
        "sha256": {
            "spec": spec_sha256(),
            "data": common.as_mapping(selection).get("data_sha256"),
            "selection": common.file_sha256(sel_path) if sel_path else None,
            "holdout": common.file_sha256(hold_path) if hold_path else None,
            "code": code_sha256(),
        },
    }


def cmd_report(args: argparse.Namespace) -> int:
    results = gather_results(args)
    try:
        md_path, json_path = write_report(results, args.out_dir)
    except NotImplementedError as exc:
        raise common.CliError(f"cannot write the report yet: {exc}") from None
    print(md_path)
    print(json_path)
    return 0


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("calibrate", help="s* and entry latencies from Supabase (read-only)")
    p.set_defaults(handler=cmd_calibrate)

    p = sub.add_parser("train", help="training stage; writes the selection file")
    p.add_argument("--calibration", required=True, help="pine_gate_calibration_<date>.json")
    p.set_defaults(handler=cmd_train)

    p = sub.add_parser("test", help="the one-shot holdout test (needs the selection file)")
    p.add_argument("--selection", required=True)
    p.add_argument("--calibration", required=True)
    p.set_defaults(handler=cmd_test)

    p = sub.add_parser("report", help="the Indonesian report from the newest output files")
    p.add_argument("--selection")
    p.add_argument("--holdout")
    p.add_argument("--decisions")
    p.add_argument("--vst", action="append", help="repeatable; default: newest per symbol")
    p.set_defaults(handler=cmd_report)
