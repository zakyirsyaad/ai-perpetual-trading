from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import product
from statistics import mean
from typing import Any, Iterable, Sequence

from bot.perps.config import PerpsConfig
from bot.perps.exchange_adapter import InstrumentMetadata, MarketSnapshot
from bot.perps.risk import PerpsRiskEngine
from bot.perps.signal import NormalizedSignal


@dataclass(frozen=True, slots=True)
class BacktestCandle:
    timestamp: datetime
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bid: float | None = None
    ask: float | None = None
    funding_rate: float = 0.0

    def snapshot(self, next_funding_time: datetime | None = None) -> MarketSnapshot:
        spread = max(self.close * 0.0001, 0.01)
        bid = self.bid if self.bid is not None else self.close - spread / 2.0
        ask = self.ask if self.ask is not None else self.close + spread / 2.0
        return MarketSnapshot(
            symbol=self.symbol,
            bid=bid,
            ask=ask,
            last=self.close,
            mark=self.close,
            timestamp=self.timestamp,
            next_funding_time=next_funding_time,
            funding_rate=self.funding_rate,
        )


@dataclass(frozen=True, slots=True)
class BacktestSignal:
    timestamp: datetime
    signal: NormalizedSignal


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    timeframe: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    gross_pnl: Decimal
    open_fee: Decimal
    close_fee: Decimal
    funding: Decimal
    slippage: Decimal
    net_pnl: Decimal
    reason: str
    fill_ratio: float
    entry_slippage_bps: float
    exit_slippage_bps: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_trades: int
    net_pnl: Decimal
    win_rate: float
    average_net_win: Decimal
    average_net_loss: Decimal
    expectancy_per_trade: Decimal
    profit_factor: float
    max_drawdown: Decimal
    fee_as_pct_of_gross_pnl: float
    fill_ratio: float
    slippage_distribution_bps: dict[str, float]
    pnl_by_symbol_timeframe: dict[str, Decimal]
    trades: tuple[BacktestTrade, ...]


@dataclass(frozen=True, slots=True)
class SweepResult:
    params: dict[str, Any]
    metrics: BacktestMetrics


class ConservativePerpsBacktester:
    """Conservative event-driven/hybrid backtester for perpetual futures.

    It uses the signal timestamp as the decision event, simulates maker-first
    entry at bid/ask with timeout/partial-fill assumptions, then walks future
    candles conservatively. If TP and SL both occur inside one candle, SL wins.
    """

    def __init__(
        self,
        config: PerpsConfig,
        instruments: dict[str, InstrumentMetadata] | None = None,
    ) -> None:
        self._config = config
        self._risk = PerpsRiskEngine(config)
        self._instruments = instruments or self._default_instruments()

    def run(self, candles: Sequence[BacktestCandle], signals: Sequence[BacktestSignal]) -> BacktestMetrics:
        candle_map = {(c.symbol, c.timeframe, c.timestamp): c for c in candles}
        by_symbol_timeframe: dict[tuple[str, str], list[BacktestCandle]] = defaultdict(list)
        for candle in sorted(candles, key=lambda item: item.timestamp):
            by_symbol_timeframe[(candle.symbol, candle.timeframe)].append(candle)

        equity = Decimal(str(self._config.starting_equity_usdt))
        open_until: datetime | None = None
        trades: list[BacktestTrade] = []

        for signal_event in sorted(signals, key=lambda item: item.timestamp):
            signal = signal_event.signal
            if signal.side == "flat" or signal.confidence <= 0:
                continue
            if open_until is not None and signal_event.timestamp < open_until:
                continue
            candle = candle_map.get((signal.symbol, signal.timeframe, signal_event.timestamp))
            if candle is None:
                continue
            next_funding = self._next_funding_time(candle.timestamp)
            snapshot = candle.snapshot(next_funding)
            if snapshot.spread_bps > self._config.max_spread_bps:
                continue

            instrument = self._instruments[signal.symbol]
            sizing = self._risk.size_order(
                signal=signal,
                instrument=instrument,
                snapshot=snapshot,
                equity_usdt=equity,
                requested_leverage=self._config.default_leverage,
            )
            if not sizing.accepted:
                continue

            fill_ratio = self._maker_fill_ratio(snapshot)
            if fill_ratio <= 0:
                continue
            qty = instrument.round_qty(sizing.qty * Decimal(str(fill_ratio)))
            if qty <= 0:
                continue
            future = self._future_candles(
                by_symbol_timeframe[(signal.symbol, signal.timeframe)],
                signal_event.timestamp,
                signal.max_hold_minutes,
            )
            if not future:
                continue

            trade = self._simulate_trade(signal, candle, future, qty, fill_ratio)
            trades.append(trade)
            equity += trade.net_pnl
            open_until = trade.exit_time

        return self._metrics(tuple(trades))

    def sweep(
        self,
        candles: Sequence[BacktestCandle],
        signals: Sequence[BacktestSignal],
        parameter_grid: dict[str, Iterable[Any]],
    ) -> list[SweepResult]:
        keys = list(parameter_grid.keys())
        results: list[SweepResult] = []
        for values in product(*(parameter_grid[key] for key in keys)):
            params = dict(zip(keys, values))
            updated_config = replace(self._config, **{k: v for k, v in params.items() if hasattr(self._config, k)})
            filtered_signals = self._apply_signal_params(signals, params)
            metrics = ConservativePerpsBacktester(updated_config, self._instruments).run(candles, filtered_signals)
            results.append(SweepResult(params=params, metrics=metrics))
        return results

    def _simulate_trade(
        self,
        signal: NormalizedSignal,
        entry_candle: BacktestCandle,
        future: list[BacktestCandle],
        qty: Decimal,
        fill_ratio: float,
    ) -> BacktestTrade:
        entry_snapshot = entry_candle.snapshot()
        entry_base = Decimal(str(entry_snapshot.bid if signal.side == "long" else entry_snapshot.ask))
        half_slippage = Decimal(str(self._config.slippage_round_trip / 2.0))
        if signal.side == "long":
            entry_price = entry_base * (Decimal("1") + half_slippage)
            stop_price = entry_price * (Decimal("1") - Decimal(str(signal.stop_bps / 10_000.0)))
            take_price = entry_price * (Decimal("1") + Decimal(str(signal.take_bps / 10_000.0)))
        else:
            entry_price = entry_base * (Decimal("1") - half_slippage)
            stop_price = entry_price * (Decimal("1") + Decimal(str(signal.stop_bps / 10_000.0)))
            take_price = entry_price * (Decimal("1") - Decimal(str(signal.take_bps / 10_000.0)))

        exit_price = Decimal(str(future[-1].close))
        exit_time = future[-1].timestamp
        reason = "time_stop"

        for candle in future:
            if signal.side == "long":
                if Decimal(str(candle.low)) <= stop_price:
                    exit_price, exit_time, reason = stop_price, candle.timestamp, "stop_loss"
                    break
                if Decimal(str(candle.high)) >= take_price:
                    exit_price, exit_time, reason = take_price, candle.timestamp, "take_profit"
                    break
            else:
                if Decimal(str(candle.high)) >= stop_price:
                    exit_price, exit_time, reason = stop_price, candle.timestamp, "stop_loss"
                    break
                if Decimal(str(candle.low)) <= take_price:
                    exit_price, exit_time, reason = take_price, candle.timestamp, "take_profit"
                    break

        if signal.side == "long":
            exit_exec_price = exit_price * (Decimal("1") - half_slippage)
            gross = (exit_exec_price - entry_price) * qty
        else:
            exit_exec_price = exit_price * (Decimal("1") + half_slippage)
            gross = (entry_price - exit_exec_price) * qty

        open_fee = abs(entry_price * qty * Decimal(str(self._config.fee_round_trip / 2.0)))
        close_fee = abs(exit_exec_price * qty * Decimal(str(self._config.fee_round_trip / 2.0)))
        funding = self._funding_cost(entry_time=entry_candle.timestamp, exit_time=exit_time, notional=entry_price * qty)
        slippage = abs((entry_price - entry_base) * qty) + abs((exit_exec_price - exit_price) * qty)
        net = gross - open_fee - close_fee - funding

        return BacktestTrade(
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            side=signal.side,
            entry_time=entry_candle.timestamp,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_exec_price,
            qty=qty,
            gross_pnl=gross,
            open_fee=open_fee,
            close_fee=close_fee,
            funding=funding,
            slippage=slippage,
            net_pnl=net,
            reason=reason,
            fill_ratio=fill_ratio,
            entry_slippage_bps=float(half_slippage * Decimal("10000")),
            exit_slippage_bps=float(half_slippage * Decimal("10000")),
        )

    def _maker_fill_ratio(self, snapshot: MarketSnapshot) -> float:
        if snapshot.spread_bps <= self._config.max_spread_bps / 2.0:
            return self._config.maker_fill_ratio
        return min(self._config.maker_fill_ratio, self._config.partial_fill_ratio_on_timeout)

    def _future_candles(
        self,
        candles: list[BacktestCandle],
        timestamp: datetime,
        max_hold_minutes: int,
    ) -> list[BacktestCandle]:
        cutoff = timestamp + timedelta(minutes=max_hold_minutes)
        return [c for c in candles if timestamp < c.timestamp <= cutoff]

    def _funding_cost(self, *, entry_time: datetime, exit_time: datetime, notional: Decimal) -> Decimal:
        if exit_time <= entry_time:
            return Decimal("0")
        interval = timedelta(hours=self._config.funding_interval_hours)
        next_time = self._next_funding_time(entry_time)
        count = 0
        while next_time <= exit_time:
            count += 1
            next_time += interval
        # Conservative fallback until venue funding schedule/rates are wired.
        return notional * Decimal("0.0001") * Decimal(count)

    def _next_funding_time(self, timestamp: datetime) -> datetime:
        aware = timestamp.astimezone(timezone.utc)
        hour = (aware.hour // self._config.funding_interval_hours + 1) * self._config.funding_interval_hours
        day = aware.date()
        if hour >= 24:
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
        return datetime(day.year, day.month, day.day, hour=hour, tzinfo=timezone.utc)

    def _metrics(self, trades: tuple[BacktestTrade, ...]) -> BacktestMetrics:
        wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
        losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
        total_net = sum((trade.net_pnl for trade in trades), Decimal("0"))
        gross_profit = sum(wins, Decimal("0"))
        gross_loss = abs(sum(losses, Decimal("0")))
        total_fees = sum((trade.open_fee + trade.close_fee for trade in trades), Decimal("0"))
        total_gross_abs = abs(sum((trade.gross_pnl for trade in trades), Decimal("0")))

        equity = Decimal(str(self._config.starting_equity_usdt))
        peak = equity
        max_drawdown = Decimal("0")
        for trade in trades:
            equity += trade.net_pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        pnl_by: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for trade in trades:
            pnl_by[f"{trade.symbol}:{trade.timeframe}"] += trade.net_pnl

        slippages = [trade.entry_slippage_bps + trade.exit_slippage_bps for trade in trades]
        return BacktestMetrics(
            total_trades=len(trades),
            net_pnl=total_net,
            win_rate=(len(wins) / len(trades)) if trades else 0.0,
            average_net_win=(sum(wins, Decimal("0")) / Decimal(len(wins))) if wins else Decimal("0"),
            average_net_loss=(sum(losses, Decimal("0")) / Decimal(len(losses))) if losses else Decimal("0"),
            expectancy_per_trade=(total_net / Decimal(len(trades))) if trades else Decimal("0"),
            profit_factor=float(gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0,
            max_drawdown=max_drawdown,
            fee_as_pct_of_gross_pnl=float((total_fees / total_gross_abs) * Decimal("100")) if total_gross_abs > 0 else 0.0,
            fill_ratio=mean([trade.fill_ratio for trade in trades]) if trades else 0.0,
            slippage_distribution_bps={
                "min": min(slippages) if slippages else 0.0,
                "avg": mean(slippages) if slippages else 0.0,
                "max": max(slippages) if slippages else 0.0,
            },
            pnl_by_symbol_timeframe=dict(pnl_by),
            trades=trades,
        )

    @staticmethod
    def _apply_signal_params(signals: Sequence[BacktestSignal], params: dict[str, Any]) -> list[BacktestSignal]:
        confidence_threshold = params.get("confidence_threshold")
        expected_min = params.get("expected_move_bps_min")
        stop_bps = params.get("stop_bps")
        take_bps = params.get("take_bps")
        filtered: list[BacktestSignal] = []
        for event in signals:
            signal = event.signal
            if confidence_threshold is not None and signal.confidence < confidence_threshold:
                continue
            if expected_min is not None and signal.expected_move_bps < expected_min:
                continue
            if stop_bps is not None or take_bps is not None:
                signal = signal.model_copy(
                    update={
                        "stop_bps": stop_bps if stop_bps is not None else signal.stop_bps,
                        "take_bps": take_bps if take_bps is not None else signal.take_bps,
                    }
                )
            filtered.append(BacktestSignal(timestamp=event.timestamp, signal=signal))
        return filtered

    @staticmethod
    def _default_instruments() -> dict[str, InstrumentMetadata]:
        return {
            "BTC/USDT": InstrumentMetadata(
                symbol="BTC/USDT",
                base_asset="BTC",
                quote_asset="USDT",
                price_tick=Decimal("0.1"),
                qty_step=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5"),
                max_leverage=5,
            ),
            "ETH/USDT": InstrumentMetadata(
                symbol="ETH/USDT",
                base_asset="ETH",
                quote_asset="USDT",
                price_tick=Decimal("0.01"),
                qty_step=Decimal("0.0001"),
                min_qty=Decimal("0.0001"),
                min_notional=Decimal("5"),
                max_leverage=5,
            ),
        }
