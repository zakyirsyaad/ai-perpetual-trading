"""The shadow study of exit variants: pre-registered exit rules replayed on every closed live trade.

For each closed trade the study walks the 1m candles again, once under the live exits (hard stop, net floor, time
limit) and once under each pre-registered variant, and keeps the *paired* difference per trade: the same entry and the
same candles under two rules, so the market's noise cancels and far fewer trades are needed than in a live A/B. It
changes no trade; it reads rows and candles and returns numbers.

Everything that could be tuned after seeing data is fixed here, in code, before the data was looked at: the variants,
the booking conventions, the market costs, the calm-market threshold and the decision rule (``evaluate_rule``). A new
variant or threshold is a new hypothesis and needs a new evidence document, not an edit of these numbers.

The stop of a trade is not stored, so it is derived as the engine derives it (``AIOrchestrator._resolve_trade_targets``):
the stop the model asked for (read from the nearest preceding OPEN decision of the same symbol, side and confidence) or the
configured one, widened to 1.5 x the ATR percent and capped. A trade without such a decision, or without its candles,
is skipped and listed, never guessed.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence, Union

from bot.core.forward_summary import EXIT_SL, EXIT_TIME, EXIT_TP, PER_MILLION, WIB, classify_exit
from bot.storage.supabase_trade_store import parse_timestamp
from research.exit_variants.walker import (
    LONG,
    SHORT,
    SL,
    TP,
    WAKTU,
    Cols,
    Costs,
    ExitRule,
    Outcome,
    day_bootstrap,
    group_by_day,
    mean_se,
    net_for_price,
    settle,
    walk,
)
from research.pine_gate.features import INTERVAL_1M

__all__ = [
    "ALL_POSITIONS",
    "CALM_POSITIONS",
    "CONVENTIONS",
    "DECISION_CLOCK_SKEW",
    "DECISION_CONVENTION",
    "FORWARD_FROM",
    "PRIMARY_VARIANT",
    "REGISTERED_FLOOR_NET",
    "VARIANTS",
    "Calibration",
    "Candidate",
    "Convention",
    "Criterion",
    "DecisionIndex",
    "Delta",
    "LiveRules",
    "Position",
    "Simulated",
    "Skip",
    "Study",
    "Summary",
    "Variant",
    "Verdict",
    "WeekDelta",
    "calibration",
    "calm_subset",
    "derive_stop_pct",
    "derive_take_profit_pct",
    "entry_index",
    "evaluate_rule",
    "forward_subset",
    "iso_week",
    "live_rules_from_settings",
    "load_positions",
    "run_study",
    "simulate",
    "summarize",
]

TRADED_SYMBOL = "BTC/USDT"  # both bots trade BTC only, and the candle cache is BTC-USDT

# --- The pre-registered hypothesis ------------------------------------------------------------------------------
# "A profit lock adds at least +2 USDT per trade in calm markets." The variants below were fixed on 2026-10-28.
REGISTERED_FLOOR_NET = 56.0  # the live net floor the variants' numbers were chosen around; a report warns on another
PRIMARY_VARIANT = "lock 45->20"  # the only variant the decision rule judges; the others are descriptive
CALM_ATR_PCT = 0.113  # ATR3m percent below which a market is calm: the train tercile of the 14-month backtest sample
DECISION_WINDOW = timedelta(minutes=15)  # how far before the open the model's OPEN decision may lie
DECISION_CLOCK_SKEW = timedelta(seconds=5)  # created_at is the database's clock, opened_at the bot's own
# The variants were fixed on 28 Oct 2026, after the 22 trades of 26-28 Oct had been looked at (the levels were chosen on their
# peaks). Only trades opened from here on may decide the verdict; earlier ones are described, never counted.
FORWARD_FROM = datetime(2026, 10, 29, 0, 0, tzinfo=WIB)
CONFIDENCE_TOLERANCE = 1e-6  # the trade and its decision carry the same float; this absorbs storage noise
MIN_BOOTSTRAP_DAYS = 8  # a resampling of fewer days is not an interval worth printing

# --- The pre-registered decision rule (``evaluate_rule``) -------------------------------------------------------
MIN_T = 2.0
WEEKS_CONSIDERED = 4  # the last four WIB ISO weeks with a trade ...
WEEKS_REQUIRED = 3  # ... of which this many must show a positive mean difference
MIN_CALIBRATION = 0.90  # the live-rule walk must reproduce at least this share of the real exit reasons

# Market exits (stop, time) cost 0.5 bps against the position, the take-profit rests as a limit order and fills at its
# price: the live costs of config/modes_live_exit.yaml, measured on the VST account. A lock is a stop, so it pays the
# market cost; convention C+1bp adds one more bp to it, the closest reading to a live stop-market order.
LIVE_COSTS = Costs(exit_market_bps=0.5, tp_bps=0.0, lock_extra_bps=0.0)

_MIN_TARGET_NET = 0.01  # USDT; see ``_plan_target_net``
_ACTION_OF_SIDE = {LONG: "OPEN_LONG", SHORT: "OPEN_SHORT"}
_SIDE_NAMES = {"LONG": LONG, "SHORT": SHORT}
_ACTUAL_EXIT = {EXIT_TP: TP, EXIT_SL: SL, EXIT_TIME: WAKTU}  # forward_summary's buckets as the walker's reasons

SKIP_UNREADABLE = "unreadable"  # a trade row without a usable side, time, price or quantity
SKIP_SYMBOL = "symbol"  # not the symbol of the candle cache
SKIP_ATR = "atr"  # no setup.atr_pct, so the stop cannot be derived
SKIP_DECISION = "decision"  # no OPEN decision of that symbol, side and confidence in the 15 minutes before the open
SKIP_CANDLES = "candles"  # the cache lacks the entry bar, a minute, or the bars up to the exit


@dataclass(frozen=True)
class Variant:
    """One pre-registered exit variant. ``tp_net`` replaces the live floor (``None`` keeps it); ``lock_steps`` and the
    trail are the walker's (``ExitRule``)."""

    name: str
    tp_net: Optional[float] = None
    lock_steps: tuple[tuple[float, float], ...] = ()
    trail_arm: Optional[float] = None
    trail_dist: float = 0.0


@dataclass(frozen=True)
class Convention:
    """How a lock is booked inside the 1m bar that arms it (see the walker), plus extra slippage on its exits."""

    name: str
    same_bar: bool
    close_check: bool
    lock_extra_bps: float


VARIANTS: tuple[Variant, ...] = (
    Variant("lock 45->20", lock_steps=((45.0, 20.0),)),  # primary: arm at net +45 USDT, stop at net +20
    Variant("floor +45", tp_net=45.0),
    Variant("lock 40->15", lock_steps=((40.0, 15.0),)),
    Variant("trail 40/20", trail_arm=40.0, trail_dist=20.0),  # arm at +40, trail 20 USDT of net behind the peak
)

# C+1bp and C are the main rows (the realistic booking, with and without one more bp on lock exits); B is the bad-case
# bound. Booking A is not reported: it books a retraced bar at the next open, which is not a bound.
CONVENTIONS: tuple[Convention, ...] = (
    Convention("C+1bp", same_bar=False, close_check=True, lock_extra_bps=1.0),
    Convention("C", same_bar=False, close_check=True, lock_extra_bps=0.0),
    Convention("B", same_bar=True, close_check=False, lock_extra_bps=0.0),
)
DECISION_CONVENTION = "C+1bp"  # the rule below reads this booking


@dataclass(frozen=True)
class Candidate:
    """A pre-registered claim the rule tests: all trades need +5 USDT each over 60 trades, the calm ones +2 over 250."""

    name: str
    min_trades: int
    min_mean: float


ALL_POSITIONS = Candidate("semua", min_trades=60, min_mean=5.0)
CALM_POSITIONS = Candidate("tenang", min_trades=250, min_mean=2.0)


@dataclass(frozen=True)
class LiveRules:
    """The numbers of the live exits and of the stop derivation, read from the bot's settings."""

    fee_pct: float  # TAKER_FEE_PCT: the estimate the live exit nets
    floor_net: float  # VOLUME_NEUTRAL_EXIT_MIN_NET_USDT: the net the live floor exits at
    hold_minutes: int  # POSITION_HOLD_MINUTES: the time limit
    stop_loss_pct: float  # STOP_LOSS_PCT: the stop when the model gives none
    atr_sl_multiplier: float
    max_stop_loss_pct: float  # MAX_STOP_LOSS_PCT; 0 means no cap
    take_profit_pct: float  # TAKE_PROFIT_PCT: the plan's target when the model gives none
    atr_tp_multiplier: float  # ATR_TP_MULTIPLIER
    min_fee_adjusted_rr: float  # AI_MIN_FEE_ADJUSTED_RR: sets the smallest target the plan may have
    venue_fee_rates: bool  # USE_EXCHANGE_FEE_RATES: the live exit then nets the venue's rate, not TAKER_FEE_PCT


def live_rules_from_settings(settings: Any) -> LiveRules:
    """The live exit numbers from a ``Settings``-like object.

    The study models the net floor exit (``VOLUME_NEUTRAL_EXIT_MODE=floor`` on the ``net`` basis) plus the hard stop
    and the time limit. Another exit setup would make every simulated baseline wrong, so it raises ``ValueError``
    naming each key to change, on one line."""
    wrong = []
    if not settings.volume_neutral_exit_enabled:
        wrong.append("VOLUME_NEUTRAL_EXIT_ENABLED=true")
    if settings.volume_neutral_exit_mode != "floor":
        wrong.append("VOLUME_NEUTRAL_EXIT_MODE=floor")
    if settings.volume_neutral_exit_basis != "net":
        wrong.append("VOLUME_NEUTRAL_EXIT_BASIS=net")
    if not settings.volume_neutral_exit_min_net_usdt > 0:
        wrong.append("VOLUME_NEUTRAL_EXIT_MIN_NET_USDT above 0")
    if wrong:
        raise ValueError("the live exits are not the net floor this study models; it needs " + ", ".join(wrong))
    if settings.position_hold_minutes < 2:
        raise ValueError("POSITION_HOLD_MINUTES must be at least 2 minutes for the walk to have a time-exit bar")
    return LiveRules(
        fee_pct=float(settings.taker_fee_pct),
        floor_net=float(settings.volume_neutral_exit_min_net_usdt),
        hold_minutes=int(settings.position_hold_minutes),
        stop_loss_pct=float(settings.stop_loss_pct),
        atr_sl_multiplier=float(settings.atr_sl_multiplier),
        max_stop_loss_pct=float(settings.max_stop_loss_pct),
        take_profit_pct=float(settings.take_profit_pct),
        atr_tp_multiplier=float(settings.atr_tp_multiplier),
        min_fee_adjusted_rr=float(settings.ai_min_fee_adjusted_rr),
        venue_fee_rates=bool(settings.use_exchange_fee_rates),
    )


def derive_stop_pct(ai_stop_pct: Optional[float], atr_pct: float, rules: LiveRules) -> float:
    """The stop distance in percent, as ``AIOrchestrator._resolve_trade_targets`` derives it.

    The model's stop (the configured one when it gave none), widened to ``atr_sl_multiplier`` x the ATR percent,
    then capped at ``max_stop_loss_pct`` when that is above 0."""
    stop = max(ai_stop_pct or rules.stop_loss_pct, atr_pct * rules.atr_sl_multiplier)
    if rules.max_stop_loss_pct > 0:
        stop = min(stop, rules.max_stop_loss_pct)
    return stop


def derive_take_profit_pct(ai_tp_pct: Optional[float], atr_pct: float, sl_pct: float, rules: LiveRules) -> float:
    """The plan's own take-profit distance in percent, as ``_resolve_trade_targets`` derives it (after the stop).

    The widest of the model's target (the configured one when it gave none), ``atr_tp_multiplier`` x the ATR percent and
    the smallest target that still pays ``min_fee_adjusted_rr`` times the risk after the round-trip fee (twice the taker
    fee). The engine exits at this target as well as at the net floor, so the live exit is the nearer of the two."""
    round_trip = 2.0 * rules.fee_pct
    min_for_reward = round_trip + (sl_pct + round_trip) * rules.min_fee_adjusted_rr
    return max(ai_tp_pct or rules.take_profit_pct, atr_pct * rules.atr_tp_multiplier, min_for_reward)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> Optional[float]:
    number = _finite(value)
    return number if number is not None and number > 0 else None


class Decision(NamedTuple):
    created_at: datetime
    stop_loss_pct: Optional[float]  # the stop the model asked for; None when it gave none
    take_profit_pct: Optional[float]  # the target the model asked for; None when it gave none


class _DecisionFacts(NamedTuple):
    created_at: datetime
    symbol: str
    action: str
    confidence: float
    stop_loss_pct: Optional[float]
    take_profit_pct: Optional[float]


class DecisionIndex:
    """The OPEN decisions of a window, parsed once and searchable by time.

    Rows are the store's decision rows: ``created_at``, ``symbol``, ``action``, ``confidence``, ``stop_loss_pct`` and
    ``take_profit_pct`` (the model's levels, a number or the text PostgREST gives for a JSON extraction). A row that
    cannot be read, or that is not an OPEN decision, is dropped."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        facts = [fact for fact in map(self._read, rows) if fact is not None]
        facts.sort(key=lambda fact: fact.created_at)
        self._facts = facts
        self._times = [fact.created_at for fact in facts]

    @staticmethod
    def _read(row: Mapping[str, Any]) -> Optional[_DecisionFacts]:
        created_at = parse_timestamp(row.get("created_at"))
        confidence = _finite(row.get("confidence"))
        action = str(row.get("action") or "")
        if created_at is None or confidence is None or action not in _ACTION_OF_SIDE.values():
            return None
        return _DecisionFacts(
            created_at,
            str(row.get("symbol") or ""),
            action,
            confidence,
            _positive(row.get("stop_loss_pct")),
            _positive(row.get("take_profit_pct")),
        )

    def nearest(self, symbol: str, side: int, confidence: float, opened_at: datetime) -> Optional[Decision]:
        """The latest OPEN decision of this symbol, side and confidence from the 15 minutes up to and including the
        open of the trade, or ``None``.

        ``created_at`` is the database's clock and ``opened_at`` the bot's, so with no decision before the open the
        earliest one up to ``DECISION_CLOCK_SKEW`` after it is taken (a true predecessor always wins)."""
        action = _ACTION_OF_SIDE[side]
        oldest = bisect_left(self._times, opened_at - DECISION_WINDOW)
        newest = bisect_right(self._times, opened_at)  # created_at <= opened_at
        latest = bisect_right(self._times, opened_at + DECISION_CLOCK_SKEW)
        for i in [*range(newest - 1, oldest - 1, -1), *range(newest, latest)]:
            fact = self._facts[i]
            if fact.symbol == symbol and fact.action == action and abs(fact.confidence - confidence) <= CONFIDENCE_TOLERANCE:
                return Decision(fact.created_at, fact.stop_loss_pct, fact.take_profit_pct)
        return None


@dataclass(frozen=True)
class Skip:
    """A trade the study could not use, named by its open time (``None`` if unreadable), side ("?" if unreadable) and the
    reason code (the ``SKIP_`` constants)."""

    opened_at: Optional[datetime]
    side: str
    code: str


@dataclass(frozen=True)
class Position:
    """A closed trade as the simulation needs it. ``actual_exit`` is the walker's reason for the real exit (TP, SL or WAKTU),
    or "" for an exit the walk does not model (an AI or guard exit); ``volume_usdt`` is the real (entry + exit) x quantity,
    the denominator of the per-1M figures."""

    opened_at: datetime
    closed_at: datetime
    side: int
    entry_fill: float
    notional: float
    sl_pct: float
    tp_pct: float
    atr_pct: float
    actual_exit: str
    volume_usdt: float


def _side_name(side: int) -> str:
    return "LONG" if side == LONG else "SHORT"


def _position_of(
    row: Mapping[str, Any], index: DecisionIndex, rules: LiveRules, symbol: str
) -> Union[Position, Skip]:
    side_name = str(row.get("direction") or "").strip().upper()
    side = _SIDE_NAMES.get(side_name)
    opened_at, closed_at = parse_timestamp(row.get("opened_at")), parse_timestamp(row.get("closed_at"))
    entry, exit_, qty = _positive(row.get("entry_price")), _positive(row.get("exit_price")), _positive(row.get("quantity"))
    label = side_name if side is not None else "?"
    if side is None or opened_at is None or closed_at is None or entry is None or exit_ is None or qty is None:
        return Skip(opened_at, label, SKIP_UNREADABLE)
    if row.get("symbol") != symbol:
        return Skip(opened_at, label, SKIP_SYMBOL)
    metadata = row.get("factor_metadata")
    setup = metadata.get("setup") if isinstance(metadata, Mapping) else None
    atr_pct = _positive(setup.get("atr_pct")) if isinstance(setup, Mapping) else None
    if atr_pct is None:
        return Skip(opened_at, label, SKIP_ATR)
    confidence = _finite(row.get("confidence"))
    decision = None if confidence is None else index.nearest(symbol, side, confidence, opened_at)
    if decision is None:
        return Skip(opened_at, label, SKIP_DECISION)
    sl_pct = derive_stop_pct(decision.stop_loss_pct, atr_pct, rules)
    return Position(
        opened_at=opened_at,
        closed_at=closed_at,
        side=side,
        entry_fill=entry,
        notional=qty * entry,
        sl_pct=sl_pct,
        tp_pct=derive_take_profit_pct(decision.take_profit_pct, atr_pct, sl_pct, rules),
        atr_pct=atr_pct,
        actual_exit=_ACTUAL_EXIT.get(classify_exit(str(row.get("reason") or "")), ""),
        volume_usdt=(entry + exit_) * qty,
    )


def load_positions(
    trade_rows: Iterable[Mapping[str, Any]], index: DecisionIndex, rules: LiveRules, symbol: str = TRADED_SYMBOL
) -> tuple[list[Position], list[Skip]]:
    """The usable trades as positions in the order they opened, and a ``Skip`` for each trade that could not be used."""
    positions: list[Position] = []
    skipped: list[Skip] = []
    for row in trade_rows:
        loaded = _position_of(row, index, rules, symbol)
        if isinstance(loaded, Skip):
            skipped.append(loaded)
        else:
            positions.append(loaded)
    positions.sort(key=lambda position: position.opened_at)
    return positions, skipped


def entry_index(cols: Cols, opened_at: datetime) -> Optional[int]:
    """The index of the first full 1m bar after the minute of ``opened_at`` (the walk starts there: the real fill came
    inside the previous minute), or ``None`` when the cache lacks that bar."""
    target = int(opened_at.timestamp()) // 60 * INTERVAL_1M + INTERVAL_1M
    i = bisect_left(cols.open_ms, target)
    return i if i < len(cols.open_ms) and cols.open_ms[i] == target else None


class Delta(NamedTuple):
    """A variant's net minus the live rules' net for one trade (USDT), and whether its exit differs from the live one."""

    usdt: float
    changed: bool


@dataclass(frozen=True)
class Simulated:
    """One trade walked under the live rules and every variant: ``deltas`` is keyed by (variant name, convention name).

    ``plan_tp_binds`` is True when the plan's own take-profit lies nearer than the net floor, so that was the live target."""

    position: Position
    baseline_reason: str
    baseline_net: float
    deltas: Mapping[tuple[str, str], Delta]
    plan_tp_binds: bool = False


def _plan_target_net(position: Position, rules: LiveRules) -> float:
    """The net the plan's own take-profit pays: ``tp_pct`` away from the fill.

    Never below ``_MIN_TARGET_NET``: the engine holds a take-profit whose gross does not cover the fee, so it can only
    trigger once the net is above zero."""
    price = position.entry_fill * (1 + position.side * position.tp_pct / 100)
    net = net_for_price(position.entry_fill, position.side, position.notional, rules.fee_pct, price)
    return max(net, _MIN_TARGET_NET)


def _rule_for(variant: Variant, convention: Convention, rules: LiveRules, plan_net: float) -> ExitRule:
    """The variant under the convention. The target stays the nearer of its floor and the plan's own take-profit."""
    floor = rules.floor_net if variant.tp_net is None else variant.tp_net
    return ExitRule(
        name=f"{variant.name} [{convention.name}]",
        tp_net=min(floor, plan_net),
        limit_min=rules.hold_minutes - 1,  # the walk starts a minute after the real entry
        lock_steps=variant.lock_steps,
        trail_arm=variant.trail_arm,
        trail_dist=variant.trail_dist,
        same_bar=convention.same_bar,
        close_check=convention.close_check,
    )


def _covers(cols: Cols, start: int, limit: int) -> bool:
    """Whether the cache holds every minute from the entry bar to the time-exit bar, ``limit`` bars on.

    The whole window is required, not just the bars up to the exit this walk happens to take: keeping the trades whose
    walk ends inside a cache that stops short would keep the early exits and drop the long holds. Every step must be
    exactly one minute, as the walk itself requires: two ends a full window apart do not show a duplicate bar plus a hole."""
    end = start + limit
    return end < len(cols.open_ms) and all(
        cols.open_ms[i + 1] - cols.open_ms[i] == INTERVAL_1M for i in range(start, end)
    )


def _must_walk(label: str, cols: Cols, start: int, args: tuple, rule: ExitRule) -> Outcome:
    outcome = walk(cols, start, *args, rule)
    if outcome is None:  # the window was checked to be covered, so a walk always ends inside it
        raise RuntimeError(f"{label} ran out of covered bars")
    return outcome


def _simulate_position(position: Position, cols: Cols, rules: LiveRules) -> Optional[Simulated]:
    limit = rules.hold_minutes - 1
    start = entry_index(cols, position.opened_at)
    if start is None or not _covers(cols, start, limit):
        return None
    args = (position.side, position.entry_fill, position.sl_pct, position.notional, rules.fee_pct)
    plan_net = _plan_target_net(position, rules)
    base = _must_walk("the live walk", cols, start, args, ExitRule("live", tp_net=min(rules.floor_net, plan_net), limit_min=limit))
    base_net = settle(position.entry_fill, position.side, base, start, position.notional, rules.fee_pct, LIVE_COSTS).net
    base_exit = (base.reason, base.idx, base.raw_price)
    deltas: dict[tuple[str, str], Delta] = {}
    for variant in VARIANTS:
        for convention in CONVENTIONS:
            label = f"{variant.name} [{convention.name}]"
            outcome = _must_walk(label, cols, start, args, _rule_for(variant, convention, rules, plan_net))
            costs = LIVE_COSTS._replace(lock_extra_bps=convention.lock_extra_bps)
            net = settle(position.entry_fill, position.side, outcome, start, position.notional, rules.fee_pct, costs).net
            changed = (outcome.reason, outcome.idx, outcome.raw_price) != base_exit
            deltas[(variant.name, convention.name)] = Delta(net - base_net, changed)
    return Simulated(position, base.reason, base_net, deltas, plan_tp_binds=plan_net < rules.floor_net)


def simulate(
    positions: Sequence[Position], cols: Cols, rules: LiveRules
) -> tuple[list[Simulated], list[Skip]]:
    """Walk every position under the live rules and each variant; positions whose candles are incomplete are skipped."""
    simulated: list[Simulated] = []
    skipped: list[Skip] = []
    for position in positions:
        result = _simulate_position(position, cols, rules)
        if result is None:
            skipped.append(Skip(position.opened_at, _side_name(position.side), SKIP_CANDLES))
        else:
            simulated.append(result)
    return simulated, skipped


@dataclass(frozen=True)
class Study:
    rows_read: int
    simulated: tuple[Simulated, ...]
    skipped: tuple[Skip, ...]


def run_study(
    trade_rows: Sequence[Mapping[str, Any]],
    decision_rows: Iterable[Mapping[str, Any]],
    cols: Cols,
    rules: LiveRules,
) -> Study:
    """Closed-trade rows and OPEN decision rows plus 1m candles in; the paired simulation and every skip out."""
    positions, unusable = load_positions(trade_rows, DecisionIndex(decision_rows), rules)
    simulated, no_candles = simulate(positions, cols, rules)
    return Study(len(trade_rows), tuple(simulated), tuple([*unusable, *no_candles]))


@dataclass(frozen=True)
class Calibration:
    """How many simulated live walks ended for the reason the real trade ended."""

    matches: int
    total: int

    @property
    def ratio(self) -> Optional[float]:
        return self.matches / self.total if self.total else None


def calibration(simulated: Sequence[Simulated]) -> Calibration:
    matches = sum(1 for sim in simulated if sim.baseline_reason == sim.position.actual_exit)
    return Calibration(matches, len(simulated))


def forward_subset(simulated: Sequence[Simulated]) -> list[Simulated]:
    """The trades opened since the variants were fixed (``FORWARD_FROM``): the only ones the decision rule may count."""
    return [sim for sim in simulated if sim.position.opened_at >= FORWARD_FROM]


def calm_subset(simulated: Sequence[Simulated]) -> list[Simulated]:
    """The trades opened in a calm market: ``setup.atr_pct`` below ``CALM_ATR_PCT``."""
    return [sim for sim in simulated if sim.position.atr_pct < CALM_ATR_PCT]


def iso_week(moment: datetime) -> str:
    """The ISO week of ``moment`` in WIB as ``YYYY-Www`` (the ISO year, which sorts in time order)."""
    year, week, _ = moment.astimezone(WIB).isocalendar()
    return f"{year}-W{week:02d}"


class WeekDelta(NamedTuple):
    week: str
    n: int
    mean: float


@dataclass(frozen=True)
class Summary:
    """The paired differences of one variant under one convention over a set of trades (USDT per trade).

    ``t`` is the mean over its standard error (``None`` without two trades or without spread); ``per_million`` is
    1e6 x the summed difference over the real traded volume; ``changed`` counts the trades whose exit differs from the
    live one; ``interval`` is the 95 % day-bootstrap interval of the mean (``None`` under ``MIN_BOOTSTRAP_DAYS`` days);
    ``weeks`` are the WIB ISO weeks in time order."""

    n: int
    mean: float
    se: float
    t: Optional[float]
    per_million: Optional[float]
    changed: int
    interval: Optional[tuple[float, float]]
    weeks: tuple[WeekDelta, ...]


def summarize(simulated: Sequence[Simulated], variant: str, convention: str) -> Summary:
    key = (variant, convention)
    deltas = [sim.deltas[key].usdt for sim in simulated]
    mean, se = mean_se(deltas)
    volume = sum(sim.position.volume_usdt for sim in simulated)
    by_day = group_by_day(deltas, [sim.position.closed_at.astimezone(WIB).date().isoformat() for sim in simulated])
    by_week = group_by_day(deltas, [iso_week(sim.position.closed_at) for sim in simulated])
    _, low, high = day_bootstrap(by_day) if len(by_day) >= MIN_BOOTSTRAP_DAYS else (None, None, None)
    return Summary(
        n=len(deltas),
        mean=mean,
        se=se,
        t=mean / se if len(deltas) >= 2 and se > 0 else None,
        per_million=PER_MILLION * sum(deltas) / volume if volume > 0 else None,
        changed=sum(1 for sim in simulated if sim.deltas[key].changed),
        interval=None if low is None or high is None else (low, high),
        weeks=tuple(WeekDelta(week, len(values), sum(values) / len(values)) for week, values in sorted(by_week.items())),
    )


@dataclass(frozen=True)
class Criterion:
    """One line of the decision rule: ``value`` against ``needed``. For the weeks it is the count of positive weeks among
    the ``of`` last ones."""

    name: str
    value: Optional[float]
    needed: float
    met: bool
    of: Optional[int] = None


@dataclass(frozen=True)
class Verdict:
    candidate: str
    criteria: tuple[Criterion, ...]
    passed: bool


def evaluate_rule(candidate: Candidate, summary: Summary, calibration_ratio: Optional[float]) -> Verdict:
    """The pre-registered decision rule for one candidate, as data; the report prints it as text and never acts on it.

    ``summary`` is the primary variant under ``DECISION_CONVENTION`` over the candidate's trades. All five criteria must
    hold: enough trades, a big enough mean difference, ``t`` of at least ``MIN_T``, a positive weekly mean in at least
    ``WEEKS_REQUIRED`` of the last ``WEEKS_CONSIDERED`` weeks (fewer weeks than that cannot show it) and a calibration
    of at least ``MIN_CALIBRATION``."""
    last = summary.weeks[-WEEKS_CONSIDERED:]
    positive = sum(1 for week in last if week.mean > 0)
    criteria = (
        Criterion("n", summary.n, candidate.min_trades, summary.n >= candidate.min_trades),
        Criterion("mean", summary.mean, candidate.min_mean, summary.mean >= candidate.min_mean),
        Criterion("t", summary.t, MIN_T, summary.t is not None and summary.t >= MIN_T),
        Criterion(
            "weeks", positive, WEEKS_REQUIRED, len(last) >= WEEKS_CONSIDERED and positive >= WEEKS_REQUIRED, len(last)
        ),
        Criterion(
            "calibration",
            calibration_ratio,
            MIN_CALIBRATION,
            calibration_ratio is not None and calibration_ratio >= MIN_CALIBRATION,
        ),
    )
    return Verdict(candidate.name, criteria, all(criterion.met for criterion in criteria))
