"""Step 0: the Pine Phase 1 holdout replayed, checked against its file and split by side and
condition (spec section 8).

Research code; the bot never imports it. Step 0 reads only trades that already exist: it tests no
rule and changes no decision.

The Pine holdout ran once, on 2026-10-16, and its run marker forbids running its test stage again,
so step 0 never calls that stage and never touches a run marker. It rebuilds the stage's inputs
with public names of ``research.pine_gate``, assembled as the stage assembled them: the whole
dataset (``load_dataset(data_dir, None)``), the spec's ``ExecConfig``, ``GuardConfig`` and
``GateConfig``, ``HoldoutPeriod.spec()``, the calibration file, seed 0 and ``purge_tick_minutes``.
Then it replays the main run of every contender of ``build_contenders`` (c1, c2, c3).

Two checks come before anything is written, and nothing is adjusted to pass them:

* ``check_inputs``: the selection file, the configs, the data, the seed, the period and the
  calibration are the ones the holdout file records;
* ``reproduction_mismatches``: for every contender and symbol, every value ``symbol_metrics``
  gives for the replay equals the holdout file's, compared as JSON values (so 5 and 5.0 differ,
  and floats are compared exactly: JSON keeps every bit of them).

A difference raises ``ReproductionError``. Only after both checks is each trade labelled with its
side and with ``pilih_mode`` at its signal time, read from ``HourlyFrame``s built on the same
production day files (``load_pair``, 2025-08-01 to 2026-10-13), and ``split_rows`` gives one row
per contender, symbol, side and condition. ``run_step0`` writes ``step0_<YYYYMMDD>.json`` and
``.md``, dated by the WIB day of its clock, through ``research.pine_gate.fileio``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from bot.strategy.modes.config import SelectorConfig, config_sha256, load_modes_config
from bot.strategy.modes.regime import NAIK, SIDEWAYS, TIDAK_ENTRY, TURUN
from research.pine_gate.bars import wib_day
from research.pine_gate.calibrate import Calibration
from research.pine_gate.cli_common import file_sha256, load_calibration_file, read_json
from research.pine_gate.dataset import SYMBOLS, Dataset, load_dataset
from research.pine_gate.fileio import exists_message, json_text, write_exclusive
from research.pine_gate.holdout_replay import (
    Holdout,
    HoldoutPeriod,
    build_contenders,
    holdout_markets,
    symbol_metrics,
    trades_of,
)
from research.pine_gate.lock import code_sha256, latencies_sha256, python_version
from research.pine_gate.pipeline import purge_tick_minutes
from research.pine_gate.simulate import Trade
from research.pine_gate.spec import (
    HOLDOUT_END,
    SEED,
    TRAIN_START,
    WIB,
    ExecConfig,
    GateConfig,
    GuardConfig,
    ms,
)
from research.pine_gate.stats import mean_gross_bps, win_rate

from .data import PairData, PathLike, load_pair
from .frames import HourlyFrame
from .report import step0_markdown

PINE_FILES: Mapping[str, str] = MappingProxyType({
    "selection": "pine_gate_selection_20261016.json",
    "calibration": "pine_gate_calibration_20261016.json",
    "holdout": "pine_gate_holdout_20261016.json",
})

# The spec defaults the Pine test stage ran with; the holdout file records them under ``configs``.
SPEC_CONFIGS: Mapping[str, Any] = MappingProxyType({
    "exec": ExecConfig(),
    "guard": GuardConfig(),
    "gate": GateConfig(),
})

# The day files the Pine holdout load read: from the training start to the last holdout day, so
# the selector reads the same production bars as the replayed trades.
LABEL_FIRST_DAY = wib_day(ms(TRAIN_START))  # 2025-08-01
LABEL_LAST_DAY = wib_day(ms(HOLDOUT_END) - 1)  # 2026-10-13

SIDES: Mapping[int, str] = MappingProxyType({1: "LONG", -1: "SHORT"})
CONDITIONS: Tuple[str, ...] = (NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY)  # the table's order
STEP0_PREFIX = "step0_"

_SIDE_ORDER = tuple(SIDES.values())
_PER_MILLION = 1_000_000.0
_LEGS_PER_TRADE = 2  # entry and exit, each the full notional
_MISSING = "(missing)"

KondisiAt = Callable[[str, int], str]
Metrics = Dict[str, Dict[str, Dict[str, Any]]]


class ReproductionError(RuntimeError):
    """The replay is not the Pine holdout run: an input or a ``symbol_metrics`` value differs.

    ``problems`` holds every difference found, one line each."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems: Tuple[str, ...] = tuple(problems)


def pine_paths(pine_dir: PathLike) -> Dict[str, Path]:
    """The three Pine Phase 1 files under ``pine_dir``, keyed as ``PINE_FILES``.

    A missing file raises ``FileNotFoundError`` naming every one that is missing."""
    root = Path(pine_dir)
    paths = {kind: root / name for kind, name in PINE_FILES.items()}
    missing = [path.name for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"{root}: missing Pine Phase 1 files: {', '.join(missing)}")
    return paths


def _json_form(value: Any) -> str:
    """``value`` as JSON text, keys sorted: what a JSON file holds for it.

    Floats are written with every bit (shortest round-trip repr), an int never looks like a
    float, and NaN stays ``NaN``, which no stored number equals."""
    return json.dumps(value, sort_keys=True)


def _json_object(path: Path) -> Dict[str, Any]:
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return doc


# --------------------------------------------------------------------------- inputs


def _calibration_problems(paths: Mapping[str, Path], holdout_doc: Mapping[str, Any]) -> List[str]:
    """The calibration file against the holdout file's record and the selection's latency hash.

    The holdout file records s*, the stop count and the latency count; the selection file binds
    the latency sample itself (``latencies_sha256``), as the test stage checked."""
    calib = load_calibration_file(paths["calibration"])
    mine = {"s_star_bps": calib.s_star_bps, "n_stops": calib.n_stops,
            "n_latencies": len(calib.latencies_ms)}
    recorded = holdout_doc.get("calibration")
    problems = []
    if _json_form(recorded) != _json_form(mine):
        problems.append(f"the calibration file gives {_json_form(mine)}, the holdout file "
                        f"records calibration {_json_form(recorded)}")
    selection = read_json(paths["selection"])
    bound = selection.get("latencies_sha256") if isinstance(selection, dict) else None
    if bound != latencies_sha256(calib.latencies_ms):
        problems.append("the calibration file's latency sample is not the one the selection "
                        f"file binds (latencies_sha256 {bound!r})")
    return problems


def check_inputs(pine_dir: PathLike, holdout_doc: Mapping[str, Any], dataset: Dataset) -> None:
    """Refuse a replay whose inputs are not those the holdout file records.

    Checked: the selection file's sha256 against ``selection_sha256``; ``configs.exec``,
    ``configs.guard`` and ``configs.gate`` against ``asdict`` of the spec defaults;
    ``dataset.data_sha256`` against ``data_sha256``; the seed (0); the period
    (``HoldoutPeriod.spec()``); and the calibration file, whose s*, stop count and latency count
    must be the holdout file's ``calibration`` record and whose latency sample must be the one
    the selection file binds. Every difference is listed in one ``ReproductionError``."""
    paths = pine_paths(pine_dir)
    problems: List[str] = []
    selection_sha = file_sha256(paths["selection"])
    if selection_sha != holdout_doc.get("selection_sha256"):
        problems.append(f"the selection file's sha256 {selection_sha} is not the holdout file's "
                        f"selection_sha256 {holdout_doc.get('selection_sha256')!r}")
    configs = holdout_doc.get("configs")
    recorded_configs = configs if isinstance(configs, Mapping) else {}
    for name, cfg in SPEC_CONFIGS.items():
        if _json_form(recorded_configs.get(name)) != _json_form(asdict(cfg)):
            problems.append(f"configs.{name} of the holdout file is not "
                            f"asdict({type(cfg).__name__}())")
    if dataset.data_sha256 != holdout_doc.get("data_sha256"):
        problems.append(f"the loaded data's data_sha256 {dataset.data_sha256} is not the holdout "
                        f"file's {holdout_doc.get('data_sha256')!r}")
    if _json_form(holdout_doc.get("seed")) != _json_form(SEED):
        problems.append(f"the holdout file's seed {holdout_doc.get('seed')!r} is not {SEED}")
    if _json_form(holdout_doc.get("period")) != _json_form(HoldoutPeriod.spec().to_dict()):
        problems.append("the holdout file's period is not HoldoutPeriod.spec()")
    problems += _calibration_problems(paths, holdout_doc)
    if problems:
        raise ReproductionError(
            "the inputs are not those of the Pine holdout run: " + "; ".join(problems), problems)


# --------------------------------------------------------------------------- the replay


def holdout_context(dataset: Dataset, calib: Calibration) -> Holdout:
    """The ``Holdout`` of a spec run, assembled as the Pine test stage assembled it.

    The spec period and configs (``SPEC_CONFIGS``), seed 0, fresh markets in tick order (BTC
    first) and the stage's one purge, ``purge_tick_minutes`` of the calibration."""
    exec_cfg = SPEC_CONFIGS["exec"]
    period = HoldoutPeriod.spec()
    return Holdout(
        frames=dataset.frames,
        markets=holdout_markets(dataset, exec_cfg),
        period=period,
        calib=calib,
        seed=SEED,
        exec_cfg=exec_cfg,
        guard_cfg=SPEC_CONFIGS["guard"],
        gate_cfg=SPEC_CONFIGS["gate"],
        days=tuple(period.days()),
        last20_days=tuple(period.last20_days()),
        purge_tick_minutes=purge_tick_minutes(calib, exec_cfg),
    )


def replay_main_runs(
    data_dir: PathLike, pine_dir: PathLike
) -> Tuple[Dict[str, List[Trade]], Holdout]:
    """Every contender's main run on the holdout, as the Pine test stage ran it.

    Loads the whole dataset as the stage did (``load_dataset(data_dir, None)``), refuses inputs
    that differ from the holdout file's (``check_inputs``) before any run, then runs
    ``h.run(c.signal)`` for every contender of ``build_contenders``, in its order. Returns the
    trades per contender name and the ``Holdout``."""
    paths = pine_paths(pine_dir)
    holdout_doc = _json_object(paths["holdout"])
    selection = _json_object(paths["selection"])
    calib = load_calibration_file(paths["calibration"])
    dataset = load_dataset(data_dir, None)
    check_inputs(pine_dir, holdout_doc, dataset)
    h = holdout_context(dataset, calib)
    contenders, _skipped = build_contenders(selection, h.frames, h.period)
    return {c.name: h.run(c.signal) for c in contenders}, h


def replayed_metrics(trades_by_contender: Mapping[str, Sequence[Trade]], h: Holdout) -> Metrics:
    """``symbol_metrics`` of each contender's trades for every symbol, as the stage computed it."""
    return {
        name: {symbol: symbol_metrics(trades_of(trades, symbol), h) for symbol in SYMBOLS}
        for name, trades in trades_by_contender.items()
    }


# --------------------------------------------------------------------------- reproduction


def _union(first: Mapping[str, Any], second: Mapping[str, Any]) -> List[str]:
    """The keys of ``first`` in order, then those only ``second`` has."""
    return list(first) + [key for key in second if key not in first]


def _one_side(label: str, replayed: Mapping[str, Any], stored: Mapping[str, Any],
              key: str) -> Optional[str]:
    """The line for a contender or symbol present on one side only, else ``None``."""
    if key not in stored:
        return f"{label}: replayed, but not in the holdout file"
    if key not in replayed:
        return f"{label}: in the holdout file, but not replayed"
    return None


def _field_problems(
    label: str, replayed: Mapping[str, Any], stored: Mapping[str, Any]
) -> List[str]:
    """One line per ``symbol_metrics`` key whose stored value differs or is absent."""
    problems = []
    for key, value in replayed.items():
        mine = _json_form(value)
        theirs = _json_form(stored[key]) if key in stored else _MISSING
        if mine != theirs:
            problems.append(f"{label} {key}: {mine} != {theirs}")
    return problems


def reproduction_mismatches(
    replayed: Mapping[str, Mapping[str, Mapping[str, Any]]], holdout_doc: Mapping[str, Any]
) -> List[str]:
    """Every difference between the replay's metrics and ``contenders.<c>.symbols.<s>``.

    ``replayed`` is ``replayed_metrics``. Every key ``symbol_metrics`` returns is compared, as
    JSON values; the holdout file's other keys (robustness, verdict, ...) are not. A contender or
    symbol on one side only is a difference, and so is comparing nothing at all. ``[]`` means
    every value of every contender and symbol is the stored one."""
    stored = holdout_doc.get("contenders") if isinstance(holdout_doc, Mapping) else None
    if not isinstance(stored, Mapping):
        return ["the holdout file has no contenders block"]
    problems: List[str] = []
    compared = 0
    for name in _union(replayed, stored):
        one_side = _one_side(name, replayed, stored, name)
        if one_side is not None:
            problems.append(one_side)
            continue
        block = stored[name]
        symbols = block.get("symbols") if isinstance(block, Mapping) else None
        if not isinstance(symbols, Mapping):
            problems.append(f"{name}: the holdout file has no symbols block")
            continue
        for symbol in _union(replayed[name], symbols):
            label = f"{name} {symbol}"
            one_side = _one_side(label, replayed[name], symbols, symbol)
            if one_side is not None:
                problems.append(one_side)
            elif not isinstance(symbols[symbol], Mapping):
                problems.append(f"{label}: the holdout file holds no metrics")
            else:
                compared += 1
                problems += _field_problems(label, replayed[name][symbol], symbols[symbol])
    if not compared and not problems:
        problems.append("nothing was compared: the replay and the holdout file share no "
                        "contender and symbol")
    return problems


# --------------------------------------------------------------------------- the split


@dataclass(frozen=True)
class Step0Row:
    """One line of the step-0 table: the trades of one contender, symbol, side and condition.

    ``sisi`` is LONG or SHORT and ``kondisi`` the selector's condition at the signal time.
    ``menang_pct`` is 100 × TP / (TP + SL), the 16 October report's Menang, and ``None`` when no
    trade of the group ended on TP or SL. ``gross_bps`` is the mean gross return of the trades and
    ``hasil_per_1_juta`` the net USDT per 1,000,000 USDT of volume, a trade counting two legs of
    the notional."""

    contender: str
    symbol: str
    sisi: str
    kondisi: str
    posisi: int
    menang_pct: Optional[float]
    gross_bps: float
    hasil_per_1_juta: float


def _group_key(trade: Trade, kondisi: str) -> Tuple[str, str, str]:
    where = f"{trade.symbol} trade signalled at {trade.signal_ms}"
    if trade.symbol not in SYMBOLS:
        raise ValueError(f"{where}: the symbol is not one of {', '.join(SYMBOLS)}")
    if trade.side not in SIDES:
        raise ValueError(f"{where}: side {trade.side!r} is neither 1 nor -1")
    if kondisi not in CONDITIONS:
        raise ValueError(f"{where}: condition {kondisi!r} is not one of {', '.join(CONDITIONS)}")
    return trade.symbol, SIDES[trade.side], kondisi


def _table_order(key: Tuple[str, str, str]) -> Tuple[int, int, int]:
    symbol, sisi, kondisi = key
    return SYMBOLS.index(symbol), _SIDE_ORDER.index(sisi), CONDITIONS.index(kondisi)


def _row(contender: str, key: Tuple[str, str, str], trades: Sequence[Trade],
         notional_usdt: float) -> Step0Row:
    symbol, sisi, kondisi = key
    n = len(trades)
    volume = n * _LEGS_PER_TRADE * notional_usdt
    return Step0Row(
        contender=contender,
        symbol=symbol,
        sisi=sisi,
        kondisi=kondisi,
        posisi=n,
        menang_pct=win_rate(trades),
        gross_bps=float(mean_gross_bps(trades)),
        hasil_per_1_juta=_PER_MILLION * sum(t.net_usdt for t in trades) / volume,
    )


def split_rows(
    trades_by_contender: Mapping[str, Sequence[Trade]],
    kondisi_at: KondisiAt,
    *,
    notional_usdt: float = SPEC_CONFIGS["exec"].notional_usdt,
) -> List[Step0Row]:
    """The trades grouped by contender, symbol, side and the condition at their signal time.

    ``kondisi_at(symbol, signal_ms)`` is asked once per trade. Rows come per contender in the
    mapping's order, then by symbol (BTC first), side (LONG first) and condition (NAIK, TURUN,
    SIDEWAYS, TIDAK_ENTRY); a group without trades has no row. ``notional_usdt`` is the
    execution's notional (default the spec's 25,000). A symbol, side or condition outside those
    lists raises ``ValueError``, and so does a notional that is not positive and finite."""
    if not (math.isfinite(notional_usdt) and notional_usdt > 0):
        raise ValueError(f"notional_usdt must be positive and finite, got {notional_usdt!r}")
    rows: List[Step0Row] = []
    for contender, trades in trades_by_contender.items():
        groups: Dict[Tuple[str, str, str], List[Trade]] = {}
        for trade in trades:
            key = _group_key(trade, kondisi_at(trade.symbol, trade.signal_ms))
            groups.setdefault(key, []).append(trade)
        rows += [_row(contender, key, groups[key], notional_usdt)
                 for key in sorted(groups, key=_table_order)]
    return rows


def condition_lookup(pairs: Sequence[PairData], selector: SelectorConfig) -> KondisiAt:
    """``kondisi_at(symbol, t_ms)``: the condition ``HourlyFrame.context_at(t_ms)`` gives on the
    pair of ``symbol``.

    A time outside the pair's 1m bars (``[first open, last open + 1 minute)``) raises
    ``ValueError``: past the data the last closed candle would be stale and the label wrong
    without a word. So do a symbol without a pair, a pair without bars and a pair given twice."""
    frames: Dict[str, HourlyFrame] = {}
    spans: Dict[str, Tuple[int, int]] = {}
    for pair in pairs:
        bars = pair.bars_1m
        if pair.symbol in frames:
            raise ValueError(f"{pair.symbol} is given twice")
        if not len(bars):
            raise ValueError(f"{pair.symbol} has no 1m bars")
        frames[pair.symbol] = HourlyFrame.build(pair, selector)
        spans[pair.symbol] = (bars.open_ms[0], bars.open_ms[-1] + bars.interval_ms)

    def kondisi_at(symbol: str, t_ms: int) -> str:
        if symbol not in frames:
            raise ValueError(f"no hourly frame for {symbol}")
        start_ms, end_ms = spans[symbol]
        if not start_ms <= t_ms < end_ms:
            raise ValueError(f"{symbol}: time {t_ms} is outside the loaded 1m bars "
                             f"[{start_ms}, {end_ms})")
        return frames[symbol].context_at(t_ms).kondisi

    return kondisi_at


def label_lookup(data_dir: PathLike, selector: SelectorConfig) -> KondisiAt:
    """``condition_lookup`` on both pairs over ``LABEL_FIRST_DAY`` to ``LABEL_LAST_DAY``.

    ``load_pair`` refuses a missing day file and a missing or unusable warm-up."""
    pairs = [load_pair(data_dir, symbol, LABEL_FIRST_DAY, LABEL_LAST_DAY) for symbol in SYMBOLS]
    return condition_lookup(pairs, selector)


# --------------------------------------------------------------------------- run


def _output_paths(out_dir: PathLike, now: datetime) -> Tuple[Path, Path]:
    """``step0_<YYYYMMDD>.json`` and ``.md`` (the WIB day of ``now``); either existing refuses."""
    stamp = f"{now.astimezone(WIB):%Y%m%d}"
    paths = (Path(out_dir) / f"{STEP0_PREFIX}{stamp}.json",
             Path(out_dir) / f"{STEP0_PREFIX}{stamp}.md")
    for path in paths:
        if path.exists():
            raise FileExistsError(exists_message(path, "step 0"))
    return paths


def _write_both(json_path: Path, json_payload: str, md_path: Path, markdown: str) -> None:
    """Both files or neither: a failed second write removes the first."""
    write_exclusive(json_path, json_payload, "step 0")
    try:
        write_exclusive(md_path, markdown, "step 0 report")
    except BaseException:
        json_path.unlink(missing_ok=True)
        raise


def _fields(replayed: Metrics) -> List[str]:
    return sorted({key for by_symbol in replayed.values() for row in by_symbol.values()
                   for key in row})


def run_step0(
    data_dir: PathLike, pine_dir: PathLike, config_path: PathLike, out_dir: PathLike,
    now: datetime,
) -> Path:
    """Step 0 from start to finish; returns the path of the JSON file.

    In order: the output files must not exist yet (``FileExistsError``), the three Pine files must
    exist (``FileNotFoundError``) and the config must load; then the replay
    (``replay_main_runs``, which checks the inputs) and the reproduction check. A difference
    raises ``ReproductionError`` and nothing is written. Then the labels (``label_lookup``) and
    the split (``split_rows``, with the replay's notional), and both files are written through
    ``research.pine_gate.fileio``, never over an existing file. ``now`` must be
    timezone-aware; the files are dated by its WIB day."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    json_path, md_path = _output_paths(out_dir, now)
    paths = pine_paths(pine_dir)
    cfg = load_modes_config(Path(config_path))
    config_sha = config_sha256(Path(config_path))
    holdout_doc = _json_object(paths["holdout"])

    trades, h = replay_main_runs(Path(data_dir), Path(pine_dir))
    replayed = replayed_metrics(trades, h)
    exec_cfg, period = h.exec_cfg, h.period
    del h  # the dataset's frames and markets are not needed for the labels
    mismatches = reproduction_mismatches(replayed, holdout_doc)
    if mismatches:
        raise ReproductionError(
            f"the replay does not reproduce {PINE_FILES['holdout']}: {len(mismatches)} "
            "differences: " + "; ".join(mismatches), mismatches)

    rows = split_rows(trades, label_lookup(Path(data_dir), cfg.pemilih_mode),
                      notional_usdt=exec_cfg.notional_usdt)
    doc = {
        "created_at": now.isoformat(),
        "seed": SEED,
        "inputs": {
            "holdout_file": PINE_FILES["holdout"],
            "holdout_sha256": file_sha256(paths["holdout"]),
            "selection_sha256": file_sha256(paths["selection"]),
            "calibration_sha256": file_sha256(paths["calibration"]),
            # check_inputs refused any other: this is the sha256 of the data that was loaded
            "data_sha256": holdout_doc.get("data_sha256"),
            "config_sha256": config_sha,
            "code_sha256": code_sha256(),
            "holdout_code_sha256": holdout_doc.get("code_sha256"),
            "python_version": python_version(),
        },
        "period": period.to_dict(),
        "label_days": [LABEL_FIRST_DAY.isoformat(), LABEL_LAST_DAY.isoformat()],
        "exec": asdict(exec_cfg),
        "reproduction": {"contenders": list(replayed), "symbols": list(SYMBOLS),
                         "fields": _fields(replayed), "differences": 0},
        "rows": [asdict(row) for row in rows],
    }
    markdown = step0_markdown(rows, holdout_file=PINE_FILES["holdout"],
                              first_day=wib_day(period.start_ms),
                              last_day=wib_day(period.end_ms - 1), seed=SEED, exec_cfg=exec_cfg)
    _write_both(json_path, json_text(doc), md_path, markdown)
    return json_path
