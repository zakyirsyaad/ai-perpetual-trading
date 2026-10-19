"""The weekly ``compare-forward`` stage: mode L backtested over the LONG forward test's week.

Research code; the bot never imports it. Every week the operator exports the forward bot's closed
positions (``report_forward_test.py``, the CSV of ``bot.core.forward_summary``) and runs this stage
on the same dates. It backtests mode L, versions (i) and (ii), over ``[first_day 00:00, end_day
00:00)`` WIB on BTC-USDT and writes, exclusively, ``forward_compare_<first_day>_<end_day>.json`` and
an Indonesian ``.md`` for the manager: the two versions beside the forward summary (positions,
entries per calendar day, result per 1M, the result without the top positions and the exit mix).
It is a comparison, not a test: no pass status, and no lock, start record or marker is read or
written.

What it reads, and when:

1. Refusals that open no file (``CompareRefusal``, exit code 2 on the command line): a pair other
   than BTC-USDT, an empty or reversed window, and a ``--data-dir`` holding ``holdout_runs/`` (the
   Phase 1 data folder, whose one-shot records must never be near a weekly run: use a separate
   forward data folder).
2. ``config/modes.yaml``, then the one-shot refusal (``CompareRefusal``): the day before
   ``first_day`` is loaded to warm the 3m trigger, so it may not come before the date of
   ``uji.ujian_selesai`` (2026-10-14 WIB). Mode L's test period stays unread until its one-shot
   ``test``. Only then ``config/modes_live_exit.yaml``.
3. An existing output refuses the run (``FileExistsError``, exit code 1) before any work.
4. The forward trade CSV (``read_trades_csv``; anything it would not have written is a
   ``ValueError``), summarized over the window with the config's ``lolos_buang_posisi_teratas``.
5. The 1m days ``first_day - 1`` to ``end_day - 1`` and the native 1h and 4h warm-ups. The
   warm-ups hold bars from before ``ujian_selesai``: they are indicator history only (SMA50 1h,
   EMA50 4h and the 720-hour ATR median need about 831 hours). No position is simulated and no
   number reported from before ``first_day`` 00:00 WIB, and the 1m day before ``first_day`` only
   warms the trigger. A missing day (``FileNotFoundError``), a hole between the warm-ups and the
   first loaded day and warm-ups too short for the hourly series at ``first_day`` (``StageError``)
   are expected failures, exit code 1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple, Union

from bot.core.forward_summary import (
    EXIT_AI,
    EXIT_OTHER,
    EXIT_SL,
    EXIT_TIME,
    EXIT_TP,
    ForwardSummary,
    read_trades_csv,
    summarize,
    trades_in_window,
)
from bot.strategy.modes.checks import LONG
from bot.strategy.modes.config import (
    LiveExitConfig,
    ModesConfig,
    TestConfig,
    load_live_exit_config_with_sha256,
    load_modes_config_with_sha256,
)
from bot.strategy.modes.regime import NAIK
from research.pine_gate.bars import BarGapError
from research.pine_gate.cli_common import file_sha256
from research.pine_gate.fileio import write_exclusive, write_json_exclusive
from research.pine_gate.spec import WIB

from . import lock
from .data import load_pair, manifest, manifest_sha256, stage_files
from .frames import PairFrames, build_pair_frames
from .live_sim import MODE_L, VERSION_I, VERSION_II, LiveRunSpec, simulate_live_run
from .metrics import StageMetrics
from .report import exact, format_number
from .stages import NA, Period, StageError, refuse_existing, require_warm, run_metrics, stamp
from .stages_live import LIVE_PAIR, calendar_days, check_mode_l, measured_days

__all__ = [
    "COMPARE_STAGE",
    "CompareRefusal",
    "RUN_KEYS",
    "StageError",
    "compare_forward_stage",
    "output_paths",
]

PathLike = Union[str, Path]

COMPARE_STAGE = "compare_forward"
VERSIONS = (VERSION_I, VERSION_II)
RUN_KEYS = tuple(f"{MODE_L}_{versi}_{NAIK}" for versi in VERSIONS)  # L_i_NAIK, L_ii_NAIK
_ONE_DAY = timedelta(days=1)


class CompareRefusal(Exception):
    """The comparison may not run on these arguments; nothing has been read or written but the
    modes config. The command line exits 2, as for a lock refusal."""


def output_paths(out_dir: PathLike, first_day: date, end_day: date) -> Tuple[Path, Path]:
    """The result JSON and the Indonesian report of the window."""
    stem = f"forward_compare_{first_day.isoformat()}_{end_day.isoformat()}"
    folder = Path(out_dir)
    return folder / f"{stem}.json", folder / f"{stem}.md"


def _refuse_scope(pair: str, first_day: date, end_day: date, data_dir: PathLike) -> None:
    """The refusals that need no file: the pair, the window and the Phase 1 data folder."""
    if pair != LIVE_PAIR:
        raise CompareRefusal(f"mode L runs on {LIVE_PAIR} only, got pair {pair!r}")
    if end_day <= first_day:
        raise CompareRefusal(
            f"empty or reversed window: from {first_day} to {end_day} (to is exclusive)"
        )
    markers = Path(data_dir) / lock.MARKER_DIR
    if markers.exists():
        raise CompareRefusal(
            f"{data_dir} holds {lock.MARKER_DIR}/: it is the Phase 1 data folder, and a weekly "
            "comparison runs on a separate forward data folder"
        )


def _refuse_test_period(first_day: date, uji: TestConfig) -> date:
    """The first day the stage loads, ``first_day - 1``, once it is not inside Phase 1's test
    period: the day ``uji.ujian_selesai`` (a WIB midnight, the end of that period) or later."""
    warm_day = first_day - _ONE_DAY
    test_end_day = uji.ujian_selesai.astimezone(WIB).date()
    if warm_day < test_end_day:
        raise CompareRefusal(
            f"the comparison would load {warm_day}, before {test_end_day} (uji.ujian_selesai): "
            "mode L's test period stays unread until its one-shot test, so --from must be "
            f"{test_end_day + _ONE_DAY} or later"
        )
    return warm_day


def _window(first_day: date, end_day: date) -> Period:
    return Period(datetime.combine(first_day, time.min, tzinfo=WIB),
                  datetime.combine(end_day, time.min, tzinfo=WIB))


def _frames(data_dir: PathLike, pair: str, warm_day: date, last_day: date,
            cfg: ModesConfig) -> Tuple[PairFrames, str]:
    """The frames of the loaded days and the sha256 of the manifest of the files read."""
    pair_data = load_pair(data_dir, pair, warm_day, last_day)
    loaded = manifest_sha256(manifest(data_dir, stage_files(data_dir, pair, warm_day, last_day)))
    try:
        frames = build_pair_frames(pair_data, cfg)
    except BarGapError as exc:
        raise StageError(
            f"the 1h/4h warm-up files in {data_dir} do not reach the first loaded day {warm_day} "
            f"(00:00 WIB): {exc}"
        ) from exc
    return frames, loaded


@dataclass(frozen=True)
class _VersionRun:
    versi: str
    spec: LiveRunSpec
    metrics: StageMetrics
    blocked: Dict[str, int]

    @property
    def key(self) -> str:
        return f"{MODE_L}_{self.versi}_{NAIK}"

    def record(self) -> Dict[str, Any]:
        return {"mode": MODE_L, "versi": self.versi, "sisi": "LONG", "kondisi_gate": NAIK,
                "metrics": asdict(self.metrics), "blocked": dict(self.blocked)}


def _run_versions(frames: PairFrames, cfg: ModesConfig, live: LiveExitConfig, pair: str,
                  period: Period, hari_naik: int) -> Tuple[_VersionRun, ...]:
    """Mode L, both versions, over the window: positions open and close inside it."""
    runs = []
    for versi in VERSIONS:
        spec = LiveRunSpec(pair, LONG, NAIK, versi, period.start_ms, period.end_ms)
        result = simulate_live_run(frames, cfg, live, spec)
        metrics = run_metrics(result, hari_naik, cfg, calendar_days(period))
        runs.append(_VersionRun(versi, spec, metrics, dict(result.blocked)))
    return tuple(runs)


@dataclass(frozen=True)
class _Forward:
    """The forward side: the trade file, its rows and their summary over the window."""

    path: Path
    sha256: str
    rows: int
    rows_in_window: int
    summary: ForwardSummary

    def record(self) -> Dict[str, Any]:
        return {"file": self.path.name, "path": str(self.path), "sha256": self.sha256,
                "rows": self.rows, "rows_in_window": self.rows_in_window}


def _forward(path: PathLike, first_day: date, end_day: date, drop_top: int) -> _Forward:
    trades_path = Path(path)
    trades = read_trades_csv(trades_path)
    in_window = trades_in_window(trades, first_day, end_day)
    return _Forward(trades_path, file_sha256(trades_path), len(trades), len(in_window),
                    summarize(trades, first_day, end_day, drop_top))


# ----------------------------------------------------------------------------- the report


_TABLE_HEADER = ("Baris", "Posisi", "Entry per hari kalender", "Hasil per 1 juta",
                 "Tanpa {top} teratas", "TP", "SL", "Potong", "Waktu", "AI", "Lain")


def _line(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _count(value: int) -> str:
    return format_number(value, 0)


def _mode_l_line(run: _VersionRun) -> str:
    m = run.metrics
    return _line((f"Mode L ({run.versi})", _count(m.posisi),
                  format_number(m.entry_per_hari_kalender, 2),
                  format_number(m.hasil_per_1_juta, 0), format_number(m.hasil_tanpa_teratas, 0),
                  _count(m.tp), _count(m.sl), _count(m.potong), _count(m.waktu), NA, NA))


def _forward_line(s: ForwardSummary) -> str:
    e = s.exits
    return _line(("Uji maju (bot AI LONG)", _count(s.posisi),
                  format_number(s.entry_per_hari_kalender, 2),
                  format_number(s.hasil_per_1_juta, 0), format_number(s.hasil_tanpa_teratas, 0),
                  _count(e[EXIT_TP]), _count(e[EXIT_SL]), NA, _count(e[EXIT_TIME]),
                  _count(e[EXIT_AI]), _count(e[EXIT_OTHER])))


def _table(runs: Sequence[_VersionRun], forward: ForwardSummary, drop_top: int) -> str:
    header = [cell.format(top=drop_top) for cell in _TABLE_HEADER]
    lines = [_line(header), "|" + "---|" * len(header)]
    lines += [_mode_l_line(run) for run in runs]
    lines.append(_forward_line(forward))
    return "\n".join(lines)


def _days_text(days: Sequence[date]) -> str:
    return ", ".join(day.isoformat() for day in days) if days else "tidak ada"


@dataclass(frozen=True)
class _Comparison:
    """Everything one run measured, for the result file and the report."""

    pair: str
    period: Period
    warm_day: date
    test_end_day: date
    naik_days: Tuple[date, ...]
    runs: Tuple[_VersionRun, ...]
    forward: _Forward
    drop_top: int


def _status_line(c: _Comparison, first_day: date, end_day: date) -> str:
    parts = [f"mode L ({run.versi}) hasil per 1 juta = {exact(run.metrics.hasil_per_1_juta)}, "
             f"posisi {run.metrics.posisi}" for run in c.runs]
    s = c.forward.summary
    parts.append(f"uji maju hasil per 1 juta = {exact(s.hasil_per_1_juta)}, posisi {s.posisi}")
    return (f"Banding uji maju {c.pair} {first_day.isoformat()} s/d {end_day.isoformat()} "
            f"(WIB, akhir tidak termasuk): {'; '.join(parts)}.")


def _report(c: _Comparison, cfg: ModesConfig, live: LiveExitConfig, first_day: date,
            end_day: date, loaded_sha: str) -> str:
    rules, costs = live.mode_l, live.biaya
    return "\n".join([
        f"# Banding mingguan: mode L dan uji maju {c.pair}",
        "",
        f"Jendela {first_day.isoformat()} sampai {end_day.isoformat()} WIB (akhir tidak "
        f"termasuk), {calendar_days(c.period)} hari kalender; hari NAIK: "
        f"{_days_text(c.naik_days)}.",
        "",
        "Ini perbandingan mingguan, bukan ujian: tidak ada status lolos atau gagal, dan tidak ada "
        "lock atau marker yang dibaca atau ditulis. Mode L adalah LONG saat kondisi NAIK ketika "
        f"harga dekat rata-rata (pemicu {rules.tf_pemicu} bot live) dengan exit bot live; versi "
        f"ii menambah potong rugi {format_number(rules.potong_rugi_persen, 2)}% mulai menit ke-"
        f"{rules.potong_rugi_setelah_menit}. Baris uji maju adalah posisi bot AI LONG di BingX "
        "VST yang ditutup di dalam jendela.",
        "",
        _table(c.runs, c.forward.summary, c.drop_top),
        "",
        "Hasil per 1 juta = 1.000.000 × jumlah PnL bersih / jumlah volume (kedua kaki); tanpa "
        f"{c.drop_top} teratas membuang {c.drop_top} posisi dengan PnL terbesar. Mode L memakai "
        f"fee {format_number(cfg.umum.fee_taker_persen, 3)}% per kaki dan slippage dari "
        f"`modes_live_exit.yaml` (entry {format_number(costs.slippage_entry_bps, 1)} bps, exit "
        f"pasar {format_number(costs.slippage_exit_market_bps, 1)} bps); uji maju memakai fill "
        "dan fee nyata. Keluar uji maju: TP = neutral_exit atau take_profit, SL = stop_loss, "
        "Waktu = time_stop, AI = keputusan AI (strategy_exit), Lain = alasan lain. Mode L tidak "
        "punya keluar AI, dan potong rugi hanya ada di versi ii.",
        "",
        f"Posisi mode L hanya disimulasikan di dalam jendela; hari {c.warm_day.isoformat()} "
        "dimuat hanya untuk memanaskan pemicu 3m. Indikator 1h dan 4h (SMA50 1h, EMA50 4h, median "
        "ATR 720 jam) memakai bar pemanasan bertanggal sebelum jendela, sebagian sebelum "
        f"{c.test_end_day.isoformat()} (akhir masa ujian Fase 1), hanya sebagai riwayat "
        f"indikator: tidak ada posisi atau angka dari sebelum {first_day.isoformat()} 00:00 WIB.",
        "",
        f"Data: klines produksi, manifest sha256 `{loaded_sha}`; file posisi uji maju "
        f"`{c.forward.path.name}` (sha256 `{c.forward.sha256}`, {c.forward.rows} baris, "
        f"{c.forward.rows_in_window} di dalam jendela). Tabel membulatkan angka; file JSON "
        "menyimpan angka persis.",
        "",
    ])


def _doc(c: _Comparison, first_day: date, end_day: date, shas: Dict[str, str],
         files: Sequence[str], now: datetime) -> Dict[str, Any]:
    return {
        "stage": COMPARE_STAGE, "mode": MODE_L, "pair": c.pair, "created_at": now.isoformat(),
        **shas,
        "window": {"first_day": first_day.isoformat(), "end_day": end_day.isoformat(),
                   **c.period.record()},
        "loaded_days": {"first": c.warm_day.isoformat(),
                        "last": c.period.last_day.isoformat()},
        "ujian_selesai_day": c.test_end_day.isoformat(),
        "hari_kalender": calendar_days(c.period),
        "naik_days": [day.isoformat() for day in c.naik_days],
        "hari_naik": len(c.naik_days),
        "drop_top": c.drop_top,
        "runs": {run.key: run.record() for run in c.runs},
        "forward": asdict(c.forward.summary),
        "forward_trades": c.forward.record(),
        "files": list(files),
        "status_line": _status_line(c, first_day, end_day),
    }


def compare_forward_stage(
    config_path: PathLike,
    live_config_path: PathLike,
    data_dir: PathLike,
    forward_trades: PathLike,
    pair: str,
    first_day: date,
    end_day: date,
    out_dir: PathLike,
    now: datetime,
) -> Path:
    """Backtest mode L (i) and (ii) over ``[first_day, end_day)`` WIB beside the forward test's
    summary of the same window; write the result and the report and return the result file. The
    module docstring gives the order of the checks and what is read."""
    stamp(now)  # a naive clock raises ValueError before anything else
    _refuse_scope(pair, first_day, end_day, data_dir)
    cfg, config_sha = load_modes_config_with_sha256(Path(config_path))
    warm_day = _refuse_test_period(first_day, cfg.uji)
    live, live_sha = load_live_exit_config_with_sha256(Path(live_config_path))
    check_mode_l(live, cfg)
    result, report = output_paths(out_dir, first_day, end_day)
    refuse_existing([result, report])

    drop_top = cfg.uji.lolos_buang_posisi_teratas
    forward = _forward(forward_trades, first_day, end_day, drop_top)
    period = _window(first_day, end_day)
    frames, loaded_sha = _frames(data_dir, pair, warm_day, period.last_day, cfg)
    require_warm(frames, period)
    naik_days = tuple(measured_days(frames, cfg, period, NAIK))
    comparison = _Comparison(pair, period, warm_day, cfg.uji.ujian_selesai.astimezone(WIB).date(),
                             naik_days, _run_versions(frames, cfg, live, pair, period,
                                                      len(naik_days)),
                             forward, drop_top)
    shas = {"config_sha256": config_sha, "live_config_sha256": live_sha,
            "code_sha256": lock.code_sha256(), "loaded_manifest_sha256": loaded_sha}
    doc = _doc(comparison, first_day, end_day, shas, [result.name, report.name], now)
    write_json_exclusive(result, doc, "comparison result")
    write_exclusive(report, _report(comparison, cfg, live, first_day, end_day, loaded_sha),
                    "comparison report")
    return result
