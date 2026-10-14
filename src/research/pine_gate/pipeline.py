"""Training stage and the holdout lock (spec 5.1, 5.3, 5.6, 5.8, 5.9 step 0).

Research code; the bot never imports it. Standard library only.

``load_dataset`` reads production 1m day files, the native 1h/4h warm-up and
the funding times, and cuts every bar series at ``until_ms``. ``train_stage``
refuses a dataset loaded past the training end + 60 minutes, so no holdout bar
can reach a feature, a trade or the model. It simulates every rule with entry
ticks blocked in the 65 minutes before each block end (purge), runs the
two-stage selection, fits contender 3's meta-model and writes a selection file
that records the spec sha256. ``load_selection_checked`` is the lock the test
stage must pass: no file, a broken file or a different spec sha256 refuses.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import statistics
import tempfile
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .bars import (
    Bars,
    bars_from_rows,
    day_path,
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
from .metalabel import Candidate, candidate_features, train_meta
from .outcomes import OutcomeCache, gross_bps
from .rules import contender2_signal, rule_signal, trend_label
from .selection import RuleResult, Selection, eligible, rank_value, select, summarize
from .simulate import Market, SignalFn, Trade, in_entry_window, simulate, trading_days
from .spec import (
    BLOCKS,
    CSCV_BLOCKS,
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
from .stats import mean_gross_bps, spa_pvalue, trades_per_trading_day

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
SHA256_HEX_LEN = 64
SELECTION_PREFIX = "pine_gate_selection_"
_WARMUP_INTERVALS = ("1h", "4h")

TimeRange = Tuple[int, int]
MetaTrainer = Callable[..., Tuple[Any, Dict[str, Any]]]


class HoldoutLockError(RuntimeError):
    """The selection file is missing, unreadable or was made under another spec."""


# --------------------------------------------------------------------------- segments


def equal_ranges(start_ms: int, end_ms: int, n: int) -> Tuple[TimeRange, ...]:
    """``n`` half-open ranges of equal length tiling [start, end); the last absorbs rounding."""
    if n < 1 or end_ms <= start_ms:
        raise ValueError(f"cannot cut {start_ms}..{end_ms} into {n} ranges")
    step = (end_ms - start_ms) // n
    edges = [start_ms + k * step for k in range(n)] + [end_ms]
    return tuple(zip(edges, edges[1:]))


def _check_tiling(name: str, ranges: Sequence[TimeRange], start_ms: int, end_ms: int) -> None:
    if not ranges or ranges[0][0] != start_ms or ranges[-1][1] != end_ms:
        raise ValueError(f"{name} must start at {start_ms} and end at {end_ms}")
    for (a, b), (c, _) in zip(ranges, ranges[1:]):
        if b != c:
            raise ValueError(f"{name} leaves a gap or overlap at {b}..{c}")
    if any(b <= a for a, b in ranges):
        raise ValueError(f"{name} has an empty range")


@dataclass(frozen=True)
class Segments:
    """Training range, its B1-B4 selection blocks and the CSCV blocks, in UTC ms."""

    train_start_ms: int
    train_end_ms: int
    block_bounds_ms: Tuple[TimeRange, ...]
    cscv_bounds_ms: Tuple[TimeRange, ...]

    def __post_init__(self) -> None:
        if len(self.block_bounds_ms) != len(BLOCKS):
            raise ValueError(
                f"need {len(BLOCKS)} selection blocks, got {len(self.block_bounds_ms)}"
            )
        if len(self.cscv_bounds_ms) < 2 or len(self.cscv_bounds_ms) % 2:
            raise ValueError("the number of CSCV blocks must be even and >= 2")
        start, end = self.train_start_ms, self.train_end_ms
        _check_tiling("block_bounds_ms", self.block_bounds_ms, start, end)
        _check_tiling("cscv_bounds_ms", self.cscv_bounds_ms, start, end)

    @classmethod
    def spec(cls) -> "Segments":
        start, end = ms(TRAIN_START), ms(TRAIN_END)
        return cls(
            train_start_ms=start,
            train_end_ms=end,
            block_bounds_ms=tuple((ms(a), ms(b)) for a, b in BLOCKS),
            cscv_bounds_ms=equal_ranges(start, end, CSCV_BLOCKS),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "train_start_ms": self.train_start_ms,
            "train_end_ms": self.train_end_ms,
            "block_bounds_ms": [list(r) for r in self.block_bounds_ms],
            "cscv_bounds_ms": [list(r) for r in self.cscv_bounds_ms],
        }


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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_line(data_dir: Path, path: Path) -> str:
    return f"{Path(path).relative_to(data_dir).as_posix()}:{_file_sha256(path)}"


def _read_warmup(path: Path, interval: str, until_ms: Optional[int]) -> Bars:
    bars = bars_from_rows(read_day(path), interval_to_ms(interval))
    return bars if until_ms is None else slice_until(bars, until_ms)


def _read_funding(path: Path, until_ms: Optional[int]) -> Tuple[int, ...]:
    """Funding times from JSON (required); kept below ``until_ms`` + 1 day."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or any(type(t) is not int for t in raw):
        raise ValueError(f"{path}: expected a JSON list of integer milliseconds")
    limit = None if until_ms is None else until_ms + _MS_PER_DAY
    return tuple(sorted(t for t in raw if limit is None or t < limit))


def _load_symbol(
    data_dir: Path,
    symbol: str,
    days: Tuple[date, date],
    until_ms: Optional[int],
    exec_cfg: ExecConfig,
) -> Tuple[FeatureFrame, Market, List[str], List[str]]:
    """One symbol's frame and market, plus its file hash lines and missing warm-ups."""
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
    return frame, market, lines, missing


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
    frames: Dict[str, FeatureFrame] = {}
    markets: Dict[str, Market] = {}
    lines: List[str] = []
    missing: List[str] = []
    for symbol in SYMBOLS:
        frame, market, sym_lines, sym_missing = _load_symbol(
            data_dir, symbol, (first, last), until_ms, cfg
        )
        frames[symbol], markets[symbol] = frame, market
        lines.extend(sym_lines)
        missing.extend(sym_missing)
    digest = hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()
    loaded_until = until_ms if until_ms is not None else wib_day_start_ms(last + timedelta(1))
    return Dataset(
        frames=MappingProxyType(frames),
        markets=MappingProxyType(markets),
        data_sha256=digest,
        loaded_until_ms=loaded_until,
        missing_warmups=tuple(missing),
    )


# --------------------------------------------------------------------------- signals


def rule_signal_fn(rule: RuleSpec, frames: Mapping[str, FeatureFrame]) -> SignalFn:
    def signal(symbol: str, t: int) -> int:
        frame = frames[symbol]
        return rule_signal(rule, frame.snapshot(t), lambda: frame.poc_24h(t))

    return signal


def contender2_fn(frames: Mapping[str, FeatureFrame]) -> SignalFn:
    return lambda symbol, t: contender2_signal(frames[symbol].snapshot(t))


def _random_side_fn(seed: int) -> SignalFn:
    """Benchmark: a seeded random side on every call (always-long's every entry tick)."""
    rng = random.Random(seed)
    return lambda symbol, t: rng.choice((1, -1))


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

    def run(self, signal: SignalFn, start_ms: Optional[int] = None) -> List[Trade]:
        return simulate_training(
            signal, self.markets, self.segments, self.calib, seed=self.seed,
            exec_cfg=self.exec_cfg, guard_cfg=self.guard_cfg, start_ms=start_ms,
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
        for bars in (dataset.markets[symbol].bars_1m, dataset.frames[symbol].bars_1m):
            if len(bars) and bars.open_ms[-1] + bars.interval_ms > limit:
                raise ValueError(f"{symbol} holds a bar closing after the training limit {limit}")


def _check_purge_covers(calib: Calibration, exec_cfg: ExecConfig) -> None:
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


def _training_markets(dataset: Dataset, exec_cfg: ExecConfig) -> Tuple[Market, ...]:
    """Fresh outcome caches on the same ExecConfig the simulation uses."""
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


def _daily_gross(trades: Sequence[Trade], days: Sequence[date]) -> List[float]:
    """Mean gross bps of each listed day's trades; 0.0 on a day without trades."""
    by_day: Dict[date, List[float]] = {}
    for t in trades:
        by_day.setdefault(t.wib_day, []).append(t.gross_bps)
    return [statistics.fmean(by_day[d]) if d in by_day else 0.0 for d in days]


def _spa(ctx: _Context, results: Mapping[str, RuleResult], spa_n: int) -> float:
    """Hansen SPA of every evaluated rule against the random-side benchmark."""
    days = ctx.train_days()
    bench = _daily_gross(ctx.run(_random_side_fn(ctx.seed)), days)
    diff = {}
    for key, r in results.items():
        series = _daily_gross(r.trades, days)
        diff[key] = [a - b for a, b in zip(series, bench)]
    return spa_pvalue(diff, n=spa_n, seed=ctx.seed)


def _primary_rule(sel: Selection, cfg: SelectionConfig) -> Optional[RuleSpec]:
    """Contender 1's trend and entry with strength off; else the best Stage-1 rule."""
    if sel.choice is not None:
        return RuleSpec(sel.choice.trend, sel.choice.entry, "OFF")
    best: Optional[RuleSpec] = None
    best_value = -math.inf
    for rule in stage1_rules():  # strict > keeps the earlier (simpler) rule on ties
        value = rank_value(sel.results[rule.key()], cfg)
        if value > best_value:
            best, best_value = rule, value
    return best


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
    order_ms = c + delay_ms
    idx = bisect_right(open_ms, order_ms) - 1
    if idx < 0 or order_ms >= open_ms[idx] + _MS_PER_MIN:
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
    empty = {"meta_model": None, "cv_logloss": None, "chosen_C": None,
             "reliability": None, "n_oof": 0, "n_candidates": 0}
    if primary is None:
        return empty
    candidates = meta_candidates(ctx, primary)
    model, diag = trainer(candidates, _supply_fn(ctx, candidates), ctx.model_cfg)
    return {
        "meta_model": model.to_dict(),
        "cv_logloss": {str(C): list(v) for C, v in diag["cv_logloss"].items()},
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
        "seed": ctx.seed,
        "segments": ctx.segments.to_dict(),
        "configs": {
            "exec": asdict(ctx.exec_cfg),
            "guard": asdict(ctx.guard_cfg),
            "selection": asdict(ctx.sel_cfg),
            "model": asdict(ctx.model_cfg),
        },
        "rules": {k: _rule_metrics(r, ctx.sel_cfg) for k, r in sorted(sel.results.items())},
    }
    doc.update(extra)
    return doc


def _write_json_atomic(path: Path, doc: Mapping[str, Any]) -> None:
    text = json.dumps(doc, sort_keys=True, indent=1, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _check_spec_sha(spec_sha: str) -> None:
    if (not isinstance(spec_sha, str) or len(spec_sha) != SHA256_HEX_LEN
            or any(ch not in "0123456789abcdef" for ch in spec_sha)):
        raise ValueError(f"spec_sha must be a lowercase sha256 hex digest, got {spec_sha!r}")


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

    ``train_meta`` validates on the dates of ``spec.BLOCKS``, so injected segments
    need an injected ``meta_trainer`` (tests only).
    """
    _check_spec_sha(spec_sha)
    seg = segments if segments is not None else Segments.spec()
    if meta_trainer is train_meta and seg != Segments.spec():
        raise ValueError(
            "train_meta validates on spec.BLOCKS; injected segments need a meta_trainer"
        )
    _check_dataset(dataset, seg)
    cfg = exec_cfg if exec_cfg is not None else ExecConfig()
    _check_purge_covers(calib, cfg)
    ctx = _Context(
        frames=dataset.frames, markets=_training_markets(dataset, cfg), segments=seg,
        calib=calib, seed=seed, exec_cfg=cfg,
        guard_cfg=guard_cfg if guard_cfg is not None else GuardConfig(),
        sel_cfg=sel_cfg if sel_cfg is not None else SelectionConfig(),
        model_cfg=model_cfg if model_cfg is not None else ModelConfig(),
    )
    sel = select(_evaluator(ctx), ctx.sel_cfg)
    primary = _primary_rule(sel, ctx.sel_cfg)
    created = datetime.now(timezone.utc)
    extra = {
        "spec_sha256": spec_sha,
        "data_sha256": dataset.data_sha256,
        "data_loaded_until_ms": dataset.loaded_until_ms,
        "missing_warmups": list(dataset.missing_warmups),
        "created_at": created.isoformat(),
        "spa_p": _spa(ctx, sel.results, spa_n),
        "spa_n": spa_n,
        "regime_counts": regime_counts(ctx.frames, seg.train_start_ms, seg.train_end_ms),
        **_contender3(ctx, primary, meta_trainer),
    }
    doc = _selection_document(ctx, sel, primary, extra)
    path = Path(out_dir) / f"{SELECTION_PREFIX}{created.astimezone(WIB):%Y%m%d}.json"
    _write_json_atomic(path, doc)
    return path


def load_selection_checked(path: Path, spec_sha: str) -> Dict[str, Any]:
    """The holdout lock: the selection document, only if it was made under ``spec_sha``."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise HoldoutLockError(f"{path}: no selection file; run the train stage first") from exc
    except OSError as exc:
        raise HoldoutLockError(f"{path}: selection file unreadable ({exc})") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # includes UnicodeDecodeError
        raise HoldoutLockError(f"{path}: selection file is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("spec_sha256"), str):
        raise HoldoutLockError(f"{path}: selection file records no spec_sha256")
    if doc["spec_sha256"] != spec_sha:
        raise HoldoutLockError(
            f"{path}: spec_sha256 {doc['spec_sha256']} does not match the current spec {spec_sha}"
        )
    return doc
