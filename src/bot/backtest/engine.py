from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Settings
from bot.models import PositionDirection
from bot.strategy.volume_strategy import VolumeStrategy


@dataclass(slots=True)
class Candle:
    ts: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class SimulatedPosition:
    symbol: str
    direction: PositionDirection
    entry_price: float
    quantity: float
    stop_price: float
    take_price: float
    opened_at: datetime
    open_bar_index: int


@dataclass(slots=True)
class SimulatedTrade:
    symbol: str
    direction: PositionDirection
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    reason: str
    opened_at: datetime
    closed_at: datetime

    @property
    def notional_volume(self) -> float:
        return (self.entry_price * self.quantity) + (self.exit_price * self.quantity)


@dataclass(slots=True)
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    stop_losses: int
    total_pnl_usdt: float
    total_notional_volume: float
    trades: list[SimulatedTrade]


class VolumeBacktestEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._strategy = VolumeStrategy(settings)

    def run(
        self,
        csv_path: Path,
        max_total_losing_trades: int | None = None,
        max_total_trades: int | None = None,
    ) -> BacktestResult:
        candles = self._load_candles(csv_path)
        if not candles:
            raise ValueError("Backtest CSV has no rows.")

        timeline = self._build_timeline(candles)
        if len(timeline) < 2:
            raise ValueError("Backtest requires at least 2 timestamps.")

        timeframe_minutes = self._infer_timeframe_minutes(timeline)
        entry_interval_bars = max(1, round(self._settings.entry_interval_minutes / timeframe_minutes))
        hold_bars = max(1, round(self._settings.position_hold_minutes / timeframe_minutes))

        daily_trade_count = 0
        daily_stop_losses = 0
        daily_pnl_usdt = 0.0
        daily_consecutive_losses = 0
        total_losing_trades = 0
        current_day = timeline[0].date()
        next_entry_idx = 0
        open_position: SimulatedPosition | None = None
        trades: list[SimulatedTrade] = []

        for idx, ts in enumerate(timeline):
            if ts.date() != current_day:
                current_day = ts.date()
                daily_trade_count = 0
                daily_stop_losses = 0
                daily_pnl_usdt = 0.0
                daily_consecutive_losses = 0
                next_entry_idx = idx

            if open_position:
                candle = candles.get((open_position.symbol, ts))
                if candle:
                    closed_trade = self._try_close_position(
                        open_position=open_position,
                        candle=candle,
                        current_index=idx,
                        hold_bars=hold_bars,
                    )
                    if closed_trade:
                        finalized_trade = self._apply_total_loss_guard(
                            trade=closed_trade,
                            total_losing_trades=total_losing_trades,
                            max_total_losing_trades=max_total_losing_trades,
                        )
                        trades.append(finalized_trade)
                        daily_trade_count += 1
                        daily_pnl_usdt += finalized_trade.pnl_usdt
                        if finalized_trade.pnl_usdt < 0:
                            daily_stop_losses += 1
                            total_losing_trades += 1
                            daily_consecutive_losses += 1
                        else:
                            daily_consecutive_losses = 0
                        open_position = None
                        if max_total_trades is not None and len(trades) >= max_total_trades:
                            break

            can_open = (
                open_position is None
                and idx >= next_entry_idx
                and daily_trade_count < self._settings.trade_target_per_day
                and daily_stop_losses < self._settings.max_stop_loss_per_day
                and daily_pnl_usdt > -abs(self._settings.max_daily_drawdown_usdt)
                and daily_consecutive_losses < self._settings.max_consecutive_losses
                and (max_total_trades is None or len(trades) < max_total_trades)
            )
            if can_open:
                plan = self._strategy.next_trade()
                candle = candles.get((plan.symbol, ts))
                if candle:
                    open_position = self._open_simulated_position(plan, candle, idx)
                    next_entry_idx = idx + entry_interval_bars
        if max_total_trades is not None and len(trades) > max_total_trades:
            trades = trades[:max_total_trades]

        if open_position:
            last_candle = candles.get((open_position.symbol, timeline[-1]))
            if last_candle:
                if max_total_trades is None or len(trades) < max_total_trades:
                    trades.append(
                        self._close_trade(
                            position=open_position,
                            exit_price=last_candle.close,
                            reason="end_of_data",
                            closed_at=last_candle.ts,
                        )
                    )

        return self._summarize(trades)

    def _open_simulated_position(self, plan, candle: Candle, idx: int) -> SimulatedPosition:
        qty = self._calculate_quantity(plan.usdt_size, plan.leverage, candle.close)
        if plan.direction == PositionDirection.LONG:
            stop_price = candle.close * (1 - plan.stop_loss_pct / 100)
            take_price = candle.close * (1 + plan.take_profit_pct / 100)
        else:
            stop_price = candle.close * (1 + plan.stop_loss_pct / 100)
            take_price = candle.close * (1 - plan.take_profit_pct / 100)

        return SimulatedPosition(
            symbol=plan.symbol,
            direction=plan.direction,
            entry_price=candle.close,
            quantity=qty,
            stop_price=stop_price,
            take_price=take_price,
            opened_at=candle.ts,
            open_bar_index=idx,
        )

    def _try_close_position(
        self,
        open_position: SimulatedPosition,
        candle: Candle,
        current_index: int,
        hold_bars: int,
    ) -> SimulatedTrade | None:
        # Conservative execution: when SL and TP are both inside a candle, SL is assumed first.
        if open_position.direction == PositionDirection.LONG:
            if candle.low <= open_position.stop_price:
                return self._close_trade(open_position, open_position.stop_price, "stop_loss", candle.ts)
            if candle.high >= open_position.take_price:
                return self._close_trade(open_position, open_position.take_price, "take_profit", candle.ts)
        else:
            if candle.high >= open_position.stop_price:
                return self._close_trade(open_position, open_position.stop_price, "stop_loss", candle.ts)
            if candle.low <= open_position.take_price:
                return self._close_trade(open_position, open_position.take_price, "take_profit", candle.ts)

        if current_index - open_position.open_bar_index >= hold_bars:
            return self._close_trade(open_position, candle.close, "time_stop", candle.ts)
        return None

    def _close_trade(
        self,
        position: SimulatedPosition,
        exit_price: float,
        reason: str,
        closed_at: datetime,
    ) -> SimulatedTrade:
        gross_pnl = self._estimate_gross_pnl(
            position.direction, position.entry_price, exit_price, position.quantity
        )
        total_fee = self._estimate_total_taker_fee(
            position.entry_price,
            exit_price,
            position.quantity,
            self._settings.taker_fee_pct,
        )
        pnl = gross_pnl - total_fee
        return SimulatedTrade(
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            pnl_usdt=pnl,
            reason=reason,
            opened_at=position.opened_at,
            closed_at=closed_at,
        )

    @staticmethod
    def _apply_total_loss_guard(
        trade: SimulatedTrade,
        total_losing_trades: int,
        max_total_losing_trades: int | None,
    ) -> SimulatedTrade:
        if max_total_losing_trades is None:
            return trade
        if trade.pnl_usdt >= 0:
            return trade
        if total_losing_trades < max_total_losing_trades:
            return trade

        return SimulatedTrade(
            symbol=trade.symbol,
            direction=trade.direction,
            entry_price=trade.entry_price,
            exit_price=trade.entry_price,
            quantity=trade.quantity,
            pnl_usdt=0.0,
            reason="loss_guard_breakeven",
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
        )

    @staticmethod
    def _calculate_quantity(usdt_size: float, leverage: int, price: float) -> float:
        return (usdt_size * leverage) / price

    @staticmethod
    def _estimate_gross_pnl(direction: PositionDirection, entry: float, exit_price: float, qty: float) -> float:
        if direction == PositionDirection.LONG:
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty

    @staticmethod
    def _estimate_total_taker_fee(
        entry_price: float,
        exit_price: float,
        qty: float,
        taker_fee_pct: float,
    ) -> float:
        fee_rate = taker_fee_pct / 100
        entry_notional = entry_price * qty
        exit_notional = exit_price * qty
        return (entry_notional * fee_rate) + (exit_notional * fee_rate)

    @staticmethod
    def _infer_timeframe_minutes(timeline: list[datetime]) -> float:
        seconds = (timeline[1] - timeline[0]).total_seconds()
        if seconds <= 0:
            raise ValueError("Invalid candle timestamps, non-increasing sequence.")
        return seconds / 60

    @staticmethod
    def _build_timeline(candles: dict[tuple[str, datetime], Candle]) -> list[datetime]:
        all_ts = {ts for _, ts in candles.keys()}
        return sorted(all_ts)

    @staticmethod
    def _parse_symbol(value: str) -> str:
        return value.replace("-", "/").upper()

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        v = value.strip()
        if v.isdigit():
            ts = int(v)
            if len(v) >= 13:
                return datetime.fromtimestamp(ts / 1000, tz=UTC)
            return datetime.fromtimestamp(ts, tz=UTC)
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)

    def _load_candles(self, csv_path: Path) -> dict[tuple[str, datetime], Candle]:
        by_key: dict[tuple[str, datetime], Candle] = {}
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise ValueError(
                    "CSV columns required: timestamp,symbol,open,high,low,close,volume"
                )
            for row in reader:
                ts = self._parse_timestamp(row["timestamp"])
                symbol = self._parse_symbol(row["symbol"])
                candle = Candle(
                    ts=ts,
                    symbol=symbol,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                by_key[(symbol, ts)] = candle
        return by_key

    @staticmethod
    def _summarize(trades: list[SimulatedTrade]) -> BacktestResult:
        wins = sum(1 for t in trades if t.pnl_usdt > 0)
        losses = sum(1 for t in trades if t.pnl_usdt < 0)
        stops = sum(1 for t in trades if t.reason == "stop_loss")
        total_pnl = sum(t.pnl_usdt for t in trades)
        total_volume = sum(t.notional_volume for t in trades)
        return BacktestResult(
            total_trades=len(trades),
            winning_trades=wins,
            losing_trades=losses,
            stop_losses=stops,
            total_pnl_usdt=total_pnl,
            total_notional_volume=total_volume,
            trades=trades,
        )

