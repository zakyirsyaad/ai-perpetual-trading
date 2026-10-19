"""The train and test stages of mode L: the Phase 1 one-shot experiment with the live bot's exits.

Research code; the bot never imports it. Mode L runs on BTC-USDT only (spec section 8). It reads
two configuration files, each once, hashing the bytes it parses: ``config/modes.yaml`` (the
general rules, the selector, Mode D's trigger numbers and the ``uji`` dates and pass rule) and
``config/modes_live_exit.yaml`` (mode L's trigger band, its exits and the costs). A ``mode L``
with ``aktif: false``, a pair other than BTC-USDT and a live trigger that differs from Mode D's
are refused (``StageError``, ``ModesConfigError``; exit code 1) before anything is written.

Each stage runs four runs (spec section 4) and the comparator that never trades:

* ``L_i_NAIK`` and ``L_ii_NAIK``: mode L, the near-mean LONG in NAIK, with the live exits (i) and
  with the loss cut added (ii);
* ``S_i_TURUN`` and ``S_ii_TURUN``: comparator S, the same rule as a SHORT in TURUN;
* "Tidak trading": no position and a result of 0, in the result file and the table.

L is measured on the stage's NAIK days and S on its TURUN days (``measured_days``), both with the
weekly window and ``uji.hari_naik_minimal_jam_naik``, counted from the first hour in which the
trigger can signal: an hour counts once the first 3m stretch is known before the hour ends, the
rule of Phase 1's ``_naik_days`` with the stretch in place of Mode A's EMA. Every run also gets
the entries per calendar day, over the WIB calendar days of the stage (``calendar_days``). The pass
status (LOLOS, GAGAL or BELUM CUKUP SAMPEL, the Phase 1 rule) is given for L(i) and L(ii) only; S
is a comparator, shown with its numbers and no status, as Phase 1's comparator was.

``train_live_stage`` reads the training days only and writes
``mode_L_BTC-USDT_train_<YYYYMMDD>.json``, a trade log per run (the live columns) and, last, the lock ``mode_L_BTC-USDT_lock.json``. The lock
binds what Phase 1's binds (``config_sha256`` of ``modes.yaml``, ``code_sha256``,
``train_manifest_sha256``, ``python``) and ``live_config_sha256``, the sha256 of the live config,
checked through ``lock.check_lock``'s ``extra``.

``test_live_stage`` keeps Phase 1's order of checks:

1. read both configurations and the lock; hash the training files (both warm-ups among them),
   reading nothing else; refuse (``LockError``, exit code 2) unless the lock binds both config
   sha256s, the code, the training files and this Python, and describes this mode, this pair,
   these two stages and a training summary of the four runs;
2. refuse once the test has started (a mode L marker in this data folder, or the start record next
   to the lock; ``LockError``), when a test result of any date or an output of today exists
   (``FileExistsError``) and when a day or warm-up file is missing or no regular file
   (``FileNotFoundError``): the test days are only ``stat``-ed here, their content is
   ``verify-copy``'s check;
3. claim the start record ``mode_L_BTC-USDT_test_started.json`` and then the run marker
   ``<data_dir>/holdout_runs/mode_L_BTC-USDT_<lock sha256>.json``;
4. only then hash and read the days from ``latihan_mulai`` to the day before ``ujian_selesai``,
   run the four runs over the test stage and write the result, the trade logs and the Indonesian
   report, each exclusively. The result and the report record ``loaded_manifest_sha256`` and
   ``test_days_manifest_sha256`` as Phase 1's do.

A run that dies after step 3 has used the test. Mode A's records are named ``mode_A_...`` and mode
L's ``mode_L_...``, so neither refuses the other.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from bot.core.entry_window import parse_weekly_window
from bot.strategy.modes.checks import LONG, SHORT
from bot.strategy.modes.config import (
    LiveExitConfig,
    ModesConfig,
    TestConfig,
    check_live_trigger_matches,
    load_live_exit_config_with_sha256,
    load_modes_config_with_sha256,
)
from bot.strategy.modes.regime import NAIK, TURUN
from research.pine_gate.features import INTERVAL_1H
from research.pine_gate.fileio import write_exclusive, write_json_exclusive

from . import lock
from .data import load_pair, manifest, manifest_sha256, stage_files
from .frames import PairFrames, build_pair_frames
from .live_sim import MODE_L, MODE_S, VERSION_I, VERSION_II, LiveRunSpec, simulate_live_run
from .metrics import StageMetrics, pass_status
from .report import (
    LiveRow,
    exact,
    format_number,
    live_no_trading_row,
    live_status_line,
    live_table,
)
from .simulate import RunResult
from .stages import (
    LATIHAN,
    PHASE1_PAIR,
    TEST,
    TRAIN,
    UJIAN,
    Bindings,
    Outputs,
    Period,
    ReadData,
    StageError,
    locked_metrics,
    outputs_for,
    read_data,
    refuse_after_the_test,
    refuse_before_the_marker,
    refuse_existing,
    require_warm,
    run_metrics,
    stamp,
    testing_period,
    training_manifest_sha,
    training_period,
)
from .tradelog import write_trade_log

__all__ = [
    "LIVE_CONFIG_KEY",
    "LIVE_PAIR",
    "RUN_KEYS",
    "StageError",
    "calendar_days",
    "check_mode_l",
    "measured_days",
    "test_live_stage",
    "train_live_stage",
]

PathLike = Union[str, Path]

LIVE_PAIR = PHASE1_PAIR  # mode L runs on BTC-USDT only
LIVE_CONFIG_KEY = "live_config_sha256"  # the extra binding of the lock
STATUS_VERSIONS = (VERSION_I, VERSION_II)  # the versions of L that get a pass status
_SIDE_NAMES = {LONG: "LONG", SHORT: "SHORT"}
_PASS_RULE = (
    "lolos_minimal_posisi",
    "lolos_hasil_per_1_juta_lebih_dari",
    "lolos_buang_posisi_teratas",
    "lolos_hasil_tanpa_teratas_lebih_dari",
)


@dataclass(frozen=True)
class _Kind:
    """One of the four runs: the mode, its side, the condition it trades in and the version."""

    mode: str
    side: int
    kondisi: str
    versi: str

    @property
    def key(self) -> str:
        return f"{self.mode}_{self.versi}_{self.kondisi}"


_KINDS = tuple(
    _Kind(mode, side, kondisi, versi)
    for mode, side, kondisi in ((MODE_L, LONG, NAIK), (MODE_S, SHORT, TURUN))
    for versi in (VERSION_I, VERSION_II)
)
RUN_KEYS = tuple(kind.key for kind in _KINDS)  # L_i_NAIK, L_ii_NAIK, S_i_TURUN, S_ii_TURUN


def _load_configs(config_path: PathLike, live_config_path: PathLike,
                  pair: str) -> Tuple[ModesConfig, str, LiveExitConfig, str]:
    """Both configurations, each with the sha256 of the bytes it was parsed from, once mode L can
    run them as pre-registered: BTC-USDT (checked before any file is opened), an active mode L and
    a live trigger equal to Mode D's."""
    if pair != LIVE_PAIR:
        raise StageError(f"mode L runs on {LIVE_PAIR} only, got pair {pair!r}")
    cfg, config_sha = load_modes_config_with_sha256(Path(config_path))
    live, live_sha = load_live_exit_config_with_sha256(Path(live_config_path))
    check_mode_l(live, cfg)
    return cfg, config_sha, live, live_sha


def check_mode_l(live: LiveExitConfig, cfg: ModesConfig) -> None:
    """Mode L can run as pre-registered: switched on (``StageError`` otherwise) and with a live
    trigger equal to Mode D's (``ModesConfigError`` otherwise)."""
    if not live.mode_l.aktif:
        raise StageError("mode.L_cermin_live.aktif is false: mode L is switched off")
    check_live_trigger_matches(live, cfg)


def calendar_days(period: Period) -> int:
    """The WIB calendar days of the stage, its first and last day included."""
    return (period.last_day - period.first_day).days + 1


def _first_stretch_ms(frames: PairFrames) -> Optional[int]:
    """The close of the first 3m candle with a known stretch, or None when none has one."""
    bars = frames.stretch_bars
    for i, value in enumerate(frames.stretch):
        if value is not None:
            return bars.open_ms[i] + bars.interval_ms
    return None


def measured_days(frames: PairFrames, cfg: ModesConfig, period: Period,
                  kondisi: str) -> List[date]:
    """The days of ``kondisi`` (NAIK for L, TURUN for S) in the stage, on which the runs are
    measured. The 1h close at ``h`` is the condition of the 3m closes in ``[h, h + 1h)``, so an
    hour counts when the first known stretch comes before ``h + 1h``."""
    known_ms = _first_stretch_ms(frames)
    if known_ms is None or known_ms >= period.end_ms:
        return []
    window = parse_weekly_window(cfg.umum.jendela_entry_mingguan)
    start_ms = max(period.start_ms, known_ms - INTERVAL_1H + 1)
    days = frames.hourly.condition_days(
        start_ms, period.end_ms, window, cfg.uji.hari_naik_minimal_jam_naik, kondisi
    )
    return sorted(days)


@dataclass(frozen=True)
class _LiveRun:
    """One run of a stage: what it was and its result."""

    kind: _Kind
    spec: LiveRunSpec
    result: RunResult
    metrics: StageMetrics

    def record(self, trade_log: str) -> Dict[str, Any]:
        return {
            "mode": self.kind.mode,
            "versi": self.kind.versi,
            "sisi": _SIDE_NAMES[self.kind.side],
            "kondisi_gate": self.kind.kondisi,
            "metrics": asdict(self.metrics),
            "blocked": dict(self.result.blocked),
            "trade_log": trade_log,
        }


@dataclass(frozen=True)
class _Measured:
    """What one stage measured: its condition days, its calendar days and the four runs."""

    period: Period
    naik_days: Tuple[date, ...]
    turun_days: Tuple[date, ...]
    hari_kalender: int
    runs: Tuple[_LiveRun, ...]

    def run(self, mode: str, versi: str) -> _LiveRun:
        return next(r for r in self.runs if (r.kind.mode, r.kind.versi) == (mode, versi))

    def no_trading(self, pair: str, tahap: str) -> LiveRow:
        return live_no_trading_row(pair, tahap, len(self.naik_days), self.hari_kalender)

    def record(self, cfg: ModesConfig) -> Dict[str, Any]:
        return {
            "naik_days": [day.isoformat() for day in self.naik_days],
            "hari_naik": len(self.naik_days),
            "turun_days": [day.isoformat() for day in self.turun_days],
            "hari_turun": len(self.turun_days),
            "hari_kalender": self.hari_kalender,
            "drop_top": cfg.uji.lolos_buang_posisi_teratas,
        }


def _measure(frames: PairFrames, cfg: ModesConfig, live: LiveExitConfig, pair: str,
             period: Period) -> _Measured:
    """The four runs over ``period``, each measured on its own condition's days."""
    days = {kondisi: tuple(measured_days(frames, cfg, period, kondisi))
            for kondisi in (NAIK, TURUN)}
    hari_kalender = calendar_days(period)
    runs = []
    for kind in _KINDS:
        spec = LiveRunSpec(pair, kind.side, kind.kondisi, kind.versi, period.start_ms,
                           period.end_ms)
        result = simulate_live_run(frames, cfg, live, spec)
        metrics = run_metrics(result, len(days[kind.kondisi]), cfg, hari_kalender)
        runs.append(_LiveRun(kind, spec, result, metrics))
    return _Measured(period, days[NAIK], days[TURUN], hari_kalender, tuple(runs))


def _rows(pair: str, tahap: str, measured: _Measured) -> List[LiveRow]:
    """The table lines of one stage: the four runs, then "Tidak trading"."""
    rows = [LiveRow(r.kind.mode, r.kind.versi, pair, tahap, r.metrics) for r in measured.runs]
    return [*rows, measured.no_trading(pair, tahap)]


def _write_logs(outputs: Outputs, runs: Sequence[_LiveRun]) -> None:
    for run in runs:
        write_trade_log(outputs.folder / outputs.logs[run.kind.key], run.result.trades, live=True)


def _run_records(outputs: Outputs, runs: Sequence[_LiveRun]) -> Dict[str, Any]:
    return {run.kind.key: run.record(outputs.logs[run.kind.key]) for run in runs}


def _no_trading_record(pair: str, tahap: str, measured: _Measured) -> Dict[str, Any]:
    return asdict(measured.no_trading(pair, tahap).metrics)


# ----------------------------------------------------------------------------- train


def _train_status(pair: str, measured: _Measured) -> str:
    parts = [
        f"versi {versi} hasil per 1 juta = {exact(m.hasil_per_1_juta)}, posisi {m.posisi}"
        for versi in STATUS_VERSIONS
        for m in (measured.run(MODE_L, versi).metrics,)
    ]
    return (
        f"Latihan mode L {pair}: {'; '.join(parts)}; hari NAIK {len(measured.naik_days)}, hari "
        f"TURUN {len(measured.turun_days)}, hari kalender {measured.hari_kalender}."
    )


def _train_doc(pair: str, cfg: ModesConfig, measured: _Measured, bound: Bindings, live_sha: str,
               files: int, outputs: Outputs, lock_file: Path, now: datetime) -> Dict[str, Any]:
    return {
        "stage": TRAIN, "mode": MODE_L, "pair": pair, "created_at": now.isoformat(),
        **asdict(bound), LIVE_CONFIG_KEY: live_sha, "train_files": files,
        "bounds": measured.period.record(), **measured.record(cfg),
        "runs": _run_records(outputs, measured.runs),
        "tidak_trading": _no_trading_record(pair, LATIHAN, measured),
        "lock": lock_file.name,
        "files": [*(outputs.logs[key] for key in RUN_KEYS), outputs.result.name, lock_file.name],
        "status_line": _train_status(pair, measured),
    }


def _lock_doc(pair: str, cfg: ModesConfig, measured: _Measured, bound: Bindings, live_sha: str,
              outputs: Outputs, now: datetime) -> Dict[str, Any]:
    """The lock: the bindings, the stage bounds and the training summary of the four runs."""
    return {
        "mode": MODE_L, "pair": pair, **asdict(bound), LIVE_CONFIG_KEY: live_sha,
        "latihan": measured.period.record(), "ujian": testing_period(cfg.uji).record(),
        "summary": {**measured.record(cfg),
                    "runs": {run.kind.key: asdict(run.metrics) for run in measured.runs}},
        "train_result": outputs.result.name,
        "created_at": now.isoformat(),
    }


def train_live_stage(
    config_path: PathLike,
    live_config_path: PathLike,
    data_dir: PathLike,
    out_dir: PathLike,
    pair: str,
    now: datetime,
) -> Path:
    """Run mode L's training stage and write its result, its trade logs and the lock; return the
    result file. The module docstring says what it reads and what it refuses."""
    day_stamp = stamp(now)
    cfg, config_sha, live, live_sha = _load_configs(config_path, live_config_path, pair)
    code_sha, python = lock.code_sha256(), lock.python_version()
    refuse_after_the_test(data_dir, out_dir, MODE_L, pair)
    outputs = outputs_for(out_dir, lock.run_name(MODE_L, pair), TRAIN, day_stamp, RUN_KEYS)
    lock_file = lock.lock_path(out_dir, MODE_L, pair)
    refuse_existing([*outputs.paths(), lock_file])

    period = training_period(cfg.uji)
    pair_data = load_pair(data_dir, pair, period.first_day, period.last_day)
    files = stage_files(data_dir, pair, period.first_day, period.last_day)
    bound = Bindings(config_sha, code_sha, manifest_sha256(manifest(data_dir, files)), python)
    frames = build_pair_frames(pair_data, cfg)
    require_warm(frames, period)
    measured = _measure(frames, cfg, live, pair, period)
    _write_logs(outputs, measured.runs)
    write_json_exclusive(outputs.result,
                         _train_doc(pair, cfg, measured, bound, live_sha, len(files), outputs,
                                    lock_file, now),
                         "training result")
    write_json_exclusive(lock_file, _lock_doc(pair, cfg, measured, bound, live_sha, outputs, now),
                         "lock")
    return outputs.result


# ----------------------------------------------------------------------------- test


def _locked_rows(doc: Mapping[str, Any], path: Path, pair: str, cfg: ModesConfig) -> List[LiveRow]:
    """The Latihan lines of the report, once the lock is checked to be mode L's for this pair
    and these two stages, with a training summary of every run; the ``LockError`` lists every
    problem."""
    training = training_period(cfg.uji)
    wanted = (("mode", MODE_L), ("pair", pair), ("latihan", training.record()),
              ("ujian", testing_period(cfg.uji).record()))
    problems = [f"{key}: locked {doc.get(key)!r}, this run has {value!r}"
                for key, value in wanted if doc.get(key) != value]
    summary = doc.get("summary")
    runs = summary.get("runs") if isinstance(summary, Mapping) else None
    metrics: Dict[str, StageMetrics] = {}
    for key in RUN_KEYS:
        found, _problem = locked_metrics(runs, key)
        if found is None:
            problems.append(f"summary.runs.{key}: not the metrics of a training run")
        else:
            metrics[key] = found
    if problems:
        raise lock.LockError(
            f"{path}: the lock is not the one of this run: {'; '.join(problems)}", problems)
    rows = [LiveRow(kind.mode, kind.versi, pair, LATIHAN, metrics[kind.key]) for kind in _KINDS]
    hari_naik = metrics[_KINDS[0].key].hari_naik
    return [*rows, live_no_trading_row(pair, LATIHAN, hari_naik, calendar_days(training))]


def _status(measured: _Measured, uji: TestConfig) -> Dict[str, str]:
    """The pass status of each version of L; S is a comparator and gets none."""
    return {versi: pass_status(measured.run(MODE_L, versi).metrics, uji)
            for versi in STATUS_VERSIONS}


def _status_lines(pair: str, measured: _Measured, uji: TestConfig) -> List[str]:
    return [live_status_line(versi, measured.run(MODE_L, versi).metrics, uji, pair)
            for versi in STATUS_VERSIONS]


def _test_doc(pair: str, cfg: ModesConfig, measured: _Measured, bound: Bindings, live_sha: str,
              read: ReadData, outputs: Outputs, lock_ref: Mapping[str, str],
              now: datetime) -> Dict[str, Any]:
    uji, report = cfg.uji, outputs.result.with_suffix(".md")
    return {
        "stage": TEST, "mode": MODE_L, "pair": pair, "created_at": now.isoformat(),
        **lock_ref, **asdict(bound), LIVE_CONFIG_KEY: live_sha, **asdict(read),
        "bounds": measured.period.record(), **measured.record(cfg),
        "pass_rule": {name: getattr(uji, name) for name in _PASS_RULE},
        "runs": _run_records(outputs, measured.runs),
        "tidak_trading": _no_trading_record(pair, UJIAN, measured),
        "status": _status(measured, uji),
        "status_line": "\n".join(_status_lines(pair, measured, uji)),
        "report": report.name,
        "files": [lock_ref["started"], *(outputs.logs[key] for key in RUN_KEYS),
                  outputs.result.name, report.name],
    }


def _days_text(days: Sequence[date]) -> str:
    return ", ".join(day.isoformat() for day in days) if days else "tidak ada"


def _test_report(pair: str, cfg: ModesConfig, live: LiveExitConfig, measured: _Measured,
                 rows: Sequence[LiveRow], status_lines: Sequence[str], lock_note: str) -> str:
    """The Indonesian report of the test stage: what ran, the days, the table and the status.
    ``rows`` starts with the Latihan lines from the lock, L then S, whose ``hari_naik`` holds the
    training's NAIK and TURUN days."""
    rules, latihan, ujian = live.mode_l, training_period(cfg.uji), measured.period
    hari = {row.mode: row.metrics.hari_naik for row in rows
            if row.tahap == LATIHAN and row.versi == VERSION_I}  # both versions share the days
    return "\n".join([
        f"# Mode L {pair}: latihan dan ujian",
        "",
        f"Mode L adalah LONG saat kondisi NAIK ketika harga dekat rata-rata (pemicu "
        f"{rules.tf_pemicu} bot live: jarak ke EMA{rules.ema_periode} paling jauh "
        f"{format_number(rules.maks_jarak_ke_ema_kali_atr, 2)} × ATR{rules.atr_periode}), "
        "dengan exit bot live. Versi i: stop-loss dan take-profit; versi ii: ditambah potong rugi "
        f"{format_number(rules.potong_rugi_persen, 2)}% mulai menit ke-"
        f"{rules.potong_rugi_setelah_menit}. "
        "Baris S adalah pembanding: aturan yang sama sebagai SHORT saat kondisi TURUN, tanpa "
        "status lolos. Baris \"Tidak trading\" adalah pembanding tanpa posisi (hasil 0). Entry "
        "per hari aktif dihitung atas hari NAIK untuk L dan hari TURUN untuk S; entry per hari "
        "kalender atas semua hari kalender WIB tahap itu. Baris Latihan diambil dari lock.",
        "",
        f"Latihan {latihan.first_day.isoformat()} sampai {latihan.last_day.isoformat()} WIB "
        f"({calendar_days(latihan)} hari kalender, {hari[MODE_L]} hari NAIK, "
        f"{hari[MODE_S]} hari TURUN). Ujian {ujian.first_day.isoformat()} sampai "
        f"{ujian.last_day.isoformat()} WIB ({measured.hari_kalender} hari kalender); hari NAIK: "
        f"{_days_text(measured.naik_days)}; hari TURUN: {_days_text(measured.turun_days)}.",
        "",
        live_table(rows),
        *status_lines,
        "",
        f"Tabel membulatkan angka; baris status memakai angka persis. {lock_note}",
        "",
    ])


def test_live_stage(
    config_path: PathLike,
    live_config_path: PathLike,
    data_dir: PathLike,
    out_dir: PathLike,
    pair: str,
    now: datetime,
) -> Path:
    """Run mode L's one-shot test stage against the lock and write its verdict; return the
    result file. The module docstring gives the order of its checks."""
    day_stamp = stamp(now)
    cfg, config_sha, live, live_sha = _load_configs(config_path, live_config_path, pair)
    lock_file = lock.lock_path(out_dir, MODE_L, pair)
    locked, lock_sha = lock.read_lock(lock_file)
    bound = Bindings(config_sha, lock.code_sha256(),
                     training_manifest_sha(data_dir, pair, cfg.uji), lock.python_version())
    lock.check_lock(locked, config_sha=bound.config_sha256, code_sha=bound.code_sha256,
                    train_manifest_sha=bound.train_manifest_sha256, python=bound.python,
                    extra={LIVE_CONFIG_KEY: live_sha})
    latihan_rows = _locked_rows(locked, lock_file, pair, cfg)
    outputs = outputs_for(out_dir, lock.run_name(MODE_L, pair), TEST, day_stamp, RUN_KEYS)
    refuse_before_the_marker(data_dir, MODE_L, pair, outputs, cfg.uji)
    started = lock.started_path(out_dir, MODE_L, pair)
    marker = lock.marker_path(data_dir, MODE_L, pair, lock_sha)
    lock_ref = {"lock": lock_file.name, "lock_sha256": lock_sha, "marker": str(marker),
                "started": started.name}
    claim = {"mode": MODE_L, "pair": pair, **lock_ref, **asdict(bound), LIVE_CONFIG_KEY: live_sha,
             "ujian": testing_period(cfg.uji).record(), "created_at": now.isoformat()}
    lock.claim_started(started, {**claim, "data_dir": str(Path(data_dir).resolve())})
    lock.claim_marker(marker, claim)

    # Only now is any day of the test stage read: hashed for the record, then loaded.
    training, period = training_period(cfg.uji), testing_period(cfg.uji)
    read = read_data(data_dir, pair, cfg.uji)
    frames = build_pair_frames(load_pair(data_dir, pair, training.first_day, period.last_day), cfg)
    measured = _measure(frames, cfg, live, pair, period)
    doc = _test_doc(pair, cfg, measured, bound, live_sha, read, outputs, lock_ref, now)
    rows = [*latihan_rows, *_rows(pair, UJIAN, measured)]
    _write_logs(outputs, measured.runs)
    write_json_exclusive(outputs.result, doc, "test result")
    lock_note = (f"Lock `{lock_file.name}` (sha256 `{lock_sha}`), marker "
                 f"`{lock.MARKER_DIR}/{marker.name}`. {read.note()}")
    report = _test_report(pair, cfg, live, measured, rows,
                          _status_lines(pair, measured, cfg.uji), lock_note)
    write_exclusive(outputs.result.with_suffix(".md"), report, "test report")
    return outputs.result
