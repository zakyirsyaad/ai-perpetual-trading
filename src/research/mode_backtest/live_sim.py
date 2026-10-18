"""Mode L's 1m exit walk, the money of its trades and its event loop.

Research code; the bot never imports it. ``resolve_live_exit`` is the walk of ``simulate.resolve_exit``
with the loss cut of version (ii) added; ``settle_live`` is ``simulate.settle`` with the slippage
chosen by the kind of exit. Both take plain numbers and bars and read no clock, file or setting.
``simulate_live_run`` drives them over the closed 3m candles of one segment, for mode L (LONG in
NAIK) and for comparator S (the same rule, SHORT in TURUN); see "The event loop" at the end.

The walk (spec section 4). Order inside one bar:

1. the time exit: the bar ``limit_min`` minutes after the entry bar exits at its open, before any
   of its levels are looked at;
2. open beyond the stop: SL at the open (``gapped``);
3. open beyond the active cut: POTONG at the open (``gapped``);
4. open beyond the take-profit: TP at the open (``gapped``);
5. the bar's worst price through the stop: SL at the stop price;
6. else through the active cut: POTONG at the cut price;
7. else the bar's best price through the take-profit: TP at the take-profit price.

A loss level (SL or POTONG) therefore wins a bar that also reached the take-profit, because a 1m bar
does not say which came first; ``both_touched`` records that case. The cut is active on the bars
with index ``entry_idx + cut_from_min`` or later, and only when ``levels.cut`` is not ``None``
(version i has none). The walk needs every minute: a bar that is not one minute after the one
before it ends the walk as the end of the data does, with ``None``.

Money (spec section 5): the entry fills ``entry_bps`` against the position at the entry open; a take-profit
exit uses ``slippage_tp_bps`` (0 when the target rests as a limit order, so it fills at its price) and
every other exit uses ``slippage_exit_market_bps``.

The event loop (spec sections 4 and 6), the loop of ``simulate.simulate_run`` with the pieces of
mode L:

* The clock is the close ``t`` of each 3m candle in ``[start_ms, end_ms)``. A candle signals when
  its stretch is within ``maks_jarak_ke_ema_kali_atr`` of the mean, on either side of it
  (``NearMeanTrigger``); a flat window has no stretch and so no signal and no levels.
* A signal meets the seven gates of Phase 1 in Phase 1's order (``simulate.Gates``), with mode L's
  time limit (``batas_waktu_menit``) as the segment edge. The condition it needs is NAIK for a LONG
  and TURUN for a SHORT.
* A signal that passes opens at the open of the 1m bar at ``t``. The levels come from the entry
  fill and the ATR percent of the signal candle (``PairFrames.atr_pct_3m``): the stop, the
  take-profit and, for version (ii), the cut. Then the walk and the money.
* The frames must carry Mode D's window and periods, which mode L's trigger repeats
  (``check_live_trigger_matches``), and a column of ATR percents, one per 3m candle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bot.strategy.modes.checks import (
    LONG,
    SHORT,
    check_side,
    is_whole,
    require_not_negative,
    require_positive,
)
from bot.strategy.modes.config import (
    CostConfig,
    LiveExitConfig,
    ModesConfig,
    check_live_trigger_matches,
)
from bot.strategy.modes.live_exit import LiveExitLevels, live_exit_levels
from bot.strategy.modes.position_rules import adverse_fill
from bot.strategy.modes.regime import NAIK, TURUN
from research.pine_gate.bars import Bars, entry_index_for, interval_to_ms
from research.pine_gate.features import INTERVAL_1M

from .frames import HourlyContext, PairFrames
from .simulate import (
    EXIT_POTONG,
    EXIT_SL,
    EXIT_TP,
    EXIT_WAKTU,
    BLOCK_REASONS,
    Book,
    ExitOutcome,
    Gates,
    Money,
    NearMeanTrigger,
    RunResult,
    Trade,
    candle_closes,
    check_column,
    check_inputs,
    check_walk,
    moment,
    outcome_exit_ms,
    reaches_sl,
    reaches_tp,
)

__all__ = [
    "BLOCK_REASONS",
    "MODE_L",
    "MODE_S",
    "LiveRunSpec",
    "resolve_live_exit",
    "settle_live",
    "simulate_live_run",
]

MODE_L = "L"  # the near-mean LONG with the live bot's exits
MODE_S = "S"  # the comparator: the same rule as SHORT
VERSION_I = "i"  # the live exits: stop and take-profit
VERSION_II = "ii"  # version (i) plus the loss cut
_VERSIONS = (VERSION_I, VERSION_II)
_EXIT_REASONS = (EXIT_TP, EXIT_SL, EXIT_POTONG, EXIT_WAKTU)
_GATE_OF_SIDE = {LONG: NAIK, SHORT: TURUN}
_BPS_PER_PERCENT = 100.0  # 1 percent = 100 bps


def _check_cut(side: int, levels: LiveExitLevels, cut_from_min: int) -> None:
    if not is_whole(cut_from_min) or cut_from_min < 1:
        raise ValueError(
            f"cut_from_min must be a whole number of minutes of at least 1, got {cut_from_min!r}"
        )
    if levels.cut is None:
        return
    require_positive("cut", levels.cut)
    # the cut lies between the stop and the take-profit, on the stop's side of the entry
    between = levels.sl < levels.cut < levels.tp if side == LONG else levels.tp < levels.cut < levels.sl
    if not between:
        raise ValueError(
            f"the cut must lie between the stop-loss and the take-profit, got sl {levels.sl!r}, "
            f"cut {levels.cut!r}, tp {levels.tp!r}"
        )


def resolve_live_exit(
    bars_1m: Bars,
    entry_idx: int,
    side: int,
    levels: LiveExitLevels,
    cut_from_min: int,
    limit_min: int,
) -> Optional[ExitOutcome]:
    """How the mode L position that entered at the open of bar ``entry_idx`` ends, or ``None``.

    ``levels`` carries the stop, take-profit and (version ii) cut prices; ``cut_from_min`` is the
    minute from which the cut acts and ``limit_min`` the time limit. The walk goes bar by bar in the
    order of the module docstring and stops at the first bar that decides. It returns ``None`` when
    the data ends first, which includes a hole in the minutes and a missing time-exit bar.

    Raises ``ValueError`` for bars that are not 1m, a ``side`` that is not ``LONG`` or ``SHORT``, an
    ``entry_idx`` outside the bars, a ``limit_min`` or ``cut_from_min`` that is not a whole number
    of at least 1, and levels that are not finite positive numbers or are not in order (the cut
    between the stop and the take-profit)."""
    check_walk(bars_1m, entry_idx, side, levels.tp, levels.sl, limit_min)
    _check_cut(side, levels, cut_from_min)
    sl, tp, cut = levels.sl, levels.tp, levels.cut
    cut_idx = entry_idx + cut_from_min
    limit_idx = entry_idx + limit_min
    open_ms, opens, highs, lows = bars_1m.open_ms, bars_1m.open, bars_1m.high, bars_1m.low
    for i in range(entry_idx, min(limit_idx, len(bars_1m) - 1) + 1):
        if i > entry_idx and open_ms[i] != open_ms[i - 1] + INTERVAL_1M:
            return None
        bar_open = opens[i]
        if i == limit_idx:
            return ExitOutcome(EXIT_WAKTU, i, bar_open, False, False)
        active_cut = cut if cut is not None and i >= cut_idx else None
        if reaches_sl(side, bar_open, sl):
            return ExitOutcome(EXIT_SL, i, bar_open, True, False)
        if active_cut is not None and reaches_sl(side, bar_open, active_cut):
            return ExitOutcome(EXIT_POTONG, i, bar_open, True, False)
        if reaches_tp(side, bar_open, tp):
            return ExitOutcome(EXIT_TP, i, bar_open, True, False)
        worst, best = (lows[i], highs[i]) if side == LONG else (highs[i], lows[i])
        if reaches_sl(side, worst, sl):
            return ExitOutcome(EXIT_SL, i, float(sl), False, reaches_tp(side, best, tp))
        if active_cut is not None and reaches_sl(side, worst, active_cut):
            return ExitOutcome(EXIT_POTONG, i, float(active_cut), False, reaches_tp(side, best, tp))
        if reaches_tp(side, best, tp):
            return ExitOutcome(EXIT_TP, i, float(tp), False, False)
    return None


def settle_live(
    entry_open: float,
    outcome: ExitOutcome,
    side: int,
    notional: float,
    fee_pct: float,
    costs: CostConfig,
) -> Money:
    """The money of a mode L trade that entered at ``entry_open`` and ended as ``outcome``.

    ``fee_pct`` is a percent (0.035 in the document) and ``notional`` the position size in USDT at
    the entry fill. With ``entry_fill = adverse_fill(entry_open, side, entry_bps / 100, closing=False)``
    and ``exit_fill = adverse_fill(outcome.raw_price, side, bps / 100, closing=True)``, where ``bps``
    is ``slippage_tp_bps`` for a take-profit and ``slippage_exit_market_bps`` for every other exit:

    * ``qty = notional / entry_fill``;
    * ``volume = qty x entry_fill + qty x exit_fill``;
    * ``fee = fee_pct / 100 x volume``;
    * ``pnl = side x qty x (exit_fill - entry_fill) - fee``;
    * ``slippage = qty x |entry_fill - entry_open| + qty x |raw - exit_fill|``.

    Raises ``ValueError`` for a ``side`` that is not ``LONG`` or ``SHORT``, an ``outcome.reason`` that
    is not TP, SL, POTONG or WAKTU, a price that is not a finite number above 0, a ``notional`` that
    is not a finite number above 0, and a ``fee_pct`` or slippage that is not a finite number of at
    least 0."""
    check_side(side)
    if outcome.reason not in _EXIT_REASONS:
        raise ValueError(
            f"outcome.reason must be one of {', '.join(_EXIT_REASONS)}, got {outcome.reason!r}"
        )
    require_positive("notional", notional)
    require_not_negative("fee_pct", fee_pct)
    exit_bps = costs.slippage_tp_bps if outcome.reason == EXIT_TP else costs.slippage_exit_market_bps
    raw_exit = outcome.raw_price
    entry_fill = adverse_fill(entry_open, side, costs.slippage_entry_bps / _BPS_PER_PERCENT, False)
    exit_fill = adverse_fill(raw_exit, side, exit_bps / _BPS_PER_PERCENT, True)
    qty = notional / entry_fill
    volume = qty * entry_fill + qty * exit_fill
    fee = fee_pct / 100 * volume
    pnl = side * qty * (exit_fill - entry_fill) - fee
    slippage = qty * abs(entry_fill - entry_open) + qty * abs(raw_exit - exit_fill)
    return Money(entry_fill, exit_fill, qty, fee, slippage, pnl, volume)


@dataclass(frozen=True)
class LiveRunSpec:
    """One run of the mode L loop: ``side`` on ``pair`` over the segment ``[start_ms, end_ms)``.

    ``side`` is ``LONG`` for mode L, which needs NAIK, or ``SHORT`` for comparator S, which needs
    TURUN; ``kondisi_gate`` repeats that condition and must match the side. ``versi`` is ``"i"``
    (stop and take-profit) or ``"ii"`` (with the loss cut). The times are UTC milliseconds and the
    end is not part of the segment. Anything else raises ``ValueError``, as do times that are not
    whole numbers and an end before the start; a start equal to the end is an empty run."""

    pair: str
    side: int
    kondisi_gate: str
    versi: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        check_side(self.side)
        if self.kondisi_gate != _GATE_OF_SIDE[self.side]:
            raise ValueError(
                f"a {'LONG' if self.side == LONG else 'SHORT'} runs in "
                f"{_GATE_OF_SIDE[self.side]}, got {self.kondisi_gate!r}"
            )
        if self.versi not in _VERSIONS:
            raise ValueError(f"versi must be one of {', '.join(_VERSIONS)}, got {self.versi!r}")
        if not (is_whole(self.start_ms) and is_whole(self.end_ms)):
            raise ValueError(
                f"start_ms and end_ms must be whole milliseconds, got {self.start_ms!r} and "
                f"{self.end_ms!r}"
            )
        if self.end_ms < self.start_ms:
            raise ValueError(f"end_ms {self.end_ms} is before start_ms {self.start_ms}")


def _check_live_frames(frames: PairFrames, cfg: ModesConfig, live: LiveExitConfig, run: LiveRunSpec) -> float:
    """Refuse frames or configs that do not fit the run; returns the pair's ``faktor_k``."""
    check_live_trigger_matches(live, cfg)
    check_inputs(frames, cfg, run.pair)
    faktor_k = cfg.pair[run.pair].faktor_k
    if faktor_k is None:
        raise ValueError(
            f"pair.{run.pair}.faktor_k is 'otomatis', which Phase 1 cannot compute: "
            "the pair cannot run"
        )
    if faktor_k != 1.0:
        raise ValueError(
            f"pair.{run.pair}.faktor_k is {faktor_k!r}: mode L does not scale its levels by "
            "faktor_k, so only 1.0 is accepted"
        )
    interval_ms = interval_to_ms(live.mode_l.tf_pemicu)
    check_column(frames.stretch_bars, frames.stretch, interval_ms, "3m candles", "stretch")
    check_column(frames.stretch_bars, frames.atr_pct_3m, interval_ms, "3m candles", "ATR percent")
    return faktor_k


def _open_live_position(
    frames: PairFrames,
    cfg: ModesConfig,
    live: LiveExitConfig,
    run: LiveRunSpec,
    faktor_k: float,
    atr_pct: float,
    t_ms: int,
    context: HourlyContext,
) -> Trade:
    """The trade of a signal at ``t_ms`` that passed every gate; it never comes back empty."""
    bars_1m, umum, rules, costs, side = frames.bars_1m, cfg.umum, live.mode_l, live.biaya, run.side
    idx = entry_index_for(bars_1m, t_ms)
    if idx is None or bars_1m.open_ms[idx] != t_ms:
        raise RuntimeError(f"no 1m bar opens at the signal at {moment(t_ms)}")
    entry_open = bars_1m.open[idx]
    entry_fill = adverse_fill(entry_open, side, costs.slippage_entry_bps / _BPS_PER_PERCENT, False)
    levels = live_exit_levels(
        entry_fill, side, atr_pct, umum.notional_usdt, rules, run.versi == VERSION_II
    )
    outcome = resolve_live_exit(
        bars_1m, idx, side, levels, rules.potong_rugi_setelah_menit, rules.batas_waktu_menit
    )
    if outcome is None:
        raise RuntimeError(
            f"the position of the signal at {moment(t_ms)} does not end inside the 1m bars: a "
            "minute is missing or the data ends before its exit"
        )
    money = settle_live(entry_open, outcome, side, umum.notional_usdt, umum.fee_taker_persen, costs)
    exit_ms = outcome_exit_ms(bars_1m, outcome)
    return _live_trade(run, faktor_k, t_ms, context, levels, outcome, exit_ms, money, umum.notional_usdt)


def _live_trade(
    run: LiveRunSpec,
    faktor_k: float,
    t_ms: int,
    context: HourlyContext,
    levels: LiveExitLevels,
    outcome: ExitOutcome,
    exit_ms: int,
    money: Money,
    notional: float,
) -> Trade:
    """The ``Trade`` record of one settled mode L or comparator S position."""
    return Trade(
        mode=MODE_L if run.side == LONG else MODE_S,
        pair=run.pair,
        side=run.side,
        kondisi=context.kondisi,
        context=context,
        faktor_k=faktor_k,
        signal_ms=t_ms,
        entry_ms=t_ms,
        entry_fill=money.entry_fill,
        tp=levels.tp,
        sl=levels.sl,
        exit_ms=exit_ms,
        exit_fill=money.exit_fill,
        exit_reason=outcome.reason,
        gapped=outcome.gapped,
        both_touched=outcome.both_touched,
        qty=money.qty,
        notional_usdt=notional,
        fee_usdt=money.fee_usdt,
        slippage_usdt=money.slippage_usdt,
        pnl_usdt=money.pnl_usdt,
        volume_usdt=money.volume_usdt,
        versi=run.versi,
        sl_pct=levels.sl_pct,
        potong=levels.cut,
    )


def simulate_live_run(
    frames: PairFrames, cfg: ModesConfig, live: LiveExitConfig, run: LiveRunSpec
) -> RunResult:
    """Simulate mode L (LONG) or comparator S (SHORT) on the frames of ``run.pair``.

    ``cfg`` is ``config/modes.yaml`` (the general rules and the periods the frames were built
    with) and ``live`` is ``config/modes_live_exit.yaml`` (the trigger band, the exits and the
    costs). At each 3m close inside the run the trigger reads the candle's stretch; a signal meets
    the gates in Phase 1's order and one that passes opens a position whose whole life is read from
    the 1m bars before the next candle (see "The event loop" above). The trades have ``mode`` "L"
    for a LONG and "S" for a SHORT, with ``versi``, ``sl_pct`` and ``potong`` set.

    Raises ``ValueError`` when the frames or the configs do not fit the run: a trigger in
    ``live`` that differs from Mode D's (``ModesConfigError``), frames of another pair or built
    with other periods, more than one position per pair, an ``otomatis`` ``faktor_k`` or one other than 1.0, bars of the
    wrong interval, and a stretch or ATR percent column that does not match the 3m candles (an
    empty ``atr_pct_3m`` included). Raises ``RuntimeError`` for a hole in the data the run needs: a
    missing 3m candle, a signal without an ATR percent, no 1m bar opening at a signal that passed
    every gate, or a walk that cannot end."""
    faktor_k = _check_live_frames(frames, cfg, live, run)
    rules = live.mode_l
    trigger = NearMeanTrigger(
        frames.stretch_bars, frames.stretch, frames.hourly, rules.maks_jarak_ke_ema_kali_atr
    )
    gates = Gates(cfg.umum, run.kondisi_gate, run.end_ms, rules.batas_waktu_menit)
    book = Book()
    for i, t_ms in candle_closes(trigger.bars, run.start_ms, run.end_ms):
        context = trigger.signal_at(i, t_ms)
        if context is None:
            continue
        atr_pct = frames.atr_pct_3m[i]
        if atr_pct is None:
            raise RuntimeError(
                f"the 3m candle closing at {moment(t_ms)} has a stretch but no ATR percent: the "
                "frames are inconsistent"
            )
        reason = gates.first_failure(t_ms, context, book)
        if reason is not None:
            book.block(reason)
            continue
        book.enter(_open_live_position(frames, cfg, live, run, faktor_k, atr_pct, t_ms, context))
    return book.result()
