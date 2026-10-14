"""Check subcommands of the Pine gate CLI: vst-check (spec 5.12) and decisions (5.13).

Research code; the bot never imports it. Standard library only. Both read the
selection file through the holdout lock and use the contender the holdout
chose (or ``--contender``). Supabase is read-only, through
``cli_common.make_get_rows``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import cli_common as common
from .bars import Bars, interval_to_ms, load_days, wib_day
from .calibrate import _fetch_all
from .checks import (
    DecisionRow,
    _entry_index,
    decision_buckets,
    load_edge_klines_vst,
    parse_decision,
    vst_agreement,
)
from .cli_data import read_warmups
from .features import FeatureFrame, _htf_bars
from .holdout import HOLDOUT_PREFIX
from .holdout_replay import HoldoutPeriod, build_contenders, parse_rule
from .metalabel import MetaModel, candidate_features
from .outcomes import OutcomeCache, gross_bps
from .pipeline import load_selection_checked
from .rules import contender2_signal, rule_signal
from .spec import (
    ENTRY_PARAMS,
    STRENGTH_PARAMS,
    TRAIN_START,
    VOL_PARAMS,
    VP_PARAMS,
    ExecConfig,
    FillConfig,
    RuleSpec,
    ms,
    spec_sha256,
)

CONTENDERS = ("c1", "c2", "c3")
EDGE_KLINES_DEFAULT = "~/replay/edge_klines.json"
EDGE_KLINES_SYMBOL = "BTC-USDT"  # the VST cache holds BTC only
VST_PREFIX = "pine_gate_vst_"
DECISIONS_PREFIX = "pine_gate_decisions_"
DECISIONS_SYMBOL = "BTC-USDT"
DECISIONS_STORED_SYMBOLS = "in.(BTC/USDT,BTC-USDT)"  # internal and venue forms
DECISIONS_SINCE = "2026-08-31T00:00:00+07:00"  # spec 5.2, WIB day
METRICS_SINCE = "2026-08-30T00:00:00+07:00"  # one day earlier: a call precedes its decision
_MS_5M = 5 * 60_000
_MS_1H = interval_to_ms("1h")
_MS_4H = interval_to_ms("4h")
_FILL = FillConfig()

_TREND_FIELDS = {
    "E200": ("close_5m", "ema200_5m"),
    "H1": ("h1_ema20", "h1_ema50", "h1_atr14"),
    "H4": ("h4_ema20", "h4_ema50", "h4_atr14"),
}
# candidate_features inputs besides the trend strength and the POC
_C3_FIELDS = ("close_5m", "atr14_5m", "rsi14_5m", "adx14_5m", "di_plus14_5m",
              "di_minus14_5m", "h1_vol_pct")


@dataclass(frozen=True)
class Chosen:
    """The contender under test: c1's rule or c3's primary rule and model."""

    name: str
    rule: Optional[RuleSpec] = None
    model: Optional[MetaModel] = None


def resolve_contender(args: argparse.Namespace) -> str:
    if args.contender:
        return args.contender
    path = (Path(args.holdout).expanduser() if args.holdout
            else common.newest(args.out_dir, f"{HOLDOUT_PREFIX}*.json"))
    if path is None:
        raise common.CliError("no holdout file found; pass --contender c1|c2|c3 or --holdout")
    chosen = common.json_doc(path).get("chosen")
    if chosen not in CONTENDERS:
        raise common.CliError(f"{path}: the holdout chose no contender; pass --contender c1|c2|c3")
    return chosen


def chosen_from_selection(selection: Mapping[str, Any], name: str) -> Chosen:
    if name == "c2":
        return Chosen("c2")
    if name == "c1":
        if selection.get("contender1") is None:
            raise common.CliError("the selection file has no contender 1")
        return Chosen("c1", parse_rule(selection["contender1"]))
    if selection.get("primary_rule") is None or selection.get("meta_model") is None:
        raise common.CliError("the selection file has no contender 3 (primary rule and model)")
    return Chosen("c3", parse_rule(selection["primary_rule"]),
                  MetaModel.from_dict(selection["meta_model"]))


def _rule_fields(rule: RuleSpec) -> Tuple[str, ...]:
    entry = ENTRY_PARAMS[rule.entry]
    if entry["kind"] == "pullback":
        fields = ("rsi14_5m",)
    elif entry["bars"] == ENTRY_PARAMS["B12"]["bars"]:
        fields = ("close_5m", "high12_prev", "low12_prev")
    else:
        fields = ("close_5m", "high24_prev", "low24_prev")
    strength = STRENGTH_PARAMS[rule.strength]
    if strength["enabled"]:
        fields += ("adx14_5m",)
        if strength["di_agrees"]:
            fields += ("di_plus14_5m", "di_minus14_5m")
    if VOL_PARAMS[rule.vol]["enabled"]:
        fields += ("h1_vol_pct",)
    if VP_PARAMS[rule.vp]["enabled"]:
        fields += ("close_5m",)
    return _TREND_FIELDS[rule.trend] + fields


def needed_fields(c: Chosen) -> Tuple[str, ...]:
    if c.name == "c2":
        return _TREND_FIELDS["H1"] + ("stretch_3m",)
    assert c.rule is not None
    return _rule_fields(c.rule) + (_C3_FIELDS if c.name == "c3" else ())


def needs_poc(c: Chosen) -> bool:
    return c.name == "c3" or (c.rule is not None and VP_PARAMS[c.rule.vp]["enabled"])


def snapshot_valid(c: Chosen, frame: FeatureFrame, poc_frame: FeatureFrame, t: int) -> bool:
    """Every input the contender reads at ``t`` exists (POC read from ``poc_frame``)."""
    s = frame.snapshot(t)
    if any(getattr(s, name) is None for name in needed_fields(c)):
        return False
    return not needs_poc(c) or poc_frame.poc_24h(t) is not None


def signal_for(c: Chosen) -> Callable[[FeatureFrame, FeatureFrame, int], int]:
    """``(features, poc_frame, t) -> side`` of the contender; the POC comes from production."""

    def signal(features: FeatureFrame, poc_frame: FeatureFrame, t: int) -> int:
        s = features.snapshot(t)
        if c.name == "c2":
            return contender2_signal(s)
        assert c.rule is not None
        side = rule_signal(c.rule, s, lambda: poc_frame.poc_24h(t))
        if c.name == "c1" or side == 0:
            return side
        assert c.model is not None
        x = candidate_features(s, side, c.rule.trend, poc_frame.poc_24h(t), t)
        return side if x is not None and c.model.prob(x) >= c.model.threshold else 0

    return signal


# --------------------------------------------------------------------------- data


def _days_on_disk(data_dir: Path, source: str, symbol: str) -> List[date]:
    folder = Path(data_dir) / source / symbol / "1m"
    days = []
    for path in folder.glob("????-??-??.csv.gz"):
        try:
            days.append(date.fromisoformat(path.name[:10]))
        except ValueError:
            continue
    return sorted(days)


def prod_bars(data_dir: Path, symbol: str, until_day: date) -> Tuple[Bars, Optional[Bars],
                                                                     Optional[Bars]]:
    """Production 1m bars from the training start to the last day file <= ``until_day``,
    with the native warm-ups: the same series the holdout frames are built from."""
    first = wib_day(ms(TRAIN_START))
    days = [d for d in _days_on_disk(data_dir, "prod", symbol) if first <= d <= until_day]
    if not days:
        raise common.CliError(f"no prod/{symbol}/1m day files from {first} to {until_day}")
    bars = load_days(data_dir, "prod", symbol, "1m", first, days[-1])
    return (bars, *read_warmups(data_dir, symbol))


def _vst_bars(args: argparse.Namespace) -> Bars:
    if args.symbol == EDGE_KLINES_SYMBOL:
        return load_edge_klines_vst(Path(args.edge_klines).expanduser())
    days = _days_on_disk(args.data_dir, "vst", args.symbol)
    if not days:
        raise common.CliError(f"no vst/{args.symbol}/1m day files in {args.data_dir}")
    return load_days(args.data_dir, "vst", args.symbol, "1m", days[0], days[-1])


def _last_close(bars: Bars) -> int:
    return bars.open_ms[-1] + bars.interval_ms


def overlap_times(a: Bars, b: Bars) -> range:
    """Every 5m bar close inside both series."""
    start = max(a.open_ms[0], b.open_ms[0])
    end = min(_last_close(a), _last_close(b))
    return range(-(-start // _MS_5M) * _MS_5M, end + 1, _MS_5M)


# --------------------------------------------------------------------------- vst-check


def cmd_vst_check(args: argparse.Namespace) -> int:
    selection = load_selection_checked(Path(args.selection).expanduser(), spec_sha256())
    chosen = chosen_from_selection(selection, resolve_contender(args))
    path = common.stamped_path(args.out_dir, f"{VST_PREFIX}{args.symbol}_")
    if path.exists():
        raise FileExistsError(f"{path}: already exists; remove it deliberately to write again")
    vst = _vst_bars(args)
    if not len(vst):
        raise common.CliError(f"no VST bars for {args.symbol}")
    bars, warm_1h, warm_4h = prod_bars(args.data_dir, args.symbol, wib_day(vst.open_ms[-1]))
    prod = FeatureFrame.build(args.symbol, bars, warm_1h, warm_4h)
    # VST history is too short for the 4h EMA50: warm up on production 1h/4h bars
    vst_frame = FeatureFrame.build(args.symbol, vst, _htf_bars(bars, warm_1h, _MS_1H),
                                   _htf_bars(bars, warm_4h, _MS_4H))
    times = [t for t in overlap_times(bars, vst)
             if snapshot_valid(chosen, prod, prod, t) and snapshot_valid(chosen, vst_frame, prod, t)]
    agreement = vst_agreement(prod, vst_frame, signal_for(chosen), times, ExecConfig())
    common.write_json_exclusive(path, asdict(agreement))
    print(f"vst-check {args.symbol} contender {chosen.name}: n {agreement.n}, "
          f"side agree {agreement.side_agree}, outcome agree {agreement.outcome_agree}, "
          f"WR prod {agreement.wr_prod} vs VST {agreement.wr_vst}, passed {agreement.passed}")
    print(path)
    return 0


# --------------------------------------------------------------------------- decisions


async def _read_decisions(get_rows: Any, settings: Any) -> Tuple[List[dict], List[dict]]:
    decisions = await _fetch_all(get_rows, settings.supabase_ai_decisions_table, [
        ("select", "id,created_at,symbol,action,confidence,payload_json"),
        ("symbol", DECISIONS_STORED_SYMBOLS),
        ("exchange", "eq.bingx"),
        ("created_at", f"gte.{DECISIONS_SINCE}"),
    ])
    metrics = await _fetch_all(get_rows, settings.supabase_ai_call_metrics_table, [
        ("select", "id,created_at,call_id,latency_ms,purpose,success"),
        ("purpose", f"eq.{_FILL.latency_purpose}"),
        ("created_at", f"gte.{METRICS_SINCE}"),
    ])
    return decisions, metrics


def entry_latencies(metrics: List[dict]) -> Dict[str, float]:
    """``call_id -> latency_ms`` of successful entry calls with a finite latency >= 0."""
    out: Dict[str, float] = {}
    for row in metrics:
        value = row.get("latency_ms")
        if (row.get("success") is not True or isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            continue
        if row.get("call_id") is not None:
            out[str(row["call_id"])] = float(value)
    return out


def _s_star(selection: Mapping[str, Any]) -> float:
    value = selection.get("s_star_bps")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("the selection file records no finite s_star_bps")
    return float(value)


@dataclass(frozen=True)
class PlacedRows:
    """Decisions inside the production data with their tick start and order time."""

    rows: Tuple[DecisionRow, ...]
    ticks: Mapping[int, int]  # id(row) -> tick start (created_at - AI latency)
    orders: Mapping[Tuple[str, int], int]  # (symbol, tick) -> order time
    n_fallback: int  # rows timed with the median latency (no linked call)
    n_outside: int  # rows dropped: tick or order outside the production bars


def place_rows(rows: Sequence[DecisionRow], latency: Mapping[str, float], median_latency: float,
               bars: Bars, order_delay_ms: int) -> PlacedRows:
    """The tick started one AI latency before ``created_at`` (the linked call's, else
    the median); the order goes out ``order_delay_ms`` after the decision is stored."""
    kept: List[DecisionRow] = []
    ticks: Dict[int, int] = {}
    orders: Dict[Tuple[str, int], int] = {}
    n_fallback = n_outside = 0
    for row in rows:
        linked = latency.get(row.ai_call_id) if row.ai_call_id is not None else None
        tick = int(round(row.created_ms - (median_latency if linked is None else linked)))
        order_ms = row.created_ms + order_delay_ms
        if tick < bars.open_ms[0] or order_ms >= _last_close(bars):
            n_outside += 1
            continue
        n_fallback += linked is None
        ticks[id(row)] = tick
        orders.setdefault((row.symbol, tick), order_ms)
        kept.append(row)
    return PlacedRows(tuple(kept), ticks, orders, n_fallback, n_outside)


def _live_decisions() -> Tuple[List[dict], Dict[str, float]]:
    """Stored decisions and the entry-call latencies, read-only from Supabase."""
    settings = common.call_live("loading the bot settings", common.make_settings)
    get_rows = common.call_live("opening the Supabase reader",
                                lambda: common.make_get_rows(settings))
    raw, metrics = common.call_live("reading Supabase",
                                    lambda: asyncio.run(_read_decisions(get_rows, settings)))
    latency = entry_latencies(metrics)
    if not latency:
        raise common.CliError("no successful entry-call latency in ai_call_metrics")
    return raw, latency


def cmd_decisions(args: argparse.Namespace) -> int:
    selection = load_selection_checked(Path(args.selection).expanduser(), spec_sha256())
    name = resolve_contender(args)
    chosen_from_selection(selection, name)  # refuse early when it is absent
    overshoot = _s_star(selection)
    path = common.stamped_path(args.out_dir, DECISIONS_PREFIX)
    if path.exists():
        raise FileExistsError(f"{path}: already exists; remove it deliberately to write again")
    raw, latency = _live_decisions()
    median_latency = statistics.median(latency.values())
    bars, warm_1h, warm_4h = prod_bars(args.data_dir, DECISIONS_SYMBOL, common.wib_today())
    frame = FeatureFrame.build(DECISIONS_SYMBOL, bars, warm_1h, warm_4h)
    cfg = ExecConfig()
    usable = [r for r in map(parse_decision, raw) if r is not None and r.symbol == DECISIONS_SYMBOL]
    placed = place_rows(usable, latency, median_latency, bars, cfg.order_delay_s * 1000)

    cache = OutcomeCache(bars, cfg)

    def outcome_for(symbol: str, t: int, side: int) -> Optional[Tuple[str, float]]:
        i = _entry_index(bars, placed.orders[(symbol, t)])
        o = cache.get(i, side) if i is not None else None
        return None if o is None else (o.kind, gross_bps(o, side, overshoot, cfg))

    contenders, _ = build_contenders(selection, {DECISIONS_SYMBOL: frame}, HoldoutPeriod.spec())
    signal = next(c.signal for c in contenders if c.name == name)
    buckets = decision_buckets(placed.rows, lambda r: placed.ticks[id(r)], signal, outcome_for)
    doc = {
        "contender": name,
        "symbol": DECISIONS_SYMBOL,
        "since": DECISIONS_SINCE,
        "n_read": len(raw),
        "n_unusable": len(raw) - len(usable),
        "n_rows": len(placed.rows),
        "n_outside_data": placed.n_outside,
        "n_fallback_latency": placed.n_fallback,
        "median_latency_ms": median_latency,
        "s_star_bps": overshoot,
        "data_until_ms": _last_close(bars),
        "buckets": buckets,
    }
    common.write_json_exclusive(path, doc)
    print(f"decisions {DECISIONS_SYMBOL} contender {name}: {len(placed.rows)} rows "
          f"({placed.n_outside} outside the data, {placed.n_fallback} with the median latency)")
    print(path)
    return 0


def add_parsers(sub: Any, symbol_type: Callable[[str], str]) -> None:
    p = sub.add_parser("vst-check", help="VST vs production agreement (spec 5.12)")
    p.add_argument("--symbol", type=symbol_type, required=True)
    p.add_argument("--selection", required=True)
    p.add_argument("--edge-klines", default=EDGE_KLINES_DEFAULT,
                   help=f"VST cache for {EDGE_KLINES_SYMBOL}")
    p.add_argument("--contender", choices=CONTENDERS, help="default: the holdout's choice")
    p.add_argument("--holdout", help="default: the newest holdout file in --out-dir")
    p.set_defaults(handler=cmd_vst_check)

    p = sub.add_parser("decisions", help="AI decision buckets A/B/E/F (spec 5.13)")
    p.add_argument("--selection", required=True)
    p.add_argument("--contender", choices=CONTENDERS, help="default: the holdout's choice")
    p.add_argument("--holdout", help="default: the newest holdout file in --out-dir")
    p.set_defaults(handler=cmd_decisions)
