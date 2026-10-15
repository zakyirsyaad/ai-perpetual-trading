"""The one-shot holdout test stage (spec 5.9, 5.10, 5.11).

Research code; the bot never imports it. Standard library only.

``test_stage`` checks the holdout lock first (``load_selection_checked``: the
selection file must exist and carry the current spec sha256 of a spec run) and
that the calibration is the one the selection was made with; only then does it
read the holdout. It replays the holdout [2026-08-05, 2026-10-14) WIB with entry
ticks blocked in the final ``purge_tick_minutes`` (the selection's, refused under the
current spec sha256 unless the calibration gives it), runs contenders 1-3 and the
controls, applies the nine gate criteria per symbol, chooses what moves on and writes
``pine_gate_holdout_<YYYYMMDD>.json``. The holdout runs once: any existing
holdout file in ``out_dir`` or next to the selection file (its real folder, links
resolved), of any date, refuses the run, and so does an existing run marker
``<data_dir>/holdout_runs/<spec_sha256>.json`` (mandatory under the current spec
sha256). Every data check that can refuse comes before the marker is written (the
files checked without computing anything, the training part hashed from a
training-only load), and the full holdout load after it; ``<spec_sha256>.started.json``
next to the marker is written right before the evaluation. A run under the current
spec sha256 must also use the seed recorded in the selection file (the spec seed),
run on the code and the Python version that trained (``code_sha256``,
``python_version``), follow a training C0 that passed, write next to its selection
file and read holdout data whose training part is the data the selection was
trained on. Criterion 2 is also reported under a 5-day block bootstrap
(``sensitivities.c0_block``, report only).

Every per-symbol number comes from that symbol's trades inside the joint
one-position run over BTC and ETH (BTC first), except the C0 controls, which
run each symbol on its own: in a joint run an always-long BTC would take every
tick and ETH would never trade.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .bars import index_closing_at
from .calibrate import Calibration
from .fileio import write_json_exclusive
from .holdout_gate import GateVerdict, choose_contender, gate_symbol, traded_symbols
from .holdout_replay import (
    FEE_SENSITIVITY_PCT,
    LB_Z,
    OVERSHOOT_SENSITIVITY_BPS,
    Contender,
    Holdout,
    HoldoutPeriod,
    brief_metrics,
    build_contenders,
    holdout_markets,
    mean_or_none,
    permutation_ks,
    symbol_metrics,
    trades_of,
)
from .lock import (
    HoldoutLockError,
    check_spec_seed,
    check_spec_sha,
    code_sha256,
    is_spec_sha,
    latencies_sha256,
    load_selection_checked,
    python_version,
)
from .outcomes import OutcomeCache
from .dataset import TRAIN_DATA_UNTIL_MS, DataSource
from .pipeline import (SYMBOLS, Dataset, check_purge_covers, check_recorded_purge,
                       purge_tick_minutes)
from .rules import reference_signal
from .simulate import Market, SignalFn, Trade
from .spec import HOLDOUT_END, WIB, ExecConfig, GateConfig, GuardConfig, ms, spec_sha256
from .stats import (
    bootstrap_paired_block_p,
    bootstrap_paired_p,
    day_aggregates,
    holm,
    permutation_p,
    quantile_sorted as _quantile,
    win_rate,
    wr_lower_bound,
)

HOLDOUT_PREFIX = "pine_gate_holdout_"
HOLDOUT_RUNS_DIR = "holdout_runs"  # one run marker per spec sha256, under the data folder
_MS_PER_MIN = 60_000
_BAR_5M_MS = 5 * _MS_PER_MIN
_PCT = 100.0

# Ruling: offset k starts the clock k * 9 s late (0..171 s within the 182 s tick), seed + k.
OFFSET_STEP_S = 9
# Cadence sensitivity: a one-minute clock that acts only on 5m closes, with no latency
# and no order delay, so the order lands in the first 1m bar after the 5m close.
CADENCE_TICK_S = 60
# "Supply with guards off": limits no holdout day can reach.
GUARDS_OFF = GuardConfig(max_stops=10**9, max_consecutive_losses=10**9,
                         max_drawdown_usdt=1e18, max_entries=10**9, max_volume_usdt=1e18)
COUNT_FIELDS = ("offsets", "permutation_shifts", "c1_seeds", "bootstrap_n")


# --------------------------------------------------------------------------- runs


@dataclass(frozen=True)
class _Runs:
    main: Tuple[Trade, ...]
    overshoot: Tuple[Trade, ...]
    offsets: Tuple[Tuple[Trade, ...], ...]  # offset k = 0 is the main run
    source: Mapping[str, Dict[str, Any]]  # per symbol, criterion 7
    primary: Optional[Tuple[Trade, ...]] = None  # c3 only


def _shifted_p(observed: Optional[float], stats: Sequence[Optional[float]]) -> float:
    """Permutation p; a shifted run without TP+SL counts as reaching the observed WR
    (conservative), and no observed WR gives p = 1."""
    if observed is None:
        return 1.0
    return permutation_p(observed, [math.inf if s is None else s for s in stats])


def _permutation_source(h: Holdout, c: Contender, main: Sequence[Trade]) -> Dict[str, Dict]:
    """Each distinct shift k simulated once; its per-symbol WR counts with multiplicity."""
    ks = permutation_ks(h.seed, h.gate_cfg.permutation_shifts, h.period.length_days())
    multiplicity = Counter(ks)
    stats: Dict[str, List[Optional[float]]] = {s: [] for s in SYMBOLS}
    for k in sorted(multiplicity):
        trades = h.run(c.shifted(k))
        for s in SYMBOLS:
            stats[s].extend([win_rate(trades_of(trades, s))] * multiplicity[k])
    out = {}
    for s in SYMBOLS:
        defined = [w for w in stats[s] if w is not None]
        out[s] = {
            "kind": "permutation", "p": _shifted_p(win_rate(trades_of(main, s)), stats[s]),
            "shifts": len(ks), "distinct_k": sorted(multiplicity),
            "mean_shifted_wr": statistics.fmean(defined) if defined else None,
            "shifted_without_tp_sl": len(stats[s]) - len(defined),
        }
    return out


def _paired_source(h: Holdout, main: Sequence[Trade], primary: Sequence[Trade]) -> Dict[str, Dict]:
    out = {}
    for s in SYMBOLS:
        mine, base = trades_of(main, s), trades_of(primary, s)
        p = bootstrap_paired_p(day_aggregates(mine, h.days), day_aggregates(base, h.days),
                               n=h.gate_cfg.bootstrap_n, seed=h.seed)
        out[s] = {"kind": "paired_vs_primary", "p": p, "primary_wr": win_rate(base),
                  "primary_n": len(base)}
    return out


def _run_contender(h: Holdout, c: Contender) -> _Runs:
    main = tuple(h.run(c.signal))
    overshoot = tuple(h.run(c.signal, overshoot_bps=h.gate_cfg.robust_overshoot_bps))
    offsets = [main] + [
        tuple(h.run(c.signal, seed=h.seed + k, offset_s=k * OFFSET_STEP_S))
        for k in range(1, h.gate_cfg.offsets)
    ]
    if c.shifted is not None:
        return _Runs(main, overshoot, tuple(offsets), _permutation_source(h, c, main))
    primary = tuple(h.run(c.primary))
    return _Runs(main, overshoot, tuple(offsets), _paired_source(h, main, primary), primary)


def _c0_runs(h: Holdout) -> Dict[str, Dict[str, List[Trade]]]:
    """Always long and always short, each symbol on its own clock."""
    return {
        m.symbol: {"long": h.run(lambda s, t: 1, markets=(m,)),
                   "short": h.run(lambda s, t: -1, markets=(m,))}
        for m in h.markets
    }


def _c0_doc(h: Holdout, c0: Mapping[str, Mapping[str, List[Trade]]]) -> Dict[str, Any]:
    out = {}
    for s, runs in c0.items():
        long, short = symbol_metrics(runs["long"], h), symbol_metrics(runs["short"], h)
        out[s] = {"long": long, "short": short, "wr": mean_or_none(long["wr"], short["wr"])}
    return out


# --------------------------------------------------------------------------- gate


def _p_c0(h: Holdout, runs: Mapping[str, _Runs], c0) -> Dict[str, Dict[str, float]]:
    """Criterion 2's raw p: contender above C0 (long + short pooled per day)."""
    out: Dict[str, Dict[str, float]] = {name: {} for name in runs}
    for s in SYMBOLS:
        base = day_aggregates(c0[s]["long"] + c0[s]["short"], h.days)
        for name, r in runs.items():
            out[name][s] = bootstrap_paired_p(day_aggregates(trades_of(r.main, s), h.days), base,
                                              n=h.gate_cfg.bootstrap_n, seed=h.seed)
    return out


def _p_c0_block(h: Holdout, runs: Mapping[str, _Runs], c0) -> Dict[str, Any]:
    """Criterion 2 again under a circular block bootstrap of ``c0_block_days`` days,
    raw and Holm-adjusted (report only: trend regimes last weeks)."""
    days = h.gate_cfg.c0_block_days
    raw: Dict[str, Dict[str, float]] = {name: {} for name in runs}
    for s in SYMBOLS:
        base = day_aggregates(c0[s]["long"] + c0[s]["short"], h.days)
        for name, r in runs.items():
            raw[name][s] = bootstrap_paired_block_p(
                day_aggregates(trades_of(r.main, s), h.days), base,
                n=h.gate_cfg.bootstrap_n, seed=h.seed, block_days=days)
    return {"block_days": days, "p": raw, "p_holm": _holm_by_symbol(raw, h.gate_cfg.holm_family)}


def _holm_by_symbol(
    p_c0: Mapping[str, Mapping[str, float]], family: int
) -> Dict[str, Dict[str, float]]:
    """Holm within each symbol over a family of ``family`` contenders (spec: 3): an
    absent contender counts with p = 1, so skipping one never relaxes the others."""
    pads = {f"<absent {k}>": 1.0 for k in range(max(0, family - len(p_c0)))}
    out: Dict[str, Dict[str, float]] = {name: {} for name in p_c0}
    for s in SYMBOLS:
        adjusted = holm({**{name: p[s] for name, p in p_c0.items()}, **pads})
        for name in p_c0:
            out[name][s] = adjusted[name]
    return out


def _evidence(name: str, s: str, r: _Runs, m: Mapping[str, Any], c0: Mapping[str, Any],
              p_holm: float, pbo_pass: Any) -> Dict[str, Any]:
    """Gate evidence without the supply pair, which depends on the passing set."""
    return {
        "contender": name, "symbol": s, "tp": m["tp"], "sl": m["sl"],
        "gross_bps": m["gross_bps"], "lower_bound": m["lower_bound"], "p_holm": p_holm,
        "wr_halves": list(m["halves"]),
        "wr_overshoot": win_rate(trades_of(r.overshoot, s)),
        "wr_offsets": [win_rate(trades_of(run, s)) for run in r.offsets],
        "source_p": r.source[s]["p"],
        "c0_long_wr": c0[s]["long"]["wr"], "c0_short_wr": c0[s]["short"]["wr"],
        "c0_long_gross": c0[s]["long"]["gross_bps"],
        "c0_short_gross": c0[s]["short"]["gross_bps"],
        "pbo_pass": pbo_pass,
    }


def _gate_contender(
    evidence: Mapping[str, Dict[str, Any]], metrics: Mapping[str, Mapping[str, Any]],
    cfg: GateConfig,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, GateVerdict]]:
    """Criterion 8 supply = combined supply of the symbols passing 1-7 and 9, plus
    the symbol itself when it does not (which fails it anyway)."""
    open_supply = {"supply_holdout": math.inf, "supply_last20": math.inf}
    passing = {s for s, ev in evidence.items() if gate_symbol({**ev, **open_supply}, cfg).passed}
    full, verdicts = {}, {}
    for s, ev in evidence.items():
        members = passing | {s}
        full[s] = {**ev,
                   "supply_holdout": sum(metrics[x]["supply"] for x in members),
                   "supply_last20": sum(metrics[x]["supply_last20"] for x in members),
                   "supply_set": [x for x in SYMBOLS if x in members]}
        verdicts[s] = gate_symbol(full[s], cfg)
    return full, verdicts


def _pooled_lower_bound(
    h: Holdout, r: _Runs, verdicts: Mapping[str, GateVerdict]
) -> Optional[float]:
    """WR lower bound of the trades on the contender's passing symbols, pooled per day."""
    if not any(v.passed for v in verdicts.values()):
        return None
    passing = [t for t in r.main if verdicts[t.symbol].passed]
    return wr_lower_bound(day_aggregates(passing, h.days), LB_Z)


# --------------------------------------------------------------------------- controls


def _entry_index(market: Market, trade: Trade) -> int:
    i = index_closing_at(market.bars_1m, trade.entry_ms)
    if i is None:
        raise ValueError(f"{trade.symbol}: no 1m bar closes at {trade.entry_ms}")
    return i


def _random_side_wr(market: Market, entries: Sequence[int], seed: int) -> Optional[float]:
    rng = random.Random(seed)
    tp = sl = 0
    for idx in entries:
        o = market.outcomes.get(idx, rng.choice((1, -1)))
        if o is not None:
            tp += o.kind == "TP"
            sl += o.kind == "SL"
    return _PCT * tp / (tp + sl) if tp + sl else None


def _c1_control(h: Holdout, main: Sequence[Trade]) -> Dict[str, Dict[str, Any]]:
    """C1: the contender's entry bars with a random side, one seed per draw."""
    out = {}
    for market in h.markets:
        entries = [_entry_index(market, t) for t in trades_of(main, market.symbol)]
        wrs = [_random_side_wr(market, entries, h.seed + j) for j in range(h.gate_cfg.c1_seeds)]
        valid = sorted(w for w in wrs if w is not None)
        out[market.symbol] = {
            "n_entries": len(entries), "n_seeds": len(wrs),
            "mean": statistics.fmean(valid) if valid else None,
            "p5": _quantile(valid, 0.05), "p95": _quantile(valid, 0.95),
        }
    return out


# --------------------------------------------------------------------------- sensitivities


class _CoSignal:
    """Wraps a signal; at each evaluated tick asks every symbol once and counts the
    ticks on which both signal."""

    def __init__(self, signal: SignalFn) -> None:
        self._signal = signal
        self._t: Optional[int] = None
        self._sides: Dict[str, int] = {}
        self.ticks = 0
        self.both = 0

    def __call__(self, symbol: str, t: int) -> int:
        if t != self._t:
            self._t = t
            self._sides = {s: self._signal(s, t) for s in SYMBOLS}
            self.ticks += 1
            self.both += all(self._sides.values())
        return self._sides[symbol]

    def summary(self) -> Dict[str, Any]:
        share = self.both / self.ticks if self.ticks else None
        return {"evaluated_ticks": self.ticks, "both_signal": self.both, "share": share}


def _eth_first(h: Holdout, c: Contender) -> Dict[str, Any]:
    recorder = _CoSignal(c.signal)
    trades = h.run(recorder, markets=tuple(reversed(h.markets)))
    return {"symbols": {s: brief_metrics(trades_of(trades, s), h) for s in SYMBOLS},
            "co_signal": recorder.summary()}


def _cadence(h: Holdout, c: Contender) -> Dict[str, Any]:
    """Entry at the close of the first 1m bar after the 5m close, zero latency."""
    cfg = replace(h.exec_cfg, tick_s=CADENCE_TICK_S, idle_tick_s=0, order_delay_s=0)
    markets = tuple(Market(m.symbol, m.bars_1m, OutcomeCache(m.bars_1m, cfg), m.funding_ms)
                    for m in h.markets)

    def signal(symbol: str, t: int) -> int:
        return c.signal(symbol, t) if t % _BAR_5M_MS == 0 else 0

    trades = h.run(signal, markets=markets, exec_cfg=cfg, latencies_ms=(),
                   allow_zero_latency=True)
    return {s: brief_metrics(trades_of(trades, s), h) for s in SYMBOLS}


def _guards_off(h: Holdout, c: Contender) -> Dict[str, Any]:
    trades = h.run(c.signal, guard_cfg=GUARDS_OFF)
    return {"symbols": {s: brief_metrics(trades_of(trades, s), h) for s in SYMBOLS},
            "combined_supply": len(trades) / len(h.days)}


def _overshoot(h: Holdout, c: Contender) -> Dict[str, Dict[str, Any]]:
    """The main run again with each sensitivity stop overshoot (spec 5.10)."""
    out = {}
    for bps in OVERSHOOT_SENSITIVITY_BPS:
        trades = h.run(c.signal, overshoot_bps=bps)
        out[str(float(bps))] = {s: brief_metrics(trades_of(trades, s), h) for s in SYMBOLS}
    return out


def _sensitivities(h: Holdout, contenders: Sequence[Contender]) -> Dict[str, Any]:
    """Reported only; none of these numbers reaches the gate."""
    frames = h.frames
    reference = h.run(lambda s, t: reference_signal(frames[s].snapshot(t)))
    return {
        "eth_first": {c.name: _eth_first(h, c) for c in contenders},
        "cadence": {c.name: _cadence(h, c) for c in contenders},
        "guards_off": {c.name: _guards_off(h, c) for c in contenders},
        "overshoot": {c.name: _overshoot(h, c) for c in contenders},
        "reference": {s: symbol_metrics(trades_of(reference, s), h) for s in SYMBOLS},
        "fee_pct": FEE_SENSITIVITY_PCT,
    }


# --------------------------------------------------------------------------- evaluation


def _contender_doc(h: Holdout, c: Contender, r: _Runs, metrics, evidence, verdicts,
                   p_c0, p_holm) -> Dict[str, Any]:
    symbols = {}
    for s in SYMBOLS:
        symbols[s] = {
            **metrics[s],
            "robustness": {"wr_overshoot": evidence[s]["wr_overshoot"],
                           "overshoot_bps": h.gate_cfg.robust_overshoot_bps,
                           "wr_offsets": evidence[s]["wr_offsets"],
                           "offsets_s": [k * OFFSET_STEP_S for k in range(len(r.offsets))]},
            "source": r.source[s], "p_c0": p_c0[s], "p_holm": p_holm[s],
            "evidence": evidence[s], "verdict": asdict(verdicts[s]),
        }
    doc = {"rule": c.rule.key() if c.rule is not None else None, "symbols": symbols,
           "pooled_lower_bound": _pooled_lower_bound(h, r, verdicts),
           "symbols_passed": [s for s in SYMBOLS if verdicts[s].passed]}
    if r.primary is not None:
        doc["primary"] = {s: symbol_metrics(trades_of(r.primary, s), h) for s in SYMBOLS}
    return doc


def _evaluate(h: Holdout, selection: Mapping[str, Any]) -> Dict[str, Any]:
    contenders, skipped = build_contenders(selection, h.frames, h.period)
    c0 = _c0_runs(h)
    c0_doc = _c0_doc(h, c0)
    runs = {c.name: _run_contender(h, c) for c in contenders}
    p_c0 = _p_c0(h, runs, c0)
    p_holm = _holm_by_symbol(p_c0, h.gate_cfg.holm_family)
    docs, all_verdicts = {}, []
    for c in contenders:
        r = runs[c.name]
        metrics = {s: symbol_metrics(trades_of(r.main, s), h) for s in SYMBOLS}
        evidence = {s: _evidence(c.name, s, r, metrics[s], c0_doc, p_holm[c.name][s],
                                 selection.get("pbo_pass")) for s in SYMBOLS}
        evidence, verdicts = _gate_contender(evidence, metrics, h.gate_cfg)
        all_verdicts.extend(verdicts[s] for s in SYMBOLS)
        docs[c.name] = _contender_doc(h, c, r, metrics, evidence, verdicts,
                                      p_c0[c.name], p_holm[c.name])
    chosen = choose_contender(all_verdicts, {n: d["pooled_lower_bound"] for n, d in docs.items()})
    return {
        "contenders": docs, "skipped": skipped,
        "controls": {"c0": c0_doc,
                     "c1": {c.name: _c1_control(h, runs[c.name].main) for c in contenders}},
        "sensitivities": {**_sensitivities(h, contenders), "c0_block": _p_c0_block(h, runs, c0)},
        "chosen": chosen, "symbols_to_trade": list(traded_symbols(all_verdicts, chosen)),
    }


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class _StageSettings:
    """Everything a caller may inject; a spec run uses the defaults for all of it."""

    period: HoldoutPeriod
    exec_cfg: ExecConfig
    guard_cfg: GuardConfig
    gate_cfg: GateConfig

    @classmethod
    def spec(cls) -> "_StageSettings":
        return cls(HoldoutPeriod.spec(), ExecConfig(), GuardConfig(), GateConfig())

    def record(self) -> Dict[str, Any]:
        return {
            "configs": {"exec": asdict(self.exec_cfg), "guard": asdict(self.guard_cfg),
                        "gate": asdict(self.gate_cfg)},
            "period": self.period.to_dict(),
            "counts": {f: getattr(self.gate_cfg, f) for f in COUNT_FIELDS},
        }

    def non_spec_fields(self) -> List[str]:
        mine, spec = self.record(), _StageSettings.spec().record()
        fields = [n for n in spec["configs"] if mine["configs"][n] != spec["configs"][n]]
        return fields + (["period"] if mine["period"] != spec["period"] else [])


def _settings(period, exec_cfg, guard_cfg, gate_cfg, counts: Mapping[str, Optional[int]]):
    for name, value in counts.items():
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{name} must be a positive integer, got {value!r}")
    spec = _StageSettings.spec()
    gate = gate_cfg if gate_cfg is not None else spec.gate_cfg
    gate = replace(gate, **{k: v for k, v in counts.items() if v is not None})
    return _StageSettings(period if period is not None else spec.period,
                          exec_cfg if exec_cfg is not None else spec.exec_cfg,
                          guard_cfg if guard_cfg is not None else spec.guard_cfg, gate)


def _check_spec_run(
    spec_sha: str, settings: _StageSettings, selection: Mapping[str, Any], seed: int
) -> None:
    """Under the current spec sha256: spec settings only, and the selection's seed, so
    the one holdout run cannot be repeated with other seeds."""
    if spec_sha != spec_sha256():
        return
    fields = settings.non_spec_fields()
    if fields:
        raise ValueError("the current spec sha256 may only label a spec run; non-spec "
                         "settings: " + ", ".join(fields))
    recorded = selection.get("seed")
    if type(recorded) is not int or seed != recorded:
        raise ValueError(f"a spec run must use the selection's seed {recorded!r}, got {seed!r}")
    check_spec_seed(seed)


def check_calibration(path: Path, selection: Mapping[str, Any], calib: Calibration) -> None:
    """The holdout must run on the s* and latency sample the selection was made with."""
    if (selection.get("s_star_bps") != calib.s_star_bps
            or selection.get("n_latencies") != len(calib.latencies_ms)):
        raise HoldoutLockError(
            f"{path}: calibration differs from the selection (s* {selection.get('s_star_bps')} "
            f"vs {calib.s_star_bps}, latencies {selection.get('n_latencies')} vs "
            f"{len(calib.latencies_ms)})"
        )
    if selection.get("latencies_sha256") != latencies_sha256(calib.latencies_ms):
        raise HoldoutLockError(f"{path}: the latency sample differs from the selection's")


def _selection_sha256(path: Path, selection: Mapping[str, Any]) -> str:
    """sha256 of the selection file, refused if it changed after the lock read it."""
    raw = Path(path).read_bytes()
    try:
        same = json.loads(raw.decode("utf-8")) == selection
    except ValueError:
        same = False
    if not same:
        raise HoldoutLockError(f"{path}: selection file changed while the holdout started")
    return hashlib.sha256(raw).hexdigest()


def _check_holdout_dataset(dataset: Dataset, period: HoldoutPeriod) -> None:
    if dataset.loaded_until_ms < period.end_ms:
        raise ValueError(
            f"dataset loaded until {dataset.loaded_until_ms}, before the holdout end "
            f"{period.end_ms}"
        )
    for symbol in SYMBOLS:
        if symbol not in dataset.markets or symbol not in dataset.frames:
            raise ValueError(f"dataset lacks {symbol}")


def selection_folder(path: Path) -> Path:
    """The real folder of the selection file: symlinks resolved before the parent."""
    return Path(path).resolve().parent


def refuse_any_holdout(out_dir: Path, *more_dirs: Path) -> None:
    """The holdout runs once: any earlier holdout file, of any date, in ``out_dir`` or
    ``more_dirs`` (the selection file's real folder) refuses the run."""
    existing = sorted({p for d in (out_dir, *more_dirs)
                       for p in Path(d).glob(f"{HOLDOUT_PREFIX}*.json")})
    if existing:
        names = ", ".join(str(p) for p in existing)
        raise FileExistsError(
            f"holdout file already exists: {names}; the holdout runs once, so re-running "
            "means removing it deliberately"
        )


def _check_locked_run(path: Path, selection: Mapping[str, Any], out_dir: Path) -> None:
    """Under the current spec sha256: the code and Python that trained, a passing
    training C0 and the holdout file next to its selection file."""
    recorded, current = selection.get("code_sha256"), code_sha256()
    if recorded != current:
        raise HoldoutLockError(f"{path}: code sha256 {recorded!r} differs from the current "
                               f"code {current}; the holdout runs on the code that trained")
    trained_on, running = selection.get("python_version"), python_version()
    if trained_on != running:
        raise HoldoutLockError(f"{path}: Python {trained_on!r} trained, this is Python "
                               f"{running}; the holdout runs on the Python version that "
                               "trained (float sums can differ)")
    if selection.get("c0_training_pass") is not True:
        raise HoldoutLockError(f"{path}: fill model invalid on training C0; fix before test")
    if Path(out_dir).resolve() != selection_folder(path):
        raise HoldoutLockError(f"{out_dir}: a locked holdout file is written next to its "
                               f"selection file, in {selection_folder(path)}")


def check_ready(path: Path, selection: Mapping[str, Any], calib: Calibration,
                spec_sha: str, out_dir: Path) -> None:
    """Every check that needs no holdout data: no earlier holdout file, the selection's
    calibration and, under the current spec sha256, ``_check_locked_run``."""
    refuse_any_holdout(out_dir, selection_folder(path))
    check_calibration(path, selection, calib)
    if is_spec_sha(spec_sha):
        _check_locked_run(path, selection, out_dir)


def run_marker_path(data_dir: Path, spec_sha: str) -> Path:
    """``<data_dir>/holdout_runs/<spec_sha256>.json``: one holdout run per spec sha256,
    whatever the output or selection folder."""
    return Path(data_dir) / HOLDOUT_RUNS_DIR / f"{spec_sha}.json"


def run_started_path(data_dir: Path, spec_sha: str) -> Path:
    """``<data_dir>/holdout_runs/<spec_sha256>.started.json``: written right before the
    evaluation, so a marker without it is a failure before any holdout result."""
    return Path(data_dir) / HOLDOUT_RUNS_DIR / f"{spec_sha}.started.json"


def holdout_run_status(data_dir: Path, spec_sha: str) -> Dict[str, bool]:
    """Whether this spec's run marker and its evaluation-start file exist."""
    return {"marker": run_marker_path(data_dir, spec_sha).exists(),
            "started": run_started_path(data_dir, spec_sha).exists()}


def _mark_started(data_dir: Optional[Path], spec_sha: str) -> None:
    if data_dir is not None:
        write_json_exclusive(run_started_path(data_dir, spec_sha),
                             {"spec_sha256": spec_sha,
                              "created_at": datetime.now(timezone.utc).isoformat()},
                             "holdout evaluation start")


def _marker_refusal(path: Path) -> HoldoutLockError:
    return HoldoutLockError(f"{path}: this spec's holdout already started (run marker); the "
                            "holdout runs once, so re-running means removing it deliberately")


def claim_holdout_run(data_dir: Path, selection_path: Path, selection_sha: str,
                      spec_sha: str, out_dir: Path) -> Path:
    """Write the run marker exclusively before any holdout data is read; an existing
    marker refuses the run (``HoldoutLockError`` naming it)."""
    path = run_marker_path(data_dir, spec_sha)
    if path.exists():
        raise _marker_refusal(path)
    doc = {"spec_sha256": spec_sha, "selection_file": str(Path(selection_path).resolve()),
           "selection_sha256": selection_sha, "code_sha256": code_sha256(),
           "created_at": datetime.now(timezone.utc).isoformat(),
           "out_dir": str(Path(out_dir).resolve())}
    try:
        write_json_exclusive(path, doc, "holdout run marker")
    except FileExistsError:
        raise _marker_refusal(path) from None
    return path


def _check_marker_free(data_dir: Optional[Path], spec_sha: str) -> None:
    """Before any data is read: a spec run needs ``data_dir`` for its marker, and an
    existing marker refuses the run (``claim_holdout_run`` checks again on writing)."""
    if data_dir is None:
        if is_spec_sha(spec_sha):
            raise HoldoutLockError("a spec holdout run needs data_dir for its run marker "
                                   f"({HOLDOUT_RUNS_DIR}/<spec_sha256>.json)")
        return
    path = run_marker_path(data_dir, spec_sha)
    if path.exists():
        raise _marker_refusal(path)


def _claim(data_dir: Optional[Path], selection_path: Path, selection_sha: str,
           spec_sha: str, out_dir: Path) -> None:
    """The run marker; skipped for a test sha without ``data_dir``."""
    if data_dir is not None:
        claim_holdout_run(data_dir, selection_path, selection_sha, spec_sha, out_dir)


def _check_training_part(path: Path, selection: Mapping[str, Any], dataset: Dataset,
                         spec_sha: str) -> None:
    """Under the current spec sha256 the holdout data, cut at the training boundary,
    must be the data the selection was trained on."""
    if is_spec_sha(spec_sha) and dataset.train_part_sha256 != selection.get("train_data_sha256"):
        raise HoldoutLockError(f"{path}: the training part of the holdout data differs from "
                               "the data the selection was trained on")


def _check_period_stored(period: HoldoutPeriod) -> None:
    """A data source loads to the holdout end; a period past it cannot be replayed."""
    if period.end_ms > ms(HOLDOUT_END):
        raise ValueError(f"period ends at {period.end_ms}, after the holdout end "
                         f"{ms(HOLDOUT_END)} the stored data reach")


HoldoutData = Union[Dataset, DataSource]


def _check_data(data: HoldoutData, path: Path, selection: Mapping[str, Any],
                settings: "_StageSettings", spec_sha: str) -> None:
    """Every data-dependent refusal that can come before the run marker: a loaded
    dataset is checked whole; a source has its files checked (nothing computed) and,
    under the current spec sha256, its training-only load hashed against the
    selection's ``train_data_sha256``."""
    if isinstance(data, Dataset):
        _check_holdout_dataset(data, settings.period)
        _check_training_part(path, selection, data, spec_sha)
        return
    _check_period_stored(settings.period)
    data.check_files(None)
    if is_spec_sha(spec_sha):
        _check_training_part(path, selection, data.load(TRAIN_DATA_UNTIL_MS), spec_sha)


def _context(dataset: Dataset, settings: _StageSettings, calib: Calibration, seed: int) -> Holdout:
    period = settings.period
    return Holdout(
        frames=dataset.frames, markets=holdout_markets(dataset, settings.exec_cfg),
        period=period, calib=calib, seed=seed, exec_cfg=settings.exec_cfg,
        guard_cfg=settings.guard_cfg, gate_cfg=settings.gate_cfg,
        days=tuple(period.days()), last20_days=tuple(period.last20_days()),
        purge_tick_minutes=purge_tick_minutes(calib, settings.exec_cfg),
    )


def _provenance(
    dataset: Dataset, calib: Calibration, settings: _StageSettings, seed: int
) -> Dict[str, Any]:
    """What the holdout file records about its inputs besides the selection file."""
    return {
        "data_sha256": dataset.data_sha256,
        "data_content_sha256": dataset.train_data_sha256,
        "train_part_sha256": dataset.train_part_sha256,
        "code_sha256": code_sha256(),
        "python_version": python_version(),
        "data_loaded_until_ms": dataset.loaded_until_ms,
        "missing_warmups": list(dataset.missing_warmups),
        "seed": seed,
        "calibration": {"s_star_bps": calib.s_star_bps, "n_stops": calib.n_stops,
                        "n_latencies": len(calib.latencies_ms)},
        **settings.record(),
    }


def _start(data: HoldoutData, selection_path: Path, calib: Calibration, out_dir: Path,
           spec_sha: str, seed: int, settings_args: Tuple[Any, ...], data_dir: Optional[Path]
           ) -> Tuple[Dict[str, Any], _StageSettings, str]:
    """Every check that needs no holdout data, the data checks that can refuse before
    evaluation (``_check_data``), then the run marker; returns the selection, the
    settings and the selection file's sha256."""
    selection = load_selection_checked(selection_path, spec_sha)
    check_spec_sha(spec_sha)
    settings = _settings(*settings_args)
    _check_spec_run(spec_sha, settings, selection, seed)
    check_ready(selection_path, selection, calib, spec_sha, out_dir)
    check_purge_covers(calib, settings.exec_cfg)
    if is_spec_sha(spec_sha):
        check_recorded_purge(selection_path, selection, calib, settings.exec_cfg)
    permutation_ks(seed, 1, settings.period.length_days())  # fail early on a short holdout
    selection_sha = _selection_sha256(selection_path, selection)
    _check_marker_free(data_dir, spec_sha)
    _check_data(data, selection_path, selection, settings, spec_sha)
    _claim(data_dir, selection_path, selection_sha, spec_sha, out_dir)
    return selection, settings, selection_sha


def test_stage(
    dataset: HoldoutData,
    selection_path: Path,
    calib: Calibration,
    out_dir: Path,
    spec_sha: str,
    seed: int = 0,
    *,
    data_dir: Optional[Path] = None,
    period: Optional[HoldoutPeriod] = None,
    exec_cfg: Optional[ExecConfig] = None,
    guard_cfg: Optional[GuardConfig] = None,
    gate_cfg: Optional[GateConfig] = None,
    offsets: Optional[int] = None,
    permutation_shifts: Optional[int] = None,
    c1_seeds: Optional[int] = None,
    bootstrap_n: Optional[int] = None,
) -> Path:
    """Run the holdout once and write its file; returns the path.

    The lock comes first: nothing is read unless the selection file passes
    ``load_selection_checked`` and ``check_ready``. Then every data check that can
    refuse comes before the run marker ``<data_dir>/holdout_runs/<spec_sha256>.json``
    (mandatory under the current spec sha256; an existing marker refuses the run), so
    a refusal leaves no marker: for a ``DataSource`` the files are checked without
    computing anything and, under the current spec sha256, a training-only load must
    hash to the selection's ``train_data_sha256``; the full load comes after the
    marker. Injected settings and counts are for tests and are refused under the
    current spec sha256, as is a seed other than the selection's and the spec's. Any
    existing ``pine_gate_holdout_*.json`` in ``out_dir`` or next to the selection
    file refuses the run.
    """
    counts = {"offsets": offsets, "permutation_shifts": permutation_shifts,
              "c1_seeds": c1_seeds, "bootstrap_n": bootstrap_n}
    selection, settings, selection_sha = _start(
        dataset, selection_path, calib, out_dir, spec_sha, seed,
        (period, exec_cfg, guard_cfg, gate_cfg, counts), data_dir)
    if not isinstance(dataset, Dataset):
        dataset = dataset.load(None)
        _check_holdout_dataset(dataset, settings.period)
        _check_training_part(selection_path, selection, dataset, spec_sha)
    created = datetime.now(timezone.utc)
    path = Path(out_dir) / f"{HOLDOUT_PREFIX}{created.astimezone(WIB):%Y%m%d}.json"
    _mark_started(data_dir, spec_sha)  # the audit trail: evaluation begins now
    doc = _evaluate(_context(dataset, settings, calib, seed), selection)
    doc.update(_provenance(dataset, calib, settings, seed))
    doc.update({"spec_sha256": spec_sha, "selection_file": Path(selection_path).resolve().name,
                "selection_sha256": selection_sha, "created_at": created.isoformat()})
    # a concurrent run may have written in the meantime
    refuse_any_holdout(out_dir, selection_folder(selection_path))
    write_json_exclusive(path, doc, "holdout")
    return path
