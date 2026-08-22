from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any

import httpx

from bot.config import Settings
from bot.exchange.okx_client import OKXFuturesClient, OKXFundingRateInfo, OKXOrderBook
from bot.models import PositionDirection
from bot.volume import VolumeBuildProfile
from bot.arena.factors import FactorRegistry
from bot.strategy.confluence import evaluate_confluence
from bot.strategy.indicators import atr, ema, macd, rsi
from bot.data.market_signals import TechnicalFeatures


@dataclass(frozen=True, slots=True)
class BacktestCandle:
    timestamp_ms: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    symbol: str
    direction: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    quantity: float
    notional_volume_usdt: float
    gross_pnl_usdt: float
    fee_usdt: float
    net_pnl_usdt: float
    exit_reason: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RealtimeBacktestSummary:
    generated_at_utc: str
    data_source: str
    symbols: list[str]
    interval: str
    candles_per_symbol: dict[str, int]
    guard_status: dict[str, dict[str, Any]]
    simulated_trades: int
    skipped_entries: int
    window_notional_volume_usdt: float
    window_gross_pnl_usdt: float
    window_fee_usdt: float
    window_net_pnl_usdt: float
    average_net_pnl_per_trade_usdt: float
    winning_trades: int
    losing_trades: int
    breakeven_exits: int
    stop_loss_exits: int
    take_profit_exits: int
    time_stop_exits: int
    configured_volume_per_entry_usdt: float
    configured_daily_entries_to_target: int
    configured_daily_volume_target_usdt: float
    configured_total_volume_target_usdt: float
    projected_daily_volume_usdt: float
    projected_daily_fee_usdt: float
    projected_daily_net_pnl_usdt: float
    projected_days_to_total_target: float
    trades: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def simulate_realtime_backtest(
    settings: Any,
    candles_by_symbol: dict[str, list[BacktestCandle]],
    *,
    order_books: dict[str, OKXOrderBook | None],
    funding: dict[str, OKXFundingRateInfo | None],
    interval_minutes: int,
    data_source: str = "provided",
    include_trades: bool = True,
    strategy: str = "volume",
) -> RealtimeBacktestSummary:
    profile = VolumeBuildProfile.from_settings(settings)
    symbols = tuple(settings.symbols)
    hold_bars = max(1, ceil(float(settings.position_hold_minutes) / interval_minutes))
    entry_bars = max(1, ceil(float(settings.entry_interval_minutes) / interval_minutes))
    min_len = min((len(candles_by_symbol.get(symbol, [])) for symbol in symbols), default=0)
    max_entry_index = max(0, min_len - hold_bars - 1)
    daily_target = float(getattr(settings, "volume_daily_target_usdt", 0.0) or 0.0)
    volume_per_entry = float(profile.volume_per_entry_usdt)
    entries_to_target = int(getattr(settings, "trade_target_per_day", 0) or 0)
    if daily_target > 0 and volume_per_entry > 0:
        entries_to_target = min(entries_to_target, max(1, ceil(daily_target / volume_per_entry)))

    guard_status = {
        symbol: _guard_status(settings, symbol, order_books.get(symbol), funding.get(symbol), float(profile.notional_per_side_usdt))
        for symbol in symbols
    }

    trades: list[SimulatedTrade] = []
    skipped_entries = 0
    symbol_index = 0
    direction_index = 0
    use_confluence = strategy in {"volume", "ai_volume"} and bool(
        getattr(settings, "confluence_enabled", True)
    )
    registry = FactorRegistry() if use_confluence else None
    for entry_index in range(0, max_entry_index + 1, entry_bars):
        if len(trades) >= int(getattr(settings, "trade_target_per_day", 0) or 0):
            break
        symbol = symbols[symbol_index % len(symbols)]
        symbol_index += 1
        if not guard_status[symbol]["ok"]:
            skipped_entries += 1
            continue
        candles = candles_by_symbol.get(symbol, [])
        if entry_index >= len(candles):
            skipped_entries += 1
            continue

        if use_confluence:
            features = _features_from_window(symbol, candles, entry_index)
            if features is None:
                skipped_entries += 1
                continue
            atr_pct = (features.atr_14 / features.last_price * 100.0) if features.last_price > 0 else 0.0
            snapshots = registry.score_symbol(features)
            decision = evaluate_confluence(
                snapshots,
                atr_pct=atr_pct,
                min_agreement=float(settings.confluence_min_agreement),
                min_atr_pct=float(settings.confluence_min_atr_pct),
                min_score=float(settings.confluence_min_score),
                atr_tp_mult=float(settings.confluence_atr_tp_mult),
                atr_sl_mult=float(settings.confluence_atr_sl_mult),
                tp_floor_pct=float(settings.take_profit_pct),
                sl_floor_pct=float(settings.stop_loss_pct),
            )
            if not decision.enter:
                skipped_entries += 1
                continue
            trade = _simulate_trade(
                settings,
                symbol=symbol,
                direction=decision.direction,
                candles=candles,
                entry_index=entry_index,
                hold_bars=hold_bars,
                stop_loss_pct=decision.stop_loss_pct,
                take_profit_pct=decision.take_profit_pct,
            )
            trades.append(trade)
            continue

        direction = (PositionDirection.LONG, PositionDirection.SHORT)[direction_index % 2]
        direction_index += 1
        trade = _simulate_trade(
            settings,
            symbol=symbol,
            direction=direction,
            candles=candles,
            entry_index=entry_index,
            hold_bars=hold_bars,
        )
        trades.append(trade)

    total_volume = sum(item.notional_volume_usdt for item in trades)
    gross = sum(item.gross_pnl_usdt for item in trades)
    fees = sum(item.fee_usdt for item in trades)
    net = sum(item.net_pnl_usdt for item in trades)
    average_net = net / len(trades) if trades else 0.0
    average_fee = fees / len(trades) if trades else 0.0
    average_volume = total_volume / len(trades) if trades else volume_per_entry
    projected_daily_volume = average_volume * entries_to_target
    projected_daily_fee = average_fee * entries_to_target
    projected_daily_net = average_net * entries_to_target
    total_target = float(getattr(settings, "volume_target_usdt", 0.0) or 0.0)
    projected_days = (total_target / projected_daily_volume) if projected_daily_volume > 0 else 0.0

    return RealtimeBacktestSummary(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        data_source=data_source,
        symbols=list(symbols),
        interval=f"{interval_minutes}m",
        candles_per_symbol={symbol: len(candles_by_symbol.get(symbol, [])) for symbol in symbols},
        guard_status=guard_status,
        simulated_trades=len(trades),
        skipped_entries=skipped_entries,
        window_notional_volume_usdt=total_volume,
        window_gross_pnl_usdt=gross,
        window_fee_usdt=fees,
        window_net_pnl_usdt=net,
        average_net_pnl_per_trade_usdt=average_net,
        winning_trades=sum(1 for item in trades if item.net_pnl_usdt > 0),
        losing_trades=sum(1 for item in trades if item.net_pnl_usdt < 0),
        breakeven_exits=sum(1 for item in trades if item.exit_reason == "breakeven_exit"),
        stop_loss_exits=sum(1 for item in trades if item.exit_reason == "stop_loss"),
        take_profit_exits=sum(1 for item in trades if item.exit_reason == "take_profit"),
        time_stop_exits=sum(1 for item in trades if item.exit_reason == "time_stop"),
        configured_volume_per_entry_usdt=volume_per_entry,
        configured_daily_entries_to_target=entries_to_target,
        configured_daily_volume_target_usdt=daily_target,
        configured_total_volume_target_usdt=total_target,
        projected_daily_volume_usdt=projected_daily_volume,
        projected_daily_fee_usdt=projected_daily_fee,
        projected_daily_net_pnl_usdt=projected_daily_net,
        projected_days_to_total_target=projected_days,
        trades=[asdict(item) for item in trades] if include_trades else [],
    )


def _simulate_trade(
    settings: Any,
    *,
    symbol: str,
    direction: PositionDirection,
    candles: list[BacktestCandle],
    entry_index: int,
    hold_bars: int,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimulatedTrade:
    entry = candles[entry_index]
    entry_price = entry.close
    quantity = (float(settings.volume_margin_per_entry_usdt or settings.position_usdt) * int(settings.leverage)) / entry_price
    stop_pct = float(settings.stop_loss_pct if stop_loss_pct is None else stop_loss_pct)
    take_pct = float(settings.take_profit_pct if take_profit_pct is None else take_profit_pct)
    fee_pct = _maker_fee_pct(settings) if _is_maker_only(settings) else float(settings.taker_fee_pct)
    breakeven_enabled = bool(getattr(settings, "volume_breakeven_exit_enabled", False))
    breakeven_min_bars = 1 if int(getattr(settings, "volume_breakeven_min_seconds", 0) or 0) > 0 else 0
    breakeven_price = _breakeven_exit_price(
        direction,
        entry_price,
        quantity,
        fee_pct,
        float(getattr(settings, "volume_breakeven_min_profit_usdt", 0.0) or 0.0),
    )

    exit_price = candles[min(entry_index + hold_bars, len(candles) - 1)].close
    exit_index = min(entry_index + hold_bars, len(candles) - 1)
    reason = "time_stop"
    for index in range(entry_index + 1, min(entry_index + hold_bars, len(candles) - 1) + 1):
        candle = candles[index]
        bars_elapsed = index - entry_index
        if direction == PositionDirection.LONG:
            stop_price = entry_price * (1.0 - stop_pct / 100.0)
            take_price = entry_price * (1.0 + take_pct / 100.0)
            if candle.low <= stop_price:
                exit_price, exit_index, reason = stop_price, index, "stop_loss"
                break
            if breakeven_enabled and bars_elapsed >= breakeven_min_bars and candle.high >= breakeven_price:
                exit_price, exit_index, reason = breakeven_price, index, "breakeven_exit"
                break
            if candle.high >= take_price:
                exit_price, exit_index, reason = take_price, index, "take_profit"
                break
        else:
            stop_price = entry_price * (1.0 + stop_pct / 100.0)
            take_price = entry_price * (1.0 - take_pct / 100.0)
            if candle.high >= stop_price:
                exit_price, exit_index, reason = stop_price, index, "stop_loss"
                break
            if breakeven_enabled and bars_elapsed >= breakeven_min_bars and candle.low <= breakeven_price:
                exit_price, exit_index, reason = breakeven_price, index, "breakeven_exit"
                break
            if candle.low <= take_price:
                exit_price, exit_index, reason = take_price, index, "take_profit"
                break

    gross = (exit_price - entry_price) * quantity
    if direction == PositionDirection.SHORT:
        gross = -gross
    fee = _estimate_round_trip_fee(settings, entry_price, exit_price, quantity, reason)
    volume = (entry_price * quantity) + (exit_price * quantity)
    return SimulatedTrade(
        symbol=symbol,
        direction=direction.value,
        entry_index=entry_index,
        exit_index=exit_index,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        notional_volume_usdt=volume,
        gross_pnl_usdt=gross,
        fee_usdt=fee,
        net_pnl_usdt=gross - fee,
        exit_reason=reason,
        metadata=metadata,
    )


def _guard_status(
    settings: Any,
    symbol: str,
    order_book: OKXOrderBook | None,
    funding: OKXFundingRateInfo | None,
    target_notional: float,
) -> dict[str, Any]:
    if not bool(getattr(settings, "execution_guards_enabled", True)):
        return {"ok": True, "reason": "guards_disabled"}
    if order_book is None:
        return {"ok": False, "reason": "missing_orderbook"}
    if order_book.spread_bps > float(getattr(settings, "okx_max_spread_bps", 0.0) or 0.0):
        return {"ok": False, "reason": "spread_too_wide", "spread_bps": order_book.spread_bps}
    required_depth = target_notional * float(getattr(settings, "min_orderbook_depth_multiplier", 0.0) or 0.0)
    executable_depth = min(order_book.bid_depth_notional, order_book.ask_depth_notional)
    if required_depth > 0 and executable_depth < required_depth:
        return {
            "ok": False,
            "reason": "insufficient_depth",
            "required_depth_usdt": required_depth,
            "executable_depth_usdt": executable_depth,
        }
    funding_rate = funding.funding_rate if funding is not None else 0.0
    max_funding = float(getattr(settings, "max_abs_funding_rate", 0.0) or 0.0)
    if max_funding > 0 and abs(funding_rate) > max_funding:
        return {"ok": False, "reason": "funding_extreme", "funding_rate": funding_rate}
    return {
        "ok": True,
        "reason": "passed",
        "spread_bps": order_book.spread_bps,
        "bid_depth_notional": order_book.bid_depth_notional,
        "ask_depth_notional": order_book.ask_depth_notional,
        "funding_rate": funding_rate,
    }


def _breakeven_exit_price(
    direction: PositionDirection,
    entry_price: float,
    quantity: float,
    taker_fee_pct: float,
    target_net_usdt: float,
) -> float:
    fee_rate = taker_fee_pct / 100.0
    qty = max(quantity, 1e-12)
    entry_notional = entry_price * qty
    if direction == PositionDirection.LONG:
        return (entry_notional * (1.0 + fee_rate) + target_net_usdt) / (qty * (1.0 - fee_rate))
    return (entry_notional * (1.0 - fee_rate) - target_net_usdt) / (qty * (1.0 + fee_rate))


def _is_maker_only(settings: Any) -> bool:
    return str(getattr(settings, "volume_execution_mode", "") or "").strip().lower() == "maker_only"


def _maker_fee_pct(settings: Any) -> float:
    return float(getattr(settings, "maker_fee_pct", settings.taker_fee_pct))


def _estimate_round_trip_fee(
    settings: Any,
    entry_price: float,
    exit_price: float,
    qty: float,
    exit_reason: str,
) -> float:
    """Model fees per leg from the bot's execution mode.

    In maker_only mode the entry is always a post-only maker fill. Exits are maker
    too, except forced stop_loss/time_stop legs that cross the book as taker
    (MAKER_ALLOW_TAKER_EMERGENCY_EXIT). Otherwise both legs are taker.
    """
    taker_pct = float(settings.taker_fee_pct)
    if not _is_maker_only(settings):
        entry_pct = exit_pct = taker_pct
    else:
        maker_pct = _maker_fee_pct(settings)
        entry_pct = maker_pct
        forced_taker_exit = bool(getattr(settings, "maker_allow_taker_emergency_exit", True))
        exit_pct = taker_pct if (forced_taker_exit and exit_reason in {"stop_loss", "time_stop"}) else maker_pct
    entry_fee = (entry_price * qty) * (entry_pct / 100.0)
    exit_fee = (exit_price * qty) * (exit_pct / 100.0)
    return entry_fee + exit_fee


def _estimate_total_taker_fee(entry_price: float, exit_price: float, qty: float, taker_fee_pct: float) -> float:
    fee_rate = taker_fee_pct / 100.0
    return ((entry_price * qty) + (exit_price * qty)) * fee_rate


def _parse_interval_minutes(value: str) -> int:
    clean = value.strip().lower()
    if clean.endswith("m"):
        return int(clean[:-1])
    raise ValueError("Only minute intervals like 1m, 3m, and 5m are supported.")


def _build_settings(use_env: bool) -> Settings:
    """Build Settings for the backtest.

    By default reads the project .env so the backtest mirrors the live bot's
    tuning (stop/take, size, leverage, fees). --no-env uses config defaults with
    placeholder credentials, which keeps CI hermetic.
    """
    if not use_env:
        return Settings(
            _env_file=None,
            OKX_API_KEY="backtest-only",
            OKX_SECRET_KEY="backtest-only",
            OKX_PASSPHRASE="backtest-only",
            STRATEGY_MODE="volume",
        )
    env_path = Path(__file__).resolve().parent.parent / ".env"
    return Settings(_env_file=str(env_path) if env_path.exists() else None)


async def _run(args: argparse.Namespace) -> RealtimeBacktestSummary:
    settings = _build_settings(use_env=not args.no_env)
    interval = args.interval or settings.ai_kline_interval
    interval_minutes = _parse_interval_minutes(interval)
    source = args.source.lower()
    if source not in {"auto", "okx", "coingecko"}:
        raise ValueError("--source must be auto, okx, or coingecko")
    if source == "coingecko":
        return await _run_coingecko(settings, interval_minutes, include_trades=not args.no_trades, strategy=args.strategy)
    try:
        return await _run_okx(settings, interval, interval_minutes, args)
    except Exception:
        if source != "auto":
            raise
        return await _run_coingecko(settings, interval_minutes, include_trades=not args.no_trades, strategy=args.strategy)


async def _run_okx(settings: Settings, interval: str, interval_minutes: int, args: argparse.Namespace) -> RealtimeBacktestSummary:
    client = OKXFuturesClient(
        api_key=settings.okx_api_key,
        secret_key=settings.okx_secret_key,
        passphrase=settings.okx_passphrase,
        base_url=settings.okx_base_url,
        demo_trading=settings.okx_demo_trading,
        margin_mode=settings.okx_margin_mode,
        instrument_type=settings.okx_instrument_type,
        timeout_seconds=settings.request_timeout_seconds,
        insecure_ssl=settings.okx_insecure_ssl,
        debug_http=False,
    )
    try:
        candles_by_symbol: dict[str, list[BacktestCandle]] = {}
        order_books: dict[str, OKXOrderBook | None] = {}
        funding: dict[str, OKXFundingRateInfo | None] = {}
        for symbol in settings.symbols:
            rows = await client.get_klines(symbol, interval=interval, limit=args.limit)
            candles_by_symbol[symbol] = [
                BacktestCandle(
                    timestamp_ms=float(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                for row in rows
            ]
            try:
                order_books[symbol] = await client.get_order_book(symbol, depth=settings.orderbook_depth_levels)
            except Exception:
                order_books[symbol] = None
            try:
                funding[symbol] = await client.get_funding_rate_info(symbol)
            except Exception:
                funding[symbol] = None
        return simulate_realtime_backtest(
            settings,
            candles_by_symbol,
            order_books=order_books,
            funding=funding,
            interval_minutes=interval_minutes,
            data_source="okx",
            include_trades=not args.no_trades,
            strategy=args.strategy,
        )
    finally:
        await client.close()


async def _run_coingecko(
    settings: Settings,
    interval_minutes: int,
    *,
    include_trades: bool,
    strategy: str,
) -> RealtimeBacktestSummary:
    symbol_to_coin = {"BTC/USDT": "bitcoin", "ETH/USDT": "ethereum"}
    candles_by_symbol: dict[str, list[BacktestCandle]] = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for symbol in settings.symbols:
            coin = symbol_to_coin.get(symbol)
            if coin is None:
                candles_by_symbol[symbol] = []
                continue
            response = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
                params={"vs_currency": "usd", "days": "1"},
            )
            response.raise_for_status()
            prices = response.json().get("prices", [])
            candles_by_symbol[symbol] = _candles_from_prices(prices)

    now = datetime.now(timezone.utc)
    synthetic_books = {
        symbol: OKXOrderBook(
            symbol=symbol,
            best_bid=(candles[-1].close * 0.9999) if candles else 0.0,
            best_ask=(candles[-1].close * 1.0001) if candles else 0.0,
            mid_price=candles[-1].close if candles else 0.0,
            spread_bps=2.0,
            bid_depth_notional=10_000_000.0,
            ask_depth_notional=10_000_000.0,
            timestamp=now,
            raw={"source": "coingecko_synthetic_orderbook"},
        )
        for symbol, candles in candles_by_symbol.items()
    }
    synthetic_funding = {
        symbol: OKXFundingRateInfo(symbol=symbol, funding_rate=0.0, next_funding_time=None, raw={"source": "coingecko"})
        for symbol in settings.symbols
    }
    inferred_interval = _infer_interval_minutes(candles_by_symbol) or interval_minutes
    return simulate_realtime_backtest(
        settings,
        candles_by_symbol,
        order_books=synthetic_books,
        funding=synthetic_funding,
        interval_minutes=max(inferred_interval, 1),
        data_source="coingecko",
        include_trades=include_trades,
        strategy=strategy,
    )


def _features_from_window(symbol: str, candles: list[BacktestCandle], idx: int) -> TechnicalFeatures | None:
    """Build TechnicalFeatures from the candle window ending at idx (inclusive)."""
    window = candles[: idx + 1]
    if len(window) < 35:
        return None
    closes = [c.close for c in window]
    highs = [c.high for c in window]
    lows = [c.low for c in window]
    vols = [c.volume for c in window]
    last = closes[-1]
    macd_line, macd_sig, macd_hist = macd(closes)
    chg1 = ((closes[-1] - closes[-2]) / closes[-2] * 100.0) if len(closes) > 1 and closes[-2] else 0.0
    chg5 = ((closes[-1] - closes[-6]) / closes[-6] * 100.0) if len(closes) > 5 and closes[-6] else 0.0
    vol_now = vols[-1]
    vol_prev = vols[-2] if len(vols) > 1 and vols[-2] else vol_now
    vol_chg = ((vol_now - vol_prev) / vol_prev * 100.0) if vol_prev else 0.0
    return TechnicalFeatures(
        symbol=symbol, last_price=last, ema_fast=ema(closes, 9), ema_slow=ema(closes, 21),
        ema_21=ema(closes, 21), rsi_14=rsi(closes, 14), atr_14=atr(highs, lows, closes, 14),
        macd_line=macd_line, macd_signal=macd_sig, macd_hist=macd_hist,
        change_pct_1=chg1, change_pct_5=chg5, volume=vol_now, volume_change_pct=vol_chg,
        funding_rate=0.0, open_interest=0.0, open_interest_change_pct=0.0,
        spread_bps=1.0, bid_depth_notional=5_000_000.0, ask_depth_notional=5_000_000.0,
    )


def _candles_from_prices(raw_prices: list[Any]) -> list[BacktestCandle]:
    candles: list[BacktestCandle] = []
    previous_price: float | None = None
    for item in raw_prices:
        if not isinstance(item, list) or len(item) < 2:
            continue
        timestamp = float(item[0])
        close = float(item[1])
        open_price = previous_price if previous_price is not None else close
        candles.append(
            BacktestCandle(
                timestamp_ms=timestamp,
                open=open_price,
                high=max(open_price, close),
                low=min(open_price, close),
                close=close,
                volume=0.0,
            )
        )
        previous_price = close
    return candles


def _infer_interval_minutes(candles_by_symbol: dict[str, list[BacktestCandle]]) -> int | None:
    deltas: list[float] = []
    for candles in candles_by_symbol.values():
        for before, after in zip(candles, candles[1:]):
            delta_minutes = (after.timestamp_ms - before.timestamp_ms) / 60_000.0
            if delta_minutes > 0:
                deltas.append(delta_minutes)
    if not deltas:
        return None
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return max(1, int(round(median)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Realtime OKX candle dry-run backtest. No orders are placed.")
    parser.add_argument("--source", default="auto", help="auto, okx, or coingecko. auto falls back when OKX is blocked.")
    parser.add_argument("--strategy", default="volume", choices=("volume", "ai_volume"), help="Backtest strategy.")
    parser.add_argument("--interval", default=None, help="OKX candle interval, default AI_KLINE_INTERVAL.")
    parser.add_argument("--limit", type=int, default=300, help="Candles per symbol to fetch.")
    parser.add_argument("--no-trades", action="store_true", help="Omit per-trade rows from JSON output.")
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="Ignore the project .env and use config defaults with placeholder creds (for CI).",
    )
    return parser


def main() -> None:
    summary = asyncio.run(_run(build_parser().parse_args()))
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
