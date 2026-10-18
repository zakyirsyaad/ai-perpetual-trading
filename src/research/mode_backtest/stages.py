"""The train and test stages of the mode backtest: one pre-registered, one-shot experiment.

Research code; the bot never imports it. Phase 1 runs Mode A on BTC-USDT and nothing else (spec
section 9.2). Every number comes from the configuration file, read once: its bytes are hashed and
those bytes are parsed. The stages are the ``uji`` dates, WIB midnights: training is
``[latihan_mulai, latihan_selesai)`` and the test ``[latihan_selesai, ujian_selesai)``.

``train_stage`` reads the training days only, ``latihan_mulai`` to the day before
``latihan_selesai``, so it opens no day file of the test stage, and it refuses a warm-up too short
for every hourly series to exist at 00:00 WIB of the first day, which would shrink the sample
without a word. It runs Mode A on 15m candles and the comparator, the current SHORT rule (Mode D on
3m candles) in NAIK. When ``wants_5m`` says so it runs Mode A once more on 5m candles and locks
5m. It writes ``mode_A_BTC-USDT_train_<YYYYMMDD>.json``, dated by the WIB day of ``now``, one trade
log per run and, last, the lock (see ``lock``).

``test_stage`` works in this order: read the configuration and the lock; hash the training files
(both warm-ups among them), reading nothing else; refuse unless the lock matches them, the
configuration, the code and the Python version, unless the test has not started yet (no run
marker in this data folder and no start record next to the lock) and unless every day and warm-up
file it will read is a regular file; claim the start record and then the run marker; and only
then read the days from ``latihan_mulai`` to the day before ``ujian_selesai``, so every series is
warm when the test starts. Of the test days only names and file types are looked at before the
claims (a ``stat`` each): what is inside them is ``verify-copy``'s check, run right before the
test, and a day that fails to read after the claims ends the run with the test used up. After the
claims the test hashes what it is about to load, and its result and report record two manifest
sha256s, one of every file it loads and one of the test days alone, so a later run can show that
it read the same bytes. It runs Mode A on the locked timeframe and the comparator over the test
stage and writes ``mode_A_BTC-USDT_test_<YYYYMMDD>.json``, a trade log per run and a Markdown
report: the table of spec section 2.9 (the Latihan rows from the lock), the status line, LOLOS,
GAGAL or BELUM CUKUP SAMPEL, and the lock, the marker and the two manifests.

Every run of a stage trades only in NAIK and is measured on the stage's NAIK days (``naik_days``
with the weekly window and ``uji.hari_naik_minimal_jam_naik``), counted from the first hour in
which Mode A can signal (``_naik_days``): the first 40 hours of training have no 15m EMA yet, so
they hold no NAIK hour of the 15m run or of the comparator measured with it, and the 5m run counts
from its own EMA. ``stage_metrics`` is called in one place,
with ``uji.lolos_buang_posisi_teratas``. The status lines print the exact numbers the rules compare
(``repr`` of each float), because the table rounds them.

Refusals: ``StageError`` (exit code 1) for what Phase 1 cannot run as pre-registered, ``LockError``
(exit code 2) for the lock, the marker and the start record, ``FileExistsError`` (exit code 1)
before any work when an output file exists, and ``FileNotFoundError`` (exit code 1) before the
claims when a day file is missing or no regular file: outputs are never overwritten, and the test
runs once, whatever data folder a rerun names.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from bot.core.entry_window import parse_weekly_window
from bot.strategy.modes.config import ModesConfig, TestConfig, load_modes_config_with_sha256
from bot.strategy.modes.regime import NAIK
from research.pine_gate.features import INTERVAL_1H
from research.pine_gate.fileio import write_exclusive, write_json_exclusive
from research.pine_gate.spec import WIB, ms

from . import lock
from .data import day_files, load_pair, manifest, manifest_sha256, stage_files
from .frames import PairFrames, build_pair_frames
from .metrics import StageMetrics, pass_status, stage_metrics, wants_5m
from .report import ReportRow, exact as _exact, mode_table, status_figures
from .simulate import MODE_A, MODE_D, RunResult, RunSpec, simulate_run
from .tradelog import write_trade_log

__all__ = [
    "COMPARATOR_LABEL",
    "PHASE1_MODE",
    "PHASE1_PAIR",
    "StageError",
    "check_phase1",
    "test_stage",
    "train_stage",
]

PathLike = Union[str, Path]

PHASE1_MODE = MODE_A
PHASE1_PAIR = "BTC-USDT"
FIRST_TF = "15m"  # Mode A trains on 15m candles first
SWITCH_TF = "5m"  # the one pre-registered adjustment (spec section 2.8)
COMPARATOR_LABEL = "D di NAIK"  # the current SHORT rule run in NAIK, as the table names it
TRAIN, TEST = "train", "test"
LATIHAN, UJIAN = "Latihan", "Ujian"
NA = "–"

# The selector's inputs at the first training minute: none may still be None (D3).
_HOURLY_SERIES = ("sma20_1h", "sma50_1h", "atr14_1h", "atr_median", "close_4h", "ema50_4h")


class StageError(ValueError):
    """The stage cannot run as pre-registered; nothing has been written."""


@dataclass(frozen=True)
class _Period:
    """One stage, ``[start, end)``: two WIB midnights from the ``uji`` block."""

    start: datetime
    end: datetime

    @property
    def start_ms(self) -> int:
        return ms(self.start)

    @property
    def end_ms(self) -> int:
        return ms(self.end)

    @property
    def first_day(self) -> date:
        return self.start.date()

    @property
    def last_day(self) -> date:
        return (self.end - timedelta(days=1)).date()

    def record(self) -> Dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "start_ms": self.start_ms, "end_ms": self.end_ms}


def _training(uji: TestConfig) -> _Period:
    return _Period(uji.latihan_mulai, uji.latihan_selesai)


def _testing(uji: TestConfig) -> _Period:
    return _Period(uji.latihan_selesai, uji.ujian_selesai)


@dataclass(frozen=True)
class _Bindings:
    """What a lock binds, under the names the lock and the result files use."""

    config_sha256: str
    code_sha256: str
    train_manifest_sha256: str
    python: str


@dataclass(frozen=True)
class _Run:
    """One run of a stage: its key in the outputs (``A_15m_NAIK``), what it was and its result."""

    key: str
    spec: RunSpec
    result: RunResult
    metrics: StageMetrics

    def record(self, trade_log: str) -> Dict[str, Any]:
        return {
            "mode": self.spec.mode,
            "tf": self.spec.tf,
            "kondisi_gate": self.spec.kondisi_gate,
            "metrics": asdict(self.metrics),
            "blocked": dict(self.result.blocked),
            "trade_log": trade_log,
        }


@dataclass(frozen=True)
class _Outputs:
    """The result file of a stage and the trade log of each run key, by file name."""

    folder: Path
    result: Path
    logs: Mapping[str, str]

    def paths(self) -> List[Path]:
        return [self.result, *(self.folder / name for name in self.logs.values())]


def _outputs(out_dir: PathLike, name: str, stage: str, stamp: str,
             keys: Iterable[str]) -> _Outputs:
    folder = Path(out_dir)
    return _Outputs(folder, folder / f"{name}_{stage}_{stamp}.json",
                    {key: f"{name}_{stage}_{stamp}_{key}.csv" for key in keys})


def _run_key(mode: str, tf: str) -> str:
    return f"{mode}_{tf}_{NAIK}"


def check_phase1(mode: str, pair: str) -> None:
    """Phase 1 runs Mode A on BTC-USDT; any other mode or pair raises ``StageError``."""
    if (mode, pair) != (PHASE1_MODE, PHASE1_PAIR):
        raise StageError(
            f"Phase 1 runs Mode {PHASE1_MODE} on {PHASE1_PAIR} only, got mode {mode!r} and "
            f"pair {pair!r}"
        )


def _stamp(now: datetime) -> str:
    """The WIB day of ``now`` as ``YYYYMMDD``; a naive ``now`` raises ``ValueError``."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now must be timezone-aware, got {now!r}")
    return now.astimezone(WIB).strftime("%Y%m%d")


def _refuse_existing(paths: Iterable[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError(
            f"output files already exist: {', '.join(existing)}; outputs are never overwritten"
        )


def _refuse_after_the_test(data_dir: PathLike, out_dir: PathLike, mode: str, pair: str) -> None:
    """Once the test has started for the mode and pair, it has run: no new lock, no rerun. It has
    started when a run marker of the mode and pair is in ``data_dir`` or the start record is next
    to the lock in ``out_dir``, so a fresh copy of the data does not reopen it."""
    started = lock.started_path(out_dir, mode, pair)
    claims = lock.existing_markers(data_dir, mode, pair) + ([started] if started.exists() else [])
    if claims:
        raise lock.LockError(
            f"the test already ran for Mode {mode} on {pair} ({', '.join(map(str, claims))}); it "
            "runs once, so neither a new lock nor a second test is allowed"
        )


def _naik_days(frames: PairFrames, cfg: ModesConfig, period: _Period, tf: str) -> List[date]:
    """The NAIK days of the stage for Mode A on ``tf`` candles: the runs are measured on these.

    An hour counts only once Mode A can signal in it (spec section 5): the 1h close at ``h`` is
    the condition of the trigger closes in ``[h, h + 1h)``, so the hour counts when the first
    known trigger EMA comes before ``h + 1h``. Training starts with no 1m bar before it, so its
    first 160 candles (40 hours on 15m) have no EMA and hold no NAIK hour."""
    known_ms = frames.triggers[tf].first_known_ms()
    if known_ms is None or known_ms >= period.end_ms:
        return []
    window = parse_weekly_window(cfg.umum.jendela_entry_mingguan)
    start_ms = max(period.start_ms, known_ms - INTERVAL_1H + 1)
    days = frames.hourly.naik_days(
        start_ms, period.end_ms, window, cfg.uji.hari_naik_minimal_jam_naik
    )
    return sorted(days)


def _metrics(result: RunResult, hari_naik: int, cfg: ModesConfig) -> StageMetrics:
    """The metrics of a run; the one call of ``stage_metrics``, so the trim is the config's."""
    return stage_metrics(result.trades, hari_naik, cfg.uji.lolos_buang_posisi_teratas)


def _run(frames: PairFrames, cfg: ModesConfig, mode: str, tf: str, period: _Period,
         hari_naik: int) -> _Run:
    """``mode`` on ``tf`` candles over ``period``, trading in NAIK only."""
    spec = RunSpec(mode, frames.symbol, tf, NAIK, period.start_ms, period.end_ms)
    result = simulate_run(frames, cfg, spec)
    return _Run(_run_key(mode, tf), spec, result, _metrics(result, hari_naik, cfg))


def _comparator(frames: PairFrames, cfg: ModesConfig, period: _Period, hari_naik: int) -> _Run:
    return _run(frames, cfg, MODE_D, cfg.mode_d.tf_pemicu, period, hari_naik)


def _write_logs(outputs: _Outputs, runs: Sequence[_Run]) -> None:
    for run in runs:
        write_trade_log(outputs.folder / outputs.logs[run.key], run.result.trades)


def _run_records(outputs: _Outputs, runs: Sequence[_Run]) -> Dict[str, Any]:
    return {run.key: run.record(outputs.logs[run.key]) for run in runs}


# ----------------------------------------------------------------------------- train


@dataclass(frozen=True)
class _Training:
    """What the training stage measured: its runs in output order, among them the first Mode A
    run (15m), the one whose timeframe is locked and the comparator, the 5m switch and the
    training files it read."""

    period: _Period
    files: int
    manifest_sha256: str
    naik_days: Tuple[date, ...]
    runs: Tuple[_Run, ...]
    first: _Run
    locked: _Run
    comparator: _Run
    switch: Mapping[str, Any]


def _require_first_tf(cfg: ModesConfig) -> None:
    if cfg.mode_a.tf_pemicu != FIRST_TF:
        raise StageError(
            f"mode.A_pullback_long.tf_pemicu is {cfg.mode_a.tf_pemicu}: training starts on "
            f"{FIRST_TF} candles and reaches {SWITCH_TF} only through the pre-registered switch"
        )


def _require_warm(frames: PairFrames, period: _Period) -> None:
    """Every hourly series must exist at the first training minute, or the stage would lose its
    first days without a word: a warm-up too short is refused, not run."""
    context = frames.hourly.context_at(period.start_ms)
    cold = [name for name in _HOURLY_SERIES if getattr(context, name) is None]
    if cold:
        raise StageError(
            f"{', '.join(cold)} still None at {period.start.isoformat()}, the start of training: "
            "the warm-up files are too short for the hourly series"
        )


def _train(data_dir: PathLike, pair: str, cfg: ModesConfig) -> _Training:
    """Run the training stage on its own days: Mode A on 15m, the comparator and, when the
    switch says so, Mode A once more on 5m."""
    period = _training(cfg.uji)
    pair_data = load_pair(data_dir, pair, period.first_day, period.last_day)
    files = stage_files(data_dir, pair, period.first_day, period.last_day)
    manifest_sha = manifest_sha256(manifest(data_dir, files))
    frames = build_pair_frames(pair_data, cfg)
    _require_warm(frames, period)
    naik = tuple(_naik_days(frames, cfg, period, cfg.mode_a.tf_pemicu))
    first = _run(frames, cfg, MODE_A, cfg.mode_a.tf_pemicu, period, len(naik))
    comparator = _comparator(frames, cfg, period, len(naik))
    switched = wants_5m(first.metrics, cfg.uji)
    if switched:
        naik_5m = _naik_days(frames, cfg, period, SWITCH_TF)
        locked = _run(frames, cfg, MODE_A, SWITCH_TF, period, len(naik_5m))
    else:
        locked = first
    switch = {
        "entry_per_hari_naik_15m": first.metrics.entry_per_hari_aktif,
        "pindah_bila_kurang_dari": cfg.uji.pindah_5m_jika_entry_per_hari_naik_kurang_dari,
        "pindah": switched,
    }
    runs = (first, locked, comparator) if switched else (first, comparator)
    return _Training(period, len(files), manifest_sha, naik, runs, first, locked, comparator,
                     switch)


def _train_status(mode: str, pair: str, cfg: ModesConfig, training: _Training) -> str:
    m, tf, switch = training.locked.metrics, training.locked.spec.tf, training.switch
    decision = f"pindah ke {SWITCH_TF}" if switch["pindah"] else f"tetap {FIRST_TF}"
    return (
        f"Latihan Mode {mode} {pair}: entry per hari NAIK pada {FIRST_TF} = "
        f"{_exact(switch['entry_per_hari_naik_15m'])} (pindah ke {SWITCH_TF} bila < "
        f"{_exact(switch['pindah_bila_kurang_dari'])}): {decision}, dikunci {tf}. "
        f"Hasil per 1 juta ({tf}) = {_exact(m.hasil_per_1_juta)}; tanpa "
        f"{cfg.uji.lolos_buang_posisi_teratas} posisi teratas = {_exact(m.hasil_tanpa_teratas)}; "
        f"posisi {m.posisi}."
    )


def _train_doc(mode: str, pair: str, cfg: ModesConfig, training: _Training, bound: _Bindings,
               outputs: _Outputs, lock_file: Path, now: datetime) -> Dict[str, Any]:
    return {
        "stage": TRAIN, "mode": mode, "pair": pair, "tf": training.locked.spec.tf,
        "created_at": now.isoformat(), **asdict(bound), "train_files": training.files,
        "bounds": training.period.record(),
        "naik_days": [day.isoformat() for day in training.naik_days],
        "hari_naik": len(training.naik_days), "drop_top": cfg.uji.lolos_buang_posisi_teratas,
        "switch_5m": dict(training.switch), "runs": _run_records(outputs, training.runs),
        "lock": lock_file.name,
        "files": [*(outputs.logs[run.key] for run in training.runs), outputs.result.name,
                  lock_file.name],
        "status_line": _train_status(mode, pair, cfg, training),
    }


def _lock_doc(mode: str, pair: str, cfg: ModesConfig, training: _Training, bound: _Bindings,
              outputs: _Outputs, now: datetime) -> Dict[str, Any]:
    """The lock: the bindings, the locked timeframe, the bounds and the training summary."""
    return {
        "mode": mode, "pair": pair, "tf": training.locked.spec.tf, **asdict(bound),
        "latihan": training.period.record(), "ujian": _testing(cfg.uji).record(),
        "summary": {
            "hari_naik": len(training.naik_days),
            "drop_top": cfg.uji.lolos_buang_posisi_teratas,
            "pindah_5m": training.switch["pindah"],
            "mode_a_15m": asdict(training.first.metrics),
            "mode_a": asdict(training.locked.metrics),
            "pembanding": asdict(training.comparator.metrics),
        },
        "train_result": outputs.result.name,
        "created_at": now.isoformat(),
    }


def train_stage(
    config_path: PathLike,
    data_dir: PathLike,
    out_dir: PathLike,
    mode: str,
    pair: str,
    now: datetime,
) -> Path:
    """Run the training stage and write its result, its trade logs and the lock; return the
    result file. The module docstring says what it reads and what it refuses."""
    check_phase1(mode, pair)
    stamp = _stamp(now)
    cfg, config_sha = load_modes_config_with_sha256(Path(config_path))
    code_sha, python = lock.code_sha256(), lock.python_version()
    _require_first_tf(cfg)
    _refuse_after_the_test(data_dir, out_dir, mode, pair)
    keys = (_run_key(MODE_A, FIRST_TF), _run_key(MODE_A, SWITCH_TF),
            _run_key(MODE_D, cfg.mode_d.tf_pemicu))
    outputs = _outputs(out_dir, lock.run_name(mode, pair), TRAIN, stamp, keys)
    lock_file = lock.lock_path(out_dir, mode, pair)
    _refuse_existing([*outputs.paths(), lock_file])

    training = _train(data_dir, pair, cfg)
    bound = _Bindings(config_sha, code_sha, training.manifest_sha256, python)
    _write_logs(outputs, training.runs)
    write_json_exclusive(outputs.result,
                         _train_doc(mode, pair, cfg, training, bound, outputs, lock_file, now),
                         "training result")
    write_json_exclusive(lock_file, _lock_doc(mode, pair, cfg, training, bound, outputs, now),
                         "lock")
    return outputs.result


# ----------------------------------------------------------------------------- test


def _training_manifest_sha(data_dir: PathLike, pair: str, uji: TestConfig) -> str:
    """The sha256 of the training files as they are now: hashed, never parsed. A file that is
    gone means the training data is not the one locked."""
    period = _training(uji)
    files = stage_files(data_dir, pair, period.first_day, period.last_day)
    try:
        return manifest_sha256(manifest(data_dir, files))
    except FileNotFoundError as exc:
        raise lock.LockError(f"the training files are not those of the lock: {exc}") from exc


def _locked_metrics(summary: Any, key: str) -> Tuple[Optional[StageMetrics], Optional[str]]:
    """The metrics the lock's summary holds under ``key``, or None and why they cannot be."""
    names = sorted(field.name for field in fields(StageMetrics))
    part = summary.get(key) if isinstance(summary, Mapping) else None
    if not isinstance(part, Mapping) or sorted(part) != names:
        return None, f"summary.{key}: not the metrics of a training run"
    return StageMetrics(**part), None


def _switch_problem(tf: Any, m15: StageMetrics, cfg: ModesConfig) -> Optional[str]:
    """Why the locked timeframe is not what the pre-registered switch gives for the locked 15m
    run, or None: an edit of ``tf`` alone cannot change the timeframe the test runs on."""
    try:
        expected = SWITCH_TF if wants_5m(m15, cfg.uji) else FIRST_TF
    except ValueError as exc:
        return f"summary.mode_a_15m: {exc}"
    if tf != expected:
        return f"tf: locked {tf!r}, but the switch rule gives {expected!r} for the locked 15m run"
    return None


def _locked_rows(doc: Mapping[str, Any], path: Path, mode: str, pair: str,
                 cfg: ModesConfig) -> Tuple[str, Tuple[ReportRow, ReportRow]]:
    """The locked timeframe and the Latihan rows of the report, once the lock is checked to be
    the one of this mode, this pair and these two stages, with the timeframe the switch rule
    gives for its own 15m run; the ``LockError`` lists everything that is not so."""
    wanted = (("mode", mode), ("pair", pair), ("latihan", _training(cfg.uji).record()),
              ("ujian", _testing(cfg.uji).record()))
    mismatches = [f"{key}: locked {doc.get(key)!r}, this run has {value!r}"
                  for key, value in wanted if doc.get(key) != value]
    summary = doc.get("summary")
    m15, m15_problem = _locked_metrics(summary, "mode_a_15m")
    mode_a, mode_a_problem = _locked_metrics(summary, "mode_a")
    comparator, comparator_problem = _locked_metrics(summary, "pembanding")
    tf = doc.get("tf")
    switch_problem = _switch_problem(tf, m15, cfg) if m15 is not None else None
    found = (m15_problem, mode_a_problem, comparator_problem, switch_problem)
    problems = [*mismatches, *(problem for problem in found if problem is not None)]
    if problems:
        raise lock.LockError(
            f"{path}: the lock is not the one of this run: {'; '.join(problems)}", problems)
    return tf, (ReportRow(mode, pair, LATIHAN, mode_a),
                ReportRow(COMPARATOR_LABEL, pair, LATIHAN, comparator))


@dataclass(frozen=True)
class _ReadData:
    """What the test stage reads, as the result file records it: the manifest sha256 of every
    file it loads (the days from ``latihan_mulai`` to the day before ``ujian_selesai`` and both
    warm-ups) and of the test-period days alone, each with its number of files."""

    loaded_files: int
    loaded_manifest_sha256: str
    test_day_files: int
    test_days_manifest_sha256: str

    def note(self) -> str:
        """The sentence of the report that names both manifests."""
        return (
            f"Data yang dibaca ujian: {self.loaded_files} berkas (hari latihan, hari ujian dan "
            f"kedua warm-up), manifest sha256 `{self.loaded_manifest_sha256}`; hari ujian saja: "
            f"{self.test_day_files} berkas, manifest sha256 `{self.test_days_manifest_sha256}`."
        )


def _read_data(data_dir: PathLike, pair: str, uji: TestConfig) -> _ReadData:
    """Hash the files the test loads. It reads every test day, so it runs after the claims."""
    training, period = _training(uji), _testing(uji)
    loaded = manifest(data_dir, stage_files(data_dir, pair, training.first_day, period.last_day))
    test_days = manifest(data_dir, day_files(data_dir, pair, period.first_day, period.last_day))
    return _ReadData(len(loaded), manifest_sha256(loaded), len(test_days),
                     manifest_sha256(test_days))


def _refuse_unusable_days(data_dir: PathLike, pair: str, uji: TestConfig) -> None:
    """Every day and warm-up file the test reads must be a regular file. Only names and file
    types are checked, one ``stat`` each, so nothing is read; a wrong file content is found by
    ``verify-copy``, not here."""
    root = Path(data_dir)
    paths = stage_files(root, pair, _training(uji).first_day, _testing(uji).last_day)
    problems = [
        f"{path.relative_to(root).as_posix()} ({'no regular file' if path.exists() else 'missing'})"
        for path in paths
        if not path.is_file()
    ]
    if problems:
        raise FileNotFoundError(
            f"day or warm-up files missing or not regular files in {root}: {', '.join(problems)}"
        )


def _refuse_before_the_marker(data_dir: PathLike, mode: str, pair: str, outputs: _Outputs,
                              uji: TestConfig) -> None:
    """The refusals before the claims: the test started already (a marker of the mode and pair,
    or the start record next to the lock), a test result of any date, an output of today, a day
    or warm-up file that is missing or no regular file. Nothing of the test stage is read."""
    _refuse_after_the_test(data_dir, outputs.folder, mode, pair)
    stem = outputs.result.name.rsplit("_", 1)[0]
    pattern = re.compile(re.escape(stem) + r"_[0-9]{8}\.json")
    earlier = sorted(str(p) for p in outputs.folder.glob(f"{stem}_*.json")
                     if pattern.fullmatch(p.name))
    if earlier:
        raise FileExistsError(f"a test result already exists: {', '.join(earlier)}; "
                              "the test runs once")
    _refuse_existing([*outputs.paths(), outputs.result.with_suffix(".md")])
    _refuse_unusable_days(data_dir, pair, uji)


def _test_status(mode: str, pair: str, cfg: ModesConfig, run: _Run, status: str) -> str:
    m, uji = run.metrics, cfg.uji
    return (
        f"Ujian Mode {mode} {pair} ({run.spec.tf}): {status}. {status_figures(m, uji)}"
    )


def _test_report(mode: str, cfg: ModesConfig, tf: str, rows: Sequence[ReportRow],
                 status_line: str, lock_note: str) -> str:
    """The Indonesian report of the test stage: what ran, the table and the status line."""
    training, period = _training(cfg.uji), _testing(cfg.uji)
    return "\n".join([
        f"# Mode {mode} {rows[0].pair}: latihan dan ujian",
        "",
        f"Pemicu Mode {mode} dikunci pada {tf} saat latihan. Latihan "
        f"{training.first_day.isoformat()} sampai {training.last_day.isoformat()} WIB, ujian "
        f"{period.first_day.isoformat()} sampai {period.last_day.isoformat()} WIB. Baris "
        f"\"{COMPARATOR_LABEL}\" adalah pembanding: aturan SHORT sekarang (Mode D, "
        f"{cfg.mode_d.tf_pemicu}) yang dijalankan saat kondisi NAIK. Baris Latihan diambil dari "
        "lock.",
        "",
        mode_table(rows),
        status_line,
        "",
        f"Tabel membulatkan angka; baris status memakai angka persis. {lock_note}",
        "",
    ])


def _test_doc(mode: str, pair: str, cfg: ModesConfig, tf: str, runs: Sequence[_Run],
              naik: Sequence[date], bound: _Bindings, read: _ReadData, outputs: _Outputs,
              lock_ref: Mapping[str, str], now: datetime) -> Dict[str, Any]:
    uji, report = cfg.uji, outputs.result.with_suffix(".md")
    status = pass_status(runs[0].metrics, uji)
    return {
        "stage": TEST, "mode": mode, "pair": pair, "tf": tf, "created_at": now.isoformat(),
        **lock_ref, **asdict(bound), **asdict(read), "bounds": _testing(uji).record(),
        "naik_days": [day.isoformat() for day in naik], "hari_naik": len(naik),
        "drop_top": uji.lolos_buang_posisi_teratas,
        "pass_rule": {
            "lolos_minimal_posisi": uji.lolos_minimal_posisi,
            "lolos_hasil_per_1_juta_lebih_dari": uji.lolos_hasil_per_1_juta_lebih_dari,
            "lolos_buang_posisi_teratas": uji.lolos_buang_posisi_teratas,
            "lolos_hasil_tanpa_teratas_lebih_dari": uji.lolos_hasil_tanpa_teratas_lebih_dari,
        },
        "runs": _run_records(outputs, runs), "status": status,
        "status_line": _test_status(mode, pair, cfg, runs[0], status), "report": report.name,
        "files": [lock_ref["started"], *(outputs.logs[run.key] for run in runs),
                  outputs.result.name, report.name],
    }


def test_stage(
    config_path: PathLike,
    data_dir: PathLike,
    out_dir: PathLike,
    mode: str,
    pair: str,
    now: datetime,
) -> Path:
    """Run the one-shot test stage against the lock and write its verdict; return the result
    file. The module docstring gives the order of its checks."""
    check_phase1(mode, pair)
    stamp = _stamp(now)
    cfg, config_sha = load_modes_config_with_sha256(Path(config_path))
    lock_file = lock.lock_path(out_dir, mode, pair)
    locked, lock_sha = lock.read_lock(lock_file)
    bound = _Bindings(config_sha, lock.code_sha256(),
                      _training_manifest_sha(data_dir, pair, cfg.uji), lock.python_version())
    lock.check_lock(locked, config_sha=bound.config_sha256, code_sha=bound.code_sha256,
                    train_manifest_sha=bound.train_manifest_sha256, python=bound.python)
    tf, latihan_rows = _locked_rows(locked, lock_file, mode, pair, cfg)
    keys = (_run_key(MODE_A, tf), _run_key(MODE_D, cfg.mode_d.tf_pemicu))
    outputs = _outputs(out_dir, lock.run_name(mode, pair), TEST, stamp, keys)
    _refuse_before_the_marker(data_dir, mode, pair, outputs, cfg.uji)
    started = lock.started_path(out_dir, mode, pair)
    marker = lock.marker_path(data_dir, mode, pair, lock_sha)
    lock_ref = {"lock": lock_file.name, "lock_sha256": lock_sha, "marker": str(marker),
                "started": started.name}
    claim = {"mode": mode, "pair": pair, "tf": tf, **lock_ref, **asdict(bound),
             "ujian": _testing(cfg.uji).record(), "created_at": now.isoformat()}
    lock.claim_started(started, {**claim, "data_dir": str(Path(data_dir).resolve())})
    lock.claim_marker(marker, claim)

    # Only now is any day of the test stage read: hashed for the record, then loaded.
    training, period = _training(cfg.uji), _testing(cfg.uji)
    read = _read_data(data_dir, pair, cfg.uji)
    frames = build_pair_frames(load_pair(data_dir, pair, training.first_day, period.last_day), cfg)
    naik = _naik_days(frames, cfg, period, tf)
    runs = (_run(frames, cfg, MODE_A, tf, period, len(naik)),
            _comparator(frames, cfg, period, len(naik)))
    doc = _test_doc(mode, pair, cfg, tf, runs, naik, bound, read, outputs, lock_ref, now)
    rows = (*latihan_rows, ReportRow(mode, pair, UJIAN, runs[0].metrics),
            ReportRow(COMPARATOR_LABEL, pair, UJIAN, runs[1].metrics))
    _write_logs(outputs, runs)
    write_json_exclusive(outputs.result, doc, "test result")
    lock_note = (f"Lock `{lock_file.name}` (sha256 `{lock_sha}`), marker "
                 f"`{lock.MARKER_DIR}/{marker.name}`. {read.note()}")
    write_exclusive(outputs.result.with_suffix(".md"),
                    _test_report(mode, cfg, tf, rows, doc["status_line"], lock_note), "test report")
    return outputs.result
