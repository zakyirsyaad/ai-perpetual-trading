"""The one-shot holdout test stage (spec 5.9, 5.10, 5.11).

Research code; the bot never imports it. Standard library only.

``test_stage`` checks the holdout lock first (``load_selection_checked``: the
selection file must exist and carry the current spec sha256 of a spec run) and
that the calibration is the one the selection was made with; only then does it
read the holdout. It replays the holdout [2026-08-05, 2026-10-14) WIB with entry
ticks blocked in the final 65 minutes, runs contenders 1-3 and the controls,
applies the nine gate criteria per symbol, chooses what moves on and writes
``pine_gate_holdout_<YYYYMMDD>.json``. The holdout runs once: any existing
holdout file in ``out_dir`` or next to the selection file, of any date, refuses
the run. A run under the current spec sha256 must also use the seed recorded in
the selection file, run on the code that trained (``code_sha256``), follow a
training C0 that passed, write next to its selection file and read holdout data
whose training part is the data the selection was trained on.

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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
    check_spec_sha,
    code_sha256,
    is_spec_sha,
    latencies_sha256,
    load_selection_checked,
)
from .outcomes import OutcomeCache
from .pipeline import SYMBOLS, Dataset, check_purge_covers
from .rules import reference_signal
from .simulate import Market, SignalFn, Trade
from .spec import WIB, ExecConfig, GateConfig, GuardConfig, spec_sha256
from .stats import bootstrap_paired_p, day_aggregates, holm, permutation_p, win_rate, wr_lower_bound

HOLDOUT_PREFIX = "pine_gate_holdout_"
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


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear interpolation between order statistics of ``values``, which must be
    sorted ascending; ``None`` for no values."""
    if not values:
        return None
    pos = q * (len(values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (pos - lo) * (values[hi] - values[lo])


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

    trades = h.run(signal, markets=markets, exec_cfg=cfg, latencies_ms=())
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
        "sensitivities": _sensitivities(h, contenders),
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


def refuse_any_holdout(out_dir: Path, *more_dirs: Path) -> None:
    """The holdout runs once: any earlier holdout file, of any date, in ``out_dir`` or
    ``more_dirs`` (the selection file's folder) refuses the run."""
    existing = sorted({p for d in (out_dir, *more_dirs)
                       for p in Path(d).glob(f"{HOLDOUT_PREFIX}*.json")})
    if existing:
        names = ", ".join(str(p) for p in existing)
        raise FileExistsError(
            f"holdout file already exists: {names}; the holdout runs once, so re-running "
            "means removing it deliberately"
        )


def _check_locked_run(path: Path, selection: Mapping[str, Any], out_dir: Path) -> None:
    """Under the current spec sha256: the code that trained, a passing training C0 and
    the holdout file next to its selection file."""
    recorded, current = selection.get("code_sha256"), code_sha256()
    if recorded != current:
        raise HoldoutLockError(f"{path}: code sha256 {recorded!r} differs from the current "
                               f"code {current}; the holdout runs on the code that trained")
    if selection.get("c0_training_pass") is not True:
        raise HoldoutLockError(f"{path}: fill model invalid on training C0; fix before test")
    if Path(out_dir).resolve() != Path(path).parent.resolve():
        raise HoldoutLockError(f"{out_dir}: a locked holdout file is written next to its "
                               f"selection file, in {Path(path).parent}")


def check_ready(path: Path, selection: Mapping[str, Any], calib: Calibration,
                spec_sha: str, out_dir: Path) -> None:
    """Every check that needs no holdout data: no earlier holdout file, the selection's
    calibration and, under the current spec sha256, ``_check_locked_run``."""
    refuse_any_holdout(out_dir, Path(path).parent)
    check_calibration(path, selection, calib)
    if is_spec_sha(spec_sha):
        _check_locked_run(path, selection, out_dir)


def _check_training_part(path: Path, selection: Mapping[str, Any], dataset: Dataset,
                         spec_sha: str) -> None:
    """Under the current spec sha256 the holdout data, cut at the training boundary,
    must be the data the selection was trained on."""
    if is_spec_sha(spec_sha) and dataset.train_part_sha256 != selection.get("train_data_sha256"):
        raise HoldoutLockError(f"{path}: the training part of the holdout data differs from "
                               "the data the selection was trained on")


def _context(dataset: Dataset, settings: _StageSettings, calib: Calibration, seed: int) -> Holdout:
    period = settings.period
    return Holdout(
        frames=dataset.frames, markets=holdout_markets(dataset, settings.exec_cfg),
        period=period, calib=calib, seed=seed, exec_cfg=settings.exec_cfg,
        guard_cfg=settings.guard_cfg, gate_cfg=settings.gate_cfg,
        days=tuple(period.days()), last20_days=tuple(period.last20_days()),
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
        "data_loaded_until_ms": dataset.loaded_until_ms,
        "missing_warmups": list(dataset.missing_warmups),
        "seed": seed,
        "calibration": {"s_star_bps": calib.s_star_bps, "n_stops": calib.n_stops,
                        "n_latencies": len(calib.latencies_ms)},
        **settings.record(),
    }


def test_stage(
    dataset: Dataset,
    selection_path: Path,
    calib: Calibration,
    out_dir: Path,
    spec_sha: str,
    seed: int = 0,
    *,
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

    The lock comes first: nothing of the holdout is read unless the selection file
    passes ``load_selection_checked`` and ``check_ready``. Injected settings and
    counts are for tests and are refused under the current spec sha256, as is a
    seed other than the selection's. Any existing ``pine_gate_holdout_*.json`` in
    ``out_dir`` or next to the selection file refuses the run.
    """
    selection = load_selection_checked(selection_path, spec_sha)
    check_spec_sha(spec_sha)
    counts = {"offsets": offsets, "permutation_shifts": permutation_shifts,
              "c1_seeds": c1_seeds, "bootstrap_n": bootstrap_n}
    settings = _settings(period, exec_cfg, guard_cfg, gate_cfg, counts)
    _check_spec_run(spec_sha, settings, selection, seed)
    check_ready(selection_path, selection, calib, spec_sha, out_dir)
    _check_holdout_dataset(dataset, settings.period)
    _check_training_part(selection_path, selection, dataset, spec_sha)
    check_purge_covers(calib, settings.exec_cfg)
    permutation_ks(seed, 1, settings.period.length_days())  # fail early on a short holdout
    created = datetime.now(timezone.utc)
    path = Path(out_dir) / f"{HOLDOUT_PREFIX}{created.astimezone(WIB):%Y%m%d}.json"
    selection_sha = _selection_sha256(selection_path, selection)
    doc = _evaluate(_context(dataset, settings, calib, seed), selection)
    doc.update(_provenance(dataset, calib, settings, seed))
    doc.update({"spec_sha256": spec_sha, "selection_file": Path(selection_path).name,
                "selection_sha256": selection_sha, "created_at": created.isoformat()})
    refuse_any_holdout(out_dir, Path(selection_path).parent)  # a concurrent run may have written
    write_json_exclusive(path, doc, "holdout")
    return path
