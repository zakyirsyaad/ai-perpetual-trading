"""Engine-side hooks that turn trading events into dashboard messages.

`TradeEngine` calls these at fixed points (a decision, an entry, a mark, a close) and
nothing else. Telemetry is observability, so every hook on `EngineTelemetry` catches
its own failures and logs them: a bug here can cost a dashboard row, never a tick, an
order or a stop loss. The hooks are plain synchronous calls that touch memory only, so
they also cannot delay the loop.

Two honest simplifications, because the dashboard's contract was written for a model
that publishes a probability per action and this bot does not:

- `probabilities` is one-hot on the action the bot actually took. The model's own
  self-reported certainty travels separately as `confidence`, never as a probability.
- `upIn10`, a required key with no counterpart here, is the neutral 0.5.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol

from bot.ai.usage import DailyAiUsage
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.poller import coin_of
from bot.telemetry.wire import (
    REASON_MAX_CHARS,
    BlockEvent,
    Decision,
    FillData,
    FillMessage,
    Position,
    Probabilities,
    Quote,
    QuoteMessage,
    Resting,
    Side,
    Totals,
    PricePointFill,
    UsageDay,
    UsageSummary,
    VolumeDay,
    VolumeSummary,
)

LOGGER = logging.getLogger("volume_bot.telemetry")

NEUTRAL_UP_IN10 = 0.5
HOLD_REASON = "strategy returned HOLD"
MARK_INTERVAL_SECONDS = 5.0

Action = Literal["buy", "sell", "hold"]
Intent = Literal["open", "close", "hold"]


class EngineObserver(Protocol):
    def decided(self, plan: TradePlan | None, *, latency_ms: int) -> None: ...

    def entry_quoted(self, plan: TradePlan, *, price: float, size: float, order_id: str) -> None: ...

    def entry_filled(self, position: OpenPosition, *, leverage: int, fee_usdt: float) -> None: ...

    def position_resumed(self, position: OpenPosition, *, leverage: int) -> None: ...

    def position_marked(self, position: OpenPosition, *, net_usdt: float) -> None: ...

    def position_closed(self, trade: ClosedTrade, *, fee_usdt: float) -> None: ...

    def volume_updated(self, *, today: str, daily: Sequence[tuple[str, float]], daily_target_usdt: float) -> None: ...

    def usage_updated(self, *, today: str, daily: Sequence[DailyAiUsage]) -> None: ...

    def history_loaded(self, trades: Sequence[ClosedTrade], *, realized_usd: float = 0.0) -> None: ...


class NullObserver:
    """What the engine uses when the dashboard feed is off."""

    def decided(self, plan: TradePlan | None, *, latency_ms: int) -> None:
        return None

    def entry_quoted(self, plan: TradePlan, *, price: float, size: float, order_id: str) -> None:
        return None

    def entry_filled(self, position: OpenPosition, *, leverage: int, fee_usdt: float) -> None:
        return None

    def position_resumed(self, position: OpenPosition, *, leverage: int) -> None:
        return None

    def position_marked(self, position: OpenPosition, *, net_usdt: float) -> None:
        return None

    def position_closed(self, trade: ClosedTrade, *, fee_usdt: float) -> None:
        return None

    def volume_updated(self, *, today: str, daily: Sequence[tuple[str, float]], daily_target_usdt: float) -> None:
        return None

    def usage_updated(self, *, today: str, daily: Sequence[DailyAiUsage]) -> None:
        return None

    def history_loaded(self, trades: Sequence[ClosedTrade], *, realized_usd: float = 0.0) -> None:
        return None


NULL_OBSERVER = NullObserver()


def _never_raises(method: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception:  # telemetry must never break trading
            LOGGER.warning("telemetry %s failed", method.__name__, exc_info=True)

    return guarded


def _open_side(direction: PositionDirection) -> Side:
    return "buy" if direction is PositionDirection.LONG else "sell"


def _close_side(direction: PositionDirection) -> Side:
    return "sell" if direction is PositionDirection.LONG else "buy"


def _bias(direction: PositionDirection) -> Literal["long", "short"]:
    return "long" if direction is PositionDirection.LONG else "short"


def _order_id(raw: str | None) -> int | None:
    """The dashboard keys orders by integer; a venue id that is not one is simply omitted."""
    return int(raw) if raw is not None and raw.isdigit() else None


def _decision(
    action: Action,
    intent: Intent,
    *,
    latency_ms: int,
    bias: Literal["long", "short"] | None = None,
    leverage: float | None = None,
    confidence: float | None = None,
    reason: str | None = None,
) -> Decision:
    return Decision(
        action=action,
        intent=intent,
        bias=bias,
        leverage=leverage,
        probabilities=Probabilities(
            buy=1.0 if action == "buy" else 0.0,
            sell=1.0 if action == "sell" else 0.0,
            hold=1.0 if action == "hold" else 0.0,
        ),
        up_in10=NEUTRAL_UP_IN10,
        latency_ms=max(0, int(latency_ms)),
        late=False,
        confidence=None if confidence is None else min(1.0, max(0.0, confidence)),
        reason=None if reason is None else reason[:REASON_MAX_CHARS],
    )


FLAT = Position(side="flat", size=0.0, entry_price=None, leverage=None, unrealized_usd=0.0, unrealized_sz=0.0)


class EngineTelemetry:
    def __init__(
        self,
        hub: TelemetryHub,
        symbols: Sequence[str],
        *,
        account_balance_usdt: float,
        clock: Callable[[], float] = time.time,
        mark_interval_seconds: float = MARK_INTERVAL_SECONDS,
    ) -> None:
        self._hub = hub
        self._coins = tuple(coin_of(s) for s in symbols)
        self._balance = account_balance_usdt
        self._clock = clock
        self._mark_interval = mark_interval_seconds
        self._block_number = 0
        self._open: dict[str, Position] = {}
        self._entry_block: dict[str, int] = {}
        self._last_mark_at: dict[str, float] = {}
        self._blocks = 0
        self._decisions = 0
        self._quotes = 0
        self._fills = 0
        self._fee_usd = 0.0
        self._realized_usd = 0.0

    # -- hooks the engine calls -------------------------------------------

    @_never_raises
    def decided(self, plan: TradePlan | None, *, latency_ms: int) -> None:
        number = self._next_block()
        if plan is None:
            hold = _decision("hold", "hold", latency_ms=latency_ms, reason=HOLD_REASON)
            for coin in self._coins:
                self._publish(coin, number, hold)
            return
        coin = coin_of(plan.symbol)
        decision = _decision(
            _open_side(plan.direction),
            "open",
            latency_ms=latency_ms,
            bias=_bias(plan.direction),
            leverage=float(plan.leverage),
            confidence=plan.confidence,
            reason=plan.decision_reason,
        )
        if self._publish(coin, number, decision):
            self._entry_block[coin] = number

    @_never_raises
    def entry_quoted(self, plan: TradePlan, *, price: float, size: float, order_id: str) -> None:
        coin = coin_of(plan.symbol)
        quote = Quote(
            side=_open_side(plan.direction),
            price=price,
            size=size,
            tx_hash=None,
            cancel=[],
            status="placed",
            order_id=_order_id(order_id),
            capped=False,
        )
        self._quotes += 1
        self._hub.record_quote(QuoteMessage(coin=coin, block=self._entry_block.get(coin, self._block_number), quote=quote))

    @_never_raises
    def entry_filled(self, position: OpenPosition, *, leverage: int, fee_usdt: float) -> None:
        coin = coin_of(position.symbol)
        held = self._held(position, leverage)
        self._open[coin] = held
        self._fills += 1
        self._fee_usd += fee_usdt
        fill = FillData(
            side=_open_side(position.direction),
            size=position.quantity,
            price=position.entry_price,
            tx_hash=None,
            order_id=_order_id(position.exchange_order_id) or 0,
            simulated=False,
            fee_usd=fee_usdt,
            direction="open",
        )
        block = self._entry_block.get(coin, self._block_number)
        self._hub.record_fill(FillMessage(coin=coin, block=block, fill=fill, ts=self._now_ms()))
        self._hub.refresh_latest(coin, position=held, totals=self._totals())

    @_never_raises
    def position_resumed(self, position: OpenPosition, *, leverage: int) -> None:
        coin = coin_of(position.symbol)
        self._open[coin] = self._held(position, leverage)
        number = self._next_block()
        if self._publish(coin, number, None, fallback_price=position.entry_price):
            self._entry_block[coin] = number

    @_never_raises
    def position_marked(self, position: OpenPosition, *, net_usdt: float) -> None:
        coin = coin_of(position.symbol)
        held = self._open.get(coin)
        if held is None:
            return
        now = self._clock()
        last = self._last_mark_at.get(coin)
        if last is not None and now - last < self._mark_interval:
            return
        self._last_mark_at[coin] = now
        held = held.model_copy(update={"unrealized_usd": net_usdt})
        self._open[coin] = held
        update: dict[str, Any] = {"position": held, "totals": self._totals()}
        latest = self._hub.latest_price(coin)
        if latest is not None:
            update.update(
                ts=latest.ts,
                mid=latest.mid,
                best_bid=latest.best_bid,
                best_ask=latest.best_ask,
                spread_bps=latest.spread_bps,
            )
        self._hub.refresh_latest(coin, **update)

    @_never_raises
    def position_closed(self, trade: ClosedTrade, *, fee_usdt: float) -> None:
        coin = coin_of(trade.symbol)
        self._open.pop(coin, None)
        self._last_mark_at.pop(coin, None)
        self._entry_block.pop(coin, None)
        self._realized_usd += trade.pnl_usdt
        self._fee_usd += fee_usdt
        self._fills += 1
        number = self._next_block()
        side = _close_side(trade.direction)
        decision = _decision(side, "close", latency_ms=0, bias=_bias(trade.direction), reason=trade.reason)
        self._publish(coin, number, decision, fallback_price=trade.exit_price)
        fill = FillData(
            side=side,
            size=trade.quantity,
            price=trade.exit_price,
            tx_hash=None,
            order_id=_order_id(trade.exchange_exit_order_id) or 0,
            simulated=False,
            fee_usd=fee_usdt,
            closed_pnl=trade.pnl_usdt,
            direction="close",
        )
        self._hub.record_fill(FillMessage(coin=coin, block=number, fill=fill, ts=self._now_ms()))

    @_never_raises
    def volume_updated(self, *, today: str, daily: Sequence[tuple[str, float]], daily_target_usdt: float) -> None:
        """Turn the store's per-day volumes into the dashboard's series with a running total."""
        by_day = {str(day): float(volume) for day, volume in daily}
        by_day.setdefault(today, 0.0)  # today is always listed, even before its first trade
        running = 0.0
        days: list[VolumeDay] = []
        for day in sorted(by_day):
            running += by_day[day]
            days.append(VolumeDay(day=day, volume_usd=round(by_day[day], 2), cumulative_usd=round(running, 2)))
        self._hub.record_volume(
            VolumeSummary(
                today=today,
                today_usd=round(by_day[today], 2),
                daily_target_usd=max(float(daily_target_usdt), 0.0),
                total_usd=round(running, 2),
                days=days,
            )
        )

    @_never_raises
    def usage_updated(self, *, today: str, daily: Sequence[DailyAiUsage]) -> None:
        """Turn the store's per-day token spend into the dashboard's series, oldest day first."""
        by_day = {usage.day: usage for usage in daily}
        # Today is always listed, even before its first model call, so the panel never starts empty.
        by_day.setdefault(today, DailyAiUsage(today, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self._hub.record_usage(
            UsageSummary(
                today=today,
                days=[
                    UsageDay(
                        day=usage.day,
                        calls=usage.calls,
                        failed=usage.failed,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        reasoning_tokens=usage.reasoning_tokens,
                        total_tokens=usage.total_tokens,
                        entry_tokens=usage.entry_tokens,
                        exit_tokens=usage.exit_tokens,
                        closed_trades=usage.closed_trades,
                    )
                    for usage in (by_day[day] for day in sorted(by_day))
                ],
            )
        )

    @_never_raises
    def history_loaded(self, trades: Sequence[ClosedTrade], *, realized_usd: float = 0.0) -> None:
        """Rebuild the two fills the live path would have produced for trades already in the store.

        The close fill carries the net PnL and the total fee, exactly as `position_closed` does;
        the fee is recovered as gross minus net because the store keeps only the net figure.
        """
        by_coin: dict[str, list[tuple[int, PricePointFill]]] = {}
        for trade in trades:
            try:
                if not (trade.entry_price > 0 and trade.exit_price > 0 and trade.quantity > 0):
                    continue
                move = (
                    trade.exit_price - trade.entry_price
                    if trade.direction is PositionDirection.LONG
                    else trade.entry_price - trade.exit_price
                )
                fee = max(move * trade.quantity - trade.pnl_usdt, 0.0)
                opened = PricePointFill(
                    side=_open_side(trade.direction), price=trade.entry_price, size=trade.quantity, direction="open"
                )
                closed = PricePointFill(
                    side=_close_side(trade.direction),
                    price=trade.exit_price,
                    size=trade.quantity,
                    direction="close",
                    closed_pnl=trade.pnl_usdt,
                    fee_usd=fee,
                )
                items = by_coin.setdefault(coin_of(trade.symbol), [])
                items.append((int(trade.opened_at.timestamp() * 1000), opened))
                items.append((int(trade.closed_at.timestamp() * 1000), closed))
            except Exception:  # one unreadable trade must not hide the others
                LOGGER.warning("telemetry skipped an unreadable stored trade", exc_info=True)
        for coin, items in by_coin.items():
            self._hub.seed_fills(coin, items)
        # The header reads realized and total PnL from the next block's totals, whose counters restart
        # from zero with the process. Assigned, not added: this runs once, before any live trade.
        self._realized_usd = float(realized_usd)

    # -- internals ----------------------------------------------------------

    def _next_block(self) -> int:
        self._block_number += 1
        return self._block_number

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    @staticmethod
    def _held(position: OpenPosition, leverage: int) -> Position:
        return Position(
            side=_bias(position.direction),
            size=position.quantity,
            entry_price=position.entry_price,
            leverage=float(leverage),
            unrealized_usd=0.0,
            unrealized_sz=0.0,
        )

    def _totals(self) -> Totals:
        pnl = self._realized_usd + sum(p.unrealized_usd for p in self._open.values())
        return Totals(
            blocks=self._blocks,
            decisions=self._decisions,
            quotes=self._quotes,
            fills=self._fills,
            reverted=0,
            late_blocks=0,
            ai_cost_usd=0.0,
            fee_sz=0.0,
            fee_usd=self._fee_usd,
            realized_usd=self._realized_usd,
            pnl_usd=pnl,
            pnl_sz=0.0,
            pnl_pct=pnl / self._balance * 100.0 if self._balance > 0 else 0.0,
        )

    def _publish(
        self,
        coin: str,
        number: int,
        decision: Decision | None,
        *,
        fallback_price: float | None = None,
    ) -> bool:
        """Record one block for `coin`. False when there is no price to put on it yet."""
        block = self._block(coin, number, decision, fallback_price)
        if block is None:
            LOGGER.debug("telemetry skipped a %s block: no price known yet", coin)
            return False
        self._hub.record_block(block)
        return True

    def _block(
        self,
        coin: str,
        number: int,
        decision: Decision | None,
        fallback_price: float | None,
    ) -> BlockEvent | None:
        latest = self._hub.latest_price(coin)
        if latest is not None:
            mid, bid, ask, spread = latest.mid, latest.best_bid, latest.best_ask, latest.spread_bps
        elif fallback_price is not None and fallback_price > 0:
            mid = bid = ask = fallback_price
            spread = 0.0
        else:
            return None
        self._blocks += 1
        if decision is not None:
            self._decisions += 1
        return BlockEvent(
            coin=coin,
            block=number,
            ts=self._now_ms(),
            mid=mid,
            best_bid=bid,
            best_ask=ask,
            spread_bps=spread,
            decision=decision,
            quote=None,
            fill=None,
            resting=Resting(bid_sz=0.0, ask_sz=0.0),
            position=self._open.get(coin, FLAT),
            totals=self._totals(),
            account_value=None,
            withdrawable=None,
        )
