"""Training stage and the holdout lock (spec 5.1, 5.3, 5.6, 5.8, 5.9 step 0).

Research code; the bot never imports it. Standard library only.

``load_dataset`` (in ``dataset``, importable from here) reads the stored data and
cuts every bar series at ``until_ms``. ``train_stage``
refuses a dataset loaded past the training end + 60 minutes, so no holdout bar
can reach a feature, a trade or the model. It simulates every rule with entry
ticks blocked in the ``purge_tick_minutes`` before each block end (purge), runs the
two-stage selection, fits contender 3's meta-model and writes a selection file
that records the spec sha256 and the purge length. The lock the test stage must pass
(``load_selection_checked``) and the run settings it binds live in ``lock``.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .bars import entry_index_for, index_closing_at, wib_day
from .calibrate import Calibration
from .dataset import SYMBOLS, TRAIN_DATA_UNTIL_MS, Dataset, load_dataset
from .features import FeatureFrame, Snapshot
from .fileio import exists_message, write_json_exclusive
from .holdout_gate import c0_side_ok
from .lock import (
    HoldoutLockError,
    MetaTrainer,
    RunSettings,
    Segments,
    TimeRange,
    check_spec_run,
    check_spec_sha,
    code_sha256,
    equal_ranges,
    is_spec_sha,
    latencies_sha256,
    load_selection_checked,
    python_version,
    run_settings,
)
from .metalabel import Candidate, candidate_features, train_meta
from .outcomes import OutcomeCache, gross_bps
from .rules import contender2_signal, rule_signal, trend_label
from .selection import (
    RuleResult,
    Selection,
    block_median_lb,
    eligible,
    rank_value,
    select,
    summarize,
)
from .simulate import Market, SignalFn, Trade, in_entry_window, simulate, trading_days
from .spec import (
    PURGE_MINUTES,
    TRENDS,
    WIB,
    ExecConfig,
    GuardConfig,
    ModelConfig,
    RuleSpec,
    SelectionConfig,
    stage1_rules,
)
from .stats import mean_gross_bps, spa_pvalue, trades_per_trading_day, win_rate

_MS_PER_MIN = 60_000
_MS_PER_HOUR = 60 * _MS_PER_MIN
_MS_PER_DAY = 24 * _MS_PER_HOUR
_BAR_5M_MS = 5 * _MS_PER_MIN

REGIME_MIN_N = 60
SELECTION_PREFIX = "pine_gate_selection_"
# HoldoutLockError, Segments, equal_ranges, latencies_sha256 and load_selection_checked
# live in ``lock``, SYMBOLS, TRAIN_DATA_UNTIL_MS, Dataset and load_dataset in
# ``dataset``; all stay importable from here.

# --------------------------------------------------------------------------- segments


def purge_tick_minutes(calib: Calibration, exec_cfg: ExecConfig) -> int:
    """The purge in TICK time, from the hash-locked latency sample (purge ruling).

    A trade from tick t fills by t + worst latency + order delay + one 1m bar and
    its time stop exits PURGE_MINUTES later, so the window is PURGE_MINUTES plus that
    lead, rounded up to whole minutes (an empty sample counts as 0 latency)."""
    lead_ms = (max(calib.latencies_ms, default=0.0) + exec_cfg.order_delay_s * 1000
               + _MS_PER_MIN)
    return PURGE_MINUTES + math.ceil(lead_ms / _MS_PER_MIN)


def purge_windows(segments: Segments, purge_minutes: int) -> Tuple[TimeRange, ...]:
    """Blocked entry-tick windows [B - purge_minutes, B) before every block end B."""
    purge = purge_minutes * _MS_PER_MIN
    return tuple((end - purge, end) for _, end in segments.block_bounds_ms)


def _in_any(t_ms: int, windows: Sequence[TimeRange]) -> bool:
    return any(a <= t_ms < b for a, b in windows)


# --------------------------------------------------------------------------- signals


def rule_signal_fn(rule: RuleSpec, frames: Mapping[str, FeatureFrame]) -> SignalFn:
    def signal(symbol: str, t: int) -> int:
        frame = frames[symbol]
        return rule_signal(rule, frame.snapshot(t), lambda: frame.poc_24h(t))

    return signal


def contender2_fn(frames: Mapping[str, FeatureFrame]) -> SignalFn:
    return lambda symbol, t: contender2_signal(frames[symbol].snapshot(t))


# --------------------------------------------------------------------------- regimes


def _trend_known(s: Snapshot, trend: str) -> bool:
    if trend == "E200":
        needed = (s.close_5m, s.ema200_5m)
    elif trend == "H1":
        needed = (s.h1_ema20, s.h1_ema50, s.h1_atr14)
    else:
        needed = (s.h4_ema20, s.h4_ema50, s.h4_atr14)
    return all(v is not None for v in needed)


def _five_minute_closes(start_ms: int, end_ms: int) -> range:
    first = -(-start_ms // _BAR_5M_MS) * _BAR_5M_MS
    return range(first, end_ms, _BAR_5M_MS)


def _regime_cell(counts: Mapping[str, int]) -> Dict[str, Any]:
    n = counts["up"] + counts["down"] + counts["neutral"]
    cell: Dict[str, Any] = dict(counts)
    cell["n"] = n
    for name in ("up", "down", "neutral"):
        cell[f"share_{name}"] = counts[name] / n if n else None
    cell["insufficient"] = n < REGIME_MIN_N
    return cell


def regime_counts(
    frames: Mapping[str, FeatureFrame], start_ms: int, end_ms: int
) -> Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]:
    """Step 0: up/down/neutral counts and shares per symbol, trend source and WIB month.

    Sampled at every 5m bar close in [start, end). A label whose inputs are not yet
    valid counts as ``unknown`` and is left out of ``n``; n < 60 is "insufficient".
    """
    names = {1: "up", -1: "down", 0: "neutral"}
    out: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for symbol, frame in frames.items():
        raw: Dict[str, Dict[str, Dict[str, int]]] = {t: {} for t in TRENDS}
        for c in _five_minute_closes(start_ms, end_ms):
            s = frame.snapshot(c)
            month = wib_day(c).strftime("%Y-%m")
            for trend in TRENDS:
                counts = raw[trend].setdefault(
                    month, {"up": 0, "down": 0, "neutral": 0, "unknown": 0}
                )
                key = names[trend_label(s, trend)] if _trend_known(s, trend) else "unknown"
                counts[key] += 1
        out[symbol] = {
            trend: {m: _regime_cell(c) for m, c in sorted(months.items())}
            for trend, months in raw.items()
        }
    return out


# --------------------------------------------------------------------------- training


def simulate_training(
    signal: SignalFn,
    markets: Sequence[Market],
    segments: Segments,
    calib: Calibration,
    *,
    seed: int,
    exec_cfg: ExecConfig,
    guard_cfg: GuardConfig,
    start_ms: Optional[int] = None,
) -> List[Trade]:
    """The one-position replay over training with the block-end purge
    (``purge_tick_minutes`` of ``calib`` and ``exec_cfg``)."""
    return simulate(
        signal,
        markets,
        segments.train_start_ms if start_ms is None else start_ms,
        segments.train_end_ms,
        exec_cfg=exec_cfg,
        guard_cfg=guard_cfg,
        latencies_ms=calib.latencies_ms,
        seed=seed,
        overshoot_bps=calib.s_star_bps,
        offset_s=0,
        blocked_entry_windows=purge_windows(segments, purge_tick_minutes(calib, exec_cfg)),
    )


@dataclass(frozen=True)
class _Context:
    frames: Mapping[str, FeatureFrame]
    markets: Tuple[Market, ...]  # SYMBOLS order
    segments: Segments
    calib: Calibration
    seed: int
    exec_cfg: ExecConfig
    guard_cfg: GuardConfig
    sel_cfg: SelectionConfig
    model_cfg: ModelConfig

    def run(self, signal: SignalFn, start_ms: Optional[int] = None,
            markets: Optional[Sequence[Market]] = None) -> List[Trade]:
        return simulate_training(
            signal, self.markets if markets is None else markets, self.segments, self.calib,
            seed=self.seed, exec_cfg=self.exec_cfg, guard_cfg=self.guard_cfg, start_ms=start_ms,
        )

    def train_days(self) -> List[date]:
        return trading_days(self.segments.train_start_ms, self.segments.train_end_ms)


def _check_dataset(dataset: Dataset, segments: Segments) -> None:
    """Refuse any dataset that holds a bar closing after training end + 1 hour."""
    limit = segments.train_end_ms + _MS_PER_HOUR
    if dataset.loaded_until_ms > limit:
        raise ValueError(
            f"dataset loaded until {dataset.loaded_until_ms}, past the training limit {limit}"
        )
    for symbol in SYMBOLS:
        if symbol not in dataset.markets or symbol not in dataset.frames:
            raise ValueError(f"dataset lacks {symbol}")
        for bars in (dataset.markets[symbol].bars_1m, *dataset.frames[symbol].series()):
            if len(bars) and bars.open_ms[-1] + bars.interval_ms > limit:
                raise ValueError(
                    f"{symbol} holds a {bars.interval_ms // _MS_PER_MIN}-minute bar closing "
                    f"after the training limit {limit}"
                )


def _check_warmups(spec_sha: str, dataset: Dataset) -> None:
    """A spec run needs every 1h/4h warm-up: without one, the H4 EMA50 stays ``None``
    for weeks of training (spec 5.2)."""
    if is_spec_sha(spec_sha) and dataset.missing_warmups:
        raise ValueError("a spec run needs every 1h/4h warm-up; missing or unused: "
                         + ", ".join(dataset.missing_warmups))


def check_purge_covers(calib: Calibration, exec_cfg: ExecConfig) -> None:
    """The purge must outlast the longest trade a tick can open (worst latency, the
    order delay, the fill bar and the time stop), or a trade could cross a block end.

    ``purge_tick_minutes`` covers any latency by construction; this asserts that
    invariant, which only a time stop longer than PURGE_MINUTES can break."""
    purge = purge_tick_minutes(calib, exec_cfg)
    worst_latency = max(calib.latencies_ms, default=0.0)
    longest_ms = (worst_latency + exec_cfg.order_delay_s * 1000 + _MS_PER_MIN
                  + exec_cfg.time_stop_min * _MS_PER_MIN)
    if longest_ms > purge * _MS_PER_MIN:
        raise ValueError(
            f"a trade can last {longest_ms / _MS_PER_MIN:.1f} min from its tick, longer than "
            f"the {purge}-minute purge"
        )


def check_recorded_purge(path: Path, selection: Mapping[str, Any], calib: Calibration,
                         exec_cfg: ExecConfig) -> None:
    """The holdout must purge as training did: the selection's ``purge_tick_minutes``
    must equal the value the (hash-locked) calibration gives."""
    recorded, derived = selection.get("purge_tick_minutes"), purge_tick_minutes(calib, exec_cfg)
    if type(recorded) is not int or recorded != derived:
        raise HoldoutLockError(f"{path}: the selection's purge_tick_minutes {recorded!r} "
                               f"differs from the calibration's {derived}")


def fresh_markets(dataset: Dataset, exec_cfg: ExecConfig) -> Tuple[Market, ...]:
    """Both markets in tick order with fresh outcome caches on ``exec_cfg``."""
    return tuple(
        Market(m.symbol, m.bars_1m, OutcomeCache(m.bars_1m, exec_cfg), m.funding_ms)
        for m in (dataset.markets[s] for s in SYMBOLS)
    )


def _evaluator(ctx: _Context) -> Callable[[RuleSpec], RuleResult]:
    seg = ctx.segments
    block_days = [trading_days(a, b) for a, b in seg.block_bounds_ms]
    days = ctx.train_days()

    def evaluate(rule: RuleSpec) -> RuleResult:
        trades = ctx.run(rule_signal_fn(rule, ctx.frames))
        return summarize(
            rule, trades, seg.block_bounds_ms, seg.cscv_bounds_ms, block_days, days,
            z=ctx.sel_cfg.z,
        )

    return evaluate


def direction_series(
    trades: Sequence[Trade],
    markets: Sequence[Market],
    days: Sequence[date],
    *,
    seed: int,
    overshoot_bps: float,
    exec_cfg: ExecConfig,
) -> List[float]:
    """The value of a rule's direction per listed day (the SPA benchmark, final ruling).

    For each trade, in order, a seeded random side (``random.Random(seed)``, one draw
    per trade, as C1) is bracketed from the same entry bar through the market's
    outcome cache; the day's value is the mean over its trades of (rule gross bps -
    random-side gross bps). A day without trades is 0.0 for both sides, so 0.0; a
    trade whose random-side bracket is unresolved is left out of its day."""
    rng = random.Random(seed)
    by_symbol = {m.symbol: m for m in markets}
    by_day: Dict[date, List[float]] = {}
    for t in trades:
        market = by_symbol[t.symbol]
        idx = index_closing_at(market.bars_1m, t.entry_ms)
        if idx is None:
            raise ValueError(f"{t.symbol}: no 1m bar closes at {t.entry_ms}")
        side = rng.choice((1, -1))
        other = market.outcomes.get(idx, side)
        if other is not None:
            diff = t.gross_bps - gross_bps(other, side, overshoot_bps, exec_cfg)
            by_day.setdefault(t.wib_day, []).append(diff)
    return [statistics.fmean(by_day[d]) if d in by_day else 0.0 for d in days]


def _spa(ctx: _Context, results: Mapping[str, RuleResult], spa_n: int) -> float:
    """Hansen SPA over every evaluated rule of its direction value against a random
    side on its own entries (``direction_series``)."""
    days = ctx.train_days()
    diff = {key: direction_series(r.trades, ctx.markets, days, seed=ctx.seed,
                                  overshoot_bps=ctx.calib.s_star_bps, exec_cfg=ctx.exec_cfg)
            for key, r in results.items()}
    return spa_pvalue(diff, n=spa_n, seed=ctx.seed)


def c0_training(ctx: _Context) -> Dict[str, Any]:
    """Always long and always short per symbol on training (spec 5.9, final ruling).

    Same clock and settings as the rule runs (offset 0, purge), each symbol on its own
    clock as in the holdout. ``c0_training_pass`` uses criterion 9's tolerances; a
    real holdout run refuses to start when it is false."""
    cells: Dict[str, Dict[str, Dict[str, Any]]] = {}
    passed = True
    for market in ctx.markets:
        cells[market.symbol] = {}
        for name, side in (("long", 1), ("short", -1)):
            trades = ctx.run(lambda symbol, t, side=side: side, markets=(market,))
            cell = {"n": len(trades), "wr": win_rate(trades), "gross_bps": mean_gross_bps(trades)}
            cells[market.symbol][name] = cell
            passed = passed and c0_side_ok(cell["wr"], cell["gross_bps"])
    return {"c0_training": cells, "c0_training_pass": passed}


def _fallback_order(index: int, r: RuleResult, cfg: SelectionConfig) -> Optional[Tuple]:
    """Sort key of a Stage-1 rule when no rule ranks: the median block lower bound
    without the vote (none last), then pooled WR, then Stage-1 order; ``None`` for a
    rule without any TP+SL."""
    median = block_median_lb(r, cfg)
    has_median = math.isfinite(median)
    if not has_median and r.wr is None:
        return None
    return (not has_median, -median if has_median else 0.0, r.wr is None, -(r.wr or 0.0), index)


def _primary_rule(sel: Selection, cfg: SelectionConfig) -> Optional[RuleSpec]:
    """Trend and entry of contender 1, else of the best-ranked Stage-1 rule, else (every
    Stage-1 rule ranks -inf) of the first rule in ``_fallback_order``, with strength off
    and no filters (ruling: the fallback is a primary rule too). ``None`` only when no
    Stage-1 rule has any TP+SL."""
    if sel.choice is not None:
        return RuleSpec(sel.choice.trend, sel.choice.entry, "OFF")
    best: Optional[RuleSpec] = None
    best_value = -math.inf
    for rule in stage1_rules():  # strict > keeps the earlier (simpler) rule on ties
        value = rank_value(sel.results[rule.key()], cfg)
        if value > best_value:
            best, best_value = rule, value
    if best is None:
        keyed = [(_fallback_order(i, sel.results[rule.key()], cfg), rule)
                 for i, rule in enumerate(stage1_rules())]
        keyed = [(key, rule) for key, rule in keyed if key is not None]
        best = min(keyed, key=lambda kr: kr[0])[1] if keyed else None
    return None if best is None else RuleSpec(best.trend, best.entry, "OFF")


# --------------------------------------------------------------------------- contender 3


def _block_of(t_ms: int, segments: Segments) -> int:
    for i, (a, b) in enumerate(segments.block_bounds_ms):
        if a <= t_ms < b:
            return i
    raise ValueError(f"{t_ms} lies outside every training block")


def _candidate(
    ctx: _Context, market: Market, primary: RuleSpec, c: int, delay_ms: int
) -> Optional[Candidate]:
    """The primary rule's candidate at 5m close ``c``; None when it does not signal."""
    frame = ctx.frames[market.symbol]
    s = frame.snapshot(c)
    side = rule_signal(primary, s, lambda: frame.poc_24h(c))
    if side == 0:
        return None
    x = candidate_features(s, side, primary.trend, frame.poc_24h(c), c)
    if x is None:
        return None
    open_ms = market.bars_1m.open_ms
    idx = entry_index_for(market.bars_1m, c + delay_ms)
    if idx is None:
        return None
    o = market.outcomes.get(idx, side)
    if o is None:
        return None
    label = 1 if gross_bps(o, side, ctx.calib.s_star_bps, ctx.exec_cfg) > 0 else 0
    end_ms = open_ms[o.exit_idx] + (0 if o.gapped else _MS_PER_MIN)
    return Candidate(market.symbol, c, side, x, label, open_ms[idx] + _MS_PER_MIN, end_ms,
                     _block_of(c, ctx.segments))


def meta_candidates(ctx: _Context, primary: RuleSpec) -> List[Candidate]:
    """Every training 5m close in the entry window, outside the purge, where the
    primary rule signals; bracket from c + median latency + the order delay."""
    latencies = ctx.calib.latencies_ms
    latency = int(round(statistics.median(latencies))) if latencies else 0
    delay = latency + ctx.exec_cfg.order_delay_s * 1000
    windows = purge_windows(ctx.segments, purge_tick_minutes(ctx.calib, ctx.exec_cfg))
    out: List[Candidate] = []
    for market in ctx.markets:
        for c in _five_minute_closes(ctx.segments.train_start_ms, ctx.segments.train_end_ms):
            if not in_entry_window(c) or _in_any(c, windows):
                continue
            cand = _candidate(ctx, market, primary, c, delay)
            if cand is not None:
                out.append(cand)
    return out


def _supply_fn(
    ctx: _Context, candidates: Sequence[Candidate]
) -> Callable[[Mapping[Tuple[str, int], float], float], float]:
    """Trades per B2-B4 trading day when only candidates with OOF prob >= p enter."""
    sides = {(c.symbol, c.t_ms): c.side for c in candidates}
    start = ctx.segments.block_bounds_ms[1][0]
    days = trading_days(start, ctx.segments.train_end_ms)

    def supply_for(oof: Mapping[Tuple[str, int], float], p: float) -> float:
        def signal(symbol: str, t: int) -> int:
            key = (symbol, t // _BAR_5M_MS * _BAR_5M_MS)
            return sides[key] if key in sides and oof.get(key, 0.0) >= p else 0

        return trades_per_trading_day(ctx.run(signal, start_ms=start), days)

    return supply_for


def _contender3(
    ctx: _Context, primary: Optional[RuleSpec], trainer: MetaTrainer
) -> Dict[str, Any]:
    empty = {"meta_model": None, "cv_logloss": None, "cv_fold_sizes": None, "chosen_C": None,
             "reliability": None, "n_oof": 0, "n_candidates": 0,
             "supply_oof_at_threshold": None, "supply_final_at_threshold": None,
             "threshold_admits_all": None, "threshold_supply_met": None}
    if primary is None:
        return empty
    candidates = meta_candidates(ctx, primary)
    model, diag = trainer(candidates, _supply_fn(ctx, candidates), ctx.model_cfg)
    return {
        "meta_model": model.to_dict(),
        "cv_logloss": {str(C): list(v) for C, v in diag["cv_logloss"].items()},
        "cv_fold_sizes": diag.get("fold_sizes"),
        "chosen_C": diag["chosen_C"],
        "reliability": [list(row) for row in diag["reliability"]],
        "n_oof": len(diag["oof"]),
        "n_candidates": len(candidates),
        # Trades per B2-B4 day at the threshold: validated (out-of-fold) and the
        # final refit's own (no labels), to show whether the refit keeps the supply.
        "supply_oof_at_threshold": diag.get("supply_oof_at_threshold"),
        "supply_final_at_threshold": diag.get("supply_final_at_threshold"),
        # p* at the minimum OOF probability (no percentile met the supply floor):
        # contender 3 then admits every candidate of its primary rule
        "threshold_admits_all": diag.get("threshold_admits_all"),
        "threshold_supply_met": diag.get("threshold_supply_met"),
    }


# --------------------------------------------------------------------------- selection file


def _finite(value: Optional[float]) -> Optional[float]:
    return value if value is not None and math.isfinite(value) else None


def _rule_metrics(r: RuleResult, cfg: SelectionConfig) -> Dict[str, Any]:
    return {
        "n_trades": len(r.trades),
        "n_tp_sl": r.n_tp_sl,
        "supply_per_day": r.supply_per_day,
        "wr": r.wr,
        "lower_bound": r.lower_bound,
        "block_wr": list(r.block_wr),
        "block_lb": list(r.block_lb),
        "block_counts": [list(c) for c in r.block_counts],
        "cscv_counts": [list(c) for c in r.cscv_counts],
        "rank_value": _finite(rank_value(r, cfg)),
        "mean_gross_bps": mean_gross_bps(r.trades),
        "eligible": eligible(r, cfg),
    }


def pbo_record(pbo: Optional[float], cfg: SelectionConfig) -> Dict[str, Any]:
    """PBO, its pass (< ``pbo_max``) and, when undefined, why: criterion 9 then fails
    for contenders 1 and 3."""
    if pbo is None:
        return {"pbo": None, "pbo_pass": False,
                "pbo_reason": f"PBO undefined: fewer than two rules with at least "
                              f"{cfg.min_tp_sl} TP+SL trades"}
    return {"pbo": pbo, "pbo_pass": pbo < cfg.pbo_max, "pbo_reason": None}


def _selection_document(
    ctx: _Context, sel: Selection, primary: Optional[RuleSpec], extra: Mapping[str, Any]
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "contender1": sel.choice.key() if sel.choice is not None else None,
        "top": [r.key() for r in sel.top],
        "stable": sel.stable,
        **pbo_record(sel.pbo, ctx.sel_cfg),
        "supply_limited": sel.supply_limited,
        "primary_rule": primary.key() if primary is not None else None,
        "s_star_bps": ctx.calib.s_star_bps,
        "n_stops": ctx.calib.n_stops,
        "n_latencies": len(ctx.calib.latencies_ms),
        "latencies_sha256": latencies_sha256(ctx.calib.latencies_ms),
        "purge_tick_minutes": purge_tick_minutes(ctx.calib, ctx.exec_cfg),
        "seed": ctx.seed,
        "rules": {k: _rule_metrics(r, ctx.sel_cfg) for k, r in sorted(sel.results.items())},
    }
    doc.update(extra)
    return doc


def train_stage(
    dataset: Dataset,
    calib: Calibration,
    out_dir: Path,
    spec_sha: str,
    seed: int = 0,
    *,
    segments: Optional[Segments] = None,
    exec_cfg: Optional[ExecConfig] = None,
    guard_cfg: Optional[GuardConfig] = None,
    sel_cfg: Optional[SelectionConfig] = None,
    model_cfg: Optional[ModelConfig] = None,
    spa_n: int = 2000,
    meta_trainer: MetaTrainer = train_meta,
) -> Path:
    """Select contender 1, fit contender 3 and write the selection file; returns its path.

    Injected segments, configs or ``meta_trainer`` are for tests: they are recorded
    in the file, refused under the current spec sha256, and the holdout lock refuses
    their file. ``train_meta`` validates on the dates of ``spec.BLOCKS``, so injected
    segments also need an injected ``meta_trainer``. An existing selection file of
    the same day is never overwritten (``FileExistsError``).
    """
    check_spec_sha(spec_sha)
    settings = run_settings(segments, exec_cfg, guard_cfg, sel_cfg, model_cfg, meta_trainer)
    check_spec_run(spec_sha, settings, seed)
    _check_warmups(spec_sha, dataset)
    _check_dataset(dataset, settings.segments)
    check_purge_covers(calib, settings.exec_cfg)
    code_sha = code_sha256()  # the code that runs, taken before the long run
    created = datetime.now(timezone.utc)
    path = Path(out_dir) / f"{SELECTION_PREFIX}{created.astimezone(WIB):%Y%m%d}.json"
    if path.exists():  # fail before hours of work; the final write checks again
        raise FileExistsError(exists_message(path, "selection"))
    ctx = _context(dataset, calib, seed, settings)
    sel = select(_evaluator(ctx), ctx.sel_cfg)
    primary = _primary_rule(sel, ctx.sel_cfg)
    extra = {
        "spec_sha256": spec_sha, "created_at": created.isoformat(),
        "code_sha256": code_sha, "python_version": python_version(),
        **_data_record(dataset), **c0_training(ctx),
        "spa_p": _spa(ctx, sel.results, spa_n), "spa_n": spa_n,
        "regime_counts": regime_counts(ctx.frames, ctx.segments.train_start_ms,
                                       ctx.segments.train_end_ms),
        **settings.record(),
        **_contender3(ctx, primary, settings.meta_trainer),
    }
    write_json_exclusive(path, _selection_document(ctx, sel, primary, extra), "selection")
    return path


def _context(dataset: Dataset, calib: Calibration, seed: int, settings: RunSettings) -> _Context:
    return _Context(
        frames=dataset.frames, markets=fresh_markets(dataset, settings.exec_cfg),
        segments=settings.segments, calib=calib, seed=seed, exec_cfg=settings.exec_cfg,
        guard_cfg=settings.guard_cfg, sel_cfg=settings.sel_cfg, model_cfg=settings.model_cfg,
    )


def _data_record(dataset: Dataset) -> Dict[str, Any]:
    return {
        "data_sha256": dataset.data_sha256,
        "train_data_sha256": dataset.train_data_sha256,
        "data_loaded_until_ms": dataset.loaded_until_ms,
        "missing_warmups": list(dataset.missing_warmups),
    }
