"""Training stage and the holdout lock (spec 5.1, 5.3, 5.6, 5.8, 5.9 step 0).

Research code; the bot never imports it. Standard library only.

``load_dataset`` reads production 1m day files, the native 1h/4h warm-up and
the funding times, and cuts every bar series at ``until_ms``. ``train_stage``
refuses a dataset loaded past the training end + 60 minutes, so no holdout bar
can reach a feature, a trade or the model. It simulates every rule with entry
ticks blocked in the 65 minutes before each block end (purge), runs the
two-stage selection, fits contender 3's meta-model and writes a selection file
that records the spec sha256. The lock the test stage must pass
(``load_selection_checked``) and the run settings it binds live in ``lock``.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import sys
from array import array
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .bars import (
    Bars,
    bars_from_rows,
    day_path,
    entry_index_for,
    index_closing_at,
    funding_path,
    interval_to_ms,
    load_days,
    read_day,
    slice_until,
    warmup_path,
    wib_day,
    wib_day_start_ms,
)
from .calibrate import Calibration
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
    latencies_sha256,
    load_selection_checked,
    run_settings,
)
from .metalabel import Candidate, candidate_features, train_meta
from .outcomes import OutcomeCache, gross_bps
from .rules import contender2_signal, rule_signal, trend_label
from .selection import RuleResult, Selection, eligible, rank_value, select, summarize
from .simulate import Market, SignalFn, Trade, in_entry_window, simulate, trading_days
from .spec import (
    HOLDOUT_END,
    PURGE_MINUTES,
    TRAIN_END,
    TRAIN_START,
    TRENDS,
    WIB,
    ExecConfig,
    GuardConfig,
    ModelConfig,
    RuleSpec,
    SelectionConfig,
    ms,
    stage1_rules,
)
from .stats import mean_gross_bps, spa_pvalue, trades_per_trading_day, win_rate

SYMBOLS: Tuple[str, ...] = ("BTC-USDT", "ETH-USDT")  # tick order: BTC first

_MS_PER_MIN = 60_000
_MS_PER_HOUR = 60 * _MS_PER_MIN
_MS_PER_DAY = 24 * _MS_PER_HOUR
_BAR_5M_MS = 5 * _MS_PER_MIN

# Outcomes may resolve up to 60 minutes after the training end; nothing later is loaded.
TRAIN_DATA_UNTIL_MS = ms(TRAIN_END) + _MS_PER_HOUR
# Task 8 ruling: the purge gates the TICK time, so it is 5 minutes longer than
# PURGE_MINUTES; a tick at B - 65 min fills by about B - 63 min and its 60-minute
# time stop exits before B.
PURGE_TICK_MINUTES = PURGE_MINUTES + 5
REGIME_MIN_N = 60
SELECTION_PREFIX = "pine_gate_selection_"
_WARMUP_INTERVALS = ("1h", "4h")
# HoldoutLockError, Segments, equal_ranges, latencies_sha256 and load_selection_checked
# live in ``lock`` and stay importable from here.

# --------------------------------------------------------------------------- segments


def purge_windows(segments: Segments) -> Tuple[TimeRange, ...]:
    """Blocked entry-tick windows [B - 65 min, B) before every block end B."""
    purge = PURGE_TICK_MINUTES * _MS_PER_MIN
    return tuple((end - purge, end) for _, end in segments.block_bounds_ms)


def _in_any(t_ms: int, windows: Sequence[TimeRange]) -> bool:
    return any(a <= t_ms < b for a, b in windows)


# --------------------------------------------------------------------------- loading


@dataclass(frozen=True)
class Dataset:
    frames: Mapping[str, FeatureFrame]
    markets: Mapping[str, Market]
    data_sha256: str  # sha256 over the sorted "relative_path:sha256" of every file read
    loaded_until_ms: int  # nothing at or after this time was kept
    missing_warmups: Tuple[str, ...] = ()
    # sha256 over the arrays actually kept after the cut: 1m bars, warm-ups, funding times
    train_data_sha256: str = ""
    # the same hash over those arrays cut at TRAIN_DATA_UNTIL_MS: for a training load it
    # equals train_data_sha256; the holdout compares it with the selection's (spec 5.1)
    train_part_sha256: str = ""


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_line(data_dir: Path, path: Path) -> str:
    return f"{Path(path).relative_to(data_dir).as_posix()}:{_file_sha256(path)}"


_BAR_FIELDS = ("open_ms", "open", "high", "low", "close", "volume")


def _array_bytes(values: array) -> bytes:
    """Little-endian bytes of an array, whatever the platform."""
    if sys.byteorder == "little":
        return values.tobytes()
    swapped = array(values.typecode, values)
    swapped.byteswap()
    return swapped.tobytes()


def _content_sha256(
    bars: Bars, warmups: Mapping[str, Optional[Bars]], funding: Sequence[int]
) -> str:
    """sha256 over one symbol's kept arrays, each section labelled with its length."""
    h = hashlib.sha256()
    sections = [("1m", bars)] + [(k, warmups[k]) for k in _WARMUP_INTERVALS]
    for label, part in sections:
        if part is None:
            h.update(f"{label}:absent\n".encode("ascii"))
            continue
        for field in _BAR_FIELDS:
            values = getattr(part, field)
            h.update(f"{label}/{field}:{len(values)}\n".encode("ascii"))
            h.update(_array_bytes(values))
    h.update(f"funding:{len(funding)}\n".encode("ascii"))
    h.update(_array_bytes(array("q", funding)))
    return h.hexdigest()


def _read_warmup(path: Path, interval: str, until_ms: Optional[int]) -> Bars:
    bars = bars_from_rows(read_day(path), interval_to_ms(interval))
    return bars if until_ms is None else slice_until(bars, until_ms)


def _read_funding(path: Path, until_ms: Optional[int]) -> Tuple[int, ...]:
    """Funding times from JSON (required); kept below ``until_ms``.

    A tick never runs at or after ``until_ms``, so a later funding time can never
    start a blackout."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or any(type(t) is not int for t in raw):
        raise ValueError(f"{path}: expected a JSON list of integer milliseconds")
    return tuple(sorted(t for t in raw if until_ms is None or t < until_ms))


def _training_part_sha256(
    bars: Bars, warmups: Mapping[str, Optional[Bars]], funding: Sequence[int]
) -> str:
    """``_content_sha256`` of the arrays cut as a training load cuts them."""
    cut = TRAIN_DATA_UNTIL_MS
    part = {k: None if w is None else slice_until(w, cut) for k, w in warmups.items()}
    return _content_sha256(slice_until(bars, cut), part, [t for t in funding if t < cut])


@dataclass(frozen=True)
class _Loaded:
    frame: FeatureFrame
    market: Market
    lines: List[str]  # "relative_path:sha256" of every file read
    missing: List[str]  # warm-up files not found
    content: str  # sha256 of the arrays kept after the cut
    train_part: str  # the same over the training part


def _load_symbol(
    data_dir: Path,
    symbol: str,
    days: Tuple[date, date],
    until_ms: Optional[int],
    exec_cfg: ExecConfig,
) -> _Loaded:
    """One symbol's frame and market with the hashes of what was read and kept."""
    start_day, end_day = days
    bars = load_days(data_dir, "prod", symbol, "1m", start_day, end_day)
    if until_ms is not None:
        bars = slice_until(bars, until_ms)
    lines = [
        _file_line(data_dir, day_path(data_dir, "prod", symbol, "1m", start_day + timedelta(k)))
        for k in range((end_day - start_day).days + 1)
    ]
    warmups: Dict[str, Optional[Bars]] = {}
    missing: List[str] = []
    for interval in _WARMUP_INTERVALS:
        path = warmup_path(data_dir, symbol, interval)
        if path.exists():
            lines.append(_file_line(data_dir, path))
            warmups[interval] = _read_warmup(path, interval, until_ms)
        else:
            missing.append(path.relative_to(data_dir).as_posix())
            warmups[interval] = None
    fpath = funding_path(data_dir, symbol)
    funding = _read_funding(fpath, until_ms)
    lines.append(_file_line(data_dir, fpath))
    frame = FeatureFrame.build(symbol, bars, warmups["1h"], warmups["4h"])
    market = Market(symbol, bars, OutcomeCache(bars, exec_cfg), funding)
    return _Loaded(frame, market, lines, missing, _content_sha256(bars, warmups, funding),
                   _training_part_sha256(bars, warmups, funding))


def load_dataset(
    data_dir: Path,
    until_ms: Optional[int],
    *,
    start_day: Optional[date] = None,
    exec_cfg: Optional[ExecConfig] = None,
) -> Dataset:
    """Load both symbols from ``start_day`` (default: the training start's WIB day).

    With ``until_ms`` the day files run to ``wib_day(until_ms - 1)`` and every bar
    series is cut to bars closed by ``until_ms``; without it they run to the last
    holdout day. Missing day or funding files raise; a missing warm-up is recorded.
    """
    data_dir = Path(data_dir)
    first = start_day if start_day is not None else wib_day(ms(TRAIN_START))
    end_ms = until_ms if until_ms is not None else ms(HOLDOUT_END)
    last = wib_day(end_ms - 1)
    cfg = exec_cfg if exec_cfg is not None else ExecConfig()
    loaded = {s: _load_symbol(data_dir, s, (first, last), until_ms, cfg) for s in SYMBOLS}
    lines = sorted(line for x in loaded.values() for line in x.lines)
    loaded_until = until_ms if until_ms is not None else wib_day_start_ms(last + timedelta(1))
    return Dataset(
        frames=MappingProxyType({s: x.frame for s, x in loaded.items()}),
        markets=MappingProxyType({s: x.market for s, x in loaded.items()}),
        data_sha256=_text_sha256(lines),
        loaded_until_ms=loaded_until,
        missing_warmups=tuple(m for x in loaded.values() for m in x.missing),
        train_data_sha256=_text_sha256([f"{s}:{x.content}" for s, x in loaded.items()]),
        train_part_sha256=_text_sha256([f"{s}:{x.train_part}" for s, x in loaded.items()]),
    )


def _text_sha256(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


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
    """The one-position replay over training with the block-end purge."""
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
        blocked_entry_windows=purge_windows(segments),
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


def check_purge_covers(calib: Calibration, exec_cfg: ExecConfig) -> None:
    """The purge must outlast the longest trade a tick can open (worst latency, the
    order delay, the fill bar and the time stop), or a trade could cross a block end."""
    worst_latency = max(calib.latencies_ms, default=0.0)
    longest_ms = (worst_latency + exec_cfg.order_delay_s * 1000 + _MS_PER_MIN
                  + exec_cfg.time_stop_min * _MS_PER_MIN)
    if longest_ms > PURGE_TICK_MINUTES * _MS_PER_MIN:
        raise ValueError(
            f"a trade can last {longest_ms / _MS_PER_MIN:.1f} min from its tick, longer than "
            f"the {PURGE_TICK_MINUTES}-minute purge"
        )


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


def _primary_rule(sel: Selection, cfg: SelectionConfig) -> Optional[RuleSpec]:
    """Trend and entry of contender 1, else of the best-ranked Stage-1 rule, with
    strength off and no filters (ruling: the fallback is a primary rule too)."""
    if sel.choice is not None:
        return RuleSpec(sel.choice.trend, sel.choice.entry, "OFF")
    best: Optional[RuleSpec] = None
    best_value = -math.inf
    for rule in stage1_rules():  # strict > keeps the earlier (simpler) rule on ties
        value = rank_value(sel.results[rule.key()], cfg)
        if value > best_value:
            best, best_value = rule, value
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
    windows = purge_windows(ctx.segments)
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
             "reliability": None, "n_oof": 0, "n_candidates": 0}
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


def _selection_document(
    ctx: _Context, sel: Selection, primary: Optional[RuleSpec], extra: Mapping[str, Any]
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "contender1": sel.choice.key() if sel.choice is not None else None,
        "top": [r.key() for r in sel.top],
        "stable": sel.stable,
        "pbo": sel.pbo,
        "pbo_pass": sel.pbo < ctx.sel_cfg.pbo_max,
        "supply_limited": sel.supply_limited,
        "primary_rule": primary.key() if primary is not None else None,
        "s_star_bps": ctx.calib.s_star_bps,
        "n_stops": ctx.calib.n_stops,
        "n_latencies": len(ctx.calib.latencies_ms),
        "latencies_sha256": latencies_sha256(ctx.calib.latencies_ms),
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
    check_spec_run(spec_sha, settings)
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
        "code_sha256": code_sha, **_data_record(dataset), **c0_training(ctx),
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
