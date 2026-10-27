"""State the dashboard reads, plus the publish side that feeds it.

The hub owns the per-symbol decision history and chart tape and fans every new
message out through the bus. It is observability only: every `record_*` method
swallows its own failures after logging, so a telemetry bug can never reach the
trading loop that calls it.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from bot.telemetry.bus import TelemetryBus
from bot.telemetry.chart import VenueChart
from bot.telemetry.clip import clip_history, clip_snapshot_tape, clip_tape
from bot.telemetry.wire import (
    BlockEvent,
    FillMessage,
    Meta,
    PnlSummary,
    PriceMessage,
    PricePointFill,
    QuoteMessage,
    UsageSummary,
    VolumeSummary,
    to_wire,
)

LOGGER = logging.getLogger("volume_bot.telemetry")

DEFAULT_HISTORY_SIZE = 1000


class TelemetryHub:
    def __init__(
        self,
        meta: Meta,
        bus: TelemetryBus,
        *,
        history_size: int = DEFAULT_HISTORY_SIZE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._meta = meta
        self._bus = bus
        self._clock = clock
        self._history: dict[str, deque[BlockEvent]] = {s.coin: deque(maxlen=history_size) for s in meta.sleeves}
        self._charts: dict[str, VenueChart] = {s.coin: VenueChart() for s in meta.sleeves}
        self._latest_price: dict[str, PriceMessage] = {}
        self._volume: VolumeSummary | None = None
        self._usage: UsageSummary | None = None
        self._pnl: PnlSummary | None = None

    @property
    def bus(self) -> TelemetryBus:
        return self._bus

    @property
    def started_at(self) -> int:
        return self._meta.started_at

    # -- writes: never raise ------------------------------------------------

    def record_block(self, event: BlockEvent) -> None:
        try:
            if not self._known(event.coin):
                return
            self._history[event.coin].append(event)
            self._bus.publish(event)
        except Exception:  # telemetry must never break trading
            LOGGER.warning("telemetry record_block failed", exc_info=True)

    def record_quote(self, message: QuoteMessage) -> None:
        try:
            if not self._known(message.coin):
                return
            self._replace_block(message.coin, message.block, {"quote": message.quote})
            self._bus.publish(message)
        except Exception:
            LOGGER.warning("telemetry record_quote failed", exc_info=True)

    def record_fill(self, message: FillMessage) -> None:
        try:
            if not self._known(message.coin):
                return
            ts = message.ts if message.ts is not None else int(self._clock() * 1000)
            fill = message.fill
            self._charts[message.coin].add_fill(
                ts=ts,
                side=fill.side,
                price=fill.price,
                size=fill.size,
                direction=fill.direction,
                hash=fill.tx_hash,
                closed_pnl=fill.closed_pnl,
                fee_usd=fill.fee_usd,
            )
            self._replace_block(message.coin, message.block, {"fill": fill})
            # The dashboard places the mark by `ts`, so always send one.
            self._bus.publish(message.model_copy(update={"ts": ts}))
        except Exception:
            LOGGER.warning("telemetry record_fill failed", exc_info=True)

    def record_price(self, message: PriceMessage) -> None:
        try:
            if not self._known(message.coin):
                return
            self._charts[message.coin].add_mid(message.mid, message.ts)
            self._latest_price[message.coin] = message
            self._bus.publish(message)
        except Exception:
            LOGGER.warning("telemetry record_price failed", exc_info=True)

    def refresh_latest(self, coin: str, **update: Any) -> None:
        """Update the newest block of `coin` in place and republish it under the same number.

        The dashboard replaces a block that arrives with a number it already holds, so
        this keeps a live position current without adding a history row per refresh.
        """
        try:
            if not self._known(coin):
                return
            history = self._history[coin]
            if not history:
                return
            refreshed = history[-1].model_copy(update=update)
            history[-1] = refreshed
            self._bus.publish(refreshed)
        except Exception:
            LOGGER.warning("telemetry refresh_latest failed", exc_info=True)

    def seed_fills(self, coin: str, fills: Iterable[tuple[int, PricePointFill]]) -> None:
        """Put past fills on the tape so History and Trades survive a restart.

        Quiet on purpose: no block is touched and nothing is published, because no client has
        anything to replace yet. A client that connects later receives these through /tape.
        """
        try:
            if not self._known(coin):
                return
            chart = self._charts[coin]
            for ts, fill in fills:
                chart.add_fill(
                    ts=ts,
                    side=fill.side,
                    price=fill.price,
                    size=fill.size,
                    direction=fill.direction,
                    hash=fill.hash,
                    closed_pnl=fill.closed_pnl,
                    fee_usd=fill.fee_usd,
                )
        except Exception:
            LOGGER.warning("telemetry seed_fills failed", exc_info=True)

    def record_volume(self, summary: VolumeSummary) -> None:
        try:
            self._volume = summary
        except Exception:
            LOGGER.warning("telemetry record_volume failed", exc_info=True)

    def record_usage(self, summary: UsageSummary) -> None:
        try:
            self._usage = summary
        except Exception:
            LOGGER.warning("telemetry record_usage failed", exc_info=True)

    def record_pnl(self, summary: PnlSummary) -> None:
        try:
            self._pnl = summary
        except Exception:
            LOGGER.warning("telemetry record_pnl failed", exc_info=True)

    def load_klines(self, coin: str, bar: Literal["1m", "15m"], rows: Iterable[Mapping[str, float]]) -> None:
        try:
            if self._known(coin):
                self._charts[coin].load_klines(bar, rows)
        except Exception:
            LOGGER.warning("telemetry load_klines failed", exc_info=True)

    # -- reads --------------------------------------------------------------

    def latest_price(self, coin: str) -> PriceMessage | None:
        return self._latest_price.get(coin)

    def root_body(self) -> dict[str, Any]:
        latest = {coin: (to_wire(h[-1]) if h else None) for coin, h in self._history.items()}
        return {**to_wire(self._meta), "latestByCoin": latest}

    def volume_body(self) -> dict[str, Any]:
        """Volume figures for `GET /volume`; `{}` until the engine has published the first set."""
        return to_wire(self._volume) if self._volume is not None else {}

    def usage_body(self) -> dict[str, Any]:
        """Model token figures for `GET /usage`; `{}` until the engine has published the first set."""
        return to_wire(self._usage) if self._usage is not None else {}

    def pnl_body(self) -> dict[str, Any]:
        """Realized and unrealized for `GET /pnl`; `{}` until the observer has published the first pair."""
        return to_wire(self._pnl) if self._pnl is not None else {}

    def snapshot_body(self) -> dict[str, Any]:
        history = {coin: [to_wire(e) for e in clip_history(list(h))] for coin, h in self._history.items()}
        tape = {coin: [to_wire(p) for p in clip_snapshot_tape(c.points())] for coin, c in self._charts.items()}
        return {**to_wire(self._meta), "historyByCoin": history, "tapeByCoin": tape}

    def history_body(self) -> dict[str, list[dict[str, Any]]]:
        return {coin: [to_wire(e) for e in h] for coin, h in self._history.items()}

    def tape_body(self) -> dict[str, list[dict[str, Any]]]:
        return {coin: [to_wire(p) for p in clip_tape(c.points())] for coin, c in self._charts.items()}

    # -- internals ----------------------------------------------------------

    def _known(self, coin: str) -> bool:
        if coin in self._history:
            return True
        LOGGER.warning("telemetry dropped a message for unknown coin %s", coin)
        return False

    def _replace_block(self, coin: str, block: int, update: dict[str, Any]) -> None:
        history = self._history[coin]
        for index in range(len(history) - 1, -1, -1):
            if history[index].block == block:
                history[index] = history[index].model_copy(update=update)
                return
