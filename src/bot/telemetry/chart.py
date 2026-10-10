"""Chart tape for one symbol: venue candles, live 1 second candles and fill marks.

Ported from jev-trade's VenueChart (MIT). The dashboard builds its candles from this
tape, so the shape of `points()` is what it reads: `bar` tags candles, `fill` tags marks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from bot.telemetry.wire import PricePoint, PricePointFill

SECOND_MS = 1000
MAX_SECOND_BARS = 15 * 60
DEFAULT_MAX_MINUTE_BARS = 5000
DEFAULT_MAX_QUARTER_BARS = 1000

Bar = Literal["1s", "1m", "15m"]


@dataclass(frozen=True, slots=True)
class _Ohlc:
    ts: int
    open: float
    high: float
    low: float
    close: float


class VenueChart:
    def __init__(
        self,
        *,
        max_minute_bars: int = DEFAULT_MAX_MINUTE_BARS,
        max_quarter_bars: int = DEFAULT_MAX_QUARTER_BARS,
    ) -> None:
        self._caps: dict[str, int] = {"1m": max_minute_bars, "15m": max_quarter_bars}
        self._bars: dict[str, dict[int, _Ohlc]] = {"1s": {}, "1m": {}, "15m": {}}
        self._last_second: _Ohlc | None = None
        self._fills: list[PricePoint] = []
        self._seen_fills: set[tuple[int, str, float, float]] = set()
        self._cached: list[PricePoint] = []
        self._dirty = True

    def points(self) -> list[PricePoint]:
        """Time-sorted tape. The returned list is cached: callers must not mutate it."""
        if self._dirty:
            self._cached = self._build()
            self._dirty = False
        return self._cached

    def load_klines(self, bar: Literal["1m", "15m"], rows: Iterable[Mapping[str, float]]) -> None:
        """Load exchange klines (`timestamp` in milliseconds plus open/high/low/close)."""
        for row in rows:
            self.upsert_candle(bar, int(row["timestamp"]), row["open"], row["high"], row["low"], row["close"])

    def upsert_candle(self, bar: Bar, ts: int, open_: float, high: float, low: float, close: float) -> None:
        if not ts > 0 or not close > 0:
            return
        first = open_ if open_ > 0 else close
        top = high if high > 0 else max(first, close)
        bottom = low if low > 0 else min(first, close)
        candle = _Ohlc(ts, first, max(top, first, close), min(bottom, first, close), close)
        bars = self._bars[bar]
        if bars.get(ts) == candle:
            return
        bars[ts] = candle
        self._trim(bar)
        self._dirty = True

    def add_mid(self, mid: float, ts: int) -> None:
        """Fold a live mid into the forming 1 second candle."""
        if not mid > 0 or not ts > 0:
            return
        bucket = ts // SECOND_MS * SECOND_MS
        seconds = self._bars["1s"]
        previous = seconds.get(bucket)
        if previous is not None:
            high = max(previous.high, mid)
            low = min(previous.low, mid)
            if high == previous.high and low == previous.low and mid == previous.close:
                return
            candle = _Ohlc(bucket, previous.open, high, low, mid)
        else:
            last = self._last_second
            opening = last.close if last is not None and last.ts < bucket else mid
            candle = _Ohlc(bucket, opening, mid, mid, mid)
        seconds[bucket] = candle
        self._last_second = candle
        if len(seconds) > MAX_SECOND_BARS:
            cutoff = bucket - (MAX_SECOND_BARS - 1) * SECOND_MS
            for stale in [t for t in seconds if t < cutoff]:
                del seconds[stale]
        self._dirty = True

    def add_fill(
        self,
        *,
        ts: int,
        side: Literal["buy", "sell"],
        price: float,
        size: float,
        direction: Literal["open", "close", "flip"] | None = None,
        hash: str | None = None,
        closed_pnl: float | None = None,
        fee_usd: float | None = None,
    ) -> bool:
        """Mark a fill. Returns False for an unusable or already seen fill."""
        if not ts > 0 or not price > 0:
            return False
        key = (ts, side, price, size)
        if key in self._seen_fills:
            return False
        self._seen_fills.add(key)
        self._fills.append(
            PricePoint(
                ts=ts,
                mid=price,
                fill=PricePointFill(
                    side=side,
                    price=price,
                    size=size,
                    direction=direction,
                    hash=hash,
                    closed_pnl=closed_pnl,
                    fee_usd=fee_usd,
                ),
            )
        )
        self._dirty = True
        return True

    def _trim(self, bar: Bar) -> None:
        cap = self._caps.get(bar)
        bars = self._bars[bar]
        if cap is None or len(bars) <= cap:
            return
        for stale in sorted(bars)[: len(bars) - cap]:
            del bars[stale]

    def _build(self) -> list[PricePoint]:
        points: list[PricePoint] = []
        for bar in ("15m", "1m", "1s"):
            for candle in self._bars[bar].values():
                points.append(
                    PricePoint(
                        ts=candle.ts,
                        mid=candle.close,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        bar=bar,
                    )
                )
        points.extend(self._fills)
        points.sort(key=lambda p: (p.ts, 1 if p.fill else 0, 0 if p.bar == "15m" else 1))
        return points
